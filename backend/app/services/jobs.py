"""Durable database-backed job queue.

Design:
- Jobs persist in the `jobs` table with status/priority/next_run_at.
- Workers take a LEASE (`services.job_leases`) and claim jobs transactionally
  (one conditional UPDATE whose `rowcount` decides), so multiple processes are
  safe.
- A running job may be recovered only when its lease EXPIRED. A live worker
  heartbeats, so a live job is never stolen; a dead worker stops heartbeating,
  so its job becomes recoverable within one TTL. `recover_orphans()` used to
  reset *every* RUNNING row on every worker boot, which with two workers meant
  an unrelated deploy could hand a live, possibly-billed render to a second
  worker and run it twice.
- Workers are pooled by workload class (`services.worker_pool`) so a long
  render cannot starve the planner, inbox sync, publishing or small jobs.
- Failures retry with exponential backoff; after max_retries they go DEAD
  (dead-letter visible via API).
- Idempotency keys prevent duplicate submissions -- including when two
  writers submit the same key concurrently, which returns the same ``None`` the
  sequential duplicate returns instead of leaking the database's 23505.
- A re-entry attempt consults the paid ledger before it runs
  (`job_leases.assess_paid_reentry`): a crash after a billable submit must not
  become a second purchase.
- cancel_requested is checked by handlers between steps via a token object.

The queue runs inside the FastAPI process as asyncio tasks. For horizontal
scale, the same `enqueue`/handler-registry interface runs behind more worker
processes; the lease, not a broker, is what makes that safe.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db import session_scope
from app.models import AgentRun, Job
from app.models.base import JobStatus, utcnow
from app.services import job_leases
from app.services.job_leases import ClaimedJob, PaidVerdict

Handler = Callable[["JobContext"], Awaitable[dict]]

_handlers: dict[str, Handler] = {}
_worker_tasks: list[asyncio.Task] = []
_shutdown = asyncio.Event()
#: The process-wide pool (services.worker_pool), kept here so `main.py` keeps
#: calling start_workers/stop_workers unchanged.
_pool = None


def register_handler(job_type: str, handler: Handler) -> None:
    if job_type in _handlers:
        raise ValueError(f"duplicate handler for {job_type}")
    _handlers[job_type] = handler


def handler(job_type: str):
    def deco(fn):
        register_handler(job_type, fn)
        return fn

    return deco


@dataclass
class JobContext:
    job_id: str
    type: str
    workspace_id: str | None
    cycle_id: str | None
    payload: dict
    attempt: int
    cancelled: Callable[[], bool]
    report_progress: Callable[[float], None] = lambda pct: None
    artifacts: dict = field(default_factory=dict)
    # -- Work 16 §2: what the recovery path decided about this attempt. BLOCKED
    # never reaches a handler, so a handler reading `recovery_mode` is looking
    # at "this attempt may adopt an existing paid submission".
    recovery_mode: str = ""
    #: The remote job a re-entry attempt must adopt instead of submitting again.
    paid_remote_id: str = ""
    #: The ledger row that already owns this operation's money.
    paid_entry_id: str = ""
    #: The lease holder, so a handler can log or report ownership.
    claimed_by: str = ""
    #: The workload class the pool routed this job to.
    workload: str = ""


def _unique_violation(exc: BaseException) -> bool:
    """Whether ``exc`` is a UNIQUE constraint being refused, on either backend.

    23505 on PostgreSQL; SQLite's driver reports no SQLSTATE and puts
    ``UNIQUE constraint failed`` in the message. Anything else (a NOT NULL, a
    foreign key, a CHECK) is a real bug and is NOT swallowed.
    """
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if state is not None:
        return str(state) == "23505"
    return "unique constraint failed" in str(orig).lower()


def _idempotency_key_won(stamp_key: str) -> bool:
    """Whether some other writer's row for this key committed.

    The confirming half of the concurrent-duplicate answer. A read-then-write
    ``SELECT``/``INSERT`` has a window between the two halves, and on a
    server-grade engine two writers walk through it together: both read "not
    there", both insert, and the UNIQUE index refuses the second with 23505 --
    correctly, but as an *exception*, which is not what a dedupe is allowed to
    look like to the caller. The index keeps guaranteeing one row per key; this
    only decides what the loser is told.

    Re-reading is deliberate rather than trusting the error text: it PROVES the
    key was taken by somebody, so a 23505 from some other constraint can never
    be silently converted into "duplicate, nothing to do".
    """
    with session_scope() as s:
        return s.scalar(
            select(Job.id).where(Job.idempotency_key == stamp_key)) is not None


def enqueue(
    job_type: str,
    payload: dict,
    *,
    workspace_id: str | None = None,
    cycle_id: str | None = None,
    priority: int = 100,
    delay_seconds: float = 0.0,
    max_retries: int | None = None,
    idempotency_key: str | None = None,
    paid: bool = False,
    workload: str = "",
) -> str | None:
    """Enqueue a job. Returns job id, or None when idempotency dedupes it.

    ``None`` is the answer for **both** duplicate paths, and making it so is the
    whole of Work 16 §4. The fast path -- a row with this key already exists --
    has always returned ``None``. The concurrent path did not: two writers that
    both missed the pre-read raced each other into the UNIQUE index, and the
    loser got a raised ``23505`` out of an API that is documented to return
    ``None`` for a duplicate. Callers were therefore forced to handle two
    different shapes of the same event, and the shape they could not handle was
    the one that only appeared under concurrency. Measured before this change on
    PostgreSQL 17 with eight real sessions on one key: three raised 23505, the
    rest returned ``None`` or an id. Integrity was never at risk -- the index
    held, one row always survived -- only the caller-visible contract was.

    ``paid=True`` is a one-word contract with the recovery path: *this job may
    spend money*. It is what makes :func:`job_leases.assess_paid_reentry`
    consult the ledger before a re-entry attempt, instead of running the handler
    and hoping the handler is idempotent about its own billable calls. It is
    opt-in deliberately: assuming every job spends money would let one
    unresolved paid row in a workspace stall every unrelated retry in it, and a
    stall nobody can explain is worse than the rare duplicate it prevents.

    ``workload`` overrides the pool's routing for this job.
    """
    body = dict(payload or {})
    if paid:
        body.setdefault("paid", True)
    if workload:
        body["workload"] = str(workload)
    key = str(idempotency_key or "") or None
    try:
        with session_scope() as s:
            if key:
                existing = s.scalar(
                    select(Job.id).where(Job.idempotency_key == key))
                if existing:
                    return None
            job = Job(
                type=job_type,
                payload=body,
                workspace_id=workspace_id,
                cycle_id=cycle_id,
                priority=priority,
                max_retries=settings.job_default_max_retries if max_retries is None else max_retries,
                next_run_at=utcnow() + timedelta(seconds=delay_seconds),
                idempotency_key=key,
            )
            s.add(job)
            s.flush()
            job_id = job.id
    except IntegrityError as exc:
        # ``session_scope`` has already rolled back, so this is a clean
        # transaction and the re-read below is not reading poisoned state.
        if not (key and _unique_violation(exc) and _idempotency_key_won(key)):
            raise
        logger.info(
            "enqueue of {} lost the idempotency race for key {}: the winner's "
            "row is canonical, so this is a no-op", job_type, key[:32])
        return None
    # Redis dispatch signal (best-effort; DB row is the source of truth).
    try:
        from app.services import queue_redis as _qr

        _qr.push(job_id)
    except Exception:
        pass
    return job_id


def _to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.type,
        "workspace_id": job.workspace_id,
        "cycle_id": job.cycle_id,
        "status": job.status,
        "priority": job.priority,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "next_run_at": job.next_run_at.isoformat() + "Z" if job.next_run_at else None,
        "started_at": job.started_at.isoformat() + "Z" if job.started_at else None,
        "completed_at": job.completed_at.isoformat() + "Z" if job.completed_at else None,
        "last_error": (job.last_error or "")[:400],
        "payload": job.payload or {},
        "result": job.result or {},
        "created_at": job.created_at.isoformat() + "Z",
        # Work 16 §2: ownership, so an operator can see who is running what
        # without reading the pool's logs.
        "claimed_by": job.claimed_by or "",
        "claimed_at": job.claimed_at.isoformat() + "Z" if job.claimed_at else None,
        "lease_expires_at": (job.lease_expires_at.isoformat() + "Z"
                             if job.lease_expires_at else None),
        "heartbeat_at": job.heartbeat_at.isoformat() + "Z" if job.heartbeat_at else None,
        "lease_state": str(job_leases.lease_state(job)),
    }


def get_job(job_id: str) -> dict | None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            return None
        return _to_dict(job)


def cancel_job(job_id: str) -> bool:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            return False
        if job.status in (JobStatus.QUEUED.value, JobStatus.WAITING.value, JobStatus.RETRYING.value):
            job.status = JobStatus.CANCELLED.value
            return True
        if job.status == JobStatus.RUNNING.value:
            job.cancel_requested = True
            return True
        return False


def list_jobs(workspace_id: str | None = None, status: str | None = None, limit: int = 100):
    with session_scope() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500))
        if workspace_id:
            q = q.where(Job.workspace_id == workspace_id)
        if status:
            q = q.where(Job.status == status)
        rows = s.scalars(q).all()
        s.expunge_all()
        return [_to_dict(j) for j in rows]


def recover_orphans() -> int:
    """Return lease-EXPIRED running jobs to the queue.

    **Work 16 §2 rewrote this function, and the old one was a live-work
    incident waiting to happen.** It read::

        UPDATE jobs SET status='RETRYING' WHERE status='RUNNING'

    on every worker boot, with no condition beyond "somebody is running this".
    One worker therefore decided the fate of every other worker's work: a
    second replica restarting for an unrelated deploy flipped a live 30-minute
    render back onto the queue, and whichever worker polled first ran it AGAIN --
    a duplicate render, a duplicate provider submission, a duplicate publish,
    and for a paid render a second invoice. Nothing in the schema distinguished
    "orphaned by a dead worker" from "actively running on a live worker", so the
    code had to assume the rare one and break the common one.

    Now the only thing that may authorise a steal is a lapsed lease. A live
    worker renews every third of its TTL, so its jobs are never eligible; a
    worker that died stops renewing, so its jobs become eligible within one TTL
    and this returns them. Same repair, restricted to the jobs that actually
    need it.

    ``status='WAITING'`` is untouched, which is what keeps the GPU slot ledger
    (``media_intel_runs``) intact: a held slot is parked in ``WAITING``, not
    ``RUNNING``, precisely so recovery cannot see it.
    """
    return len(job_leases.reclaim_expired())


async def start_workers(count: int | None = None) -> None:
    """Start the worker pool.

    ``count`` sizes the default single ``SMALL`` pool, preserving the
    pre-Work-16 behaviour for every deployment that has not configured
    ``job_worker_pools``. Startup runs one recovery sweep synchronously -- the
    crashed-worker jobs are the most urgent thing in the queue at boot -- and
    the pool then runs its own sweep on a timer, because the worker that died
    is usually not this process and nothing else would ever reclaim its work.
    """
    global _shutdown, _pool
    from app.services import worker_pool

    _shutdown = asyncio.Event()
    reclaimed = await asyncio.to_thread(recover_orphans)
    if reclaimed:
        logger.warning("recovered {} job(s) whose lease had expired", reclaimed)
    plan = None
    if count:
        # An explicit count is the pre-Work-16 request for "N workers", so it
        # gets N undifferentiated slots and behaviour identical to before. The
        # classed plan is the DEFAULT when no count is passed, because a caller
        # that never asked about pools must still not be able to starve its own
        # planner with a render.
        from app.services.worker_pool import PoolPlan

        plan = PoolPlan(slots={None: int(count)})
    _pool = worker_pool.WorkerPool(plan=plan)
    await _pool.start(_execute_claimed)
    _worker_tasks.extend(_pool._tasks)  # noqa: SLF001 - same-module lifecycle
    await worker_pool._start_reclaim_sweep()  # noqa: SLF001


async def stop_workers(timeout: float | None = None) -> None:
    """Drain: stop claiming, let in-flight work finish, bound the wait.

    Past the deadline the pool cancels its worker tasks, which leaves those
    jobs ``RUNNING`` with a lease that stops being renewed -- so the next
    recovery sweep picks them up. A job abandoned mid-render is recoverable; a
    paid render cancelled after the provider accepted it is not.
    """
    global _pool
    _shutdown.set()
    if _pool is not None:
        await _pool.drain(timeout)
        _pool = None
    for t in _worker_tasks:
        if not t.done():
            t.cancel()
    _worker_tasks.clear()


def pool_ownership() -> dict:
    """What this process currently holds. Operator/debuggable, no API needed."""
    from app.services import worker_pool

    running = worker_pool.running()
    if running is None:
        return {"worker": "", "slots": {}, "in_flight": []}
    return {
        "worker": running.worker,
        "slots": {str(k): v for k, v in running.plan.slots.items()},
        "in_flight": sorted(running.in_flight),
        "draining": running.draining,
    }


# The autopilot cycle is the ONE job type whose handler reconciles its own prior
# billable submission before it spends: ``production.py`` resolves by variant /
# request hash, refuses outright on a SUBMISSION_UNKNOWN prior attempt, and
# reattaches to a live engine task. That is a more precise correlation than the
# queue layer can make, so the paid re-entry gate records the finding and
# defers to it instead of blocking a whole cycle on an unrelated unresolved paid
# row in the same workspace. Declared here, never inferred -- guessing that a
# handler is idempotent is how one render is purchased twice.
job_leases.register_paid_job_type("autopilot.start_next_cycle")


def _is_cancelled(job_id: str) -> bool:
    with session_scope() as s:
        job = s.get(Job, job_id)
        return bool(job and job.cancel_requested)


class _Cancelled(Exception):
    pass


class _Backpressure(Exception):
    """Raised when a handler defers itself due to capacity limits.

    Treated as a clean, non-failure outcome: the job was already re-enqueued
    with a delay by the handler before raising this.
    """




def _gpu_enabled() -> bool:
    """This process may run GPU-gated jobs (operator-asserted lane)."""
    return bool(settings.gpu_worker)


def _for_update(query, session):
    """Row-level lock for the claim SELECT on Postgres; plain read elsewhere.

    Kept as a name here because the claim moved to :mod:`app.services.job_leases`
    and this module's existing tests reach for it directly. The behaviour is
    unchanged and now has one implementation instead of two.
    """
    return job_leases._for_update(query, session)


def _context_for(claimed: ClaimedJob) -> JobContext:
    """Build the handler's view of a claimed job."""
    return JobContext(
        job_id=claimed.job_id,
        type=claimed.type,
        workspace_id=claimed.workspace_id,
        cycle_id=claimed.cycle_id,
        payload=dict(claimed.payload or {}),
        attempt=claimed.attempt,
        cancelled=lambda jid=claimed.job_id: _is_cancelled(jid),
        claimed_by=claimed.claimed_by,
        workload=claimed.workload,
    )


