"""Leases: who owns a running job, until when, and what a recovery may do.

Work 16 §2. Before this module the queue could not tell two situations apart::

    worker A is rendering this job right now
    worker A died holding this job and nobody will ever finish it

Both read as ``status='RUNNING'``. ``jobs.recover_orphans`` resolved the
ambiguity by assuming the first was rare and the second was normal: on every
worker boot it flipped **all** ``RUNNING`` rows back to ``RETRYING``. In a
single process that was survivable. With two workers it buys the second one a
duplicate execution of a live job -- and, for a paid job, a duplicate
purchase.

The lease is the missing evidence. A worker takes a lease, renews it while it
works, and releases it when done; **only an expired lease may be reclaimed.**
A live worker renews on a fixed cadence, so a live job's lease never lapses and
a dead worker's lapses within one TTL. Expiry is therefore proof of absence, not
a guess, and the reclaim sweep becomes safe to run on a timer, from any process,
any number of times.

Three properties this file is written to hold, each of them testable by
breaking it:

**Atomicity across processes.** A claim is ONE conditional UPDATE
(``WHERE status IN ('QUEUED','RETRYING')``) whose ``rowcount`` is the answer.
There is no read-then-write window for two workers to interleave in, so the
loser of a race gets ``rowcount == 0`` and moves on instead of running a job
somebody else already owns. PostgreSQL additionally gets ``SKIP LOCKED`` on the
candidate SELECT so N workers do not queue behind one another's row locks;
SQLite has no such clause and does not need one, because the conditional UPDATE
is what decides the winner, not the SELECT.

**Two real states, not one blur.** ``QUEUED -> CLAIMED -> RUNNING`` is
observable: a lease is taken (``claimed_at``, ``started_at`` still NULL) and
the handler is entered later (``started_at``). The status column keeps saying
``RUNNING`` across both, which is deliberate -- a dozen readers across the
codebase query ``status IN ('QUEUED','RUNNING')`` as "in flight", and a
seventeenth spelling they do not know about would make a live job invisible to
all of them.

**Money.** See :func:`assess_paid_reentry`. A re-entry attempt consults the
paid ledger before it runs anything, because re-running a job whose billable
submit already left is how one render is purchased twice.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from loguru import logger
from sqlalchemy import or_, select, update

from app.db import session_scope
from app.models import CostEntry, Job
from app.models.base import JobStatus, utcnow

__all__ = [
    "ClaimedJob",
    "LeaseKeeper",
    "LeaseState",
    "PaidAssessment",
    "PaidVerdict",
    "assess_paid_reentry",
    "begin_run",
    "claim_by_id",
    "claim_next",
    "held_job_ids",
    "lease_is_valid",
    "lease_seconds",
    "lease_state",
    "reclaim_expired",
    "register_paid_job_type",
    "register_paid_probe",
    "renew_lease",
    "requeue",
    "worker_identity",
]

#: This process's worker identity, resolved once. A module-level cache rather
#: than a fresh uuid per call, because the identity is compared against
#: ``jobs.claimed_by`` on every heartbeat: a new one per call would be a
#: different worker on every renewal.
_identity_cache: str = ""

#: The only statuses a claim may move. ``WAITING`` is deliberately absent: it is
#: how the GPU slot ledger parks a live slot (media_intel_runs), so claiming it
#: would hand a held slot to a second worker.
CLAIMABLE: tuple[str, ...] = (JobStatus.QUEUED.value, JobStatus.RETRYING.value)

#: Statuses that represent held work. A job is *lease-protected* in exactly
#: these states, and only these are subject to expiry-based recovery.
LEASE_HELD: tuple[str, ...] = (JobStatus.RUNNING.value,)

#: How many candidate rows one claim reads. Small on purpose: it is on the hot
#: path of every worker, and an idle worker must not drag a thousand rows out of
#: the table to discover there is nothing to do.
_CANDIDATE_BATCH = 20

#: How many candidates to read once a class-pinned worker found none in a FULL
#: narrow batch. Without this escalation the pool starves itself: a queue whose
#: first twenty rows are all ``SMALL`` leaves a ``RENDER`` slot seeing nothing,
#: forever, while renders sit further down that same queue. It only triggers
#: after a full miss, so the idle cost is still one narrow read.
_CANDIDATE_BATCH_WIDE = 1000

#: How many times one claim re-reads the head of the queue before reporting
#: "nothing for me". Each try is a handful of statements, not a stall.
_CANDIDATE_TRIES = 4

#: Push-back delay for a job a worker refused (wrong class, or GPU-gated on a
#: non-GPU worker). Long enough to leave the front of the priority queue, so
#: the same worker does not re-read the same head-of-queue row every poll for
#: the next hour; short enough that a mis-tagged job is not visibly delayed.
_REFUSED_PUSHBACK_SECONDS = 60.0

#: Ledger scan bound. Recovery is rare by construction, so a bounded scan is the
#: right trade: it cannot become a table walk, and it behaves identically on
#: SQLite and on a server-grade engine. Same reasoning as
#: ``paid_provider._ATTACH_SCAN_LIMIT``.
_LEDGER_SCAN_LIMIT = 300


class LeaseState(StrEnum):
    """Where one job is in ``QUEUED -> CLAIMED -> RUNNING -> terminal``.

    Derived, never stored as a second vocabulary: ``status`` remains the
    business lifecycle every other reader already understands, and this enum
    adds the distinction the status column deliberately does not make.
    """

    QUEUED = "QUEUED"
    #: Lease held, handler not entered yet.
    CLAIMED = "CLAIMED"
    #: Handler entered, lease being heartbeated.
    RUNNING = "RUNNING"
    #: Anything else (COMPLETED/FAILED/CANCELLED/RETRYING/DEAD/WAITING).
    TERMINAL = "TERMINAL"


# ---------------------------------------------------------------------------
# Identity and lease duration
# ---------------------------------------------------------------------------


def worker_identity(override: str = "") -> str:
    """This process's worker identity, written onto every lease it takes.

    ``host:pid:nonce`` by default. The nonce matters: a restarted process
    reuses host and pid often enough that without it, an operator asking "who
    owns job X" could be told it was the previous incarnation of the same pid.
    An operator-supplied name overrides all of it, which is how a deployed
    replica names itself.
    """
    global _identity_cache
    explicit = str(override or "").strip()
    if explicit:
        return explicit[:80]
    if _identity_cache:
        return _identity_cache
    name = f"{socket.gethostname()[:32]}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    _identity_cache = name[:80]
    return _identity_cache


def lease_seconds(workload: str = "") -> float:
    """The lease TTL for one workload class.

    The default TTL only has to outlast a missed heartbeat, never a job: a
    long job renews on a fixed cadence, so a 30-minute render and a 200ms
    quality check hold leases of the same order. Overrides are parsed from
    ``job_lease_seconds_by_workload`` (``"RENDER=900,GPU=1800"``); an
    unparseable entry is ignored with a warning rather than silently becoming
    zero, because a zero TTL turns every live job into an expired one.
    """
    from app.core.config import settings

    raw = str(settings.job_lease_seconds_by_workload or "").strip()
    if raw:
        for chunk in raw.split(","):
            name, _, value = chunk.partition("=")
            if name.strip().upper() != str(workload or "").strip().upper():
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                logger.warning(
                    "job lease override {!r} is not a number; using the default",
                    chunk.strip())
                break
            if parsed > 0:
                return parsed
            logger.warning(
                "job lease override for {} is not positive; using the default",
                name.strip())
            break
    return max(1.0, float(settings.job_lease_seconds or 120.0))


def lease_state(job) -> LeaseState:
    """Where ``job`` is in the lease lifecycle. Read-only; never writes."""
    status = str(getattr(job, "status", "") or "")
    if status not in LEASE_HELD:
        return LeaseState.TERMINAL
    return LeaseState.RUNNING if getattr(job, "started_at", None) else LeaseState.CLAIMED


def lease_is_valid(job, now: datetime | None = None) -> bool:
    """Whether this job's lease still proves a live owner.

    ``False`` for a job nobody holds (nothing to protect) and ``False`` for a
    lease whose deadline has passed (the owner is gone). Only ``True`` here
    means "do not touch it".
    """
    status = str(getattr(job, "status", "") or "")
    if status not in LEASE_HELD:
        return False
    expires = getattr(job, "lease_expires_at", None)
    if expires is None:
        # A lease nobody can renew. For a row written before 0035 this is the
        # only honest reading, and it is what makes such rows recoverable.
        return False
    return expires > (now or utcnow())


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimedJob:
    """One job a worker now owns, with the lease attached."""

    job_id: str
    type: str
    workspace_id: str | None
    cycle_id: str | None
    payload: dict
    #: The enqueue-time dedupe key. Carried because it is also the EXACT link
    #: between this job and its paid reservation: a producer that records the
    #: same key in ``reservation_extra`` gets a precise reattach on recovery
    #: instead of a workspace-scoped block.
    idempotency_key: str
    #: 1 for a first attempt; >1 means a previous attempt existed, which is what
    #: makes :func:`assess_paid_reentry` load-bearing.
    attempt: int
    claimed_by: str
    claimed_at: datetime
    lease_expires_at: datetime
    #: Resolved by the pool layer; the class this job belongs to.
    workload: str = ""
    #: True when the payload asked for a GPU-gated worker.
    requires_gpu: bool = False

    @property
    def is_reentry(self) -> bool:
        """A previous attempt existed, so this one may re-spend.

        The single honest signal, because it is written by the same statement
        that made the previous attempt fail. It deliberately does not try to
        distinguish "failed in its handler" from "the worker died holding it":
        both mean the same thing to the money question.
        """
        return self.attempt > 1


def _now(now: datetime | None = None) -> datetime:
    return now or utcnow()


def _payload_of(job) -> dict:
    return dict(job.payload or {})


def _build(job, *, worker: str, ttl: float, now: datetime,
           workload: str) -> ClaimedJob:
    payload = _payload_of(job)
    return ClaimedJob(
        job_id=job.id,
        type=job.type,
        workspace_id=job.workspace_id,
        cycle_id=job.cycle_id,
        payload=payload,
        idempotency_key=str(job.idempotency_key or ""),
        attempt=int(job.retry_count or 0) + 1,
        claimed_by=worker,
        claimed_at=now,
        lease_expires_at=now + timedelta(seconds=ttl),
        workload=workload,
        requires_gpu=bool(payload.get("requires_gpu")),
    )


class _Take(StrEnum):
    """The three answers one conditional UPDATE can give."""

    WON = "WON"
    LOST = "LOST"
    #: Won the row and then handed it straight back (GPU gate).
    REFUSED = "REFUSED"


def _for_update(query, session):
    """Row-level lock for the candidate SELECT on Postgres only.

    ``SKIP LOCKED`` lets N workers read N *different* candidates instead of
    serialising on one row's lock, which is the difference between a pool that
    scales and a pool that queues. SQLite has no such clause and silently
    ignores the modifier on some builds, so it is not requested there. Neither
    backend depends on it: the conditional UPDATE below decides the winner.
    """
    try:
        if session.get_bind().dialect.name == "postgresql":
            return query.with_for_update(skip_locked=True)
    except Exception:  # pragma: no cover - dialect probing only
        pass
    return query


def _take(job_id: str, worker: str, *, now: datetime, ttl: float,
          gpu_ok: bool | None = None) -> _Take:
    """The atomic claim: one conditional UPDATE, ``rowcount`` is the verdict.

    This is the whole concurrency argument. A plain ``SELECT ... LIMIT 1``
    followed by an ``UPDATE`` is a lost-update race -- two workers read the same
    id and both proceed. Here the status predicate is inside the statement that
    writes, so the database itself rejects the second worker: its ``UPDATE``
    matches zero rows because the first worker already moved the row out of
    ``CLAIMABLE``. There is no window between the decision and the write, and
    the same statement is correct on SQLite (which serialises writers) and on
    PostgreSQL (which does not, and does not need to).
    """
    with session_scope() as s:
        res = s.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_(CLAIMABLE),
                # A live lease on a claimable row would mean an owner that never
                # finished; refusing here surfaces it to the recovery sweep
                # instead of hiding it behind a second claim.
                or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                Job.next_run_at <= now,
            )
            .values(
                status=JobStatus.RUNNING.value,
                claimed_by=worker,
                claimed_at=now,
                lease_expires_at=now + timedelta(seconds=ttl),
                heartbeat_at=now,
                # A re-entry attempt starts its own clock. Left over from the
                # previous attempt it would make the job look like it had been
                # running since the crash.
                started_at=None,
                completed_at=None,
            )
        )
        if res.rowcount != 1:
            return _Take.LOST
        job = s.get(Job, job_id)
        if job is None:  # pragma: no cover - deleted between select and update
            return _Take.LOST
        if _needs_gpu_deferral(job) and not (
                _gpu_allowed() if gpu_ok is None else gpu_ok):
            # Won the row and handed it straight back, in the SAME transaction.
            # Bailing out instead would leave the row QUEUED at the head of the
            # priority queue, so a CPU-only worker would re-read it on every
            # poll until a GPU worker appeared. The pre-lease code pushed it
            # back for exactly this reason and the push-back is worth keeping.
            s.execute(
                update(Job)
                .where(Job.id == job_id, Job.claimed_by == worker)
                .values(
                    status=JobStatus.QUEUED.value, claimed_by="",
                    claimed_at=None, lease_expires_at=None, heartbeat_at=None,
                    started_at=None,
                    next_run_at=now + timedelta(seconds=_REFUSED_PUSHBACK_SECONDS),
                )
            )
            logger.info("job {} needs a GPU worker; pushed back {}s", job_id,
                        int(_REFUSED_PUSHBACK_SECONDS))
            return _Take.REFUSED
        return _Take.WON


def _needs_gpu_deferral(job) -> bool:
    return bool((job.payload or {}).get("requires_gpu"))


def _gpu_allowed() -> bool:
    from app.core.config import settings

    return bool(settings.gpu_worker)


#: Cost-entry categories that represent work a provider bills for. A reservation
#: in one of these is a money event; one in ``storage`` is housekeeping and must
#: never make a recovery look ambiguous.
_PAID_CATEGORIES: tuple[str, ...] = (
    "llm", "tts", "image", "video", "search", "publishing", "avatar",
    "lipsync", "broll", "audio", "dubbing", "render",
)


def _classify(job) -> str:
    from app.services.worker_pool import classify_workload

    return str(classify_workload(job.type, job.payload or {}))


def claim_by_id(worker: str, job_id: str, *, now: datetime | None = None,
                workload: str = "") -> ClaimedJob | None:
    """Claim one SPECIFIC job (a Redis-dispatched id).

    Same atomicity as a poll claim: one conditional UPDATE whose ``rowcount``
    decides. A dispatch is a hint, not a grant -- by the time a worker gets
    here the job may be claimed, cancelled, dead, or another worker's, and each
    of those is a clean ``None`` rather than an exception the caller has to
    guess about.
    """
    stamp = _now(now)
    if _take(job_id, worker, now=stamp, ttl=lease_seconds(workload)) is not _Take.WON:
        return None
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            return None
        resolved = _classify(job)
        if workload and resolved != workload:
            requeue(job_id, worker, delay_seconds=_REFUSED_PUSHBACK_SECONDS,
                    reason=f"claimed for {workload}, is {resolved}")
            return None
        return _build(job, worker=worker, ttl=lease_seconds(resolved), now=stamp,
                      workload=resolved)


def claim_next(worker: str, *, workload: str = "",
               now: datetime | None = None) -> ClaimedJob | None:
    """Atomically claim the highest-priority due job this worker may run.

    ``workload`` pins the claim to one workload class (Work 16 §3). Candidates
    are filtered by class in Python rather than in SQL because the class is
    derived from ``payload`` -- a portable JSON predicate per class would be a
    worse trade than reading twenty rows. The claim itself stays a single
    conditional UPDATE, so N workers polling the same head-of-queue row still
    produce exactly one winner.
    """
    from app.services.worker_pool import classify_workload

    stamp = _now(now)
    limit = _CANDIDATE_BATCH
    for _ in range(_CANDIDATE_TRIES):
        with session_scope() as s:
            query = (
                select(Job)
                .where(Job.status.in_(CLAIMABLE), Job.next_run_at <= stamp)
                .order_by(Job.priority.asc(), Job.next_run_at.asc())
                .limit(limit)
            )
            candidates = list(s.scalars(_for_update(query, s)).all())
        if not candidates:
            return None
        for job in candidates:
            resolved = classify_workload(job.type, job.payload or {})
            if workload and resolved != workload:
                continue
            # Compare against the enum, never by truthiness: ``_Take`` is a
            # StrEnum, so every member is a non-empty string and ``if not
            # _take(...)`` is False for LOST and REFUSED too -- which is
            # exactly how a lost race becomes a second execution of a job
            # somebody else already owns. Only a real concurrency test catches
            # that, and it caught this one.
            if _take(job.id, worker, now=stamp, ttl=lease_seconds(resolved)) is not _Take.WON:
                continue
            return _build(job, worker=worker, ttl=lease_seconds(resolved),
                          now=stamp, workload=resolved)
        # Nothing claimable for this worker in what we read. A FULL batch means
        # there may simply be more of the queue below the cut, so widen once; an
        # empty tail means we have seen the whole queue and can stop.
        if workload and len(candidates) >= limit:
            limit = _CANDIDATE_BATCH_WIDE
    return None


def begin_run(job_id: str, worker: str, *, now: datetime | None = None) -> bool:
    """``CLAIMED -> RUNNING``: the handler is being entered right now.

    Separate from the claim because the gap between the two is where a worker
    can die having taken the job but started nothing, and because "a lease is
    held" and "a handler is executing" are different questions an operator asks.
    Idempotent per owner: a second call by the same worker is a no-op, a call
    by anybody else is refused.
    """
    stamp = _now(now)
    with session_scope() as s:
        res = s.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.claimed_by == worker,
                Job.status.in_(LEASE_HELD),
                Job.started_at.is_(None),
            )
            .values(started_at=stamp)
        )
        return res.rowcount == 1


def renew_lease(job_id: str, worker: str, *, now: datetime | None = None,
                ttl: float | None = None) -> bool:
    """Heartbeat: push the deadline out. The proof-of-life write.

    Conditional on ``claimed_by``, so a worker whose lease was already
    reclaimed cannot resurrect it by heartbeating afterwards. That is the
    property that makes "expired" mean "gone" rather than "late".
    """
    stamp = _now(now)
    span = float(ttl) if ttl is not None else lease_seconds("")
    with session_scope() as s:
        res = s.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.claimed_by == worker,
                Job.status.in_(LEASE_HELD),
            )
            .values(heartbeat_at=stamp,
                    lease_expires_at=stamp + timedelta(seconds=span))
        )
        return res.rowcount == 1


def requeue(job_id: str, worker: str, *, delay_seconds: float = 5.0,
            reason: str = "") -> bool:
    """Give a claim back without consuming it. The compensation for a misroute.

    Only ever moves a row the caller may act on: a live owner's lease is never
    released by somebody else, or a worker could requeue work another worker is
    mid-flight on. ``worker=""`` is the operator/no-owner case (the GPU
    push-back), which is why it is explicit rather than inferred.
    """
    stamp = _now()
    with session_scope() as s:
        conditions = [Job.id == job_id]
        if worker:
            conditions.append(Job.claimed_by == worker)
        conditions.append(Job.status.in_(LEASE_HELD))
        res = s.execute(
            update(Job)
            .where(*conditions)
            .values(
                status=JobStatus.QUEUED.value,
                claimed_by="",
                claimed_at=None,
                lease_expires_at=None,
                heartbeat_at=None,
                started_at=None,
                next_run_at=stamp + timedelta(seconds=float(delay_seconds)),
            )
        )
        if res.rowcount == 1 and reason:
            logger.info("job {} requeued: {}", job_id, reason)
        return res.rowcount == 1


def held_job_ids(worker: str) -> list[str]:
    """Every job this worker currently holds a lease on. Operator/debuggable."""
    with session_scope() as s:
        return list(
            s.scalars(
                select(Job.id).where(Job.claimed_by == worker,
                                     Job.status.in_(LEASE_HELD))
            ).all()
        )


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReclaimedJob:
    """One job handed back to the queue because its owner stopped heartbeating."""

    job_id: str
    lost_owner: str
    expired_at: datetime | None
    attempt: int
    was_paid: bool


def reclaim_expired(*, now: datetime | None = None, limit: int = 500,
                    dry_run: bool = False) -> list[ReclaimedJob]:
    """Return lease-expired jobs to the queue. The crash-recovery sweep.

    The old ``recover_orphans`` reset **every** ``RUNNING`` row on every worker
    boot, which with more than one worker meant any deploy could hand another
    process's live, possibly-billed job to a second worker. This sweep is the
    same idea with the missing condition: ``lease_expires_at <= now``. A live
    worker heartbeats every third of its TTL, so its rows are never touched; a
    worker that died stops heartbeating and its rows become reclaimable within
    one TTL.

    Retries are consumed, because a crash is a failed attempt. A job whose
    crash budget is spent goes ``DEAD`` instead of being requeued forever: an
    unreclaimable-but-parked job is how a poison pill becomes a hot loop, and a
    dead-letter row is visible where a hot loop is not.
    """
    stamp = _now(now)
    reclaimed: list[ReclaimedJob] = []
    with session_scope() as s:
        rows = list(
            s.scalars(
                select(Job)
                .where(
                    Job.status.in_(LEASE_HELD),
                    or_(Job.lease_expires_at.is_(None),
                        Job.lease_expires_at <= stamp),
                )
                .order_by(Job.claimed_at.asc())
                .limit(limit)
            ).all()
        )
        for job in rows:
            lost_owner = str(job.claimed_by or "")
            paid = bool(_open_paid_evidence(getattr(job, "workspace_id", None)))
            attempt = int(job.retry_count or 0) + 1
            reclaimed.append(ReclaimedJob(job_id=job.id, lost_owner=lost_owner,
                                          expired_at=job.lease_expires_at,
                                          attempt=attempt, was_paid=paid))
            if dry_run:
                continue
            job.retry_count = attempt
            job.claimed_by = ""
            job.claimed_at = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.started_at = None
            job.last_error = (
                f"lease expired: worker {lost_owner or '<unknown>'} stopped "
                f"renewing"
            )[:4000]
            if attempt > int(job.max_retries or 0):
                job.status = JobStatus.DEAD.value
                job.completed_at = stamp
                # `next_run_at` stays: the column is NOT NULL and a dead job
                # has no schedule. Leaving the last value is harmless because
                # nothing claims a DEAD row.
            else:
                job.status = JobStatus.RETRYING.value
                job.next_run_at = stamp
    if reclaimed:
        logger.warning(
            "reclaimed {} job(s) whose lease expired (former owner(s): {})",
            len(reclaimed),
            ", ".join(sorted({r.lost_owner or "<unknown>" for r in reclaimed})))
    return reclaimed


# ---------------------------------------------------------------------------
# Paid re-entry
# ---------------------------------------------------------------------------


class PaidVerdict(StrEnum):
    """What a re-entry attempt may do about money it might already have spent."""

    #: No evidence of a prior submission. A fresh attempt cannot double-charge.
    NONE = "NONE"
    #: A ledger row already owns this work. Adopt it; never submit again.
    REATTACH = "REATTACH"
    #: Money may be gone and unattributable. Refuse to run; reconcile by hand.
    BLOCKED = "BLOCKED"
    #: This job's handler reconciles its own prior submission before spending
    #: (the render lane's ``_resolve_existing``). Recorded, then deferred.
    DEFERRED = "DEFERRED"


@dataclass(frozen=True)
class PaidAssessment:
    """One answer to "may this attempt spend money it might already have spent?"."""

    verdict: PaidVerdict = PaidVerdict.NONE
    remote_id: str = ""
    entry_id: str = ""
    provider: str = ""
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.verdict is PaidVerdict.BLOCKED


#: ``job type -> probe``. A probe answers "for THIS job, is there already a
#: billable submission, and what is its remote id?". It exists because the queue
#: layer cannot always know, and guessing is exactly what this guard forbids.
#: A producer that can correlate precisely registers one and gets automatic
#: reattach; one that cannot gets ``BLOCKED``, which is the safe direction.
_probes: dict[str, Callable[[ClaimedJob], str]] = {}

#: Job types whose handler reconciles its own prior submission before spending.
#: Declared, never inferred: the alternative is the queue guessing that a
#: handler is idempotent, and a wrong guess is a duplicate purchase.
_paid_aware: set[str] = set()


def register_paid_probe(job_type: str,
                        probe: Callable[[ClaimedJob], str]) -> Callable[[ClaimedJob], str]:
    """Teach the recovery path how to find THIS job's prior paid submission.

    The probe returns a remote id (adopt it) or ``""`` (no prior submission for
    this job). Returning a remote id that no ledger row owns is treated as
    ``BLOCKED``, not as permission to resubmit: the provider may have taken the
    money and the crash may have lost the only record of it.
    """
    _probes[str(job_type)] = probe
    return probe


def register_paid_job_type(job_type: str) -> str:
    """Declare that this job's handler does its own paid reconciliation."""
    _paid_aware.add(str(job_type))
    return job_type


