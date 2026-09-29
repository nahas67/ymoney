"""Route-facing lip-sync service: submit / query / cancel / health.

Wires the provider factory, the local worker queue and the durable job rows
together. Submit fails closed: a provider that is not available raises
`SubmitRejected` carrying remediation, and no job row is created.
"""

from __future__ import annotations

import threading

from app.engine.lipsync import rows as job_rows
from app.engine.lipsync.base import (
    ACTIVE_STATUSES,
    LipSyncProvider,
)
from app.engine.lipsync.factory import build_provider
from app.engine.lipsync.worker import LocalWorkerQueue
from app.models.lipsync import LipSyncJob

_queue: LocalWorkerQueue | None = None
_lock = threading.Lock()


class JobNotFound(Exception):
    """Job id is unknown or belongs to another workspace."""


class JobStateError(Exception):
    """Operation is illegal for the job's current status."""


class SubmitRejected(Exception):
    """Provider unavailable — fail closed with remediation."""

    def __init__(self, message: str, remediation: str = "", http_status: int = 503):
        super().__init__(message)
        self.remediation = remediation
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Queue / provider wiring
# ---------------------------------------------------------------------------


def get_queue() -> LocalWorkerQueue:
    global _queue
    with _lock:
        if _queue is None:
            _queue = LocalWorkerQueue()
        return _queue


def set_provider(provider: LipSyncProvider, **queue_kwargs) -> LocalWorkerQueue:
    """Install a provider (operations/tests) and return the new queue.

    Existing queues keep draining their own threads; only new jobs use the
    returned queue.
    """
    global _queue
    with _lock:
        _queue = LocalWorkerQueue(provider=provider, **queue_kwargs)
        return _queue


def reset_queue(timeout: float = 2.0) -> None:
    """Drop the cached queue (next get_queue() re-resolves from env)."""
    global _queue
    with _lock:
        old, _queue = _queue, None
    if old is not None:
        old.shutdown(timeout=timeout)


def health(provider: LipSyncProvider | None = None) -> dict:
    target = provider if provider is not None else get_queue()
    if isinstance(target, LocalWorkerQueue):
        info = target.health().to_dict()
        info["queue"] = target.stats()
        return info
    return target.health().to_dict()


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


def submit_job(
    db,
    workspace_id: str,
    *,
    video_ref: str,
    audio_ref: str,
    opts: dict | None = None,
    provider_name: str = "",
) -> dict:
    """Validate, persist a QUEUED row and hand the job to the worker queue."""
    queue = get_queue()
    name = (provider_name or "").strip().lower()
    if name and name != getattr(queue.provider, "name", ""):
        candidate = build_provider(name)
        info = candidate.health()
        if not info.available:
            raise SubmitRejected(
                f"lip-sync provider '{name}' unavailable: {info.detail}",
                info.remediation,
            )
        queue = set_provider(candidate)
    info = queue.health()
    if not info.available:
        raise SubmitRejected(
            f"lip-sync unavailable: {info.detail}", info.remediation
        )

    row = job_rows.create_job_row(
        db,
        workspace_id=workspace_id,
        provider=getattr(queue.provider, "name", name or "unknown"),
        video_ref=video_ref,
        audio_ref=audio_ref,
        opts=opts,
    )
    job_id = row.id
    db.commit()
    queue.submit(job_id, video_ref, audio_ref, workspace_id, opts or {})
    db.refresh(row)
    return job_rows.job_dto(row)


def get_job(db, workspace_id: str, job_id: str) -> LipSyncJob:
    row = job_rows.get_job_row(db, workspace_id, job_id)
    if row is None:
        raise JobNotFound(f"lip-sync job {job_id} not found")
    return row


def list_jobs(db, workspace_id: str, limit: int = 100) -> list[dict]:
    return [job_rows.job_dto(r) for r in job_rows.list_job_rows(db, workspace_id, limit)]


def cancel_job(db, workspace_id: str, job_id: str) -> dict:
    row = get_job(db, workspace_id, job_id)
    if row.status not in ACTIVE_STATUSES:
        raise JobStateError(f"job already finished with status {row.status}")
    job_rows.request_cancel(db, workspace_id, job_id)
    get_queue().cancel(job_id)
    db.commit()
    db.refresh(row)
    return job_rows.job_dto(row)


def wait_for(job_id: str, timeout: float = 5.0) -> bool:
    """Join a worker thread (tests/admin)."""
    return get_queue().wait(job_id, timeout)


__all__ = [
    "JobNotFound",
    "JobStateError",
    "SubmitRejected",
    "cancel_job",
    "get_job",
    "get_queue",
    "health",
    "list_jobs",
    "reset_queue",
    "set_provider",
    "submit_job",
    "wait_for",
]