async def _claim_next() -> JobContext | None:
    """Claim the next due job this process may run, under a lease.

    Thin wrapper over :mod:`app.services.job_leases` so the historical name and
    signature keep working for existing callers and tests. The claim itself is
    one conditional UPDATE whose ``rowcount`` decides the winner -- see
    ``job_leases._take`` for why that is atomic across processes.
    """
    worker = job_leases.worker_identity(settings.job_worker_identity)
    claimed = await asyncio.to_thread(job_leases.claim_next, worker)
    return _context_for(claimed) if claimed else None


async def _claim_by_id(job_id: str) -> JobContext | None:
    """Claim one specific job (Redis-dispatched ids). Same atomicity."""
    worker = job_leases.worker_identity(settings.job_worker_identity)
    claimed = await asyncio.to_thread(job_leases.claim_by_id, worker, job_id)
    return _context_for(claimed) if claimed else None


async def _claim_next_redis() -> JobContext | None:
    """BRPOP one dispatched id (blocking, shutdown-aware), then claim it."""
    from app.services import queue_redis as _qr

    if not _qr.configured():
        return await _claim_next()
    rid = await asyncio.to_thread(_qr.pop, settings.job_poll_interval_seconds)
    if _shutdown.is_set():
        return None
    if rid:
        claimed = await _claim_by_id(rid)
        if claimed:
            return claimed
        # id already handled/expired - fall through to a normal poll
    return await _claim_next()


