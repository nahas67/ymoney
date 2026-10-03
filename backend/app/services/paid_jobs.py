"""Paid-job safety: the contract between YMONEY and any billable provider.

A paid generation is the one place where a network error can cost real money.
If a submit request times out, YMONEY **cannot know** whether the provider
created and billed the job. Retrying blindly buys a second copy. Marking it
FAILED and moving on silently loses the first one.

This module makes that ambiguity explicit and forces a decision.

    PREPARED               we intend to submit; nothing sent
    SUBMISSION_ATTEMPTED   the request left, we have no response yet
    REMOTE_ID_CONFIRMED    the provider returned a durable id
    PROCESSING             the provider accepted it and is working
    SUCCEEDED              the artifact exists and was verified
    FAILED                 the provider definitively rejected or failed it
    SUBMISSION_UNKNOWN     it may have been billed; DO NOT resubmit

The three-way exception taxonomy is the load-bearing idea, re-derived for
YMONEY's ``httpx``/``RetryableProviderError`` idioms:

    PaidSubmissionUnconfirmed  the request may have been accepted and billed.
                               Never auto-retry. Requires reconciliation,
                               polling, or an explicit human decision.
    PaidJobRejected           the provider definitively refused (4xx). No task
                               was created, so a later retry is safe.
    PaidArtifactUndownloadable the remote job SUCCEEDED and was billed, but the
                               local fetch failed. Retry the DOWNLOAD, never
                               re-submit.

The single most important nuance, and the reason this is not a trivial
classification table: **a connect timeout and a read timeout mean different
things.** A connect timeout proves the TCP connection was never established,
so the request cannot have reached the server. A read timeout means the request
was delivered and the response was lost. Only the first is safe to retry.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry). The donor
has the same three-way taxonomy but keeps it as in-process exceptions with no
persistence; the merge with YMONEY's job ledger is the actual contribution.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import httpx

__all__ = [
    "MAX_POLL_RETRIES",
    "PaidArtifactUndownloadable",
    "PaidJobError",
    "PaidJobRejected",
    "PaidSubmissionUnconfirmed",
    "SubmissionState",
    "classify_submit_exception",
    "download_with_retry",
    "is_definitive_failure_status",
    "is_safe_to_retry",
    "poll_with_deadline",
    "record_submission",
]

#: Bounded polling. A billable job is polled, never re-submitted, so a generous
#: retry count costs nothing but latency.
MAX_POLL_RETRIES = 5
#: Linear backoff base, in seconds.
RETRY_BASE_SECONDS = 1.0
#: Never let a single poll request outlive half of the remaining deadline.
MAX_PHASE_TIMEOUT_SECONDS = 30.0


class SubmissionState(StrEnum):
    """The canonical state of a billable submission."""

    PREPARED = "PREPARED"
    SUBMISSION_ATTEMPTED = "SUBMISSION_ATTEMPTED"
    REMOTE_ID_CONFIRMED = "REMOTE_ID_CONFIRMED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    #: Work 15.7: a cancellation the provider has acknowledged. Distinct from
    #: FAILED because money is usually NOT spent, and distinct from
    #: SUBMISSION_UNKNOWN because the outcome IS known. ``poll_with_deadline``
    #: already treated the string ``"cancelled"`` as terminal while this enum
    #: had no such member; the vocabulary and the enum had drifted apart.
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:
        return self.value


#: States from which an automatic RESUBMIT is forbidden. Once a request might
#: have been billed, only reconciliation or a human may advance it.
NO_RESUBMIT = frozenset({SubmissionState.SUBMISSION_UNKNOWN,
                         SubmissionState.REMOTE_ID_CONFIRMED,
                         SubmissionState.PROCESSING,
                         SubmissionState.SUCCEEDED})

#: States in which the outcome is known, so a caller may decide a retry policy.
#: SUBMISSION_UNKNOWN is deliberately absent: it is the one state where nobody
#: may guess.
OUTCOME_KNOWN = frozenset({SubmissionState.FAILED,
                           SubmissionState.CANCELLED})

#: States where money MAY have been spent and the amount is not yet known.
#: A cost ledger must keep an unknown exposure visible rather than recording $0
#: merely because YMONEY lost the response (Work 15.7 §7).
MAY_HAVE_BEEN_BILLED = frozenset({SubmissionState.SUBMISSION_UNKNOWN,
                                 SubmissionState.REMOTE_ID_CONFIRMED,
                                 SubmissionState.PROCESSING,
                                 SubmissionState.SUCCEEDED})


class PaidJobError(Exception):
    """Base for every paid-submission failure mode."""


@dataclass
class PaidSubmissionUnconfirmed(PaidJobError):
    """The request MAY have been accepted and billed. Never auto-retry.

    ``remote_id`` is populated whenever the provider gave us an id before the
    ambiguity arose, so reconciliation can find the job even when we could not
    confirm the submission ourselves.
    """

    provider: str = ""
    detail: str = ""
    remote_id: str = ""
    #: True only when the connection was never established, which is the ONE
    #: ambiguous case that is provably safe to retry.
    provably_undelivered: bool = False
    attempt: int = 0

    def __str__(self) -> str:
        where = f" [{self.provider}]" if self.provider else ""
        rid = f" remote_id={self.remote_id}" if self.remote_id else ""
        retry = ("; connection never established, retry is safe"
                 if self.provably_undelivered
                 else "; DO NOT RESUBMIT automatically")
        return (f"paid submission unconfirmed{where}: {self.detail}{rid}{retry}")


@dataclass
class PaidJobRejected(PaidJobError):
    """The provider definitively refused. No task was created, so retry is safe."""

    provider: str = ""
    status_code: int = 0
    detail: str = ""

    def __str__(self) -> str:
        return (f"paid job rejected by {self.provider or 'provider'} "
                f"(HTTP {self.status_code}): {self.detail}")


@dataclass
class PaidArtifactUndownloadable(PaidJobError):
    """The remote job SUCCEEDED and was billed; the local fetch failed.

    The remote id is the recovery handle. Re-submitting would pay twice.
    """

    provider: str = ""
    remote_id: str = ""
    detail: str = ""
    url: str = ""

    def __str__(self) -> str:
        return (f"{self.provider or 'provider'} produced a paid artifact that "
                f"could not be downloaded: {self.detail} "
                f"(remote_id={self.remote_id or 'unknown'})")


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def is_definitive_failure_status(status_code: int) -> bool:
    """True when the provider certainly did NOT create a billable task.

    A 4xx is a rejection of the request itself. A 5xx is not: the server may
    have created the task and then failed while responding.
    """
    return 400 <= int(status_code) < 500


def is_safe_to_retry(exc: BaseException) -> bool:
    """True when we can PROVE the request never reached the provider.

    Only a connect-phase failure qualifies. A read timeout, a dropped
    connection, or a protocol error all mean the request was delivered and the
    response was lost -- the provider may already have billed us.
    """
    if isinstance(exc, httpx.ConnectTimeout):
        return True
    # A connect error that never established a socket is equally provable.
    return isinstance(exc, (httpx.ConnectError, ConnectionRefusedError,
                            socket.gaierror))


def classify_submit_exception(exc: BaseException, *, provider: str = "",
                              remote_id: str = "", attempt: int = 0
                              ) -> PaidJobError:
    """Turn a raw submit failure into the correct paid-job error.

    This is the single decision point. Getting it wrong either double-charges
    the user or silently abandons paid work.
    """
    if isinstance(exc, PaidJobError):
        return exc

    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is not None and is_definitive_failure_status(int(status)):
        return PaidJobRejected(provider=provider, status_code=int(status),
                               detail=_short(exc))

    # A read timeout on a POST that may carry a billable side effect.
    if isinstance(exc, httpx.ReadTimeout):
        return PaidSubmissionUnconfirmed(
            provider=provider, remote_id=remote_id, attempt=attempt,
            detail=(f"read timeout after the request was sent ({_short(exc)}); "
                    f"the provider may have created and billed the job"))

    # Any 5xx from a submit endpoint is ambiguous for the same reason.
    if status is not None and int(status) >= 500:
        return PaidSubmissionUnconfirmed(
            provider=provider, remote_id=remote_id, attempt=attempt,
            detail=(f"HTTP {status} from the submit endpoint "
                    f"({_short(exc)}); the task may have been created"))

    provable = is_safe_to_retry(exc)
    return PaidSubmissionUnconfirmed(
        provider=provider, remote_id=remote_id, attempt=attempt,
        provably_undelivered=provable,
        detail=f"{type(exc).__name__}: {_short(exc)}")


def _short(exc: BaseException, limit: int = 240) -> str:
    """Exception text, bounded. Full bodies can carry signed URLs."""
    text = str(exc).strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------------------
# polling
# ---------------------------------------------------------------------------


def poll_with_deadline(fetch, *, deadline_seconds: float,
                       sleep=__import__("time").sleep,
                       max_retries: int = MAX_POLL_RETRIES) -> tuple:
    """Poll a remote job to a terminal state, or raise on ambiguity.

    ``fetch`` is called as ``fetch(phase_timeout)`` and must return
    ``(state, payload)``. Unknown states are treated as UNCONFIRMED, never as
    failure: a status this client has never seen is not evidence of failure.

    Returns ``(state, payload)`` where state is a provider string in
    ``{"succeeded", "failed"}`` or the last observed non-terminal value.
    """
    started = datetime.now(UTC)
    consecutive_failures = 0
    last: tuple = ("unknown", {})
    for attempt in range(1, max_retries + 1):
        elapsed = (datetime.now(UTC) - started).total_seconds()
        remaining = deadline_seconds - elapsed
        if remaining <= 0:
            return last
        # A single GET must never outlive half the remaining budget, or it can
        # deliberately overrun the job's total deadline.
        phase_timeout = max(min(remaining / 2.0, MAX_PHASE_TIMEOUT_SECONDS),
                            0.001)
        try:
            state, payload = fetch(phase_timeout)
        except PaidJobError:
            raise
        except Exception:          # a transient poll failure
            consecutive_failures += 1
            if consecutive_failures >= max_retries:
                return last
            sleep(min(RETRY_BASE_SECONDS * consecutive_failures,
                      MAX_PHASE_TIMEOUT_SECONDS))
            continue
        # Any valid response resets the backoff: the service is answering.
        consecutive_failures = 0
        last = (state, payload)
        if state in ("succeeded", "failed", "cancelled"):
            return last
        sleep(min(RETRY_BASE_SECONDS * attempt, MAX_PHASE_TIMEOUT_SECONDS))
    return last


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def download_with_retry(fetch_bytes, *, provider: str = "", remote_id: str = "",
                        url: str = "", attempts: int = 3,
                        sleep=__import__("time").sleep) -> bytes:
    """Fetch an already-paid artifact, retrying the FETCH only.

    Re-generating the remote task costs money again, so download jitter must be
    absorbed by bounded retries against the SAME url. If every attempt fails the
    job still succeeded remotely: raise
    :class:`PaidArtifactUndownloadable` carrying the recovery id.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            data = fetch_bytes()
            if not data:
                raise ValueError("provider returned an empty body")
            return data
        except Exception as exc:          # noqa: BLE001 - deliberately broad
            last = exc
            if attempt < attempts:
                sleep(RETRY_BASE_SECONDS * attempt)
    raise PaidArtifactUndownloadable(
        provider=provider, remote_id=remote_id, url=url,
        detail=f"download failed after {attempts} attempts: {_short(last or Exception())}")


