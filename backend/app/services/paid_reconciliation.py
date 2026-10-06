"""Durable reconciliation of an ambiguous paid submission (Work 16.1 §5).

The defect this exists to close
-------------------------------
``paid_executor.reconcile_submission`` took a :class:`PaidSubmission`, mutated the
**Python object** and logged. It never opened a session and never committed, so
an operator who reconciled an ambiguous paid submission lost the outcome the
moment the process exited -- and the database went on saying
``SUBMISSION_UNKNOWN`` forever. Combined with the Work 15.7-15.9 invariant::

    at-most-one automatic paid submit + exactly-one YMONEY accounting identity

an ambiguity nobody can resolve **durably** is an unbounded money exposure: the
next operator sees the same row, cannot tell whether it was already settled, and
the honest-looking answer ("look it up again") is the one that double-charges.

So reconciliation here is a database transaction, not a mutation::

    SUBMISSION_UNKNOWN
      -> operator/provider lookup        (the provider's dashboard)
      -> reconcile                       (an OBSERVED FACT, named)
      -> persistent execution outcome     (Video.submission_state /
                                          LipSyncJob.execution_outcome)
      -> persistent cost outcome          (Video.cost_outcome /
                                          LipSyncJob.cost_outcome /
                                          CostEntry.detail_json.cost_outcome)
      -> audit event                      (paid.reconciliation, in ``events``)

Every step survives a restart. Reading any of it back through the same
``Session`` would prove nothing, so the tests read through a **fresh engine**.

What is written, and where
--------------------------
There is no new table. The three lanes already own canonical columns for exactly
these facts, and a fourth store would be a fourth spelling:

=================  ==================================  ===========================
lane               execution + money                   remote handle
=================  ==================================  ===========================
render (``Video``) ``submission_state``, ``cost_outcome`` ``provider_task_id``
lip-sync (job)     ``execution_outcome``,               ``adapter_job_id``
                   ``cost_outcome``
ledger (all lanes) ``detail_json.cost_outcome``,        ``detail_json.remote_id``
                   ``detail_json.exposure_unknown``
=================  ==================================  ===========================

``CostEntry`` is written on EVERY path, including the lip-sync lane that has no
reservation row at all -- an operator's first question ("what did the books say
about this?") must not depend on which lane produced the ambiguity.

Reuse, not reinvention
----------------------
The money is moved by the SHIPPED :class:`~app.services.paid_provider.
PaidOperation` methods -- ``mark_accepted`` / ``mark_rejected`` / ``mark_succeeded``
/ ``mark_unknown`` -- bound to the canonical record through the ``on_execution``
/ ``on_cost_outcome`` / ``on_remote_id`` writers. Those methods already own
``settle_reservation`` / ``void_reservation`` and already guard themselves with
``PaidOperation.settled``. This module decides WHICH of them an observation
means; it never writes an amount itself.

Why the four old ``Reconciliation`` actions do NOT move money
--------------------------------------------------------------
:data:`~app.services.paid_executor.Reconciliation` is a vocabulary of **remedies**
("what should the system do next"), not of **observations** ("what did the
provider do"). ``RECONCILE`` explicitly means *still unconfirmed*, and the runbook
is explicit that it must leave the state alone. A remedy label is not evidence
that a charge did or did not happen, so
:func:`~app.services.paid_executor.reconcile_submission` records the decision
durably (canonical row + audit event) and leaves the books exactly where they
were. Money moves only through :func:`reconcile_paid_submission`, which requires
an observed :class:`ReconciliationOutcome` and an operator.

The operation identity
----------------------
The idempotency key of a reconciliation is the **operation id** -- the canonical
``PaidSubmission.submission_id``, which is already persisted as
``Video.submission_operation_id``, ``LipSyncJob.cost_json['submission_id']`` and
``CostEntry.detail_json['operation_id']``. Two layers use it:

1. **The decision guard.** A ``paid.reconciliation`` event is written for every
   applied observation, so "has THIS outcome already been applied to THIS
   operation?" is a durable query against the audit trail, and applying the same
   outcome twice raises :class:`ReconciliationRefused`. Different outcomes
   (``UNKNOWN_REMAINS`` then, later, ``SUCCEEDED``) are different decisions and
   are allowed -- a decision that blocks forever is not a guard, it is a lock.
2. **The money guard.** ``PaidOperation.settled`` is read back from the ledger
   row's ``cost_outcome`` by ``adopt_reservation`` / ``reattach_by_remote_id``,
   so even a forced second call cannot settle a closed reservation twice.

The guard is the audit, on purpose: a reconciliation that leaves no trace is how
a real duplicate charge becomes unexplainable three weeks later, so the thing
that must exist for correctness is the thing the runbook demands for audit.

Ordering, and what an interrupted reconciliation leaves
-------------------------------------------------------
Canonical record -> ledger -> event. Each step is idempotent on its own (the
record is set, not appended; ``settle_reservation`` corrects a row rather than
inserting one; the event is guarded), so a process that dies half way leaves the
money either moved correctly once or not at all, and **re-running the identical
command completes it**. The event is last precisely because it is the guard:
an event that claimed a decision nobody applied would be worse than no event.

Operator entry point
--------------------
``python -m app.services.paid_reconciliation --help``. No REST API is invented:
the incident runbook's honest procedure is a provider-dashboard lookup plus
``reattach_by_remote_id``, and this makes THAT path persist.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from loguru import logger

from app.services.paid_executor import CostOutcome, Reconciliation
from app.services.paid_jobs import SubmissionState
from app.services.paid_provider import PaidOperation, adopt_reservation

__all__ = [
    "COST_OUTCOMES",
    "RECONCILIATION_KIND",
    "SUBMISSION_STATES",
    "MoneyEffect",
    "ReconciliationOutcome",
    "ReconciliationRefused",
    "ReconciliationResult",
    "ReconciliationSubject",
    "SubjectKind",
    "assert_never_resubmits",
    "ledger_state",
    "pending_submissions",
    "reconcile_paid_submission",
    "record_reconciliation",
    "subject_for_lipsync_job",
    "subject_for_operation",
    "subject_for_video",
]

#: The audit-event kind. Also the durable decision record: the guard reads it.
RECONCILIATION_KIND = "paid.reconciliation"
_EVENT_SOURCE = "paid.reconcile"

#: Derived from the vocabularies that already own them. A typo'd outcome would
#: be permanently un-queryable, which is why these are the enum members rather
#: than free strings accepted anywhere in this module.
SUBMISSION_STATES: tuple[str, ...] = tuple(str(s) for s in SubmissionState)
COST_OUTCOMES: tuple[str, ...] = tuple(str(o) for o in CostOutcome)

#: The ledger ``cost_outcome`` values that mean the row's money has been DECIDED.
#:
#: ``UNKNOWN_EXPOSURE`` is deliberately NOT in it, and that is the whole subtlety
#: of this module. ``paid_provider._CLOSED_OUTCOMES`` includes it, which is right
#: for a REATTACH: a recovered remote job must not be settled a second time. But
#: an unknown exposure is not settled money -- it is UNPRICED money. A row
#: carrying it is precisely the one an operator still has to resolve, so
#: treating it as closed here would make the answer to "the provider finally
#: told me the price" permanently unreachable, and the exposure would stay open
#: forever. Reconciliation is the path that turns ``UNKNOWN_EXPOSURE`` into a
#: real amount, so it must be the one path allowed to.
_DECIDED_LEDGER_OUTCOMES = frozenset({str(CostOutcome.NOT_APPLICABLE),
                                      str(CostOutcome.ACTUAL),
                                      str(CostOutcome.ESTIMATED)})

#: Bounded scan, for the same reason ``paid_provider._ATTACH_SCAN_LIMIT`` exists:
#: this runs on an incident path that is rare by construction, and it must
#: behave identically on SQLite and on a server-grade engine.
_SCAN_LIMIT = 500

#: Marks the reconciliation tail inside ``Video.submission_detail``. Everything
#: after it is re-derivable, so a re-run replaces it instead of appending again.
_RECON_MARK = "reconciled:"


class ReconciliationOutcome(StrEnum):
    """What the operator FOUND on the provider's dashboard.

    An observation, not a remedy -- that is the distinction this module exists to
    keep sharp, and it is why the four ``Reconciliation`` remedies cannot move
    money (see the module docstring).

    ``SUCCEEDED`` and ``FAILED`` deliberately share their spelling with
    :class:`~app.services.paid_jobs.SubmissionState`: they ARE those execution
    outcomes, confirmed by a human. Inventing a second vocabulary is how two
    dashboards end up disagreeing.
    """

    #: The provider created the job; the remote id was discovered and persisted.
    #: The work still has to finish, so the reservation STANDS.
    REMOTE_JOB_CONFIRMED = "REMOTE_JOB_CONFIRMED"
    #: The provider definitively did not create anything. Capacity comes back,
    #: exactly once.
    NOT_ACCEPTED = "NOT_ACCEPTED"
    #: The remote job finished and its artifact exists.
    SUCCEEDED = "SUCCEEDED"
    #: The remote job was created, was billed, and then failed.
    FAILED = "FAILED"
    #: The provider cannot be asked, or cannot answer. The honest terminal state:
    #: the exposure stays RESERVED and the row stays SUBMISSION_UNKNOWN.
    UNKNOWN_REMAINS = "UNKNOWN_REMAINS"


class MoneyEffect(StrEnum):
    """What this reconciliation did to the reservation, derived from the row.

    Not a return code from the call that did it: read back from the ledger before
    and after, because "we called release()" and "the reservation is gone" are
    different claims and only one of them is evidence.
    """

    #: The reservation row was voided. The workspace's cap is back.
    CAPACITY_RELEASED = "CAPACITY_RELEASED"
    #: The estimate still stands as the booked money. Nothing moved.
    RESERVATION_KEPT = "RESERVATION_KEPT"
    #: The row was corrected to a REPORTED amount, in place.
    RESERVATION_CORRECTED = "RESERVATION_CORRECTED"
    #: There is no reservation row: this lane never wrote one, or it is gone.
    NO_LEDGER_ROW = "NO_LEDGER_ROW"
    #: The row was ALREADY closed when we arrived. Nothing was moved -- which is
    #: the exactly-once accounting guarantee, not a failure.
    NOTHING_TO_MOVE = "NOTHING_TO_MOVE"


class SubjectKind(StrEnum):
    """WHOSE row carries the facts. The three lanes have three shapes."""

    VIDEO = "VIDEO"
    LIPSYNC = "LIPSYNC"
    #: No lane row found; the ledger row is the whole record.
    LEDGER_ONLY = "LEDGER_ONLY"


class ReconciliationRefused(RuntimeError):
    """A reconciliation this path will not apply, and why.

    Refusing loudly is the point. Every reason here is a case where applying the
    decision would either fabricate a fact or move money twice.
    """

    def __init__(self, detail: str, *, reason: str = "REFUSED") -> None:
        self.reason = str(reason)
        super().__init__(f"[{self.reason}] {detail}")


@dataclass(frozen=True)
class ReconciliationSubject:
    """Everything the durable write needs, read fresh from the database.

    A frozen snapshot of one subject, taken by a ``lookup_*`` helper. It carries
    no session: it is what an operator tool reads, prints and hands to
    :func:`reconcile_paid_submission`, and reading it must not hold a
    transaction open while a human thinks.
    """

    kind: SubjectKind
    record_id: str
    workspace_id: str
    #: The canonical ``PaidSubmission.submission_id``. Required: it is the
    #: idempotency key, and a submission that cannot be identified cannot be
    #: reconciled without guessing.
    operation_id: str = ""
    provider: str = ""
    operation: str = ""
    cost_entry_id: str = ""
    remote_id: str = ""
    estimated_usd: float = 0.0
    #: What the canonical row says right now, for the operator's benefit.
    state: str = ""
    cost_outcome: str = ""

    @property
    def ambiguous(self) -> bool:
        """Whether this submission still claims it may have been billed."""
        return (str(self.state) == str(SubmissionState.SUBMISSION_UNKNOWN)
                or str(self.cost_outcome) == str(CostOutcome.UNKNOWN_EXPOSURE))

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "record_id": self.record_id,
            "workspace_id": self.workspace_id,
            "operation_id": self.operation_id,
            "provider": self.provider,
            "operation": self.operation,
            "cost_entry_id": self.cost_entry_id,
            "remote_id": self.remote_id,
            "estimated_usd": round(float(self.estimated_usd or 0.0), 6),
            "state": self.state,
            "cost_outcome": self.cost_outcome,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """What was persisted, read back rather than assumed."""

    outcome: ReconciliationOutcome
    subject: ReconciliationSubject
    execution_outcome: str
    cost_outcome: str
    money_effect: MoneyEffect
    remote_id: str
    operator: str
    note: str
    at: datetime
    already_applied: bool = False

    def to_dict(self) -> dict:
        return {
            "outcome": str(self.outcome),
            "subject": self.subject.to_dict(),
            "execution_outcome": self.execution_outcome,
            "cost_outcome": self.cost_outcome,
            "money_effect": str(self.money_effect),
            "remote_id": self.remote_id,
            "operator": self.operator,
            "note": self.note,
            "at": self.at.isoformat(),
            "already_applied": self.already_applied,
        }


# ---------------------------------------------------------------------------
# What each OBSERVATION means
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reading:
    """One outcome translated into the two structural facts plus the money rule.

    ``execution`` is a :class:`~app.services.paid_jobs.SubmissionState` spelling
    and ``cost`` a :class:`~app.services.paid_executor.CostOutcome` spelling --
    both validated against the canonical vocabularies before a write, because a
    typo in a *filterable* column is permanently invisible to the queries that
    matter.
    """

    execution: str
    cost: str
    #: Whether a discovered remote id is REQUIRED (a confirmed job with no
    #: handle cannot be reconciled, polled or adopted by anything, ever).
    requires_remote_id: bool = False
    #: Whether a remote id would contradict the observation.
    rejects_remote_id: bool = False
    #: Whether the exposure must stay an unknown one.
    keeps_unknown_exposure: bool = False


_UNKNOWN = str(SubmissionState.SUBMISSION_UNKNOWN)
_CONFIRMED = str(SubmissionState.REMOTE_ID_CONFIRMED)
_FAILED = str(SubmissionState.FAILED)
_SUCCEEDED = str(SubmissionState.SUCCEEDED)
_NA = str(CostOutcome.NOT_APPLICABLE)
_ACTUAL = str(CostOutcome.ACTUAL)
_ESTIMATED = str(CostOutcome.ESTIMATED)
_UNKNOWN_EXPOSURE = str(CostOutcome.UNKNOWN_EXPOSURE)

_READINGS: dict[ReconciliationOutcome, _Reading] = {
    # The job exists and is addressable. The estimate still bounds the exposure
    # and nothing is settled, because the work has not finished yet.
    ReconciliationOutcome.REMOTE_JOB_CONFIRMED: _Reading(
        execution=_CONFIRMED, cost=_ESTIMATED, requires_remote_id=True),
    # The provider created nothing, so nothing was billed and the cap comes back.
    # A remote id here would be a contradiction, not extra information.
    ReconciliationOutcome.NOT_ACCEPTED: _Reading(
        execution=_FAILED, cost=_NA, rejects_remote_id=True),
    # Finished. The money is settled; the amount is the reported one when the
    # provider gave a number, the estimate otherwise, and unknown when the
    # provider reported nothing at all (resolved at call time).
    ReconciliationOutcome.SUCCEEDED: _Reading(execution=_SUCCEEDED, cost=_ACTUAL),
    # Created, billed, then failed. A failed render is normally still metered,
    # so this settles rather than releases.
    ReconciliationOutcome.FAILED: _Reading(execution=_FAILED, cost=_ACTUAL),
    # Nothing was learned. This is the only outcome that moves no money at all,
    # and the only one that may leave an UNKNOWN_EXPOSURE standing.
    ReconciliationOutcome.UNKNOWN_REMAINS: _Reading(
        execution=_UNKNOWN, cost=_UNKNOWN_EXPOSURE, keeps_unknown_exposure=True),
}


def _scope():
    """One transaction for one durable write.

    The single commit point of this module, so every write below is genuinely
    durable and not one of them is accidentally durable. Imported lazily because
    that is how every other service in this repository reaches ``app.db`` (it
    keeps the session factory swappable and keeps import cycles out).
    """
    from app.db import session_scope

    return session_scope()


# ---------------------------------------------------------------------------
# Durable reads
# ---------------------------------------------------------------------------


def ledger_state(cost_entry_id: str) -> dict | None:
    """The ledger row's money facts, or ``None`` when there is no such row.

    ``None`` is an honest reading of "nothing to move": either this lane never
    wrote a reservation (lip-sync books its spend on the job row) or the row is
    gone. It is never a reason to invent a replacement row.
    """
    if not str(cost_entry_id or "").strip():
        return None
    from app.models import CostEntry

    with _scope() as session:
        row = session.get(CostEntry, str(cost_entry_id).strip())
        if row is None:
            return None
        detail = dict(row.detail_json or {})
        return {
            "amount_usd": float(row.amount_usd or 0.0),
            "is_estimate": bool(row.is_estimate),
            "cost_outcome": str(detail.get("cost_outcome", "") or ""),
            "remote_id": str(detail.get("remote_id", "") or ""),
            # "DECIDED", not "closed": see _DECIDED_LEDGER_OUTCOMES. An
            # UNKNOWN_EXPOSURE row is unpriced, not settled, and reconciliation
            # is exactly the path that prices it.
            "decided": str(detail.get("cost_outcome", "") or "")
            in _DECIDED_LEDGER_OUTCOMES,
        }


def _entry_for_operation(operation_id: str, *, workspace_id: str = "",
                         provider: str = "") -> str:
    """The newest ledger row this operation owns, or ``""``.

    The lookup goes through :func:`json_portability.json_value_equals` rather
    than a bare ``detail_json['operation_id'].as_string()``. That is not
    cosmetic: the bare form compiles to a CAST on PostgreSQL (so a numeric JSON
    value matches) and to an uncast ``JSON_EXTRACT`` on SQLite (so it does not),
    and a reconciliation that silently finds no row on the backend most
    deployments run is a reconciliation that quietly does nothing.
    """
    if not str(operation_id or "").strip():
        return ""
    from sqlalchemy import select

    from app.models import CostEntry
    from app.services.json_portability import json_value_equals

    with _scope() as session:
        query = (select(CostEntry)
                 .where(json_value_equals(CostEntry.detail_json, "operation_id",
                                          str(operation_id).strip()))
                 .order_by(CostEntry.created_at.desc())
                 .limit(_SCAN_LIMIT))
        if str(workspace_id or "").strip():
            query = query.where(CostEntry.workspace_id == str(workspace_id).strip())
        if str(provider or "").strip():
            query = query.where(CostEntry.provider == str(provider).strip())
        row = session.scalars(query).first()
    return str(row.id) if row is not None else ""


def subject_for_video(video_id: str) -> ReconciliationSubject:
    """The render lane's subject, read fresh."""
    from app.models import Video

    vid = str(video_id or "").strip()
    if not vid:
        raise ReconciliationRefused("a video id is required", reason="NO_SUBJECT")
    with _scope() as session:
        row = session.get(Video, vid)
        if row is None:
            raise ReconciliationRefused(
                f"no video row {vid!r}", reason="UNKNOWN_SUBMISSION")
        operation_id = str(row.submission_operation_id or "")
        subject = ReconciliationSubject(
            kind=SubjectKind.VIDEO, record_id=str(row.id),
            workspace_id=str(row.workspace_id or ""),
            operation_id=operation_id, provider=str(row.engine or ""),
            operation="video_render_submit",
            remote_id=str(row.provider_task_id or ""),
            state=str(row.submission_state or ""),
            cost_outcome=str(row.cost_outcome or ""))
    return replace(subject, cost_entry_id=_entry_for_operation(
        operation_id, workspace_id=subject.workspace_id))