def _ledger_rows(workspace_id: str | None) -> list[dict]:
    """This workspace's money rows, newest first, as plain dicts.

    A filtered scan rather than a JSON-path query, for the reason
    ``paid_provider.reattach_by_remote_id`` gives: it runs on a rare path and
    has to behave identically on both backends.
    """
    if not str(workspace_id or "").strip():
        return []
    with session_scope() as s:
        rows = s.scalars(
            select(CostEntry)
            .where(CostEntry.workspace_id == str(workspace_id).strip())
            .order_by(CostEntry.created_at.desc())
            .limit(_LEDGER_SCAN_LIMIT)
        ).all()
    return [
        {
            "entry_id": str(row.id),
            "provider": str(row.provider or ""),
            "category": str(row.category or ""),
            "amount_usd": float(row.amount_usd or 0.0),
            "detail": dict(row.detail_json or {}),
        }
        for row in rows
    ]


def _evidence(row: dict) -> str:
    """Why this money row is evidence that a submit may already exist.

    Three kinds, weakest first:

    * an OPEN reservation (no cost outcome recorded) -- ``authorize`` writes it
      before the request leaves, so its presence proves a billable request was
      about to be made and nothing has since said it was not;
    * ``UNKNOWN_EXPOSURE`` -- the response was lost, money may be gone;
    * a ``remote_id`` -- the provider accepted and handed back a durable handle.
      This is the strongest, and it is the one a crash cannot fabricate.
    """
    detail = row.get("detail") or {}
    if str(detail.get("remote_id") or "").strip():
        return "REMOTE_ID"
    outcome = str(detail.get("cost_outcome") or "").strip()
    if outcome == "UNKNOWN_EXPOSURE":
        return "UNKNOWN_EXPOSURE"
    if not outcome and row.get("category") in _PAID_CATEGORIES:
        return "OPEN_RESERVATION"
    return ""


