"""Word alignment, speaker mapping and anonymous speaker identity (Lane B).

Contracts §4 + §5. The engine is pure orchestration + measurement: it resolves a
provider through the registry, drives one run through
``app.services.media_intel_runs`` (so the cache/chunk/cost rules of contracts §3
exist exactly once), and persists the rows. It never imports a model and never
computes an alignment itself.

The pipeline the contract fixes::

    audio -> ASR -> word timestamps -> diarization -> segments -> word<->speaker

with four hard rules that each function below enforces:

1. **Words exist only when the provider produced them.** ``align_words`` stores
   ``media_intel_words`` rows only for genuinely aligned words. A segment-level
   backend yields ``words_available=False`` plus a machine reason and ZERO rows
   -- words are never interpolated from segment boundaries.
2. **No speaker is ever fabricated.** Diarization unavailable => every word keeps
   ``speaker_id = NULL`` and the payload reports ``speakers_resolved=false`` with
   the reason. Speech-activity segments (``kind=SPEECH_ACTIVITY``) are stored with
   ``speaker_id = NULL`` always and are NEVER used to fill a word's speaker.
3. **No sensitive inference.** Ids are anonymous ``SPEAKER_00``/``SPEAKER_01``
   assigned by first appearance within a run and stable for that run. No gender,
   race, age, identity or any other attribute is derived, stored, hinted at or
   exposed. The ONLY naming source is an operator alias
   (:func:`create_alias`), stored apart from the segments.
4. **Words in a gap stay NULL.** :func:`map_words_to_speakers` assigns by MAXIMAL
   temporal overlap; a word that overlaps no ``SPEAKER`` segment is left NULL
   rather than nearest-guessed across the gap.

Emission rule (contracts §3): ``record_event``/``track_cost`` each open their own
session, so nothing here emits. Every payload carries an ``events`` list that the
ROUTE publishes after ``db.commit()`` -- exactly like
``engine/collab/reviews.py`` does it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.intel import registry as intel_registry
from app.engine.intel.base import (
    IntelRequest,
    MediaIntelProvider,
    ProviderCancelled,
    ProviderHealth,
    ProviderTimeout,
    ProviderUnavailable,
    safe_health,
)
from app.engine.intel.impl.ffmpeg_speech_activity import (
    SEGMENTS_KIND as ACTIVITY_KIND,
)
from app.engine.intel.impl.pyannote_diarization import (
    SEGMENTS_KIND as SPEAKER_SEGMENT_KIND,
)
from app.models import MediaAsset, MediaIntelWord, Workspace
from app.models.media_intel import DiarizationSegment, SpeakerAlias
from app.services import media_intel_runs as runs_service

logger = logging.getLogger("ymoney.intel")

#: capability kind for the alignment chain
ALIGNMENT_KIND = "alignment"
#: capability kind for the diarization chain
DIARIZATION_KIND = "diarization"
#: capability kind for the local VAD chain
SPEECH_ACTIVITY_KIND = "speech_activity"

#: ``diarization_segments.kind`` vocabulary (models/media_intel.py)
SPEAKER_KIND = SPEAKER_SEGMENT_KIND
ACTIVITY_KIND = ACTIVITY_KIND
SEGMENT_KINDS: tuple[str, ...] = (SPEAKER_KIND, ACTIVITY_KIND)

#: the anonymous id shape -- the ONLY identifier this module mints
SPEAKER_ID_RE = re.compile(r"^SPEAKER_(\d{2,})$")
SPEAKER_PREFIX = "SPEAKER_"
MAX_SPEAKER_INDEX = 999

#: reasons surfaced when the capability is dark
NO_ALIGNMENT_REASON = "no word-alignment provider is installed"
NO_DIARIZATION_REASON = "no diarization provider is installed"
NO_MEDIA_PATH_REASON = (
    "the asset has no readable file in this workspace's storage; the intelligence "
    "run needs the real media"
)

#: the event kinds this engine surfaces (whitelist in services/webhooks.py)
EVENT_WORDS_ALIGNED = "MEDIA_INTEL_WORDS_ALIGNED"
EVENT_SPEAKERS_RESOLVED = "MEDIA_INTEL_SPEAKERS_RESOLVED"
EVENT_SPEAKER_ALIAS_SET = "MEDIA_INTEL_SPEAKER_ALIAS_SET"
EVENT_SPEAKER_ALIAS_REMOVED = "MEDIA_INTEL_SPEAKER_ALIAS_REMOVED"

#: words shorter than this are dropped from a dubbing cue
DEFAULT_MIN_CUE_WORDS = 1
#: a run with more words than this is reported truncated (rows are kept)
DEFAULT_MAX_WORDS = 200_000


class SpeechAlignmentError(ValueError):
    """A deliberate domain error (maps to 422 at the route edge)."""


# ---------------------------------------------------------------------------
# anonymous speaker identity (contracts §5)
# ---------------------------------------------------------------------------


def anonymous_speaker_id(index: int) -> str:
    """``SPEAKER_00``/``SPEAKER_01``/... for a 0-based first-appearance index."""
    if index < 0 or index > MAX_SPEAKER_INDEX:
        raise SpeechAlignmentError(
            f"speaker index {index} is outside the anonymous id range "
            f"0..{MAX_SPEAKER_INDEX}"
        )
    return f"{SPEAKER_PREFIX}{index:02d}"


def is_anonymous_speaker_id(value: Any) -> bool:
    """True only for the minted anonymous shape -- never a name, never a guess."""
    return bool(SPEAKER_ID_RE.match(str(value or "").strip()))


def assign_speaker_ids(
    turns: Sequence[Mapping[str, Any]],
    *,
    start_index: int = 0,
    mapping: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Map provider-raw labels to anonymous ids by FIRST APPEARANCE.

    ``turns`` is the provider's ``[{speaker, start_s, end_s}, ...]`` list. Returns
    ``(rows, raw_to_anonymous)`` where every row carries ``speaker_id`` and
    ``raw_label``. The raw label is kept in the returned mapping only (the
    caller decides whether to persist it -- the DTO never exposes it) so a
    stable run can be audited without ever storing a name.

    Ordering is the provider's own order, which the adapters already sort by
    ``start_s``; the first time a label appears decides its ``SPEAKER_nn``.
    ``mapping``/``start_index`` seed a run that already minted ids, so a resume
    keeps them (see :func:`merge_speaker_ids`).
    """
    known = {str(k): str(v) for k, v in (mapping or {}).items()}
    next_index = int(start_index)
    rows: list[dict[str, Any]] = []
    for turn in turns or []:
        raw = str(turn.get("speaker") or "").strip()
        if not raw:
            continue
        if raw not in known:
            known[raw] = anonymous_speaker_id(next_index)
            next_index += 1
        rows.append({
            "speaker_id": known[raw],
            "raw_label": raw,
            "start_s": float(turn.get("start_s") or 0.0),
            "end_s": float(turn.get("end_s") or 0.0),
            "confidence": turn.get("confidence"),
        })
    return rows, known


