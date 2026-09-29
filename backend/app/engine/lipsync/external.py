"""HTTP lip-sync adapter for a remote/self-hosted worker endpoint.

Talks to an operator-configured base URL (GPU box, queue service, vendor API):

    GET  {base}/health           -> optional readiness payload
    POST {base}/jobs             -> {"job_id": "..."} (also accepts {"id"})
    GET  {base}/jobs/{id}        -> {"status", "progress", "error"}
    POST {base}/jobs/{id}/cancel -> {"cancelled": bool}
    GET  {base}/jobs/{id}/result -> {"asset_ref", "gpu_seconds", "cost_usd"}

Transient failures (connect/timeout/5xx/429) raise `LipSyncTransient` so the
worker queue retries them with bounded exponential backoff; everything else
fails closed with remediation. Nothing is called at import time.
"""

from __future__ import annotations

import httpx

from app.engine.lipsync.base import (
    HEALTH_AVAILABLE,
    HEALTH_DEGRADED,
    HEALTH_UNAVAILABLE,
    JOB_SUCCEEDED,
    Health,
    LipSyncError,
    LipSyncProvider,
    LipSyncTransient,
    LipSyncUnavailable,
    default_concurrency,
    env_float,
    env_str,
    gpu_present,
)

REMEDIATION_NOT_CONFIGURED = (
    "No lip-sync endpoint configured: set LIPSYNC_EXTERNAL_BASE_URL to a "
    "reachable lip-sync worker (e.g. https://gpu-box:8080), or configure "
    "MuseTalk instead."
)
REMEDIATION_UNREACHABLE = (
    "The lip-sync endpoint is unreachable — start the remote worker, check the "
    "URL/firewall, or fall back to LIPSYNC_PROVIDER=auto (fails closed)."
)


class ExternalAdapter(LipSyncProvider):
    """Remote lip-sync worker over plain HTTP."""

    name = "external"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
    ):
        self._base_url = (base_url if base_url is not None else env_str("LIPSYNC_EXTERNAL_BASE_URL")).rstrip("/")
        self._api_key = api_key if api_key is not None else env_str("LIPSYNC_EXTERNAL_API_KEY")
        self._timeout = timeout if timeout is not None else env_float("LIPSYNC_EXTERNAL_TIMEOUT_SECONDS", 60.0)

    # -- helpers ---------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 timeout: float | None = None) -> dict:
        url = f"{self._base_url}{path}"
        try:
            resp = httpx.request(
                method,
                url,
                json=json_body,
                headers=self._headers(),
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LipSyncTransient(
                f"lip-sync endpoint timed out: {method} {path}",
                remediation=REMEDIATION_UNREACHABLE,
            ) from exc
        except httpx.HTTPError as exc:
            raise LipSyncTransient(
                f"lip-sync endpoint unreachable: {exc}",
                remediation=REMEDIATION_UNREACHABLE,
            ) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise LipSyncTransient(
                f"lip-sync endpoint returned {resp.status_code} for {method} {path}",
                remediation=REMEDIATION_UNREACHABLE,
            )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:300]
            raise LipSyncError(
                f"lip-sync endpoint rejected {method} {path} with {resp.status_code}: {detail}",
                remediation="fix the request or the worker's validation rules",
            )
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"value": data}

    # -- contract --------------------------------------------------------

    def health(self) -> Health:
        checks = {
            "base_url_configured": bool(self._base_url),
            "max_concurrency": default_concurrency(),
            "gpu": gpu_present(),
        }
        if not self._base_url:
            return Health(
                provider=self.name,
                status=HEALTH_UNAVAILABLE,
                detail="LIPSYNC_EXTERNAL_BASE_URL is not set",
                remediation=REMEDIATION_NOT_CONFIGURED,
                checks=checks,
            )
        try:
            data = self._request("GET", "/health", timeout=min(self._timeout, 5.0))
        except LipSyncTransient as exc:
            return Health(
                provider=self.name,
                status=HEALTH_UNAVAILABLE,
                detail=str(exc),
                remediation=REMEDIATION_UNREACHABLE,
                checks=checks,
            )
        except LipSyncError as exc:
            # Reachable but no /health endpoint — jobs API may still work.
            return Health(
                provider=self.name,
                status=HEALTH_DEGRADED,
                detail=str(exc),
                remediation="expose GET /health on the worker for full readiness checks",
                checks=checks,
            )
        checks["remote"] = data
        return Health(
            provider=self.name,
            status=HEALTH_AVAILABLE,
            detail=f"endpoint {self._base_url} reachable",
            checks=checks,
        )

    def submit(
        self,
        video_ref: str,
        audio_ref: str,
        workspace_id: str,
        opts: dict | None = None,
    ) -> str:
        health = self.health()
        if not health.available:
            raise LipSyncUnavailable(
                f"external lip-sync unavailable: {health.detail}",
                remediation=health.remediation,
            )
        data = self._request(
            "POST",
            "/jobs",
            json_body={
                "video_ref": video_ref,
                "audio_ref": audio_ref,
                "workspace_id": workspace_id,
                "opts": dict(opts or {}),
            },
        )
        job_id = str(data.get("job_id") or data.get("id") or "").strip()
        if not job_id:
            raise LipSyncError(
                "lip-sync endpoint did not return a job id",
                remediation="the worker must answer POST /jobs with {job_id}",
            )
        return job_id

    def status(self, job_id: str) -> dict:
        data = self._request("GET", f"/jobs/{job_id}")
        raw = str(data.get("status") or "").upper()
        return {
            "status": raw,
            "progress": float(data.get("progress") or 0.0),
            "error": str(data.get("error") or ""),
        }

    def cancel(self, job_id: str) -> bool:
        data = self._request("POST", f"/jobs/{job_id}/cancel")
        return bool(data.get("cancelled", True))

    def result(self, job_id: str) -> dict:
        state = self.status(job_id)
        if state["status"] and state["status"] != JOB_SUCCEEDED:
            raise LipSyncError(
                f"no usable result for job {job_id} (status {state['status']}: "
                f"{state.get('error', '')})",
                remediation="inspect the worker logs and resubmit",
            )
        data = self._request("GET", f"/jobs/{job_id}/result")
        asset_ref = str(data.get("asset_ref") or data.get("video_ref") or "").strip()
        if not asset_ref:
            raise LipSyncError(
                f"lip-sync worker returned no asset_ref for job {job_id}",
                remediation="the worker must publish the output asset reference",
            )
        return {
            "asset_ref": asset_ref,
            "gpu_seconds": float(data.get("gpu_seconds") or 0.0),
            "cost_usd": float(data.get("cost_usd") or data.get("cost") or 0.0),
            "provider": self.name,
        }


__all__ = ["ExternalAdapter"]
