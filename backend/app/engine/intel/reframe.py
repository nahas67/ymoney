"""Smart reframing + multi-speaker layouts (Work 12 Lane G) -- contracts 11/12.

Everything here is LOCAL and STDLIB. There is no model in this module and none
is needed: the *framing math* is geometry, and a model (active speaker, salient
face) only ever INFORMS it. That split is the whole point of the lane:

    evidence (optional)  ->  priority resolution  ->  keyframes (editable)
                                                      ->  layout rects
                                                      ->  canonical timeline ops
                                                      ->  optional preview render

Without any model the plan still resolves -- it simply falls further down the
contract 11 priority chain and says so in every keyframe's ``source``:

    1. ``active_speaker``  a RESOLVED ``active_speaker_map`` row + its face box
    2. ``subject``         the most salient face (area x centrality)
    3. ``focal_point``     the operator-configured point
    4. ``fallback``        the safe centre crop inside the safe area

An UNRESOLVED active-speaker row is NOT evidence. It is a refusal, and
:func:`resolve_anchors` treats it exactly like "no evidence" -- a guess would
be a silent failure (contracts 10), not a feature.

Design rules that must not be relaxed:

* **Editable, never baked.** A plan is ``reframe_plans`` + ``reframe_keyframes``
  rows. Nothing renders a crop into a source file. The optional preview writes a
  NEW derived asset and never touches the source (contracts 0).
* **Jitter is a first-class output.** ``build_keyframes`` clamps movement per
  second and ramps across a speaker switch, so a switch is a transition with
  ``reason='transition:...'`` rows rather than a jump. QC (lane H) measures what
  these rows report, so the clamp has to be real arithmetic, not a label.
* **Canonical ops only.** A plan rides the Work 02 operation layer
  (``engine/timeline_ops.py``); the only types this module emits are members of
  that module's ``OP_TYPES``. There is no second edit path.
* **Honest unavailability.** Background blur/replace/mask consume lane F's
  ``mask_assets`` rows. With no mask row -- i.e. no segmentation provider --
  they return ``status='UNAVAILABLE'`` with a reason. They never invent a mask.
* **No new event kinds from here.** This module never calls ``record_event`` or
  ``track_cost``: both open their own session, so calling them mid-transaction
  deadlocks the SQLite writer (contracts 3). Emissions are RETURNED in the
  result payload and the ROUTE publishes them after ``db.commit()``.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.intel.ffmpeg_util import ffmpeg_available, probe, run_filter
from app.engine.timeline_ops import OP_TYPES
from app.models import (
    ActiveSpeakerMap,
    FaceTrack,
    FaceTrackSample,
    MaskAsset,
    MediaAsset,
    ReframeKeyframe,
    ReframePlan,
)

logger = logging.getLogger("ymoney.intel")

# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------

#: source aspects this lane is specified for (contracts 11: 16:9 -> these)
TARGET_ASPECTS: tuple[str, ...] = ("9:16", "4:5", "1:1")
#: the 16:9 source aspect the provider is specified against
SOURCE_ASPECT = "16:9"

#: contracts 11 layout vocabulary. Every one of these produces per-slot rects
#: AND the canonical operations that would realise them.
LAYOUTS: tuple[str, ...] = (
    "ACTIVE_SPEAKER",
    "SPLIT_SCREEN",
    "TWO_SHOT",
    "GRID",
    "HOST_GUEST",
    "PODCAST_DYNAMIC",
)

#: contracts 11 priority chain, most trusted first. The value written to
#: ``reframe_keyframes.source`` IS one of these, so the chosen level is
#: recoverable per keyframe without re-deriving anything.
KEYFRAME_SOURCES: tuple[str, ...] = (
    "active_speaker",
    "subject",
    "focal_point",
    "fallback",
)
#: appended by :func:`update_keyframe` when an operator moves a keyframe, so a
#: re-plan can tell a machine row from a human one.
SOURCE_OPERATOR = "operator"
#: every value ``source`` may hold, in priority order then operator.
ALL_SOURCES: tuple[str, ...] = (*KEYFRAME_SOURCES, SOURCE_OPERATOR)

#: ``models/media_intel.py``'s ``ReframeKeyframe`` docstring spells two levels
#: differently (``salient_face`` / ``fallback_crop``). The column is a free-form
#: ``String(40)``, so both readings can be stored; the brief's spelling is
#: canonical on write and these aliases are accepted on read.
SOURCE_ALIASES: dict[str, str] = {
    "salient_face": "subject",
    "fallback_crop": "fallback",
    "safe_center_crop": "fallback",
    "configured_focal_point": "focal_point",
    "active_speaker_resolved": "active_speaker",
}

#: contracts 12 background operations
BACKGROUND_OPS: tuple[str, ...] = (
    "background_blur",
    "background_replace",
    "person_mask",
    "object_mask",
)

#: default safe-area margin as a fraction of the source frame. A fallback crop
#: stays inside this so a subject at the very edge is never half-cut by default.
DEFAULT_SAFE_AREA = 0.08
#: default max crop movement per second, as a fraction of source WIDTH. A 16:9
#: frame may not swing more than ~1/4 of its width per second.
DEFAULT_MAX_MOVE_PER_S = 0.25
#: default speaker-switch transition length in seconds.
DEFAULT_TRANSITION_S = 0.4
#: default number of interpolated rows a transition is broken into.
DEFAULT_RAMP_STEPS = 4
#: exponential smoothing factor for the anchor walk (0 = frozen, 1 = raw).
DEFAULT_SMOOTHING = 0.35
#: How far from ``t`` a face sample may be and still describe that instant.
#: Must exceed the face-tracker's own sampling interval: contracts 8 samples
#: face tracks at 2 fps by default (0.5 s apart), so a tighter tolerance would
#: reject EVERY sample from a correctly-behaving tracker.
DEFAULT_SAMPLE_GAP_S = 0.75
#: default output height for a rendered preview. Width follows the aspect.
DEFAULT_OUT_HEIGHT = 1920
#: preview filename stem prefix
DERIVATION_KIND = "reframe_preview"

#: Anchor written into every ``reframe_keyframes.rect_json`` and recorded on the
#: plan. HARD INTEROP CONTRACT with lane H (contracts 13): ``x``/``y`` are the
#: crop CENTRE, never the top-left corner, and ``app.engine.intel.qc`` reads them
#: with ``KEYFRAME_ANCHOR == "center"``. Kept as a local constant rather than
#: imported from ``qc`` so this module has no dependency on the QC lane (lanes
#: land in parallel and QC may be absent); the two are asserted equal in
#: ``tests/test_reframe_layouts.py``.
KEYFRAME_ANCHOR = "center"

_REASON_MAX = 60
#: ``scale`` is a ZOOM factor (1.0 = the whole source frame, >1 = tighter), so
#: the usable range starts at 1.0: a value below it would ask the renderer for
#: more frame than exists. The upper bound is a 16x punch-in.
_ZOOM_MIN = 1.0
_ZOOM_MAX = 16.0
#: a confidence outside this range is a bug, not a measurement
_CONFIDENCE_MIN = 0.0
_CONFIDENCE_MAX = 1.0

#: activity-feed event kinds. Reported to the orchestrator for the
#: ``services/webhooks.WEBHOOK_EVENTS`` allowlist; never emitted from here.
EVENT_PLAN_CREATED = "MEDIA_INTEL_REFRAME_PLAN_CREATED"
EVENT_KEYFRAME_EDITED = "MEDIA_INTEL_REFRAME_KEYFRAME_EDITED"
EVENT_BACKGROUND_APPLIED = "MEDIA_INTEL_BACKGROUND_APPLIED"
EVENT_BACKGROUND_UNAVAILABLE = "MEDIA_INTEL_BACKGROUND_UNAVAILABLE"


class ReframeError(ValueError):
    """Domain error for an unusable reframe request (routes answer 422)."""


# ---------------------------------------------------------------------------
# aspect geometry (pure, no I/O)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AspectGeometry:
    """Crop + output rectangles for one (source, target aspect) pair.

    ``crop_w``/``crop_h`` are EVEN pixels: every encoder this lane may invoke
    (libx264/yuv420p, and the PNG path for masks) rejects odd dimensions, so an
    odd crop would fail at render time instead of at plan time.
    """

    source_width: int
    source_height: int
    aspect: str
    crop_width: int
    crop_height: int
    out_width: int
    out_height: int

    def to_dict(self) -> dict:
        return {
            "source_width": self.source_width,
            "source_height": self.source_height,
            "aspect": self.aspect,
            "crop_width": self.crop_width,
            "crop_height": self.crop_height,
            "out_width": self.out_width,
            "out_height": self.out_height,
            "crop_aspect": round(self.crop_width / self.crop_height, 6),
            "out_aspect": round(self.out_width / self.out_height, 6),
            "max_x": max(0, self.source_width - self.crop_width),
            "max_y": max(0, self.source_height - self.crop_height),
        }


def aspect_ratio(aspect: str) -> float:
    """Width / height of an ``"W:H"`` aspect string.

    Raises :class:`ReframeError` for anything not in :data:`TARGET_ASPECTS`, so
    a typo is a 422 rather than a silently wrong crop.
    """
    name = str(aspect or "").strip()
    if name not in TARGET_ASPECTS:
        raise ReframeError(
            f"aspect must be one of {', '.join(TARGET_ASPECTS)}; got {aspect!r}"
        )
    width, _, height = name.partition(":")
    return float(width) / float(height)


def _even(value: float, *, minimum: int = 2) -> int:
    """Largest even integer <= ``value`` (never below ``minimum``)."""
    out = int(math.floor(value))
    if out % 2:
        out -= 1
    return max(minimum, out)


def target_geometry(
    source_width: int,
    source_height: int,
    aspect: str = "9:16",
    *,
    out_height: int | None = None,
) -> AspectGeometry:
    """Largest crop of ``aspect`` that fits the source, plus the output frame.

    The crop is the *maximum* rect of the target aspect inside the source, so a
    16:9 frame cropped to 9:16 keeps full source height and takes the narrowest
    possible width. The output frame is ``aspect`` at ``out_height`` (default
    :data:`DEFAULT_OUT_HEIGHT`).
    """
    src_w = int(source_width or 0)
    src_h = int(source_height or 0)
    if src_w <= 0 or src_h <= 0:
        raise ReframeError("source dimensions must be positive")
    ratio = aspect_ratio(aspect)
    if src_w / src_h > ratio:
        crop_h = _even(src_h)
        crop_w = _even(crop_h * ratio)
    else:
        crop_w = _even(src_w)
        crop_h = _even(crop_w / ratio)
    # a rounded crop must never exceed the source on the other axis
    crop_w = min(crop_w, _even(src_w))
    crop_h = min(crop_h, _even(src_h))
    height = int(out_height or DEFAULT_OUT_HEIGHT)
    height = max(2, height - (height % 2))
    out_w = _even(height * ratio)
    return AspectGeometry(
        source_width=src_w,
        source_height=src_h,
        aspect=aspect,
        crop_width=crop_w,
        crop_height=crop_h,
        out_width=out_w,
        out_height=height,
    )


def crop_from_center(
    geometry: AspectGeometry,
    center_x: float,
    center_y: float,
    *,
    safe_area: float = 0.0,
) -> tuple[int, int]:
    """Top-left crop origin for a target centre, clamped inside the frame.

    The frame bound is always enforced. ``safe_area`` (a margin fraction) is
    applied ONLY by priority level 4 -- the safe fallback. Applying it to every
    level would refuse to crop a subject at the frame edge, which is precisely
    the job a smart reframe exists to do.
    """
    max_x = max(0, geometry.source_width - geometry.crop_width)
    max_y = max(0, geometry.source_height - geometry.crop_height)
    raw_x = float(center_x) - geometry.crop_width / 2.0
    raw_y = float(center_y) - geometry.crop_height / 2.0
    return (
        int(round(min(max(raw_x, 0.0), float(max_x)))),
        int(round(min(max(raw_y, 0.0), float(max_y)))),
    )


def crop_from_safe_center(
    geometry: AspectGeometry,
    center_x: float,
    center_y: float,
    *,
    safe_area: float = DEFAULT_SAFE_AREA,
) -> tuple[int, int]:
    """Level 4 only: a crop origin confined to the safe area.

    The fallback subject is the frame centre, so the origin lands mid-travel.
    Confining it to the safe rectangle is what makes the fallback SAFE: the
    window never rests with a hard edge at the frame border.
    """
    max_x = max(0, geometry.source_width - geometry.crop_width)
    max_y = max(0, geometry.source_height - geometry.crop_height)
    margin = max(0.0, min(0.45, float(safe_area or 0.0)))
    lo_x = math.floor(max_x * margin)
    lo_y = math.floor(max_y * margin)
    hi_x = math.ceil(max_x * (1.0 - margin))
    hi_y = math.ceil(max_y * (1.0 - margin))
    raw_x = float(center_x) - geometry.crop_width / 2.0
    raw_y = float(center_y) - geometry.crop_height / 2.0
    return (
        int(round(min(max(raw_x, float(lo_x)), float(hi_x)))),
        int(round(min(max(raw_y, float(lo_y)), float(hi_y)))),
    )


def scale_for(geometry: AspectGeometry) -> float:
    """ZOOM factor written to ``reframe_keyframes.scale`` (Lane H contract).

    1.0 shows the whole source frame; a value > 1 is a tighter crop. A consumer
    recovers the visible width fraction as ``1/scale``, which is exactly what
    :func:`app.engine.intel.qc.crop_rect_at` does when it falls back to the
    ``x``/``y``/``scale`` columns. A 9:16 crop of a 16:9 frame is
    ``320/100 = 3.2``.

    This is the RECIPROCAL of the visible-width fraction, which is the trap
    worth documenting: writing the fraction (0.3125) makes QC read a full-frame
    width and every measurement wrong.
    """
    fraction = geometry.crop_width / float(geometry.source_width)
    return round(1.0 / fraction, 6) if fraction > 0 else 1.0


# ---------------------------------------------------------------------------
# evidence + anchors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameBox:
    """One detection box in SOURCE pixels at one instant."""

    t_s: float
    x: float
    y: float
    w: float
    h: float
    label: str = ""
    confidence: float | None = None

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2.0

    def centrality(self, geometry: AspectGeometry) -> float:
        """1.0 dead centre, 0.0 on the frame edge (contract 11 "area + centrality")."""
        cx = geometry.source_width / 2.0
        cy = geometry.source_height / 2.0
        norm = math.sqrt((geometry.source_width / 2.0) ** 2 + (geometry.source_height / 2.0) ** 2)
        return max(0.0, 1.0 - math.dist((self.center_x, self.center_y), (cx, cy)) / norm)


@dataclass(frozen=True)
class Anchor:
    """A resolved framing target for one instant, plus WHY it was chosen."""

    t_s: float
    center_x: float
    center_y: float
    source: str
    reason: str
    confidence: float | None = None
    anchor_id: str = ""
    box: FrameBox | None = None


def _nearest_box(
    boxes: Sequence[FrameBox],
    t_s: float,
    *,
    max_gap: float,
) -> FrameBox | None:
    """Box whose timestamp is closest to ``t_s`` within ``max_gap`` (None if none)."""
    best: FrameBox | None = None
    best_gap = float("inf")
    for box in boxes:
        gap = abs(box.t_s - t_s)
        if gap <= max_gap and gap < best_gap:
            best, best_gap = box, gap
    return best


def load_evidence(
    db: Session,
    workspace_id: str,
    asset_id: str,
    *,
    track_run_id: str | None = None,
) -> dict:
    """Load active-speaker + face evidence for one asset (workspace-scoped).

    Returns ``{"active_speaker": [...], "face_boxes": {label: [FrameBox]}}``.
    An ``UNRESOLVED`` active-speaker row is returned separately under
    ``unresolved`` and is never usable as framing evidence.

    UNIT CONTRACT (Lane E): ``face_track_samples.x/y/w/h`` are in the SOURCE
    FRAME'S PIXEL SPACE -- Lane E's ``normalize_detection`` clips a detector box
    to the frame, it does not divide by the frame size. Values are therefore
    passed through UNCHANGED into :class:`FrameBox` (documented "in SOURCE
    pixels"), and the conversion to crop coordinates happens downstream in
    :func:`crop_from_center` / :func:`_origin` against
    ``geometry.source_width`` / ``source_height``. Nothing here assumes
    normalised input: reading a pixel box as 0..1 collapses every face to a
    sub-pixel speck in the corner and the crop follows nothing.

    PROVENANCE CONTRACT (Lane E/F): ``unresolved_crossing`` is NOT a
    ``face_tracks`` column -- there is no such column -- and this lane does not
    read one. Lane E persists the flagged track LABELS into
    ``media_intel_runs.metrics_json["unresolved_crossings"]`` and Lane F
    deliberately keeps them as provenance: contracts 10 sets no threshold, so a
    crossing must not demote a resolved speaker or penalise its confidence.
    A crossed track is framed exactly like any other here, on purpose.
    """
    asm_query = select(ActiveSpeakerMap).where(
        ActiveSpeakerMap.workspace_id == str(workspace_id),
        ActiveSpeakerMap.asset_id == str(asset_id),
    )
    tracks_query = select(FaceTrack).where(
        FaceTrack.workspace_id == str(workspace_id),
        FaceTrack.asset_id == str(asset_id),
    )
    if track_run_id:
        asm_query = asm_query.where(ActiveSpeakerMap.run_id == str(track_run_id))
        tracks_query = tracks_query.where(FaceTrack.run_id == str(track_run_id))
    speaker_rows = db.scalars(asm_query.order_by(ActiveSpeakerMap.start_s)).all()
    track_rows = db.scalars(tracks_query.order_by(FaceTrack.start_s)).all()

    track_by_id = {row.id: row for row in track_rows}
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for row in speaker_rows:
        payload = {
            "id": row.id,
            "run_id": row.run_id,
            "speaker_id": row.speaker_id or "",
            "face_track_id": row.face_track_id or "",
            "track_label": str(getattr(track_by_id.get(row.face_track_id or ""), "track_id", "") or ""),
            "start_s": float(row.start_s or 0.0),
            "end_s": float(row.end_s or 0.0),
            "confidence": row.confidence,
            "status": str(row.status or "UNRESOLVED"),
            "reason": str(row.reason or ""),
        }
        (resolved if payload["status"] == "RESOLVED" else unresolved).append(payload)

    boxes: dict[str, list[FrameBox]] = {}
    for track in track_rows:
        samples = db.scalars(
            select(FaceTrackSample)
            .where(
                FaceTrackSample.workspace_id == str(workspace_id),
                FaceTrackSample.run_id == track.run_id,
                FaceTrackSample.track_id == track.id,
            )
            .order_by(FaceTrackSample.t_s)
        ).all()
        label = str(track.track_id or track.id)
        boxes[label] = [
            FrameBox(
                t_s=float(s.t_s or 0.0),
                x=float(s.x or 0.0),
                y=float(s.y or 0.0),
                w=float(s.w or 0.0),
                h=float(s.h or 0.0),
                label=label,
                confidence=s.confidence,
            )
            for s in samples
            if float(s.w or 0.0) > 0 and float(s.h or 0.0) > 0
        ]
    return {
        "active_speaker": resolved,
        "unresolved": unresolved,
        "face_boxes": boxes,
    }


def resolve_anchors(
    geometry: AspectGeometry,
    evidence: dict,
    sample_times: Sequence[float],
    *,
    focal_point: tuple[float, float] | None = None,
    safe_area: float = DEFAULT_SAFE_AREA,
    max_gap: float = DEFAULT_SAMPLE_GAP_S,
) -> list[Anchor]:
    """Apply the contract 11 priority chain at every sample time.

    1. active speaker -- a ``RESOLVED`` row covering ``t`` whose face track has a
       box near ``t``. An UNRESOLVED row is skipped like no evidence at all.
    2. subject -- the most salient face box: ``area * centrality``, so a big
       off-centre face can outrank a small central one.
    3. focal point -- the operator's configured point (source pixels).
    4. fallback -- the centre of the safe area.
    """
    times = [float(t) for t in sample_times]
    if not times:
        return []
    resolved_rows = list(evidence.get("active_speaker") or [])
    boxes_by_label: dict[str, list[FrameBox]] = dict(evidence.get("face_boxes") or {})

    def _boxes_for(label: str) -> list[FrameBox]:
        return boxes_by_label.get(str(label or ""), [])

    def _fallback_center() -> tuple[float, float]:
        """Centre of the region the crop may legally travel in.

        Level 4 is a *safe* crop, not a blind centre crop: the origin bounds in
        :func:`crop_from_center` keep the window inside the safe area, so the
        centre of that travel range is the position a subject with no evidence
        is framed at.
        """
        return (
            geometry.source_width / 2.0,
            geometry.source_height / 2.0,
        )

    fallback_cx, fallback_cy = _fallback_center()
    anchors: list[Anchor] = []
    for t_s in times:
        chosen: Anchor | None = None
        for row in resolved_rows:
            if not (float(row["start_s"]) - 1e-9 <= t_s <= float(row["end_s"]) + 1e-9):
                continue
            box = _nearest_box(_boxes_for(row["track_label"]), t_s, max_gap=max_gap)
            if box is None and row["face_track_id"]:
                for label, candidate in boxes_by_label.items():
                    if str(row["face_track_id"]) in label:
                        box = _nearest_box(candidate, t_s, max_gap=max_gap)
                        break
            if box is None:
                continue
            chosen = Anchor(
                t_s=t_s,
                center_x=box.center_x,
                center_y=box.center_y,
                source="active_speaker",
                reason="active_speaker_resolved",
                confidence=row["confidence"],
                anchor_id=f"speaker:{row['speaker_id'] or row['id']}:{row['track_label']}",
                box=box,
            )
            break
        if chosen is None:
            best: tuple[float, FrameBox] | None = None
            for label, boxes in boxes_by_label.items():
                box = _nearest_box(boxes, t_s, max_gap=max_gap)
                if box is None:
                    continue
                score = (box.w * box.h) * box.centrality(geometry)
                if best is None or score > best[0]:
                    best = (score, box)
            if best is not None:
                box = best[1]
                chosen = Anchor(
                    t_s=t_s,
                    center_x=box.center_x,
                    center_y=box.center_y,
                    source="subject",
                    reason="salient_face_area_centrality",
                    confidence=box.confidence,
                    anchor_id=f"track:{box.label}",
                    box=box,
                )
        if chosen is None and focal_point is not None:
            chosen = Anchor(
                t_s=t_s,
                center_x=float(focal_point[0]),
                center_y=float(focal_point[1]),
                source="focal_point",
                reason="configured_focal_point",
                confidence=None,
                anchor_id="focal",
            )
        if chosen is None:
            chosen = Anchor(
                t_s=t_s,
                center_x=fallback_cx,
                center_y=fallback_cy,
                source="fallback",
                reason="safe_center_crop",
                confidence=None,
                anchor_id="safe",
            )
        anchors.append(chosen)
    return anchors


# ---------------------------------------------------------------------------
# keyframes: smoothing, movement clamp, speaker-switch ramp
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Keyframe:
    """One editable crop row, in the QC interop unit contract (Lane H).

    ``x``/``y`` are the crop CENTRE -- never the top-left corner -- and
    ``scale`` is a ZOOM factor (1.0 shows the whole frame, >1 is tighter), so a
    consumer recovers the visible width as ``1/scale``. ``rect_json`` is
    authoritative: it carries the same rect in NORMALISED (0..1) coordinates
    with an explicit ``anchor: "center"``, which is what
    :func:`app.engine.intel.qc.crop_rect_at` reads.

    Units are normalised, not source pixels. That is measured, not assumed:
    ``qc.check_excessive_crop_movement`` compares movement against
    ``CROP_MOVEMENT_CLAMP_PER_SEC = 0.25`` *normalised frame-widths per second*,
    so a pixel-valued rect measures ~136x over the clamp and HARD-FAILs every
    correct plan. ``rect_json["px"]`` keeps the source-pixel rect for the
    renderer, the preview graph and the UI.
    """

    t_s: float
    #: crop CENTRE, normalised to the source frame
    x: float
    y: float
    #: zoom factor: visible width fraction == 1/scale
    scale: float
    rect: dict
    confidence: float | None
    reason: str
    source: str
    anchor_id: str = ""
    transition: bool = False

    def to_row(self) -> dict:
        return {
            "t_s": round(float(self.t_s), 4),
            "x": round(float(self.x), 6),
            "y": round(float(self.y), 6),
            "scale": round(float(self.scale), 6),
            "rect_json": dict(self.rect),
            "confidence": self.confidence,
            "reason": str(self.reason)[:_REASON_MAX],
            "source": str(self.source)[:40],
        }


def _clamp_anchor_movement(
    anchors: Sequence[Anchor],
    *,
    max_move_per_s: float,
    smoothing: float,
    source_width: int,
) -> list[Anchor]:
    """Smoothing pass + max-movement-per-second clamp on the anchor WALK.

    ``max_move_per_s`` is a FRACTION of the source width per second (see
    :data:`DEFAULT_MAX_MOVE_PER_S`), converted to pixels here so the parameter
    means the same thing on a 320 px fixture and a 4K source.

    Smoothing first (an EMA towards the raw target), then a hard clamp on the
    per-second rate. The clamp is applied to the CENTRE, not the crop origin:
    clamping the origin would let the safe-area bound silently move the crop
    faster than the configured rate, which is exactly the jitter this exists to
    prevent.
    """
    if not anchors:
        return []
    cap = max(0.0, float(max_move_per_s)) * max(1, int(source_width))
    blend = min(1.0, max(0.0, float(smoothing)))
    out: list[Anchor] = []
    smooth_x = float(anchors[0].center_x)
    smooth_y = float(anchors[0].center_y)
    for index, anchor in enumerate(anchors):
        if index == 0:
            out.append(anchor)
            continue
        target_x = smooth_x + (float(anchor.center_x) - smooth_x) * blend
        target_y = smooth_y + (float(anchor.center_y) - smooth_y) * blend
        dt = max(1e-6, float(anchor.t_s) - float(anchors[index - 1].t_s))
        dx = target_x - out[-1].center_x
        dy = target_y - out[-1].center_y
        distance = math.hypot(dx, dy)
        limit = cap * dt
        if distance > limit > 0.0:
            ratio = limit / distance
            target_x = out[-1].center_x + dx * ratio
            target_y = out[-1].center_y + dy * ratio
        smooth_x, smooth_y = target_x, target_y
        out.append(replace(anchor, center_x=target_x, center_y=target_y))
    return out


def build_keyframes(
    anchors: Sequence[Anchor],
    geometry: AspectGeometry,
    *,
    safe_area: float = DEFAULT_SAFE_AREA,
    max_move_per_s: float = DEFAULT_MAX_MOVE_PER_S,
    transition_s: float = DEFAULT_TRANSITION_S,
    ramp_steps: int = DEFAULT_RAMP_STEPS,
    smoothing: float = DEFAULT_SMOOTHING,
) -> list[Keyframe]:
    """Turn resolved anchors into editable, jitter-controlled keyframes.

    A change of ``anchor_id`` (i.e. speaker A -> speaker B) is a TRANSITION, not
    a cut: ``ramp_steps`` interpolated rows with ``transition=True`` are
    inserted, spread over ``transition_s`` seconds, ending on the real anchor
    row. The destination rows keep the destination's ``source`` so the chosen
    priority level stays recoverable; the ``reason`` field is what marks them as
    ramp rows (``transition:...``).
    """
    if not anchors:
        return []
    walked = _clamp_anchor_movement(
        anchors, max_move_per_s=max_move_per_s, smoothing=smoothing,
        source_width=geometry.source_width,
    )
    steps = max(1, int(ramp_steps))
    span = max(0.0, float(transition_s))
    base_scale = scale_for(geometry)
    # Normalised constants for the QC unit contract (see :class:`Keyframe`).
    norm_w = geometry.crop_width / geometry.source_width
    norm_h = geometry.crop_height / geometry.source_height

    def _make_rect(px_x: int, px_y: int, **extra) -> dict:
        """Authoritative rect: NORMALISED, centre-anchored, plus a pixel copy.

        ``qc.crop_rect_at`` prefers ``rect_json`` and reads ``x``/``y`` as the
        centre when ``anchor != "top_left"``, so the normalised centre goes at
        the top level; the source-pixel rect is kept under ``px`` for the
        renderer, the preview graph and the UI.
        """
        cx = (px_x + geometry.crop_width / 2.0) / geometry.source_width
        cy = (px_y + geometry.crop_height / 2.0) / geometry.source_height
        return {
            "x": round(cx, 6),
            "y": round(cy, 6),
            "w": round(norm_w, 6),
            "h": round(norm_h, 6),
            "anchor": "center",
            "px": {
                "x": int(px_x),
                "y": int(px_y),
                "w": int(geometry.crop_width),
                "h": int(geometry.crop_height),
            },
            **extra,
        }

    def _origin(anchor: Anchor) -> tuple[int, int]:
        """Crop origin (source pixels) for one anchor.

        Priority: CONTAIN the subject, then centre, then bounds.

        A crop that is merely centred on a subject can still clip it whenever
        the crop is narrower than the travel -- and "the face is half out of
        frame" is the failure this whole lane exists to prevent, so a level 1-3
        anchor with a known box always yields a crop that CONTAINS that box.
        Only a level-4 anchor (no evidence at all) is confined to the safe
        area.
        """
        if anchor.source == "fallback":
            return crop_from_safe_center(
                geometry, anchor.center_x, anchor.center_y, safe_area=safe_area
            )
        x, y = crop_from_center(geometry, anchor.center_x, anchor.center_y)
        box = anchor.box
        if box is None:
            return (x, y)
        # shift the window just enough to contain the box, still inside the frame
        lo_x = int(math.floor(box.x + box.w - geometry.crop_width))
        hi_x = int(math.ceil(box.x))
        lo_y = int(math.floor(box.y + box.h - geometry.crop_height))
        hi_y = int(math.ceil(box.y))
        max_x = max(0, geometry.source_width - geometry.crop_width)
        max_y = max(0, geometry.source_height - geometry.crop_height)
        return (
            int(min(max(x, lo_x), hi_x, max_x)),
            int(min(max(y, lo_y), hi_y, max_y)),
        )

    ordered: list[Keyframe] = []
    for index, anchor in enumerate(walked):
        previous = walked[index - 1] if index else None
        switched = previous is not None and previous.anchor_id != anchor.anchor_id
        if previous is not None and switched and span > 0.0 and steps > 1:
            # The ramp must never be SHORTER than the rate limit allows, or the
            # interpolated rows would move faster than `max_move_per_s` and
            # reintroduce the very jitter the clamp removed. The travel time is
            # therefore max(transition_s, distance / cap).
            travel = math.dist(
                (float(previous.center_x), float(previous.center_y)),
                (float(anchor.center_x), float(anchor.center_y)),
            )
            cap_px_s = max_move_per_s * geometry.source_width
            needed = travel / cap_px_s if cap_px_s > 0 else 0.0
            ramp_span = max(span, needed)
            transition_start = max(0.0, float(anchor.t_s) - ramp_span)
            for step in range(1, steps):
                frac = step / steps
                t_s = transition_start + (float(anchor.t_s) - transition_start) * frac
                cx = float(previous.center_x) + (float(anchor.center_x) - float(previous.center_x)) * frac
                cy = float(previous.center_y) + (float(anchor.center_y) - float(previous.center_y)) * frac
                px_x, px_y = crop_from_center(geometry, cx, cy)
                ordered.append(
                    Keyframe(
                        t_s=t_s,
                        x=round((px_x + geometry.crop_width / 2.0) / geometry.source_width, 6),
                        y=round((px_y + geometry.crop_height / 2.0) / geometry.source_height, 6),
                        scale=base_scale,
                        rect=_make_rect(
                            px_x, px_y,
                            anchor_from=previous.anchor_id,
                            anchor_to=anchor.anchor_id,
                            transition=True,
                        ),
                        confidence=min(
                            value
                            for value in (previous.confidence, anchor.confidence)
                            if value is not None
                        )
                        if (previous.confidence is not None or anchor.confidence is not None)
                        else None,
                        reason=f"transition:{previous.anchor_id}->{anchor.anchor_id}"[:_REASON_MAX],
                        source=anchor.source,
                        anchor_id=anchor.anchor_id,
                        transition=True,
                    )
                )
        reason = anchor.reason
        if switched and span > 0.0:
            reason = f"switch:{reason}"[:_REASON_MAX]
        x, y = _origin(anchor)
        anchor_rect = _make_rect(x, y, anchor_id=anchor.anchor_id, transition=False)
        ordered.append(
            Keyframe(
                t_s=float(anchor.t_s),
                x=anchor_rect["x"],
                y=anchor_rect["y"],
                scale=base_scale,
                rect=anchor_rect,
                confidence=anchor.confidence,
                reason=reason,
                source=anchor.source,
                anchor_id=anchor.anchor_id,
            )
        )
    ordered.sort(key=lambda k: (k.t_s, k.transition))
    return _enforce_rate_on_emitted(ordered, geometry, max_move_per_s)


def _enforce_rate_on_emitted(
    keyframes: list[Keyframe],
    geometry: AspectGeometry,
    max_move_per_s: float,
) -> list[Keyframe]:
    """Final rate clamp applied to the NORMALISED centres that are persisted.

    The continuous walk is clamped in :func:`_clamp_anchor_movement`, but
    rounding a crop origin to whole pixels can add up to ~1 px per step, which
    on a short step is a large instantaneous rate. QC measures the emitted
    rows, so the guarantee has to hold for the emitted rows too: this pass
    walks them in time order and pulls any over-rate step back to the limit.

    It only ever moves a keyframe BACK towards its predecessor, so a keyframe
    can lag its target but never overshoot it. The pixel rect inside
    ``rect_json["px"]`` is updated with the normalised centre so the renderer
    and the two representations never disagree.
    """
    if len(keyframes) < 2:
        return keyframes
    # `max_move_per_s` is a fraction of source WIDTH per second (see
    # DEFAULT_MAX_MOVE_PER_S), which is exactly the unit QC measures in.
    cap = max(0.0, float(max_move_per_s))
    if cap <= 0.0:
        return keyframes
    half_w = geometry.crop_width / 2.0
    half_h = geometry.crop_height / 2.0
    max_px_x = max(0, geometry.source_width - geometry.crop_width)
    max_px_y = max(0, geometry.source_height - geometry.crop_height)
    out = list(keyframes)
    for index in range(1, len(out)):
        previous = out[index - 1]
        current = out[index]
        dt = float(current.t_s) - float(previous.t_s)
        if dt <= 0:
            continue
        dx = float(current.x) - float(previous.x)
        dy = float(current.y) - float(previous.y)
        distance = math.hypot(dx, dy)
        limit = cap * dt
        if distance <= limit or distance <= 0.0:
            continue
        ratio = limit / distance
        new_x = round(float(previous.x) + dx * ratio, 6)
        new_y = round(float(previous.y) + dy * ratio, 6)
        # re-derive the pixel rect from the clamped centre, kept in frame
        px_x = int(min(max(round(new_x * geometry.source_width - half_w), 0), max_px_x))
        px_y = int(min(max(round(new_y * geometry.source_height - half_h), 0), max_px_y))
        rect = dict(current.rect)
        rect["x"] = new_x
        rect["y"] = new_y
        px = dict(rect.get("px") or {})
        px.update({"x": px_x, "y": px_y,
                   "w": int(geometry.crop_width), "h": int(geometry.crop_height)})
        rect["px"] = px
        out[index] = replace(current, x=new_x, y=new_y, rect=rect)
    return out


def sample_times_for(
    duration_s: float,
    *,
    fps: float = 2.0,
    min_interval: float = 0.25,
) -> list[float]:
    """Even sample instants across a duration, always including 0 and the end.

    A minimum interval keeps a long clip from producing thousands of rows: a
    keyframe denser than the movement clamp can resolve is wasted precision, and
    an unbounded row count is a denial-of-service vector on a 2-hour source.
    """
    total = float(duration_s or 0.0)
    if total <= 0:
        return [0.0]
    rate = float(fps or 0.0)
    step = max(float(min_interval), 1.0 / rate if rate > 0 else float(min_interval))
    step = max(step, 1e-3)
    count = int(math.floor(total / step))
    times = [round(i * step, 4) for i in range(count + 1)]
    if times[-1] < total - 1e-6:
        times.append(round(total, 4))
    return sorted(dict.fromkeys(times))


def keyframe_pixel_rect(keyframe: Keyframe, geometry: AspectGeometry) -> dict:
    """The keyframe's crop rect in SOURCE PIXELS, top-left + size.

    The persisted ``x``/``y``/``rect_json`` are NORMALISED and centre-anchored
    (the Lane H contract), so every internal consumer that needs pixels -- the
    jitter metric, the layout slots, the timeline ops and the ffmpeg preview
    graph -- goes through this one accessor rather than re-deriving the unit
    conversion at each site.
    """
    rect = getattr(keyframe, "rect_json", None)
    if not isinstance(rect, dict):
        rect = getattr(keyframe, "rect", None) or {}
    px = dict(rect.get("px") or {})
    if {"x", "y", "w", "h"} <= px.keys():
        return {
            "x": int(px["x"]),
            "y": int(px["y"]),
            "w": int(px.get("w") or geometry.crop_width),
            "h": int(px.get("h") or geometry.crop_height),
        }
    # a hand-edited row whose rect_json lost `px`: recover it from the centre
    return {
        "x": int(min(max(round(float(keyframe.x) * geometry.source_width
                                  - geometry.crop_width / 2.0), 0),
                      max(0, geometry.source_width - geometry.crop_width))),
        "y": int(min(max(round(float(keyframe.y) * geometry.source_height
                                  - geometry.crop_height / 2.0), 0),
                      max(0, geometry.source_height - geometry.crop_height))),
        "w": int(geometry.crop_width),
        "h": int(geometry.crop_height),
    }


def keyframe_center_px(keyframe: Keyframe, geometry: AspectGeometry) -> tuple[float, float]:
    """The keyframe's crop CENTRE in source pixels (for the layout binding)."""
    rect = keyframe_pixel_rect(keyframe, geometry)
    return (rect["x"] + rect["w"] / 2.0, rect["y"] + rect["h"] / 2.0)


def measure_jitter(
    keyframes: Sequence[Keyframe], geometry: AspectGeometry | None = None
) -> dict:
    """Measured movement statistics for QC (contracts 13 "unstable crop").

    ``max_move_per_s`` is derived from the emitted rows, so QC compares a
    measurement against the plan rather than trusting the plan's own claim.
    """
    levels: dict[str, int] = {}
    for keyframe in keyframes:
        levels[keyframe.source] = levels.get(keyframe.source, 0) + 1
    if geometry is None or len(keyframes) < 2:
        return {
            "samples": len(keyframes),
            "max_move_px_per_s": None,
            "mean_move_px_per_s": None,
            "max_move_norm_per_s": None,
            "mean_move_norm_per_s": None,
            "total_move_px": None,
            "transitions": sum(1 for k in keyframes if k.transition),
            "levels": levels,
        }
    moves: list[float] = []
    total = 0.0
    for previous, current in zip(keyframes, keyframes[1:], strict=False):
        dt = float(current.t_s) - float(previous.t_s)
        if dt <= 0:
            continue
        a = keyframe_pixel_rect(previous, geometry)
        b = keyframe_pixel_rect(current, geometry)
        distance = math.dist(
            (a["x"] + a["w"] / 2.0, a["y"] + a["h"] / 2.0),
            (b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0),
        )
        total += distance
        moves.append(distance / dt)
    # the QC unit: fraction of the source WIDTH travelled per second
    norm = [m / max(1, geometry.source_width) for m in moves]
    return {
        "samples": len(keyframes),
        "max_move_px_per_s": round(max(moves), 4) if moves else 0.0,
        "mean_move_px_per_s": round(sum(moves) / len(moves), 4) if moves else 0.0,
        "max_move_norm_per_s": round(max(norm), 6) if norm else 0.0,
        "mean_move_norm_per_s": round(sum(norm) / len(norm), 6) if norm else 0.0,
        "total_move_px": round(total, 3),
        "transitions": sum(1 for k in keyframes if k.transition),
        "levels": levels,
    }


# ---------------------------------------------------------------------------
# layouts -> per-slot rects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One composited rectangle of a layout, in OUTPUT pixels.

    ``source_rect`` is the region of the source this slot shows; it is what the
    preview renders and what a ``crop`` transform carries onto the timeline.
    ``out_width``/``out_height`` are the frame this slot sits in, kept explicit
    so the normalised rect is honest even for a slot that does not reach the
    frame edge.
    """

    index: int
    label: str
    x: int
    y: int
    w: int
    h: int
    out_width: int
    out_height: int
    source_rect: dict
    role: str = "primary"
    tracks_subject: bool = True

    def to_dict(self) -> dict:
        out_w = max(1, int(self.out_width))
        out_h = max(1, int(self.out_height))
        return {
            "index": self.index,
            "label": self.label,
            "role": self.role,
            "x": int(self.x),
            "y": int(self.y),
            "w": int(self.w),
            "h": int(self.h),
            "norm_x": round(self.x / out_w, 6),
            "norm_y": round(self.y / out_h, 6),
            "norm_w": round(self.w / out_w, 6),
            "norm_h": round(self.h / out_h, 6),
            "source_rect": dict(self.source_rect),
            "tracks_subject": bool(self.tracks_subject),
        }


def _slot(
    index: int,
    label: str,
    geometry: AspectGeometry,
    out_x: float,
    out_y: float,
    out_w: float,
    out_h: float,
    source_rect: dict,
    *,
    role: str = "primary",
    tracks_subject: bool = True,
) -> Slot:
    x = int(round(out_x))
    y = int(round(out_y))
    # clamp inside the output frame so a rounded cell can never overhang it
    w = max(2, min(_even(out_w), _even(geometry.out_width - x)))
    h = max(2, min(_even(out_h), _even(geometry.out_height - y)))
    rect = {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "out_width": geometry.out_width,
        "out_height": geometry.out_height,
        **source_rect,
    }
    return Slot(
        index=index,
        label=label,
        x=x,
        y=y,
        w=w,
        h=h,
        out_width=geometry.out_width,
        out_height=geometry.out_height,
        source_rect=rect,
        role=role,
        tracks_subject=tracks_subject,
    )


def _source_crop(
    geometry: AspectGeometry,
    anchor: Anchor,
    *,
    safe_area: float,
) -> dict:
    """Source rect for one slot, following the same level-aware rule as
    :func:`build_keyframes`: a level-4 anchor is confined to the safe area, a
    tracked subject is not."""
    if anchor.source == "fallback":
        x, y = crop_from_safe_center(
            geometry, anchor.center_x, anchor.center_y, safe_area=safe_area
        )
    else:
        x, y = crop_from_center(geometry, anchor.center_x, anchor.center_y)
    return {"src_x": x, "src_y": y, "src_w": geometry.crop_width, "src_h": geometry.crop_height}


def _anchor_centers(
    keyframes: Sequence[Keyframe],
    geometry: AspectGeometry,
) -> list[Anchor]:
    """Crop centres recovered from keyframes, as :class:`Anchor` values.

    The ``source`` is carried through so the level-aware safe-area rule in
    :func:`_source_crop` still applies to a slot built from persisted rows.
    """
    out: list[Anchor] = []
    for keyframe in keyframes:
        out.append(
            Anchor(
                t_s=float(keyframe.t_s),
                center_x=float(keyframe.x) + geometry.crop_width / 2.0,
                center_y=float(keyframe.y) + geometry.crop_height / 2.0,
                source=keyframe.source,
                reason=keyframe.reason,
                confidence=keyframe.confidence,
                anchor_id=keyframe.anchor_id,
            )
        )
    return out


def _distinct_subjects(
    keyframes: Sequence[Keyframe],
    geometry: AspectGeometry,
) -> list[Anchor]:
    """One representative anchor per DISTINCT subject, in first-seen order.

    A multi-participant layout must map pane N to subject N, not to "the Nth
    keyframe": with one speaker on camera, the second pane would otherwise
    duplicate the hero's crop and the composition would show the same person
    twice. The representative is the subject's LAST anchor (where they end up).
    """
    order: list[str] = []
    latest: dict[str, Anchor] = {}
    for keyframe in keyframes:
        anchor = Anchor(
            t_s=float(keyframe.t_s),
            center_x=keyframe_center_px(keyframe, geometry)[0],
            center_y=keyframe_center_px(keyframe, geometry)[1],
            source=keyframe.source,
            reason=keyframe.reason,
            confidence=keyframe.confidence,
            anchor_id=keyframe.anchor_id or f"kf:{keyframe.t_s}",
        )
        if anchor.anchor_id not in latest:
            order.append(anchor.anchor_id)
        latest[anchor.anchor_id] = anchor
    return [latest[key] for key in order]


def _even_region(
    geometry: AspectGeometry,
    index: int,
    of: int,
) -> dict:
    """A distinct static region of the source for a slot with no subject.

    Used when a multi-slot layout has fewer tracked subjects than slots. Two
    slots must never show the identical region, or the composition is a bug
    rather than a layout.
    """
    max_x = max(0, geometry.source_width - geometry.crop_width)
    max_y = max(0, geometry.source_height - geometry.crop_height)
    if of <= 1:
        return {"src_x": max_x // 2, "src_y": max_y // 2,
                "src_w": geometry.crop_width, "src_h": geometry.crop_height}
    frac = index / float(of)
    return {
        "src_x": int(round(max_x * frac)),
        "src_y": int(round(max_y * (1.0 - frac))),
        "src_w": geometry.crop_width,
        "src_h": geometry.crop_height,
    }


def layout_slots(
    layout: str,
    geometry: AspectGeometry,
    keyframes: Sequence[Keyframe],
    *,
    participants: int = 2,
    safe_area: float = DEFAULT_SAFE_AREA,
    grid_rows: int = 2,
    host_ratio: float = 0.68,
) -> list[Slot]:
    """Per-slot rects for one of the six contracts 11 layouts.

    Every layout is a pure function of (geometry, keyframes, params), so the
    same plan always yields the same rects and the UI preview cannot disagree
    with what the timeline ops would build. ``ACTIVE_SPEAKER`` is the only
    single-slot layout: the other five are multi-participant compositions.

    Slot N is bound to the Nth DISTINCT subject (see :func:`_distinct_subjects`).
    A layout with more slots than tracked subjects fills the remainder with
    distinct static regions of the source, because two slots showing the same
    crop is a collapsed composition, not a layout.
    """
    name = str(layout or "").strip().upper()
    if name not in LAYOUTS:
        raise ReframeError(f"layout must be one of {', '.join(LAYOUTS)}; got {layout!r}")
    anchors = _anchor_centers(keyframes, geometry)
    # A level-4 anchor means "NO evidence", so it must never own a pane: two
    # slots bound to the same "safe" centre would show the identical crop.
    subjects = [a for a in _distinct_subjects(keyframes, geometry) if a.source != "fallback"]
    hero = anchors[-1] if anchors else Anchor(
        t_s=0.0,
        center_x=geometry.source_width / 2.0,
        center_y=geometry.source_height / 2.0,
        source="fallback",
        reason="safe_center_crop",
        anchor_id="safe",
    )
    count = max(1, min(8, int(participants or 1)))
    out_w, out_h = geometry.out_width, geometry.out_height
    assigned: list[tuple[int, int]] = []

    def _distinct(rect: dict) -> dict:
        """Nudge a filler region until it differs from every region already used."""
        max_x = max(0, geometry.source_width - geometry.crop_width)
        if (rect["src_x"], rect["src_y"]) in assigned:
            step = max(2, _even(max_x / 4.0)) or 2
            for bump in range(1, 5):
                candidate = int(min(max_x, rect["src_x"] + step * bump))
                if (candidate, rect["src_y"]) not in assigned:
                    rect = {**rect, "src_x": candidate}
                    break
        assigned.append((rect["src_x"], rect["src_y"]))
        return rect

    def crop_at(index: int, inset: float = 1.0) -> dict:
        if index < len(subjects):
            rect = _source_crop(geometry, subjects[index], safe_area=safe_area)
        elif index == 0 and hero.source != "fallback":
            # the hero always shows the live subject
            rect = _source_crop(geometry, hero, safe_area=safe_area)
        else:
            # no subject for this slot: a distinct static region
            rect = _distinct(_even_region(geometry, index, count))
        if inset != 1.0:
            width = max(2, _even(rect["src_w"] * inset))
            height = max(2, _even(rect["src_h"] * inset))
            rect = {
                "src_x": max(0, int(rect["src_x"] - (width - rect["src_w"]) / 2)),
                "src_y": max(0, int(rect["src_y"] - (height - rect["src_h"]) / 2)),
                "src_w": min(width, geometry.source_width),
                "src_h": min(height, geometry.source_height),
            }
        return rect

    if name == "ACTIVE_SPEAKER":
        return [
            _slot(0, "active_speaker", geometry, 0, 0, out_w, out_h, crop_at(0),
                  role="hero")
        ]

    if name in {"SPLIT_SCREEN", "TWO_SHOT"}:
        # A square frame splits HORIZONTALLY: two 9:16 panes stacked would each
        # be half as tall as wide, which is not a split of the output at all.
        # TWO_SHOT widens each pane's crop (inset) so a neighbour stays partly
        # in frame; that is its whole difference from SPLIT_SCREEN.
        frame_w, frame_h = geometry.out_width, geometry.out_height
        vertical = frame_h > frame_w
        inset = 1.35 if name == "TWO_SHOT" else 1.0
        label = "speaker" if name == "SPLIT_SCREEN" else "two_shot"
        slots = []
        for index in range(2):
            if vertical:
                span = frame_h / 2.0
                out_x, out_y, slot_w, slot_h = 0.0, span * index, frame_w, span
            else:
                span = frame_w / 2.0
                out_x, out_y, slot_w, slot_h = span * index, 0.0, span, frame_h
            slots.append(
                _slot(
                    index,
                    f"{label}_{index}",
                    geometry,
                    out_x,
                    out_y,
                    slot_w,
                    slot_h,
                    crop_at(index, inset=inset),
                    role="pane",
                )
            )
        return slots

    if name == "GRID":
        rows = max(1, int(grid_rows or 1))
        cols = max(1, int(math.ceil(count / rows)))
        cell_w = out_w / cols
        cell_h = out_h / rows
        slots: list[Slot] = []
        for index in range(count):
            row, col = divmod(index, cols)
            slots.append(
                _slot(
                    index,
                    f"grid_{index}",
                    geometry,
                    cell_w * col,
                    cell_h * row,
                    cell_w,
                    cell_h,
                    crop_at(index),
                    role="tile",
                )
            )
        return slots

    if name == "HOST_GUEST":
        host_h = out_h * max(0.5, min(0.9, float(host_ratio)))
        pip_w = out_w * 0.34
        pip_h = pip_w * 0.75
        return [
            _slot(0, "host", geometry, 0, 0, out_w, host_h, crop_at(0), role="hero"),
            _slot(
                1,
                "guest",
                geometry,
                out_w - pip_w - int(out_w * 0.04),
                out_h - pip_h - int(out_h * 0.04),
                pip_w,
                pip_h,
                crop_at(1),
                role="pip",
            ),
        ]

    # PODCAST_DYNAMIC: the hero slot is the full frame and RETARGETS to the
    # active speaker over time; the other participants share a bottom rail.
    hero_h = out_h * 0.72
    rail_count = max(1, count - 1)
    rail_h = out_h - hero_h
    rail_w = out_w / rail_count
    slots = [_slot(0, "dynamic_hero", geometry, 0, 0, out_w, hero_h, crop_at(0), role="hero")]
    for index in range(rail_count):
        slots.append(
            _slot(
                index + 1,
                f"rail_{index + 1}",
                geometry,
                rail_w * index,
                hero_h,
                rail_w,
                rail_h,
                crop_at(index + 1),
                role="rail",
            )
        )
    return slots


# ---------------------------------------------------------------------------
# canonical timeline operations
# ---------------------------------------------------------------------------


def layout_ops(
    layout: str,
    slots: Sequence[Slot],
    keyframes: Sequence[Keyframe],
    *,
    geometry: AspectGeometry,
    track: str = "video",
    tracks: Sequence[str] = (),
    clip_ids: Sequence[str] = (),
    duration_s: float = 0.0,
    asset_id: str = "",
    clip_prefix: str = "reframe",
) -> list[dict]:
    """Canonical Work 02 operations that would realise a layout.

    Only ``OP_TYPES`` members are emitted (asserted at the end of this
    function, not merely by convention), because a plan that reaches the editor
    through a private op type would be a second edit path -- exactly what
    contracts 21 forbids.

    With no ``clip_ids`` the plan ADDS one clip per slot (``add_item``) and then
    drives the hero clip's crop with one ``update_transform`` per keyframe,
    which is the canonical realisation of "reframe plans become
    update_transform". Supplying ``clip_ids`` targets existing clips instead --
    no ``add_item`` is emitted -- so re-planning an already populated timeline
    moves the existing clips rather than duplicating them.
    """
    if not slots:
        return []
    if not (0.0 < float(duration_s or 0.0) < 24.0 * 60.0):
        raise ReframeError("layout_ops needs a positive duration in seconds")
    if clip_ids and len(clip_ids) != len(slots):
        raise ReframeError("clip_ids must have one id per slot")
    track_pool: list[str] = []
    for index in range(len(slots)):
        track_pool.append(str(tracks[index]) if index < len(tracks) else track)

    ops: list[dict] = []
    clip_ids = list(clip_ids)
    for index, slot in enumerate(slots):
        slot_track = track_pool[index]
        if clip_ids:
            continue
        clip_id = f"{clip_prefix}_{slot.label}_{index}"
        clip_ids.append(clip_id)
        ops.append(
            {
                "type": "add_item",
                "track": slot_track,
                "clip": {
                    "id": clip_id,
                    "name": f"{layout} {slot.label}",
                    "start": 0.0,
                    "duration": round(float(duration_s), 4),
                    "source": {"asset_id": str(asset_id or "")},
                    "transform": {
                        "crop": {
                            "x": int(slot.source_rect["src_x"]),
                            "y": int(slot.source_rect["src_y"]),
                            "w": int(slot.source_rect["src_w"]),
                            "h": int(slot.source_rect["src_h"]),
                        }
                    },
                },
            }
        )

    # the hero slot is the one the plan's keyframes drive; static layouts that
    # are not a single crop (GRID) have nothing to animate per keyframe.
    hero_clip = clip_ids[0]
    hero_track = track_pool[0]
    if keyframes and len(slots) == 1:
        for keyframe in keyframes:
            px = keyframe_pixel_rect(keyframe, geometry)
            cx = px["x"] + px["w"] / 2.0
            cy = px["y"] + px["h"] / 2.0
            ops.append(
                {
                    "type": "update_transform",
                    "track": hero_track,
                    "clip_id": hero_clip,
                    "transform": {
                        # the TIMELINE transform is in SOURCE PIXELS (the unit
                        # ffmpeg's crop=w:h:x:y and the preview graph use), not
                        # the normalised QC unit of the keyframe row
                        "x": cx,
                        "y": cy,
                        "scale": px["w"] / max(1, geometry.source_width),
                        "crop": {
                            "x": px["x"],
                            "y": px["y"],
                            "w": px["w"],
                            "h": px["h"],
                            "anchor": "top_left",
                        },
                    },
                }
            )
    illegal = sorted({op["type"] for op in ops} - set(OP_TYPES))
    if illegal:  # pragma: no cover - guards a future edit of this function
        raise ReframeError(f"layout_ops emitted non-canonical op types: {illegal}")
    return ops


# ---------------------------------------------------------------------------
# persistence (editable rows, never a baked crop)
# ---------------------------------------------------------------------------


def _iso(value) -> str | None:
    return (value.isoformat() + "Z") if value else None


def keyframe_dto(row: ReframeKeyframe) -> dict:
    """One editable keyframe row as the API/UI sees it."""
    rect = dict(row.rect_json or {})
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "workspace_id": row.workspace_id,
        "t_s": float(row.t_s or 0.0),
        "x": float(row.x or 0.0),
        "y": float(row.y or 0.0),
        "scale": float(row.scale or 1.0),
        "rect": rect,
        "confidence": row.confidence,
        "reason": str(row.reason or ""),
        "source": str(row.source or ""),
        "transition": bool(rect.get("transition")),
        "operator_edited": str(row.source or "") == SOURCE_OPERATOR,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def plan_dto(plan: ReframePlan, keyframes: Sequence[ReframeKeyframe] | None = None) -> dict:
    """Plan + its editable keyframes, plus the recorded geometry and metrics."""
    rows = list(keyframes if keyframes is not None else [])
    meta = dict(plan.meta_json or {})
    return {
        "id": plan.id,
        "workspace_id": plan.workspace_id,
        "run_id": plan.run_id,
        "source_asset_id": plan.source_asset_id,
        "layout": str(plan.layout or ""),
        "aspect": str(plan.aspect or ""),
        "strategy": str(plan.strategy or ""),
        "meta": meta,
        "geometry": meta.get("geometry") or {},
        "jitter": meta.get("jitter") or {},
        "slots": meta.get("slots") or [],
        "ops": meta.get("ops") or [],
        "editable": True,
        "keyframe_count": len(rows),
        "keyframes": [keyframe_dto(row) for row in rows],
        "created_at": _iso(plan.created_at),
        "updated_at": _iso(plan.updated_at),
    }


def get_plan(db: Session, workspace_id: str, plan_id: str) -> ReframePlan | None:
    """Workspace-scoped plan fetch. A foreign id is ``None`` (-> 404, never 403)."""
    if not plan_id:
        return None
    row = db.get(ReframePlan, str(plan_id))
    if row is None or row.workspace_id != str(workspace_id):
        return None
    return row


def list_keyframes(db: Session, plan_id: str) -> list[ReframeKeyframe]:
    return list(
        db.scalars(
            select(ReframeKeyframe)
            .where(ReframeKeyframe.plan_id == str(plan_id))
            .order_by(ReframeKeyframe.t_s, ReframeKeyframe.created_at)
        ).all()
    )


def _append_history(
    plan: ReframePlan,
    entry: dict,
    *,
    limit: int = 200,
) -> None:
    """Append to the plan's edit history. History is append-only, never rewritten."""
    meta = dict(plan.meta_json or {})
    history = list(meta.get("keyframe_history") or [])
    history.append(entry)
    meta["keyframe_history"] = history[-limit:]
    plan.meta_json = meta


def update_keyframe(
    db: Session,
    workspace_id: str,
    keyframe_id: str,
    patch: dict,
    *,
    actor_id: str | None = None,
) -> tuple[ReframeKeyframe, dict]:
    """Apply an operator edit to one keyframe. This is what makes plans EDITABLE.

    The row is updated in place, the previous values are copied into the plan's
    append-only ``keyframe_history``, and the row's ``source`` becomes
    ``operator`` so a later re-plan can tell a human row from a machine one.
    Nothing here re-crops any media: the source file is not touched, and the
    next preview/render reads these rows.
    """
    row = db.get(ReframeKeyframe, str(keyframe_id or ""))
    if row is None or row.workspace_id != str(workspace_id):
        raise KeyError("keyframe not found")
    if not isinstance(patch, dict) or not patch:
        raise ReframeError("keyframe patch must be a non-empty object")

    plan = get_plan(db, workspace_id, row.plan_id)
    geometry = dict((plan.meta_json if plan else {}).get("geometry") or {})
    before = keyframe_dto(row)
    changes: dict[str, Any] = {}

    if "t_s" in patch:
        t_s = float(patch["t_s"])
        if t_s < 0.0:
            raise ReframeError("t_s must be >= 0")
        changes["t_s"] = t_s
    for axis in ("x", "y"):
        if axis in patch:
            value = float(patch[axis])
            # x/y are the crop CENTRE, NORMALISED to 0..1 (Lane H contract)
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                raise ReframeError(
                    f"{axis} must be a normalised crop centre between 0 and 1"
                )
            changes[axis] = round(value, 6)
    if "scale" in patch:
        scale = float(patch["scale"])
        # scale is a ZOOM factor: 1.0 = whole frame, >1 tighter. A value < 1
        # would ask the renderer to show more than the source has.
        if not (_ZOOM_MIN <= scale <= _ZOOM_MAX):
            raise ReframeError(
                f"scale is a zoom factor and must be between {_ZOOM_MIN} and {_ZOOM_MAX}"
            )
        changes["scale"] = round(scale, 6)
    if "confidence" in patch:
        value = patch["confidence"]
        if value is None:
            changes["confidence"] = None
        else:
            confidence = float(value)
            if not (_CONFIDENCE_MIN <= confidence <= _CONFIDENCE_MAX):
                raise ReframeError("confidence must be between 0 and 1")
            changes["confidence"] = confidence
    if "reason" in patch:
        reason = str(patch["reason"] or "").strip()
        if not reason:
            raise ReframeError("reason must not be empty")
        changes["reason"] = reason[:_REASON_MAX]
    if "rect" in patch:
        rect = patch["rect"]
        if not isinstance(rect, dict):
            raise ReframeError("rect must be an object")
        merged = dict(row.rect_json or {})
        merged.update(rect)
        # an operator-supplied rect must keep the anchor and the unit contract
        merged["anchor"] = KEYFRAME_ANCHOR
        for key in ("x", "y", "w", "h"):
            if key in merged:
                try:
                    merged[key] = float(merged[key])
                except (TypeError, ValueError) as exc:
                    raise ReframeError(f"rect.{key} must be a number") from exc
                if not (0.0 <= merged[key] <= 1.0):
                    raise ReframeError(
                        f"rect.{key} must be a normalised value between 0 and 1"
                    )
        # the pixel copy is derived, not operator-authored: rebuild it from the
        # (possibly edited) normalised centre so the two never disagree
        if geometry.get("source_width") and geometry.get("crop_width"):
            source_w = int(geometry["source_width"])
            source_h = int(geometry["source_height"])
            crop_w = int(geometry["crop_width"])
            crop_h = int(geometry["crop_height"])
            centre_x = float(merged.get("x", row.x))
            centre_y = float(merged.get("y", row.y))
            merged["px"] = {
                "x": int(min(max(round(centre_x * source_w - crop_w / 2.0), 0),
                            max(0, source_w - crop_w))),
                "y": int(min(max(round(centre_y * source_h - crop_h / 2.0), 0),
                            max(0, source_h - crop_h))),
                "w": crop_w,
                "h": crop_h,
            }
        changes["rect_json"] = merged
    if not changes:
        raise ReframeError(
            "keyframe patch must change one of: t_s, x, y, scale, confidence, reason, rect"
        )

    for field_name, value in changes.items():
        setattr(row, field_name, value)
    # an x/y/scale edit must move the authoritative rect with it
    if any(axis in changes for axis in ("x", "y", "scale")) and "rect_json" not in changes:
        rebuilt = dict(row.rect_json or {})
        rebuilt["x"] = float(row.x)
        rebuilt["y"] = float(row.y)
        rebuilt["anchor"] = KEYFRAME_ANCHOR
        if geometry.get("source_width") and geometry.get("crop_width"):
            source_w = int(geometry["source_width"])
            source_h = int(geometry["source_height"])
            crop_w = int(geometry["crop_width"])
            crop_h = int(geometry["crop_height"])
            rebuilt["px"] = {
                "x": int(min(max(round(float(row.x) * source_w - crop_w / 2.0), 0),
                            max(0, source_w - crop_w))),
                "y": int(min(max(round(float(row.y) * source_h - crop_h / 2.0), 0),
                            max(0, source_h - crop_h))),
                "w": crop_w,
                "h": crop_h,
            }
        row.rect_json = rebuilt
    row.source = SOURCE_OPERATOR
    db.flush()

    entry = {
        "at": time.time(),
        "by": str(actor_id or ""),
        "keyframe_id": row.id,
        "before": {k: before[k] for k in ("t_s", "x", "y", "scale", "reason", "source")},
        "after": {k: before[k] for k in ("t_s", "x", "y", "scale", "reason", "source")}
        | {k: (v if k != "rect_json" else True) for k, v in changes.items()},
    }
    if plan is not None:
        _append_history(plan, entry)
        db.flush()
    return row, entry


# ---------------------------------------------------------------------------
# preview render (optional, NEW derived asset, source never touched)
# ---------------------------------------------------------------------------


def _resolve_asset_path(db: Session, workspace_id: str, asset_id: str, what: str) -> str:
    """Resolve a workspace asset's on-disk path through the storage boundary.

    ``managed_path`` is the fail-closed boundary: a ``storage_key`` that escapes
    the workspace directory resolves to ``None`` and we raise rather than read
    an arbitrary path from a database value.

    BOTH key conventions the repo writes are tried, each THROUGH
    ``managed_path`` so the security gate stays the single one (the same
    reasoning as :func:`app.engine.intel.qc.asset_media_path`):
    ``services.storage`` writes the CWD-relative ``data/videos/<ws>/<file>``,
    while ``providers/longform_assets.py`` and this lane's own
    :func:`_register_derived` write the workspace-relative ``<file>``. Trying
    only the first makes every mask asset and every rendered preview
    unresolvable -- a bare ``<file>`` resolves against CWD, not the workspace
    root, so it is correctly refused.
    """
    from pathlib import Path

    from app.services.storage import STORAGE_ROOT, managed_path

    asset = db.get(MediaAsset, str(asset_id or ""))
    if asset is None or asset.workspace_id != str(workspace_id):
        raise ReframeError(f"{what} not found in this workspace")
    key = str(asset.storage_key or "")
    candidates = [key, str(Path(STORAGE_ROOT) / str(workspace_id) / key)]
    for candidate in candidates:
        resolved = managed_path(str(workspace_id), candidate)
        if resolved is not None and os.path.isfile(resolved):
            return str(resolved)
    raise ReframeError(f"{what} has no resolvable storage path on this host")


def _mask_path_for(db: Session, workspace_id: str, mask_asset_id: str) -> str:
    return _resolve_asset_path(db, workspace_id, mask_asset_id, "mask asset")


def _escape_filter_path(path: str) -> str:
    r"""Escape a path for embedding in an ffmpeg filter argument.

    ``movie=filename=`` parses its value as a filter option, so ``:`` and ``,``
    are structural. ffmpeg's own parser needs a doubled backslash before the
    colon (verified against the real binary on Windows AND on POSIX paths).
    """
    return str(path).replace("\\", "/").replace(":", "\\\\:").replace(",", "\\\\,")


def _even_crop(x: int, y: int, w: int, h: int) -> str:
    return f"crop={max(2, _even(w))}:{max(2, _even(h))}:{max(0, int(x))}:{max(0, int(y))}"


def _scale(w: int, h: int) -> str:
    return f"scale={max(2, _even(w))}:{max(2, _even(h))}:flags=bicubic"


def preview_filter_graph(
    layout: str,
    geometry: AspectGeometry,
    keyframes: Sequence[Keyframe],
    *,
    mask_path: str | None = None,
    background_path: str | None = None,
    blur_strength: int = 8,
) -> str:
    """The single-input ``-vf`` graph a preview render would apply.

    Every branch was verified against ffmpeg 8.1.1 through
    :func:`app.engine.intel.ffmpeg_util.run_filter`; the graphs are built to fit
    that helper's fixed ``-i src <filter args> <extra args> dst`` argv, so a
    second input can only enter through the ``movie`` filter.
    """
    name = str(layout or "ACTIVE_SPEAKER").upper()
    hero = keyframes[-1] if keyframes else None
    hero_rect = keyframe_pixel_rect(hero, geometry) if hero is not None else {
        "x": 0, "y": 0, "w": geometry.crop_width, "h": geometry.crop_height,
    }

    if name == "BACKGROUND_BLUR":
        if not mask_path:
            raise ReframeError("background_blur needs a mask asset")
        return (
            "split=2[fg][bgx];"
            f"[bgx]boxblur={max(1, int(blur_strength))}:1[bl];"
            f"movie=filename={_escape_filter_path(mask_path)},format=gray,"
            f"scale={geometry.source_width}:{geometry.source_height},fps=10[msk];"
            "[fg][msk]alphamerge[fgm];[bl][fgm]overlay=format=auto"
        )
    if name == "BACKGROUND_REPLACE":
        if not mask_path or not background_path:
            raise ReframeError("background_replace needs a mask asset and a background asset")
        return (
            f"movie=filename={_escape_filter_path(background_path)},format=rgb24,"
            f"scale={geometry.source_width}:{geometry.source_height},fps=10[bgi];"
            f"movie=filename={_escape_filter_path(mask_path)},format=gray,"
            f"scale={geometry.source_width}:{geometry.source_height},fps=10,negate[mskb];"
            "[bgi][mskb]alphamerge[bgia];"
            "[0:v][bgia]overlay=format=auto:eof_action=repeat"
        )

    if name in {"SPLIT_SCREEN", "TWO_SHOT"}:
        inset = 1.35 if name == "TWO_SHOT" else 1.0
        # the two most recent PIXEL rects -- the persisted rect_json is the
        # normalised QC unit, and ffmpeg crop needs source pixels
        panes = [keyframe_pixel_rect(k, geometry) for k in keyframes[-2:]]
        while len(panes) < 2:
            panes.append(hero_rect)
        frame_w, frame_h = geometry.out_width, geometry.out_height
        if frame_h > frame_w:
            pane_w, pane_h = frame_w, frame_h / 2.0
            joiner = "[qa][qb]vstack=inputs=2:shortest=1"
        else:
            pane_w, pane_h = frame_w / 2.0, frame_h
            joiner = "[qa][qb]hstack=inputs=2:shortest=1"
        parts = ["split=2[a][b]"]
        for label, rect in zip(("a", "b"), panes, strict=False):
            base_w = float(rect["w"] or geometry.crop_width)
            base_h = float(rect["h"] or geometry.crop_height)
            # An inset wider than the SOURCE is impossible; clamp it, or the
            # render asks ffmpeg for pixels that do not exist.
            src_w = min(float(geometry.source_width), base_w * inset)
            src_h = min(float(geometry.source_height), base_h * inset)
            off_x = float(rect["x"] or 0) - (src_w - base_w) / 2.0
            off_y = float(rect["y"] or 0) - (src_h - base_h) / 2.0
            parts.append(
                f"[{label}]{_even_crop(off_x, off_y, src_w, src_h)},"
                f"{_scale(pane_w, pane_h)}[q{label}]"
            )
        parts.append(joiner)
        return ";".join(parts)

    if name == "GRID":
        # 2x2: four corner crops of the plan's crop region, hstacked into two
        # rows then vstacked. ``_even_crop`` makes every cell even, which is what
        # hstack/vstack require to line up without a scale re-sample.
        in_labels = ("aa", "ab", "ba", "bb")
        out_labels = ("qa", "qb", "qc", "qd")
        corners = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))
        cell_w = geometry.out_width / 2.0
        cell_h = geometry.out_height / 2.0
        # a quadrant can never be larger than the source it is cut from
        quad_w = min(float(geometry.source_width), geometry.crop_width / 2.0)
        quad_h = min(float(geometry.source_height), geometry.crop_height / 2.0)
        max_x = geometry.source_width - quad_w
        max_y = geometry.source_height - quad_h
        parts = ["split=4[aa][ab][ba][bb]"]
        for (fx, fy), in_label, out_label in zip(corners, in_labels, out_labels, strict=True):
            parts.append(
                f"[{in_label}]{_even_crop(max_x * fx, max_y * fy, quad_w, quad_h)},"
                f"{_scale(cell_w, cell_h)}[{out_label}]"
            )
        parts.append("[qa][qb]hstack=inputs=2[row_top]")
        parts.append("[qc][qd]hstack=inputs=2[row_bottom]")
        parts.append("[row_top][row_bottom]vstack=inputs=2")
        return ";".join(parts)

    if name in {"HOST_GUEST", "PODCAST_DYNAMIC"}:
        # Both need the same source frame TWICE (hero + inset), so the graph
        # opens with an explicit split. run_filter can only emit -vf, so every
        # branch must stay inside that one single-input graph -- a second `-i`
        # or a `-filter_complex` is not available through the shared helper.
        crop = _even_crop(
            hero_rect.get("x", 0), hero_rect.get("y", 0),
            geometry.crop_width, geometry.crop_height,
        )
        if name == "PODCAST_DYNAMIC":
            # hero band on top, a full-width rail of the other participants below
            hero_h = geometry.out_height * 0.72
            rail_h = geometry.out_height - hero_h
            return ";".join(
                [
                    "split=2[hero_in][rail_in]",
                    f"[hero_in]{crop},{_scale(geometry.out_width, hero_h)}[hero]",
                    f"[rail_in]{crop},{_scale(geometry.out_width, rail_h)}[rail]",
                    "[hero][rail]vstack=inputs=2:shortest=1",
                ]
            )
        # picture-in-picture: hero fills the frame, guest sits bottom-right
        pip_w = geometry.out_width * 0.34
        pip_h = pip_w * 0.75
        pad_x = geometry.out_width * 0.04
        pad_y = geometry.out_height * 0.04
        return ";".join(
            [
                "split=2[hero_in][pip_in]",
                f"[hero_in]{crop},{_scale(geometry.out_width, geometry.out_height)}[hero]",
                f"[pip_in]{crop},{_scale(pip_w, pip_h)}[pip]",
                f"[hero][pip]overlay=x={pad_x:.0f}:y=main_h-overlay_h-{pad_y:.0f}:format=auto",
            ]
        )

    # ACTIVE_SPEAKER: a time expression when the plan actually moves, so the
    # preview demonstrates the transition instead of one static crop.
    moving = _moving_crop_expression(keyframes, geometry)
    crop = moving or _even_crop(
        hero_rect.get("x", 0), hero_rect.get("y", 0), geometry.crop_width, geometry.crop_height
    )
    return f"{crop},{_scale(geometry.out_width, geometry.out_height)},setsar=1"


