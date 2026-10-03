"""LocalWorkerQueue: isolated lip-sync execution.

One daemon thread per job, admission gated by a concurrency (VRAM) semaphore
(default 1 without a GPU), with:

* hard timeout  -> TIMEOUT status, adapter job cancelled, cost recorded;
* cancellation  -> CANCELLED status, adapter job cancelled, no result;
* bounded retries with exponential backoff for transient submit failures;
* progress polling written to the `lipsync_jobs` row + activity events;
* fail-closed terminal writes (conditional, so a cancelled row stays cancelled).

Work 15.7 changed one of those bullets on purpose. "Bounded retries for
transient submit failures" used to re-``POST`` the billable job, so a read
timeout -- which proves the request was DELIVERED and only the answer was lost
-- could buy the same render up to three times. The submit is now attempted
exactly once. A ``PaidSubmissionUnconfirmed`` from the adapter means the worker
may already have created and billed the job, so it is persisted as
``SUBMISSION_UNKNOWN`` with whatever remote id we hold and is never resubmitted;
the retry budget applies to the POLL and to the RESULT fetch, which are free.
"""

from __future__ import annotations

import contextlib
import threading
import time

from loguru import logger

from app.engine.lipsync import rows as job_rows
from app.engine.lipsync.base import (
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_TIMEOUT,
    Health,
    LipSyncError,
    LipSyncProvider,
    LipSyncTransient,
    LipSyncUnavailable,
    default_concurrency,
    env_float,
    env_int,
)
from app.services.paid_executor import (
    CostOutcome,
    RetryVerdict,
    verdict_for,
)
from app.services.paid_jobs import PaidJobError, PaidSubmissionUnconfirmed

_UNKNOWN_STATUS_LIMIT = 5

#: Paid-submission state for a submit that may already have been billed. It
#: matches the value `engine/agents/production.py` writes for a render whose
#: submit was delivered but unconfirmed, so both lanes speak one vocabulary.
#: It is recorded in the job row's ``cost_json``, not in ``status``: the status
#: vocabulary belongs to `lipsync/base.py`, which this lane does not own.
SUBMISSION_UNKNOWN_STATE = "SUBMISSION_UNKNOWN"

#: Adapter attribute naming a submit that a retry could charge for. An
#: adapter that does not declare it is treated as operator compute, so its
#: transient retry budget is unchanged; the billable adapter declares it
#: and loses its automatic submit retry on purpose.
BILLABLE_SUBMIT_ATTR = "submit_is_billable"


class _Entry:
    __slots__ = ("job_id", "video_ref", "audio_ref", "workspace_id", "opts",
                 "cancel_event", "thread", "adapter_job_id", "attempts")

    def __init__(self, job_id: str, video_ref: str, audio_ref: str,
                 workspace_id: str, opts: dict):
        self.job_id = job_id
        self.video_ref = video_ref
        self.audio_ref = audio_ref
        self.workspace_id = workspace_id
        self.opts = dict(opts or {})
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.adapter_job_id = ""
        self.attempts = 0


