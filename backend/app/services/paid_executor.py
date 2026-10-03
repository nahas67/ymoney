"""The one paid-submission path every billable provider shares.

Work 15.6 audited the billable providers and found the same missing safety
logic reimplemented (or, more often, omitted) in each one. The audit's own
numbers: 2 of roughly 20 billable call sites used the contract, one row
claimed a paid engine was free, and a lost response could be re-POSTed up to
four times by the generic job runner.

This module is the shared executor. It is deliberately NOT a generic
abstraction that hides provider semantics: a provider keeps its own request
building and response parsing, and supplies only a small adapter. What lives
here is the part that must not be re-derived per provider, because getting it
wrong costs money:

    budget check  ->  record attempt  ->  submit ONCE
                  ->  reconcile      ->  record cost (or unknown exposure)

The invariant, stated once:

    billable submit
      -> was acceptance definitely known?
           yes                  -> continue/poll the SAME remote job
           definitely rejected  -> the caller's safe-retry policy applies
           unknown              -> SUBMISSION_UNKNOWN, DO NOT RESUBMIT

Three design choices worth defending:

**The attempt is persisted BEFORE the submit.** If the process dies between the
outbound request and the response, the row is already there. A row written
afterwards would lose exactly the evidence that matters.

**A blind retry is refused, not merely discouraged.** :meth:`execute` submits
once. Re-entry with a record whose state is in :data:`NO_RESUBMIT` raises
instead of sending a second billable request. The retry that IS allowed --
polling, fetching a result, downloading bytes -- goes through
:meth:`poll_remote` / :meth:`download`, which never resubmit.

**Unknown cost is a first-class value.** ``CostOutcome.UNKNOWN_EXPOSURE`` is
recorded rather than ``0.0``. A provider that billed us and whose response we
lost must not appear in the ledger as free.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from loguru import logger

from app.services.paid_jobs import (
    NO_RESUBMIT,
    OUTCOME_KNOWN,
    PaidArtifactUndownloadable,
    PaidJobError,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionState,
    classify_submit_exception,
    download_with_retry,
    is_safe_to_retry,
    poll_with_deadline,
)

__all__ = [
    "AmbiguousSubmission",
    "CostOutcome",
    "IdempotencySupport",
    "PaidProviderExecutor",
    "PaidSubmission",
    "Reconciliation",
    "RemoteSubmission",
    "RetrySafety",
    "RetryVerdict",
]


class IdempotencySupport(StrEnum):
    """Whether the VENDOR officially accepts a deduplication key.

    Only ``SUPPORTED`` may send a header. Inventing one wastes a round trip and
    teaches the reader that the header means something when it does not.
    """

    SUPPORTED = "IDEMPOTENCY_SUPPORTED"
    UNSUPPORTED = "IDEMPOTENCY_UNSUPPORTED"
    UNVERIFIED = "IDEMPOTENCY_UNVERIFIED"


class RetrySafety(StrEnum):
    """Whether re-sending the request can incur a SECOND charge."""

    SAFE = "RETRY_IF_CONFIRMED_SAFE"
    UNSAFE = "DO_NOT_RESUBMIT"


class RetryVerdict(StrEnum):
    """What a caller should do about a failed billable submission."""

    RETRY = "RETRY"
    RECONCILE = "RECONCILE"
    DO_NOT_RETRY = "DO_NOT_RETRY"


class CostOutcome(StrEnum):
    """What the ledger may say about a billable operation's cost.

    ``UNKNOWN_EXPOSURE`` is the important one. Recording ``$0`` because YMONEY
    lost the response is how a real charge disappears from the books; the
    money may well have been spent.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    ACTUAL = "ACTUAL"
    ESTIMATED = "ESTIMATED"
    UNKNOWN_EXPOSURE = "UNKNOWN_EXPOSURE"


