"""Anonymous face tracking over real decoded frames (Work 12 Lane E).

Contracts §8. This module is the whole tracking brain and is deliberately
independent of WHICH detector produced the boxes:

* :func:`analyze` samples frames through
  :func:`app.engine.intel.ffmpeg_util.extract_frames` (real ffmpeg, real PNG
  bytes), asks an INJECTED detector backend for boxes in that frame's own pixel
  space, normalises them, associates them across time and returns
  :class:`FaceTrack` objects.
* :class:`FaceTracker` holds the association policy on its own, so it is unit
  testable in pure Python while production keeps the real detector.

Invariants (contracts §0/§8):

* **Anonymous by construction.** A track is a *session-local label* ``FT_00``,
  ``FT_01``, ... assigned by first appearance. There is no embedding, no
  template, no cross-video linking and no name field anywhere in this module.
  Real-identity face recognition is out of scope and forbidden.
* **Never merge uncertain identities.** An ambiguous crossing (a detection that
  matches two tracks equally well) starts a NEW track and flags it
  (``unresolved_crossing``) instead of picking a winner. A temporary exit that
  resumes inside the tolerance window keeps its id and bumps ``reentry_count``.
* **Honest partial evidence.** ``max_samples_per_track`` caps the samples and
  sets ``truncated``; a detector that reports no confidence keeps
  ``confidence=None``; landmarks are stored only when the detector supplied
  them.
* **Derived only.** Frames are decoded into a private temp directory that is
  removed again; the source media is read, never written.

No ML package is imported here: the detector is a parameter. The MediaPipe
adapter (``app.engine.intel.impl.mediapipe_faces``) imports the heavy backend
lazily and injects itself as that parameter, which is also how the tests drive
the identical code path with a deterministic double.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engine.intel import ffmpeg_util as ff
from app.engine.intel.base import (
    CancelFn,
    ProgressFn,
    check_control,
)

logger = logging.getLogger("ymoney.intel")

# --- tunables (contracts §8 defaults; every one is overridable per run) ------

#: sampled frames per second (contracts §8 default)
DEFAULT_SAMPLE_FPS = 2.0
#: per-track sample cap (contracts §8 default); hitting it sets ``truncated``
DEFAULT_MAX_SAMPLES = 2000
#: hard bounds so a request cannot ask for an absurd sampling density
MIN_SAMPLE_FPS = 0.1
MAX_SAMPLE_FPS = 30.0
MIN_MAX_SAMPLES = 1
MAX_MAX_SAMPLES = 20_000

#: minimum IoU for a detection to continue an existing track
DEFAULT_MIN_IOU = 0.30
#: two candidate tracks within this IoU of each other are an AMBIGUOUS crossing
DEFAULT_AMBIGUITY_MARGIN = 0.10
#: largest accepted area ratio between a track's last box and a detection
DEFAULT_SIZE_TOLERANCE = 2.0
#: largest accepted centre distance, in units of the mean box half-diagonal
DEFAULT_CENTER_TOLERANCE = 1.5
#: consecutive frames a track may go unmatched before it is closed
DEFAULT_MAX_GAP_FRAMES = 3
#: how long a closed track may resume and keep its id (re-entry tolerance)
DEFAULT_REENTRY_WINDOW_S = 2.0
#: IoU a detection must reach to resume a closed track
DEFAULT_REENTRY_MIN_IOU = 0.20
#: smallest box side (px) accepted from a detector
MIN_BOX_SIDE = 2.0
#: how many warnings a single run keeps
MAX_WARNINGS = 20

TrackIdFn = Callable[[int], str]


# ---------------------------------------------------------------------------
# per-frame evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    """One normalised detection in a frame's own pixel space.

    ``confidence`` and ``landmarks`` are ``None`` when the detector did not
    supply them -- honest emptiness, never a placeholder value.
    """

    x: float
    y: float
    w: float
    h: float
    confidence: float | None = None
    landmarks: list | None = None

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    @property
    def has_landmarks(self) -> bool:
        return bool(self.landmarks)

    def to_dict(self) -> dict:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "w": round(self.w, 3),
            "h": round(self.h, 3),
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "landmarks": list(self.landmarks) if self.landmarks else None,
        }


@dataclass(frozen=True)
class TrackSample:
    """One sampled box of a track: geometry + evidence at ``t_s``."""

    t_s: float
    x: float
    y: float
    w: float
    h: float
    confidence: float | None = None
    landmarks: list | None = None

    @property
    def has_landmarks(self) -> bool:
        return bool(self.landmarks)

    def to_dict(self) -> dict:
        return {
            "t_s": round(self.t_s, 4),
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "w": round(self.w, 3),
            "h": round(self.h, 3),
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "landmarks": list(self.landmarks) if self.landmarks else None,
        }


def anonymous_track_label(index: int) -> str:
    """``0 -> "FT_00"``, ``9 -> "FT_09"``, ``100 -> "FT_100"``."""
    number = max(0, int(index))
    return f"FT_{number:02d}" if number < 100 else f"FT_{number}"


def _finite(value: Any) -> float | None:
    """``float(value)`` when it is a real finite number, else ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normalize_detection(
    raw: Any,
    width: int,
    height: int,
    *,
    min_side: float = MIN_BOX_SIDE,
) -> Detection | None:
    """Normalise one raw detector box; ``None`` when it is not usable.

    Accepts a mapping (what every detector adapter emits) or an existing
    :class:`Detection` (returned as-is when it already fits the frame). The box
    is clamped to the frame; a degenerate or non-finite box is DROPPED rather
    than repaired, because a repaired box is a fabricated face.
    """
    if raw is None:
        return None
    if isinstance(raw, Detection):
        candidate: Mapping[str, Any] = {
            "x": raw.x, "y": raw.y, "w": raw.w, "h": raw.h,
            "confidence": raw.confidence, "landmarks": raw.landmarks,
        }
    elif isinstance(raw, Mapping):
        candidate = raw
    else:
        return None

    fw = _finite(width)
    fh = _finite(height)
    x = _finite(candidate.get("x"))
    y = _finite(candidate.get("y"))
    w = _finite(candidate.get("w"))
    h = _finite(candidate.get("h"))
    if None in (fw, fh, x, y, w, h):
        return None
    x, y, w, h = float(x), float(y), float(w), float(h)
    if w < min_side or h < min_side:
        return None
    # clip to the frame; a box that does not intersect it at all is DROPPED
    # (squeezing an off-frame box into a sliver would fabricate a face)
    x0 = min(max(x, 0.0), fw)
    y0 = min(max(y, 0.0), fh)
    w = min(w, fw - x0)
    h = min(h, fh - y0)
    if w < min_side or h < min_side:
        return None
    confidence = _finite(candidate.get("confidence"))
    landmarks = candidate.get("landmarks")
    return Detection(
        x=x0,
        y=y0,
        w=w,
        h=h,
        confidence=None if confidence is None else max(0.0, min(1.0, confidence)),
        landmarks=list(landmarks) if isinstance(landmarks, (list, tuple)) and landmarks else None,
    )


