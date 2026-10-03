"""Run lifecycle, cache, chunks and GPU admission for media intelligence.

Contracts §3. Everything that needs to know "did we already do this work?", "can
I resume it?" and "may I touch the GPU right now?" goes through this module, so
those three rules exist exactly once:

* **Cache** -- ``cache_key = sha256(asset_checksum | provider_key |
  model_version | canonical_params_json)`` scoped to the workspace. A hit
  returns the prior run with ``cache_hit=True`` and performs NO recompute; it
  also records NO cost and NO event (a cached answer is not new work).
  ``force=True`` bypasses the lookup and repoints the cache row at the new run.
* **Resumability** -- chunks are planned from the media duration, each chunk
  records its own ``input_checksum``, and a resume re-runs only non-COMPLETED
  chunks whose checksum still matches. A changed chunk input invalidates THAT
  chunk, never the whole run.
* **GPU admission** -- :func:`gpu_semaphore` (async) and
  :func:`gpu_semaphore_sync` are DB-backed slot counters limited by
  ``settings.max_concurrent_gpu_jobs`` (default 1), PER WORKSPACE, with a
  timeout. CPU-only providers simply never enter the semaphore.

Run state machine (never inferred, always written)::

    PENDING -> RUNNING -> COMPLETED
                        -> FAILED
                        -> CANCELLED
             -> UNAVAILABLE            (terminal, "no provider could serve")

``UNAVAILABLE`` is a first-class terminal state and is retryable, because a
missing backend usually appears after an operator installs it.

The GPU slot ledger reuses the existing ``jobs`` table: acquiring a slot
INSERTS a ``MEDIA_INTEL_GPU_SLOT`` row whose UNIQUE ``idempotency_key`` is
``<type>:<workspace>:<index>`` for the first free index in ``0..limit-1``, and
releasing it DELETEs the row. The unique key is what makes admission an atomic
compare-and-set across threads AND processes (a lost race is an IntegrityError,
never a check-then-insert window), the ``WAITING`` status keeps a live slot out
of the worker claim query and out of ``services.jobs.recover_orphans``, and the
DELETE leaves no residue in the job list. A slot older than ``stale_seconds``
is reclaimed, so a crashed holder fails OPEN instead of leaking a slot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import session_scope
from app.models import Job, MediaAsset, MediaIntelCache, MediaIntelChunk, MediaIntelRun
from app.models.base import JobStatus, utcnow
from app.models.media_intel import (
    CHUNK_STATUSES,
    RETRYABLE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
)

logger = logging.getLogger("ymoney.intel")

#: default chunk length in seconds (contracts §3, configurable)
DEFAULT_CHUNK_SECONDS = 600
#: default GPU admission limit when neither caller nor settings say otherwise
DEFAULT_GPU_LIMIT = 1
#: a slot held longer than this is considered abandoned and reclaimed
DEFAULT_SLOT_STALE_SECONDS = 3600.0
#: default admission timeout
DEFAULT_SLOT_TIMEOUT = 900.0
#: ``jobs.type`` used for the GPU slot ledger rows
SLOT_JOB_TYPE = "MEDIA_INTEL_GPU_SLOT"
#: the 7 activity-feed kinds this service emits (whitelist them in
#: ``services/webhooks.py::WEBHOOK_EVENTS`` at integration time)
EVENT_RUN_CREATED = "MEDIA_INTEL_RUN_CREATED"
EVENT_RUN_STARTED = "MEDIA_INTEL_RUN_STARTED"
EVENT_RUN_COMPLETED = "MEDIA_INTEL_RUN_COMPLETED"
EVENT_RUN_FAILED = "MEDIA_INTEL_RUN_FAILED"
EVENT_RUN_CANCELLED = "MEDIA_INTEL_RUN_CANCELLED"
EVENT_RUN_UNAVAILABLE = "MEDIA_INTEL_RUN_UNAVAILABLE"
EVENT_RUN_RETRIED = "MEDIA_INTEL_RUN_RETRIED"

#: separator between the four cache-key parts (never appears in a digest)
_CACHE_SEPARATOR = "|"


class GpuSlotTimeout(RuntimeError):
    """No GPU slot became free within the timeout -- never swallowed."""


class RunNotRetryable(ValueError):
    """``retry_run`` called on a run that is not in a retryable state."""


# ---------------------------------------------------------------------------
# cache key
# ---------------------------------------------------------------------------


def canonical_params(params: dict | None) -> str:
    """Sorted-key JSON of ``params`` -- the stable cache-key input.

    Key order must never change the digest, so nested dicts are sorted too and
    anything that is not JSON-serialisable is coerced to its ``str`` form
    rather than raising.
    """
    return json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)


def params_hash(params: dict | None) -> str:
    """sha256 of :func:`canonical_params`."""
    return hashlib.sha256(canonical_params(params).encode("utf-8")).hexdigest()


def cache_key(
    asset_checksum: str,
    provider_key: str,
    model_version: str,
    params: dict | None = None,
) -> str:
    """sha256 of the four cache-key parts joined with ``|`` (contracts §3).

    A change to ANY part -- a re-encoded asset, a different provider, a new
    model version, a tweaked parameter -- produces a different key, so a stale
    result can never be served for changed work.
    """
    parts = (
        str(asset_checksum or ""),
        str(provider_key or ""),
        str(model_version or ""),
        canonical_params(params),
    )
    return hashlib.sha256(_CACHE_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return (value.isoformat() + "Z") if value else None


def run_dto(row: MediaIntelRun, *, cache_hit: bool = False) -> dict:
    """The run shape served by the API and stored on the job result."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "asset_id": row.asset_id,
        "kind": str(row.kind or ""),
        "provider_key": str(row.provider_key or ""),
        "model_version": str(row.model_version or ""),
        "params": dict(row.params_json or {}),
        "params_hash": str(row.params_hash or ""),
        "asset_checksum": str(row.asset_checksum or ""),
        "status": str(row.status or "PENDING"),
        "progress": int(row.progress or 0),
        "chunks_total": int(row.chunks_total or 0),
        "chunks_done": int(row.chunks_done or 0),
        "cancel_requested": bool(row.cancel_requested),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "processing_ms": int(row.processing_ms or 0),
        "gpu_ms": int(row.gpu_ms or 0),
        "cost_micros": int(row.cost_micros or 0),
        "warnings": list(row.warnings_json or []),
        "metrics": dict(row.metrics_json or {}),
        "error_code": str(row.error_code or ""),
        "output_asset_id": row.output_asset_id,
        "requested_by": row.requested_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "terminal": str(row.status or "") in TERMINAL_RUN_STATUSES,
        "retryable": str(row.status or "") in RETRYABLE_RUN_STATUSES,
        "cache_hit": bool(cache_hit),
    }