class LocalWorkerQueue:
    """Runs adapter jobs off the request path with timeout/cancel/retry."""

    def __init__(
        self,
        provider: LipSyncProvider | None = None,
        *,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        poll_interval: float | None = None,
        concurrency: int | None = None,
        gpu_usd_per_hour: float | None = None,
    ):
        from app.engine.lipsync.factory import build_provider

        self.provider = provider if provider is not None else build_provider()
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else env_float("LIPSYNC_TIMEOUT_SECONDS", 900.0)
        )
        self.max_retries = (
            max_retries if max_retries is not None else env_int("LIPSYNC_MAX_RETRIES", 2)
        )
        self.backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else env_float("LIPSYNC_BACKOFF_SECONDS", 0.5)
        )
        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else env_float("LIPSYNC_POLL_INTERVAL_SECONDS", 0.5)
        )
        self.concurrency = concurrency if concurrency else default_concurrency()
        self.gpu_usd_per_hour = (
            gpu_usd_per_hour
            if gpu_usd_per_hour is not None
            else env_float("LIPSYNC_GPU_USD_PER_HOUR", 0.0)
        )
        self._sem = threading.BoundedSemaphore(max(1, int(self.concurrency)))
        self._entries: dict[str, _Entry] = {}
        self._attempts_log: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- public API ------------------------------------------------------

    def submit(
        self,
        job_id: str,
        video_ref: str,
        audio_ref: str,
        workspace_id: str,
        opts: dict | None = None,
    ) -> None:
        entry = _Entry(job_id, video_ref, audio_ref, workspace_id, opts or {})
        with self._lock:
            if job_id in self._entries:
                raise ValueError(f"job {job_id} is already queued")
            self._entries[job_id] = entry
        entry.thread = threading.Thread(
            target=self._run, args=(entry,), name=f"lipsync-{job_id[:8]}", daemon=True
        )
        entry.thread.start()

    def cancel(self, job_id: str) -> bool:
        """Signal cancellation; True when a live entry accepted it."""
        with self._lock:
            entry = self._entries.get(job_id)
        if entry is None:
            return False
        entry.cancel_event.set()
        return True

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        """Block until the job's worker thread exits (tests / admin joins)."""
        with self._lock:
            entry = self._entries.get(job_id)
        if entry is None or entry.thread is None:
            return True
        entry.thread.join(timeout)
        return not entry.thread.is_alive()

    def attempts(self, job_id: str) -> int:
        with self._lock:
            entry = self._entries.get(job_id)
        if entry is not None and entry.attempts:
            return entry.attempts
        return self._attempts_log.get(job_id, -1)

    def stats(self) -> dict:
        with self._lock:
            active = len(self._entries)
        return {
            "provider": getattr(self.provider, "name", "unknown"),
            "active": active,
            "concurrency": self.concurrency,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }

    def health(self) -> Health:
        return self.provider.health()

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            entry.cancel_event.set()
        for entry in entries:
            if entry.thread is not None:
                entry.thread.join(timeout)

    # -- worker ----------------------------------------------------------

    def _run(self, entry: _Entry) -> None:
        try:
            self._execute(entry)
        except Exception as exc:  # pragma: no cover - defensive terminal write
            logger.exception(f"lipsync worker crashed for {entry.job_id}: {exc}")
            job_rows.finish_job(entry.job_id, JOB_FAILED, error=f"worker crashed: {exc}")
        finally:
            with self._lock:
                self._entries.pop(entry.job_id, None)

    def _execute(self, entry: _Entry) -> None:
        started = time.monotonic()
        if entry.cancel_event.is_set() or job_rows.row_status(entry.job_id) == JOB_CANCELLED:
            job_rows.finish_job(entry.job_id, JOB_CANCELLED, error="cancelled before start")
            job_rows.emit(entry.workspace_id, "lipsync.cancelled",
                          "Lip-sync job cancelled before start", data={"job_id": entry.job_id})
            return
        if not job_rows.mark_running(entry.job_id, provider=getattr(self.provider, "name", "")):
            return  # row is no longer QUEUED (cancelled or claimed elsewhere)
        job_rows.emit(
            entry.workspace_id,
            "lipsync.started",
            f"Lip-sync job started via {getattr(self.provider, 'name', 'unknown')}",
            data={"job_id": entry.job_id},
        )

        if not self._acquire_slot(entry, started):
            return
        try:
            adapter_job = self._submit_with_retries(entry)
            if adapter_job is None:
                return
            self._poll(entry, adapter_job, started)
        finally:
            with contextlib.suppress(ValueError):  # double-release guard
                self._sem.release()

    def _acquire_slot(self, entry: _Entry, started: float) -> bool:
        """VRAM/concurrency gate; cancellable and bounded by the job deadline."""
        while True:
            if entry.cancel_event.is_set() or job_rows.row_status(entry.job_id) == JOB_CANCELLED:
                job_rows.finish_job(entry.job_id, JOB_CANCELLED, error="cancelled while queued")
                return False
            if self._sem.acquire(timeout=0.05):
                return True
            if time.monotonic() - started >= self.timeout_seconds:
                job_rows.finish_job(
                    entry.job_id,
                    JOB_TIMEOUT,
                    error=f"no worker slot within {self.timeout_seconds:.0f}s "
                          f"(concurrency {self.concurrency})",
                    cost=self._cost(time.monotonic() - started),
                )
                return False

    def _max_attempts(self) -> int:
        return max(1, self.max_retries + 1)

    def _submit_with_retries(self, entry: _Entry) -> str | None:
        """Submit the job, retrying only what a retry cannot charge for.

        Work 15.7. The billable adapter (``ExternalAdapter``) declares
        ``submit_is_billable = True``, and for it an ambiguous submit is
        terminal: the POST may have been delivered AND billed, so the job is
        persisted as SUBMISSION_UNKNOWN with the remote id and stops. A local
        operator-compute adapter (MuseTalk, and the test doubles) costs CPU per
        retry, not money, so its transient retry budget is unchanged.

        The name is historical in exactly one respect: the billable lane now
        attempts the submit ONCE.
        """
        from app.services.cost import BudgetExceededError

        attempt = 0
        max_attempts = self._max_attempts()
        while attempt < max_attempts:
            attempt += 1
            entry.attempts = attempt
            with self._lock:
                self._attempts_log[entry.job_id] = attempt
            if entry.cancel_event.is_set():
                job_rows.finish_job(entry.job_id, JOB_CANCELLED, error="cancelled before submit")
                return None
            try:
                adapter_job = self.provider.submit(
                    entry.video_ref, entry.audio_ref, entry.workspace_id, entry.opts
                )
            except LipSyncUnavailable as exc:
                self._fail(entry, exc, status_note="provider unavailable")
                return None
            except PaidJobError as exc:
                # THE money branch, and it is authoritative: the adapter
                # classified the submit through the shared paid contract. A
                # verdict of RETRY means the connection was never established,
                # so a second POST cannot bill twice; anything else is unknown
                # and terminal.
                if self._may_resubmit(exc) and attempt < max_attempts:
                    self._sleep_backoff(entry, attempt)
                    continue
                if self._is_ambiguous(exc):
                    self._record_unknown_submission(entry, exc, attempts=attempt)
                    return None
                self._fail(entry, LipSyncError(
                    f"submit failed after {attempt} attempt(s): {exc}",
                    remediation=(getattr(exc, "remediation", None)
                                 or "the lip-sync backend refused the job; "
                                    "check its validation rules"),
                ))
                return None
            except BudgetExceededError as exc:
                # The provider's pre-spend gate refused BEFORE the request left.
                # Nothing was sent, so this is a plain failure, not ambiguity.
                self._fail(entry, LipSyncError(
                    f"budget refused the submit: {exc}",
                    remediation="raise the workspace budget, or wait for the "
                                "spending window to reset",
                ))
                return None
            except LipSyncError as exc:
                transient = isinstance(exc, LipSyncTransient)
                if transient and self._submit_may_bill() and not self._provably_undelivered(exc):
                    # Billable adapter outside the paid contract: assume the
                    # worst, because a second render is the expensive mistake.
                    self._record_unknown_submission(entry, exc, attempts=attempt)
                    return None
                if transient:
                    if attempt < max_attempts:
                        self._sleep_backoff(entry, attempt)
                        continue
                    # Retries exhausted: report the bound, not just the last error.
                    self._fail(entry, LipSyncError(
                        f"submit failed after {attempt} attempt(s): {exc}",
                        remediation=(exc.remediation
                                     or "the lip-sync backend never became "
                                        "reachable — check its logs"),
                    ))
                    return None
                self._fail(entry, exc)
                return None
            except Exception as exc:  # pragma: no cover - unexpected adapter bug
                self._fail(entry, LipSyncError(f"adapter submit crashed: {exc}"))
                return None
            # Accepted. Persist the remote id BEFORE polling so a crash mid-poll
            # still leaves an addressable job.
            entry.adapter_job_id = adapter_job
            job_rows.set_adapter_job(entry.job_id, adapter_job)
            return adapter_job
        return None

    def _submit_may_bill(self) -> bool:
        """Whether re-POSTing this adapter's submit could cost money.

        An adapter that says nothing is assumed to be operator compute, which
        is what every local lane (MuseTalk) and every test double is. The one
        adapter that bills declares ``submit_is_billable = True``, and that
        declaration is what switches this worker from "retry the transient" to
        "persist the ambiguity and stop". Guessing the wrong way round here is
        how a read timeout bought three renders, so the declaration is read
        from the adapter rather than inferred from its message.
        """
        return bool(getattr(self.provider, BILLABLE_SUBMIT_ATTR, False))

    # -- paid-submission helpers ------------------------------------------

    @staticmethod
    def _record_of(exc):
        return getattr(exc, "submission", None)

    @classmethod
    def _may_resubmit(cls, exc) -> bool:
        """Reuse the contract's own rule instead of re-deriving one here."""
        record = cls._record_of(exc)
        if record is not None:
            return verdict_for(record) is RetryVerdict.RETRY
        return bool(getattr(exc, "provably_undelivered", False))

    @classmethod
    def _is_ambiguous(cls, exc) -> bool:
        """True when the adapter says money may already have been spent."""
        if cls._record_of(exc) is not None:
            return verdict_for(cls._record_of(exc)) is RetryVerdict.RECONCILE
        return isinstance(exc, PaidSubmissionUnconfirmed) and not cls._may_resubmit(exc)

    @classmethod
    def _provably_undelivered(cls, exc) -> bool:
        """Did the classifier prove the request never reached the worker?

        An adapter outside the paid contract carries no flag, so a transient is
        treated as UNDELIVERED only when it says so. Assuming "transient means
        retry me" is exactly what bought three renders.
        """
        record = cls._record_of(exc)
        if record is not None:
            return verdict_for(record) is RetryVerdict.RETRY
        flagged = getattr(exc, "provably_undelivered", None)
        if flagged is not None:
            return bool(flagged)
        text = str(exc).lower()
        return "timed out" in text and "connect" in text

    @staticmethod
    def _remote_id_of(exc) -> str:
        record = getattr(exc, "submission", None)
        if record is not None and getattr(record, "remote_id", ""):
            return str(record.remote_id)
        return str(getattr(exc, "remote_id", "") or "")

    def _record_unknown_submission(self, entry: _Entry, exc, *,
                                   attempts: int = 1) -> None:
        """Terminal write for a submit that may already have been billed.

        The job row's status vocabulary has no SUBMISSION_UNKNOWN member and
        this worker does not own that module, so the paid state is recorded in
        the fields it does own: ``cost_json`` carries the submission state, the
        exposure and the resubmit prohibition, and the error text names it. No
        ``mirror_cost`` here -- it would write ``amount_usd = 0.0`` for a job
        that may well have been paid for, which is the lie this lane removes.
        """
        remote_id = self._remote_id_of(exc)
        if remote_id:
            # A billed job nobody can address is an invoice nobody can
            # reconcile, so the id is persisted even on this branch.
            entry.adapter_job_id = remote_id
            job_rows.set_adapter_job(entry.job_id, remote_id)
        record = self._record_of(exc)
        cost = self._cost(None)
        cost.update({
            "submission_state": SUBMISSION_UNKNOWN_STATE,
            "resubmit_forbidden": True,
            "exposure": CostOutcome.UNKNOWN_EXPOSURE.value,
            "remote_id": remote_id,
            "submission_id": str(getattr(record, "submission_id", "") or ""),
            "attempts": attempts,
        })
        message = (
            "submit was delivered but not confirmed: the lip-sync worker may "
            "have accepted and billed this job. DO NOT RESUBMIT -- reconcile "
            "the worker's job list first"
            + (f" (remote job id: {remote_id})" if remote_id else "")
        )
        job_rows.finish_job(entry.job_id, JOB_FAILED, error=message, cost=cost)
        job_rows.emit(entry.workspace_id, "lipsync.submission_unknown", message,
                      level="error",
                      data={"job_id": entry.job_id, "remote_id": remote_id,
                            "attempts": attempts})
        logger.error("lipsync job %s: %s", entry.job_id, message)

    def _poll(self, entry: _Entry, adapter_job: str, started: float) -> None:
        deadline = started + self.timeout_seconds
        status_errors = 0
        last_progress = -1.0
        while True:
            if entry.cancel_event.is_set() or job_rows.row_status(entry.job_id) == JOB_CANCELLED:
                self._cancel_adapter(adapter_job)
                job_rows.finish_job(entry.job_id, JOB_CANCELLED, error="cancelled by request")
                job_rows.emit(entry.workspace_id, "lipsync.cancelled",
                              "Lip-sync job cancelled", data={"job_id": entry.job_id})
                return
            if time.monotonic() >= deadline:
                self._cancel_adapter(adapter_job)
                elapsed = time.monotonic() - started
                job_rows.finish_job(
                    entry.job_id,
                    JOB_TIMEOUT,
                    error=f"hard timeout after {self.timeout_seconds:.0f}s",
                    cost=self._cost(elapsed),
                )
                job_rows.mirror_cost(entry.workspace_id, entry.job_id,
                                     self._cost(elapsed), getattr(self.provider, "name", ""))
                job_rows.emit(entry.workspace_id, "lipsync.timeout",
                              f"Lip-sync job timed out after {elapsed:.0f}s",
                              level="warning", data={"job_id": entry.job_id})
                return
            try:
                state = self.provider.status(adapter_job)
                status_errors = 0
            except LipSyncError as exc:
                status_errors += 1
                if status_errors >= max(3, self.max_retries + 1):
                    self._fail(entry, exc, elapsed=time.monotonic() - started)
                    return
                self._sleep_backoff(entry, status_errors)
                continue
            state = state or {}
            if not isinstance(state, dict):  # pragma: no cover - defensive
                state = {"status": str(state)}
            status = str(state.get("status") or "").upper()
            progress = _as_float(state.get("progress"))
            if status == JOB_RUNNING or status == "QUEUED":
                if progress > last_progress and (
                    last_progress < 0.0 or progress - last_progress >= 0.1
                ):
                    last_progress = progress
                    job_rows.update_progress(entry.job_id, progress)
                    job_rows.emit(
                        entry.workspace_id,
                        "lipsync.progress",
                        f"Lip-sync {int(max(progress, 0.0) * 100)}%",
                        data={"job_id": entry.job_id, "progress": progress},
                    )
                self._sleep(entry, self.poll_interval)
                continue
            if status == JOB_SUCCEEDED:
                self._succeed(entry, adapter_job, started)
                return
            if status == JOB_CANCELLED:
                job_rows.finish_job(entry.job_id, JOB_CANCELLED, error="cancelled by provider")
                return
            if status == JOB_TIMEOUT:
                job_rows.finish_job(entry.job_id, JOB_TIMEOUT,
                                    error=str(state.get("error") or "provider timeout"),
                                    cost=self._cost(time.monotonic() - started))
                return
            if status == JOB_FAILED:
                self._fail(entry, LipSyncError(str(state.get("error") or "adapter failed")),
                           elapsed=time.monotonic() - started)
                return
            status_errors += 1
            if status_errors >= _UNKNOWN_STATUS_LIMIT:
                self._fail(
                    entry,
                    LipSyncError(f"adapter returned unknown status {status!r}"),
                    elapsed=time.monotonic() - started,
                )
                return
            self._sleep(entry, self.poll_interval)

    # -- terminal helpers -------------------------------------------------

    def _succeed(self, entry: _Entry, adapter_job: str, started: float) -> None:
        elapsed = time.monotonic() - started
        try:
            result = self.provider.result(adapter_job) or {}
        except LipSyncError as exc:
            # Fail closed: a "succeeded" job with no verifiable asset ships nothing.
            self._fail(entry, exc, elapsed=elapsed)
            return
        asset_ref = str(result.get("asset_ref") or "")
        if not asset_ref:
            self._fail(
                entry,
                LipSyncError("provider reported success without an asset reference",
                             remediation="the adapter must return a non-empty asset_ref"),
                elapsed=elapsed,
            )
            return
        reported = _as_float(result.get("cost_usd"))
        cost = {
            "gpu_seconds": _as_float(result.get("gpu_seconds")) or round(elapsed, 3),
            "cost_usd": reported or round(elapsed * self.gpu_usd_per_hour / 3600.0, 6),
            "provider": str(result.get("provider") or getattr(self.provider, "name", "")),
            "attempts": entry.attempts,
            # ``cost_priced`` says whether the amount is the worker's own
            # invoice or a local guess. A billed job whose amount nobody
            # reported must not read as $0 in the books.
            "cost_priced": bool(reported or self.gpu_usd_per_hour),
        }
        job_rows.finish_job(entry.job_id, JOB_SUCCEEDED, asset_ref=asset_ref,
                            cost=cost, progress=1.0)
        if cost["cost_priced"]:
            job_rows.mirror_cost(entry.workspace_id, entry.job_id, cost,
                                 getattr(self.provider, "name", ""))
        else:
            # No invoice, no price: record the exposure instead of a zero row.
            logger.warning("lipsync job %s succeeded with no reported cost; "
                           "exposure recorded as UNKNOWN rather than $0",
                           entry.job_id)
        self._settle_paid_submission(entry, reported or None)
        job_rows.emit(entry.workspace_id, "lipsync.succeeded",
                      f"Lip-sync finished ({cost['gpu_seconds']:.1f}s GPU)",
                      data={"job_id": entry.job_id, "asset_ref": asset_ref})

    def _settle_paid_submission(self, entry: _Entry,
                                actual_cost: float | None) -> None:
        """Close the paid-submission record for this job, when the adapter has one.

        Only an adapter that speaks the paid contract can settle it; the rest
        have no record to settle and are left alone.
        """
        settle = getattr(self.provider, "settle_submission", None)
        if settle is None:
            return
        try:
            settle(actual_cost=actual_cost,
                   detail=f"lipsync job {entry.job_id} succeeded")
        except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a render
            logger.debug("lipsync submission not settled: %s: %s",
                         type(exc).__name__, exc)

    def _fail(self, entry: _Entry, exc: Exception, *, elapsed: float | None = None,
              status_note: str = "") -> None:
        message = exc.with_remediation() if isinstance(exc, LipSyncError) else str(exc)
        if status_note:
            message = f"{status_note}: {message}"
        cost = self._cost(elapsed)
        job_rows.finish_job(entry.job_id, JOB_FAILED, error=message, cost=cost)
        if cost.get("gpu_seconds"):
            job_rows.mirror_cost(entry.workspace_id, entry.job_id, cost,
                                 getattr(self.provider, "name", ""))
        job_rows.emit(entry.workspace_id, "lipsync.failed",
                      f"Lip-sync failed: {message[:200]}", level="error",
                      data={"job_id": entry.job_id})
        logger.warning(f"lipsync job {entry.job_id} failed: {message}")

    def _cancel_adapter(self, adapter_job: str) -> None:
        try:
            self.provider.cancel(adapter_job)
        except Exception:  # pragma: no cover - cancel is best-effort
            logger.debug("adapter cancel failed; worker timeout still applies")

    # -- small helpers ----------------------------------------------------

    def _cost(self, elapsed: float | None) -> dict:
        seconds = round(max(0.0, float(elapsed or 0.0)), 3)
        return {
            "gpu_seconds": seconds,
            "cost_usd": round(seconds * self.gpu_usd_per_hour / 3600.0, 6),
        }

    def _sleep(self, entry: _Entry, seconds: float) -> None:
        """Cancellation-responsive sleep (10ms slices)."""
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if entry.cancel_event.is_set():
                return
            time.sleep(min(0.01, max(0.0, end - time.monotonic())))

    def _sleep_backoff(self, entry: _Entry, attempt: int) -> None:
        self._sleep(entry, min(self.backoff_seconds * (2 ** (attempt - 1)), 30.0))


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["LocalWorkerQueue"]