async def _execute_claimed(claimed: ClaimedJob) -> None:
    """The pool's execute hook: paid gate, then handler, then outcome."""
    ctx = _context_for(claimed)

    # ---- Work 16 §2: the money gate, BEFORE the handler runs --------------
    # A re-entry attempt (a retry, or a job this very module just recovered from
    # a dead worker's lease) may already have spent. The pre-16 runner called
    # the handler unconditionally, which meant a crash between "provider
    # accepted" and "we wrote the result" became a second billable submit on
    # the next attempt -- Work 15.6 counted that path as reachable up to four
    # times per job.
    verdict = await asyncio.to_thread(
        job_leases.assess_paid_reentry, claimed, attempt=claimed.attempt)
    if verdict.verdict is PaidVerdict.BLOCKED:
        # No execution. A stuck job is an incident; a second purchase is an
        # invoice, and only one of those can be undone.
        _finish(ctx, JobStatus.WAITING, permanent=False,
                error=f"paid re-entry blocked: {verdict.detail}"[:4000],
                result={"paid_reentry": str(PaidVerdict.BLOCKED),
                        "remote_id": verdict.remote_id,
                        "cost_entry_id": verdict.entry_id,
                        "provider": verdict.provider,
                        "detail": verdict.detail})
        _emit_paid_reentry(ctx, verdict)
        return
    if verdict.verdict is PaidVerdict.REATTACH:
        # The provider already has this work. The handler resumes it through
        # ``ctx.paid_remote_id``; nothing is submitted and nothing is booked a
        # second time (paid_provider.reattach_by_remote_id owns that).
        ctx.recovery_mode = str(PaidVerdict.REATTACH)
        ctx.paid_remote_id = verdict.remote_id
        ctx.paid_entry_id = verdict.entry_id
        ctx.artifacts["paid_reattached_cost_row"] = verdict.entry_id
        logger.warning(
            "job {} re-entering a paid operation it already bought "
            "(remote_id={}, cost row={}): the handler must adopt it, not "
            "submit again", ctx.job_id, verdict.remote_id, verdict.entry_id)
    elif verdict.verdict is PaidVerdict.DEFERRED:
        ctx.recovery_mode = str(PaidVerdict.DEFERRED)

    await _execute(ctx)