def merge_speaker_ids(
    previous: Mapping[str, str],
    turns: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Like :func:`assign_speaker_ids` but reuses ids already minted in a run.

    A resumed/retried run must keep the SAME speaker ids for the same raw label,
    so the caller hands back the mapping it already stored. New labels continue
    from the highest id already in use, in first-appearance order.
    """
    known = {str(k): str(v) for k, v in (previous or {}).items()}
    used = [
        int(value.rsplit("_", 1)[-1]) for value in known.values()
        if is_anonymous_speaker_id(value)
    ]
    return assign_speaker_ids(
        turns, start_index=(max(used) + 1) if used else 0, mapping=known
    )


def stored_speaker_map(db: Session, run_id: str) -> dict[str, str]:
    """``{anonymous_id: raw_label}`` for a run -- reconstructs the raw mapping.

    The raw provider label is written to ``diarization_segments.speaker_id`` of a
    row whose ``kind`` is ``SPEAKER``; :func:`store_speaker_turns` keeps the two
    id spaces in one string (``"SPEAKER_00\x1fraw"``) so a resume can rebuild the
    mapping from the DB alone. Rows whose value has no separator are already
    anonymous and map to themselves.
    """
    rows = db.scalars(
        select(DiarizationSegment).where(
            DiarizationSegment.run_id == str(run_id),
            DiarizationSegment.kind == SPEAKER_KIND,
        )
    ).all()
    out: dict[str, str] = {}
    for row in rows:
        anonymous, _, raw = str(row.speaker_id or "").partition(SEP)
        if is_anonymous_speaker_id(anonymous):
            out[anonymous] = raw
    return out


#: separator between the anonymous id and the raw provider label in one column
SEP = "\x1f"


# ---------------------------------------------------------------------------
# word <-> speaker mapping (contracts §4)
# ---------------------------------------------------------------------------


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def map_words_to_speakers(
    words: Sequence[Any],
    segments: Sequence[Any],
    *,
    min_overlap_s: float = 0.0,
) -> list[dict[str, Any]]:
    """Assign each word to the ``SPEAKER`` segment it overlaps MOST.

    ``words`` are word rows (ORM objects or mappings with ``start_s``/``end_s``);
    ``segments`` are diarization rows. Only ``kind == "SPEAKER"`` rows are
    considered -- a ``SPEECH_ACTIVITY`` row is a VAD measurement and can never
    become a word's speaker.

    A word overlapping several segments takes the one with the largest overlap
    (ties break to the earlier segment). A word whose best overlap is below
    ``min_overlap_s`` -- or which overlaps nothing at all -- keeps
    ``speaker_id = None`` and reports ``reason``: never nearest-guessed across a
    gap. Returns NEW dicts (the inputs are never mutated) with the input fields
    plus ``speaker_id``, ``speaker_overlap_s`` and ``speaker_reason``.
    """
    speaker_spans: list[dict[str, Any]] = []
    for segment in segments or []:
        if _kind_of(segment) != SPEAKER_KIND:
            continue
        anonymous = _speaker_column(segment)
        if not is_anonymous_speaker_id(anonymous):
            continue
        speaker_spans.append({
            "speaker_id": anonymous,
            "start_s": float(_get(segment, "start_s") or 0.0),
            "end_s": float(_get(segment, "end_s") or 0.0),
        })
    speaker_spans.sort(key=lambda row: (row["start_s"], row["end_s"]))

    out: list[dict[str, Any]] = []
    for word in words or []:
        start = float(_get(word, "start_s") or 0.0)
        end = float(_get(word, "end_s") or 0.0)
        best_id: str | None = None
        best_overlap = 0.0
        for span in speaker_spans:
            overlap = _overlap(start, end, span["start_s"], span["end_s"])
            if overlap > best_overlap:
                best_overlap = overlap
                best_id = span["speaker_id"]
        floor = max(0.0, float(min_overlap_s or 0.0))
        assigned = best_id if (best_id is not None and best_overlap >= floor) else None
        row = _as_dict(word)
        row["speaker_id"] = assigned
        row["speaker_overlap_s"] = round(best_overlap, 6) if assigned else 0.0
        row["speaker_reason"] = (
            "max_overlap" if assigned else "no_speaker_segment"
        )
        out.append(row)
    return out


def _get(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _kind_of(segment: Any) -> str:
    return str(_get(segment, "kind") or SPEAKER_KIND)


def _speaker_column(segment: Any) -> str:
    """The stored ``speaker_id`` value with the raw-label suffix stripped."""
    return str(_get(segment, "speaker_id") or "").partition(SEP)[0].strip()


def _as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    return {
        "idx": getattr(item, "idx", 0),
        "word": str(getattr(item, "word", "") or ""),
        "start_s": float(getattr(item, "start_s", 0.0) or 0.0),
        "end_s": float(getattr(item, "end_s", 0.0) or 0.0),
        "speaker_id": getattr(item, "speaker_id", None),
        "confidence": getattr(item, "confidence", None),
    }


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------


def _asset_path(ws_id: str, asset: MediaAsset) -> str:
    """Resolve the asset's real file inside the workspace storage boundary.

    Two key conventions exist in the repo and both must resolve: a
    ``MediaAsset.storage_key`` is workspace-relative (``validate_storage_key`` ->
    ``STORAGE_ROOT/<ws>/<key>``, resolved by the exporter's
    :func:`~app.engine.exporter.verify.resolve_artifact_path`), while some
    render paths store a CWD-relative key (``services.storage.managed_path``).
    The first hit wins; both refuse anything that escapes the workspace root, so
    a hostile stored value can never be read.
    """
    from app.engine.exporter.verify import resolve_artifact_path
    from app.services.storage import managed_path

    for resolver in (resolve_artifact_path, managed_path):
        try:
            resolved = resolver(str(ws_id or ""), str(asset.storage_key or ""))
        except Exception:  # noqa: BLE001 - a broken resolver must not break a run
            logger.debug("storage resolver failed for asset %s", getattr(asset, "id", "?"))
            continue
        if resolved and Path(str(resolved)).is_file():
            return str(resolved)
    return ""


def _dto(row: MediaIntelWord) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "asset_id": row.asset_id,
        "idx": int(row.idx or 0),
        "word": str(row.word or ""),
        "start_s": round(float(row.start_s or 0.0), 6),
        "end_s": round(float(row.end_s or 0.0), 6),
        "speaker_id": _speaker_column(row) or None,
        "confidence": (
            None if row.confidence is None else round(float(row.confidence), 6)
        ),
    }


def _segment_dto(row: DiarizationSegment) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "asset_id": row.asset_id,
        "kind": str(row.kind or SPEAKER_KIND),
        "speaker_id": _speaker_column(row) or None,
        "start_s": round(float(row.start_s or 0.0), 6),
        "end_s": round(float(row.end_s or 0.0), 6),
        "confidence": (
            None if row.confidence is None else round(float(row.confidence), 6)
        ),
    }


def _alias_dto(row: SpeakerAlias) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "asset_id": row.asset_id,
        "run_id": row.run_id,
        "speaker_id": str(row.speaker_id or ""),
        "label": str(row.label or ""),
        "created_by": row.created_by,
    }


def store_words(
    db: Session,
    ws: Workspace,
    run_id: str,
    asset: MediaAsset,
    words: Sequence[Mapping[str, Any]],
) -> int:
    """Append the word rows for one run. Returns how many were written.

    ``idx`` is the row's position in the provider's time order, so the
    ``UNIQUE(run_id, idx)`` constraint of the table holds. A word with no
    speaker stays NULL -- the engine never guesses.
    """
    written = 0
    for position, word in enumerate(words or []):
        token = str(word.get("word") or "").strip()
        if not token:
            continue
        confidence = word.get("confidence")
        db.add(MediaIntelWord(
            run_id=str(run_id),
            workspace_id=str(ws.id),
            asset_id=str(asset.id),
            idx=int(position),
            word=token[:200],
            start_s=float(word.get("start_s") or 0.0),
            end_s=float(word.get("end_s") or 0.0),
            speaker_id=(str(word["speaker_id"]) if word.get("speaker_id") else None),
            confidence=None if confidence is None else float(confidence),
        ))
        written += 1
    db.flush()
    return written


def store_speaker_turns(
    db: Session,
    ws: Workspace,
    run_id: str,
    asset: MediaAsset,
    turns: Sequence[Mapping[str, Any]],
) -> tuple[int, dict[str, str]]:
    """Append ``kind='SPEAKER'`` segments; returns ``(rows, raw->anonymous)``.

    The stored ``speaker_id`` column is ``"<anonymous>\\x1f<raw label>"`` so a
    resume can rebuild the id mapping from the DB alone
    (:func:`stored_speaker_map`). The raw label is a provider key, never a name,
    and the API DTO (:func:`_segment_dto`) only ever emits the anonymous part.
    """
    previous = stored_speaker_map(db, run_id)
    rows, mapping = merge_speaker_ids(previous, turns)
    written = 0
    for row in rows:
        confidence = row.get("confidence")
        db.add(DiarizationSegment(
            run_id=str(run_id),
            workspace_id=str(ws.id),
            asset_id=str(asset.id),
            speaker_id=f"{row['speaker_id']}{SEP}{row['raw_label']}",
            kind=SPEAKER_KIND,
            start_s=float(row["start_s"]),
            end_s=float(row["end_s"]),
            confidence=None if confidence is None else float(confidence),
        ))
        written += 1
    db.flush()
    return written, mapping


def store_activity_segments(
    db: Session,
    ws: Workspace,
    run_id: str,
    asset: MediaAsset,
    segments: Sequence[Mapping[str, Any]],
) -> int:
    """Append ``kind='SPEECH_ACTIVITY'`` rows. ``speaker_id`` is ALWAYS NULL.

    This is the one function that writes the VAD measurement, and it hard-codes
    the NULL: an activity row must never be able to masquerade as a speaker.
    """
    written = 0
    for segment in segments or []:
        start = float(segment.get("start_s") or 0.0)
        end = float(segment.get("end_s") or 0.0)
        if end <= start:
            continue
        confidence = segment.get("confidence")
        db.add(DiarizationSegment(
            run_id=str(run_id),
            workspace_id=str(ws.id),
            asset_id=str(asset.id),
            speaker_id=None,  # activity is NOT a speaker (contracts §4)
            kind=ACTIVITY_KIND,
            start_s=start,
            end_s=end,
            confidence=None if confidence is None else float(confidence),
        ))
        written += 1
    db.flush()
    return written


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_run(db: Session, ws_id: str, run_id: str):
    """Workspace-scoped run fetch -> ``None`` for a foreign/missing id (=> 404)."""
    return runs_service.get_run(db, str(ws_id or ""), str(run_id or ""))


def run_result(
    db: Session,
    ws_id: str,
    run,
    *,
    cache_hit: bool = False,
) -> dict[str, Any]:
    """The read model of a speech run: manifest + words/segments + reasons.

    ``words_available``/``speakers_resolved``/``reason`` are computed from the
    stored rows, never from optimism: a COMPLETED alignment with zero word rows
    reports ``words_available=false`` with the recorded reason.
    """
    dto = runs_service.run_dto(run, cache_hit=cache_hit)
    words = list_words(db, ws_id, run_id=run.id)
    segments = list_segments(db, ws_id, run_id=run.id)
    metrics = dict(run.metrics_json or {})
    speakers = sorted({
        _speaker_column(row) for row in segments
        if row["kind"] == SPEAKER_KIND and _speaker_column(row)
    })
    has_activity = any(row["kind"] == ACTIVITY_KIND for row in segments)
    return {
        "run": dto,
        "run_id": run.id,
        "cache_hit": bool(cache_hit),
        "words_available": bool(words),
        "word_count": len(words),
        "words": words,
        "segments": segments,
        "speakers": speakers,
        "speakers_resolved": bool(speakers),
        "speech_activity": has_activity,
        "reason": str(metrics.get("reason") or ""),
        "model_version": str(run.model_version or ""),
        "provider_key": str(run.provider_key or ""),
    }


def list_words(
    db: Session,
    ws_id: str,
    *,
    run_id: str | None = None,
    asset_id: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Workspace-scoped word rows in media order (``idx``)."""
    query = select(MediaIntelWord).where(MediaIntelWord.workspace_id == str(ws_id or ""))
    if run_id:
        query = query.where(MediaIntelWord.run_id == str(run_id))
    if asset_id:
        query = query.where(MediaIntelWord.asset_id == str(asset_id))
    rows = db.scalars(
        query.order_by(MediaIntelWord.idx.asc()).limit(max(1, min(int(limit), 50_000)))
    ).all()
    return [_dto(row) for row in rows]


def list_segments(
    db: Session,
    ws_id: str,
    *,
    run_id: str | None = None,
    asset_id: str | None = None,
    kind: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Workspace-scoped segments in media order."""
    query = select(DiarizationSegment).where(
        DiarizationSegment.workspace_id == str(ws_id or "")
    )
    if run_id:
        query = query.where(DiarizationSegment.run_id == str(run_id))
    if asset_id:
        query = query.where(DiarizationSegment.asset_id == str(asset_id))
    if kind:
        if str(kind) not in SEGMENT_KINDS:
            raise SpeechAlignmentError(
                f"unknown segment kind {kind!r}; expected one of {', '.join(SEGMENT_KINDS)}"
            )
        query = query.where(DiarizationSegment.kind == str(kind))
    rows = db.scalars(
        query.order_by(
            DiarizationSegment.start_s.asc(), DiarizationSegment.id.asc()
        ).limit(max(1, min(int(limit), 50_000)))
    ).all()
    return [_segment_dto(row) for row in rows]


def list_speakers(
    db: Session,
    ws_id: str,
    *,
    asset_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Anonymous speakers with their measured coverage. Never a name, never an attribute.

    Every entry is ``{speaker_id, segments, start_s, end_s, total_seconds,
    word_count, label, named}``. ``label`` is the OPERATOR alias (empty until one
    exists) and ``named`` says whether it came from a human; ``speaker_id`` is
    the anonymous id either way.
    """
    segments = list_segments(db, ws_id, run_id=run_id, asset_id=asset_id,
                             kind=SPEAKER_KIND)
    words = list_words(db, ws_id, run_id=run_id, asset_id=asset_id)
    labels = alias_map(db, ws_id, asset_id=asset_id, run_id=run_id)
    per: dict[str, dict[str, Any]] = {}
    for row in segments:
        speaker_id = str(row["speaker_id"] or "")
        if not speaker_id:
            continue
        entry = per.setdefault(speaker_id, {
            "speaker_id": speaker_id,
            "segments": 0,
            "start_s": None,
            "end_s": None,
            "total_seconds": 0.0,
            "word_count": 0,
        })
        entry["segments"] += 1
        entry["start_s"] = (
            row["start_s"] if entry["start_s"] is None
            else min(entry["start_s"], row["start_s"])
        )
        entry["end_s"] = (
            row["end_s"] if entry["end_s"] is None
            else max(entry["end_s"], row["end_s"])
        )
        entry["total_seconds"] += max(0.0, row["end_s"] - row["start_s"])
    for word in words:
        speaker_id = str(word["speaker_id"] or "")
        if speaker_id in per:
            per[speaker_id]["word_count"] += 1
    out: list[dict[str, Any]] = []
    for speaker_id in sorted(per):
        entry = per[speaker_id]
        label = str(labels.get(speaker_id) or "")
        out.append({
            "speaker_id": speaker_id,
            "segments": int(entry["segments"]),
            "start_s": entry["start_s"],
            "end_s": entry["end_s"],
            "total_seconds": round(float(entry["total_seconds"]), 6),
            "word_count": int(entry["word_count"]),
            "label": label,
            "named": bool(label),
        })
    return out


# ---------------------------------------------------------------------------
# speaker aliases -- the ONLY naming source (contracts §5)
# ---------------------------------------------------------------------------


def create_alias(
    db: Session,
    ws: Workspace,
    *,
    speaker_id: str,
    label: str,
    asset_id: str | None = None,
    run_id: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Store an OPERATOR label for an anonymous speaker id.

    Validation is deliberately strict: the id must have the minted anonymous
    shape (``SPEAKER_nn``) so no free-form name, demographic or identity can be
    smuggled into a segment, and the label must be non-empty and bounded. One
    label per ``(workspace, asset, run, speaker_id)`` scope: a repeat upserts the
    same row, so a rename never leaves a stale duplicate behind.
    """
    anonymous = str(speaker_id or "").strip()
    if not is_anonymous_speaker_id(anonymous):
        raise SpeechAlignmentError(
            "speaker_id must be an anonymous id of the form SPEAKER_00 "
            f"(got {anonymous!r}); nothing in this system infers a name"
        )
    text = " ".join(str(label or "").split())
    if not text:
        raise SpeechAlignmentError("label must not be empty")
    if len(text) > 120:
        raise SpeechAlignmentError("label must be at most 120 characters")
    scope_asset = str(asset_id) if asset_id else None
    scope_run = str(run_id) if run_id else None
    row = db.scalar(
        select(SpeakerAlias).where(
            SpeakerAlias.workspace_id == str(ws.id),
            SpeakerAlias.speaker_id == anonymous,
            SpeakerAlias.asset_id.is_(None) if scope_asset is None
            else SpeakerAlias.asset_id == scope_asset,
            SpeakerAlias.run_id.is_(None) if scope_run is None
            else SpeakerAlias.run_id == scope_run,
        )
    )
    if row is None:
        row = SpeakerAlias(
            workspace_id=str(ws.id),
            asset_id=scope_asset,
            run_id=scope_run,
            speaker_id=anonymous,
        )
        db.add(row)
    row.label = text
    row.created_by = str(created_by) if created_by else None
    db.flush()
    return _alias_dto(row)


def list_aliases(
    db: Session,
    ws_id: str,
    *,
    asset_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Workspace-scoped operator labels (newest scope first: run, asset, global)."""
    rows = db.scalars(
        select(SpeakerAlias)
        .where(SpeakerAlias.workspace_id == str(ws_id or ""))
        .order_by(SpeakerAlias.created_at.desc())
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        if asset_id and row.asset_id and str(row.asset_id) != str(asset_id):
            continue
        if run_id and row.run_id and str(row.run_id) != str(run_id):
            continue
        out.append(_alias_dto(row))
    return out


def alias_map(
    db: Session,
    ws_id: str,
    *,
    asset_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, str]:
    """``{speaker_id: label}`` for the most specific scope that has a label.

    Precedence is run -> asset -> workspace, so a per-video rename never
    overrides an unrelated video's label and a global fallback still applies.
    """
    scoped: dict[str, tuple[int, str]] = {}
    for row in list_aliases(db, ws_id, asset_id=asset_id, run_id=run_id):
        speaker_id = str(row["speaker_id"] or "")
        if not is_anonymous_speaker_id(speaker_id):
            continue
        row_run = 2 if row["run_id"] else (1 if row["asset_id"] else 0)
        if run_id and row["run_id"] and str(row["run_id"]) != str(run_id):
            continue
        current = scoped.get(speaker_id)
        if current is None or row_run >= current[0]:
            scoped[speaker_id] = (row_run, str(row["label"] or ""))
    return {speaker: label for speaker, (_rank, label) in scoped.items() if label}


def delete_alias(db: Session, ws_id: str, alias_id: str) -> bool:
    """Delete one operator label. ``False`` when it is foreign/missing (=> 404)."""
    row = db.get(SpeakerAlias, str(alias_id or ""))
    if row is None or str(row.workspace_id) != str(ws_id or ""):
        return False
    db.delete(row)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# Work 07 dubbing bridge (contracts §5 / §21)
# ---------------------------------------------------------------------------


def to_dubbing_cues(
    words: Iterable[Any],
    segments: Iterable[Any] = (),
    *,
    labels: Mapping[str, str] | None = None,
    max_gap_s: float = 0.6,
    max_words: int = 24,
) -> list[dict[str, Any]]:
    """Group mapped words into Work 07 dubbing cues.

    The output feeds ``app.engine.dubbing.plan.build_plan`` DIRECTLY: every cue
    carries ``index``/``start``/``end``/``text`` plus ``speaker`` (the anonymous
    id, or the operator label when one exists) and ``speaker_id`` (always the
    anonymous id). ``build_plan`` reads ``speaker_id`` first, so the plan keys on
    the ANONYMOUS id while the operator label rides along as a human hint -- a
    label is never turned into an inferred attribute.

    A cue is broken when the speaker changes or the gap to the previous word
    exceeds ``max_gap_s``; words with no speaker are skipped rather than folded
    into a neighbour's cue (they have no speaker to dub against). Cues with
    fewer than one word cannot exist, so the result is always non-empty-or-empty.
    """
    mapped = [row for row in words or [] if str(_get(row, "word") or "").strip()]
    mapped.sort(key=lambda row: (float(_get(row, "start_s") or 0.0),
                                 float(_get(row, "end_s") or 0.0)))
    names = {str(k): str(v) for k, v in (labels or {}).items()}
    cues: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_speaker: str | None = None
    previous_end: float | None = None
    gap_limit = max(0.0, float(max_gap_s or 0.0))
    word_limit = max(1, int(max_words or 1))

    def flush() -> None:
        nonlocal current, current_speaker
        if not current:
            return
        anonymous = current_speaker or ""
        label = names.get(anonymous, "")
        cues.append({
            "index": len(cues) + 1,
            "start": round(float(current[0]["start_s"]), 6),
            "end": round(float(current[-1]["end_s"]), 6),
            "text": " ".join(str(row["word"]) for row in current).strip(),
            "speaker": label or anonymous,
            "speaker_id": anonymous,
            "label": label,
            "words": [
                {
                    "word": str(row["word"]),
                    "start_s": round(float(row["start_s"]), 6),
                    "end_s": round(float(row["end_s"]), 6),
                    "confidence": row.get("confidence"),
                }
                for row in current
            ],
        })
        current = []
        current_speaker = None

    for row in mapped:
        if not isinstance(row, Mapping):
            row = _as_dict(row)
        speaker = str(row.get("speaker_id") or "")
        start = float(row.get("start_s") or 0.0)
        end = float(row.get("end_s") or 0.0)
        breaks = (
            not current
            or speaker != current_speaker
            or (previous_end is not None and start - previous_end > gap_limit)
            or len(current) >= word_limit
        )
        if breaks:
            flush()
            current_speaker = speaker
        current.append({
            "word": str(row.get("word") or ""),
            "start_s": start,
            "end_s": end,
            "confidence": row.get("confidence"),
        })
        previous_end = end
    flush()
    return cues


def dubbing_voice_map(
    cues: Sequence[Mapping[str, Any]],
    *,
    target_voices: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """``voice_map`` for ``build_plan``, offering the operator label as a hint.

    Only ``target_voices`` (an explicit operator/voice-designer choice) sets a
    ``target_voice``; the alias label is recorded as ``label`` and left out of
    the voice field, because a name is a name and never a voice. Speakers without
    a chosen voice stay unmapped, which is what makes ``build_plan`` flag them
    ``voice_review`` instead of inventing a voice.
    """
    chosen = {str(k): str(v) for k, v in (target_voices or {}).items()}
    out: dict[str, dict[str, Any]] = {}
    for cue in cues or []:
        speaker_id = str(cue.get("speaker_id") or "").strip()
        if not speaker_id:
            continue
        entry = out.setdefault(speaker_id, {
            "label": str(cue.get("label") or ""), "source_voice": "", "target_voice": "",
        })
        if chosen.get(speaker_id):
            entry["target_voice"] = chosen[speaker_id]
    return out


# ---------------------------------------------------------------------------
# the two engines
# ---------------------------------------------------------------------------


def _resolve(kind: str, *, commercial_mode: bool = False):
    return intel_registry.resolve(kind, commercial_mode=bool(commercial_mode))


def _fail_unavailable(
    db: Session,
    run,
    provider_key: str,
    reason: str,
) -> dict[str, Any]:
    """Terminal ``UNAVAILABLE`` run + a payload that says WHY, not that it tried."""
    runs_service.unavailable_run(db, run, reason)
    return {
        "run": runs_service.run_dto(run),
        "run_id": run.id,
        "cache_hit": False,
        "status": str(run.status),
        "words_available": False,
        "word_count": 0,
        "words": [],
        "segments": [],
        "speakers": [],
        "speakers_resolved": False,
        "speech_activity": False,
        "reason": str(reason),
        "provider_key": provider_key,
        "model_version": str(run.model_version or ""),
        "events": [],
    }


def _model_version(provider: MediaIntelProvider, kind: str) -> str:
    """Cache-key model string: the provider's own, else its health version."""
    reader = getattr(provider, "model_version", None)
    if callable(reader):
        try:
            return str(reader() or "")
        except Exception:  # noqa: BLE001 - a bad probe must not break the run
            logger.debug("model_version probe failed for %s", kind)
    health: ProviderHealth = safe_health(provider)
    return str(health.version or "")


def _payload_of(result, artifact: str) -> Mapping[str, Any]:
    """The ``artifacts[kind]['payload']`` mapping of a provider result.

    Defensive on purpose: a half-shaped provider result must degrade into "no
    evidence plus a reason", never into an ``AttributeError`` that would mark a
    healthy run FAILED.
    """
    artifacts = getattr(result, "artifacts", None)
    if not isinstance(artifacts, Mapping):
        return {}
    entry = artifacts.get(artifact)
    if not isinstance(entry, Mapping):
        return {}
    payload = entry.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _result_warnings(result) -> list[str]:
    return [str(note)[:200] for note in (getattr(result, "warnings", None) or [])]


def align_words(
    db: Session,
    ws: Workspace,
    asset: MediaAsset,
    *,
    language: str = "en",
    provider: MediaIntelProvider | None = None,
    provider_key: str = "whisperx_alignment",
    params: Mapping[str, Any] | None = None,
    commercial_mode: bool = False,
    force: bool = False,
    requested_by: str | None = None,
    should_cancel=None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the alignment chain and persist the word rows (contracts §4).

    Returns a payload with ``words_available`` and a ``reason``; the caller
    commits, then publishes ``events``. A cache hit returns the prior run's rows
    and performs NO recompute. Words are written ONLY when the provider genuinely
    aligned them.
    """
    resolved = provider
    if resolved is None:
        resolved, _reasons = _resolve(ALIGNMENT_KIND, commercial_mode=commercial_mode)
    model_version = _model_version(resolved, ALIGNMENT_KIND) if resolved is not None else ""
    created = runs_service.create_run(
        db, ws, kind=ALIGNMENT_KIND, asset=asset, provider_key=provider_key,
        model_version=model_version, params=dict(params or {}), force=bool(force),
        requested_by=requested_by,
    )
    if created.get("cache_hit"):
        prior = runs_service.get_run(db, ws.id, created["id"])
        if prior is not None:
            return run_result(db, ws.id, prior, cache_hit=True)

    run = runs_service.get_run(db, ws.id, created["id"])
    if run is None:  # pragma: no cover - create_run always returns a row
        raise SpeechAlignmentError("the alignment run could not be created")
    if resolved is None:
        return _fail_unavailable(db, run, provider_key, NO_ALIGNMENT_REASON)

    path = _asset_path(ws.id, asset)
    if not path:
        return _fail_unavailable(db, run, provider_key, NO_MEDIA_PATH_REASON)

    runs_service.start_run(db, run)
    request = IntelRequest(
        workspace_id=str(ws.id), asset_id=str(asset.id), storage_path=path,
        params={**dict(params or {}), "language": str(language or "en"),
                "commercial_mode": bool(commercial_mode)},
        asset_checksum=str(asset.checksum or ""), run_id=str(run.id),
    )
    started = time.perf_counter()
    elapsed_ms = 0
    try:
        result = resolved.run(
            request,
            progress=lambda pct: _progress(db, run, pct),
            should_cancel=should_cancel or (lambda: False),
            deadline=deadline,
        )
    except ProviderUnavailable as exc:
        return _fail_unavailable(db, run, provider_key, str(exc))
    except ProviderCancelled:
        runs_service.cancel_run(db, run)
    except ProviderTimeout:
        runs_service.fail_run(db, run, "TIMEOUT")
    except Exception as exc:  # noqa: BLE001 - a provider crash is a FAILED run
        logger.warning("alignment provider failed: %s", type(exc).__name__)
        runs_service.fail_run(db, run, "PROVIDER_ERROR")
    else:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _store_alignment_words(db, ws, run, asset, result, processing_ms=elapsed_ms)
    db.commit()
    return run_result(db, ws.id, run)


def _store_alignment_words(
    db: Session,
    ws: Workspace,
    run,
    asset: MediaAsset,
    result,
    *,
    processing_ms: int = 0,
) -> tuple[int, str]:
    """Persist the provider's words and COMPLETE the run. Returns ``(rows, reason)``.

    ``words_available`` in the run metrics is ``True`` only when rows were
    actually written -- an empty alignment is recorded as
    ``words_available=False`` with the provider's reason rather than a silent
    success with zero rows. Completing the run here (and only here) keeps
    exactly one ``MEDIA_INTEL_RUN_COMPLETED`` event per run.
    """
    payload = _payload_of(result, "words")
    raw_words = payload.get("words")
    if not (bool(payload.get("words_available")) and raw_words):
        reason = str(payload.get("reason") or NO_ALIGNMENT_REASON)
        runs_service.complete_run(
            db, run,
            metrics={**dict(payload.get("metrics") or {}),
                     "words_available": False, "word_count": 0, "reason": reason},
            warnings=_result_warnings(result) + ["no word rows written"],
            processing_ms=int(processing_ms),
        )
        return 0, reason
    rows = list(raw_words)
    reason = ""
    if len(rows) > DEFAULT_MAX_WORDS:
        rows = rows[:DEFAULT_MAX_WORDS]
        reason = f"word list truncated at the {DEFAULT_MAX_WORDS} ceiling"
    written = store_words(db, ws, run.id, asset, rows)
    runs_service.complete_run(
        db, run,
        metrics={
            **dict(payload.get("metrics") or {}),
            "words_available": bool(written),
            "word_count": written,
            "reason": reason,
        },
        warnings=_result_warnings(result) + ([reason] if reason else []),
        processing_ms=int(processing_ms),
    )
    return written, reason



def _progress(db: Session, run, pct: float) -> None:
    try:
        run.progress = max(0, min(99, int(float(pct) * 100)))
        db.flush()
    except Exception:  # noqa: BLE001 - progress must never break a run
        logger.debug("progress update failed for run %s", getattr(run, "id", "?"))


def diarize(
    db: Session,
    ws: Workspace,
    asset: MediaAsset,
    *,
    provider: MediaIntelProvider | None = None,
    provider_key: str = "pyannote_diarization",
    model: str = "",
    include_speech_activity: bool = True,
    params: Mapping[str, Any] | None = None,
    commercial_mode: bool = False,
    force: bool = False,
    requested_by: str | None = None,
    should_cancel=None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run the diarization chain and persist the segments (contracts §4/§5).

    With ``include_speech_activity`` the local ffmpeg VAD provider is consulted
    as well, so an install with no diarizer still gets honest SPEECH_ACTIVITY
    rows (speaker_id NULL). The run COMPLETES in that case with
    ``speakers_resolved=false`` and a reason; the activity rows are NOT dressed
    up as speakers. When neither chain can serve, the run is terminal
    ``UNAVAILABLE``.
    """
    resolved = provider
    if resolved is None:
        resolved, _reasons = _resolve(DIARIZATION_KIND, commercial_mode=commercial_mode)
    model_version = _model_version(resolved, DIARIZATION_KIND) if resolved is not None else ""
    created = runs_service.create_run(
        db, ws, kind=DIARIZATION_KIND, asset=asset, provider_key=provider_key,
        model_version=model_version, params=dict(params or {}), force=bool(force),
        requested_by=requested_by,
    )
    if created.get("cache_hit"):
        prior = runs_service.get_run(db, ws.id, created["id"])
        if prior is not None:
            return run_result(db, ws.id, prior, cache_hit=True)

    run = runs_service.get_run(db, ws.id, created["id"])
    if run is None:  # pragma: no cover - create_run always returns a row
        raise SpeechAlignmentError("the diarization run could not be created")

    path = _asset_path(ws.id, asset)
    activity_provider = None
    if include_speech_activity and path:
        activity_provider, _why = _resolve(
            SPEECH_ACTIVITY_KIND, commercial_mode=commercial_mode
        )
    if resolved is None and activity_provider is None:
        reason = NO_DIARIZATION_REASON
        if not path:
            reason = NO_MEDIA_PATH_REASON
        return _fail_unavailable(db, run, provider_key, reason)
    if not path:
        return _fail_unavailable(db, run, provider_key, NO_MEDIA_PATH_REASON)

    runs_service.start_run(db, run)
    started = time.perf_counter()
    cancel = should_cancel or (lambda: False)
    warnings: list[str] = []
    reason = ""
    speaker_reported = False

    if resolved is not None:
        request = IntelRequest(
            workspace_id=str(ws.id), asset_id=str(asset.id), storage_path=path,
            params={**dict(params or {}), "commercial_mode": bool(commercial_mode),
                    **({"model": model} if model else {})},
            asset_checksum=str(asset.checksum or ""), run_id=str(run.id),
        )
        try:
            result = resolved.run(
                request, progress=lambda pct: _progress(db, run, pct),
                should_cancel=cancel, deadline=deadline,
            )
        except ProviderUnavailable as exc:
            reason = str(exc)
            warnings.append("diarization unavailable: " + reason[:160])
        except ProviderCancelled:
            runs_service.cancel_run(db, run)
            db.commit()
            return run_result(db, ws.id, run)
        except ProviderTimeout:
            runs_service.fail_run(db, run, "TIMEOUT")
            db.commit()
            return run_result(db, ws.id, run)
        except Exception as exc:  # noqa: BLE001 - a provider crash is a FAILED run
            logger.warning("diarization provider failed: %s", type(exc).__name__)
            runs_service.fail_run(db, run, "PROVIDER_ERROR")
            db.commit()
            return run_result(db, ws.id, run)
        else:
            # store_speaker_turns is the single place that mints the anonymous
            # ids for a run (it resumes an existing mapping when there is one).
            raw_turns = list(_payload_of(result, "segments").get("segments") or [])
            written, _mapping = store_speaker_turns(db, ws, run.id, asset, raw_turns)
            if written:
                speaker_reported = True
            else:
                reason = reason or "the diarizer produced no speaker segment"
                warnings.append(reason)
            warnings.extend(_result_warnings(result))

    activity_written = 0
    if activity_provider is not None:
        activity_written = _store_activity(db, ws, run, asset, activity_provider, path,
                                           params, commercial_mode, cancel, deadline,
                                           warnings)
    if not speaker_reported and not activity_written:
        return _fail_unavailable(
            db, run, provider_key,
            reason or "diarization produced neither speakers nor speech activity",
        )
    runs_service.complete_run(
        db, run,
        metrics={
            "speakers_resolved": bool(speaker_reported),
            "speaker_count": len(stored_speaker_map(db, run.id)),
            "speech_activity_segments": activity_written,
            "reason": reason or ("speech activity only" if not speaker_reported else ""),
        },
        warnings=warnings,
        processing_ms=int((time.perf_counter() - started) * 1000),
    )
    db.commit()
    return run_result(db, ws.id, run)


def _store_activity(

    db: Session,
    ws: Workspace,
    run,
    asset: MediaAsset,
    provider: MediaIntelProvider,
    path: str,
    params: Mapping[str, Any] | None,
    commercial_mode: bool,
    should_cancel,
    deadline: float | None,
    warnings: list[str],
) -> int:
    """Run the VAD provider into ``SPEECH_ACTIVITY`` rows; 0 when unavailable."""
    request = IntelRequest(
        workspace_id=str(ws.id), asset_id=str(asset.id), storage_path=path,
        params={**dict(params or {}), "commercial_mode": bool(commercial_mode)},
        asset_checksum=str(asset.checksum or ""), run_id=str(run.id),
    )
    try:
        result = provider.run(
            request, progress=lambda _pct: None, should_cancel=should_cancel,
            deadline=deadline,
        )
    except ProviderUnavailable as exc:
        warnings.append("speech activity unavailable: " + str(exc)[:160])
        return 0
    except (ProviderCancelled, ProviderTimeout):
        raise
    except Exception as exc:  # noqa: BLE001 - activity is optional evidence
        logger.warning("speech activity failed: %s", type(exc).__name__)
        warnings.append("speech activity failed; no activity rows written")
        return 0
    payload = _payload_of(result, "segments")
    segments = list(payload.get("segments") or [])
    if not segments:
        warnings.append("speech activity produced no segment")
        return 0
    written = store_activity_segments(db, ws, run.id, asset, segments)
    warnings.extend(_result_warnings(result))
    return written


__all__ = [
    "ACTIVITY_KIND",
    "ALIGNMENT_KIND",
    "DEFAULT_MAX_WORDS",
    "DIARIZATION_KIND",
    "EVENT_SPEAKERS_RESOLVED",
    "EVENT_SPEAKER_ALIAS_REMOVED",
    "EVENT_SPEAKER_ALIAS_SET",
    "EVENT_WORDS_ALIGNED",
    "NO_ALIGNMENT_REASON",
    "NO_DIARIZATION_REASON",
    "NO_MEDIA_PATH_REASON",
    "SPEAKER_ID_RE",
    "SPEAKER_KIND",
    "SPEECH_ACTIVITY_KIND",
    "SpeechAlignmentError",
    "align_words",
    "alias_map",
    "anonymous_speaker_id",
    "assign_speaker_ids",
    "create_alias",
    "delete_alias",
    "diarize",
    "dubbing_voice_map",
    "get_run",
    "is_anonymous_speaker_id",
    "list_aliases",
    "list_segments",
    "list_speakers",
    "list_words",
    "map_words_to_speakers",
    "merge_speaker_ids",
    "run_result",
    "store_activity_segments",
    "store_speaker_turns",
    "store_words",
    "stored_speaker_map",
    "to_dubbing_cues",
]