def _moving_crop_expression(keyframes: Sequence[Keyframe], geometry: AspectGeometry) -> str:
    """Piecewise crop expression over time, or "" when the plan is static.

    A plan whose crop never moves previews faster and identically to the baked
    form, so the expression is only built when there are at least two distinct
    positions. It is a real ffmpeg expression (``if(lt(t,..),..)``) evaluated
    per frame, which is why the preview shows the speaker switch as motion.
    """
    positions: list[tuple[float, int]] = []
    for keyframe in keyframes:
        if keyframe.transition:
            continue
        point = (round(float(keyframe.t_s), 3), keyframe_pixel_rect(keyframe, geometry)["x"])
        if not positions or positions[-1][1] != point[1]:
            positions.append(point)
    if len(positions) < 2:
        return ""
    expression = str(max(0, geometry.source_width - geometry.crop_width))
    for t_s, x in reversed(positions[:-1]):
        expression = f"if(lt(t\\,{t_s:g})\\,{x}\\,{expression})"
    return f"crop=w={geometry.crop_width}:h={geometry.crop_height}:x='{expression}':y=0"


#: encoder args a preview must pass: a re-encode is unavoidable once a filter
#: graph is applied, and audio is re-encoded rather than stream-copied because
#: the graph changes frame timing guarantees.
PREVIEW_ENCODE_ARGS: tuple[str, ...] = (
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "128k",
    "-movflags", "+faststart",
)