def _emit_paid_reentry(ctx: JobContext, verdict) -> None:
    """Make a blocked re-entry visible. A job nobody can see is not resolved."""
    try:
        from app.services.events import record_event

        record_event(
            workspace_id=ctx.workspace_id,
            kind="paid.reentry_blocked",
            message=(
                f"Refusing to re-run {ctx.type}: a billable submission for it "
                f"may already have been made ({verdict.detail}). No provider "
                f"request was sent; reconcile before re-spending."),
            level="error",
            source="jobs",
            data={"job_id": ctx.job_id, "type": ctx.type,
                  "remote_id": verdict.remote_id,
                  "cost_entry_id": verdict.entry_id,
                  "provider": verdict.provider,
                  "attempt": ctx.attempt},
        )
    except Exception:  # pragma: no cover - telemetry never fails a job
        pass


async def _execute(ctx: JobContext) -> None:
    handler_fn = _handlers.get(ctx.type)
    started = time.monotonic()
    if handler_fn is None:
        _finish(ctx, JobStatus.FAILED, error=f"no handler registered for '{ctx.type}'", permanent=True)
        return
    try:
        # Sync handlers (the common case: pipeline stages doing blocking IO)
        # run in a worker thread so the event loop stays responsive.
        result = await asyncio.to_thread(handler_fn, ctx)
        duration_ms = int((time.monotonic() - started) * 1000)
        _finish(ctx, JobStatus.COMPLETED, result={"duration_ms": duration_ms, "output": result or {}})
        logger.info(f"job {ctx.type}({ctx.job_id}) completed in {duration_ms}ms")
    except _Cancelled:
        _finish(ctx, JobStatus.CANCELLED, error="cancelled by request")
    except _Backpressure as bp:
        _finish(ctx, JobStatus.WAITING, result={"deferred": str(bp)})
        logger.info(f"job {ctx.type}({ctx.job_id}) deferred: {bp}")
    except Exception as exc:
        logger.opt(exception=exc).error(f"job {ctx.type}({ctx.job_id}) failed: {exc}")
        exc_type = type(exc).__name__
        exc_msg = str(exc)

        def record_failure():
            with session_scope() as s:
                job = s.get(Job, ctx.job_id)
                retries_left = job.retry_count < job.max_retries
                job.retry_count += 1
                job.last_error = f"{exc_type}: {exc_msg}"[:4000]
                backoff = min(2 ** job.retry_count * 5, 600)
                if retries_left:
                    job.status = JobStatus.RETRYING.value
                    job.next_run_at = utcnow() + timedelta(seconds=backoff)
                else:
                    job.status = JobStatus.DEAD.value
                    job.completed_at = utcnow()
                # The outcome is written, so the lease is released in the same
                # statement. A COMPLETED job holding a renewable lease would
                # keep looking recoverable to an operator reading the queue.
                job.claimed_by = ""
                job.lease_expires_at = None
                job.heartbeat_at = None
                return job.status

        final_status = await asyncio.to_thread(record_failure)
        if final_status == JobStatus.DEAD.value:
            _on_dead(ctx, exc)