# ---------------------------------------------------------------------------
# persistence helper
# ---------------------------------------------------------------------------


@dataclass
class SubmissionRecord:
    """What to persist for a paid submission, so it survives a crash."""

    workspace_id: str
    provider: str
    state: SubmissionState = SubmissionState.PREPARED
    remote_id: str = ""
    idempotency_key: str = ""
    detail: str = ""
    attempts: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def may_resubmit(self) -> bool:
        """False the moment a resubmit could cost a second payment."""
        return self.state not in NO_RESUBMIT

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "state": str(self.state),
            "remote_id": self.remote_id,
            "idempotency_key": self.idempotency_key,
            "detail": self.detail,
            "attempts": self.attempts,
            "may_resubmit": self.may_resubmit,
            "updated_at": self.updated_at.isoformat(),
        }


def record_submission(record: SubmissionRecord, outcome: BaseException | None
                      ) -> SubmissionRecord:
    """Advance a record from a submit attempt's outcome.

    ``outcome is None`` means the provider returned a durable id, so the state
    becomes ``REMOTE_ID_CONFIRMED``. Any exception is classified rather than
    guessed.
    """
    record.attempts += 1
    record.updated_at = datetime.now(UTC)
    if outcome is None:
        record.state = SubmissionState.REMOTE_ID_CONFIRMED
        return record
    if isinstance(outcome, PaidJobRejected):
        record.state = SubmissionState.FAILED
        record.detail = str(outcome)
        return record
    if isinstance(outcome, PaidSubmissionUnconfirmed):
        record.state = SubmissionState.SUBMISSION_UNKNOWN
        record.detail = str(outcome)
        if outcome.remote_id:
            record.remote_id = outcome.remote_id
        return record
    classified = classify_submit_exception(
        outcome, provider=record.provider, remote_id=record.remote_id,
        attempt=record.attempts)
    record.state = (SubmissionState.FAILED
                    if isinstance(classified, PaidJobRejected)
                    else SubmissionState.SUBMISSION_UNKNOWN)
    record.detail = str(classified)
    return record