def _publish(
    db: Session,
    workspace_id: str,
    event: str,
    message: str,
    *,
    level: str = "info",
    cost: MediaIntelRun | None = None,
    **data: Any,
) -> None:
    """Commit the run's durable state, THEN publish cost + the activity event.

    The ordering is load-bearing. ``record_event``/``track_cost`` open their own
    session, so publishing while this session still holds an open write
    transaction makes a SECOND writer wait for our lock -- on SQLite that is a
    busy-timeout stall and then a dropped event. Committing first means an event
    never describes uncommitted work and telemetry can never block a run.

    Everything here is best-effort: a telemetry failure is logged, never raised
    (contracts: a run's outcome is decided by the state machine, not by the
    activity feed). Callers may still call ``db.commit()`` afterwards; it is a
    no-op.
    """
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 - never lose the telemetry step
        logger.warning("media-intel commit before %s failed: %s", event, type(exc).__name__)
    if cost is not None:
        _track_cost(workspace_id, cost)
    try:
        from app.services.events import record_event

        record_event(workspace_id, event, message, level=level, source="media_intel", data=data)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
        logger.warning("media-intel event %s failed: %s", event, type(exc).__name__)


def _track_cost(workspace_id: str, run: MediaIntelRun) -> None:
    """Cost entry for a run that ACTUALLY did work (never for a cache hit)."""
    micros = int(run.cost_micros or 0)
    if micros <= 0:
        return
    try:
        from app.services.cost import track_cost

        track_cost(
            workspace_id,
            "media_intel",
            micros / 1_000_000.0,
            provider=str(run.provider_key or ""),
            detail={
                "run_id": run.id,
                "kind": str(run.kind or ""),
                "processing_ms": int(run.processing_ms or 0),
                "gpu_ms": int(run.gpu_ms or 0),
            },
            is_estimate=False,
        )
    except Exception as exc:  # noqa: BLE001 - accounting must not fail a run
        logger.warning("media-intel cost tracking failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# run lifecycle
# ---------------------------------------------------------------------------


def get_run(db: Session, workspace_id: str, run_id: str) -> MediaIntelRun | None:
    """Workspace-scoped run fetch. A foreign/missing id is ``None`` (=> 404)."""
    if not run_id:
        return None
    row = db.get(MediaIntelRun, str(run_id))
    if row is None or row.workspace_id != str(workspace_id):
        return None
    return row


def create_run(
    db: Session,
    ws: Any,
    *,
    kind: str,
    asset: MediaAsset,
    provider_key: str,
    model_version: str = "",
    params: dict | None = None,
    force: bool = False,
    requested_by: str | None = None,
) -> dict:
    """Create a PENDING run, or return the cached prior one (contracts §3).

    A cache hit requires the recorded run to be ``COMPLETED`` -- a FAILED or
    CANCELLED prior attempt is not a result and must be recomputed. On a hit no
    run row is inserted, no work is queued, no cost is tracked and no event is
    emitted; the returned DTO simply carries ``cache_hit=True``.

    ``force=True`` always creates a new run and repoints the cache row at it.
    """
    workspace_id = str(getattr(ws, "id", ws) or "")
    payload_params = dict(params or {})
    digest = params_hash(payload_params)
    key = cache_key(
        str(getattr(asset, "checksum", "") or ""), provider_key, model_version, payload_params
    )

    if not force:
        cached = db.scalar(
            select(MediaIntelCache).where(
                MediaIntelCache.workspace_id == workspace_id,
                MediaIntelCache.cache_key == key,
            )
        )
        if cached is not None:
            prior = db.get(MediaIntelRun, cached.run_id)
            if prior is not None and prior.status == "COMPLETED":
                return run_dto(prior, cache_hit=True)

    run = MediaIntelRun(
        workspace_id=workspace_id,
        asset_id=asset.id,
        kind=str(kind or ""),
        provider_key=str(provider_key or ""),
        model_version=str(model_version or ""),
        params_json=payload_params,
        params_hash=digest,
        asset_checksum=str(getattr(asset, "checksum", "") or ""),
        status="PENDING",
        requested_by=requested_by,
    )
    db.add(run)
    db.flush()

    # point (or repoint) the workspace cache entry at this run
    entry = db.scalar(
        select(MediaIntelCache).where(
            MediaIntelCache.workspace_id == workspace_id,
            MediaIntelCache.cache_key == key,
        )
    )
    if entry is None:
        db.add(
            MediaIntelCache(
                workspace_id=workspace_id,
                cache_key=key,
                run_id=run.id,
                asset_checksum=run.asset_checksum,
                provider_key=run.provider_key,
                model_version=run.model_version,
                params_hash=digest,
            )
        )
    else:
        entry.run_id = run.id
        entry.asset_checksum = run.asset_checksum
        entry.provider_key = run.provider_key
        entry.model_version = run.model_version
        entry.params_hash = digest
    db.flush()
    _publish(db, workspace_id, EVENT_RUN_CREATED, f"media-intel {run.kind} run queued",
             run_id=run.id, kind=run.kind, asset_id=run.asset_id, provider=run.provider_key,
             force=bool(force))
    return run_dto(run)


def start_run(db: Session, run: MediaIntelRun) -> MediaIntelRun:
    """PENDING -> RUNNING. Idempotent; never resurrects a terminal run."""
    if run.status in TERMINAL_RUN_STATUSES:
        raise ValueError(f"run {run.id} is terminal ({run.status}) and cannot start")
    if not run.started_at:
        run.started_at = utcnow()
    run.status = "RUNNING"
    run.error_code = ""
    db.flush()
    _publish(db, run.workspace_id, EVENT_RUN_STARTED, f"media-intel {run.kind} run started",
             run_id=run.id, kind=run.kind)
    return run


def complete_run(
    db: Session,
    run: MediaIntelRun,
    *,
    metrics: dict | None = None,
    warnings: list[str] | None = None,
    output_asset_id: str | None = None,
    processing_ms: int = 0,
    gpu_ms: int = 0,
    cost_micros: int = 0,
) -> MediaIntelRun:
    """RUNNING -> COMPLETED, writing the manifest + cost/event records.

    ``cost_micros`` > 0 records one cost entry for the work that actually ran
    (a cache hit never reaches here -- it returns before a run exists).
    """
    run.status = "COMPLETED"
    run.finished_at = utcnow()
    run.processing_ms = int(processing_ms or 0)
    run.gpu_ms = int(gpu_ms or 0)
    run.cost_micros = int(cost_micros or 0)
    run.error_code = ""
    run.progress = 100
    if warnings is not None:
        run.warnings_json = [str(w) for w in warnings]
    if metrics is not None:
        run.metrics_json = dict(metrics)
    if output_asset_id is not None:
        run.output_asset_id = str(output_asset_id) or None
    db.flush()
    _publish(db, run.workspace_id, EVENT_RUN_COMPLETED,
             f"media-intel {run.kind} run completed",
             cost=run, run_id=run.id, kind=run.kind, output_asset_id=run.output_asset_id,
             cost_micros=run.cost_micros, warnings=list(run.warnings_json or []))
    return run


def fail_run(db: Session, run: MediaIntelRun, error_code: str = "FAILED") -> MediaIntelRun:
    """RUNNING/PENDING -> FAILED. Partial chunk state is kept for a resume."""
    run.status = "FAILED"
    run.finished_at = utcnow()
    run.error_code = str(error_code or "FAILED")[:60]
    db.flush()
    _publish(db, run.workspace_id, EVENT_RUN_FAILED,
             f"media-intel {run.kind} run failed ({run.error_code})",
             level="error", run_id=run.id, kind=run.kind, error_code=run.error_code)
    return run


def unavailable_run(db: Session, run: MediaIntelRun, reason: str) -> MediaIntelRun:
    """Terminal UNAVAILABLE: no provider could serve the request.

    Distinct from FAILED on purpose -- nothing broke, the capability simply is
    not installed, and the reason is surfaced verbatim to the UI. The reason
    also lands in ``metrics_json`` so it survives the run listing.
    """
    run.status = "UNAVAILABLE"
    run.finished_at = utcnow()
    run.error_code = "PROVIDER_UNAVAILABLE"
    metrics = dict(run.metrics_json or {})
    metrics["reason"] = str(reason or "")[:500]
    run.metrics_json = metrics
    db.flush()
    _publish(db, run.workspace_id, EVENT_RUN_UNAVAILABLE,
             f"media-intel {run.kind} unavailable: {str(reason)[:120]}",
             level="warning", run_id=run.id, kind=run.kind, reason=str(reason)[:200])
    return run


def cancel_run(db: Session, run: MediaIntelRun) -> MediaIntelRun:
    """Request cancellation: flag it and, while non-terminal, finish CANCELLED.

    Chunk rows are left exactly as they are so a later resume continues from
    the last completed chunk (contracts §3).
    """
    run.cancel_requested = True
    if run.status not in TERMINAL_RUN_STATUSES:
        run.status = "CANCELLED"
        run.finished_at = utcnow()
        run.error_code = "CANCELLED"
        db.flush()
        _publish(db, run.workspace_id, EVENT_RUN_CANCELLED,
                 f"media-intel {run.kind} run cancelled",
                 run_id=run.id, kind=run.kind)
    return run


def retry_run(db: Session, run: MediaIntelRun) -> MediaIntelRun:
    """Re-arm a FAILED / CANCELLED / UNAVAILABLE run for another attempt.

    Keeps ``chunks_done`` so the worker can resume where it stopped; the
    attempt count lives on the job, not on the run. Anything else raises
    :class:`RunNotRetryable` (the route maps it to 409).
    """
    if run.status not in RETRYABLE_RUN_STATUSES:
        raise RunNotRetryable(f"run {run.id} is {run.status}; only FAILED/CANCELLED/UNAVAILABLE retry")
    run.status = "PENDING"
    run.finished_at = None
    run.error_code = ""
    run.progress = 0
    run.cancel_requested = False
    db.flush()
    _publish(db, run.workspace_id, EVENT_RUN_RETRIED, f"media-intel {run.kind} run retried",
             run_id=run.id, kind=run.kind, previous_status="RETRY")
    return run


def list_runs(
    db: Session,
    workspace_id: str,
    *,
    kind: str | None = None,
    status: str | None = None,
    asset_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Workspace-scoped run listing, newest first, as DTOs."""
    query = select(MediaIntelRun).where(MediaIntelRun.workspace_id == str(workspace_id))
    if kind:
        query = query.where(MediaIntelRun.kind == str(kind))
    if status:
        query = query.where(MediaIntelRun.status == str(status))
    if asset_id:
        query = query.where(MediaIntelRun.asset_id == str(asset_id))
    rows = db.scalars(
        query.order_by(MediaIntelRun.created_at.desc()).limit(max(1, min(int(limit), 500)))
    ).all()
    return [run_dto(row) for row in rows]


# ---------------------------------------------------------------------------
# chunks / resumability
# ---------------------------------------------------------------------------


def plan_chunks(duration: float, chunk_seconds: int = DEFAULT_CHUNK_SECONDS) -> list[dict]:
    """Split a media duration into ``[{"idx","start_s","end_s"}, ...]``.

    The last chunk is short (media is rarely an exact multiple). A duration of
    0 or less yields ``[]`` -- there is nothing to process, and a lane must not
    invent one chunk for an empty asset.
    """
    try:
        total = float(duration or 0.0)
        step = float(chunk_seconds or 0)
    except (TypeError, ValueError):
        return []
    if total <= 0:
        return []
    if step <= 0:
        raise ValueError("chunk_seconds must be positive")
    chunks: list[dict] = []
    start = 0.0
    idx = 0
    while start < total:
        end = min(total, start + step)
        chunks.append({"idx": idx, "start_s": round(start, 6), "end_s": round(end, 6)})
        start = end
        idx += 1
    return chunks


def plan_run_chunks(
    db: Session,
    run: MediaIntelRun,
    duration: float,
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
) -> list[dict]:
    """Create/refresh the chunk rows for ``run`` and store ``chunks_total``.

    Existing rows are left alone: re-planning after a resume must NOT wipe the
    per-chunk state that makes resuming possible.
    """
    chunks = plan_chunks(duration, chunk_seconds)
    run.chunks_total = len(chunks)
    for chunk in chunks:
        existing = db.scalar(
            select(MediaIntelChunk).where(
                MediaIntelChunk.run_id == run.id, MediaIntelChunk.idx == chunk["idx"]
            )
        )
        if existing is None:
            db.add(MediaIntelChunk(run_id=run.id, idx=chunk["idx"],
                                    start_s=chunk["start_s"], end_s=chunk["end_s"]))
    db.flush()
    _refresh_chunk_progress(db, run)
    return chunks


def mark_chunk(
    db: Session,
    run: MediaIntelRun,
    idx: int,
    status: str,
    input_checksum: str = "",
) -> MediaIntelChunk:
    """Set one chunk's status (+ its current input checksum) and refresh counts.

    ``status`` must be one of the ``CHUNK_STATUSES`` vocabulary; an unknown
    value raises ``ValueError`` rather than being written to the row.
    """
    state = str(status or "").strip().upper()
    if state not in CHUNK_STATUSES:
        raise ValueError(f"unknown chunk status {status!r}")
    chunk = db.scalar(
        select(MediaIntelChunk).where(
            MediaIntelChunk.run_id == run.id, MediaIntelChunk.idx == int(idx)
        )
    )
    if chunk is None:
        chunk = MediaIntelChunk(run_id=run.id, idx=int(idx))
        db.add(chunk)
    chunk.status = state
    if input_checksum:
        chunk.input_checksum = str(input_checksum)[:64]
    db.flush()
    _refresh_chunk_progress(db, run)
    return chunk


def _refresh_chunk_progress(db: Session, run: MediaIntelRun) -> None:
    done = int(
        db.scalar(
            select(func.count())
            .select_from(MediaIntelChunk)
            .where(
                MediaIntelChunk.run_id == run.id,
                MediaIntelChunk.status.in_(("COMPLETED", "SKIPPED")),
            )
        )
        or 0
    )
    run.chunks_done = done
    db.flush()


def pending_chunks(db: Session, run: MediaIntelRun) -> list[MediaIntelChunk]:
    """Chunks still to process (not COMPLETED/SKIPPED), in media order."""
    rows = db.scalars(
        select(MediaIntelChunk)
        .where(
            MediaIntelChunk.run_id == run.id,
            MediaIntelChunk.status.notin_(("COMPLETED", "SKIPPED")),
        )
        .order_by(MediaIntelChunk.idx.asc())
    ).all()
    return list(rows)


def resume_plan(
    db: Session,
    run: MediaIntelRun,
    current_checksums: dict[int, str] | None = None,
) -> list[dict]:
    """Which chunks a resume must (re)run (contracts §3).

    * COMPLETED/SKIPPED chunk whose ``input_checksum`` still matches the
      caller's ``current_checksums`` -> skipped;
    * COMPLETED chunk whose checksum CHANGED -> that ONE chunk is invalidated
      (back to PENDING) and returned for reprocessing;
    * anything not completed -> returned.

    With ``current_checksums=None`` the caller has no new information, so every
    completed chunk is trusted. Returns ``[{"idx","start_s","end_s"}, ...]``.
    """
    rows = db.scalars(
        select(MediaIntelChunk).where(MediaIntelChunk.run_id == run.id)
        .order_by(MediaIntelChunk.idx.asc())
    ).all()
    todo: list[dict] = []
    for chunk in rows:
        done = chunk.status in ("COMPLETED", "SKIPPED")
        if done and current_checksums is not None:
            current = current_checksums.get(chunk.idx)
            if current is not None and str(current) != str(chunk.input_checksum or ""):
                chunk.status = "PENDING"  # only THIS chunk is invalidated
                done = False
        if not done:
            todo.append({
                "idx": int(chunk.idx),
                "start_s": float(chunk.start_s or 0.0),
                "end_s": float(chunk.end_s or 0.0),
            })
    db.flush()
    _refresh_chunk_progress(db, run)
    return todo


# ---------------------------------------------------------------------------
# GPU admission (DB-backed slot counter, per workspace)
# ---------------------------------------------------------------------------


def gpu_limit(limit: int | None = None) -> int:
    """Admission limit: explicit ``limit`` > settings > env > 1.

    ``settings.max_concurrent_gpu_jobs`` is the contract name (contracts §3); it
    is read defensively with ``getattr`` so this lane does not have to own
    ``core/config.py``. ``INTEL_MAX_CONCURRENT_GPU_JOBS`` is the env override.
    """
    if limit is not None:
        try:
            explicit = int(limit)
        except (TypeError, ValueError):
            explicit = 0
        if explicit > 0:
            return explicit
    configured = getattr(settings, "max_concurrent_gpu_jobs", None)
    try:
        value = int(configured) if configured is not None else 0
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    try:
        env_value = int(os.getenv("INTEL_MAX_CONCURRENT_GPU_JOBS", "0"))
    except ValueError:
        env_value = 0
    return env_value if env_value > 0 else DEFAULT_GPU_LIMIT


def held_gpu_slots(workspace_id: str) -> int:
    """How many GPU slots this workspace currently holds (test/debug helper)."""
    with session_scope() as session:
        return _held_slots(session, str(workspace_id or ""))


def _held_slots(session: Session, workspace_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.type == SLOT_JOB_TYPE,
                Job.workspace_id == workspace_id,
            )
        )
        or 0
    )


def _slot_key(workspace_id: str, index: int) -> str:
    """Deterministic per-slot key. UNIQUE on ``jobs.idempotency_key`` is what
    makes acquisition an atomic compare-and-set across threads AND processes."""
    return f"{SLOT_JOB_TYPE}:{workspace_id or 'global'}:{index}"


def _reclaim_stale(session: Session, workspace_id: str, stale_seconds: float) -> int:
    """DELETE abandoned slot rows so a crashed holder cannot leak a slot.

    Release DELETES rather than marks a row complete, so a live slot is
    invisible to the job worker's claim query AND to
    ``services.jobs.recover_orphans`` (which only rewrites RUNNING rows).
    """
    cutoff = utcnow() - timedelta(seconds=float(stale_seconds))
    rows = session.scalars(
        select(Job).where(
            Job.type == SLOT_JOB_TYPE,
            Job.workspace_id == workspace_id,
            Job.started_at < cutoff,
        )
    ).all()
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()
    return len(rows)


def _try_acquire(workspace_id: str, limit: int, kind: str, stale_seconds: float) -> str:
    """One attempt: take the first free slot index. Returns its job id or "".

    Each index is a UNIQUE ``idempotency_key``, so a lost race raises
    IntegrityError instead of over-admitting -- there is no check-then-insert
    window, and the guarantee holds across processes.
    """
    with session_scope() as session:
        _reclaim_stale(session, workspace_id, stale_seconds)
        for index in range(max(1, limit)):
            row = Job(
                type=SLOT_JOB_TYPE,
                workspace_id=workspace_id or None,
                status=JobStatus.WAITING.value,
                payload={"slot": str(kind or "intel"), "pid": os.getpid(), "index": index},
                priority=10,
                max_retries=0,
                started_at=utcnow(),
                idempotency_key=_slot_key(workspace_id, index),
            )
            try:
                with session.begin_nested():  # SAVEPOINT: a lost race is recoverable
                    session.add(row)
                    session.flush()
            except IntegrityError:
                continue  # this index is taken
            return row.id
    return ""


def _release_slot(slot_id: str) -> None:
    """Release a slot. Safe to call twice; never raises."""
    if not slot_id:
        return
    try:
        with session_scope() as session:
            row = session.get(Job, slot_id)
            if row is not None and row.type == SLOT_JOB_TYPE:
                session.delete(row)
    except Exception as exc:  # noqa: BLE001 - release must never propagate
        logger.warning("gpu slot release failed: %s", type(exc).__name__)


def _timeout_error(workspace_id: str, limit: int, timeout: float) -> GpuSlotTimeout:
    return GpuSlotTimeout(
        f"no GPU slot free for workspace {workspace_id or 'global'} after {timeout}s "
        f"(limit {limit}); raise max_concurrent_gpu_jobs or retry later"
    )


@asynccontextmanager
async def gpu_semaphore(
    limit: int | None = None,
    *,
    workspace_id: str = "",
    timeout: float = DEFAULT_SLOT_TIMEOUT,
    poll_seconds: float = 0.05,
    kind: str = "",
    stale_seconds: float = DEFAULT_SLOT_STALE_SECONDS,
):
    """Async admission for GPU work: at most ``limit`` concurrent per workspace.

    The slot counter lives in the database, so the guarantee holds across
    processes and asyncio tasks. The wait is a bounded poll -- with a deadline,
    never an unbounded wait -- and the slot is ALWAYS released in ``finally``,
    including on timeout/cancellation, so a lane cannot deadlock the queue. On
    timeout :class:`GpuSlotTimeout` is raised with an actionable message.

    CPU-only providers must NOT enter this semaphore.
    """
    cap = gpu_limit(limit)
    space = str(workspace_id or "")
    slot_id = ""
    deadline = time.monotonic() + float(timeout)
    try:
        while True:
            slot_id = await asyncio.to_thread(_try_acquire, space, cap, kind, stale_seconds)
            if slot_id:
                break
            if time.monotonic() >= deadline:
                raise _timeout_error(space, cap, timeout)
            await asyncio.sleep(max(0.005, float(poll_seconds)))
        yield slot_id
    finally:
        _release_slot(slot_id)


@contextlib.contextmanager
def gpu_semaphore_sync(
    limit: int | None = None,
    *,
    workspace_id: str = "",
    timeout: float = DEFAULT_SLOT_TIMEOUT,
    poll_seconds: float = 0.05,
    kind: str = "",
    stale_seconds: float = DEFAULT_SLOT_STALE_SECONDS,
) -> Iterator[str]:
    """Blocking twin of :func:`gpu_semaphore` for sync lanes/tests.

    Same DB counter, same bounded poll, same guaranteed release, same
    :class:`GpuSlotTimeout`.
    """
    cap = gpu_limit(limit)
    space = str(workspace_id or "")
    slot_id = ""
    deadline = time.monotonic() + float(timeout)
    try:
        while True:
            slot_id = _try_acquire(space, cap, kind, stale_seconds)
            if slot_id:
                break
            if time.monotonic() >= deadline:
                raise _timeout_error(space, cap, timeout)
            time.sleep(max(0.005, float(poll_seconds)))
        yield slot_id
    finally:
        _release_slot(slot_id)


__all__ = [
    "CHUNK_STATUSES",
    "DEFAULT_CHUNK_SECONDS",
    "DEFAULT_GPU_LIMIT",
    "DEFAULT_SLOT_TIMEOUT",
    "EVENT_RUN_CANCELLED",
    "EVENT_RUN_COMPLETED",
    "EVENT_RUN_CREATED",
    "EVENT_RUN_FAILED",
    "EVENT_RUN_RETRIED",
    "EVENT_RUN_STARTED",
    "EVENT_RUN_UNAVAILABLE",
    "GpuSlotTimeout",
    "RunNotRetryable",
    "SLOT_JOB_TYPE",
    "cache_key",
    "canonical_params",
    "cancel_run",
    "complete_run",
    "create_run",
    "fail_run",
    "get_run",
    "gpu_limit",
    "gpu_semaphore",
    "gpu_semaphore_sync",
    "held_gpu_slots",
    "list_runs",
    "mark_chunk",
    "params_hash",
    "pending_chunks",
    "plan_chunks",
    "plan_run_chunks",
    "resume_plan",
    "retry_run",
    "run_dto",
    "start_run",
    "unavailable_run",
]