def _finish(
    ctx: JobContext, status: JobStatus, *, result: dict | None = None, error: str | None = None, permanent: bool = False
) -> None:
    with session_scope() as s:
        job = s.get(Job, ctx.job_id)
        if not job:
            return
        job.status = status.value
        job.completed_at = utcnow()
        job.result = result or {}
        job.last_error = error or ""
        if permanent:
            job.max_retries = 0
        # Release the lease with the outcome, in one transaction. Leaving a
        # finished job holding a renewable lease makes it look recoverable to
        # an operator reading the queue, and leaves ``claimed_by`` naming a
        # worker that is demonstrably doing nothing.
        job.claimed_by = ""
        job.claimed_at = None
        job.lease_expires_at = None
        job.heartbeat_at = None


def _on_dead(ctx: JobContext, exc: Exception) -> None:
    """Dead-letter hook — surface into events feed and fail the owning cycle."""
    try:
        from app.services.events import record_event

        record_event(
            workspace_id=ctx.workspace_id,
            kind="job.dead",
            message=f"Job {ctx.type} failed permanently after {ctx.attempt} attempts: {exc}",
            level="error",
            source="jobs",
            data={"job_id": ctx.job_id, "type": ctx.type},
        )
    except Exception:  # pragma: no cover
        pass
    if ctx.type.startswith("cycle."):
        try:
            from app.db import session_scope
            from app.models import Cycle, ScheduleEntry

            with session_scope() as s:
                # A scheduled upload (cycle_id=None) has no owning cycle to
                # fail, so surface it as FAILED on the schedule entry itself.
                # Without this, a retryable platform failure that exhausts the
                # queue would leave the entry QUEUED forever and the sweep's
                # lease recovery would re-claim it every few minutes against a
                # dead idempotency key.
                scheduled_entry_id = (ctx.payload or {}).get("scheduled_entry_id")
                if scheduled_entry_id and not ctx.cycle_id:
                    entry = s.get(ScheduleEntry, scheduled_entry_id)
                    if entry and entry.workspace_id == ctx.workspace_id and entry.status != "DONE":
                        entry.status = "FAILED"
                if ctx.cycle_id:
                    cycle = s.get(Cycle, ctx.cycle_id)
                    if cycle and cycle.status == "RUNNING":
                        cycle.status = "FAILED"
                        cycle.error = f"stage {cycle.stage} failed permanently: {exc}"
                        cycle.finished_at = utcnow()
        except Exception:  # pragma: no cover
            logger.exception("failed to mark cycle failed after dead-letter")
        # let the autopilot decide whether to continue with the next cycle
        try:
            from app.engine.autopilot import on_cycle_failed

            if ctx.workspace_id and ctx.cycle_id:
                on_cycle_failed(ctx.workspace_id, ctx.cycle_id, str(exc))
        except Exception:  # pragma: no cover
            logger.exception("autopilot failure handler errored")