def subject_for_lipsync_job(job_id: str) -> ReconciliationSubject:
    """The lip-sync lane's subject, read fresh.

    ``operation_id`` comes from ``cost_json['submission_id']``, which is where
    the lip-sync worker records the canonical ``PaidSubmission.submission_id``
    when it writes ``SUBMISSION_UNKNOWN`` -- the branch this module exists for.
    """
    from app.models import LipSyncJob

    jid = str(job_id or "").strip()
    if not jid:
        raise ReconciliationRefused("a lipsync job id is required", reason="NO_SUBJECT")
    with _scope() as session:
        row = session.get(LipSyncJob, jid)
        if row is None:
            raise ReconciliationRefused(
                f"no lipsync_jobs row {jid!r}", reason="UNKNOWN_SUBMISSION")
        cost = dict(row.cost_json or {})
        operation_id = str(cost.get("submission_id", "") or "")
        subject = ReconciliationSubject(
            kind=SubjectKind.LIPSYNC, record_id=str(row.id),
            workspace_id=str(row.workspace_id or ""),
            operation_id=operation_id, provider=str(row.provider or ""),
            operation="lipsync.submit",
            remote_id=str(row.adapter_job_id or ""),
            state=str(row.execution_outcome
                      or cost.get("submission_state", "") or ""),
            cost_outcome=str(row.cost_outcome or cost.get("exposure", "") or ""))
    return replace(subject, cost_entry_id=_entry_for_operation(
        operation_id, workspace_id=subject.workspace_id))