class Reconciliation(StrEnum):
    """The operator-facing remedy for an incident."""

    RECONCILE = "RECONCILE"
    RETRY_IF_CONFIRMED_SAFE = "RETRY_IF_CONFIRMED_SAFE"
    MARK_FAILED = "MARK_FAILED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


@dataclass
class CostRecord:
    """What the ledger is told about one billable operation."""

    outcome: CostOutcome = CostOutcome.NOT_APPLICABLE
    estimated: float = 0.0
    actual: float | None = None
    currency: str = "USD"

    @property
    def ledger_value(self) -> float | None:
        """The number to book, or ``None`` when exposure is unknown.

        ``None`` is meaningful: it says "money may have been spent and we do not
        know how much", which is categorically different from zero.
        """
        if self.outcome is CostOutcome.ACTUAL:
            return float(self.actual or 0.0)
        if self.outcome is CostOutcome.ESTIMATED:
            return float(self.estimated or 0.0)
        if self.outcome is CostOutcome.UNKNOWN_EXPOSURE:
            return None
        return None

    def to_dict(self) -> dict:
        return {
            "outcome": str(self.outcome),
            "estimated": round(float(self.estimated or 0.0), 6),
            "actual": None if self.actual is None else round(float(self.actual), 6),
            "ledger_value": self.ledger_value,
            "unknown_exposure": self.outcome is CostOutcome.UNKNOWN_EXPOSURE,
            "currency": self.currency,
        }


