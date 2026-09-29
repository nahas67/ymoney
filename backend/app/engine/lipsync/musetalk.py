"""MuseTalk lip-sync adapter — isolated subprocess / GPU-worker style.

MuseTalk is OPTIONAL. Nothing in this module runs at import time, so
importing `app` (and starting the server) works on a box with no GPU, no
CUDA and no MuseTalk checkout: health() then reports `unavailable` with
remediation and submit() fails closed.

Integration is command-template based (no vendored MuseTalk code, no added
dependency — see docs/oss/OSS_COMPONENTS.md for the license evaluation):

    LIPSYNC_MUSE_COMMAND  e.g. "python /opt/musetalk/inference.py \
                               --video {video} --audio {audio} --out {out}"
    LIPSYNC_MUSE_MODEL_DIR  path to the downloaded weights directory
    LIPSYNC_GPU_USD_PER_HOUR  cost accounting rate (0 = unpriced self-host)

The heavy step always executes in a spawned process that the worker queue
watches with a hard timeout; it never runs inside an API request.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.engine.lipsync.base import (
    HEALTH_AVAILABLE,
    HEALTH_DEGRADED,
    HEALTH_UNAVAILABLE,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    Health,
    LipSyncError,
    LipSyncProvider,
    LipSyncUnavailable,
    default_concurrency,
    env_float,
    env_int,
    env_str,
    gpu_present,
)

REMEDIATION_INSTALL = (
    "MuseTalk is not installed/configured. YMONEY starts without it: either "
    "set LIPSYNC_MUSE_COMMAND + LIPSYNC_MUSE_MODEL_DIR (weights from "
    "https://huggingface.co/TMElyralab/MuseTalk), or set "
    "LIPSYNC_EXTERNAL_BASE_URL to a lip-sync API, or leave LIPSYNC_PROVIDER=auto "
    "to keep the fail-closed fallback."
)
REMEDIATION_GPU = (
    "No CUDA GPU detected (nvidia-smi not found). MuseTalk inference needs a "
    "GPU; set LIPSYNC_GPU=1 if detection is wrong, otherwise use "
    "LIPSYNC_EXTERNAL_BASE_URL or leave the fail-closed fallback in place."
)
REMEDIATION_WEIGHTS = (
    "MuseTalk weights missing: download them (huggingface.co/TMElyralab/MuseTalk "
    "+ sd-vae/whisper/dwpose/syncnet components) and point LIPSYNC_MUSE_MODEL_DIR "
    "at the models directory."
)


@dataclass
class _MuseJob:
    job_id: str
    command: list[str]
    output_ref: str
    started_at: float
    expected_seconds: float
    proc: subprocess.Popen | None = None
    cancelled: bool = False
    finished_at: float | None = None
    returncode: int | None = None
    log_path: str = ""
    extra: dict = field(default_factory=dict)


class MuseTalkAdapter(LipSyncProvider):
    """Subprocess-backed MuseTalk runner (never in-request, never import-time)."""

    name = "musetalk"

    def __init__(
        self,
        command: str | None = None,
        model_dir: str | None = None,
        *,
        gpu_usd_per_hour: float | None = None,
    ):
        self._command = (command if command is not None else env_str("LIPSYNC_MUSE_COMMAND")).strip()
        self._model_dir = (model_dir if model_dir is not None else env_str("LIPSYNC_MUSE_MODEL_DIR")).strip()
        self._gpu_usd_per_hour = (
            gpu_usd_per_hour
            if gpu_usd_per_hour is not None
            else env_float("LIPSYNC_GPU_USD_PER_HOUR", 0.0)
        )
        self._jobs: dict[str, _MuseJob] = {}

    # -- health ---------------------------------------------------------

    def health(self) -> Health:
        checks = {
            "command_configured": bool(self._command),
            "model_dir_configured": bool(self._model_dir),
            "model_dir_exists": bool(self._model_dir) and Path(self._model_dir).exists(),
            "gpu": gpu_present(),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "max_concurrency": default_concurrency(),
            "musetalk_enabled": bool(self._command),
        }
        if not self._command:
            return Health(
                provider=self.name,
                status=HEALTH_UNAVAILABLE,
                detail="MuseTalk command not configured (MuseTalk not installed)",
                remediation=REMEDIATION_INSTALL,
                checks=checks,
            )
        if not self._model_dir or not Path(self._model_dir).exists():
            return Health(
                provider=self.name,
                status=HEALTH_UNAVAILABLE,
                detail="MuseTalk model weights not found",
                remediation=REMEDIATION_WEIGHTS,
                checks=checks,
            )
        if not gpu_present():
            return Health(
                provider=self.name,
                status=HEALTH_UNAVAILABLE,
                detail="no CUDA GPU detected for MuseTalk inference",
                remediation=REMEDIATION_GPU,
                checks=checks,
            )
        if not checks["ffmpeg"]:
            return Health(
                provider=self.name,
                status=HEALTH_DEGRADED,
                detail="GPU + weights ready but ffmpeg is missing from PATH",
                remediation="install ffmpeg (MuseTalk needs it to mux frames/audio)",
                checks=checks,
            )
        return Health(
            provider=self.name,
            status=HEALTH_AVAILABLE,
            detail="GPU, weights and command present",
            checks=checks,
        )

    # -- submit ---------------------------------------------------------

    def submit(
        self,
        video_ref: str,
        audio_ref: str,
        workspace_id: str,
        opts: dict | None = None,
    ) -> str:
        opts = dict(opts or {})
        health = self.health()
        if not health.available:
            raise LipSyncUnavailable(
                f"musetalk unavailable: {health.detail}",
                remediation=health.remediation,
            )
        if not (video_ref or "").strip() or not (audio_ref or "").strip():
            raise LipSyncError(
                "video_ref and audio_ref are required",
                remediation="pass both inputs (source video + dubbed audio track)",
            )
        for label, ref in (("video", video_ref), ("audio", audio_ref)):
            if "://" not in ref and not Path(ref).exists():
                raise LipSyncError(
                    f"{label} input not found: {ref}",
                    remediation=f"register/upload the {label} asset before submitting",
                )

        job_id = f"muse-{uuid.uuid4().hex[:16]}"
        output_ref = str(opts.get("output_ref") or "").strip()
        if not output_ref:
            root = _workspace_outdir(workspace_id)
            output_ref = str(root / f"{job_id}.mp4")
        mapping = {
            "video": video_ref,
            "audio": audio_ref,
            "out": output_ref,
            "workspace_id": workspace_id,
            "model_dir": self._model_dir,
            "job_id": job_id,
        }
        try:
            command = shlex.split(self._command.format(**mapping))
        except (KeyError, ValueError) as exc:
            raise LipSyncError(
                f"LIPSYNC_MUSE_COMMAND template is invalid: {exc}",
                remediation="use only the {video} {audio} {out} {model_dir} placeholders",
            ) from exc
        if not command:
            raise LipSyncError("LIPSYNC_MUSE_COMMAND resolved to an empty command")

        Path(output_ref).parent.mkdir(parents=True, exist_ok=True)
        log_path = f"{output_ref}.log"
        expected = float(opts.get("expected_seconds") or env_int("LIPSYNC_TIMEOUT_SECONDS", 900))
        job = _MuseJob(
            job_id=job_id,
            command=command,
            output_ref=output_ref,
            started_at=time.time(),
            expected_seconds=max(1.0, expected),
            log_path=log_path,
        )
        try:
            # The child keeps its own inherited handle, so the parent's copy
            # can close as soon as Popen returns.
            with open(log_path, "w", encoding="utf-8", errors="replace") as log_handle:
                job.proc = subprocess.Popen(  # operator-configured command template
                    command,
                    cwd=str(opts["cwd"]) if opts.get("cwd") else None,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
        except OSError as exc:
            raise LipSyncError(
                f"failed to start MuseTalk subprocess: {exc}",
                remediation=(
                    "verify LIPSYNC_MUSE_COMMAND, that its interpreter exists and "
                    "that the output directory is writable"
                ),
            ) from exc
        self._jobs[job_id] = job
        return job_id

    # -- status / cancel / result ---------------------------------------

    def status(self, job_id: str) -> dict:
        job = self._get(job_id)
        if job.cancelled:
            return {"status": JOB_CANCELLED, "progress": 1.0, "error": "cancelled"}
        if job.proc is None:
            return {"status": JOB_FAILED, "progress": 0.0, "error": "subprocess never started"}
        rc = job.proc.poll()
        if rc is None:
            elapsed = time.time() - job.started_at
            progress = min(0.95, max(0.05, elapsed / job.expected_seconds))
            return {"status": JOB_RUNNING, "progress": round(progress, 3), "error": ""}
        job.returncode = rc
        job.finished_at = job.finished_at or time.time()
        if rc == 0:
            return {"status": JOB_SUCCEEDED, "progress": 1.0, "error": ""}
        return {
            "status": JOB_FAILED,
            "progress": 1.0,
            "error": f"MuseTalk exited with code {rc}: {_log_tail(job)}",
        }

    def cancel(self, job_id: str) -> bool:
        job = self._get(job_id)
        job.cancelled = True
        proc = job.proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except OSError:
            return False
        job.finished_at = time.time()
        return True

    def result(self, job_id: str) -> dict:
        job = self._get(job_id)
        state = self.status(job_id)
        if state["status"] != JOB_SUCCEEDED:
            raise LipSyncError(
                f"no usable result for adapter job {job_id} (status {state['status']}: "
                f"{state.get('error', '')})",
                remediation="inspect the adapter log, fix the inputs and resubmit",
            )
        asset_ref = job.output_ref
        if not asset_ref:
            raise LipSyncError(
                "MuseTalk produced no output reference",
                remediation="include {out} in LIPSYNC_MUSE_COMMAND or pass opts.output_ref",
            )
        if "://" not in asset_ref and not Path(asset_ref).exists():
            raise LipSyncError(
                f"expected output missing: {asset_ref} — refusing to ship",
                remediation="check the adapter log for a mux/path failure and resubmit",
            )
        gpu_seconds = round((job.finished_at or time.time()) - job.started_at, 3)
        return {
            "asset_ref": asset_ref,
            "gpu_seconds": gpu_seconds,
            "cost_usd": round(gpu_seconds * self._gpu_usd_per_hour / 3600.0, 6),
            "provider": self.name,
            "log_ref": job.log_path,
        }

    # -- helpers ---------------------------------------------------------

    def _get(self, job_id: str) -> _MuseJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise LipSyncError(
                f"unknown adapter job id: {job_id}",
                remediation="job ids are per-process; resubmit after a restart",
            )
        return job


def _log_tail(job: _MuseJob, limit: int = 400) -> str:
    try:
        text = Path(job.log_path).read_text(encoding="utf-8", errors="replace")
        return text[-limit:].strip()
    except OSError:
        return ""


def _workspace_outdir(workspace_id: str) -> Path:
    from app.services.storage import STORAGE_ROOT

    path = STORAGE_ROOT / (workspace_id or "_system") / "lipsync"
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = ["MuseTalkAdapter"]