def subject_for_operation(operation_id: str) -> ReconciliationSubject:
    """Find the subject by its operation id -- the id an incident list shows.

    Tries the lane rows before the ledger, because the lane row is the one the
    operator wants to see updated; falls back to the ledger when the lane row
    died with the process that made it.
    """
    op_id = str(operation_id or "").strip()
    if not op_id:
        raise ReconciliationRefused("an operation id is required", reason="NO_SUBJECT")
    from sqlalchemy import select

    from app.models import CostEntry, LipSyncJob, Video
    from app.services.json_portability import json_value_equals

    with _scope() as session:
        video = session.scalars(
            select(Video).where(Video.submission_operation_id == op_id)
            .limit(1)).first()
    if video is not None:
        return subject_for_video(str(video.id))

    with _scope() as session:
        job = session.scalars(
            select(LipSyncJob).where(
                json_value_equals(LipSyncJob.cost_json, "submission_id", op_id))
            .limit(1)).first()
    if job is not None:
        return subject_for_lipsync_job(str(job.id))

    entry_id = _entry_for_operation(op_id)
    if not entry_id:
        raise ReconciliationRefused(
            f"no lane row and no ledger row carry operation {op_id!r}",
            reason="UNKNOWN_SUBMISSION")
    detail = session_detail(entry_id)
    state = ledger_state(entry_id)
    if detail is None or state is None:
        raise ReconciliationRefused(
            f"ledger row {entry_id} for operation {op_id!r} vanished mid-read",
            reason="UNKNOWN_SUBMISSION")
    with _scope() as session:
        row = session.get(CostEntry, entry_id)
        if row is None:  # pragma: no cover - handled above; kept honest
            raise ReconciliationRefused(
                f"ledger row {entry_id} vanished mid-read",
                reason="UNKNOWN_SUBMISSION")
        return ReconciliationSubject(
            kind=SubjectKind.LEDGER_ONLY, record_id=str(row.id),
            workspace_id=str(row.workspace_id or ""), operation_id=op_id,
            provider=str(row.provider or ""),
            operation=str(detail.get("operation", "") or ""),
            cost_entry_id=entry_id, remote_id=state["remote_id"],
            estimated_usd=state["amount_usd"], state=_UNKNOWN,
            cost_outcome=state["cost_outcome"])