def normalize_detections(
    raw: Sequence[Any] | None,
    width: int,
    height: int,
    *,
    min_side: float = MIN_BOX_SIDE,
) -> list[Detection]:
    """Normalise a frame's detections, deterministically ordered by position."""
    out: list[Detection] = []
    for item in raw or ():
        det = normalize_detection(item, width, height, min_side=min_side)
        if det is not None:
            out.append(det)
    out.sort(key=lambda d: (d.x, d.y))
    return out


def iou(a: Detection, b: Detection) -> float:
    """Intersection-over-union of two boxes (0.0 when disjoint)."""
    ix = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    iy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if ix <= 0.0 or iy <= 0.0:
        return 0.0
    inter = ix * iy
    union = a.area + b.area - inter
    return inter / union if union > 0.0 else 0.0


# ---------------------------------------------------------------------------
# association policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackParams:
    """Every association knob in one frozen value (validated on construction)."""

    min_iou: float = DEFAULT_MIN_IOU
    ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN
    size_tolerance: float = DEFAULT_SIZE_TOLERANCE
    center_tolerance: float = DEFAULT_CENTER_TOLERANCE
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES
    reentry_window_s: float = DEFAULT_REENTRY_WINDOW_S
    reentry_min_iou: float = DEFAULT_REENTRY_MIN_IOU
    max_samples_per_track: int = DEFAULT_MAX_SAMPLES

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> TrackParams:
        """Build from a request ``params`` dict, clamping every value."""
        raw = dict(params or {})
        return cls(
            min_iou=_clamp_float(raw.get("min_iou"), DEFAULT_MIN_IOU, 0.0, 1.0),
            ambiguity_margin=_clamp_float(
                raw.get("ambiguity_margin"), DEFAULT_AMBIGUITY_MARGIN, 0.0, 1.0
            ),
            size_tolerance=_clamp_float(
                raw.get("size_tolerance"), DEFAULT_SIZE_TOLERANCE, 1.0, 10.0
            ),
            center_tolerance=_clamp_float(
                raw.get("center_tolerance"), DEFAULT_CENTER_TOLERANCE, 0.1, 10.0
            ),
            max_gap_frames=_clamp_int(
                raw.get("max_gap_frames"), DEFAULT_MAX_GAP_FRAMES, 0, 120
            ),
            reentry_window_s=_clamp_float(
                raw.get("reentry_window_s"), DEFAULT_REENTRY_WINDOW_S, 0.0, 600.0
            ),
            reentry_min_iou=_clamp_float(
                raw.get("reentry_min_iou"), DEFAULT_REENTRY_MIN_IOU, 0.0, 1.0
            ),
            max_samples_per_track=_clamp_int(
                raw.get("max_samples_per_track"),
                DEFAULT_MAX_SAMPLES,
                MIN_MAX_SAMPLES,
                MAX_MAX_SAMPLES,
            ),
        )


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    out = _finite(value)
    if out is None:
        return default
    return max(low, min(high, out))


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    out = _finite(value)
    if out is None:
        return default
    return max(low, min(high, int(out)))


