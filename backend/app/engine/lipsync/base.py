"""Lip-sync provider layer (Work 07 Lane B).

Contract + safety rules:

* Heavy inference NEVER runs in-request. Adapters submit work to an
  isolated worker (subprocess / remote HTTP) and are driven by
  `app.engine.lipsync.worker.LocalWorkerQueue` with hard timeout,
  cancellation, bounded retries and a concurrency (VRAM) semaphore.
* Every adapter fails CLOSED: no GPU/model/endpoint means
  `health().available is False` and `submit()` raises `LipSyncUnavailable`
  carrying remediation — never partial or fabricated output.
* Importing this package (and therefore `app`) must work on a machine with
  no GPU, no MuseTalk checkout and no model weights.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any

# Job statuses shared by the DB rows and adapter-level jobs.
#
# This vocabulary is the BUSINESS status: what the render is doing. It is
# deliberately NOT widened to carry a money fact. `FAILED` means "no video was
# produced"; it does not mean "nothing was billed", and an operator who needs to
# tell those apart must not have to parse JSON to do it (Work 15.8 §7). The two
# facts that were being crammed into it live in their own columns, named below.
JOB_QUEUED = "QUEUED"
JOB_RUNNING = "RUNNING"
JOB_SUCCEEDED = "SUCCEEDED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"
JOB_TIMEOUT = "TIMEOUT"

JOB_STATUSES = (
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_FAILED,
    JOB_CANCELLED,
    JOB_TIMEOUT,
)
TERMINAL_STATUSES = frozenset({JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED, JOB_TIMEOUT})
ACTIVE_STATUSES = frozenset({JOB_QUEUED, JOB_RUNNING})

# ---------------------------------------------------------------------------
# Work 15.8 §7: the two facts `status` must never be made to carry
# ---------------------------------------------------------------------------
# The values below are the CANONICAL vocabularies that already exist --
# `app.services.paid_jobs.SubmissionState` and
# `app.services.paid_executor.CostOutcome` -- re-exported under lip-sync names
# rather than re-invented. A job whose submit may already have been billed is
# `EXECUTION_SUBMISSION_UNKNOWN` here and `SUBMISSION_UNKNOWN` in the render
# lane and the cost ledger; one spelling is what makes an incident query work.

#: What HAPPENED to the paid submit, apart from whether it produced a video.
#: ``PREPARED`` is the canonical "we intend to submit; nothing sent" state, and
#: it is also the honest default for a row whose submit has not been reached.
EXECUTION_PREPARED = "PREPARED"
EXECUTION_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
EXECUTION_REJECTED = "FAILED"
EXECUTION_CANCELLED = "CANCELLED"
EXECUTION_CONFIRMED = "REMOTE_ID_CONFIRMED"
EXECUTION_SUCCEEDED = "SUCCEEDED"
EXECUTION_OUTCOMES = (
    EXECUTION_PREPARED,
    EXECUTION_SUBMISSION_UNKNOWN,
    EXECUTION_REJECTED,
    EXECUTION_CANCELLED,
    EXECUTION_CONFIRMED,
    EXECUTION_SUCCEEDED,
)

#: What the LEDGER may say. ``COST_UNKNOWN_EXPOSURE`` is the one that used to
#: be reachable only by parsing `cost_json`.
COST_NOT_APPLICABLE = "NOT_APPLICABLE"
COST_ACTUAL = "ACTUAL"
COST_ESTIMATED = "ESTIMATED"
COST_UNKNOWN_EXPOSURE = "UNKNOWN_EXPOSURE"
COST_OUTCOMES = (
    COST_NOT_APPLICABLE,
    COST_ACTUAL,
    COST_ESTIMATED,
    COST_UNKNOWN_EXPOSURE,
)

#: The business status an ambiguous submission must NOT be reported as. Used by
#: the incident query as a regression guard: "FAILED with an unknown exposure" is
#: the honest rendering, and "SUCCEEDED" with one is not.
AMBIGUOUS_BUSINESS_STATUS = JOB_FAILED

HEALTH_AVAILABLE = "available"
HEALTH_DEGRADED = "degraded"
HEALTH_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Env helpers (config stays local to this package so startup never depends
# on GPU tooling being present)
# ---------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def gpu_present() -> bool:
    """Cheap local GPU probe — no torch import, no subprocess, import-safe."""
    if env_flag("LIPSYNC_GPU"):
        return True
    if env_str("LIPSYNC_GPU") in {"0", "false", "no", "off"}:
        return False
    import shutil

    return shutil.which("nvidia-smi") is not None


def default_concurrency() -> int:
    """VRAM-aware admission limit: 1 without a GPU, configurable either way."""
    override = env_int("LIPSYNC_MAX_CONCURRENCY", 0)
    if override > 0:
        return override
    return 1 if not gpu_present() else 2


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LipSyncError(Exception):
    """Adapter failure. `retryable` drives the queue's bounded backoff."""

    def __init__(self, message: str, *, remediation: str = "", retryable: bool = False):
        super().__init__(message)
        self.remediation = remediation
        self.retryable = retryable

    def with_remediation(self) -> str:
        msg = str(self)
        return f"{msg} — {self.remediation}" if self.remediation else msg