def _linked(row: dict, job) -> str:
    """Whether this money row provably belongs to THIS job, and how.

    Only an EXPLICIT link counts. The queue layer deliberately does not guess:
    "the newest paid row in this workspace" is the right row for one job at a
    time and the wrong one for two, and a wrong reattach would poll somebody
    else's remote job and settle somebody else's money. A producer that
    records its idempotency key on both the job and the reservation gets an
    exact match here; a producer that registers a
    :func:`register_paid_probe` gets a precise answer by a different route.
    """
    detail = row.get("detail") or {}
    key = str(getattr(job, "idempotency_key", "") or "")
    if key and detail.get("idempotency_key") == key:
        return "IDEMPOTENCY_KEY"
    return ""


def _paid_evidence_for_job(job) -> list[dict]:
    """Money rows that are evidence for ``job`` specifically."""
    rows = _ledger_rows(getattr(job, "workspace_id", None))
    return [r for r in rows if _linked(r, job) and _evidence(r)]


def _open_paid_evidence(workspace_id: str | None) -> list[dict]:
    """Any money row in this workspace that may represent a live purchase.

    Workspace-scoped on purpose, and used only where an exact link is not
    available: :meth:`reclaim_expired` has to decide, for a job whose owner
    vanished, whether to warn an operator that money may be behind it. A
    warning that is sometimes over-broad is fine; a warning that is silent when
    it should have fired is how a duplicate invoice becomes untraceable.
    """
    return [r for r in _ledger_rows(workspace_id) if _evidence(r)]