@dataclass
class FaceTrack:
    """One session-local anonymous track (``FT_00``) and its samples.

    ``track_id`` is a per-run label, never an identity. ``reentry_count``
    counts how often the track resumed after a temporary exit;
    ``unresolved_crossing`` marks a track created because the association was
    ambiguous -- the system refused to merge identities there.
    ``truncated`` says the sample cap was reached, so the evidence is partial.

    Internal bookkeeping (``missed``, ``closed``, ``_last``) never reaches a
    DTO.
    """

    track_id: str
    samples: list[TrackSample] = field(default_factory=list)
    reentry_count: int = 0
    truncated: bool = False
    unresolved_crossing: bool = False
    #: frames the track was OPEN and unmatched -- QC evidence of a face lost
    #: mid-track. A closed track stops counting, so this is "how long was the
    #: face lost WHILE it was being tracked", not the total absence.
    gap_count: int = 0
    missed: int = 0
    closed: bool = False
    #: most recent MATCHED detection, kept even when its sample hit the cap, so
    #: association keeps following the face while stored evidence stays capped
    seen: Detection | None = field(default=None, repr=False, compare=False)

    # -- derived ---------------------------------------------------------

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    @property
    def start_s(self) -> float:
        return self.samples[0].t_s if self.samples else 0.0

    @property
    def end_s(self) -> float:
        return self.samples[-1].t_s if self.samples else 0.0

    @property
    def confidence_max(self) -> float | None:
        values = [s.confidence for s in self.samples if s.confidence is not None]
        return max(values) if values else None

    @property
    def has_landmarks(self) -> bool:
        return any(s.has_landmarks for s in self.samples)

    def _last(self) -> Detection | None:
        """The association reference: the newest MATCHED detection."""
        if self.seen is not None:
            return self.seen
        if not self.samples:
            return None
        last = self.samples[-1]
        return Detection(
            x=last.x, y=last.y, w=last.w, h=last.h,
            confidence=last.confidence, landmarks=last.landmarks,
        )

    def add_sample(self, t_s: float, det: Detection, *, max_samples: int) -> bool:
        """Append a sample unless the cap is already reached (``truncated``)."""
        if len(self.samples) >= max(0, int(max_samples)):
            self.truncated = True
            return False
        self.samples.append(
            TrackSample(
                t_s=round(float(t_s), 4),
                x=det.x, y=det.y, w=det.w, h=det.h,
                confidence=det.confidence, landmarks=det.landmarks,
            )
        )
        return True

    def to_dict(self, *, include_samples: bool = True) -> dict:
        """Anonymous DTO. Contains no name, gender, age, identity or alias field."""
        payload = {
            "track_id": self.track_id,
            "start_s": round(self.start_s, 4),
            "end_s": round(self.end_s, 4),
            "sample_count": self.sample_count,
            "confidence_max": (
                None if self.confidence_max is None else round(self.confidence_max, 4)
            ),
            "truncated": bool(self.truncated),
            "reentry_count": int(self.reentry_count),
            "unresolved_crossing": bool(self.unresolved_crossing),
            "gap_count": int(self.gap_count),
            "landmarks": bool(self.has_landmarks),
        }
        if include_samples:
            payload["samples"] = [s.to_dict() for s in self.samples]
        return payload


