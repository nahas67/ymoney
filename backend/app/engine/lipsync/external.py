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

Work 15.7: ``POST /jobs`` is the billable submit, and the generic retry loop
above used to re-POST it after a lost response -- buying the same render up to
three times. The submit now runs exactly once through
:class:`~app.services.paid_executor.PaidProviderExecutor`, which persists the
returned ``job_id`` immediately and turns an ambiguous outcome into
``PaidSubmissionUnconfirmed``. The worker retries the POLL and the RESULT fetch
against that id and never the create.
"""

from __future__ import annotations

import httpx
from loguru import logger

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
from app.services.paid_executor import (
    CostOutcome,
    CostRecord,
    IdempotencySupport,
    PaidProviderExecutor,
    PaidSubmission,
    PaidSubmissionUnconfirmed,
    RemoteSubmission,
    SubmissionState,
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

#: Records of the billable submissions this adapter made, newest last. The
#: worker reads the tail of this list to persist the remote id on BOTH the
#: success and the ambiguity branch -- a job id we held but never wrote is an
#: invoice nobody can reconcile.
SUBMISSIONS: list[PaidSubmission] = []

#: Bounded so a long-running process cannot grow it without limit.
MAX_RETAINED_SUBMISSIONS = 64


def _paid(operation: str, *, workspace_id: str = "") -> PaidProviderExecutor:
    """Executor for one billable ``POST /jobs``.

    ``persist`` is best-effort: the durable record is written by the worker onto
    the ``lipsync_jobs`` row, and telemetry must never be able to abort a render
    that has already been paid for.
    """
    def persist(record: PaidSubmission) -> None:
        SUBMISSIONS.append(record)
        if len(SUBMISSIONS) > MAX_RETAINED_SUBMISSIONS:
            del SUBMISSIONS[:-MAX_RETAINED_SUBMISSIONS]
        try:
            from app.services.events import record_event

            record_event(record.workspace_id or None, kind="paid.submission",
                         message=f"{record.provider}.{record.operation} "
                                 f"{record.state} (submission {record.submission_id})",
                         level="warning" if record.state
                         is SubmissionState.SUBMISSION_UNKNOWN else "info",
                         source="lipsync.external", data=record.to_dict())
        except Exception as exc:  # noqa: BLE001 - telemetry never breaks a render
            logger.warning("paid lipsync submission not recorded: "
                           f"{type(exc).__name__}: {exc}")

    def cost_hook(record: PaidSubmission) -> None:
        # The worker reports the real cost_usd from the result payload; this
        # hook only has to make sure an unpriced exposure is never booked $0.
        amount = record.cost.ledger_value
        if amount is None or amount <= 0:
            logger.warning("lipsync submission %s has no reported cost; "
                           "exposure=%s", record.submission_id,
                           str(record.cost.outcome))
            return
        try:
            from app.services import cost as cost_service

            cost_service.track_cost(
                record.workspace_id or "", "video", amount,
                provider=record.provider,
                is_estimate=record.cost.outcome is CostOutcome.ESTIMATED,
                detail=record.to_dict())
        except Exception as exc:  # noqa: BLE001 - ledger must not break a render
            logger.warning("lipsync cost not booked: %s: %s",
                           type(exc).__name__, exc)

    return PaidProviderExecutor(
        workspace_id=workspace_id,
        provider="external_lipsync_worker",
        operation=operation,
        persist=persist,
        cost_hook=cost_hook,
        submit_budget=lambda: _assert_lipsync_budget(workspace_id),
        idempotency=IdempotencySupport.UNSUPPORTED,
    )


def _assert_lipsync_budget(workspace_id: str) -> None:
    """Pre-spend gate. BEFORE ``POST /jobs``, or it is not a gate.

    The amount is unknown up front -- the worker reports ``cost_usd`` per job,
    which arrives with the result -- so this enforces the caps against an
    estimated spend derived from the GPU rate the queue is configured with.
    """
    from app.services.cost import assert_can_spend

    estimate = 0.0
    try:
        estimate = round(env_float("LIPSYNC_GPU_USD_PER_HOUR", 0.0)
                         * env_float("LIPSYNC_TIMEOUT_SECONDS", 900.0) / 3600.0, 6)
    except Exception:  # noqa: BLE001 - an unparseable env must not guess money
        estimate = 0.0
    assert_can_spend(workspace_id, estimate)


def last_submission() -> PaidSubmission | None:
    """The most recent billable submission record, or ``None``."""
    return SUBMISSIONS[-1] if SUBMISSIONS else None


class ExternalAdapter(LipSyncProvider):
    """Remote lip-sync worker over plain HTTP.

    ``submit_is_billable = True`` is the declaration the worker queue reads to
    decide whether an ambiguous submit may be retried: the job is billed by the
    remote operator, so the queue persists the ambiguity and stops instead of
    POSTing /jobs again.
    """

    name = "external"
    submit_is_billable = True

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
        executor = _paid("lipsync.job_submit", workspace_id=workspace_id or "")

        def create(_idempotency_key: str) -> RemoteSubmission:
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
                # Accepted (or not) with no handle: we cannot tell a refusal
                # from a billable job we lost track of, so we refuse to guess
                # and the caller must NOT resubmit on this answer.
                raise PaidSubmissionUnconfirmed(
                    provider="external_lipsync_worker",
                    detail="lip-sync endpoint did not return a job id for "
                           "POST /jobs",
                )
            return RemoteSubmission(remote_id=job_id, raw=data)

        # ONE POST. On ambiguity this raises and the remote id (if the worker
        # gave us one before the failure) is already in SUBMISSIONS for the
        # worker to persist. It is never resubmitted from here.
        return executor.execute(create).remote_id

    def settle_submission(self, *, actual_cost: float | None = None,
                          detail: str = "") -> PaidSubmission | None:
        """Close the book on the last submit once the result is known.

        ``cost_usd`` comes back with the job result, so the amount is only ever
        recorded once the worker actually reported it. Without it the exposure
        stays UNKNOWN rather than being booked as zero.
        """
        record = last_submission()
        if record is None:
            return None
        if actual_cost is not None:
            record.cost = CostRecord(outcome=CostOutcome.ACTUAL,
                                     estimated=record.estimated_cost,
                                     actual=float(actual_cost))
        else:
            record.cost = CostRecord(outcome=CostOutcome.UNKNOWN_EXPOSURE,
                                     estimated=record.estimated_cost)
        if detail:
            record.detail = f"{record.detail} | {detail}"
        record.touch()
        executor = _paid(record.operation, workspace_id=record.workspace_id)
        executor.mark_succeeded(record)
        return record

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


__all__ = ["SUBMISSIONS", "ExternalAdapter", "last_submission"]