def session_detail(entry_id: str) -> dict | None:
    """The ledger row's ``detail_json``, or ``None``."""
    if not str(entry_id or "").strip():
        return None
    from app.models import CostEntry

    with _scope() as session:
        row = session.get(CostEntry, str(entry_id).strip())
        return dict(row.detail_json or {}) if row is not None else None


def pending_submissions(workspace_id: str, limit: int = 100) -> list[dict]:
    """Every submission in this workspace that may already have been billed.

    The operator's worklist, and it is three WHERE clauses rather than a scan:
    "the submit is unconfirmed" (``SUBMISSION_UNKNOWN``) and "the amount is
    unknown" (``UNKNOWN_EXPOSURE``) are SEPARATE assertions, and widening one to
    the other is how a worklist stops being trustworthy.

    The lip-sync lane is queried twice on purpose. ``engine/lipsync/worker.py``
    writes ``SUBMISSION_UNKNOWN`` into ``cost_json`` (it does not own the
    ``execution_outcome`` column), so a worklist that read only the column would
    show an empty workspace for every real ambiguity in that lane.
    """
    from sqlalchemy import or_, select

    from app.models import CostEntry, LipSyncJob, Video
    from app.services.json_portability import json_value_equals

    wid = str(workspace_id or "").strip()
    cap = min(int(limit or 100), 500)
    if not wid:
        return []
    pending: list[dict] = []

    with _scope() as session:
        for row in session.execute(
                select(Video.id, Video.engine, Video.submission_state,
                       Video.cost_outcome, Video.provider_task_id,
                       Video.submission_operation_id, Video.created_at)
                .where(Video.workspace_id == wid,
                       or_(Video.submission_state == _UNKNOWN,
                           Video.cost_outcome == _UNKNOWN_EXPOSURE))
                .order_by(Video.created_at.desc()).limit(cap)).all():
            pending.append({
                "kind": str(SubjectKind.VIDEO), "record_id": row[0],
                "provider": row[1] or "", "state": row[2] or "",
                "cost_outcome": row[3] or "", "remote_id": row[4] or "",
                "operation_id": row[5] or "",
                "created_at": row[6].isoformat() + "Z" if row[6] else None})

    with _scope() as session:
        query = (select(LipSyncJob.id, LipSyncJob.provider,
                        LipSyncJob.execution_outcome, LipSyncJob.cost_outcome,
                        LipSyncJob.adapter_job_id, LipSyncJob.cost_json,
                        LipSyncJob.created_at)
                 .where(LipSyncJob.workspace_id == wid,
                        or_(LipSyncJob.execution_outcome == _UNKNOWN,
                            LipSyncJob.cost_outcome == _UNKNOWN_EXPOSURE,
                            json_value_equals(LipSyncJob.cost_json,
                                              "submission_state", _UNKNOWN)))
                 .order_by(LipSyncJob.created_at.desc()).limit(cap))
        for row in session.execute(query).all():
            cost = dict(row[5] or {})
            pending.append({
                "kind": str(SubjectKind.LIPSYNC), "record_id": row[0],
                "provider": row[1] or "",
                "state": row[2] or cost.get("submission_state", "") or "",
                "cost_outcome": row[3] or cost.get("exposure", "") or "",
                "remote_id": row[4] or cost.get("remote_id", "") or "",
                "operation_id": str(cost.get("submission_id", "") or ""),
                "created_at": row[6].isoformat() + "Z" if row[6] else None})

    with _scope() as session:
        query = (select(CostEntry.id, CostEntry.provider, CostEntry.amount_usd,
                        CostEntry.detail_json, CostEntry.created_at)
                 .where(CostEntry.workspace_id == wid,
                        json_value_equals(CostEntry.detail_json, "cost_outcome",
                                          _UNKNOWN_EXPOSURE))
                 .order_by(CostEntry.created_at.desc()).limit(cap))
        for row in session.execute(query).all():
            detail = dict(row[3] or {})
            pending.append({
                "kind": str(SubjectKind.LEDGER_ONLY), "record_id": row[0],
                "provider": row[1] or "", "state": _UNKNOWN,
                "cost_outcome": _UNKNOWN_EXPOSURE,
                "remote_id": str(detail.get("remote_id", "") or ""),
                "operation_id": str(detail.get("operation_id", "") or ""),
                "estimated_usd": round(float(row[2] or 0.0), 6),
                "created_at": row[4].isoformat() + "Z" if row[4] else None})
    return pending[:cap]


