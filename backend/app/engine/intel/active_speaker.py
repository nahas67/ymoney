"""Active-speaker mapping: who is speaking, and can we prove it (Lane F).

Contracts §10. Diarization knows WHEN somebody talks. A face tracker knows WHERE
faces are. Neither knows that the voice and the face belong to the same person,
and this module exists to refuse that conclusion until the evidence clears an
explicit bar:

* a speaker window is matched to a face track by TEMPORAL OVERLAP (plus the
  sampled box coverage, which is finer than the track's coarse window);
* when two or more faces explain the same speaker window equally well, the row
  is ``UNRESOLVED/ambiguous_tie`` -- picking the bigger face would be a guess,
  and a guess here silently mis-attributes a face to a person;
* when nothing explains the window, the row is ``UNRESOLVED/low_overlap``;
* with no diarization or no face tracks at all, the honest reason is
  ``no_diarization`` / ``no_face_track``.

Motion energy (:func:`app.engine.intel.ffmpeg_util.frame_difference_energy`) is
**optional supporting evidence only**. It is labelled in ``evidence`` and it
lifts the reported ``confidence`` of an already-resolved row, but it can never
settle a tie -- a talking face is not the only thing that moves on screen, so
letting motion break ``ambiguous_tie`` would manufacture an identity from
lighting and camera shake.

There is no sensitive inference anywhere in this module: speakers are anonymous
``SPEAKER_00``-style labels and faces are session-local ``FT_00`` tracks. No
gender, age, race, identity or biometric attribute is derived, stored or
returned (contracts §0).

Interop with Lane E's face tracker (the three facts that shape this file):

* ``face_track_samples.x/y/w/h`` are **source-frame PIXELS** -- the tracker
  clamps them to the probed video width/height and never normalises them to
  ``[0, 1]``. This mapper therefore consumes only a sample's ``t_s`` and never
  compares geometry, which makes its evidence scale-free by construction. Any
  future size-aware check must compare against the SOURCE width/height, never
  against a normalised or assumed box.
* A sample's ``track_id`` is the ``face_tracks`` ROW id (the FK); the ``FT_00``
  label is denormalised into ``track_label``. Samples are indexed under BOTH
  keys so coverage works whichever one a caller hands over.
* ``unresolved_crossing`` is NOT a column. It is a property of the in-memory
  track and is persisted as the list of track LABELS in
  ``runs.metrics_json["unresolved_crossings"]``. This module surfaces that list
  as provenance (a warning plus per-row metrics); it deliberately does NOT turn
  it into a status change or a confidence penalty, because contracts §10 defines
  no threshold for that and inventing one would be exactly the fabricated rule
  this module exists to avoid.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engine.intel import ffmpeg_util

logger = logging.getLogger("ymoney.intel")

#: vocabulary (contracts §10 + models.media_intel.ACTIVE_SPEAKER_STATUSES)
STATUS_RESOLVED = "RESOLVED"
STATUS_UNRESOLVED = "UNRESOLVED"
ACTIVE_SPEAKER_STATUSES: tuple[str, ...] = (STATUS_RESOLVED, STATUS_UNRESOLVED)

#: every machine-readable unresolved reason, in the order the mapper tests them.
REASON_NO_DIARIZATION = "no_diarization"
REASON_NO_FACE_TRACK = "no_face_track"
REASON_AMBIGUOUS_TIE = "ambiguous_tie"
REASON_LOW_OVERLAP = "low_overlap"
REASON_LOW_CONFIDENCE = "low_confidence"
UNRESOLVED_REASONS: tuple[str, ...] = (
    REASON_NO_DIARIZATION,
    REASON_NO_FACE_TRACK,
    REASON_AMBIGUOUS_TIE,
    REASON_LOW_OVERLAP,
    REASON_LOW_CONFIDENCE,
)

#: evidence labels surfaced on every row (contracts §10)
EVIDENCE_OVERLAP = "overlap"
EVIDENCE_MOTION = "motion"
MOTION_EVIDENCE_METHOD = "motion_energy"

#: default thresholds. Deliberately explicit and overridable per call: a caller
#: that wants a different bar must say so, rather than inherit a hidden default.
DEFAULT_MIN_OVERLAP = 0.35
DEFAULT_MIN_CONFIDENCE = 0.55
DEFAULT_TIE_MARGIN = 0.10
DEFAULT_MOTION_WEIGHT = 0.15
DEFAULT_MOTION_FPS = 2.0
#: frame-difference energy (mean |delta luma| / 255) below which motion is treated
#: as absent. Measured, not guessed: the fixtures put a moving subject near 0.01.
DEFAULT_MOTION_FLOOR = 0.004
#: energy that counts as FULL motion support (4x the floor).
MOTION_REFERENCE_FACTOR = 4.0


@dataclass(frozen=True)
class SpeakerWindow:
    """One ``kind='SPEAKER'`` diarization segment (an anonymous speaker turn)."""

    speaker_id: str
    start_s: float
    end_s: float
    confidence: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end_s) - float(self.start_s))


@dataclass(frozen=True)
class FaceWindow:
    """One ``face_tracks`` row plus the samples that refine its coverage.

    ``row_id`` is the ``face_tracks.id`` FK; ``label`` is the ``FT_00`` session
    label a caller sees. Sample geometry is deliberately ABSENT: it is
    source-frame pixels, and this mapper's evidence is temporal only.
    """

    label: str
    start_s: float
    end_s: float
    row_id: str = ""
    confidence_max: float | None = None
    samples: tuple[float, ...] = ()
    truncated: bool = False
    reentry_count: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end_s) - float(self.start_s))


@dataclass(frozen=True)
class ActiveSpeakerRow:
    """One output row. ``status`` is never optimistic (contracts §10)."""

    speaker_id: str | None
    face_track_id: str | None
    start_s: float
    end_s: float
    confidence: float | None
    status: str
    reason: str = ""
    face_track_label: str | None = None
    evidence: tuple[str, ...] = ()
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "speaker_id": self.speaker_id,
            "face_track_id": self.face_track_id,
            "face_track_label": self.face_track_label,
            "start_s": round(float(self.start_s), 4),
            "end_s": round(float(self.end_s), 4),
            "confidence": (round(float(self.confidence), 4)
                           if self.confidence is not None else None),
            "status": self.status,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "metrics": dict(self.metrics or {}),
        }


@dataclass(frozen=True)
class ActiveSpeakerReport:
    """The whole mapping: rows plus the honest counters around them."""

    rows: tuple[ActiveSpeakerRow, ...]
    evidence_sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    motion_measured: bool = False
    thresholds: dict = field(default_factory=dict)
    #: track LABELS the face-tracking run flagged as having an unresolved
    #: crossing (contracts §8); provenance only, never a status input.
    unresolved_crossings: tuple[str, ...] = ()

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.rows if r.status == STATUS_RESOLVED)

    @property
    def unresolved(self) -> int:
        return sum(1 for r in self.rows if r.status == STATUS_UNRESOLVED)

    def reason_histogram(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            if row.status == STATUS_UNRESOLVED and row.reason:
                counts[row.reason] = counts.get(row.reason, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "items": [r.to_dict() for r in self.rows],
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "evidence_sources": list(self.evidence_sources),
            "warnings": list(self.warnings),
            "motion_measured": bool(self.motion_measured),
            "thresholds": dict(self.thresholds or {}),
            "unresolved_crossings": list(self.unresolved_crossings),
        }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _overlap_seconds(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(float(a1), float(b1)) - max(float(a0), float(b0)))


def _crossing_labels(value: Sequence[str] | int | None) -> tuple[str, ...]:
    """Normalise the ``unresolved_crossings`` input to a tuple of track labels.

    Accepts Lane E's ``["FT_02", ...]`` label list, a bare count (surfaced as
    ``"#N"`` so the provenance is not lost), a single label string, or nothing.
    Never raises and never invents a label.
    """
    if value is None or isinstance(value, bool):
        return ()
    if isinstance(value, int):
        return (f"#{int(value)}",) if value > 0 else ()
    if isinstance(value, str):
        # A bare string is a shape Lane E never writes; keep it as ONE label
        # rather than iterating it character by character.
        name = value.strip()
        return (name,) if name else ()
    labels: list[str] = []
    for item in value or ():
        name = str(item or "").strip()
        if name and name not in labels:
            labels.append(name)
    return tuple(labels)


def normalise_segments(rows: Iterable[Any]) -> list[SpeakerWindow]:
    """Keep only real SPEAKER rows; ``SPEECH_ACTIVITY`` is VAD, not a speaker.

    A row without a ``speaker_id`` is not a speaker turn (contracts §4), so it is
    dropped rather than mapped with an invented identity.
    """
    windows: list[SpeakerWindow] = []
    for row in rows or []:
        if isinstance(row, SpeakerWindow):
            windows.append(row)
            continue
        data = _row_data(row)
        kind = str(data.get("kind") or "SPEAKER").upper()
        speaker_id = str(data.get("speaker_id") or "").strip()
        if kind != "SPEAKER" or not speaker_id:
            continue
        start = _as_float(data.get("start_s"))
        end = _as_float(data.get("end_s"))
        if end <= start:
            continue
        confidence = data.get("confidence")
        windows.append(
            SpeakerWindow(
                speaker_id=speaker_id,
                start_s=start,
                end_s=end,
                confidence=float(confidence) if confidence is not None else None,
            )
        )
    windows.sort(key=lambda w: (w.start_s, w.speaker_id))
    return windows


def normalise_tracks(rows: Iterable[Any], samples: Iterable[Any] | None = None) -> list[FaceWindow]:
    """Build face windows from ``face_tracks`` rows (+ their sample times).

    Lane E writes ``FaceTrackSample.track_id`` = the track ROW id and
    ``track_label`` = the ``FT_00`` label, so sample times are indexed under both
    keys; a caller that hands over only labels (or only row ids) still gets
    coverage. Only ``t_s`` is read -- the boxes are source-frame pixels and this
    mapper never compares geometry.
    """
    by_key: dict[str, list[float]] = {}
    for sample in samples or []:
        data = _row_data(sample)
        t_s = data.get("t_s")
        if t_s is None:
            continue
        for key in (data.get("track_id"), data.get("track_label")):
            name = str(key or "").strip()
            if name:
                by_key.setdefault(name, []).append(_as_float(t_s))
    windows: list[FaceWindow] = []
    for row in rows or []:
        if isinstance(row, FaceWindow):
            windows.append(row)
            continue
        data = _row_data(row)
        label = str(data.get("track_id") or data.get("label") or "").strip()
        row_id = str(data.get("id") or data.get("row_id") or "").strip()
        start = _as_float(data.get("start_s"))
        end = _as_float(data.get("end_s"))
        if end < start:
            start, end = end, start
        times = tuple(sorted(by_key.get(row_id, []) or by_key.get(label, [])))
        if times:
            start = min(start, times[0])
            end = max(end, times[-1])
        if not label and not row_id:
            continue
        confidence = data.get("confidence_max")
        windows.append(
            FaceWindow(
                label=label or row_id,
                start_s=start,
                end_s=end,
                row_id=row_id,
                confidence_max=float(confidence) if confidence is not None else None,
                samples=times,
                truncated=bool(data.get("truncated")),
                reentry_count=int(_as_float(data.get("reentry_count"))),
            )
        )
    windows.sort(key=lambda w: (w.start_s, w.label))
    return windows


def _row_data(row: Any) -> dict:
    """Read a dict, an ORM row, or any object exposing the expected attributes.

    The attribute list is the READ contract with Lane E's tables. It contains no
    ``unresolved_crossing``: that field has no column (it is reconstructed from
    ``runs.metrics_json`` by the caller), so reading it here would always be a
    silent ``None``.
    """
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "_mapping"):  # SQLAlchemy Row
        return dict(row._mapping)
    return {
        name: getattr(row, name)
        for name in (
            "id", "run_id", "kind", "speaker_id", "start_s", "end_s", "confidence",
            "track_id", "track_label", "t_s", "confidence_max", "truncated",
            "reentry_count",
        )
        if hasattr(row, name)
    }


def _support_for_window(
    window: SpeakerWindow, face: FaceWindow
) -> tuple[float, float, float]:
    """``(overlap_ratio, sample_coverage, overlap_seconds)`` for one candidate.

    Two independent views of the same fact, because a coarse track window can
    make a genuinely-on-camera face look absent AND a long track can make a
    briefly-on-camera face look absent:

    * ``overlap_ratio`` -- the fraction of the SPEAKER window inside the track
      window (coarse, from ``face_tracks.start_s/end_s``);
    * ``sample_coverage`` -- the fraction of the track's sampled boxes that fall
      inside the speaker window (finer, from ``face_track_samples.t_s``).

    Either can be the larger: a face on camera for 1 s inside a 10 s speaker turn
    has a low window ratio but full sample coverage, while a 10 s track with
    samples only at its edges has the opposite. The mapper judges on the LARGER
    of the two (the better-supported view) and reports both in ``metrics``.
    Only timestamps are read -- the sample boxes are source-frame pixels.
    """
    span = window.duration
    overlap = _overlap_seconds(window.start_s, window.end_s, face.start_s, face.end_s)
    overlap_ratio = (overlap / span) if span > 0 else 0.0
    coverage = 0.0
    if face.samples:
        inside = sum(
            1 for t in face.samples if window.start_s <= t <= window.end_s
        )
        coverage = inside / float(len(face.samples))
    return min(1.0, overlap_ratio), min(1.0, coverage), overlap


def _mean_energy(energy: Sequence[float] | None, start: float, end: float, fps: float) -> float:
    """Mean frame-difference energy over ``[start, end]``; 0.0 when unknown.

    ``frame_difference_energy`` returns one value per consecutive frame PAIR, so
    index ``i`` covers ``[i/fps, (i+1)/fps]``; the window is intersected with
    those intervals rather than with points.
    """
    if not energy or fps <= 0:
        return 0.0
    total = 0.0
    weight = 0.0
    for index, value in enumerate(energy):
        lo = index / fps
        hi = (index + 1) / fps
        span = _overlap_seconds(lo, hi, start, end)
        if span <= 0:
            continue
        total += float(value) * span
        weight += span
    return (total / weight) if weight > 0 else 0.0


class ActiveSpeakerMapper:
    """Diarization + face windows (+ optional motion) -> resolved/unresolved rows.

    Thresholds are constructor arguments with explicit defaults; every one is
    echoed in :attr:`thresholds` so a stored row's evidence can be re-checked
    against the bar it was judged by.
    """

    def __init__(
        self,
        *,
        min_overlap: float = DEFAULT_MIN_OVERLAP,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        tie_margin: float = DEFAULT_TIE_MARGIN,
        motion_weight: float = DEFAULT_MOTION_WEIGHT,
        motion_floor: float = DEFAULT_MOTION_FLOOR,
        motion_fps: float = DEFAULT_MOTION_FPS,
    ) -> None:
        """Thresholds are explicit and reported, never buried.

        ``min_overlap`` is the floor on the best available temporal support: the
        track-window overlap OR the finer ``face_track_samples`` coverage, whichever
        is higher (see :func:`_support_for_window`).
        """
        self.min_overlap = float(min_overlap)
        self.min_confidence = float(min_confidence)
        self.tie_margin = float(tie_margin)
        self.motion_weight = max(0.0, float(motion_weight))
        self.motion_floor = max(0.0, float(motion_floor))
        self.motion_fps = float(motion_fps)

    @property
    def thresholds(self) -> dict:
        return {
            "min_overlap": round(self.min_overlap, 4),
            "min_confidence": round(self.min_confidence, 4),
            "tie_margin": round(self.tie_margin, 4),
            "motion_weight": round(self.motion_weight, 4),
            "motion_floor": round(self.motion_floor, 4),
            "motion_fps": round(self.motion_fps, 4),
        }

    # -- evidence --------------------------------------------------------

    def measure_motion(
        self, media_path: str | Path | None, *, fps: float | None = None
    ) -> tuple[list[float], str]:
        """Frame-difference energy for the clip, measured locally via ffmpeg.

        Returns ``(energy, reason)``; ``reason`` is non-empty when the evidence
        could not be measured, which is reported as a warning rather than as
        motion that was never there.
        """
        rate = float(fps or self.motion_fps)
        if not str(media_path or ""):
            return [], "no media path supplied for motion evidence"
        energy = ffmpeg_util.frame_difference_energy(str(media_path), rate)
        if not energy:
            return [], "frame_difference_energy returned no samples (no video stream?)"
        return energy, ""

    def _motion_support(self, energy: Sequence[float] | None, start: float, end: float) -> float:
        """Motion support in ``[0, 1]`` -- evidence strength, never a decision."""
        mean = _mean_energy(energy, start, end, self.motion_fps)
        if mean < self.motion_floor:
            return 0.0
        reference = max(self.motion_floor * MOTION_REFERENCE_FACTOR, 1e-9)
        return max(0.0, min(1.0, mean / reference))

    # -- the mapping -----------------------------------------------------

    def map(
        self,
        *,
        segments: Iterable[Any] = (),
        tracks: Iterable[Any] = (),
        samples: Iterable[Any] | None = None,
        motion_energy: Sequence[float] | None = None,
        use_motion: bool = False,
        media_path: str | Path | None = None,
        unresolved_crossings: Sequence[str] | int = (),
    ) -> ActiveSpeakerReport:
        """Map speakers to faces. Never raises; never guesses.

        ``unresolved_crossings`` carries the track LABELS Lane E flagged in the
        face run's ``metrics_json`` (there is no column for it). It is recorded
        as provenance on the report and on every row, and it never changes a
        status or a confidence: contracts §10 defines no threshold for it, and a
        rule invented here would be a fabricated one.
        """
        windows = normalise_segments(segments)
        faces = normalise_tracks(tracks, samples)
        warnings: list[str] = []
        evidence_sources: list[str] = [EVIDENCE_OVERLAP]

        crossings = _crossing_labels(unresolved_crossings)
        if crossings:
            warnings.append(
                f"face tracking reported {len(crossings)} unresolved crossing(s) "
                f"({', '.join(crossings)}); those tracks' face<->speaker "
                "association is less certain"
            )

        energy: list[float] = list(motion_energy or [])
        motion_measured = bool(energy)
        if use_motion and not energy:
            energy, reason = self.measure_motion(media_path)
            motion_measured = bool(energy)
            if not energy:
                warnings.append(f"motion evidence unavailable: {reason}")
        if motion_measured:
            evidence_sources.append(EVIDENCE_MOTION)

        thresholds = self.thresholds

        if not windows:
            # Without speaker turns there is nothing to map; say so explicitly
            # rather than emitting a face-only row that implies attribution.
            start = min((f.start_s for f in faces), default=0.0)
            end = max((f.end_s for f in faces), default=0.0)
            row = ActiveSpeakerRow(
                speaker_id=None,
                face_track_id=None,
                start_s=start,
                end_s=end,
                confidence=None,
                status=STATUS_UNRESOLVED,
                reason=REASON_NO_DIARIZATION,
                evidence=tuple(evidence_sources),
                metrics={
                    "speaker_windows": 0,
                    "face_windows": len(faces),
                    "unresolved_crossings": list(crossings),
                },
            )
            return ActiveSpeakerReport(
                rows=(row,),
                evidence_sources=tuple(evidence_sources),
                warnings=tuple(warnings),
                motion_measured=motion_measured,
                thresholds=thresholds,
                unresolved_crossings=crossings,
            )

        if not faces:
            rows = tuple(
                ActiveSpeakerRow(
                    speaker_id=w.speaker_id,
                    face_track_id=None,
                    start_s=w.start_s,
                    end_s=w.end_s,
                    confidence=None,
                    status=STATUS_UNRESOLVED,
                    reason=REASON_NO_FACE_TRACK,
                    evidence=tuple(evidence_sources),
                    metrics={
                        "face_windows": 0,
                        "overlap_ratio": 0.0,
                        "unresolved_crossings": list(crossings),
                    },
                )
                for w in windows
            )
            return ActiveSpeakerReport(
                rows=rows,
                evidence_sources=tuple(evidence_sources),
                warnings=tuple(warnings),
                motion_measured=motion_measured,
                thresholds=thresholds,
                unresolved_crossings=crossings,
            )

        rows: list[ActiveSpeakerRow] = []
        for window in windows:
            scored: list[tuple[float, float, float, FaceWindow]] = []
            for face in faces:
                overlap_ratio, coverage, overlap = _support_for_window(window, face)
                scored.append((max(overlap_ratio, coverage), overlap_ratio, overlap, face))
            # Deterministic order: strongest support first, ties broken by label
            # so two runs over the same inputs produce the same report.
            scored.sort(key=lambda item: (-item[0], item[3].label))
            best_score, best_overlap, best_seconds, best_face = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            motion = (
                self._motion_support(energy, window.start_s, window.end_s)
                if motion_measured
                else 0.0
            )
            evidence = tuple(evidence_sources)
            metrics = {
                "overlap_ratio": round(best_overlap, 4),
                "overlap_seconds": round(best_seconds, 4),
                "runner_up_ratio": round(runner_up, 4),
                "margin": round(best_score - runner_up, 4),
                "motion_support": round(motion, 4),
                "face_candidates": len(faces),
                # provenance from Lane E's manifest, plus the winning track's own
                # honesty flags (both are real columns on face_tracks)
                "unresolved_crossings": list(crossings),
                "best_track_crossing": best_face.label in crossings,
                "best_track_truncated": bool(best_face.truncated),
                "best_track_reentries": int(best_face.reentry_count),
            }

            # 1) a tie is never broken, whatever the motion says.
            if len(scored) > 1 and (best_score - runner_up) <= self.tie_margin:
                rows.append(
                    ActiveSpeakerRow(
                        speaker_id=window.speaker_id,
                        face_track_id=None,
                        start_s=window.start_s,
                        end_s=window.end_s,
                        confidence=round(min(1.0, best_score + self.motion_weight * motion), 4),
                        status=STATUS_UNRESOLVED,
                        reason=REASON_AMBIGUOUS_TIE,
                        evidence=evidence,
                        metrics=metrics,
                    )
                )
                continue
            # 2) nothing explains the window. The floor is judged on the BEST
            # temporal support available (window overlap or the finer sample
            # coverage), not on the coarse ratio alone -- see _support_for_window.
            if best_score < self.min_overlap:
                rows.append(
                    ActiveSpeakerRow(
                        speaker_id=window.speaker_id,
                        face_track_id=None,
                        start_s=window.start_s,
                        end_s=window.end_s,
                        confidence=None,
                        status=STATUS_UNRESOLVED,
                        reason=REASON_LOW_OVERLAP,
                        evidence=evidence,
                        metrics=metrics,
                    )
                )
                continue
            confidence = min(1.0, best_score + self.motion_weight * motion)
            # 3) overlap cleared the floor but the combined evidence is thin.
            if confidence < self.min_confidence:
                rows.append(
                    ActiveSpeakerRow(
                        speaker_id=window.speaker_id,
                        face_track_id=None,
                        start_s=window.start_s,
                        end_s=window.end_s,
                        confidence=round(confidence, 4),
                        status=STATUS_UNRESOLVED,
                        reason=REASON_LOW_CONFIDENCE,
                        evidence=evidence,
                        metrics=metrics,
                    )
                )
                continue
            rows.append(
                ActiveSpeakerRow(
                    speaker_id=window.speaker_id,
                    face_track_id=best_face.row_id or None,
                    start_s=window.start_s,
                    end_s=window.end_s,
                    confidence=round(confidence, 4),
                    status=STATUS_RESOLVED,
                    face_track_label=best_face.label,
                    evidence=evidence,
                    metrics=metrics,
                )
            )
        return ActiveSpeakerReport(
            rows=tuple(rows),
            evidence_sources=tuple(evidence_sources),
            warnings=tuple(warnings),
            motion_measured=motion_measured,
            thresholds=thresholds,
            unresolved_crossings=crossings,
        )


# ---------------------------------------------------------------------------
# persistence + DTOs
# ---------------------------------------------------------------------------


def persist_report(
    db: Any,
    *,
    workspace_id: str,
    run_id: str,
    asset_id: str,
    report: ActiveSpeakerReport,
) -> list[ActiveSpeakerRow]:
    """Append one ``active_speaker_map`` row per mapping decision.

    Append-only per run (contracts §2): re-running the capability creates a new
    run rather than rewriting history. ``speaker_id``/``face_track_id`` stay
    NULL on unresolved rows -- an unresolved row that carries an invented id is
    the failure mode this whole module exists to prevent.
    """
    from app.models import ActiveSpeakerMap

    stored: list[ActiveSpeakerRow] = []
    for row in report.rows:
        db.add(
            ActiveSpeakerMap(
                run_id=run_id,
                workspace_id=workspace_id,
                asset_id=asset_id,
                speaker_id=row.speaker_id,
                face_track_id=row.face_track_id,
                start_s=float(row.start_s),
                end_s=float(row.end_s),
                confidence=(float(row.confidence) if row.confidence is not None else None),
                status=row.status,
                reason=str(row.reason or "")[:60],
            )
        )
        stored.append(row)
    db.flush()
    return stored


def row_dto(row: Any) -> dict:
    """API shape for one stored mapping row (contracts §14)."""
    return {
        "id": row.id,
        "run_id": row.run_id,
        "workspace_id": row.workspace_id,
        "asset_id": row.asset_id,
        "speaker_id": row.speaker_id,
        "face_track_id": row.face_track_id,
        "start_s": round(float(row.start_s or 0.0), 4),
        "end_s": round(float(row.end_s or 0.0), 4),
        "confidence": (
            round(float(row.confidence), 4) if row.confidence is not None else None
        ),
        "status": str(row.status or STATUS_UNRESOLVED),
        "reason": str(row.reason or ""),
    }


def list_rows(db: Any, workspace_id: str, run_id: str) -> list[Any]:
    """Workspace-scoped rows for one run, in media time."""
    from sqlalchemy import select

    from app.models import ActiveSpeakerMap

    return list(
        db.scalars(
            select(ActiveSpeakerMap)
            .where(
                ActiveSpeakerMap.workspace_id == str(workspace_id),
                ActiveSpeakerMap.run_id == str(run_id),
            )
            .order_by(ActiveSpeakerMap.start_s.asc())
        ).all()
    )


def load_diarization(db: Any, workspace_id: str, run_id: str) -> list[Any]:
    """SPEAKER rows of one diarization run (workspace-scoped)."""
    from sqlalchemy import select

    from app.models import DiarizationSegment

    return list(
        db.scalars(
            select(DiarizationSegment)
            .where(
                DiarizationSegment.workspace_id == str(workspace_id),
                DiarizationSegment.run_id == str(run_id),
            )
            .order_by(DiarizationSegment.start_s.asc())
        ).all()
    )


def load_face_tracks(db: Any, workspace_id: str, run_id: str) -> tuple[list[Any], list[Any]]:
    """``(tracks, samples)`` of one face-tracking run (workspace-scoped).

    Sample rows are returned whole (Lane E's shape: ``track_id`` = the track ROW
    id, ``track_label`` = ``FT_00``, boxes in source-frame pixels). The mapper
    reads only ``t_s`` from them, so the pixel geometry passes through untouched.
    """
    from sqlalchemy import select

    from app.models import FaceTrack, FaceTrackSample

    tracks = list(
        db.scalars(
            select(FaceTrack)
            .where(
                FaceTrack.workspace_id == str(workspace_id),
                FaceTrack.run_id == str(run_id),
            )
            .order_by(FaceTrack.start_s.asc())
        ).all()
    )
    samples = list(
        db.scalars(
            select(FaceTrackSample)
            .where(
                FaceTrackSample.workspace_id == str(workspace_id),
                FaceTrackSample.run_id == str(run_id),
            )
            .order_by(FaceTrackSample.t_s.asc())
        ).all()
    )
    return tracks, samples


def face_track_crossings(run: Any) -> tuple[str, ...]:
    """``unresolved_crossings`` for a face-tracking run, read from its MANIFEST.

    There is no ``unresolved_crossing`` column (contracts §8/§2): Lane E persists
    the flagged track LABELS into ``media_intel_runs.metrics_json``. Returns
    ``()`` for a missing/empty/malformed manifest -- provenance only, and an
    unreadable manifest is never treated as "no crossings".
    """
    metrics = getattr(run, "metrics_json", None)
    if not isinstance(metrics, dict):
        return ()
    return _crossing_labels(metrics.get("unresolved_crossings"))


def latest_run_of_kind(
    db: Any, workspace_id: str, asset_id: str, kind: str
) -> Any | None:
    """Newest COMPLETED run of ``kind`` for one asset in one workspace.

    ``None`` when there is none -- the caller then reports the honest reason
    ("no face-tracking run for this asset") instead of mapping against nothing.
    """
    from sqlalchemy import select

    from app.models import MediaIntelRun

    return db.scalar(
        select(MediaIntelRun)
        .where(
            MediaIntelRun.workspace_id == str(workspace_id),
            MediaIntelRun.asset_id == str(asset_id),
            MediaIntelRun.kind == str(kind),
            MediaIntelRun.status == "COMPLETED",
        )
        .order_by(MediaIntelRun.created_at.desc())
        .limit(1)
    )


__all__ = [
    "ACTIVE_SPEAKER_STATUSES",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_OVERLAP",
    "DEFAULT_MOTION_FLOOR",
    "DEFAULT_MOTION_FPS",
    "DEFAULT_MOTION_WEIGHT",
    "DEFAULT_TIE_MARGIN",
    "EVIDENCE_MOTION",
    "EVIDENCE_OVERLAP",
    "MOTION_EVIDENCE_METHOD",
    "REASON_AMBIGUOUS_TIE",
    "REASON_LOW_CONFIDENCE",
    "REASON_LOW_OVERLAP",
    "REASON_NO_DIARIZATION",
    "REASON_NO_FACE_TRACK",
    "STATUS_RESOLVED",
    "STATUS_UNRESOLVED",
    "UNRESOLVED_REASONS",
    "ActiveSpeakerMapper",
    "ActiveSpeakerReport",
    "ActiveSpeakerRow",
    "FaceWindow",
    "SpeakerWindow",
    "face_track_crossings",
    "latest_run_of_kind",
    "list_rows",
    "load_diarization",
    "load_face_tracks",
    "normalise_segments",
    "normalise_tracks",
    "persist_report",
    "row_dto",
]