def _gated(prev: Detection, cand: Detection, params: TrackParams) -> bool:
    """IoU + size + centre gating: only a plausible continuation passes."""
    if iou(prev, cand) < params.min_iou:
        return False
    smaller = min(prev.area, cand.area)
    if smaller <= 0.0:
        return False
    if max(prev.area, cand.area) / smaller > params.size_tolerance:
        return False
    reach = max(1.0, ((prev.w + prev.h) + (cand.w + cand.h)) / 4.0)
    return math.hypot(prev.cx - cand.cx, prev.cy - cand.cy) <= params.center_tolerance * reach


class FaceTracker:
    """Frame-by-frame multi-face association with an honest uncertainty policy.

    Per frame:

    1. every active track proposes the detections that pass the gates;
    2. a detection whose best and second-best candidate tracks are within
       ``ambiguity_margin`` is AMBIGUOUS -- it starts a NEW flagged track and no
       track consumes it, because choosing a winner here would merge identities;
    3. the remaining detections are assigned greedily by descending IoU;
    4. an unassigned detection first tries to RESUME a recently closed track
       (same id, ``reentry_count`` bumped); only then does it start a new id;
    5. a track unmatched for more than ``max_gap_frames`` is closed.
    """

    def __init__(
        self,
        params: TrackParams | None = None,
        *,
        label_fn: TrackIdFn = anonymous_track_label,
    ) -> None:
        self.params = params or TrackParams()
        self._label_fn = label_fn
        self._active: list[FaceTrack] = []
        self._closed: list[FaceTrack] = []
        #: every track ever created, so a pruned (no longer reachable) track is
        #: still part of the result -- evidence is never dropped from a run
        self._seen: list[FaceTrack] = []
        self._next_index = 0
        self._warnings: list[str] = []
        self._frame_count = 0
        self._detection_count = 0

    # -- state ----------------------------------------------------------

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def detection_count(self) -> int:
        return self._detection_count

    def tracks(self) -> list[FaceTrack]:
        """Every track this pass produced, ordered by first appearance."""
        return sorted(self._seen, key=lambda t: (t.start_s, t.track_id))

    def _warn(self, message: str) -> None:
        if message in self._warnings:
            return
        if len(self._warnings) < MAX_WARNINGS:
            self._warnings.append(message)
        elif len(self._warnings) == MAX_WARNINGS:
            self._warnings.append("further warnings suppressed")

    # -- main step ------------------------------------------------------

    def update(self, t_s: float, detections: Sequence[Detection]) -> list[FaceTrack]:
        """Consume one frame; returns the tracks that gained a sample."""
        now = round(float(t_s), 4)
        self._frame_count += 1
        dets = list(detections or ())
        self._detection_count += len(dets)
        params = self.params
        touched: list[FaceTrack] = []
        # only tracks that existed BEFORE this frame may be continued or aged
        existing = list(self._active)

        candidates: dict[int, list[tuple[float, FaceTrack]]] = {}
        for index in range(len(dets)):
            ranked: list[tuple[float, FaceTrack]] = []
            for track in existing:
                prev = track._last()
                if prev is None:
                    continue
                if _gated(prev, dets[index], params):
                    ranked.append((iou(prev, dets[index]), track))
            ranked.sort(key=lambda pair: (-pair[0], pair[1].track_id))
            candidates[index] = ranked

        ambiguous: set[int] = set()
        for index, ranked in candidates.items():
            if len(ranked) >= 2 and (ranked[0][0] - ranked[1][0]) <= params.ambiguity_margin:
                ambiguous.add(index)

        assigned_det: dict[int, FaceTrack] = {}
        used: set[int] = set()
        pairs: list[tuple[float, int, int]] = []
        for track_index, track in enumerate(existing):
            prev = track._last()
            if prev is None:
                continue
            for det_index in range(len(dets)):
                if det_index in ambiguous:
                    continue
                if _gated(prev, dets[det_index], params):
                    pairs.append((iou(prev, dets[det_index]), det_index, track_index))
        pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        for _, det_index, track_index in pairs:
            if det_index in assigned_det or track_index in used:
                continue
            assigned_det[det_index] = existing[track_index]
            used.add(track_index)

        for det_index, track in assigned_det.items():
            track.seen = dets[det_index]
            if track.add_sample(now, dets[det_index], max_samples=params.max_samples_per_track):
                touched.append(track)
            track.missed = 0

        # tracks nobody claimed age out; `gap_count` is QC evidence of a lost face
        for track_index, track in enumerate(existing):
            if track_index in used:
                continue
            track.missed += 1
            track.gap_count += 1
            if track.missed > params.max_gap_frames:
                track.closed = True
                self._closed.append(track)
        self._active = [track for track in existing if not track.closed]
        # only tracks whose last sample is inside the window can ever resume
        self._closed = [
            track for track in self._closed if now - track.end_s <= params.reentry_window_s
        ]

        fresh: list[FaceTrack] = []
        for det_index in sorted(
            (i for i in range(len(dets)) if i not in assigned_det),
            key=lambda i: (dets[i].x, dets[i].y),
        ):
            det = dets[det_index]
            if det_index in ambiguous:
                track = self._new_track(now, det)
                track.unresolved_crossing = True
                fresh.append(track)
                self._warn(
                    f"ambiguous crossing at t={now:.3f}s: a new anonymous track was "
                    "started instead of merging two faces"
                )
                continue
            resumed = self._resume(det, now)
            if resumed is not None:
                resumed.seen = det
                if resumed.add_sample(now, det,
                                      max_samples=params.max_samples_per_track):
                    touched.append(resumed)
                resumed.missed = 0
            else:
                fresh.append(self._new_track(now, det))
        return [*touched, *fresh]

    def _new_track(self, now: float, det: Detection) -> FaceTrack:
        track = FaceTrack(track_id=self._label_fn(self._next_index))
        self._next_index += 1
        track.seen = det
        track.add_sample(now, det, max_samples=self.params.max_samples_per_track)
        self._active.append(track)
        self._seen.append(track)
        return track

    def _resume(self, det: Detection, now: float) -> FaceTrack | None:
        """Resume the best recently-closed track this detection can continue."""
        best: tuple[float, FaceTrack] | None = None
        for track in self._closed:
            prev = track._last()
            if prev is None or (now - track.end_s) > self.params.reentry_window_s:
                continue
            if not _gated(prev, det, self.params):
                continue
            score = iou(prev, det)
            if score < self.params.reentry_min_iou:
                continue
            if best is None or score > best[0] or (
                score == best[0] and track.track_id < best[1].track_id
            ):
                best = (score, track)
        if best is None:
            return None
        track = best[1]
        self._closed = [t for t in self._closed if t is not track]
        track.closed = False
        track.reentry_count += 1
        self._active.append(track)
        self._warn(
            f"anonymous track {track.track_id} re-entered at t={now:.3f}s "
            f"(reentry #{track.reentry_count})"
        )
        return track

    def finish(self) -> list[FaceTrack]:
        """Close every open track and return the final list of all tracks."""
        for track in self._active:
            track.closed = True
            if track not in self._closed:
                self._closed.append(track)
        self._active = []
        return self.tracks()


