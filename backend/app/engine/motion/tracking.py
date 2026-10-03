"""Work 12 intelligence -> motion, with honest confidence gating (§10).

Motion may CONSUME media-intelligence evidence (word alignment, active
speaker, face tracks, masks). It may never run new inference and it may never
act on evidence it does not trust.

The rule this module enforces: **if tracking confidence is insufficient, no
automated tracked effect is attached.** :func:`subject_box_at` returns ``None``
rather than a guessed rectangle, and :func:`speaker_changes` returns only the
windows the stored evidence actually resolved. A caller that receives ``None``
records a QC finding; it does not fall back to a centre-frame guess.

Everything is workspace-scoped by construction: each read goes through the
Work 12 helpers that take ``workspace_id`` as their second argument, so a
foreign asset reads as absent.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "EvidenceUnavailable",
    "SpeakerChange",
    "SubjectBox",
    "evidence_summary",
    "resolve_speaker_changes",
    "subject_box_at",
]

#: Below this confidence a tracked effect is refused outright. Matches the
#: active-speaker default minimum so the two subsystems agree.
DEFAULT_MIN_CONFIDENCE = 0.55

#: A tracked box further than this from the nearest stored sample is treated
#: as untrusted: the subject may have left the frame.
DEFAULT_MAX_SAMPLE_GAP_S = 0.75


class EvidenceUnavailable(RuntimeError):
    """Raised only by strict callers; the helpers default to ``None``/empty."""


@dataclass(frozen=True)
class SubjectBox:
    """A tracked subject's box at one instant (source-frame pixels)."""

    x: float
    y: float
    w: float
    h: float
    t_s: float
    track_label: str
    confidence: float

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2.0

    @property
    def center_y(self) -> float:
        return self.y + self.h / 2.0

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "t_s": self.t_s, "track_label": self.track_label,
                "confidence": self.confidence,
                "center_x": self.center_x, "center_y": self.center_y}


@dataclass(frozen=True)
class SpeakerChange:
    """A resolved window where one speaker held the floor."""

    speaker_id: str
    label: str
    start_s: float
    end_s: float
    confidence: float | None = None
    face_track_id: str = ""

    def to_dict(self) -> dict:
        return {"speaker_id": self.speaker_id, "label": self.label,
                "start_s": self.start_s, "end_s": self.end_s,
                "confidence": self.confidence,
                "face_track_id": self.face_track_id}


def subject_box_at(
    db,
    workspace_id: str,
    asset_id: str,
    t_s: float,
    *,
    track_label: str = "",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_gap_s: float = DEFAULT_MAX_SAMPLE_GAP_S,
) -> SubjectBox | None:
    """Where a tracked subject is at time ``t_s``.

    Returns ``None`` when there is no evidence, when the nearest stored sample
    is too far away, or when its confidence is below ``min_confidence``. It
    NEVER interpolates across a gap wider than ``max_gap_s`` and never
    extrapolates past the last sample.
    """
    if not asset_id:
        return None
    try:
        from app.engine.intel import reframe
    except Exception:
        return None
    try:
        evidence = reframe.load_evidence(db, workspace_id, asset_id,
                                          track_run_id=None)
    except Exception:
        return None
    boxes = evidence.get("face_boxes") or {}
    candidates: list = []
    for label, frames in boxes.items():
        if track_label and label != track_label:
            continue
        for frame in frames or []:
            if abs(float(frame.t_s) - float(t_s)) <= max_gap_s:
                candidates.append(frame)
    if not candidates:
        return None
    nearest = min(candidates, key=lambda f: abs(float(f.t_s) - float(t_s)))
    confidence = float(getattr(nearest, "confidence", None) or 0.0)
    if confidence < min_confidence:
        return None
    return SubjectBox(
        x=float(nearest.x), y=float(nearest.y), w=float(nearest.w),
        h=float(nearest.h), t_s=float(nearest.t_s),
        track_label=str(getattr(nearest, "label", "") or ""),
        confidence=confidence,
    )


def resolve_speaker_changes(
    db,
    workspace_id: str,
    asset_id: str,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[SpeakerChange]:
    """Speaker windows for an asset, from STORED active-speaker rows only.

    Unresolved windows are skipped rather than guessed. Each returned window
    carries its real confidence, or ``None`` when the evidence genuinely has
    none.
    """
    if not asset_id:
        return []
    try:
        from app.engine.intel import active_speaker
    except Exception:
        return []
    try:
        run = active_speaker.latest_run_of_kind(db, workspace_id, asset_id,
                                                "active_speaker")
    except Exception:
        return []
    if run is None:
        return []
    try:
        rows = active_speaker.list_rows(db, workspace_id, run.id)
        labels = _alias_labels(db, workspace_id, asset_id)
    except Exception:
        return []
    out: list[SpeakerChange] = []
    for row in rows:
        status = str(getattr(row, "status", "") or "")
        if status != "RESOLVED":
            continue
        speaker_id = str(getattr(row, "speaker_id", "") or "")
        if not speaker_id:
            continue
        confidence = getattr(row, "confidence", None)
        if confidence is not None and float(confidence) < min_confidence:
            continue
        out.append(SpeakerChange(
            speaker_id=speaker_id,
            label=str(labels.get(speaker_id) or speaker_id),
            start_s=float(getattr(row, "start_s", 0.0)),
            end_s=float(getattr(row, "end_s", 0.0)),
            confidence=(None if confidence is None else float(confidence)),
            face_track_id=str(getattr(row, "face_track_id", "") or ""),
        ))
    out.sort(key=lambda c: c.start_s)
    return out


def _alias_labels(db, workspace_id: str, asset_id: str) -> dict[str, str]:
    try:
        from app.engine.intel import alignment

        return dict(alignment.alias_map(db, workspace_id, asset_id=asset_id))
    except Exception:
        return {}


def evidence_summary(
    db,
    workspace_id: str,
    asset_id: str,
) -> dict:
    """What motion may and may not do with this asset's evidence.

    Read by the editor so a user can see WHY an automated effect was not
    attached, instead of the effect silently not appearing.
    """
    from app.engine.captions.words import load_word_timings

    words = load_word_timings(db, workspace_id, asset_id=asset_id)
    speakers = resolve_speaker_changes(db, workspace_id, asset_id)
    try:
        from app.engine.intel import active_speaker

        face_run = active_speaker.latest_run_of_kind(
            db, workspace_id, asset_id, "face_tracking")
    except Exception:
        face_run = None
    try:
        from app.engine.intel import reframe as _reframe

        mask = _reframe.find_mask(db, workspace_id, asset_id, kind="PERSON")
        mask_available = mask is not None
    except Exception:
        mask_available = False
    return {
        "asset_id": asset_id,
        "word_alignment": words.to_dict(),
        "speaker_windows": [c.to_dict() for c in speakers],
        "face_tracking": bool(face_run is not None),
        "subject_mask": bool(mask_available),
        "allows": {
            "word_level_captions": words.word_level,
            "speaker_lower_thirds": bool(speakers),
            "tracked_callouts": face_run is not None,
            "background_blur": mask_available,
        },
        "blocked_reasons": [
            reason for reason, ok in (
                (words.reason or "no word alignment", words.word_level),
                ("no resolved active-speaker window", bool(speakers)),
                ("no face-tracking run", face_run is not None),
                ("no subject mask asset", mask_available),
            ) if not ok
        ],
        "min_confidence": DEFAULT_MIN_CONFIDENCE,
    }