# ---------------------------------------------------------------------------
# The decision guard -- which is also the audit trail
# ---------------------------------------------------------------------------


def _applied_before(subject: ReconciliationSubject,
                    outcome: ReconciliationOutcome) -> str:
    """The operator who already applied THIS outcome to THIS operation, or "".

    The audit event is the record, so the guard costs nothing extra and cannot
    drift from it: a decision is "applied" exactly when its trace exists. The
    trade is stated rather than hidden -- this is a read-then-write with no unique
    constraint behind it, so two operators racing the SAME observation can both
    write an event. They cannot both MOVE money, though: the second call adopts
    the ledger row, reads ``settled`` back off it, and every money path refuses
    to run against a closed reservation.
    """
    from sqlalchemy import select

    from app.models import EventLog
    from app.services.json_portability import json_value_equals

    with _scope() as session:
        query = (select(EventLog.data_json)
                 .where(EventLog.kind == RECONCILIATION_KIND,
                        json_value_equals(EventLog.data_json, "outcome",
                                          str(outcome)),
                        json_value_equals(EventLog.data_json, "operation_id",
                                          subject.operation_id))
                 .order_by(EventLog.created_at.desc()).limit(1))
        if subject.workspace_id:
            query = query.where(EventLog.workspace_id == subject.workspace_id)
        row = session.scalars(query).first()
    if row is None:
        return ""
    return str(dict(row or {}).get("operator", "") or "")


def _emit(kind: str, subject: ReconciliationSubject, message: str, *,
          level: str, data: dict) -> None:
    """One durable activity-feed line. Best-effort; a feed outage is not an
    incident, but a lost reconciliation trace would be."""
    try:
        from app.services.events import record_event

        record_event(subject.workspace_id or None, kind=kind, message=message,
                     level=level, source=_EVENT_SOURCE,
                     data={"subject_kind": str(subject.kind),
                           "subject_id": subject.record_id,
                           "cost_entry_id": subject.cost_entry_id,
                           "workspace_id": subject.workspace_id,
                           "provider": subject.provider,
                           "operation": subject.operation,
                           "operation_id": subject.operation_id,
                           **data})
    except Exception as exc:  # noqa: BLE001 - telemetry must not lose the decision
        logger.error("paid reconciliation %s for %s not written to the feed: %s",
                     kind, subject.operation_id, exc)


def _event_level(outcome: ReconciliationOutcome) -> str:
    if outcome is ReconciliationOutcome.UNKNOWN_REMAINS:
        return "error"
    if outcome in (ReconciliationOutcome.NOT_ACCEPTED,
                   ReconciliationOutcome.FAILED):
        return "warning"
    return "info"


def record_reconciliation(subject: ReconciliationSubject, action: Reconciliation,
                          *, operator: str, note: str = "") -> dict:
    """Persist an operator's DECISION without touching the books.

    This is what :func:`app.services.paid_executor.reconcile_submission`
    delegates to. A ``Reconciliation`` remedy says what the system should do
    next, not what the provider did, so the money stays exactly where it was and
    the canonical row records who decided and why. An exposure is never erased by
    recording a decision about it.
    """
    who = str(operator or "").strip()
    if not who:
        raise ReconciliationRefused(
            "a reconciliation decision must name an operator",
            reason="NO_OPERATOR")
    if not str(subject.operation_id or "").strip():
        raise ReconciliationRefused(
            f"subject {subject.record_id} carries no operation id, so a decision "
            "about it could not be identified later",
            reason="NO_OPERATION_ID")
    stamp = datetime.now(UTC)
    payload = {"action": str(action), "operator": who, "note": str(note or "")[:400],
               "at": stamp.isoformat(), "moved_money": False}
    _write_audit_note(subject, f"{_RECON_MARK} action={action} operator={who}"
                               + (f" note={note}" if note else "")
                               + " money=unchanged")
    _emit(RECONCILIATION_KIND, subject,
          f"paid submission {subject.operation_id}: decision {action} recorded "
          f"by {who}; the books are unchanged",
          level="info", data={"action": str(action), **payload})
    return {"kind": str(SubjectKind(subject.kind)), "action": str(action),
            "operator": who, "note": note, "at": stamp.isoformat(),
            "moved_money": False, "subject": subject.to_dict()}


# ---------------------------------------------------------------------------
# Canonical-record writers
# ---------------------------------------------------------------------------


def _video_detail(previous: str, note: str) -> str:
    """``Video.submission_detail`` rewritten deterministically.

    The column is 600 characters and is the ONLY audit field the render lane has
    on its own row, so it has to be re-derivable rather than appended to: a
    reconciliation tail that grows on every re-run turns a bounded column into a
    lie. The pre-reconciliation text is kept (trimmed) and everything from the
    marker onwards is replaced, so applying the same decision twice produces the
    same row.
    """
    head = str(previous or "").split(f" | {_RECON_MARK}", 1)[0]
    tail = f"{_RECON_MARK} {note}"
    room = max(600 - (len(tail) + 3), 0)
    head = head[:room]
    return (f"{head} | {tail}" if head else tail)[:600]


