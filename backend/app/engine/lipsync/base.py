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