def assess_paid_reentry(job, *, attempt: int | None = None) -> PaidAssessment:
    """May this attempt spend money it might already have spent?

    Runs for every attempt after the first, and for every attempt after a lease
    recovery. The question it answers is narrow and deliberately paranoid:

        is there a durable record that a billable request for THIS job may
        already have been made?

    If yes, the attempt does not get to submit. It either adopts the remote job
    that already exists (:attr:`PaidVerdict.REATTACH`) or stops and asks for a
    human (:attr:`PaidVerdict.BLOCKED`). Neither outcome can charge twice, and
    both are recoverable, which is the whole trade: a stuck job is an incident,
    a duplicate purchase is an invoice.
    """
    job_id = str(getattr(job, "id", "") or "")
    job_type = str(getattr(job, "type", "") or "")
    try:
        job_attempt = int(attempt if attempt is not None
                          else (getattr(job, "retry_count", 0) or 0) + 1)
    except (TypeError, ValueError):
        job_attempt = 1
    if job_attempt <= 1:
        # A first attempt has no prior submission to collide with. Checking
        # here anyway would block every job in a workspace that has ever had an
        # unresolved paid row, which is a stall, not a safety.
        return PaidAssessment(detail="first attempt")

    # 1. The precise answer, when the producer knows how to give one.
    probe = _probes.get(job_type)
    if probe is not None:
        remote_id = str(probe(job) or "").strip()
        if remote_id:
            return _adopt(remote_id, f"probe for {job_type}")

    # 2. A money row that names this job. Exact, so it outranks the rest.
    linked = _paid_evidence_for_job(job)
    if linked:
        strongest = next((r for r in linked
                          if (r["detail"] or {}).get("remote_id")), linked[0])
        remote_id = str((strongest["detail"] or {}).get("remote_id") or "").strip()
        if remote_id:
            return _adopt(remote_id, "money row linked to this job")
        return PaidAssessment(
            verdict=PaidVerdict.BLOCKED, entry_id=strongest["entry_id"],
            provider=strongest["provider"],
            detail=(f"{_evidence(strongest)} for job {job_id}: a billable "
                    f"request may already have been sent and no remote id was "
                    f"recorded"))

    # 3. No link. Fall back to the workspace only for a job that has declared
    #    it spends money -- otherwise a single unresolved row in a busy
    #    workspace would stall every unrelated retry in it.
    declared = _is_declared_paid(job)
    workspace_rows: list[dict] = []
    if declared and job_type not in _paid_aware:
        workspace_rows = _open_paid_evidence(getattr(job, "workspace_id", None))
        if workspace_rows:
            strongest = workspace_rows[0]
            return PaidAssessment(
                verdict=PaidVerdict.BLOCKED, entry_id=strongest["entry_id"],
                provider=strongest["provider"],
                detail=(f"job {job_id} re-enters a workspace with an unresolved "
                        f"paid submission ({_evidence(strongest)}, "
                        f"{strongest['entry_id']}); refusing to re-spend"))
    if declared and job_type in _paid_aware:
        return PaidAssessment(
            verdict=PaidVerdict.DEFERRED,
            detail=(f"{job_type} reconciles its own prior submission; queue "
                    f"deferred to the handler"))
    return PaidAssessment(detail="no prior paid submission found")