class LipSyncUnavailable(LipSyncError):
    """No usable provider (no GPU/weights/endpoint). Fail closed, no retry."""

    def __init__(self, message: str, *, remediation: str = ""):
        super().__init__(message, remediation=remediation, retryable=False)


class LipSyncTransient(LipSyncError):
    """Recoverable failure (connection reset, 5xx, lock contention)."""

    def __init__(self, message: str, *, remediation: str = ""):
        super().__init__(message, remediation=remediation, retryable=True)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass
class Health:
    provider: str
    status: str = HEALTH_UNAVAILABLE
    detail: str = ""
    remediation: str = ""
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status in {HEALTH_AVAILABLE, HEALTH_DEGRADED}

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "available": self.available,
            "detail": self.detail,
            "remediation": self.remediation,
            "checks": dict(self.checks),
        }


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------


class LipSyncProvider(abc.ABC):
    """Adapter contract: health / submit / status / cancel / result."""

    name: str = "base"

    @abc.abstractmethod
    def health(self) -> Health:
        raise NotImplementedError

    @abc.abstractmethod
    def submit(
        self,
        video_ref: str,
        audio_ref: str,
        workspace_id: str,
        opts: dict | None = None,
    ) -> str:
        """Start work; returns an adapter job id. Raises LipSyncError on failure."""
        raise NotImplementedError

    @abc.abstractmethod
    def status(self, job_id: str) -> dict:
        """{'status': JOB_*, 'progress': float, 'error': str}."""
        raise NotImplementedError

    @abc.abstractmethod
    def cancel(self, job_id: str) -> bool:
        """Best-effort cancellation; True when the adapter stopped the job."""
        raise NotImplementedError

    @abc.abstractmethod
    def result(self, job_id: str) -> dict:
        """Terminal result: {'asset_ref', 'gpu_seconds', 'cost_usd', ...}."""
        raise NotImplementedError


__all__ = [
    "ACTIVE_STATUSES",
    "AMBIGUOUS_BUSINESS_STATUS",
    "COST_ACTUAL",
    "COST_ESTIMATED",
    "COST_NOT_APPLICABLE",
    "COST_OUTCOMES",
    "COST_UNKNOWN_EXPOSURE",
    "EXECUTION_CANCELLED",
    "EXECUTION_CONFIRMED",
    "EXECUTION_OUTCOMES",
    "EXECUTION_PREPARED",
    "EXECUTION_REJECTED",
    "EXECUTION_SUBMISSION_UNKNOWN",
    "EXECUTION_SUCCEEDED",
    "HEALTH_AVAILABLE",
    "HEALTH_DEGRADED",
    "HEALTH_UNAVAILABLE",
    "JOB_CANCELLED",
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "JOB_STATUSES",
    "JOB_SUCCEEDED",
    "JOB_TIMEOUT",
    "TERMINAL_STATUSES",
    "Health",
    "LipSyncError",
    "LipSyncProvider",
    "LipSyncTransient",
    "LipSyncUnavailable",
    "default_concurrency",
    "env_flag",
    "env_float",
    "env_int",
    "env_str",
    "gpu_present",
]