def _derived_path(workspace_id: str, kind: str, out_width: int, out_height: int) -> str:
    from app.services.storage import STORAGE_ROOT

    stamp = f"{int(time.time() * 1000):x}"
    return str(
        STORAGE_ROOT / str(workspace_id) / f"{kind}_{out_width}x{out_height}_{stamp}.mp4"
    )


def _register_derived(
    db: Session,
    workspace_id: str,
    source: MediaAsset,
    path: str,
    geometry: AspectGeometry,
    *,
    kind: str,
    manifest: dict,
) -> MediaAsset:
    """Register the rendered file as a NEW derived asset with full lineage."""
    from app.services.storage import STORAGE_ROOT, validate_storage_key

    relative = os.path.relpath(path, STORAGE_ROOT / str(workspace_id)).replace("\\", "/")
    key = validate_storage_key(workspace_id, relative) or relative
    size = os.path.getsize(path) if os.path.exists(path) else None
    asset = MediaAsset(
        workspace_id=str(workspace_id),
        type="video",
        origin="generated",
        provider="motion_reframe",
        storage_key=key,
        mime_type="video/mp4",
        width=geometry.out_width,
        height=geometry.out_height,
        file_size=size,
        meta_json={
            "lineage": "derived",
            "parent_asset_id": source.id,
            "operation": kind,
            "workspace_id": str(workspace_id),
        },
        parent_asset_id=source.id,
        derivation_json=manifest,
    )
    db.add(asset)
    db.flush()
    return asset