def track_detections(
    frames: Sequence[tuple[float, Sequence[Detection]]],
    params: TrackParams | None = None,
    *,
    label_fn: TrackIdFn = anonymous_track_label,
) -> tuple[list[FaceTrack], list[str]]:
    """Track a whole frame sequence given as ``[(t_s, detections), ...]``."""
    tracker = FaceTracker(params, label_fn=label_fn)
    for t_s, detections in frames:
        tracker.update(t_s, detections)
    return tracker.finish(), tracker.warnings


# ---------------------------------------------------------------------------
# the real pipeline: ffmpeg frames -> detector -> tracks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaceTrackingResult:
    """Everything one tracking pass produced (never a fabricated face)."""

    tracks: list[FaceTrack] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frames: int = 0
    detections: int = 0
    frame_width: int = 0
    frame_height: int = 0
    sample_fps: float = DEFAULT_SAMPLE_FPS
    frame_dir: str = ""

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def sample_count(self) -> int:
        return sum(t.sample_count for t in self.tracks)

    @property
    def truncated_tracks(self) -> list[str]:
        return [t.track_id for t in self.tracks if t.truncated]

    @property
    def unresolved_crossings(self) -> list[str]:
        return [t.track_id for t in self.tracks if t.unresolved_crossing]

    @property
    def reentry_total(self) -> int:
        return sum(t.reentry_count for t in self.tracks)

    def metrics(self) -> dict:
        """Machine metrics stored on the run manifest (no row content)."""
        return {
            "frames": self.frames,
            "detections": self.detections,
            "tracks": self.track_count,
            "samples": self.sample_count,
            "sample_fps": self.sample_fps,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "truncated_tracks": self.truncated_tracks,
            "unresolved_crossings": self.unresolved_crossings,
            "reentry_total": self.reentry_total,
            "tracks_with_landmarks": sum(1 for t in self.tracks if t.has_landmarks),
        }

    def payload(self) -> dict:
        """The provider artifact payload (anonymous tracks + samples)."""
        return {
            "tracks": [t.to_dict(include_samples=True) for t in self.tracks],
            "metrics": self.metrics(),
            "warnings": list(self.warnings),
        }