@dataclass
class PaidSubmission:
    """The persisted record of one billable attempt.

    This is the evidence the work order requires: enough to decide, after the
    fact and possibly after a crash, whether re-sending is safe.
    """

    workspace_id: str
    provider: str
    operation: str
    submission_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: SubmissionState = SubmissionState.PREPARED
    remote_id: str = ""
    idempotency_key: str = ""
    idempotency_support: IdempotencySupport = IdempotencySupport.UNVERIFIED
    retry_safety: RetrySafety = RetrySafety.UNSAFE
    reconciliation: Reconciliation = Reconciliation.RECONCILE
    attempts: int = 0
    estimated_cost: float = 0.0
    cost: CostRecord = field(default_factory=CostRecord)
    detail: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    @property
    def may_resubmit(self) -> bool:
        """False the instant a re-send could cost a second payment."""
        return self.state not in NO_RESUBMIT

    @property
    def exposure_unknown(self) -> bool:
        return self.cost.outcome is CostOutcome.UNKNOWN_EXPOSURE

    def to_dict(self) -> dict:
        return {
            "submission_id": self.submission_id,
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "operation": self.operation,
            "state": str(self.state),
            "remote_id": self.remote_id,
            "idempotency_key": self.idempotency_key,
            "idempotency_support": str(self.idempotency_support),
            "retry_safety": str(self.retry_safety),
            "recommended_action": str(self.reconciliation),
            "attempts": self.attempts,
            "may_resubmit": self.may_resubmit,
            "detail": self.detail,
            "cost": self.cost.to_dict(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class AmbiguousSubmission(PaidJobError):
    """A billable submit whose acceptance could not be established.

    Carries the submission record so an operator has the provider, operation,
    timestamp and any remote id in one object.

    This is raised, never retried internally. A caller that wants to proceed
    anyway must make an explicit, recorded policy decision.
    """

    def __init__(self, submission: PaidSubmission, detail: str = ""):
        self.submission = submission
        message = detail or submission.detail or "billable submission unconfirmed"
        super().__init__(
            f"{submission.provider}.{submission.operation} "
            f"[{submission.submission_id}] is {submission.state}: {message}. "
            "The provider may have accepted and billed this request. Do not "
            "resubmit; reconcile first."
        )

    @property
    def submission_id(self) -> str:
        return self.submission.submission_id

    @property
    def recommended_action(self) -> Reconciliation:
        return self.submission.reconciliation


@dataclass
class RemoteSubmission:
    """What a provider adapter returns when a submit is accepted."""

    remote_id: str
    #: Providers that stream bytes inline (no job to poll) return the artifact
    #: path here and are polled out of existence.
    artifact_path: str = ""
    state: SubmissionState = SubmissionState.REMOTE_ID_CONFIRMED
    raw: dict = field(default_factory=dict)


class PaidProviderExecutor:
    """Shared safety wrapper for one billable operation.

    A provider supplies a ``submit_fn`` that performs exactly one outbound
    billable request and returns a :class:`RemoteSubmission`, plus a
    ``persist_fn`` used to write the submission record. Everything that must
    not vary per provider -- budget, single-submit, classification, cost --
    lives here.
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        provider: str,
        operation: str,
        persist: Callable[[PaidSubmission], None] | None = None,
        audit: Callable[[PaidSubmission, str], None] | None = None,
        cost_hook: Callable[[PaidSubmission], None] | None = None,
        submit_budget: Callable[[], None] | None = None,
        idempotency: IdempotencySupport = IdempotencySupport.UNVERIFIED,
        reconciliation: Reconciliation = Reconciliation.RECONCILE,
    ) -> None:
        self.workspace_id = workspace_id
        self.provider = provider
        self.operation = operation
        self._persist = persist
        self._audit = audit
        self._cost_hook = cost_hook
        self._submit_budget = submit_budget
        self.idempotency = idempotency
        self.reconciliation = reconciliation

    # -- budget ----------------------------------------------------------

    def check_budget(self, estimated_cost: float = 0.0) -> None:
        """Assert the workspace may spend, BEFORE anything is submitted.

        Delegated to the caller's gate so this module never owns budget policy.
        The ordering is the point: a check after the submit is worthless.
        """
        if self._submit_budget is None:
            logger.debug(
                "paid submit {} has no budget gate; spend will not be "
                "pre-authorised", f"{self.provider}.{self.operation}")
            return
        self._submit_budget()

    # -- submit ----------------------------------------------------------

    def execute(
        self,
        submit_fn: Callable[[str], RemoteSubmission],
        *,
        estimated_cost: float = 0.0,
        idempotency_key: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> RemoteSubmission:
        """Submit ONCE, classifying the outcome.

        On ambiguity this raises :class:`AmbiguousSubmission` after persisting
        ``SUBMISSION_UNKNOWN``. It never resubmits.
        """
        record = PaidSubmission(
            workspace_id=self.workspace_id,
            provider=self.provider,
            operation=self.operation,
            idempotency_key=idempotency_key,
            idempotency_support=self.idempotency,
            estimated_cost=float(estimated_cost or 0.0),
        )
        if estimated_cost > 0:
            record.cost = CostRecord(outcome=CostOutcome.ESTIMATED,
                                     estimated=float(estimated_cost))

        # 1. Budget, before any spend.
        self.check_budget(estimated_cost)

        if should_cancel is not None and should_cancel():
            record.state = SubmissionState.CANCELLED
            record.reconciliation = Reconciliation.MARK_FAILED
            record.detail = "cancelled before submission; nothing was sent"
            record.touch()
            self._save(record)
            self._emit(record, "cancelled")
            raise PaidJobRejected(provider=self.provider,
                                  detail="cancelled before submission")

        # 2. Record the ATTEMPT before sending, so a crash mid-flight still
        #    leaves evidence that a billable request was made.
        record.state = SubmissionState.SUBMISSION_ATTEMPTED
        record.attempts = 1
        record.touch()
        self._save(record)
        self._emit(record, "attempted")

        # 3. Exactly one outbound request.
        try:
            handle = submit_fn(record.idempotency_key)
        except BaseException as exc:              # noqa: BLE001 - classified
            classified = classify_submit_exception(
                exc, provider=self.provider,
                remote_id=record.remote_id, attempt=record.attempts)
            self._absorb_failure(record, classified)
            self._save(record)
            self._emit(record, "ambiguous" if isinstance(
                classified, PaidSubmissionUnconfirmed) else "rejected")
            if isinstance(classified, PaidSubmissionUnconfirmed):
                raise AmbiguousSubmission(record) from exc
            raise classified from exc

        # 4. Acceptance is known: persist the remote id immediately.
        record.remote_id = str(getattr(handle, "remote_id", "") or "")
        record.state = (SubmissionState.SUCCEEDED
                        if getattr(handle, "artifact_path", "")
                        else SubmissionState.REMOTE_ID_CONFIRMED)
        record.detail = f"accepted by {self.provider}"
        record.touch()
        self._save(record)
        self._emit(record, "accepted")
        return handle

    # -- post-accept phases: never resubmit ------------------------------

    def poll_remote(self, fetch, *, deadline_seconds: float):
        """Poll the SAME remote job. Retry is bounded and safe."""
        return poll_with_deadline(fetch, deadline_seconds=deadline_seconds)

    def download(self, fetch_bytes, *, remote_id: str = "", url: str = "",
                 attempts: int = 3) -> bytes:
        """Fetch an already-paid artifact. Never recreates the job."""
        return download_with_retry(fetch_bytes, provider=self.provider,
                                   remote_id=remote_id, url=url,
                                   attempts=attempts)

    def mark_succeeded(self, record: PaidSubmission, *,
                       actual_cost: float | None = None) -> PaidSubmission:
        record.state = SubmissionState.SUCCEEDED
        record.reconciliation = Reconciliation.MARK_FAILED
        if actual_cost is not None:
            record.cost = CostRecord(outcome=CostOutcome.ACTUAL,
                                     estimated=record.estimated_cost,
                                     actual=float(actual_cost))
        record.touch()
        self._save(record)
        self._emit(record, "succeeded")
        if self._cost_hook is not None:
            self._cost_hook(record)
        return record

    def mark_cancelled(self, record: PaidSubmission, *, detail: str = ""
                       ) -> PaidSubmission:
        """The provider acknowledged a cancellation: the outcome IS known."""
        record.state = SubmissionState.CANCELLED
        record.reconciliation = Reconciliation.MARK_FAILED
        record.detail = detail or "cancelled by provider"
        record.touch()
        self._save(record)
        self._emit(record, "cancelled")
        return record

    # -- classification --------------------------------------------------

    def _absorb_failure(self, record: PaidSubmission,
                        classified: PaidJobError) -> None:
        """Fold a classified failure into the record's money-relevant fields."""
        if isinstance(classified, PaidJobRejected):
            record.state = SubmissionState.FAILED
            record.reconciliation = Reconciliation.RETRY_IF_CONFIRMED_SAFE
            record.detail = str(classified)
            if record.estimated_cost > 0:
                record.cost = CostRecord(outcome=CostOutcome.ESTIMATED,
                                         estimated=record.estimated_cost)
            return

        # Ambiguous: money may have been spent and the amount is unknown.
        record.state = SubmissionState.SUBMISSION_UNKNOWN
        record.retry_safety = RetrySafety.UNSAFE
        record.reconciliation = self.reconciliation
        record.detail = str(classified)
        provably_undelivered = False
        if isinstance(classified, PaidSubmissionUnconfirmed):
            if classified.remote_id:
                record.remote_id = classified.remote_id
            # A connect failure PROVES nothing was delivered, so this one
            # ambiguous state is the rare case a retry cannot double-charge.
            provably_undelivered = classified.provably_undelivered
            if provably_undelivered:
                record.retry_safety = RetrySafety.SAFE
                record.reconciliation = (
                    Reconciliation.RETRY_IF_CONFIRMED_SAFE)
                record.detail += (" Connection never established, so a retry "
                                  "cannot incur a second charge.")

        if provably_undelivered:
            # Nothing was sent, so nothing was billed, and that is KNOWN.
            # Claiming an unknown exposure here would be as wrong as booking a
            # fabricated $0: it would put a phantom charge on the books for an
            # attempt that provably never reached the provider.
            record.cost = CostRecord(outcome=CostOutcome.ESTIMATED,
                                     estimated=0.0)
        else:
            record.cost = CostRecord(
                outcome=CostOutcome.UNKNOWN_EXPOSURE,
                estimated=record.estimated_cost)

    # -- plumbing --------------------------------------------------------

    def _save(self, record: PaidSubmission) -> None:
        if self._persist is not None:
            self._persist(record)

    def _emit(self, record: PaidSubmission, phase: str) -> None:
        logger.info(
            "paid submit {provider}.{op} [{sid}] {phase} state={state} "
            "remote_id={rid} exposure={exposure}",
            provider=record.provider, op=record.operation,
            sid=record.submission_id, phase=phase, state=str(record.state),
            rid=record.remote_id or "-",
            exposure=(str(record.cost.outcome)
                      if record.state in (SubmissionState.SUBMISSION_UNKNOWN,
                                          SubmissionState.PROCESSING)
                      and record.cost.outcome
                      is CostOutcome.UNKNOWN_EXPOSURE
                      else "none"))
        if self._audit is not None:
            self._audit(record, phase)


def verdict_for(record: PaidSubmission) -> RetryVerdict:
    """What a caller should do next, derived from the record alone.

    Kept as a free function so an API layer, a UI, or a CLI can all answer the
    same question identically instead of each inventing a rule.
    """
    if record.state is SubmissionState.SUBMISSION_UNKNOWN:
        if record.retry_safety is RetrySafety.SAFE:
            return RetryVerdict.RETRY
        return RetryVerdict.RECONCILE
    if record.state in OUTCOME_KNOWN:
        return RetryVerdict.RETRY
    if record.state in (SubmissionState.PROCESSING, SubmissionState.SUCCEEDED,
                        SubmissionState.REMOTE_ID_CONFIRMED):
        return RetryVerdict.DO_NOT_RETRY
    return RetryVerdict.RETRY


def reconcile_submission(record: PaidSubmission, action: Reconciliation, *,
                         operator: str = "", note: str = "") -> PaidSubmission:
    """Apply an operator decision to an ambiguous submission.

    Always audited. A manual override that leaves no trace is how a real
    duplicate charge becomes unexplainable three weeks later.
    """
    if not operator:
        raise ValueError("a reconciliation decision must name an operator")
    if action is not Reconciliation.MANUAL_OVERRIDE and \
            action not in (Reconciliation.RECONCILE,
                           Reconciliation.RETRY_IF_CONFIRMED_SAFE,
                           Reconciliation.MARK_FAILED):
        raise ValueError(f"unsupported reconciliation action: {action}")

    record.reconciliation = action
    record.touch()
    if action is Reconciliation.MARK_FAILED:
        record.state = SubmissionState.FAILED
    elif action is Reconciliation.RETRY_IF_CONFIRMED_SAFE:
        record.retry_safety = RetrySafety.SAFE
    # RECONCILE and MANUAL_OVERRIDE deliberately leave ``state`` untouched:
    # the submission is still unconfirmed, and pretending otherwise is the bug
    # this whole contract exists to prevent.

    audit_note = (f"operator={operator} action={action} note={note}".strip()
                  + f" state={record.state} exposure={record.cost.outcome}")
    record.detail = f"{record.detail} | {audit_note}".strip(" |")
    logger.warning("paid submit reconciliation: %s", audit_note)
    return record


def monotonic_age(record: PaidSubmission) -> float:
    """Seconds since the attempt was recorded; used for incident sorting."""
    return max(time.monotonic(), 0.0)


__all__ += ["reconcile_submission", "verdict_for", "CostRecord", "monotonic_age"]

# Re-exported so callers need one import for the contract, not three.
__all__ += ["PaidSubmissionUnconfirmed", "PaidJobRejected",
            "PaidArtifactUndownloadable", "SubmissionState", "is_safe_to_retry"]