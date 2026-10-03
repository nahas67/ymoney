"""Billable chat-completion safety: the LLM lane of the paid-submission contract.

Work 15.6 audited the billable providers and found
``app.providers.llm.complete`` wrapping a metered POST in a
``models_to_try`` x ``attempts`` double loop whose ``except Exception``
treated a read timeout exactly like a clean 4xx. One call could therefore
leave up to FOUR billable requests on the wire, and a request the provider
had already generated and billed was indistinguishable from a request that
was refused before it did any work. ``complete_json`` doubled that again.

This module is the LLM adapter for
:class:`~app.services.paid_executor.PaidProviderExecutor`. It owns three
decisions and delegates everything else:

* **what counts as a candidate** (:func:`build_candidates`) -- the same
  ordered list the old double loop produced, so nothing regresses for the
  one case that is still safe to walk;
* **whether the loop may advance** -- exclusively via the shared
  :func:`~app.services.paid_executor.verdict_for`. A candidate is tried only
  while the previous attempt is a PROVEN safe retry. Ambiguity is terminal;
* **what a caller may do about it** (:class:`FallbackPolicy`) -- an explicit,
  auditable opt-in, because falling back after an ambiguous completion can
  spend a second charge.

The honest limitation, stated once and made queryable
(:func:`llm_reconciliation_capability`):

    A chat completion is NOT an asynchronous job.

There is no remote id, no poll endpoint and no "fetch this result" call. When
a read timeout loses the response, the shared contract's
``Reconciliation.RECONCILE`` remedy -- "go look up the job by its id" -- is
**impossible here**, not pending. Inventing a synthetic submission id and
presenting it as a recovery handle would be the exact class of lie this
contract exists to prevent: an operator would go looking for a job at the
vendor that does not exist. So the capability is reported as
``UNRECONCILABLE``, the handle is ``None``, and the stated remedy is the only
thing that is actually true -- treat the spend as an unknown exposure and do
not re-send.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger

from app.engine.intelligence.sanitize import redact_error_text, strip_think_tags
from app.services import cost
from app.services.paid_executor import (
    AmbiguousSubmission,
    CostOutcome,
    CostRecord,
    IdempotencySupport,
    PaidJobError,
    PaidJobRejected,
    PaidProviderExecutor,
    PaidSubmission,
    Reconciliation,
    RemoteSubmission,
    RetryVerdict,
    SubmissionState,
    verdict_for,
)
from app.services.paid_provider import (
    ActorAuthority,
    OwnerlessSpendRefused,
    SpendAuthority,
    coerce_authority,
    resolve_ownership,
)

__all__ = [
    "PROVIDER",
    "CompletionCandidate",
    "CompletionOutcome",
    "FallbackDecision",
    "FallbackPolicy",
    "FallbackVerdict",
    "LLMCompletionBudgetRefused",
    "LLMCompletionCancelled",
    "LLMCompletionExhausted",
    "LLMCompletionNotAccepted",
    "LLMCompletionPaidError",
    "LLMCompletionUnusable",
    "LLMCompletionUnknown",
    "LLMReconciliationCapability",
    "LegOutcome",
    "ReconciliationAvailability",
    "SpendAuthorization",
    "build_candidates",
    "estimate_request_cost",
    "llm_reconciliation_capability",
    "run_completion",
]

#: The abstract vendor slot. Never a vendor name: the gateway is whatever
#: ``llm.base_url`` points at.
PROVIDER = "openai_compatible_llm"

#: What the accepted artifact actually is. A completion is returned IN the
#: response body, so there is nothing to download afterwards -- but saying so
#: with a path-shaped string would imply a file. Recorded verbatim on the
#: submission record and surfaced in the ``paid.submission`` event so an
#: operator reading the ledger can see the difference between "artifact at
#: <url>" and "artifact was the body we lost".
INLINE_ARTIFACT = "inline://chat/completions (returned in the response body)"

#: Provider-side tokenisation is not available before the call, so the prompt
#: side of the pre-spend estimate is a deliberately coarse bound rather than a
#: measurement. Rounded UP, because an optimistic budget figure is worse than
#: a coarse one. Every place this number is recorded says ``is_estimate``.
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# B. the no-remote-id limitation, explicit and queryable
# ---------------------------------------------------------------------------


class ReconciliationAvailability(StrEnum):
    """Whether a recovery handle exists for a lost response.

    ``RECONCILE`` means "go and find the job by its id". ``UNRECONCILABLE``
    means that remedy does not exist for this operation and no amount of
    waiting will produce it -- which is a categorically different fact from
    "not yet", and must not be collapsed into it.
    """

    RECONCILE = "RECONCILE"
    UNRECONCILABLE = "UNRECONCILABLE"


@dataclass(frozen=True)
class LLMReconciliationCapability:
    """What a higher layer must know BEFORE it retries or reports an incident.

    Frozen on purpose: this is a statement about the protocol, not a
    per-request fact, and mutating it mid-incident is how a lost completion
    becomes a duplicate charge.
    """

    operation: str
    availability: ReconciliationAvailability = ReconciliationAvailability.UNRECONCILABLE
    remote_id_field: str = ""
    reason: str = ""
    remedy: str = ""
    idempotency_header: str = ""

    @property
    def is_reconcilable(self) -> bool:
        return self.availability is ReconciliationAvailability.RECONCILE

    @property
    def handle(self) -> None:
        """The recovery handle. Always ``None`` for a chat completion.

        A property returning a constant looks odd; it is here so a caller
        written against the general contract gets ``None`` instead of an
        ``AttributeError`` when it asks for the handle it would use on an
        async job.
        """
        return None

    def explain(self) -> str:
        return (f"{self.operation}: reconciliation={self.availability}; "
                f"remote_id_field={self.remote_id_field or '<none>'}; "
                f"idempotency={self.idempotency_header or '<none>'}; "
                f"remedy={self.remedy}. {self.reason}")

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "reconcilable": self.is_reconcilable,
            "availability": str(self.availability),
            "remote_id": None,
            "remote_id_field": self.remote_id_field or None,
            "idempotency_header": self.idempotency_header or None,
            "remedy": self.remedy,
            "reason": self.reason,
        }


_NO_REMOTE_ID = (
    "OpenAI-compatible chat completions are synchronous: the generated text "
    "IS the response body. The protocol returns no request id, no job id and "
    "no status endpoint, so a lost response cannot be looked up, polled or "
    "re-fetched. It also documents no idempotency key, so a re-send is "
    "indistinguishable from a new request and is billed as one."
)


def llm_reconciliation_capability(operation: str = "complete") -> LLMReconciliationCapability:
    """Report whether ``operation`` can be reconciled after a lost response.

    The one query a higher layer needs: *is reconciliation possible, or only
    pending?* For an LLM completion the answer is always
    :data:`ReconciliationAvailability.UNRECONCILABLE`, and the honest remedy
    is to book the attempt as an unknown exposure and re-decide the work -- not
    to go hunting for a job that was never created.
    """
    return LLMReconciliationCapability(
        operation=operation,
        availability=ReconciliationAvailability.UNRECONCILABLE,
        reason=_NO_REMOTE_ID,
        remedy=("book the attempt as UNKNOWN_EXPOSURE, do not re-send, and "
                "decide the work again explicitly"),
    )


# ---------------------------------------------------------------------------
# C. the explicit, opt-in fallback policy
# ---------------------------------------------------------------------------


class FallbackDecision(StrEnum):
    ALLOW = "ALLOW_SECOND_CHARGE"
    REFUSE = "REFUSE"


@dataclass(frozen=True)
class FallbackVerdict:
    """The answer a caller asked for, with the money risk stated in it."""

    decision: FallbackDecision
    may_incur_second_charge: bool
    reason: str
    submission_id: str = ""
    remaining_models: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is FallbackDecision.ALLOW

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict:
        return {
            "decision": str(self.decision),
            "may_incur_second_charge": self.may_incur_second_charge,
            "reason": self.reason,
            "submission_id": self.submission_id,
            "remaining_models": list(self.remaining_models),
        }


@dataclass
class FallbackPolicy:
    """May this caller spend a second charge to get an answer?

    The default is :data:`FallbackDecision.REFUSE`, and that default is the
    point: retrying a billable call after an ambiguous response is a decision
    about money, so it is made by a named caller, not inherited from a loop.

    ``allow_ambiguous_fallback`` alone is not enough. An override must also
    name ``approved_by``, and every decision -- allow or refuse -- is appended
    to :attr:`history` and handed to the optional ``audit`` hook, because an
    unexplained second charge is indistinguishable from a bug three weeks
    later.

    This policy is consulted ONLY for a completion that must not be
    auto-retried. A *provably safe* retry (a 4xx rejection, or a connect
    failure that never opened a socket) is not a money decision and is
    applied by the loop without asking.
    """

    allow_ambiguous_fallback: bool = False
    approved_by: str = ""
    note: str = ""
    audit: Callable[[FallbackVerdict], None] | None = None
    history: list[FallbackVerdict] = field(default_factory=list)

    def decide(self, record: PaidSubmission, *,
               remaining_models: tuple[str, ...] = ()) -> FallbackVerdict:
        """Answer one question: may we fall back after this attempt?

        Deliberately a free-standing, side-effect-light decision so a caller
        can ask it WITHOUT issuing a request -- an API layer, a UI or a CLI
        can render the risk before a human clicks.
        """
        if not remaining_models:
            verdict = FallbackVerdict(
                decision=FallbackDecision.REFUSE,
                may_incur_second_charge=True,
                reason=("there is no further candidate to fall back to; the "
                        "only way to get an answer is a new completion"),
                submission_id=record.submission_id,
            )
        elif not self.allow_ambiguous_fallback:
            verdict = FallbackVerdict(
                decision=FallbackDecision.REFUSE,
                may_incur_second_charge=True,
                reason=(
                    f"{llm_reconciliation_capability(record.operation).availability}: "
                    f"the previous completion was submitted and its outcome is "
                    f"not known, so the next candidate may be billed a second "
                    f"time. Default policy is do-not-retry; a caller that "
                    f"accepts that risk must opt in and name an approver"),
                submission_id=record.submission_id,
                remaining_models=remaining_models,
            )
        else:
            verdict = FallbackVerdict(
                decision=FallbackDecision.ALLOW,
                may_incur_second_charge=True,
                reason=(
                    f"explicit override by {self.approved_by}: falling back to "
                    f"{', '.join(remaining_models)} may bill a second "
                    f"completion for submission {record.submission_id}"
                    + (f" ({self.note})" if self.note else "")),
                submission_id=record.submission_id,
                remaining_models=remaining_models,
            )
        self.history.append(verdict)
        if verdict.allowed and not self.approved_by:
            # Unreachable through run_completion (checked first), but a direct
            # caller must not be able to authorise spend anonymously.
            raise ValueError(
                "an ambiguous-fallback override must name approved_by; an "
                "unattributed second charge cannot be explained later")
        if verdict.allowed:
            logger.warning("LLM ambiguous fallback ALLOWED: %s", verdict.reason)
        if self.audit is not None:
            self.audit(verdict)
        return verdict

    def __post_init__(self) -> None:
        if self.allow_ambiguous_fallback and not self.approved_by:
            raise ValueError(
                "allow_ambiguous_fallback=True requires approved_by: a policy "
                "that permits a second charge must say who permitted it")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class LLMCompletionPaidError(PaidJobError):
    """A billable completion that produced no usable text.

    ``kind`` says which failure it was, ``submission`` is the record the
    executor persisted before the request left. Subclasses are NOT all the
    same thing: only some of them mean the money might be gone.
    """

    kind: str = ""
    detail: str = ""
    submission: PaidSubmission | None = None

    @property
    def outcome(self) -> LegOutcome:
        """The money fact, in the five words a caller acts on."""
        return LegOutcome.for_kind(self.kind)


class LLMCompletionNotAccepted(AmbiguousSubmission, LLMCompletionPaidError):
    """A billable completion that must not be re-sent. Never auto-retried."""


class LLMCompletionUnknown(LLMCompletionNotAccepted):
    """AMBIGUOUS: the request was sent and we do not know if it was served.

    A read timeout, a 5xx from the gateway, a dropped connection. The
    provider may have generated and billed the completion, and because a chat
    completion has no remote id there is no way to find out afterwards.
    """

    kind = "AMBIGUOUS"

    def __init__(self, submission: PaidSubmission, detail: str = ""):
        super().__init__(submission, detail)
        self.detail = detail or submission.detail


class LLMCompletionUnusable(LLMCompletionNotAccepted):
    """BILLED BUT UNUSABLE: a 2xx arrived, so the money is spent, and the body
    is not content we can use (no choices, an empty message, a scratchpad with
    no answer, an undecodable body).

    Distinct from ambiguity on purpose: here the outcome IS known -- it was
    accepted and metered. A re-send is therefore not a coin-flip, it is a
    certain second charge for a second generation. If the response reported
    token usage the amount is still priced and booked; only a body that never
    parsed leaves the exposure unknown.
    """

    kind = "BILLED_BUT_UNUSABLE"

    def __init__(self, submission: PaidSubmission, detail: str = ""):
        super().__init__(submission, detail)
        self.detail = detail or submission.detail


class LLMCompletionExhausted(LLMCompletionPaidError):
    """Every candidate was PROVABLY safe to try and all of them failed.

    Nothing was billed: each attempt was either refused with a 4xx or never
    reached a socket. This is the one exhaustion that is genuinely
    retryable, and it is reported as its own kind precisely so it cannot be
    mistaken for an unknown exposure.
    """

    kind = "EXHAUSTED_ON_PROVEN_SAFE_FAILURES"

    def __init__(self, submission: PaidSubmission, detail: str = ""):
        self.submission = submission
        self.detail = detail or submission.detail
        super().__init__(
            f"{PROVIDER}.{submission.operation} "
            f"[{submission.submission_id}] failed after "
            f"{submission.attempts} attempt(s); every attempt was a definitive "
            f"rejection or a connection that never opened, so nothing was "
            f"billed: {self.detail}")


class LLMCompletionBudgetRefused(LLMCompletionPaidError):
    """The pre-spend gate refused the leg. NOTHING was sent.

    Not a provider failure at all, which is why it gets its own kind: the
    ledger has no new row, no request left the process, and the work can be
    retried later or after a budget decision. Silently continuing past it would
    make the gate advisory, which is the state Work 15.7 §11 removed.
    """

    kind = "BUDGET_REFUSED"

    def __init__(self, detail: str, *, estimated_usd: float = 0.0,
                 submission: PaidSubmission | None = None) -> None:
        self.submission = submission
        self.detail = detail
        self.estimated_usd = float(estimated_usd or 0.0)
        super().__init__(
            f"{PROVIDER} refused an estimated ${self.estimated_usd:.4f} "
            f"before sending anything: {detail}")


class LLMCompletionCancelled(LLMCompletionPaidError):
    """Cancelled before the request left. The outcome is known and nothing
    was billed, so this is not an ambiguity and must not be reported as one.
    """

    kind = "CANCELLED"

    def __init__(self, submission: PaidSubmission, detail: str = ""):
        self.submission = submission
        self.detail = detail or submission.detail
        super().__init__(
            f"{PROVIDER}.{submission.operation} "
            f"[{submission.submission_id}] cancelled before submission; "
            f"nothing was sent and nothing was billed: {self.detail}")


# ---------------------------------------------------------------------------
# A. candidates, the guarded loop, and the money
# ---------------------------------------------------------------------------


class LegOutcome(StrEnum):
    """What one billable leg did, in the vocabulary a caller acts on.

    Deliberately not the same words as :class:`SubmissionState`: a state says
    where the request got to, an outcome says whether the money is gone. The
    five below are the complete set, and each one has exactly one remedy.

    ``for_kind`` is the one place the ``LLMCompletionPaidError.kind`` strings
    are mapped, so a caller asking "was anything billed?" gets the same answer
    whichever error object it was handed.
    """

    SUCCEEDED = "SUCCEEDED"
    #: The provider refused the request itself (4xx). Nothing billed.
    KNOWN_REJECTION = "KNOWN_REJECTION"
    #: Refused before submission, or failed in a way that proves nothing was
    #: billed. Safe to run again; still worth recording.
    FAILED_SAFE = "FAILED_SAFE"
    #: Delivered, outcome unknown -- or accepted, metered and unusable. Both
    #: mean "a re-send is a second charge", which is why they share a value.
    #: UNRECONCILABLE for a chat completion, never retried automatically.
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    #: We stopped before sending. Nothing was billed.
    CANCELLED = "CANCELLED"

    @classmethod
    def for_kind(cls, kind: str) -> LegOutcome:
        """The money fact behind an ``LLMCompletionPaidError.kind``."""
        return {
            "AMBIGUOUS": cls.SUBMISSION_UNKNOWN,
            "BILLED_BUT_UNUSABLE": cls.SUBMISSION_UNKNOWN,
            "EXHAUSTED_ON_PROVEN_SAFE_FAILURES": cls.FAILED_SAFE,
            # A budget refusal and a cancellation are the same money fact:
            # nothing was sent. The error's ``kind`` still says which it was.
            "BUDGET_REFUSED": cls.CANCELLED,
            "CANCELLED": cls.CANCELLED,
            "": cls.SUBMISSION_UNKNOWN,
        }.get(str(kind), cls.SUBMISSION_UNKNOWN)


@dataclass(frozen=True)
class CompletionCandidate:
    """One billable POST, described before it is sent."""

    model: str
    body: dict
    label: str

    @property
    def cost_hint(self) -> str:
        return f"{self.model}[{self.label}]"


@dataclass(frozen=True)
class CompletionOutcome:
    """A completion that was actually generated, and what it cost to get it.

    ``attempts`` and ``models_tried`` are part of the result on purpose: a
    caller that wants to know whether an answer cost one generation or three
    should not have to guess by reading a log.
    """

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    attempts: int
    models_tried: tuple[str, ...]
    submission_id: str
    is_estimate: bool = False
    outcome: LegOutcome = LegOutcome.SUCCEEDED


def build_candidates(model: str, body: dict, *, fallback_model: str = "",
                     json_mode: bool = False) -> list[CompletionCandidate]:
    """The ordered list of billable POSTs, model-major.

    Identical in shape to the old double loop -- primary model, then the same
    model without ``response_format`` (some OpenAI-compatible gateways reject
    the parameter), then the configured fallback model and its degraded
    variant. The difference is that walking this list is no longer free: the
    caller may only advance while the previous attempt is a PROVEN safe
    retry, so the list length is an upper bound on *rejections*, never on
    ambiguous spends.

    ``body`` is copied rather than mutated; the old loop reassigned
    ``body["model"]`` on the caller's dict.
    """
    models = [model] + ([fallback_model]
                        if fallback_model and fallback_model != model else [])
    candidates: list[CompletionCandidate] = []
    for name in models:
        primary = {**body, "model": name}
        candidates.append(CompletionCandidate(
            model=name, body=primary,
            label="primary" if name == model else "fallback-model"))
        if json_mode:
            candidates.append(CompletionCandidate(
                model=name,
                body={k: v for k, v in primary.items() if k != "response_format"},
                label="degraded-body"))
    return candidates


def estimate_request_cost(model: str, body: dict, max_tokens: int) -> float:
    """Pre-spend ESTIMATE for one candidate. Never a measurement.

    The output side is the caller's own ``max_tokens`` cap, which is a real
    bound. The input side is an upper bound on characters per message, because
    the vendor owns the tokenizer and will not tell us before the call.
    """
    chars = sum(len(str(m.get("content", "")))
                for m in body.get("messages", []) if isinstance(m, dict))
    prompt_tokens = -(-chars // _CHARS_PER_TOKEN)
    return float(cost.estimate_llm_cost(model, prompt_tokens, int(max_tokens or 0)))


# ---------------------------------------------------------------------------
# D. the spend permission that must exist BEFORE the request
# ---------------------------------------------------------------------------


@dataclass
class SpendAuthorization:
    """One leg's pre-spend permission: owner -> assert -> reserve.

    The ordering is the contract, and it is the same one every gated sibling
    in this repo uses (``images.py``, ``broll.py``, ``tts.py`` ...):

        resolve owner  ->  assert_can_spend  ->  reserve  ->  record attempt
                      ->  call  ->  reconcile

    A read-only "may I spend?" is not enough. It cannot exclude a concurrent
    spender, and "we checked before we spent" is unverifiable afterwards.
    :func:`app.services.cost.reserve_spend` decides and writes in one
    transaction, which is why this class calls the reserving mode of
    :func:`app.services.cost.assert_can_spend`.

    **Work 15.9 §1: ``enforced=False`` no longer means "carry on".** It used to
    be set silently whenever ``workspace_id`` was empty and the leg then ran
    with nothing reserved: a billable POST whose cost no cap could ever see. A
    missing workspace is now refused by :meth:`authorize` with
    :class:`~app.services.paid_provider.OwnerlessSpendRefused`, and the only
    way to have no tenant is a declared authority --
    :attr:`~app.services.paid_provider.SpendAuthority.SYSTEM_OWNED` (with a
    configured system budget) or
    :attr:`~app.services.paid_provider.SpendAuthority.EXPLICIT_NONBILLABLE`.
    """

    workspace_id: str
    amount_usd: float
    provider: str = PROVIDER
    category: str = "llm"
    reservation: object | None = None
    #: Diagnostic, NOT the gate. ``False`` means this leg's spend is not backed
    #: by a reservation, and :attr:`ownerless_reason` says why. The refusal is
    #: raised by :meth:`resolve_owner`, which is the one place the question
    #: "does this leg have a budget owner?" is answered -- the LLM leg and the
    #: provider leg must never be able to disagree about it.
    enforced: bool = True
    settled: bool = False
    #: Work 15.9 §1. Defaults to the strict reading; see
    #: :mod:`app.services.paid_provider`.
    authority: SpendAuthority = SpendAuthority.WORKSPACE_OWNED
    #: Who asked. Recorded on the ledger row, never a waiver.
    actor: ActorAuthority = ActorAuthority.USER
    #: Why this leg could not be charged, when it could not. Kept so the
    #: refusal names the missing thing instead of a generic "no workspace".
    ownerless_reason: str = ""

    def __post_init__(self) -> None:
        self.authority = coerce_authority(SpendAuthority, self.authority, "spend authority")
        self.actor = coerce_authority(ActorAuthority, self.actor, "actor authority")
        if not (self.workspace_id or "").strip() and \
                self.authority is SpendAuthority.WORKSPACE_OWNED:
            self.enforced = False
            self.ownerless_reason = (
                "workspace_id is empty, so no tenant cap could be enforced or "
                "charged")

    @property
    def entry_id(self) -> str:
        return str(getattr(self.reservation, "entry_id", "") or "")

    def resolve_owner(self):
        """This leg's owner, or a refusal. See :func:`paid_provider.resolve_ownership`.

        Delegates to the one resolver so the LLM leg and the provider leg can
        never disagree about what "system owned" means. Raises
        :class:`~app.services.paid_provider.OwnerlessSpendRefused` -- carrying
        the tenant that was missing rather than a generic message -- so the
        refusal is diagnosable from the log line alone.
        """
        try:
            return resolve_ownership(
                workspace_id=self.workspace_id, provider=self.provider,
                operation="llm_completion", estimated_usd=self.amount_usd,
                authority=self.authority, actor=self.actor)
        except OwnerlessSpendRefused as refused:
            self.enforced = False
            logger.error("llm spend refused: {}", refused)
            raise

    def authorize(self) -> None:
        """Resolve the owner, then assert + reserve. Raises BEFORE anything sends.

        Two refusals, deliberately both fatal:

        * no budget owner (:class:`~app.services.paid_provider.OwnerlessSpendRefused`)
          -- the request would be a real charge against nobody's cap. This used
          to log a warning and proceed unreserved. It is raised by
          :meth:`resolve_owner`, which is why the check happens before any
          ledger work: there is nothing to reserve against.
        * no money (:class:`app.services.cost.BudgetExceededError`) -- the gate
          doing its job.

        An *infrastructure* failure (no database, no ledger) still logs and lets
        the leg proceed, because taking content production down is not the same
        decision as declining to spend money, and pretending a broken ledger is
        a spendable balance would be the worse lie. That is a ledger outage, and
        it is deliberately NOT reachable by "I forgot the workspace id".
        """
        if self.reservation is not None:
            return
        owner = self.resolve_owner()
        if not owner.billable:
            logger.debug(
                "llm spend of about $%.4f is EXPLICIT_NONBILLABLE (actor=%s); "
                "no reservation written", float(self.amount_usd or 0.0),
                str(self.actor))
            return
        try:
            self.reservation = cost.assert_can_spend(
                owner.charged_workspace_id, float(self.amount_usd or 0.0),
                category=self.category, provider=self.provider, reserve=True)
        except (cost.BudgetExceededError, cost.RateLimitExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 - ledger outage is not a verdict
            self.enforced = False
            logger.error(
                "llm budget gate could not be enforced (%s: %s); this leg runs "
                "unreserved and the exposure is unaccounted", type(exc).__name__, exc)

    def settle(self, actual_usd: float) -> bool:
        """Replace the estimate with the real amount, on the same ledger row.

        Returns whether a row was settled. A reservation plus a separate
        ``track_cost`` would bill the same completion twice, so the booking
        path checks :attr:`reservation` before writing anything.
        """
        if self.reservation is None:
            return False
        try:
            cost.settle_reservation(self.entry_id, actual_usd)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not lose text
            logger.warning("llm reservation %s not settled: %s: %s",
                           self.entry_id, type(exc).__name__, exc)
            return False
        self.settled = True
        return True

    def void(self) -> bool:
        """Delete the reservation. ONLY for a leg that provably never happened."""
        if self.reservation is None:
            return False
        try:
            removed = cost.void_reservation(self.entry_id)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not raise
            logger.warning("llm reservation %s not voided: %s: %s",
                           self.entry_id, type(exc).__name__, exc)
            return False
        self.settled = True
        return bool(removed)


# -- telemetry --------------------------------------------------------------


def _record_event(record: PaidSubmission, *, level: str, message: str,
                 secrets: tuple[str, ...] = ()) -> None:
    """Durable, workspace-scoped record of a money-relevant phase.

    Best-effort by design: a telemetry outage must not abort a completion
    that has already been paid for. ``detail`` is scrubbed on the way out --
    an httpx error embeds the full request URL, so a gateway credential would
    otherwise land in the event log.
    """
    try:
        from app.services.events import record_event

        data = record.to_dict()
        data["detail"] = redact_error_text(data.get("detail", ""), secrets=secrets)
        record_event(record.workspace_id or None, kind="paid.submission",
                     message=redact_error_text(message, secrets=secrets),
                     level=level, source=f"llm.{record.operation}", data=data)
    except Exception as exc:  # noqa: BLE001 - telemetry never breaks a call
        logger.warning("paid llm submission not recorded: %s: %s",
                       type(exc).__name__, exc)


def _book_cost(record: PaidSubmission, *, secrets: tuple[str, ...] = (),
               authorization: SpendAuthorization | None = None) -> None:
    """Put the money in the ledger -- or admit that we do not know it.

    ``services/cost.py`` drops any amount <= 0, so a billable completion
    booked at 0.0 produces NO row and the spend vanishes. An amount we cannot
    compute is recorded as an UNKNOWN_EXPOSURE incident instead, which is
    categorically different from zero.

    When a reservation exists the amount is settled ON that row, never written
    a second time: the estimate already counted against the daily cap, so a
    separate ``track_cost`` would bill the same completion twice.
    """
    amount = record.cost.ledger_value
    if amount is None:
        _record_event(record, level="error", secrets=secrets, message=(
            f"{record.provider}.{record.operation} exposure is UNKNOWN "
            f"(submission {record.submission_id}); the completion may have "
            f"been generated and billed and no amount was reported. A chat "
            f"completion has no remote id, so this cannot be reconciled "
            f"afterwards"))
        return
    if amount <= 0:
        if authorization is not None:
            # Nothing to book, but the reservation still exists: void it so a
            # free response does not sit in the ledger pretending to be spend.
            authorization.void()
        return
    if authorization is not None and authorization.settle(float(amount)):
        return
    try:
        cost.track_cost(record.workspace_id or "", "llm", amount,
                        provider=record.provider, is_estimate=False,
                        detail=record.to_dict())
    except Exception as exc:  # noqa: BLE001 - the ledger must not break a call
        logger.warning("llm cost not booked: %s: %s", type(exc).__name__, exc)


def _executor(workspace_id: str, operation: str, *,
              secrets: tuple[str, ...] = (),
              authorization: SpendAuthorization | None = None
              ) -> tuple[PaidProviderExecutor, list[PaidSubmission]]:
    """One executor per billable attempt, plus the records it kept.

    A fresh executor per candidate is deliberate: each POST is its own
    billable attempt and must leave its own evidence. Reusing one would merge
    "we sent this" with "we sent that".

    ``submit_budget`` is the Work 15.8 §4 change. Before it, this constructor
    passed no budget callable, so ``PaidProviderExecutor.check_budget``
    short-circuited to a debug log and NO LLM spend was ever pre-authorised --
    the audit's "budget gate: no" column was true for every row of it.
    """
    records: list[PaidSubmission] = []
    executor = PaidProviderExecutor(
        workspace_id=workspace_id,
        provider=PROVIDER,
        operation=operation,
        persist=lambda rec: _record_event(
            rec,
            level=("warning" if rec.state is SubmissionState.SUBMISSION_UNKNOWN
                   else "info"),
            secrets=secrets,
            message=(f"{PROVIDER}.{operation} {rec.state} "
                     f"(submission {rec.submission_id})")),
        # The executor calls audit(record, phase); keep the record so this
        # module can settle the cost once the text exists (or cannot).
        audit=lambda rec, _phase: records.append(rec),
        cost_hook=lambda rec: _book_cost(rec, secrets=secrets,
                                         authorization=authorization),
        # The gate runs BEFORE the attempt is recorded and before the request
        # leaves: estimate -> assert -> reserve -> send.
        submit_budget=(authorization.authorize if authorization is not None
                       else None),
        # OpenAI-compatible gateways document no idempotency key. Inventing
        # one wastes a round trip and teaches the reader that the header means
        # something when it does not.
        idempotency=IdempotencySupport.UNSUPPORTED,
        # RECONCILE would tell an operator to "go and find the job by its id".
        # For a chat completion there is no id and no status endpoint (see
        # :func:`llm_reconciliation_capability`), so the recommended action is
        # a human decision about the work, not a lookup.
        reconciliation=Reconciliation.MANUAL_OVERRIDE,
    )
    return executor, records


# -- the guarded loop -------------------------------------------------------


def _submitter(transport: Callable[..., object], url: str, api_key: str,
               candidate: CompletionCandidate, timeout: float):
    """Build the exactly-one-outbound-request callable for one candidate.

    A 2xx is reported as ACCEPTED even when the body turns out to be
    unusable: the vendor metered it either way, and pretending the acceptance
    is unknown would turn a known spend into a coin-flip. The unusable-ness is
    priced and raised by the driver instead.
    """
    def submit(_idempotency_key: str) -> RemoteSubmission:
        resp = transport(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=candidate.body,
            timeout=timeout,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - a 2xx is still billable
            return RemoteSubmission(
                remote_id="", artifact_path=INLINE_ARTIFACT,
                state=SubmissionState.SUCCEEDED,
                raw={"__billed_unusable__": f"undecodable 2xx body: "
                                           f"{type(exc).__name__}"})
        if not isinstance(data, dict):
            return RemoteSubmission(
                remote_id="", artifact_path=INLINE_ARTIFACT,
                state=SubmissionState.SUCCEEDED,
                raw={"__billed_unusable__": f"2xx body was "
                                             f"{type(data).__name__}"})
        return RemoteSubmission(remote_id="", artifact_path=INLINE_ARTIFACT,
                                state=SubmissionState.SUCCEEDED, raw=data)
    return submit


class _Unusable(Exception):
    """A 2xx we cannot turn into content. The money is already spent."""

    def __init__(self, reason: str, usage: dict) -> None:
        super().__init__(reason)
        self.reason = reason
        self.usage = usage


def _read_content(raw: dict, model: str) -> tuple[str, dict]:
    """Pull the text and usage out of an accepted response body.

    Returns ``(text, usage)`` or raises :class:`_Unusable`. Every failure here
    happens AFTER acceptance, so the caller must price it and must not re-send.
    """
    if "__billed_unusable__" in raw:
        raise _Unusable(str(raw["__billed_unusable__"]), {})
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    try:
        message = raw["choices"][0]["message"]
        text = strip_think_tags(message.get("content"), provider=model)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise _Unusable(f"{type(exc).__name__}: {exc}", usage) from exc
    return text, usage


def _price(model: str, usage: dict) -> tuple[float, bool]:
    """Amount and whether it is a real measurement or an estimate."""
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return 0.0, True
    return float(cost.estimate_llm_cost(model, prompt_tokens, completion_tokens)), True


def _settle(executor: PaidProviderExecutor, record: PaidSubmission, *,
            amount: float, note: str) -> None:
    """Close the book on an accepted completion.

    A reported amount is booked; a completion with no usable token usage is
    booked as an UNKNOWN exposure, never as ``$0``.
    """
    if note:
        record.detail = f"{record.detail} | {note}" if record.detail else note
    if amount > 0:
        record.cost = CostRecord(outcome=CostOutcome.ACTUAL, actual=float(amount))
    else:
        record.cost = CostRecord(outcome=CostOutcome.UNKNOWN_EXPOSURE)
    executor.mark_succeeded(record)


def run_completion(
    *,
    transport: Callable[..., object],
    url: str,
    api_key: str,
    candidates: list[CompletionCandidate],
    max_tokens: int = 1500,
    timeout: float = 120.0,
    workspace_id: str = "",
    operation: str = "complete",
    policy: FallbackPolicy | None = None,
    secrets: tuple[str, ...] = (),
    should_cancel: Callable[[], bool] | None = None,
    preauthorized_spend: SpendAuthorization | None = None,
) -> CompletionOutcome:
    """Run the candidate list, advancing ONLY on a proven safe retry.

    The rule, once:

        a billable POST that was delivered and lost  ->  STOP, and the money
        is an unknown exposure unless the caller opts into a second charge.

    A 4xx rejection and a connect failure (no socket was ever opened) both
    prove nothing was billed, so the next candidate is tried without asking
    anyone. Everything else is terminal.

    Work 15.8 §4: each leg is also budget-gated in the required order --
    ``estimate -> assert_can_spend -> reserve -> record attempt -> call ->
    reconcile`` -- so a leg that cannot be paid for never reaches the wire and
    never books a row.
    """
    policy = policy or FallbackPolicy()
    last_record: PaidSubmission | None = None
    tried: list[str] = []

    for index, candidate in enumerate(candidates):
        remaining = tuple(c.model for c in candidates[index + 1:])
        estimated = estimate_request_cost(candidate.model, candidate.body, max_tokens)
        authorization = SpendAuthorization(
            workspace_id=workspace_id, amount_usd=estimated)
        if index == 0 and preauthorized_spend is not None:
            # The caller already reserved for THIS request (Work 15.8 §5:
            # batch translation prices one combined request, not twenty
            # segments). Reuse its row so a single POST writes a single
            # reservation; reserving again would over-count the cap.
            authorization = preauthorized_spend
        executor, records = _executor(workspace_id, operation, secrets=secrets,
                                      authorization=authorization)
        tried.append(candidate.model)
        try:
            handle = executor.execute(
                _submitter(transport, url, api_key, candidate, timeout),
                estimated_cost=estimated,
                should_cancel=should_cancel,
            )
        except cost.BudgetExceededError as refused:
            # The gate ran before the attempt was recorded and before the
            # request left, so there is nothing to reconcile and no row to
            # void. Surfaced as its own kind so it cannot be mistaken for a
            # provider failure.
            raise LLMCompletionBudgetRefused(
                str(refused), estimated_usd=estimated) from refused
        except AmbiguousSubmission as ambiguous:
            record = ambiguous.submission
            last_record = record
            if verdict_for(record) is RetryVerdict.RETRY:
                # PROVEN undelivered: a 4xx, or a connection never opened.
                # Not a money decision, so the policy is not consulted.
                authorization.void()
                logger.warning(
                    "llm completion %s was refused before any work was done "
                    "(%s); next candidate: %s", candidate.cost_hint,
                    redact_error_text(record.detail, secrets=secrets),
                    ", ".join(remaining) or "none left")
                continue

            # Money may have been spent and the amount is not yet known.
            # Book it as an unknown exposure, never as $0. The reservation is
            # left in place: it is the row that proves we authorised this
            # spend, and voiding it would make an unknown exposure invisible.
            _book_cost(record, secrets=secrets, authorization=authorization)
            verdict = policy.decide(record, remaining_models=remaining)
            if not verdict.allowed:
                # Carry the (redacted) failure text too: an operator reading
                # this needs to know it was a read timeout, and the api key
                # that a gateway URL would otherwise have leaked.
                raise LLMCompletionUnknown(
                    record,
                    detail=(f"{redact_error_text(record.detail, secrets=secrets)}; "
                            f"{verdict.reason}")) from ambiguous
            continue
        except PaidJobRejected as rejected:
            last_record = records[-1] if records else last_record
            if last_record is not None and last_record.state is SubmissionState.CANCELLED:
                # We stopped before sending. Known outcome, nothing billed, and
                # emphatically NOT an ambiguity to be retried.
                authorization.void()
                raise LLMCompletionCancelled(
                    last_record,
                    detail=redact_error_text(str(rejected), secrets=secrets)) from rejected
            # A definitive rejection: the provider refused the request itself,
            # so the reservation is voided and the next candidate is free.
            authorization.void()
            logger.warning("llm completion %s rejected: %s", candidate.cost_hint,
                           redact_error_text(str(rejected), secrets=secrets))
            continue

        # -- accepted: the money is spent, the question is what came back ----
        record = records[-1]
        last_record = record
        try:
            text, usage = _read_content(handle.raw, candidate.model)
        except _Unusable as unusable:
            amount, _ = _price(candidate.model, unusable.usage)
            _settle(executor, record, amount=amount,
                    note=f"accepted and billed, but unusable: {unusable.reason}")
            verdict = policy.decide(record, remaining_models=remaining)
            if not verdict.allowed:
                raise LLMCompletionUnusable(
                    record, detail=unusable.reason) from unusable
            continue

        amount, is_estimate = _price(candidate.model, usage)
        _settle(executor, record, amount=amount, note="completed")
        return CompletionOutcome(
            text=text, model=candidate.model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=amount, attempts=len(tried),
            models_tried=tuple(tried), submission_id=record.submission_id,
            is_estimate=is_estimate,
        )

    if last_record is None:
        raise LLMCompletionPaidError(
            f"{PROVIDER}.{operation} had no candidate to attempt")
    # The record's own detail, not the classified error's message: a
    # provably-undelivered attempt carries "the provider may have accepted and
    # billed this" boilerplate that is exactly wrong here.
    raise LLMCompletionExhausted(
        last_record,
        detail=(f"last failure was proven undelivered: "
                f"{redact_error_text(last_record.detail, secrets=secrets)}"))