def frame_size(path: str | Path) -> tuple[int, int]:
    """``(width, height)`` of the first video stream, ``(0, 0)`` when unknown."""
    data = ff.probe(path)
    for stream in data.get("streams", []) or []:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            try:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                return 0, 0
            if width > 0 and height > 0:
                return width, height
    return 0, 0


def detect_frame(
    detector: Any,
    frame_path: str,
    width: int,
    height: int,
) -> list[Detection]:
    """Ask one detector backend for a frame's boxes and normalise them.

    ``detector`` only has to expose ``detect(frame_path) -> iterable``; a
    backend that raises is a provider bug, so the exception is propagated (the
    provider turns it into a failed run) rather than swallowed into "no faces".
    """
    raw = detector.detect(str(frame_path))
    return normalize_detections(raw, width, height)


def analyze(
    video_path: str | Path,
    *,
    detector: Any,
    fps: float = DEFAULT_SAMPLE_FPS,
    params: TrackParams | None = None,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
    keep_frames: bool = False,
) -> FaceTrackingResult:
    """Full pass over a real media file: sample -> detect -> associate.

    ``progress``/``should_cancel``/``deadline`` are honoured every sampled frame
    (contracts §1.1 cooperative work); ``ProviderCancelled``/``ProviderTimeout``
    escape unchanged. Frames are decoded into a private temp directory that is
    deleted again unless ``keep_frames`` is set -- the source is only read.

    ``t_s`` is the SAMPLE GRID position (``index / fps``). ffmpeg's ``fps``
    filter resamples by picking the source frame at/just after that point, so a
    sample's pixels can be up to one source frame later than its ``t_s`` when
    the sampling rate differs from the media rate. The drift is bounded and
    reported rather than interpolated away; sampling at the media's own rate
    makes it zero.

    Sampling cadence vs motion (MEASURED, default policy): a box that moves more
    than ~1/4 of its own width per sample drops below ``min_iou`` and the
    tracker deliberately starts a NEW track instead of linking across an
    unreliable step. Callers whose subject moves fast must raise ``sample_fps``;
    the conservative fragmentation is the price of never merging identities.
    """
    sample_fps = _clamp_float(fps, DEFAULT_SAMPLE_FPS, MIN_SAMPLE_FPS, MAX_SAMPLE_FPS)
    policy = params or TrackParams()
    width, height = frame_size(video_path)
    tracker = FaceTracker(policy)
    if progress is not None:
        progress(0.0)
    if width <= 0 or height <= 0:
        return FaceTrackingResult(
            warnings=["no decodable video stream; no face tracking was performed"],
            sample_fps=sample_fps,
        )
    if detector is None:
        # no detector => no face tracking, ever (contracts §8); never a fake box
        return FaceTrackingResult(
            warnings=["no face detector backend available; no face tracking was performed"],
            frame_width=width,
            frame_height=height,
            sample_fps=sample_fps,
        )

    work_dir = Path(tempfile.mkdtemp(prefix="ymoney-faces-"))
    try:
        frames = ff.extract_frames(video_path, sample_fps, work_dir)
        if not frames:
            return FaceTrackingResult(
                warnings=["ffmpeg produced no sampled frames; no face tracking was performed"],
                frame_width=width,
                frame_height=height,
                sample_fps=sample_fps,
            )
        total = len(frames)
        for index, frame in enumerate(frames):
            check_control(should_cancel, deadline)
            dets = detect_frame(detector, frame, width, height)
            tracker.update(index / sample_fps, dets)
            if progress is not None:
                progress(min(1.0, (index + 1) / total))
        tracks = tracker.finish()
        return FaceTrackingResult(
            tracks=tracks,
            warnings=tracker.warnings,
            frames=total,
            detections=tracker.detection_count,
            frame_width=width,
            frame_height=height,
            sample_fps=sample_fps,
            frame_dir=str(work_dir) if keep_frames else "",
        )
    finally:
        if not keep_frames:
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# persistence (append-only, contracts §8 + the Lane A schema)
# ---------------------------------------------------------------------------


