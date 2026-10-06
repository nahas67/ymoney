"""The mechanics every billable provider wires the same way (Work 15.8 §8).

Four providers (``providers/images.py``, ``providers/tts.py``,
``providers/avatar.py``, ``providers/broll.py``) each grew the same ~35 lines:
build a :class:`~app.services.paid_executor.PaidProviderExecutor`, keep the
records, hand back a ``settle`` closure that repeats the same three-way
"actual / estimate / nothing reported" branch, and add a one-line budget gate.
The duplication is not the executor -- that is shared already -- it is the
*wiring around it*, and it is exactly the wiring that is easy to get subtly
wrong, because every clause of it is about money:

    authorize (reserve)  ->  record the attempt  ->  ONE request
                         ->  close the book in place, or release the
                             reservation when the request provably never left

This module owns those clauses and nothing else.

**What is deliberately NOT here.** A provider keeps its own request payload,
its own status endpoint, its own error semantics, its own idempotency support
and its own cancellation. ``PaidOperation`` never builds a body, never calls
``httpx`` and never decides what an HTTP 429 means; a caller that cannot express
its failure modes through :meth:`PaidOperation.mark_accepted` /
:meth:`PaidOperation.mark_unknown` / :meth:`PaidOperation.mark_rejected` is
telling you something real about that provider, and the fix belongs in the
provider.

**Reservations are per REQUEST, never per item.** The caller that loops decides
what one request is. A translation batch of twenty subtitle segments is ONE
completion, so it reserves ONE estimate; a per-segment reservation would
over-reserve twenty-fold and refuse work that fits in the budget.

**Exposure is a value, not a zero.** :meth:`PaidOperation.mark_unknown` keeps
the reservation (the money may well be gone) and marks the row
``UNKNOWN_EXPOSURE``. :meth:`PaidOperation.release` deletes the row, and is
only correct for a request that provably never reached the provider. Booking
``0.0`` because YMONEY lost the response would delete a real charge from the
books; booking a reservation for a 4xx would shrink the budget for work that
was never done.

**Structural outcomes, not JSON.** An operator has to be able to ask "which
submissions may have been billed?" with a WHERE clause.
:data:`EXECUTION_OUTCOMES` and :data:`COST_OUTCOMES` are the canonical
vocabularies -- :class:`app.services.paid_jobs.SubmissionState` and
:class:`app.services.paid_executor.CostOutcome` -- re-exported so a row column
can hold one of them. No new enum was invented: an ambiguous submit already has
a canonical name (``SUBMISSION_UNKNOWN``) and inventing a second spelling for it
is how two dashboards end up disagreeing.

**Work 15.9 §1/§8: every billable operation has exactly ONE owner, and a
missing workspace is not a licence to spend.** Until now :meth:`PaidOperation.authorize`
logged a warning and returned WITHOUT reserving when ``workspace_id`` was
empty, on the reasoning that "a caller with no tenant has no budget". That is the
worst of both worlds: the request still went out, still cost money, and the cap
never saw it. Localization translation reached this lane with a hardcoded
``workspace_id=""`` and was therefore unbudgeted entirely.

So ownership is now a first-class, explicit declaration (:class:`SpendAuthority`):

* :attr:`SpendAuthority.WORKSPACE_OWNED` -- the default. Requires a real
  workspace. Missing one raises :class:`OwnerlessSpendRefused` **before** the
  external call.
* :attr:`SpendAuthority.SYSTEM_OWNED` -- YMONEY's own money (an operator-run
  maintenance job). Requires an *explicitly configured* system budget
  (:data:`SYSTEM_BUDGET_ENV`); with none configured this blocks exactly like an
  ownerless call, because an unbounded system budget is the same hole with a
  nicer name.
* :attr:`SpendAuthority.EXPLICIT_NONBILLABLE` -- the caller asserts nothing is
  charged (a local operator TTS server, a test fixture). Reached only by an
  explicit declaration, never inferred from a missing workspace.

:class:`ActorAuthority` records WHO asked (:attr:`ActorAuthority.USER`,
``AUTONOMOUS_POLICY``, ``APPROVED_WORKFLOW``, ``SYSTEM``) and is persisted on
the reservation row. It deliberately relaxes nothing: ``SYSTEM`` means "this is
not a tenant request", not "so it may spend freely". An operator actor with no
workspace and no configured system budget is refused -- and that refusal is a
test, not a comment.

**Work 15.9 §3: the four providers now route through this module, and the three
clauses they each used to re-derive are gone from them.** ``providers/images``,
``providers/tts``, ``providers/avatar`` and ``providers/broll`` each carried a
``_paid()`` helper whose body was the same ~35 lines -- build the executor, keep
the records, repeat a three-way settle branch, add an advisory one-line budget
check -- and each of them had the same two defects, which are the reason this
module existed before they used it:

* the gate was ``cost.assert_can_spend``, the **advisory** read: two concurrent
  callers could both be told yes for the same last dollar;
* ``settle`` called ``track_cost`` with the workspace id from the executor,
  which for an operation with no tenant in scope is ``""``. Four provider call
  sites booked a ``CostEntry`` against an empty owner
  (``images.py:112``, ``tts.py:878``, ``avatar.py:96``, ``broll.py:118``).

Both are consequences of the same mistake: the reservation and the money were
two different writes by two different functions. Here they are one row.
:meth:`PaidOperation.close_book` settles the reservation's OWN row; it never
inserts a second one.

**Work 15.9 §5: the invariant is exactly-once ACCOUNTING, not exactly-once
network.** A remote job that was accepted and then lost to a restart has been
paid for exactly once, and the recovery path must not buy it again. That needs
one thing the ledger has to survive on its own: the remote id, written onto the
reservation row by :meth:`PaidOperation.mark_accepted`. Given it,
:func:`reattach_by_remote_id` finds that row after a restart and hands back a
:class:`PaidOperation` bound to it, so :meth:`PaidOperation.authorize` reserves
nothing and :meth:`PaidOperation.close_book` settles nothing a second time.
One remote id, one accounting identity.

**§6: one place decides what a failure does to the books.**
:func:`absorb_paid_failure` is that place. Every provider used to answer the
same question -- was money spent, yes or no? -- with its own inline branch, and
the three answers that matter pull in opposite directions: a 4xx RELEASES the
reservation, a lost response KEEPS it and marks the exposure unknown, and a
connect failure PROVES nothing was delivered so it releases despite arriving as
an ambiguity. Getting the third one wrong in either direction is a phantom
charge or an erased charge, so it is written down once and tested.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from loguru import logger

from app.services.cost import BudgetExceededError
from app.services.paid_executor import (
    AmbiguousSubmission,
    CostOutcome,
    CostRecord,
    IdempotencySupport,
    PaidProviderExecutor,
    Reconciliation,
    RetrySafety,
)
from app.services.paid_jobs import (
    PaidArtifactUndownloadable,
    PaidJobError,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionState,
)

__all__ = [
    "ACTOR_AUTHORITIES",
    "COST_OUTCOMES",
    "EXECUTION_OUTCOMES",
    "SPEND_AUTHORITIES",
    "SYSTEM_BUDGET_ENV",
    "SYSTEM_LEDGER_OWNER",
    "ActorAuthority",
    "FailureVerdict",
    "OwnerlessSpendRefused",
    "PaidOperation",
    "ReattachedReservation",
    "SpendAuthority",
    "SpendOwnership",
    "absorb_paid_failure",
    "adopt_reservation",
    "annotate_exposure",
    "cost_outcome_of",
    "execution_outcome_of",
    "paid_event",
    "paid_operation",
    "reattach_by_remote_id",
    "reservation_detail",
    "resolve_ownership",
    "system_budget_usd",
]

#: What HAPPENED to a billable request. These are the canonical
#: ``SubmissionState`` values, NOT a parallel vocabulary: a submission that may
#: have been billed is ``SUBMISSION_UNKNOWN`` everywhere in YMONEY, and a row
#: column holding it makes the answer filterable without parsing JSON.
EXECUTION_OUTCOMES: tuple[str, ...] = tuple(str(state) for state in SubmissionState)

#: What the LEDGER may say about it. ``UNKNOWN_EXPOSURE`` is the load-bearing
#: member and is categorically different from ``NOT_APPLICABLE``: the first
#: means money may be gone, the second means nothing was ever sent.
COST_OUTCOMES: tuple[str, ...] = tuple(str(outcome) for outcome in CostOutcome)


class SpendAuthority(StrEnum):
    """WHOSE MONEY pays for one billable operation. Exactly one of these.

    Not a free-form string and not inferred: an operation with no declared
    authority defaults to :attr:`WORKSPACE_OWNED`, which is the only choice
    that can refuse. The three members are mutually exclusive on purpose --
    "someone's budget", "ours", and "nobody's, and here is the proof" are three
    different claims, and collapsing them is how a tenant's cap ends up paying
    for YMONEY's own maintenance work or vice versa.
    """

    WORKSPACE_OWNED = "WORKSPACE_OWNED"
    SYSTEM_OWNED = "SYSTEM_OWNED"
    EXPLICIT_NONBILLABLE = "EXPLICIT_NONBILLABLE"


class ActorAuthority(StrEnum):
    """WHO asked for the spend, recorded next to the money.

    Orthogonal to :class:`SpendAuthority`: a user may trigger autonomous-policy
    work and an operator may trigger a tenant's render. It is stored because
    "which tenant spent this, and did a human or a policy loop ask?" is the
    first question of any spend investigation.

    :attr:`SYSTEM` is emphatically **not** a waiver. It names the actor class;
    it grants no budget. With no workspace and no configured system budget the
    operation is refused exactly as it would be for a user.
    """

    USER = "USER"
    AUTONOMOUS_POLICY = "AUTONOMOUS_POLICY"
    APPROVED_WORKFLOW = "APPROVED_WORKFLOW"
    SYSTEM = "SYSTEM"


#: Queryable spellings, for the same reason ``COST_OUTCOMES`` exists.
SPEND_AUTHORITIES: tuple[str, ...] = tuple(str(a) for a in SpendAuthority)
ACTOR_AUTHORITIES: tuple[str, ...] = tuple(str(a) for a in ActorAuthority)

#: Operator-set ceiling for :attr:`SpendAuthority.SYSTEM_OWNED` work, in USD per
#: spend window. Unset (or unparseable) means ``0.0``, which means the system
#: budget is NOT configured and system-owned work is refused. It is an
#: environment variable rather than a new settings field so that switching it
#: on is an explicit, auditable act in the deployment manifest and there is no
#: default anyone can inherit by accident.
SYSTEM_BUDGET_ENV = "YMONEY_SYSTEM_BUDGET_USD"

#: Ledger owner for YMONEY's own spend. Not a tenant: ``cost_entries`` has no
#: foreign key, and a system row must be identifiable as a system row rather
#: than be silently attributed to whichever tenant happened to trigger it.
SYSTEM_LEDGER_OWNER = "__system__"

#: How far :func:`reattach_by_remote_id` scans before it gives up looking. A
#: recovery path runs once per orphaned job, so a bounded scan is the right
#: trade: it cannot become a table walk, and a row older than this is an
#: incident that needs a human, not a silent re-book.
_ATTACH_SCAN_LIMIT = 500

#: ``detail_json`` keys, named so an operator can grep one ledger for the whole
#: authority picture instead of parsing prose.
OWNERSHIP_KEYS = ("spend_authority", "actor_authority", "budget_source",
                  "owned_workspace_id", "charged_workspace_id",
                  "system_budget_usd")


def system_budget_usd() -> float:
    """The operator's system-spend ceiling, or ``0.0`` when unconfigured.

    ``0.0`` is the honest reading of "not set". Returning a large default here
    would turn a missing configuration into unlimited spend, which is the
    failure this whole module exists to make impossible.
    """
    raw = os.environ.get(SYSTEM_BUDGET_ENV, "")
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        if str(raw).strip():
            logger.warning("paid: {}={!r} is not a number; system-owned spend "
                           "is treated as unconfigured", SYSTEM_BUDGET_ENV, raw)
        return 0.0


class OwnerlessSpendRefused(BudgetExceededError):
    """A billable request with nobody's budget behind it. Raised BEFORE sending.

    A :class:`app.services.cost.BudgetExceededError` subclass on purpose: this
    IS a budget refusal, so every existing ``except BudgetExceededError``
    handler already treats it correctly (no request went out, nothing billed),
    while a caller that cares about the distinction can catch this one.

    Carries the full authority picture so the refusal is diagnosable from the
    log line alone: who asked, what was claimed, and which of the three
    requirements was not met.
    """

    def __init__(self, *, provider: str, operation: str, reason: str,
                 actor: ActorAuthority | str = ActorAuthority.USER,
                 authority: SpendAuthority | str = SpendAuthority.WORKSPACE_OWNED,
                 workspace_id: str = "", estimated_usd: float = 0.0) -> None:
        self.provider = provider
        self.operation = operation
        self.reason = reason
        self.actor = str(actor)
        self.authority = str(authority)
        self.workspace_id = str(workspace_id or "")
        self.estimated_usd = round(float(estimated_usd or 0.0), 6)
        super().__init__(
            f"{provider}.{operation} (about ${self.estimated_usd:.4f}) has no "
            f"budget owner: {reason}. Declared authority={self.authority}, "
            f"actor={self.actor}, workspace_id="
            f"{self.workspace_id or '<empty>'}. Nothing was sent and nothing was "
            f"billed -- the request is refused rather than run unbudgeted."
        )


@dataclass(frozen=True)
class SpendOwnership:
    """Who pays for one operation, resolved once and persisted with it.

    ``budget_source`` is deliberately a distinct field rather than being
    derivable from ``workspace_id``: a system-owned row is charged to
    ``SYSTEM_LEDGER_OWNER``, so "the ledger owner is not a tenant" is exactly
    the kind of fact that is invisible if you only look at ids.
    """

    authority: SpendAuthority
    actor: ActorAuthority
    #: The tenant that initiated the work. Empty for non-billable operations and
    #: for system work that never had a tenant.
    owned_workspace_id: str
    #: The ledger row's ``workspace_id``: the tenant, or ``SYSTEM_LEDGER_OWNER``.
    charged_workspace_id: str
    budget_source: str
    estimated_usd: float
    provider: str
    operation: str
    #: The system ceiling in force, so a later audit can see the limit this
    #: operation was judged against rather than today's number.
    system_budget_usd: float = 0.0
    reservation_id: str = ""

    @property
    def billable(self) -> bool:
        return self.authority is not SpendAuthority.EXPLICIT_NONBILLABLE

    def as_detail(self) -> dict:
        """The ``detail_json`` fragment written onto the reservation row."""
        return {
            "spend_authority": str(self.authority),
            "actor_authority": str(self.actor),
            "budget_source": self.budget_source,
            "owned_workspace_id": self.owned_workspace_id,
            "charged_workspace_id": self.charged_workspace_id,
            "system_budget_usd": round(float(self.system_budget_usd), 6),
        }

    def to_dict(self) -> dict:
        return {**self.as_detail(), "provider": self.provider,
                "operation": self.operation,
                "estimated_usd": round(float(self.estimated_usd), 6),
                "reservation_id": self.reservation_id,
                "billable": self.billable}


def coerce_authority(mapping: type, value, label: str):
    """Accept the enum or its spelling; refuse anything else loudly.

    A misspelled authority stored as free text would be a permanent, silently
    un-authorised spend, so this raises rather than defaulting.
    """
    if isinstance(value, mapping):
        return value
    try:
        return mapping(str(value))
    except ValueError as exc:
        raise ValueError(
            f"unknown {label}: {value!r}; expected one of "
            f"{', '.join(str(v) for v in mapping)}") from exc


def resolve_ownership(
    *,
    workspace_id: str,
    provider: str,
    operation: str,
    estimated_usd: float = 0.0,
    authority: SpendAuthority | str = SpendAuthority.WORKSPACE_OWNED,
    actor: ActorAuthority | str = ActorAuthority.USER,
    reservation_id: str = "",
) -> SpendOwnership:
    """Resolve one operation's owner, or refuse it.

    The three rules, and why each is a hard stop rather than a fallback:

    1. :attr:`SpendAuthority.WORKSPACE_OWNED` needs a workspace. No workspace is
       not "unlimited", it is "unaccountable", and the request costs real money.
    2. :attr:`SpendAuthority.SYSTEM_OWNED` needs an explicitly configured
       system budget. Without :data:`SYSTEM_BUDGET_ENV` there is no ceiling to
       enforce, so an unbounded system budget is the same hole as no budget.
    3. :attr:`SpendAuthority.EXPLICIT_NONBILLABLE` is a positive assertion by
       the caller. It is never chosen on the caller's behalf -- an empty
       workspace does not promote anything to non-billable.

    :attr:`ActorAuthority.SYSTEM` passes through untouched: it names the actor
    and grants nothing.
    """
    ws = str(workspace_id or "").strip()
    kind = coerce_authority(SpendAuthority, authority, "spend authority")
    who = coerce_authority(ActorAuthority, actor, "actor authority")
    estimate = round(float(estimated_usd or 0.0), 6)

    if kind is SpendAuthority.EXPLICIT_NONBILLABLE:
        return SpendOwnership(
            authority=kind, actor=who, owned_workspace_id=ws,
            charged_workspace_id="", budget_source="NONE",
            estimated_usd=estimate, provider=provider, operation=operation,
            reservation_id=reservation_id)

    if kind is SpendAuthority.WORKSPACE_OWNED:
        if not ws:
            raise OwnerlessSpendRefused(
                provider=provider, operation=operation, workspace_id="",
                authority=kind, actor=who, estimated_usd=estimate,
                reason=("workspace_id is empty, so no tenant cap could be "
                        "enforced or charged"))
        return SpendOwnership(
            authority=kind, actor=who, owned_workspace_id=ws,
            charged_workspace_id=ws, budget_source="WORKSPACE_BUDGET",
            estimated_usd=estimate, provider=provider, operation=operation,
            reservation_id=reservation_id)

    # SYSTEM_OWNED: the operator's own money, against an explicit ceiling.
    cap = system_budget_usd()
    if cap <= 0:
        raise OwnerlessSpendRefused(
            provider=provider, operation=operation, workspace_id=ws,
            authority=kind, actor=who, estimated_usd=estimate,
            reason=(f"no system budget is configured (set {SYSTEM_BUDGET_ENV}), "
                    f"so SYSTEM_OWNED has no ceiling to enforce"))
    return SpendOwnership(
        authority=kind, actor=who, owned_workspace_id=ws,
        charged_workspace_id=SYSTEM_LEDGER_OWNER, budget_source="SYSTEM_BUDGET",
        estimated_usd=estimate, provider=provider, operation=operation,
        system_budget_usd=cap, reservation_id=reservation_id)


def execution_outcome_of(state: SubmissionState | str) -> str:
    """Normalise a submission state into its structural execution outcome.

    A bare string is returned unchanged so a row written before this existed
    still reads back as itself.
    """
    if isinstance(state, SubmissionState):
        return str(state)
    return str(state or "")


def cost_outcome_of(outcome: CostOutcome | str) -> str:
    """Normalise a cost outcome into its structural, queryable spelling."""
    if isinstance(outcome, CostOutcome):
        return str(outcome)
    return str(outcome or "")


def reservation_detail(op: PaidOperation, extra: dict | None = None) -> dict:
    """The ``detail_json`` a reservation row carries.

    Written into the ledger so the cost row is self-describing: the row names the
    provider, the operation and the canonical ``operation_id`` even if the
    caller's own row is later deleted, plus the resolved authority picture
    (owner, actor, budget source) so "who paid for this" survives the process
    that answered it.
    """
    payload: dict = {
        "operation_id": op.operation_id,
        "provider": op.provider,
        "operation": op.operation,
        "estimated_usd": round(float(op.estimated_cost or 0.0), 6),
    }
    payload.update(op.ownership.as_detail() if op.ownership else {})
    payload.update(extra or {})
    return payload


def annotate_exposure(entry_id: str, outcome: CostOutcome, *,
                      detail: dict | None = None) -> bool:
    """Stamp a cost outcome onto an EXISTING ledger row, in place.

    Never inserts. The reservation already counted against the daily cap, so a
    second row would bill one operation twice -- the exact mistake
    ``cost.settle_reservation``'s docstring warns about.

    Reuses :data:`app.services.cost.UNKNOWN_EXPOSURE_MARKER` rather than a
    second spelling of it: the operator incidents endpoint already reads that
    constant, and two spellings mean an incident list that is half empty.
    """
    if not str(entry_id or "").strip():
        return False
    from app.db import session_scope
    from app.models import CostEntry
    from app.services.cost import UNKNOWN_EXPOSURE_MARKER

    unknown = outcome is CostOutcome.UNKNOWN_EXPOSURE
    with session_scope() as session:
        entry = session.get(CostEntry, str(entry_id))
        if entry is None:
            # A vanished row is a bookkeeping loss, never a reason to invent a
            # replacement: writing one would double-count the operation.
            logger.warning("paid cost row %s vanished; exposure not annotated",
                           entry_id)
            return False
        payload = dict(entry.detail_json or {})
        payload["cost_outcome"] = (
            UNKNOWN_EXPOSURE_MARKER if unknown else str(outcome))
        payload["exposure_unknown"] = unknown
        payload.update(detail or {})
        entry.detail_json = payload
    return True


def adopt_reservation(entry_id: str, *, provider: str = "", operation: str = "",
                      remote_id: str = "", settled_outcomes: frozenset | None = None
                      ) -> PaidOperation | None:
    """Adopt the accounting for a reservation row named directly by its id.

    Work 16.1 §5. :func:`reattach_by_remote_id` needs a remote id, which is
    exactly what an ambiguous submission may NOT have -- that is the whole
    situation this module exists for. An operator holding the ledger row id (or
    a lane row that can name the operation it belongs to) must still be able to
    bind to it, get ``settled`` read back off the row, and therefore be unable
    to settle or release a second time.

    Returns ``None`` when the row is gone. ``None`` is "nothing to adopt", never
    "reserve a fresh one": a vanished ledger row must not become a new charge.

    ``settled_outcomes`` overrides which ledger outcomes count as ALREADY CLOSED
    for the purpose of the ``settled`` flag. It exists for the reconciliation
    path, whose whole job is to turn an ``UNKNOWN_EXPOSURE`` row into a priced
    one, and it defaults to :data:`_CLOSED_OUTCOMES` so every other caller --
    including :func:`reattach_by_remote_id` -- is unaffected.
    """
    if not str(entry_id or "").strip():
        return None

    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as session:
        found = session.get(CostEntry, str(entry_id).strip())
        if found is None:
            logger.info("paid adopt: no reservation row %s", entry_id)
            return None
        detail = dict(found.detail_json or {})
        rid = str(remote_id or detail.get("remote_id", "") or "")
        return _adopt_entry(
            detail=detail, entry_id=str(found.id),
            amount_usd=float(found.amount_usd or 0.0),
            charged_workspace_id=str(found.workspace_id or ""), remote_id=rid,
            provider=str(provider or detail.get("provider", "") or ""),
            operation=str(operation or detail.get("operation", "") or ""),
            settled_outcomes=_CLOSED_OUTCOMES if settled_outcomes is None
            else settled_outcomes)


#: Cost outcomes that mean "this ledger row is finished being argued about".
#: A row already carrying one of these has been accounted for, so a reattach
#: adopts it rather than settling it a second time.
_CLOSED_OUTCOMES = frozenset({
    str(CostOutcome.ACTUAL), str(CostOutcome.ESTIMATED),
    str(CostOutcome.UNKNOWN_EXPOSURE), str(CostOutcome.NOT_APPLICABLE),
})


def paid_event(op: PaidOperation, phase: str, level: str, message: str,
               **data) -> None:
    """One durable activity-feed line for a billable operation's phase.

    Best-effort by construction: a telemetry outage must not be able to abort a
    render that has already been paid for. The log line :meth:`PaidOperation.
    _emit` already wrote remains the fallback evidence.

    The ``data`` payload always names the provider, the operation, the
    ``operation_id`` and -- once it is known -- the ``remote_id``, so the feed
    line and the money row can be joined without guessing.
    """
    try:
        from app.services.events import record_event

        owner = (op.ownership.charged_workspace_id if op.ownership
                 else op.workspace_id) or None
        record_event(owner, kind="paid.submission", message=message,
                     level=level, source=f"paid.{op.provider}.{op.operation}",
                     data={"provider": op.provider, "operation": op.operation,
                           "operation_id": op.operation_id, "phase": phase,
                           # ``state`` is the canonical SubmissionState and
                           # ``cost_outcome`` the canonical CostOutcome. Both
                           # names are what an operator's incident query and the
                           # 15.7 audit feed both filter on.
                           "state": op.execution_outcome,
                           "cost_outcome": cost_outcome_of(op.cost_outcome),
                           "exposure_unknown": op.unknown_exposure,
                           # The canonical ``CostRecord`` serialization as well
                           # as the flat keys, because the two are the same
                           # state and a reader should never have to choose.
                           "cost": CostRecord(
                               outcome=op.cost_outcome,
                               estimated=float(op.estimated_cost or 0.0),
                           ).to_dict(),
                           "remote_id": op.remote_id, **data})
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a paid call
        logger.warning("paid %s.%s event dropped: %s", op.provider, op.operation,
                       exc)


def _provably_undelivered(exc: BaseException) -> bool:
    """True when the failure PROVES no request reached the provider.

    Two spellings, because two different objects carry the fact, and reading
    only one of them is exactly how a connect failure ends up booked as a
    phantom charge:

    * :class:`~app.services.paid_jobs.PaidSubmissionUnconfirmed` sets
      ``provably_undelivered`` directly;
    * :class:`AmbiguousSubmission` does NOT carry the original exception -- it
      carries the submission record, which is where
      :class:`~app.services.paid_executor.RetrySafety` lives. A connect failure
      reaches a provider as ``AmbiguousSubmission``, so this second spelling is
      the one that actually fires in production.
    """
    if getattr(exc, "provably_undelivered", False):
        return True
    record = getattr(exc, "submission", None)
    return getattr(record, "retry_safety", None) is RetrySafety.SAFE


class FailureVerdict(StrEnum):
    """What one classified failure did to the books."""

    #: The request provably created nothing; the reservation was released and
    #: the budget returned to the workspace.
    RELEASED = "RELEASED"
    #: Money may have been spent and the amount is unknown; the reservation was
    #: kept and marked ``UNKNOWN_EXPOSURE``.
    UNKNOWN_EXPOSURE = "UNKNOWN_EXPOSURE"


def absorb_paid_failure(op: PaidOperation, exc: BaseException) -> FailureVerdict:
    """Fold ONE classified failure into the operation's money. The only place.

    Every provider used to answer "was money spent?" with its own inline
    branch, and the answers that matter run in opposite directions:

    * a definitive refusal (4xx) RELEASES the reservation -- the work was never
      created, and keeping the row shrinks the budget for work nobody did;
    * a lost response KEEPS it and marks the exposure unknown -- the request may
      have been accepted, and booking ``$0`` erases a real charge;
    * a connect failure arrives as an ambiguity but PROVES nothing was
      delivered, so it releases. Treating it as an unknown exposure would put a
      phantom charge on the books; treating a genuine read timeout as provably
      undelivered would erase a real one.

    That third case is the reason this is written once. A provider that cannot
    express its failure modes through the shared error types is telling you
    something real, and the fix belongs in the provider.
    """
    if _provably_undelivered(exc):
        op.mark_rejected(f"{exc} (connection never established, so nothing "
                         f"was billed)", nothing_billed=True)
        return FailureVerdict.RELEASED
    if isinstance(exc, (PaidSubmissionUnconfirmed, PaidArtifactUndownloadable,
                        AmbiguousSubmission)):
        op.mark_unknown(str(exc))
        return FailureVerdict.UNKNOWN_EXPOSURE
    if isinstance(exc, PaidJobRejected):
        op.mark_rejected(str(exc), nothing_billed=True)
        return FailureVerdict.RELEASED
    if isinstance(exc, PaidJobError):
        op.mark_unknown(str(exc))
        return FailureVerdict.UNKNOWN_EXPOSURE
    # A raw exception the classifier never saw -- a provider's own parse error,
    # most likely, raised AFTER a 2xx. The request was metered, so the
    # conservative reading is that money is gone and unpriced.
    op.mark_unknown(f"{type(exc).__name__}: {exc}")
    return FailureVerdict.UNKNOWN_EXPOSURE


@dataclass(frozen=True)
class ReattachedReservation:
    """The adopted ledger row -- enough to close its book, and nothing more.

    Deliberately not a :class:`~app.services.cost.BudgetReservation`: a reattach
    holds no lock, no permission and no headroom, because the money was already
    committed by whoever submitted the job. Only the row identity travels.
    """

    entry_id: str
    amount_usd: float = 0.0
    detail: dict = field(default_factory=dict)


def reattach_by_remote_id(remote_id: str, *, provider: str = "",
                          operation: str = "", workspace_id: str = ""
                          ) -> PaidOperation | None:
    """Adopt the accounting for a remote job that is ALREADY paid for.

    Work 15.9 §5. The invariant is exactly-once ACCOUNTING, not exactly-once
    network: a job accepted before a crash has been purchased exactly once, and
    the recovery path must not reserve, re-book or re-settle it.

    Returns a :class:`PaidOperation` bound to the existing reservation row, with
    :attr:`PaidOperation.reservation` set (so ``authorize()`` reserves nothing),
    :attr:`PaidOperation.settled` read back from the row (so ``close_book()``
    moves no money again) and :attr:`PaidOperation.ownership` rehydrated (so an
    operator still sees who paid). ``None`` when the remote id is unknown --
    "nothing to adopt" must never turn into "reserve a fresh one".

    ``provider`` / ``operation`` narrow the search. The lookup is a filtered
    scan rather than a JSON-path index query, deliberately: this runs on a
    recovery path that is rare by construction, and it has to behave identically
    on SQLite and on a server-grade engine.
    """
    rid = str(remote_id or "").strip()
    if not rid:
        return None

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import CostEntry
    from app.services.json_portability import json_value_equals

    with session_scope() as session:
        query = select(CostEntry).order_by(CostEntry.created_at.desc())
        if str(provider or "").strip():
            query = query.where(CostEntry.provider == str(provider).strip())
        if str(operation or "").strip():
            query = query.where(json_value_equals(
                CostEntry.detail_json, "operation", str(operation).strip()))
        if str(workspace_id or "").strip():
            query = query.where(
                CostEntry.workspace_id == str(workspace_id).strip())
        found: CostEntry | None = None
        for row in session.scalars(query.limit(_ATTACH_SCAN_LIMIT)):
            if str((row.detail_json or {}).get("remote_id", "")) == rid:
                found = row
                break
        if found is None:
            logger.info("paid reattach: no reservation carries remote id %s", rid)
            return None
        return _adopt_entry(
            detail=dict(found.detail_json or {}), entry_id=str(found.id),
            amount_usd=float(found.amount_usd or 0.0),
            charged_workspace_id=str(found.workspace_id or ""), remote_id=rid,
            provider=str(provider or ""),
            operation=str(operation or ""))


def _adopt_entry(*, detail: dict, entry_id: str, amount_usd: float,
                 charged_workspace_id: str, remote_id: str,
                 provider: str, operation: str,
                 settled_outcomes: frozenset | None = None) -> PaidOperation:
    """Build the :class:`PaidOperation` both adoption paths share.

    ``settled`` is read back off the ROW rather than assumed, which is what makes
    a second close impossible: an operation that adopts an already-settled
    reservation refuses to move its money again. One remote id, one accounting
    identity.
    """
    operation_id = str(detail.get("operation_id", "") or "")
    authority = str(detail.get("spend_authority", "") or
                    SpendAuthority.WORKSPACE_OWNED)
    adopted = PaidOperation(
        provider=provider,
        operation=operation,
        workspace_id=charged_workspace_id,
        category=str(detail.get("category", "") or ""),
        estimated_cost=amount_usd,
        remote_id=remote_id,
        operation_id=operation_id,
        settled=str(detail.get("cost_outcome", "") or "")
        in (_CLOSED_OUTCOMES if settled_outcomes is None else settled_outcomes),
    )
    adopted.reservation = ReattachedReservation(entry_id=entry_id,
                                                amount_usd=amount_usd,
                                                detail=detail)
    adopted.ownership = SpendOwnership(
        authority=coerce_authority(SpendAuthority, authority, "spend authority"),
        actor=coerce_authority(ActorAuthority,
                               detail.get("actor_authority", "") or
                               ActorAuthority.USER, "actor authority"),
        owned_workspace_id=str(detail.get("owned_workspace_id", "") or ""),
        charged_workspace_id=charged_workspace_id,
        budget_source=str(detail.get("budget_source", "") or "WORKSPACE_BUDGET"),
        estimated_usd=amount_usd,
        provider=adopted.provider, operation=adopted.operation,
        system_budget_usd=float(detail.get("system_budget_usd", 0.0) or 0.0),
        reservation_id=entry_id,
    )
    return adopted


@dataclass
class PaidOperation:
    """One billable operation's money mechanics. Provider semantics stay out.

    Construct with :func:`paid_operation` and bind the callbacks that write the
    caller's own row. The lifecycle is:

        op = paid_operation(..., estimated_cost=estimate)
        op.bind(on_attempt=..., on_execution=..., on_cost_outcome=...)
        op.authorize()          # reserves; raises BEFORE anything is sent
        op.mark_attempt()       # durable evidence the request is about to leave
        handle = submit()       # the caller's own request, exactly once
        op.mark_accepted(remote_id)
        ...
        op.close_book(actual)   # settles on the reservation row, in place

    and on failure exactly one of ``mark_unknown`` (may have been billed),
    ``mark_rejected`` (definitively refused), or ``mark_cancelled`` (we stopped
    before sending).
    """

    provider: str
    operation: str
    workspace_id: str = ""
    category: str = ""
    estimated_cost: float = 0.0
    idempotency: IdempotencySupport = IdempotencySupport.UNVERIFIED
    reconciliation: Reconciliation = Reconciliation.RECONCILE
    #: Work 15.9 §1. Defaults to the only authority that can REFUSE, so a
    #: caller that never declares anything still cannot spend unbudgeted.
    authority: SpendAuthority = SpendAuthority.WORKSPACE_OWNED
    #: Who asked. Recorded, never a waiver.
    actor: ActorAuthority = ActorAuthority.USER
    #: Extra facts the caller wants on the reservation row (batch index, render
    #: size, segment count). Never the money: that is ``estimated_usd``.
    reservation_extra: dict = field(default_factory=dict)
    on_attempt: Callable[[str], None] | None = None
    on_execution: Callable[[str, str], None] | None = None
    on_cost_outcome: Callable[[str], None] | None = None
    on_remote_id: Callable[[str], None] | None = None
    on_event: Callable[[str, str, str], None] | None = None
    #: The canonical id of this attempt. Written onto the ledger row and onto
    #: the caller's row, so one submit can be traced from the render to the
    #: dollar after a restart.
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    reservation: object | None = None
    unknown_exposure: bool = False
    #: Work 15.9 §5. The provider's durable handle for the thing that was paid
    #: for, persisted onto the RESERVATION row. Not on the caller's row: that
    #: row lives in whatever table the caller's request touches and dies with
    #: it, while the ledger row is exactly what survives a restart.
    remote_id: str = ""
    #: Work 15.9 §5. Set once :meth:`close_book` (or :meth:`release`) has closed
    #: this operation's book, and read back from the ledger row on a reattach so
    #: a second settle cannot happen. Without it a recovery path that settles
    #: "just to be sure" moves money twice for one purchase.
    settled: bool = False
    #: What the ledger currently says about this operation's money. Kept on the
    #: object so a later annotation (the remote id, say) stamps the CURRENT
    #: outcome rather than overwriting it with the default.
    cost_outcome: CostOutcome = CostOutcome.NOT_APPLICABLE
    #: The canonical :class:`~app.services.paid_jobs.SubmissionState` the request
    #: last reached. Kept for the same reason ``cost_outcome`` is: the activity
    #: feed names it, so an operator reading "was this billed?" never has to
    #: infer it from prose.
    execution_outcome: str = ""
    #: Resolved by :meth:`authorize` and then written onto the ledger row. Kept
    #: on the object so an operator-facing read of a live operation answers
    #: "whose money" without re-deriving it from ids.
    ownership: SpendOwnership | None = None

    # -- wiring ----------------------------------------------------------

    def declared(self, *, authority: SpendAuthority | str | None = None,
                 actor: ActorAuthority | str | None = None) -> PaidOperation:
        """Declare WHO pays and WHO asked. Returns self so it chains.

        The default needs no call: an operation nobody declared anything about
        is :attr:`SpendAuthority.WORKSPACE_OWNED`, which is the strict reading.
        This exists for the two legitimate exceptions --
        :attr:`SpendAuthority.SYSTEM_OWNED` (an operator-run job on YMONEY's
        own money) and :attr:`SpendAuthority.EXPLICIT_NONBILLABLE` (a provider
        the caller asserts is free, such as a local operator TTS server).

        It is a declaration, not a permission: :meth:`authorize` still refuses
        ``SYSTEM_OWNED`` when no system budget is configured. An invalid
        spelling raises here rather than silently defaulting.
        """
        if authority is not None:
            self.authority = coerce_authority(SpendAuthority, authority, "spend authority")
        if actor is not None:
            self.actor = coerce_authority(ActorAuthority, actor, "actor authority")
        return self

    def bind(self, *, on_attempt: Callable[[str], None] | None = None,
             on_execution: Callable[[str, str], None] | None = None,
             on_cost_outcome: Callable[[str], None] | None = None,
             on_remote_id: Callable[[str], None] | None = None,
             on_event: Callable[[str, str, str], None] | None = None
             ) -> PaidOperation:
        """Attach the caller's row writers. Returns self so it can be chained."""
        if on_attempt is not None:
            self.on_attempt = on_attempt
        if on_execution is not None:
            self.on_execution = on_execution
        if on_cost_outcome is not None:
            self.on_cost_outcome = on_cost_outcome
        if on_remote_id is not None:
            self.on_remote_id = on_remote_id
        if on_event is not None:
            self.on_event = on_event
        return self

    def make_executor(self) -> PaidProviderExecutor:
        """A :class:`PaidProviderExecutor` bound to THIS operation.

        Only useful when the caller has a real ``submit_fn`` returning a
        ``RemoteSubmission`` -- i.e. when it is not delegating to another
        provider layer that already owns one (a chat completion, for instance,
        has its own executor inside ``llm_paid``).
        """
        return PaidProviderExecutor(
            # The resolved ledger owner, not the raw tenant id: for
            # SYSTEM_OWNED work the submission record must name the system
            # ledger owner too, or the submission and its cost row disagree
            # about who paid.
            workspace_id=(self.ownership.charged_workspace_id
                          if self.ownership else self.workspace_id),
            provider=self.provider,
            operation=self.operation,
            persist=self._persist_submission,
            audit=None,
            cost_hook=None,
            # The gate runs BEFORE the attempt is recorded and before the
            # request leaves: authorize -> record -> send.
            submit_budget=self.authorize,
            idempotency=self.idempotency,
            reconciliation=self.reconciliation,
        )

    # -- the money -------------------------------------------------------

    @property
    def entry_id(self) -> str:
        return str(getattr(self.reservation, "entry_id", "") or "")

    def llm_spend_authorization(self):
        """This operation's reservation, shaped for the LLM leg gate.

        A caller that already reserved for one exact request must hand that
        reservation to the inner per-leg gate, or the same POST gets reserved
        twice: conservative, never a leak, but it over-counts the cap and hides
        real headroom. Returns ``None`` when nothing is reserved yet, so the
        inner gate does its normal job.

        The resolved owner and actor travel WITH the reservation. Without them
        the inner leg would re-derive ownership from ``workspace_id`` alone and
        a legitimate ``SYSTEM_OWNED``/``EXPLICIT_NONBILLABLE`` operation would be
        refused by its own downstream gate.
        """
        if self.reservation is None:
            return None
        from app.engine.intelligence.llm_paid import SpendAuthorization

        entry_id = str(getattr(self.reservation, "entry_id", "") or "")
        if not entry_id:
            return None
        return SpendAuthorization(
            workspace_id=self.ownership.charged_workspace_id if self.ownership
            else self.workspace_id,
            amount_usd=float(self.estimated_cost or 0.0),
            provider=self.provider,
            category=self.category,
            reservation=self.reservation,
            authority=self.authority,
            actor=self.actor,
        )

    def authorize(self) -> None:
        """Resolve the owner, then reserve the estimate -- BEFORE anything sends.

        A refusal propagates: that is the gate doing its job. ``reserve_spend``
        decides and writes in one transaction under the workspace lock, so two
        concurrent callers cannot both be told yes for the same last dollar --
        an advisory read could, which is why this is not
        ``cost.assert_can_spend``'s default branch.

        **Work 15.9 §1: an empty ``workspace_id`` now blocks.** It used to log a
        warning and return unreserved, which sent a real billable request with
        no cap anywhere near it (localization translation did exactly this).
        The refusal is :class:`OwnerlessSpendRefused`, raised here, before the
        caller reaches its own submit -- so the money is never spent and the
        caller gets a diagnosable error rather than an unreserved POST.

        The two legitimate ways to have no tenant are declared, not inferred:
        :attr:`SpendAuthority.SYSTEM_OWNED` (needs a configured system budget)
        and :attr:`SpendAuthority.EXPLICIT_NONBILLABLE`.
        """
        if self.reservation is not None:
            return
        if not str(self.category or "").strip() and \
                self.authority is not SpendAuthority.EXPLICIT_NONBILLABLE:
            raise ValueError(
                "a paid reservation needs a category: the reservation IS the "
                "ledger row, and a row without a category cannot be counted "
                "against the budget")

        ownership = resolve_ownership(
            workspace_id=self.workspace_id, provider=self.provider,
            operation=self.operation, estimated_usd=self.estimated_cost,
            authority=self.authority, actor=self.actor)
        self.ownership = ownership

        if not ownership.billable:
            # The caller asserted this provider charges nothing. No ledger row:
            # a zero row would be indistinguishable from a lost response.
            logger.debug(
                "paid %s.%s is EXPLICIT_NONBILLABLE (actor=%s, about $%.4f); "
                "no reservation written", self.provider, self.operation,
                str(self.actor), float(self.estimated_cost or 0.0))
            return

        from app.services import cost as cost_service

        caps: dict = {}
        if ownership.authority is SpendAuthority.SYSTEM_OWNED:
            # A system row must be capped by the SYSTEM ceiling, not by
            # whatever ``settings.daily_budget_usd`` happens to be: that value
            # is a per-tenant default and using it here would let operator work
            # silently consume a tenant-shaped budget.
            cap = ownership.system_budget_usd
            caps = {"daily_cap_usd": cap, "per_call_cap_usd": cap}

        # ``reserve_spend`` and not ``assert_can_spend(reserve=True)``: the
        # reservation row must carry the operation id, because a ledger row that
        # cannot be paired with the submission it paid for is an unattributable
        # charge. ``assert_can_spend`` forwards no detail at all.
        self.reservation = cost_service.reserve_spend(
            ownership.charged_workspace_id, float(self.estimated_cost or 0.0),
            category=self.category, provider=self.provider,
            detail=reservation_detail(self, self.reservation_extra), **caps)
        self.ownership = replace(ownership, reservation_id=self.entry_id)

    def close_book(self, actual_usd: float | None = None, *,
                   estimate_usd: float | None = None, note: str = "") -> bool:
        """Settle the reservation ON ITS OWN ROW. Never a second ``track_cost``.

        ``actual_usd`` is only for a provider that REPORTED an amount and only
        when it differs from the estimate. With no reported amount the row stays
        an ESTIMATE: ``cost.settle_reservation`` would stamp it ``ACTUAL``,
        which is a claim no vendor invoice supports.

        **Work 15.9 §5: closing twice is a no-op.** A restarted worker that
        finds an already-closed book must not move the money a second time for
        one purchase, so the second call returns ``False`` without writing.
        """
        if self.reservation is None or self.settled:
            return False
        from app.services import cost as cost_service

        amount = actual_usd if actual_usd is not None else estimate_usd
        self.settled = True
        if amount is not None and abs(float(amount) - float(self.estimated_cost or 0.0)) > 1e-12:
            cost_service.settle_reservation(self.entry_id, float(amount))
            self._record_cost_outcome(CostOutcome.ACTUAL, note)
            return True
        self._record_cost_outcome(CostOutcome.ESTIMATED, note)
        return True

    def release(self, note: str = "") -> bool:
        """Delete the reservation. ONLY for a request that provably never sent.

        A cancelled-before-submit call billed nothing, so keeping its row would
        shrink the remaining budget for work that was never done. A call that may
        have reached the provider must NOT be released -- that is
        :meth:`mark_unknown`.
        """
        if self.reservation is None or self.settled:
            return False
        from app.services import cost as cost_service

        removed = cost_service.void_reservation(self.entry_id)
        if removed:
            self.reservation = None
            self.settled = True
            self._record_cost_outcome(CostOutcome.NOT_APPLICABLE, note)
        return bool(removed)

    # -- the lifecycle ---------------------------------------------------

    def mark_attempt(self, detail: str = "") -> None:
        """Durable evidence that a request is ABOUT TO leave.

        Written before the request, not after: a process that dies mid-flight
        must leave behind proof that money may already be committed.
        """
        self._emit("attempted", "info",
                   f"submitting to {self.provider}.{self.operation}")
        if self.on_attempt is not None:
            self.on_attempt(self.operation_id)

    def mark_accepted(self, remote_id: str = "", detail: str = "") -> None:
        """The provider returned a durable id: the money is committed.

        The id is written onto the reservation row as well as handed to the
        caller's writer. That second write is what makes §5's reattach
        possible at all: after a restart the caller's own row may be gone, and
        a ledger row with no remote id on it cannot be recovered even in
        principle.
        """
        if remote_id:
            self.remote_id = str(remote_id)
        self._record_execution(SubmissionState.REMOTE_ID_CONFIRMED,
                               detail or f"accepted by {self.provider}")
        if remote_id and self.on_remote_id is not None:
            self.on_remote_id(remote_id)
        if self.reservation is not None and remote_id:
            annotate_exposure(self.entry_id, self.cost_outcome,
                              detail={"remote_id": str(remote_id)})
        # NOT `close_book()`: acceptance commits the money but does not CLOSE the
        # book. The caller still has an artifact to collect (or a failure to
        # classify), and §5's "one settlement per operation" needs a settled flag
        # that means what it says. The ESTIMATE annotation still happens here --
        # it is what an operator reads while the job runs.
        self._record_cost_outcome(CostOutcome.ESTIMATED, "accepted by provider")
        self._emit("accepted", "info", f"{self.provider}.{self.operation} accepted")

    def mark_unknown(self, detail: str = "") -> None:
        """AMBIGUOUS: it may have been billed and we cannot prove it was not.

        The reservation is KEPT -- the estimate is a real bound on the exposure
        -- and marked ``UNKNOWN_EXPOSURE`` so an operator can filter for it
        without reading JSON. This never resubmits.
        """
        self.unknown_exposure = True
        self._record_execution(SubmissionState.SUBMISSION_UNKNOWN, detail)
        self._record_cost_outcome(CostOutcome.UNKNOWN_EXPOSURE, detail)
        if self.reservation is not None:
            annotate_exposure(
                self.entry_id, CostOutcome.UNKNOWN_EXPOSURE,
                detail={"operation_id": self.operation_id,
                        "remote_id": self.remote_id,
                        "unknown_exposure_detail": (detail or "")[:400]})
        self._emit("unknown", "error",
                   f"{self.provider}.{self.operation} is SUBMISSION_UNKNOWN "
                   f"(operation {self.operation_id}); money may have been spent")

    def mark_rejected(self, detail: str = "", *, nothing_billed: bool = True,
                      actual_usd: float | None = None,
                      estimate_usd: float | None = None,
                      amount_unknown: bool = False) -> None:
        """The provider definitively refused. Nothing was created, nothing billed.

        ``nothing_billed=False`` keeps the reservation instead of releasing it,
        for a provider that cannot prove a task was not created. That is
        Work 16.1 §5's ``FAILED`` observation: the provider DID create the job
        and DID charge us, and the render then failed. Treating that as
        never-billed would hand back capacity that was genuinely spent.

        With ``nothing_billed=False`` the amount is chosen the same three ways
        :meth:`mark_succeeded` chooses it, because the situation is the same --
        a reported amount, else an estimate, else ``UNKNOWN_EXPOSURE`` when the
        provider reported nothing. The arguments are accepted even when
        ``nothing_billed=True`` so a caller can pass what it knows; the
        reservation is released in that case regardless, because a refused
        request cannot have been charged.
        """
        self._record_execution(SubmissionState.FAILED, detail)
        if nothing_billed:
            self.release(f"rejected before acceptance: {detail}"[:400])
        elif amount_unknown:
            self.unknown_exposure = True
            self._record_cost_outcome(CostOutcome.UNKNOWN_EXPOSURE, detail)
        else:
            self.close_book(actual_usd, estimate_usd=estimate_usd, note=detail)
        self._emit("rejected", "warning",
                   f"{self.provider}.{self.operation} rejected: {detail}")

    def mark_cancelled(self, detail: str = "") -> None:
        """We stopped before sending. The outcome is known and nothing was billed."""
        self._record_execution(SubmissionState.CANCELLED, detail)
        self.release(f"cancelled before submission: {detail}"[:400])
        self._emit("cancelled", "warning",
                   f"{self.provider}.{self.operation} cancelled before send")

    def mark_succeeded(self, actual_usd: float | None = None, *,
                       estimate_usd: float | None = None,
                       detail: str = "", amount_unknown: bool = False) -> None:
        """The artifact exists. Close the book; never resubmit.

        Exactly one of three outcomes, and the caller has to choose:

        * ``actual_usd`` -- the provider REPORTED the amount and it differs from
          the estimate, so the row is corrected and marked ``ACTUAL``;
        * ``estimate_usd`` -- the call was metered but nobody reported an
          invoice, so the row keeps the estimate it already reserved;
        * ``amount_unknown=True`` -- the provider reported NOTHING, and the work
          is done, so the row is marked ``UNKNOWN_EXPOSURE``. This is the case
          four providers had to hand-roll, and it is the one that must never be
          spelled ``0.0``: ``cost.track_cost`` drops any amount ``<= 0``, so a
          "$0" render produces no row at all and the spend vanishes.
        """
        self._record_execution(SubmissionState.SUCCEEDED, detail)
        if amount_unknown:
            self.unknown_exposure = True
            self._record_cost_outcome(CostOutcome.UNKNOWN_EXPOSURE, detail)
        else:
            self.close_book(actual_usd, estimate_usd=estimate_usd, note=detail)
        self._emit("succeeded", "info", f"{self.provider}.{self.operation} finished")

    # -- plumbing --------------------------------------------------------

    def _record_execution(self, state: SubmissionState, detail: str = "") -> None:
        self.execution_outcome = execution_outcome_of(state)
        if self.on_execution is not None:
            self.on_execution(self.execution_outcome, detail or "")

    def _record_cost_outcome(self, outcome: CostOutcome, note: str = "") -> None:
        self.cost_outcome = outcome
        if self.on_cost_outcome is not None:
            self.on_cost_outcome(cost_outcome_of(outcome))

        if self.reservation is None:
            return
        annotate_exposure(self.entry_id, outcome,
                          detail={k: v for k, v in
                                  {"operation_id": self.operation_id,
                                   "remote_id": self.remote_id,
                                   "note": (note or "")[:400]}.items() if v})

    def _persist_submission(self, record) -> None:
        """Bridge for :meth:`make_executor`: mirror an executor record here.

        ``PaidProviderExecutor`` persists its own ``PaidSubmission``; this keeps
        the caller's row in step so a crash between the two writes still leaves
        the operation id and the remote id on disk.

        The executor's own UNKNOWN_EXPOSURE verdict is mirrored onto the
        reservation row too. Without that mirror an ambiguity detected inside
        ``execute()`` would leave the money row looking like an ordinary open
        reservation -- the exact "lost response booked as nothing" failure this
        module exists to prevent, arriving through the new door.
        """
        state = getattr(record, "state", "")
        self._record_execution(execution_outcome_of(state),
                               str(getattr(record, "detail", "") or ""))
        remote_id = str(getattr(record, "remote_id", "") or "")
        if remote_id:
            self.remote_id = remote_id
            if self.on_remote_id is not None:
                self.on_remote_id(remote_id)
        self.operation_id = str(getattr(record, "submission_id", "") or
                                self.operation_id)
        cost = getattr(record, "cost", None)
        if cost is not None and getattr(cost, "outcome", None) is \
                CostOutcome.UNKNOWN_EXPOSURE and not _provably_undelivered(record):
            self.unknown_exposure = True
            self._record_cost_outcome(CostOutcome.UNKNOWN_EXPOSURE,
                                      str(getattr(record, "detail", "") or ""))

    def _emit(self, phase: str, level: str, message: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(phase, level, message)
            except Exception as exc:  # noqa: BLE001 - telemetry never fails a call
                logger.debug("paid %s.%s event dropped: %s", self.provider,
                             self.operation, exc)
            return
        logger.log(level.upper(), "paid {}.{} [{}] {}: {}",
                   self.provider, self.operation, self.operation_id, phase, message)


def paid_operation(*, provider: str, operation: str, workspace_id: str = "",
                   category: str = "", estimated_cost: float = 0.0,
                   idempotency: IdempotencySupport = IdempotencySupport.UNVERIFIED,
                   reconciliation: Reconciliation = Reconciliation.RECONCILE,
                   reservation_extra: dict | None = None) -> PaidOperation:
    """One billable operation. See :class:`PaidOperation` for the lifecycle.

    This is the factory the four duplicated provider wirings should share. It
    deliberately takes no request-building arguments: what a request looks like,
    what its errors mean and whether the vendor honours an idempotency key are
    provider facts, and a helper that accepted them would be the "giant generic
    abstraction" this extraction exists to avoid.

    It takes no authority argument either, and that is deliberate rather than an
    oversight. A factory default of :attr:`SpendAuthority.WORKSPACE_OWNED` is
    the only default that can refuse, so a caller who declares nothing gets a
    gate; the two exceptions are rare enough to be worth writing out at the call
    site via :meth:`PaidOperation.declared`, where they are visible in review
    instead of hidden in a signature default.
    """
    return PaidOperation(
        provider=provider,
        operation=operation,
        workspace_id=workspace_id or "",
        category=category or "",
        estimated_cost=float(estimated_cost or 0.0),
        idempotency=idempotency,
        reconciliation=reconciliation,
        reservation_extra=dict(reservation_extra or {}),
    )