# ---------------------------------------------------------------------------
# Agent-run recording helper used by engine agents
# ---------------------------------------------------------------------------


def start_agent_run(workspace_id, agent_key, task_type, job_id=None, cycle_id=None, input_summary="") -> str:
    from app.core.request_context import request_id as _rid

    with session_scope() as s:
        run = AgentRun(
            workspace_id=workspace_id,
            agent_key=agent_key,
            task_type=task_type,
            job_id=job_id,
            cycle_id=cycle_id,
            input_summary=input_summary[:2000],
            request_id=_rid(),
        )
        s.add(run)
        s.flush()
        return run.id


def finish_agent_run(run_id: str, status="COMPLETED", output_summary="", cost_usd=0.0, error="", steps=None):

    with session_scope() as s:
        run = s.get(AgentRun, run_id)
        if not run:
            return
        run.status = status
        run.output_summary = output_summary[:4000]
        run.cost_usd = cost_usd
        run.error = error[:2000]
        if steps:
            run.steps_json = {"steps": list(steps)}
        run.duration_ms = int((utcnow() - run.created_at.replace(tzinfo=None)).total_seconds() * 1000)


def check_cancelled(ctx: JobContext) -> None:
    """Handlers call this between steps; raises to trigger clean cancellation."""
    if ctx.cancelled():
        raise _Cancelled()


def raise_if_cancelled(job_id: str) -> None:
    if _is_cancelled(job_id):
        raise _Cancelled()