def persist_tracks(
    db: Any,
    *,
    run_id: str,
    workspace_id: str,
    asset_id: str,
    tracks: Sequence[FaceTrack],
) -> dict:
    """Write ``face_tracks`` + ``face_track_samples`` rows for one run.

    Samples carry BOTH ``track_id`` (the ``face_tracks`` row id, per the schema
    FK) and the denormalised ``track_label`` (``FT_00``), so a consumer can
    group without a join. ``landmarks_json`` stays NULL when the detector
    supplied no landmarks. ``unresolved_crossing`` has no column in the
    Lane A schema, so it is returned to the caller for the run manifest.
    """
    from app.models.media_intel import FaceTrack as FaceTrackRow
    from app.models.media_intel import FaceTrackSample

    written = 0
    crossings: list[str] = []
    for track in tracks:
        row = FaceTrackRow(
            run_id=run_id,
            workspace_id=workspace_id,
            asset_id=asset_id,
            track_id=track.track_id,
            start_s=round(track.start_s, 4),
            end_s=round(track.end_s, 4),
            confidence_max=track.confidence_max,
            sample_count=track.sample_count,
            truncated=bool(track.truncated),
            reentry_count=int(track.reentry_count),
        )
        db.add(row)
        db.flush()
        if track.unresolved_crossing:
            crossings.append(track.track_id)
        for sample in track.samples:
            db.add(
                FaceTrackSample(
                    run_id=run_id,
                    workspace_id=workspace_id,
                    track_id=row.id,
                    track_label=track.track_id,
                    t_s=round(sample.t_s, 4),
                    x=round(sample.x, 3),
                    y=round(sample.y, 3),
                    w=round(sample.w, 3),
                    h=round(sample.h, 3),
                    confidence=None if sample.confidence is None else round(sample.confidence, 4),
                    landmarks_json=sample.landmarks if sample.has_landmarks else None,
                )
            )
            written += 1
    db.flush()
    return {
        "tracks": len(tracks),
        "samples": written,
        "unresolved_crossings": crossings,
        "truncated_tracks": [t.track_id for t in tracks if t.truncated],
        "reentry_total": sum(t.reentry_count for t in tracks),
    }


