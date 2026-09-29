"""Dubbing plans: speaker rows + per-speaker timing fit (Work 07 Lane B).

A plan maps every source cue to exactly one speaker, and every speaker to
exactly one target voice. Timing fit reuses `providers.dubbing.fit_ratio`
for the speed-up clamp, then applies a per-speaker rate window:

* safe sync (rate inside the window) -> `planned_rate` is recorded;
* unsafe sync (needs more than the ceiling) -> the segment is flagged
  `needs_review` with a reason — never an absurd rate, never a silent pass.

Speakers are never fabricated: cues without any labels use the honest
single-speaker default; a *mix* of labelled and unlabelled cues is an error.
Voices are never fabricated either — an unmapped speaker keeps an empty
`target_voice` and its segments go to review with a remediation string.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from app.providers.dubbing import fit_ratio

# Sane speaking-rate window (multiples of natural speed).
MIN_SPEAKING_RATE = 0.75
MAX_SPEAKING_RATE = 1.35

DEFAULT_SINGLE_SPEAKER = "speaker_1"
PLAN_STATUSES = ("DRAFT", "READY", "REVIEW")


class PlanError(Exception):
    """Plan could not be built/fitted; the message carries remediation."""


class TimingConstraints(BaseModel):
    """Per-speaker rate window; clamped to the global sane range."""

    min_rate: float = MIN_SPEAKING_RATE
    max_rate: float = MAX_SPEAKING_RATE

    def clamped(self) -> tuple[float, float]:
        low = max(MIN_SPEAKING_RATE, min(self.min_rate, MAX_SPEAKING_RATE))
        high = max(low, min(self.max_rate, MAX_SPEAKING_RATE))
        return round(low, 3), round(high, 3)


class SpeakerPlan(BaseModel):
    """One speaker: exactly one source voice and one target voice."""

    speaker_id: str
    source_voice: str = ""
    target_voice: str = ""
    language: str = ""
    speaking_rate: float = 1.0
    pronunciation_rules: dict[str, str] = Field(default_factory=dict)
    timing_constraints: TimingConstraints = Field(default_factory=TimingConstraints)


class SegmentPlan(BaseModel):
    """One cue in the target language, tied to its speaker."""

    index: int
    speaker_id: str
    start: float
    end: float
    text: str = ""
    target_text: str = ""
    audio_seconds: float | None = None
    planned_rate: float = 1.0
    needs_review: bool = False
    review_reason: str = ""
    # structural (non-timing) review cause: speaker has no target voice yet
    voice_review: bool = False

    @property
    def window_seconds(self) -> float:
        return max(0.0, self.end - self.start)


class DubbingPlan(BaseModel):
    """Typed, persistable dubbing plan (stored as `dubbing_plans.plan_json`)."""

    target_language: str
    source_ref: str = ""
    speakers: list[SpeakerPlan] = Field(default_factory=list)
    segments: list[SegmentPlan] = Field(default_factory=list)
    status: str = "DRAFT"  # DRAFT | READY | REVIEW
    needs_review: bool = False
    review_count: int = 0
    notes: list[str] = Field(default_factory=list)

    def speaker(self, speaker_id: str) -> SpeakerPlan | None:
        for row in self.speakers:
            if row.speaker_id == speaker_id:
                return row
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "target_language": self.target_language,
            "speakers": len(self.speakers),
            "segments": len(self.segments),
            "status": self.status,
            "needs_review": self.needs_review,
            "review_count": self.review_count,
            "review_samples": [
                {"index": s.index, "reason": s.review_reason}
                for s in self.segments
                if s.needs_review
            ][:5],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


def _field(item: Any, *names: str, default: Any = None) -> Any:
    """Read a value from a mapping or an object (SrtCue-style)."""
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _normalize_cues(cues_with_speakers: Iterable[Any]) -> list[dict]:
    cues: list[dict] = []
    for pos, item in enumerate(cues_with_speakers, start=1):
        try:
            start = float(_field(item, "start", default=0.0) or 0.0)
            end = float(_field(item, "end", default=0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise PlanError(
                f"cue {pos}: start/end must be numbers — supply real timing "
                "before building a plan"
            ) from exc
        if end <= start:
            raise PlanError(
                f"cue {pos}: end ({end}) must be after start ({start}) — fix the "
                "transcript timing or drop the cue"
            )
        raw_index = _field(item, "index")
        try:
            index = int(raw_index) if raw_index is not None else pos
        except (TypeError, ValueError) as exc:
            raise PlanError(f"cue {pos}: index must be an integer") from exc
        speaker = _field(item, "speaker_id", "speaker")
        speaker_id = str(speaker).strip() if speaker is not None else ""
        cues.append(
            {
                "index": index,
                "start": start,
                "end": end,
                "text": str(_field(item, "text", default="") or ""),
                "target_text": str(_field(item, "target_text", default="") or ""),
                "speaker_id": speaker_id,
                "source_voice": str(_field(item, "source_voice", default="") or ""),
                "target_voice": str(_field(item, "target_voice", default="") or ""),
            }
        )
    if not cues:
        raise PlanError("no cues supplied — transcribe the source or paste an SRT first")
    indexes = [c["index"] for c in cues]
    if len(set(indexes)) != len(indexes):
        raise PlanError("cue indexes must be unique (they key measured audio durations)")
    return cues


def _assign_speakers(cues: list[dict]) -> list[str]:
    """Honest speaker labels: all-or-nothing, else the single-speaker default."""
    labelled = [c for c in cues if c["speaker_id"]]
    if not labelled:
        return [DEFAULT_SINGLE_SPEAKER]
    if len(labelled) != len(cues):
        raise PlanError(
            "some cues carry speaker labels and others do not — label every cue, "
            f"or label none to accept the single-speaker default '{DEFAULT_SINGLE_SPEAKER}'"
        )
    seen: list[str] = []
    for cue in cues:
        if cue["speaker_id"] not in seen:
            seen.append(cue["speaker_id"])
    return seen


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _voice_entry(voice_map: Mapping[str, Any] | None, speaker_id: str) -> dict:
    if not voice_map:
        return {}
    raw = voice_map.get(speaker_id)
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"target_voice": raw}
    if isinstance(raw, Mapping):
        return dict(raw)
    raise PlanError(
        f"voice_map[{speaker_id!r}] must be a voice id or a mapping — got {type(raw).__name__}"
    )


def build_plan(
    cues_with_speakers: Iterable[Any],
    target_lang: str,
    voice_map: Mapping[str, Any] | None = None,
    glossary: Mapping[str, str] | None = None,
    *,
    source_ref: str = "",
    pronunciation_rules: Mapping[str, str] | None = None,
) -> DubbingPlan:
    """Build a speaker-aware plan.

    Parameters
    ----------
    cues_with_speakers:
        Cues as mappings/objects with `start`, `end`, `text` and optionally
        `index`, `speaker_id`, `source_voice`, `target_voice`, `target_text`.
    target_lang:
        ISO-639-1 target language (applies to every speaker row).
    voice_map:
        `speaker_id -> voice id` (or a mapping with `target_voice`,
        `source_voice`, `speaking_rate`, `pronunciation_rules`,
        `timing_constraints`). One voice per speaker — sharing is rejected.
    glossary:
        `source term -> target term`, recorded as pronunciation rules on
        every speaker row (explicit per-speaker rules win).
    """
    lang = (target_lang or "").strip().lower()
    if not lang:
        raise PlanError("target_lang is required (e.g. es, fr, de, hi)")

    cues = _normalize_cues(cues_with_speakers)
    speaker_ids = _assign_speakers(cues)

    shared_rules = {**(glossary or {}), **(pronunciation_rules or {})}

    speakers: list[SpeakerPlan] = []
    seen_voices: dict[str, str] = {}
    for speaker_id in speaker_ids:
        entry = _voice_entry(voice_map, speaker_id)
        target_voice = str(entry.get("target_voice", "") or "").strip()
        if target_voice and target_voice in seen_voices:
            raise PlanError(
                f"speakers '{seen_voices[target_voice]}' and '{speaker_id}' share target "
                f"voice '{target_voice}' — assign one voice per speaker in voice_map"
            )
        if target_voice:
            seen_voices[target_voice] = speaker_id
        rules = {**shared_rules, **dict(entry.get("pronunciation_rules") or {})}
        timing = TimingConstraints(**dict(entry.get("timing_constraints") or {}))
        speakers.append(
            SpeakerPlan(
                speaker_id=speaker_id,
                source_voice=str(entry.get("source_voice", "") or "").strip(),
                target_voice=target_voice,
                language=lang,
                speaking_rate=float(entry.get("speaking_rate", 1.0) or 1.0),
                pronunciation_rules={str(k): str(v) for k, v in rules.items()},
                timing_constraints=timing,
            )
        )

    by_id = {s.speaker_id: s for s in speakers}
    segments: list[SegmentPlan] = []
    for cue in cues:
        speaker_id = cue["speaker_id"] or speaker_ids[0]
        speaker = by_id[speaker_id]
        seg = SegmentPlan(
            index=cue["index"],
            speaker_id=speaker_id,
            start=cue["start"],
            end=cue["end"],
            text=cue["text"],
            target_text=cue["target_text"],
        )
        if cue["source_voice"] and not speaker.source_voice:
            speaker.source_voice = cue["source_voice"]
        if cue["target_voice"] and not speaker.target_voice:
            if cue["target_voice"] in seen_voices:
                raise PlanError(
                    f"speakers '{seen_voices[cue['target_voice']]}' and '{speaker_id}' share "
                    f"target voice '{cue['target_voice']}' — one voice per speaker"
                )
            speaker.target_voice = cue["target_voice"]
            seen_voices[cue["target_voice"]] = speaker_id
        if not speaker.target_voice:
            seg.voice_review = True
            seg.needs_review = True
            seg.review_reason = (
                f"no target voice mapped for speaker '{speaker_id}' — set "
                f"voice_map['{speaker_id}'] to a {lang} TTS voice id"
            )
        segments.append(seg)

    plan = DubbingPlan(
        target_language=lang,
        source_ref=source_ref,
        speakers=speakers,
        segments=segments,
    )
    if any(not s.target_voice for s in speakers):
        plan.notes.append(
            "unmapped speaker voice(s) present — plan is REVIEW until voice_map covers them"
        )
    _refresh_status(plan, fitted=False)
    return plan


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def _segment_rate(
    audio: float, window: float, floor: float, ceiling: float
) -> tuple[float, str]:
    """Rate to apply for one segment; reason string when sync is unsafe."""
    if window <= 0:
        return 1.0, f"invalid timing window ({window:.2f}s) — fix cue start/end"
    if audio <= 0:
        return 1.0, "generated audio is empty (0s) — re-synthesize this segment"
    required = audio / window
    if required > ceiling:
        # Reuse the shared atempo clamp for the speed-up path; anything past
        # the speaker ceiling is a review case, not a chipmunk case.
        clamped = min(fit_ratio(audio, window), ceiling)
        return round(clamped, 3), (
            f"audio {audio:.2f}s needs {required:.2f}x to fit the {window:.2f}s window; "
            f"ceiling is {ceiling:.2f}x — shorten the line, extend the cue or re-record"
        )
    if required < floor:
        # Too short to slow down into the window: keep the floor (safe) and
        # let the assembler pad the remainder with silence.
        return round(floor, 3), ""
    return round(required, 3), ""


def fit_plan(
    plan: DubbingPlan,
    durations: Mapping[int, float] | None = None,
    *,
    probe: Callable[[SegmentPlan], float | None] | None = None,
) -> DubbingPlan:
    """Measure generated audio per segment and fit it into its cue window.

    `durations` maps segment index -> audio seconds; `probe` may supply a
    measurement for segments missing from `durations` (e.g. ffprobe). Segments
    whose audio cannot fit inside the window at the speaker ceiling — or that
    have a broken window / empty audio — are flagged `needs_review`.
    """
    durations = dict(durations or {})
    for seg in plan.segments:
        speaker = plan.speaker(seg.speaker_id)
        floor, ceiling = (
            speaker.timing_constraints.clamped()
            if speaker
            else (MIN_SPEAKING_RATE, MAX_SPEAKING_RATE)
        )
        audio = durations.get(seg.index)
        if audio is None and probe is not None:
            audio = probe(seg)
        if audio is None:
            continue  # unmeasured: untouched, no fabricated review flag
        try:
            audio_val = float(audio)
        except (TypeError, ValueError) as exc:
            raise PlanError(
                f"segment {seg.index}: measured duration must be a number, got {audio!r}"
            ) from exc
        seg.audio_seconds = round(audio_val, 3)
        rate, reason = _segment_rate(audio_val, seg.window_seconds, floor, ceiling)
        seg.planned_rate = rate
        if reason:
            seg.needs_review = True
            seg.review_reason = reason
        elif seg.needs_review and seg.review_reason.startswith(
            ("audio ", "generated ", "invalid ")
        ):
            seg.needs_review = False
            seg.review_reason = ""

    # One rate per speaker: the fastest segment it must serve.
    for speaker in plan.speakers:
        rates = [s.planned_rate for s in plan.segments if s.speaker_id == speaker.speaker_id]
        speaker.speaking_rate = round(max(rates), 3) if rates else 1.0

    _refresh_status(plan, fitted=True)
    return plan


def _refresh_status(plan: DubbingPlan, *, fitted: bool) -> None:
    review = [s for s in plan.segments if s.needs_review]
    plan.review_count = len(review)
    plan.needs_review = bool(review)
    if plan.needs_review:
        plan.status = "REVIEW"
    elif fitted:
        plan.status = "READY"
    else:
        plan.status = "DRAFT"


# ---------------------------------------------------------------------------
# Persistence (model `dubbing_plans`, migration 0022)
# ---------------------------------------------------------------------------


def plan_to_dict(plan: DubbingPlan) -> dict:
    return plan.model_dump(mode="json")


def plan_from_dict(data: Mapping[str, Any]) -> DubbingPlan:
    try:
        return DubbingPlan.model_validate(dict(data or {}))
    except Exception as exc:  # pydantic ValidationError — surface as a plan error
        raise PlanError(f"stored plan is not valid: {exc}") from exc


def save_plan(db, workspace_id: str, plan: DubbingPlan, *, source_ref: str = "") -> Any:
    """Persist (insert) a plan for a workspace; returns the ORM row."""
    from app.models.dubbing import DubbingPlanRow

    if source_ref:
        plan.source_ref = source_ref
    row = DubbingPlanRow(
        workspace_id=workspace_id,
        source_ref=plan.source_ref[:512],
        target_language=plan.target_language[:16],
        plan_json=plan_to_dict(plan),
        status=plan.status,
        needs_review=plan.needs_review,
        review_count=plan.review_count,
    )
    db.add(row)
    db.flush()
    plan_notes = dict(row.plan_json or {})
    plan_notes["id"] = row.id
    row.plan_json = plan_notes
    return row


def load_plan(db, workspace_id: str, plan_id: str) -> Any:
    """Load a plan row scoped to the workspace (None when cross-workspace)."""
    from app.models.dubbing import DubbingPlanRow

    row = db.get(DubbingPlanRow, plan_id)
    if row is None or row.workspace_id != workspace_id:
        return None
    return row


def list_plans(db, workspace_id: str, limit: int = 100) -> list[Any]:
    from sqlalchemy import select

    from app.models.dubbing import DubbingPlanRow

    return list(
        db.scalars(
            select(DubbingPlanRow)
            .where(DubbingPlanRow.workspace_id == workspace_id)
            .order_by(DubbingPlanRow.created_at.desc())
            .limit(min(limit, 200))
        ).all()
    )