def render_preview(
    db: Session,
    workspace_id: str,
    source: MediaAsset,
    source_path: str,
    plan: ReframePlan,
    keyframes: Sequence[ReframeKeyframe],
    *,
    mask_asset_id: str | None = None,
    background_asset_id: str | None = None,
    blur_strength: int = 8,
) -> dict:
    """Render a PREVIEW of a plan into a NEW derived asset (source untouched).

    This is the only place in the lane that writes media, it is optional, and it
    refuses to run without ffmpeg. ``run_filter`` raises if ``dst == src``, so
    the derived-only invariant holds structurally rather than by convention.
    """
    if not ffmpeg_available():
        return {
            "status": "UNAVAILABLE",
            "reason": "ffmpeg not on PATH; no preview render",
            "rendered": False,
        }
    meta = dict(plan.meta_json or {})
    geometry_raw = dict(meta.get("geometry") or {})
    aspect = str(plan.aspect or "9:16")
    try:
        geometry = target_geometry(
            int(geometry_raw.get("source_width") or 0),
            int(geometry_raw.get("source_height") or 0),
            aspect,
            out_height=int(geometry_raw.get("out_height") or DEFAULT_OUT_HEIGHT),
        )
    except ReframeError as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "rendered": False}

    rows = [keyframe_dto(row) for row in keyframes]
    frames = [
        Keyframe(
            t_s=float(r["t_s"]),
            x=int(r["x"]),
            y=int(r["y"]),
            scale=float(r["scale"]),
            rect=dict(r["rect"] or {}),
            confidence=r["confidence"],
            reason=str(r["reason"]),
            source=str(r["source"]),
            transition=bool(r["transition"]),
        )
        for r in rows
    ]
    mask_path = _mask_path_for(db, workspace_id, mask_asset_id) if mask_asset_id else None
    background_path = (
        _mask_path_for(db, workspace_id, background_asset_id) if background_asset_id else None
    )
    try:
        graph = preview_filter_graph(
            str(plan.layout),
            geometry,
            frames,
            mask_path=mask_path,
            background_path=background_path,
            blur_strength=blur_strength,
        )
    except ReframeError as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "rendered": False}

    destination = _derived_path(workspace_id, DERIVATION_KIND, geometry.out_width,
                                geometry.out_height)
    started = time.perf_counter()
    result = run_filter(
        source_path, destination, ["-vf", graph],
        extra_args=PREVIEW_ENCODE_ARGS, video=True,
    )
    processing_ms = int(result.get("processing_ms") or 0)
    manifest = {
        "operation": "reframe_preview",
        "provider": "motion_reframe",
        "layout": str(plan.layout),
        "aspect": aspect,
        "geometry": geometry.to_dict(),
        "input_asset_id": source.id,
        "filter_graph": graph,
        "ffmpeg_returncode": result.get("returncode"),
        "processing_ms": processing_ms,
        "wall_ms": int((time.perf_counter() - started) * 1000),
        "keyframe_count": len(frames),
    }
    if not result.get("ok"):
        logger.warning("reframe preview failed: %s", str(result.get("stderr_tail"))[-200:])
        return {
            "status": "FAILED",
            "rendered": False,
            "reason": f"ffmpeg exited {result.get('returncode')}",
            "manifest": manifest,
        }
    asset = _register_derived(
        db, workspace_id, source, str(result["output_path"]), geometry,
        kind="reframe_preview", manifest=manifest,
    )
    return {
        "status": "COMPLETED",
        "rendered": True,
        "asset_id": asset.id,
        "storage_key": asset.storage_key,
        "parent_asset_id": source.id,
        "width": geometry.out_width,
        "height": geometry.out_height,
        "processing_ms": processing_ms,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# background tools (contracts 12)
# ---------------------------------------------------------------------------


def find_mask(
    db: Session,
    workspace_id: str,
    input_asset_id: str,
    *,
    kind: str = "PERSON",
) -> MaskAsset | None:
    """Most recent mask row of ``kind`` for an input asset (workspace-scoped)."""
    return db.scalar(
        select(MaskAsset)
        .where(
            MaskAsset.workspace_id == str(workspace_id),
            MaskAsset.input_asset_id == str(input_asset_id),
            MaskAsset.kind == str(kind).upper(),
            MaskAsset.mask_asset_id.isnot(None),
        )
        .order_by(MaskAsset.created_at.desc())
    )


def background_operation(
    db: Session,
    workspace_id: str,
    source: MediaAsset,
    operation: str,
    *,
    mask_kind: str = "PERSON",
    mask_asset_id: str | None = None,
    background_asset_id: str | None = None,
    blur_strength: int = 8,
    aspect: str = "9:16",
    out_height: int = DEFAULT_OUT_HEIGHT,
) -> dict:
    """Background blur / replace / person mask / object mask.

    Consumes lane F's ``mask_assets`` rows. With no usable mask -- which is the
    state of every workspace whose segmentation provider is not installed -- the
    answer is ``status='UNAVAILABLE'`` with a reason, never a fallback that
    produces a bad mask (contracts 9/12). The source asset is never modified;
    ``rendered=True`` implies a NEW derived asset with ``parent_asset_id``.
    """
    name = str(operation or "").strip().lower()
    if name not in BACKGROUND_OPS:
        raise ReframeError(
            f"operation must be one of {', '.join(BACKGROUND_OPS)}; got {operation!r}"
        )
    if name == "person_mask":
        wanted = "PERSON"
    elif name == "object_mask":
        wanted = "OBJECT"
    else:
        wanted = str(mask_kind or "PERSON").upper()

    row = db.get(MaskAsset, str(mask_asset_id)) if mask_asset_id else None
    if row is not None and row.workspace_id != str(workspace_id):
        row = None
    if row is None:
        row = find_mask(db, workspace_id, source.id, kind=wanted)
    if row is None or not row.mask_asset_id:
        return {
            "status": "UNAVAILABLE",
            "operation": name,
            "rendered": False,
            "reason": (
                f"no {wanted} mask asset for this input: install a segmentation "
                "provider and run POST /media-intel/masks first"
            ),
            "required": {
                "operation": name,
                "mask_kind": wanted,
                "segmentation_chain": "sam2_segmentation",
            },
        }

    mask_path = _mask_path_for(db, workspace_id, row.mask_asset_id)
    mask_meta = {
        "mask_asset_id": row.mask_asset_id,
        "mask_kind": row.kind,
        "mask_format": row.format,
        "mask_width": row.width,
        "mask_height": row.height,
        "mask_area_ratio": row.area_ratio,
        "mask_provider": row.provider_key,
        "mask_model_version": row.model_version,
        "segmentation_run_id": row.run_id,
    }
    if name in {"person_mask", "object_mask"}:
        # The mask IS the deliverable here; nothing is re-encoded and the
        # source is untouched, so this reports the existing derived mask.
        return {
            "status": "COMPLETED",
            "operation": name,
            "rendered": False,
            "reason": "",
            "source_unchanged": True,
            "output_asset_id": row.mask_asset_id,
            "mask": mask_meta,
        }

    background_path = None
    if name == "background_replace":
        if not background_asset_id:
            return {
                "status": "UNAVAILABLE",
                "operation": name,
                "rendered": False,
                "reason": "background_replace needs a background_asset_id",
                "mask": mask_meta,
            }
        background_path = _mask_path_for(db, workspace_id, background_asset_id)

    if not ffmpeg_available():
        return {
            "status": "UNAVAILABLE",
            "operation": name,
            "rendered": False,
            "reason": "ffmpeg not on PATH; background render skipped",
            "mask": mask_meta,
        }

    try:
        geometry = target_geometry(
            int(source.width or 0), int(source.height or 0), aspect, out_height=out_height
        )
    except ReframeError as exc:
        return {
            "status": "UNAVAILABLE",
            "operation": name,
            "rendered": False,
            "reason": str(exc),
            "mask": mask_meta,
        }
    graph = preview_filter_graph(
        "BACKGROUND_BLUR" if name == "background_blur" else "BACKGROUND_REPLACE",
        geometry,
        [],
        mask_path=mask_path,
        background_path=background_path,
        blur_strength=blur_strength,
    )
    destination = _derived_path(workspace_id, name, geometry.source_width, geometry.source_height)
    started = time.perf_counter()
    result = run_filter(
        _resolve_asset_path(db, workspace_id, source.id, "source asset"),
        destination,
        ["-vf", graph],
        extra_args=PREVIEW_ENCODE_ARGS,
        video=True,
    )
    manifest = {
        "operation": name,
        "provider": "motion_reframe",
        "input_asset_id": source.id,
        "filter_graph": graph,
        "ffmpeg_returncode": result.get("returncode"),
        "processing_ms": int(result.get("processing_ms") or 0),
        "wall_ms": int((time.perf_counter() - started) * 1000),
        "mask": mask_meta,
        "blur_strength": int(blur_strength) if name == "background_blur" else None,
    }
    if not result.get("ok"):
        return {
            "status": "FAILED",
            "operation": name,
            "rendered": False,
            "reason": f"ffmpeg exited {result.get('returncode')}",
            "manifest": manifest,
            "mask": mask_meta,
        }
    # a background op keeps the source frame size, not the target aspect
    geometry = replace(geometry, out_width=geometry.source_width, out_height=geometry.source_height)
    asset = _register_derived(
        db, workspace_id, source, str(result["output_path"]), geometry,
        kind=name, manifest=manifest,
    )
    return {
        "status": "COMPLETED",
        "operation": name,
        "rendered": True,
        "output_asset_id": asset.id,
        "parent_asset_id": source.id,
        "storage_key": asset.storage_key,
        "width": asset.width,
        "height": asset.height,
        "processing_ms": int(result.get("processing_ms") or 0),
        "source_unchanged": True,
        "manifest": manifest,
        "mask": mask_meta,
    }


# ---------------------------------------------------------------------------
# plan creation (the orchestration entry point used by the provider + route)
# ---------------------------------------------------------------------------


def build_plan_payload(
    db: Session,
    workspace_id: str,
    source: MediaAsset,
    *,
    aspect: str = "9:16",
    layout: str = "ACTIVE_SPEAKER",
    params: dict | None = None,
    run_id: str | None = None,
    out_height: int = DEFAULT_OUT_HEIGHT,
) -> dict:
    """Compute a full plan payload: geometry, anchors, keyframes, slots, ops.

    Pure orchestration + measurement: it reads evidence rows, writes nothing,
    and returns everything the caller needs to persist a plan. The provider
    (:mod:`app.engine.intel.impl.motion_reframe`) and the route both call this,
    so a run and a route request cannot disagree about the plan.
    """
    options = dict(params or {})
    if int(source.width or 0) <= 0 or int(source.height or 0) <= 0:
        raise ReframeError("source asset has no recorded width/height")
    geometry = target_geometry(
        int(source.width), int(source.height), aspect, out_height=out_height
    )
    duration = float(source.duration_seconds or 0.0)
    evidence = load_evidence(
        db, workspace_id, source.id, track_run_id=options.get("evidence_run_id")
    )
    focal = _focal_point(options, geometry)
    times = sample_times_for(
        duration,
        fps=float(options.get("sample_fps") or 2.0),
        min_interval=float(options.get("min_interval_s") or 0.25),
    )
    anchors = resolve_anchors(
        geometry,
        evidence,
        times,
        focal_point=focal,
        safe_area=float(options.get("safe_area", DEFAULT_SAFE_AREA)),
    )
    keyframes = build_keyframes(
        anchors,
        geometry,
        safe_area=float(options.get("safe_area", DEFAULT_SAFE_AREA)),
        max_move_per_s=float(options.get("max_move_per_s", DEFAULT_MAX_MOVE_PER_S)),
        transition_s=float(options.get("transition_s", DEFAULT_TRANSITION_S)),
        ramp_steps=int(options.get("ramp_steps", DEFAULT_RAMP_STEPS)),
        smoothing=float(options.get("smoothing", DEFAULT_SMOOTHING)),
    )
    participants = int(options.get("participants") or 2)
    slots = layout_slots(
        str(layout).upper(),
        geometry,
        keyframes,
        participants=participants,
        safe_area=float(options.get("safe_area", DEFAULT_SAFE_AREA)),
        grid_rows=int(options.get("grid_rows") or 2),
    )
    ops: list[dict] = []
    if keyframes and duration > 0:
        ops = layout_ops(
            str(layout).upper(),
            slots,
            keyframes,
            geometry=geometry,
            duration_s=duration,
            asset_id=source.id,
            tracks=list(options.get("tracks") or []),
        )
    levels = sorted({k.source for k in keyframes})
    return {
        "geometry": geometry.to_dict(),
        "aspect": aspect,
        "layout": str(layout).upper(),
        "anchors": [
            {
                "t_s": round(a.t_s, 4),
                "x": round(a.center_x, 3),
                "y": round(a.center_y, 3),
                "source": a.source,
                "reason": a.reason,
                "confidence": a.confidence,
                "anchor_id": a.anchor_id,
            }
            for a in anchors
        ],
        "keyframes": [k.to_row() for k in keyframes],
        "keyframe_objects": keyframes,
        "slots": [s.to_dict() for s in slots],
        "ops": ops,
        "jitter": measure_jitter(keyframes, geometry),
        "strategy": ">".join(levels) or "none",
        "levels_used": levels,
        "evidence": {
            "resolved_active_speaker_rows": len(evidence.get("active_speaker") or []),
            "unresolved_active_speaker_rows": len(evidence.get("unresolved") or []),
            "face_tracks": sorted((evidence.get("face_boxes") or {}).keys()),
            "focal_point": list(focal) if focal else None,
        },
    }


def _focal_point(options: dict, geometry: AspectGeometry) -> tuple[float, float] | None:
    """Read a configured focal point. Accepts pixels or normalized 0..1."""
    raw = options.get("focal_point")
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = (raw.get("x"), raw.get("y"))
    try:
        x, y = raw  # type: ignore[misc]
        fx, fy = float(x), float(y)
    except (TypeError, ValueError) as exc:
        raise ReframeError("focal_point must be a (x, y) pair") from exc
    if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0 and (fx < 0.999 or fy < 0.999):
        return (fx * geometry.source_width, fy * geometry.source_height)
    if 0.0 <= fx <= geometry.source_width and 0.0 <= fy <= geometry.source_height:
        return (fx, fy)
    raise ReframeError("focal_point is outside the source frame")


def persist_plan(
    db: Session,
    workspace_id: str,
    source: MediaAsset,
    payload: dict,
    *,
    run_id: str | None = None,
) -> ReframePlan:
    """Write the plan + its keyframes as EDITABLE rows.

    Keyframes are stored, never baked. The rows carry the geometry, measured
    jitter, per-slot rects and the canonical ops in ``meta_json`` so a re-read
    needs no recomputation and an operator edit has somewhere to live.
    """
    plan = ReframePlan(
        run_id=run_id,
        workspace_id=str(workspace_id),
        source_asset_id=source.id,
        layout=str(payload.get("layout") or "ACTIVE_SPEAKER"),
        aspect=str(payload.get("aspect") or "9:16"),
        strategy=str(payload.get("strategy") or ""),
        meta_json={
            # TOP-LEVEL source_width/source_height: lane H's
            # `qc._plan_keyframes` reads exactly these keys off meta_json, and
            # they are what its coverage check needs to normalise a plan.
            "source_width": int((payload.get("geometry") or {}).get("source_width") or 0),
            "source_height": int((payload.get("geometry") or {}).get("source_height") or 0),
            "geometry": payload.get("geometry") or {},
            "jitter": payload.get("jitter") or {},
            "slots": payload.get("slots") or [],
            "ops": payload.get("ops") or [],
            "evidence": payload.get("evidence") or {},
            "levels_used": payload.get("levels_used") or [],
            "keyframe_anchor": KEYFRAME_ANCHOR,
            "keyframe_units": "normalised",
            "baked": False,
            "schema": "reframe_plan.v1",
        },
    )
    db.add(plan)
    db.flush()
    for row in payload.get("keyframes") or []:
        db.add(
            ReframeKeyframe(
                plan_id=plan.id,
                workspace_id=str(workspace_id),
                t_s=float(row["t_s"]),
                x=float(row["x"]),
                y=float(row["y"]),
                scale=float(row["scale"]),
                rect_json=dict(row.get("rect_json") or {}),
                confidence=row.get("confidence"),
                reason=str(row.get("reason") or ""),
                source=str(row.get("source") or ""),
            )
        )
    db.flush()
    return plan


# ---------------------------------------------------------------------------
# run manifest + the QC apply gate (Lane H interop, contracts 13)
# ---------------------------------------------------------------------------


def validate_request(aspect: str, layout: str) -> None:
    """Validate aspect + layout UP FRONT, before any work or cache lookup.

    Ordering matters: the run cache is keyed on the request parameters, so an
    unvalidated aspect would let a bad request hit a good plan's cache entry and
    return 200 for an aspect that cannot be rendered. Validation is therefore
    the first thing the route does.
    """
    aspect_ratio(aspect)  # raises ReframeError on an unknown aspect
    name = str(layout or "").strip().upper()
    if name not in LAYOUTS:
        raise ReframeError(f"layout must be one of {', '.join(LAYOUTS)}; got {layout!r}")


def run_manifest(
    payload: dict,
    source: MediaAsset,
    *,
    result: dict | None = None,
) -> dict:
    """``metrics_json`` for a reframe/background run, in the QC read shape.

    HARD INTEROP CONTRACT with lane H: the manifest is the nested
    ``{"source": {...}, "output": {...}}`` form, because
    :func:`app.engine.intel.qc._flatten_metrics` expands it into the
    ``source_*`` / ``output_*`` keys its checks read (``_recorded(metrics,
    "output_duration_s", ...)`` etc). A flat ``{"output_duration_s": ...}``
    manifest is also accepted by QC, but writing the nested form is the shape
    every other Work 12 lane writes, so a run reads the same either way.
    """
    geometry = dict(payload.get("geometry") or {})
    source_block: dict[str, Any] = {
        "asset_id": str(getattr(source, "id", "") or ""),
        "width": int(getattr(source, "width", 0) or 0),
        "height": int(getattr(source, "height", 0) or 0),
        "duration_s": float(getattr(source, "duration_seconds", 0.0) or 0.0),
        "checksum": str(getattr(source, "checksum", "") or ""),
    }
    output_block: dict[str, Any] = {
        "plan_id": str((result or {}).get("plan_id") or ""),
        "layout": str(payload.get("layout") or ""),
        "aspect": str(payload.get("aspect") or ""),
        "keyframe_count": len(payload.get("keyframes") or []),
        "slot_count": len(payload.get("slots") or []),
        "op_count": len(payload.get("ops") or []),
        "strategy": str(payload.get("strategy") or ""),
        "levels_used": list(payload.get("levels_used") or []),
        "baked": False,
        "keyframe_anchor": KEYFRAME_ANCHOR,
        "keyframe_units": "normalised",
        "crop_width": int(geometry.get("crop_width") or 0),
        "crop_height": int(geometry.get("crop_height") or 0),
    }
    if result:
        for key in (
            "status", "rendered", "asset_id", "output_asset_id", "parent_asset_id",
            "storage_key", "width", "height", "processing_ms", "reason",
        ):
            if key in result:
                output_block[key] = result[key]
        if result.get("width") and result.get("height"):
            output_block.setdefault("output_width", int(result["width"]))
            output_block.setdefault("output_height", int(result["height"]))
    jitter = dict(payload.get("jitter") or {})
    if jitter:
        output_block["jitter"] = jitter
    return {"source": source_block, "output": output_block}


def assert_plan_applies(
    db: Session,
    ws: Any,
    plan: ReframePlan,
    *,
    override: bool = False,
) -> dict:
    """The apply gate: may this plan be rendered / pushed to a timeline?

    Delegates to :func:`app.engine.intel.qc.assert_qc_allows_apply` with the
    PLAN as the target (QC resolves a plan to its run's verdict). A plan with no
    QC verdict yet is BLOCKED rather than waved through -- contracts 13 makes
    "run QC before applying" the rule, and a plan nobody checked is exactly the
    plan that needs checking.

    The import is INSIDE the function: lane H lands independently, so this
    module must import cleanly with ``app.engine.intel.qc`` absent. A missing
    QC module is reported as blocked-with-reason, never as "allowed".
    """
    try:
        from app.engine.intel.qc import QCApplyBlocked, assert_qc_allows_apply
    except Exception as exc:  # pragma: no cover - QC absent in a partial deploy
        raise ReframeError(
            f"QC is unavailable, so this plan cannot be applied ({type(exc).__name__}); "
            "install the QC module or re-run QC"
        ) from exc
    try:
        return assert_qc_allows_apply(db, ws, plan, override=bool(override))
    except QCApplyBlocked as exc:
        raise ReframeError(
            f"QC blocked this plan: {exc.as_dict()['detail']}"
            + (f" (needs override: {', '.join(exc.failures[:3])})" if exc.failures else "")
        ) from exc


__all__ = [
    "ALL_SOURCES",
    "Anchor",
    "AspectGeometry",
    "BACKGROUND_OPS",
    "DEFAULT_MAX_MOVE_PER_S",
    "DEFAULT_OUT_HEIGHT",
    "DEFAULT_RAMP_STEPS",
    "DEFAULT_SAFE_AREA",
    "DEFAULT_SMOOTHING",
    "DEFAULT_TRANSITION_S",
    "EVENT_BACKGROUND_APPLIED",
    "EVENT_BACKGROUND_UNAVAILABLE",
    "EVENT_KEYFRAME_EDITED",
    "EVENT_PLAN_CREATED",
    "FrameBox",
    "KEYFRAME_ANCHOR",
    "KEYFRAME_SOURCES",
    "LAYOUTS",
    "Keyframe",
    "ReframeError",
    "Slot",
    "SOURCE_ALIASES",
    "SOURCE_OPERATOR",
    "TARGET_ASPECTS",
    "aspect_ratio",
    "assert_plan_applies",
    "background_operation",
    "build_keyframes",
    "build_plan_payload",
    "crop_from_center",
    "find_mask",
    "get_plan",
    "keyframe_dto",
    "keyframe_center_px",
    "keyframe_pixel_rect",
    "layout_ops",
    "layout_slots",
    "list_keyframes",
    "load_evidence",
    "measure_jitter",
    "persist_plan",
    "plan_dto",
    "preview_filter_graph",
    "probe",
    "render_preview",
    "resolve_anchors",
    "run_manifest",
    "sample_times_for",
    "scale_for",
    "target_geometry",
    "update_keyframe",
]