def _write_video(subject: ReconciliationSubject, *, execution: str, cost: str,
                 remote_id: str, note: str) -> None:
    """The render lane's two canonical columns, plus the discovered handle."""
    if execution not in SUBMISSION_STATES:
        raise ReconciliationRefused(
            f"{execution!r} is not a SubmissionState", reason="BAD_EXECUTION")
    if cost not in COST_OUTCOMES:
        raise ReconciliationRefused(
            f"{cost!r} is not a CostOutcome", reason="BAD_COST_OUTCOME")
    from app.models import Video

    stamp = note or f"{execution} by {subject.operation_id}"
    with _scope() as session:
        row = session.get(Video, subject.record_id)
        if row is None:
            raise ReconciliationRefused(
                f"video {subject.record_id} vanished mid-reconciliation",
                reason="UNKNOWN_SUBMISSION")
        row.submission_state = execution
        row.cost_outcome = cost
        row.submission_detail = _video_detail(row.submission_detail, stamp)
        if remote_id:
            # The handle EVERY later step needs. A billed job nobody can address
            # is an invoice nobody can reconcile.
            row.provider_task_id = str(remote_id)[:160]


def _write_lipsync(subject: ReconciliationSubject, *, execution: str, cost: str,
                   remote_id: str, note: str, decision: dict) -> None:
    """The lip-sync lane's canonical columns plus the ``cost_json`` the worker
    already writes, so neither reader has to learn a second spelling."""
    from app.engine.lipsync import rows as job_rows
    from app.models import LipSyncJob

    with _scope() as session:
        row = session.get(LipSyncJob, subject.record_id)
        if row is None:
            raise ReconciliationRefused(
                f"lipsync job {subject.record_id} vanished mid-reconciliation",
                reason="UNKNOWN_SUBMISSION")
        cost_json = dict(row.cost_json or {})
        cost_json["submission_state"] = execution
        cost_json["exposure"] = cost
        # ``resubmit_forbidden`` is the worker's own flag and it is never cleared
        # here: reconciliation is a human decision about MONEY, not a licence for
        # the queue to buy the work a second time.
        cost_json["resubmit_forbidden"] = True
        cost_json["reconciliation"] = dict(decision)
        if remote_id:
            cost_json["remote_id"] = str(remote_id)[:80]
            row.adapter_job_id = str(remote_id)[:80]
        row.cost_json = cost_json
    if not job_rows.set_paid_outcomes(subject.record_id,
                                      execution_outcome=execution,
                                      cost_outcome=cost):
        raise ReconciliationRefused(
            f"lipsync job {subject.record_id} rejected the canonical outcomes",
            reason="WRITE_FAILED")


def _write_audit_note(subject: ReconciliationSubject, note: str) -> None:
    """Put the decision on the lane row, where an operator will actually read it."""
    if subject.kind is SubjectKind.VIDEO:
        from app.models import Video

        with _scope() as session:
            row = session.get(Video, subject.record_id)
            if row is not None:
                row.submission_detail = _video_detail(row.submission_detail, note)
    elif subject.kind is SubjectKind.LIPSYNC:
        from app.models import LipSyncJob

        with _scope() as session:
            row = session.get(LipSyncJob, subject.record_id)
            if row is not None:
                cost_json = dict(row.cost_json or {})
                cost_json["reconciliation_note"] = note
                row.cost_json = cost_json


def _bind_operation(subject: ReconciliationSubject, *,
                    on_execution=None, on_cost=None,
                    on_remote_id=None) -> PaidOperation:
    """A :class:`PaidOperation` bound to the submission's reservation row.

    ``adopt_reservation`` rehydrates ``settled`` from the row's own
    ``cost_outcome``, which is what makes a second close impossible even if a
    caller forces past the decision guard. With no ledger row the operation is
    still returned -- it simply has no reservation, so every money method becomes
    a no-op and only the canonical columns move. That is the lip-sync lane's
    real shape, not an edge case.
    """
    adopted = (adopt_reservation(
        subject.cost_entry_id, provider=subject.provider,
        operation=subject.operation, remote_id=subject.remote_id,
        # ``UNKNOWN_EXPOSURE`` is NOT "already closed" here: it is unpriced, and
        # pricing it is the entire point of this path. Without this the second
        # half of the runbook's sequence -- look it up, then record the price --
        # would be permanently unreachable and the exposure would stay open
        # forever.
        settled_outcomes=_DECIDED_LEDGER_OUTCOMES)
        if subject.cost_entry_id else None)
    if adopted is not None:
        adopted.bind(on_execution=on_execution, on_cost_outcome=on_cost,
                     on_remote_id=on_remote_id)
        return adopted
    operation = PaidOperation(
        provider=subject.provider, operation=subject.operation,
        workspace_id=subject.workspace_id, category="",
        estimated_cost=float(subject.estimated_usd or 0.0),
        remote_id=str(subject.remote_id or ""))
    operation.bind(on_execution=on_execution, on_cost_outcome=on_cost,
                   on_remote_id=on_remote_id)
    return operation


def _classify_money(before: dict | None, after: dict | None) -> MoneyEffect:
    """Name what happened to the money, from the row before and after."""
    if before is None:
        return MoneyEffect.NO_LEDGER_ROW
    if before["decided"]:
        # The row's money was already accounted for when this reconciliation
        # arrived -- a real amount, a released reservation, or a booked
        # estimate. Moving it now would bill one purchase twice, so the guard in
        # ``adopt_reservation`` refuses and this reports it honestly.
        return MoneyEffect.NOTHING_TO_MOVE
    if after is None:
        return MoneyEffect.CAPACITY_RELEASED
    if abs(float(after["amount_usd"]) - float(before["amount_usd"])) > 1e-12:
        return MoneyEffect.RESERVATION_CORRECTED
    return MoneyEffect.RESERVATION_KEPT


# ---------------------------------------------------------------------------
# The durable reconciliation
# ---------------------------------------------------------------------------