def _is_declared_paid(job) -> bool:
    payload = getattr(job, "payload", None) or {}
    return bool(payload.get("paid")) if isinstance(payload, dict) else False


def _adopt(remote_id: str, why: str) -> PaidAssessment:
    """Bind to the ledger row that already owns ``remote_id``."""
    from app.services.paid_provider import reattach_by_remote_id

    adopted = reattach_by_remote_id(remote_id)
    if adopted is None:
        # The provider has a durable id and our ledger does not. The money may
        # already be gone and the only record of it died with the process. That
        # is an exposure to reconcile, NOT a licence to buy it again.
        return PaidAssessment(
            verdict=PaidVerdict.BLOCKED, remote_id=remote_id,
            detail=(f"{why}: the provider accepted remote id {remote_id} but no "
                    f"ledger row owns it, so the purchase is unaccounted for. "
                    f"Reconcile before any re-spend."))
    return PaidAssessment(
        verdict=PaidVerdict.REATTACH, remote_id=remote_id,
        entry_id=adopted.entry_id, provider=adopted.provider,
        detail=f"{why}: adopting the existing accounting for {remote_id}")


# ---------------------------------------------------------------------------
# Lease keeper
# ---------------------------------------------------------------------------


@dataclass
class LeaseKeeper:
    """Heartbeats every lease a worker holds, on a fixed cadence.

    A background *thread* rather than an asyncio task on purpose: the handlers
    run in ``asyncio.to_thread`` and some of them block the loop for minutes. A
    heartbeat that shares the loop is a heartbeat that stops when the process is
    busy, and a lease that stops being renewed is a job that gets stolen while
    it is still running -- the exact failure this module exists to remove.

    Renewal runs one statement per held job per tick, so a pool running fifty
    jobs writes fifty rows every few seconds and nothing else.
    """

    worker: str
    interval_seconds: float = 30.0
    ttl_seconds: float = 120.0
    _held: set[str] = field(default_factory=set)
    _thread: object = None
    _stop: object = None

    def track(self, job_id: str) -> None:
        self._held.add(str(job_id))

    def untrack(self, job_id: str) -> None:
        self._held.discard(str(job_id))

    @property
    def tracked(self) -> frozenset[str]:
        return frozenset(self._held)

    def start(self) -> None:
        import threading

        if self._thread is not None:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"lease-keeper-{self.worker}",
            daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def tick(self) -> int:
        """Renew everything held, once. Returns how many succeeded."""
        renewed = 0
        for job_id in sorted(self._held):
            try:
                if renew_lease(job_id, self.worker, ttl=self.ttl_seconds):
                    renewed += 1
            except Exception:  # noqa: BLE001 - a heartbeat never kills a worker
                logger.opt(exception=True).warning(
                    "lease renewal failed for job %s", job_id)
        return renewed

    def _run(self) -> None:
        stop = self._stop
        while stop is not None and not stop.wait(self.interval_seconds):
            self.tick()