def track_row_dto(row: Any, samples: Sequence[Any] | None = None) -> dict:
    """DTO for one persisted ``face_tracks`` row (anonymous fields only)."""
    payload = {
        "id": row.id,
        "run_id": row.run_id,
        "track_id": str(row.track_id or ""),
        "start_s": round(float(row.start_s or 0.0), 4),
        "end_s": round(float(row.end_s or 0.0), 4),
        "sample_count": int(row.sample_count or 0),
        "confidence_max": (
            None if row.confidence_max is None else round(float(row.confidence_max), 4)
        ),
        "truncated": bool(row.truncated),
        "reentry_count": int(row.reentry_count or 0),
    }
    if samples is not None:
        payload["samples"] = [
            {
                "t_s": round(float(s.t_s or 0.0), 4),
                "x": round(float(s.x or 0.0), 3),
                "y": round(float(s.y or 0.0), 3),
                "w": round(float(s.w or 0.0), 3),
                "h": round(float(s.h or 0.0), 3),
                "confidence": None if s.confidence is None else round(float(s.confidence), 4),
                "landmarks": list(s.landmarks_json) if s.landmarks_json else None,
            }
            for s in samples
        ]
    return payload


__all__ = [
    "DEFAULT_AMBIGUITY_MARGIN",
    "DEFAULT_CENTER_TOLERANCE",
    "DEFAULT_MAX_GAP_FRAMES",
    "DEFAULT_MAX_SAMPLES",
    "DEFAULT_MIN_IOU",
    "DEFAULT_REENTRY_MIN_IOU",
    "DEFAULT_REENTRY_WINDOW_S",
    "DEFAULT_SAMPLE_FPS",
    "DEFAULT_SIZE_TOLERANCE",
    "MAX_SAMPLE_FPS",
    "MAX_WARNINGS",
    "MIN_BOX_SIDE",
    "MIN_MAX_SAMPLES",
    "MIN_SAMPLE_FPS",
    "Detection",
    "FaceTrack",
    "FaceTracker",
    "FaceTrackingResult",
    "TrackParams",
    "TrackSample",
    "analyze",
    "anonymous_track_label",
    "detect_frame",
    "frame_size",
    "iou",
    "normalize_detection",
    "normalize_detections",
    "persist_tracks",
    "track_detections",
    "track_row_dto",
]