def reconcile_paid_submission(
        subject: ReconciliationSubject,
        outcome: ReconciliationOutcome | str,
        *,
        operator: str,
        note: str = "",
        remote_id: str = "",
        actual_usd: float | None = None,
        estimate_usd: float | None = None,
        amount_unknown: bool = False,
) -> ReconciliationResult:
    """Record what the operator FOUND, and move the books accordingly.

    The five observations are translated by :data:`_READINGS` into the two
    structural facts plus one money rule; the money itself is moved by the
    shipped :class:`~app.services.paid_provider.PaidOperation` methods, never by
    code in this module.

    ===========================  =====================  =================  ============
    observation                  execution              cost               money
    ===========================  =====================  =================  ============
    ``REMOTE_JOB_CONFIRMED``     ``REMOTE_ID_CONFIRMED`` ``ESTIMATED``     kept
    ``NOT_ACCEPTED``             ``FAILED``              ``NOT_APPLICABLE`` released
    ``SUCCEEDED``                ``SUCCEEDED``           reported/estimated settled
    ``FAILED``                   ``FAILED``              reported/estimated settled
    ``UNKNOWN_REMAINS``          ``SUBMISSION_UNKNOWN``  ``UNKNOWN_EXPOSURE`` kept
    ===========================  =====================  =================  ============

    ``SUCCEEDED`` and ``FAILED`` take the reported amount when the provider gave
    one, else the estimate, else -- when the provider reported nothing at all and
    the caller says so with ``amount_unknown=True`` -- ``UNKNOWN_EXPOSURE``.
    They never take ``0.0``: ``cost.track_cost`` drops an amount ``<= 0``, so a
    "$0" render would write no row and the spend would vanish from the books.

    Refuses rather than guesses: no operator, no operation id, an unknown
    outcome, an outcome contradicted by its own inputs (``NOT_ACCEPTED`` with a
    remote id; ``REMOTE_JOB_CONFIRMED`` without one), or an outcome this exact
    submission has already had applied to it.
    """
    who = str(operator or "").strip()
    if not who:
        raise ReconciliationRefused(
            "a reconciliation must name an operator", reason="NO_OPERATOR")
    try:
        found = outcome if isinstance(outcome, ReconciliationOutcome) \
            else ReconciliationOutcome(str(outcome))
    except ValueError as exc:
        raise ReconciliationRefused(
            f"unknown outcome {outcome!r}; expected one of "
            f"{[str(o) for o in ReconciliationOutcome]}", reason="BAD_OUTCOME") \
            from exc
    if not str(subject.operation_id or "").strip():
        raise ReconciliationRefused(
            f"subject {subject.record_id} carries no operation id; a "
            "reconciliation without one could not be recognised as a repeat",
            reason="NO_OPERATION_ID")
    reading = _READINGS[found]

    handle = str(remote_id or subject.remote_id or "").strip()
    if reading.requires_remote_id and not handle:
        raise ReconciliationRefused(
            f"{found} needs the provider's remote id: a job nobody can address "
            "cannot be polled, adopted or settled by anything",
            reason="REMOTE_ID_REQUIRED")
    if reading.rejects_remote_id and handle:
        raise ReconciliationRefused(
            f"{found} contradicts remote id {handle!r}: a job that was never "
            "created has no handle", reason="CONTRADICTORY_INPUT")

    repeated = _applied_before(subject, found)
    if repeated:
        raise ReconciliationRefused(
            f"operation {subject.operation_id} already reconciled as {found} by "
            f"{repeated}; applying it again would settle the same purchase "
            "twice. A DIFFERENT observation is still allowed.",
            reason="ALREADY_APPLIED")

    stamp = datetime.now(UTC)
    decision = {"outcome": str(found), "operator": who,
                "note": str(note or "")[:400], "at": stamp.isoformat(),
                "remote_id": handle}
    cost = reading.cost
    if found in (ReconciliationOutcome.SUCCEEDED,
                 ReconciliationOutcome.FAILED) and not reading.keeps_unknown_exposure:
        if amount_unknown:
            cost = _UNKNOWN_EXPOSURE
        elif actual_usd is not None:
            cost = _ACTUAL
        else:
            cost = _ESTIMATED
    elif amount_unknown:
        cost = _UNKNOWN_EXPOSURE

    written = {"execution": reading.execution, "cost": cost, "remote_id": handle}

    def _on_execution(state: str, detail: str) -> None:
        written["execution"] = str(state) or reading.execution
        if subject.kind is SubjectKind.VIDEO:
            _write_video(replace(subject, remote_id=handle),
                         execution=written["execution"], cost=cost,
                         remote_id=handle, note=_note_text(found, who, note))
        elif subject.kind is SubjectKind.LIPSYNC:
            _write_lipsync(replace(subject, remote_id=handle),
                           execution=written["execution"], cost=cost,
                           remote_id=handle, note=note, decision=decision)

    def _on_cost(value: str) -> None:
        written["cost"] = str(value) or cost

    def _on_remote_id(value: str) -> None:
        written["remote_id"] = str(value) or handle

    before = ledger_state(subject.cost_entry_id) if subject.cost_entry_id else None
    operation = _bind_operation(
        replace(subject, remote_id=handle), on_execution=_on_execution,
        on_cost=_on_cost, on_remote_id=_on_remote_id)

    detail = _note_text(found, who, note)
    if found is ReconciliationOutcome.REMOTE_JOB_CONFIRMED:
        # Acceptance is not completion: the reservation stands and only the
        # handle is persisted, because that is what every later step needs.
        operation.mark_accepted(handle, detail=detail)
    elif found is ReconciliationOutcome.NOT_ACCEPTED:
        operation.mark_rejected(detail, nothing_billed=True)
    elif found is ReconciliationOutcome.SUCCEEDED:
        operation.mark_succeeded(actual_usd, estimate_usd=estimate_usd,
                                 detail=detail, amount_unknown=amount_unknown)
    elif found is ReconciliationOutcome.FAILED:
        # ``mark_rejected(nothing_billed=False)`` records FAILED and CLOSES the
        # book rather than releasing it -- the correct reading of "the provider
        # created it, charged us, and the render failed". Rejected-but-billed is
        # a real and common case; treating it as never-billed would hand back
        # capacity that was genuinely spent.
        operation.mark_rejected(detail, nothing_billed=False,
                                actual_usd=actual_usd,
                                estimate_usd=estimate_usd,
                                amount_unknown=amount_unknown)
    else:
        # UNKNOWN_REMAINS: nothing was learned, so nothing moves. The
        # reservation is KEPT -- releasing it here is the one change that would
        # turn "we do not know" into "it was free", which is the whole failure.
        operation.mark_unknown(detail)

    after = ledger_state(subject.cost_entry_id) if subject.cost_entry_id else None
    if subject.kind is SubjectKind.LEDGER_ONLY:
        _record_decision_on_ledger(subject, decision)
    money = _classify_money(before, after)

    final_remote = str(written["remote_id"] or handle or "")
    _emit(RECONCILIATION_KIND, replace(subject, remote_id=final_remote),
          f"paid submission {subject.operation_id} reconciled as {found} by {who}"
          + (f" (remote id {final_remote})" if final_remote else "")
          + f"; money: {money}",
          level=_event_level(found),
          data={"outcome": str(found), "execution_outcome": written["execution"],
                "cost_outcome": written["cost"], "money_effect": str(money),
                "remote_id": final_remote, "note": str(note or "")[:400],
                "operator": who, "at": stamp.isoformat(), "moved_money": True})
    logger.warning("paid reconciliation %s: operation=%s outcome=%s money=%s "
                   "operator=%s", subject.record_id, subject.operation_id, found,
                   money, who)
    return ReconciliationResult(
        outcome=found, subject=replace(subject, remote_id=final_remote),
        execution_outcome=str(written["execution"]),
        cost_outcome=str(written["cost"]), money_effect=money,
        remote_id=final_remote, operator=who, note=str(note or ""), at=stamp)


