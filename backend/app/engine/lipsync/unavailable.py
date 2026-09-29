"""Fallback adapter: reports honestly and fails closed.

Selected automatically when no GPU/model/endpoint exists. It never raises at
import, never pretends to be ready, and never produces output.
"""

from __future__ import annotations

from app.engine.lipsync.base import (
    HEALTH_UNAVAILABLE,
    Health,
    LipSyncProvider,
    LipSyncUnavailable,
    default_concurrency,
    gpu_present,
)

DEFAULT_REMEDIATION = (
    "No lip-sync backend is configured. Either install MuseTalk (weights + "
    "CUDA GPU) and set LIPSYNC_MUSE_COMMAND / LIPSYNC_MUSE_MODEL_DIR, or point "
    "LIPSYNC_EXTERNAL_BASE_URL at a lip-sync API, or keep jobs failing closed "
    "(no degraded output is ever produced)."
)


class UnavailableAdapter(LipSyncProvider):
    """Always-unavailable provider used as the safe default fallback."""

    name = "unavailable"

    def __init__(self, reason: str = "no lip-sync provider configured",
                 remediation: str = DEFAULT_REMEDIATION):
        self._reason = reason
        self._remediation = remediation

    def health(self) -> Health:
        return Health(
            provider=self.name,
            status=HEALTH_UNAVAILABLE,
            detail=self._reason,
            remediation=self._remediation,
            checks={
                "gpu": gpu_present(),
                "max_concurrency": default_concurrency(),
            },
        )

    def submit(self, video_ref: str, audio_ref: str, workspace_id: str,
               opts: dict | None = None) -> str:
        raise LipSyncUnavailable(
            f"lip-sync submit refused: {self._reason}",
            remediation=self._remediation,
        )

    def status(self, job_id: str) -> dict:
        raise LipSyncUnavailable(
            f"lip-sync status unavailable for job {job_id}",
            remediation=self._remediation,
        )

    def cancel(self, job_id: str) -> bool:
        return False  # nothing was ever running — honest no-op

    def result(self, job_id: str) -> dict:
        raise LipSyncUnavailable(
            f"lip-sync result unavailable for job {job_id}",
            remediation=self._remediation,
        )


__all__ = ["DEFAULT_REMEDIATION", "UnavailableAdapter"]
