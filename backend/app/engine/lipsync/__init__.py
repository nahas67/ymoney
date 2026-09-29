"""Replaceable lip-sync layer (Work 07 Lane B).

Contract (`LipSyncProvider`) + adapters (`MuseTalkAdapter` subprocess/GPU,
`ExternalAdapter` HTTP, `UnavailableAdapter` fail-closed fallback), the
isolated `LocalWorkerQueue` (timeout, cancellation, bounded retries, VRAM
concurrency semaphore) and the route-facing service.

MuseTalk is strictly optional: importing this package — and therefore
`app` — works with no GPU, no model weights and no MuseTalk checkout.
"""

from app.engine.lipsync.base import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_STATUSES,
    JOB_SUCCEEDED,
    JOB_TIMEOUT,
    TERMINAL_STATUSES,
    Health,
    LipSyncError,
    LipSyncProvider,
    LipSyncTransient,
    LipSyncUnavailable,
    default_concurrency,
    gpu_present,
)
from app.engine.lipsync.external import ExternalAdapter
from app.engine.lipsync.factory import build_provider, get_lipsync_provider
from app.engine.lipsync.musetalk import MuseTalkAdapter
from app.engine.lipsync.service import (
    JobNotFound,
    JobStateError,
    SubmitRejected,
    cancel_job,
    get_job,
    get_queue,
    health,
    list_jobs,
    reset_queue,
    set_provider,
    submit_job,
)
from app.engine.lipsync.unavailable import UnavailableAdapter
from app.engine.lipsync.worker import LocalWorkerQueue

__all__ = [
    "JOB_CANCELLED",
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "JOB_STATUSES",
    "JOB_SUCCEEDED",
    "JOB_TIMEOUT",
    "TERMINAL_STATUSES",
    "ExternalAdapter",
    "Health",
    "JobNotFound",
    "JobStateError",
    "LipSyncError",
    "LipSyncProvider",
    "LipSyncTransient",
    "LipSyncUnavailable",
    "LocalWorkerQueue",
    "MuseTalkAdapter",
    "SubmitRejected",
    "UnavailableAdapter",
    "build_provider",
    "cancel_job",
    "default_concurrency",
    "get_job",
    "get_lipsync_provider",
    "get_queue",
    "gpu_present",
    "health",
    "list_jobs",
    "reset_queue",
    "set_provider",
    "submit_job",
]