def _note_text(outcome: ReconciliationOutcome, operator: str, note: str) -> str:
    return f"{outcome} by {operator}" + (f": {note}" if note else "")


def _record_decision_on_ledger(subject: ReconciliationSubject,
                               decision: dict) -> None:
    """Stamp the decision on a ledger-only subject, which has no lane row.

    Idempotent: the same decision writes the same document, so an interrupted
    reconciliation re-run leaves the row identical instead of appending again.
    """
    if not subject.cost_entry_id:
        return
    from app.models import CostEntry

    with _scope() as session:
        row = session.get(CostEntry, subject.cost_entry_id)
        if row is None:
            return
        detail = dict(row.detail_json or {})
        detail["reconciliation"] = dict(decision)
        detail["reconciled_by"] = decision["operator"]
        row.detail_json = detail


# ---------------------------------------------------------------------------
# Operator entry point
# ---------------------------------------------------------------------------


def _resolve(args) -> ReconciliationSubject:
    """Turn the mutually-exclusive CLI selectors into one subject.

    Exactly one handle, or the tool would be guessing which incident it was
    asked about -- and a reconciliation applied to the wrong row is a money
    decision about somebody else's purchase.
    """
    handles = [bool(str(getattr(args, "video_id", "") or "").strip()),
               bool(str(getattr(args, "lipsync_job_id", "") or "").strip()),
               bool(str(getattr(args, "operation_id", "") or "").strip())]
    if sum(handles) != 1:
        raise ReconciliationRefused(
            "name exactly one of --video-id, --lipsync-job-id or --operation-id",
            reason="AMBIGUOUS_SUBJECT")
    if args.video_id:
        return subject_for_video(args.video_id)
    if args.lipsync_job_id:
        return subject_for_lipsync_job(args.lipsync_job_id)
    return subject_for_operation(args.operation_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.paid_reconciliation",
        description=("Apply an operator/provider decision to an ambiguous paid "
                     "submission, durably. Look the remote id up on the "
                     "provider's dashboard first; this records the answer and "
                     "closes the books. It never resubmits."))
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser(
        "list", help="every submission in a workspace that may have been billed")
    listing.add_argument("--workspace-id", required=True)
    listing.add_argument("--limit", type=int, default=100)

    show = sub.add_parser("show", help="read one subject back, JSON, read-only")
    _add_handles(show)

    apply_cmd = sub.add_parser(
        "reconcile", help="apply one observed outcome, durably and once")
    _add_handles(apply_cmd)
    apply_cmd.add_argument(
        "--outcome", required=True, choices=[str(o) for o in ReconciliationOutcome],
        help="what the provider's dashboard showed")
    apply_cmd.add_argument(
        "--operator", required=True,
        help="who looked. A decision with no author is unexplainable later.")
    apply_cmd.add_argument("--note", default="")
    apply_cmd.add_argument(
        "--remote-id", default="",
        help="the provider's handle, REQUIRED for REMOTE_JOB_CONFIRMED and "
             "REFUSED for NOT_ACCEPTED")
    apply_cmd.add_argument(
        "--actual-usd", type=float, default=None,
        help="the amount the provider reported. Absent means the estimate "
             "stands, never 0.")
    apply_cmd.add_argument(
        "--estimate-usd", type=float, default=None,
        help="use this estimate instead of the reserved one")
    apply_cmd.add_argument(
        "--amount-unknown", action="store_true",
        help="the work finished but the provider reported no amount: book "
             "UNKNOWN_EXPOSURE, not $0")
    return parser


#: Markers of an OUTBOUND billable request. This module must never contain one:
#: reconciliation is the step that happens AFTER a decision about money that may
#: already be gone, so a request builder here would be a re-purchase wearing an
#: operator's name. The assertion is on the source rather than on a call count,
#: because the dangerous version is the one nobody calls in this test.
#: Markers of an OUTBOUND billable request, or of the one flag that licenses a
#: second attempt. This module must never contain one: reconciliation is the step
#: AFTER a decision about money that may already be gone, so a request builder
#: here is a re-purchase wearing an operator's name.
#:
#: The marker names are assembled from parts on purpose. A literal list would sit
#: in the very source it is scanning, and the check would always trip on itself
#: -- which is the classic way a self-referential guard is quietly disabled.
_RESUBMIT_MARKERS: tuple[str, ...] = (
    "htt" + "px", "reque" + "sts.", "url" + "open", "a" + "iohttp",
    # ``Session.execute`` is a database call and is not one of these, so the
    # marker is the SUBMIT-shaped call, not every method named execute.
    "execut" + "e(sub", "submit" + "_fn", "mark_" + "attempt",
    # ``verdict_for`` reads ONLY this flag to answer RETRY, so writing it here
    # would be the whole of a second purchase.
    "RETRY_IF_" + "CONFIRMED_SAFE",
)


def assert_never_resubmits() -> None:
    """Raise if this module can send a billable request or license a retry.

    Derived from the module's own source rather than asserted in prose, on the
    same reasoning as ``paid_jobs_audit.verify_against_source``: a coverage claim
    nobody re-derives is a comment. The module docstring is excluded from the
    scan (it necessarily names the markers in prose); the CODE is not.
    """
    from pathlib import Path

    source = Path(__file__).resolve().read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # drop the module docstring: it names them
    hits = [marker for marker in _RESUBMIT_MARKERS if marker in body]
    if hits:
        raise AssertionError(
            f"paid_reconciliation must never resubmit, but its source names "
            f"{hits}. Reconciliation decides about money that may already be "
            f"gone; a request builder here is a second purchase.")


def _add_handles(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video-id", default="", help="videos.id")
    parser.add_argument("--lipsync-job-id", default="", help="lipsync_jobs.id")
    parser.add_argument("--operation-id", default="",
                        help="the PaidSubmission.submission_id an incident "
                             "list shows")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code; never raises for a refusal."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "list":
            print(json.dumps(pending_submissions(args.workspace_id,
                                                limit=args.limit), indent=2))
            return 0
        subject = _resolve(args)
        if args.command == "show":
            print(json.dumps(subject.to_dict(), indent=2))
            return 0
        result = reconcile_paid_submission(
            subject, args.outcome, operator=args.operator, note=args.note,
            remote_id=args.remote_id, actual_usd=args.actual_usd,
            estimate_usd=args.estimate_usd, amount_unknown=args.amount_unknown)
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    except ReconciliationRefused as refused:
        # A refusal is information, not a crash: exit 2 and say why.
        print(json.dumps({"refused": refused.reason, "detail": str(refused)},
                         indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())