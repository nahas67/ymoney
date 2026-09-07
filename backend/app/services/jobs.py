"""Durable database-backed job queue.

Design:
- Jobs persist in the `jobs` table with status/priority/next_run_at.
- Workers claim jobs transactionally (UPDATE ... WHERE status='QUEUED' AND
  next_run_at <= now) so multiple processes are safe.
- Failures retry with exponential backoff; after max_retries they go DEAD
  (dead-letter visible via API).
- Idempotency keys prevent duplicate submissions.
- cancel_requested is checked by handlers between steps via a token object.

The queue runs inside the FastAPI process as asyncio tasks. For horizontal
scale, swap this module for a Redis/Celery implementation behind the same
`enqueue`/handler-registry interface.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, update

from app.core.config import settings
from app.db import session_scope
from app.models import AgentRun, Job
from app.models.base import JobStatus, utcnow

Handler = Callable[["JobContext"], Awaitable[dict]]

_handlers: dict[str, Handler] = {}
_worker_tasks: list[asyncio.Task] = []
_shutdown = asyncio.Event()


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
) -> str | None:
    """Enqueue a job. Returns job id, or None when idempotency dedupes it."""
    with session_scope() as s:
        if idempotency_key:
            existing = s.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing:
                return None
        job = Job(
            type=job_type,
            payload=payload,
            workspace_id=workspace_id,
            cycle_id=cycle_id,
            priority=priority,
            max_retries=settings.job_default_max_retries if max_retries is None else max_retries,
            next_run_at=utcnow() + timedelta(seconds=delay_seconds),
            idempotency_key=idempotency_key or None,
        )
        s.add(job)
        s.flush()
        return job.id


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
    """On startup, mark RUNNING jobs from a previous process as RETRYING."""
    with session_scope() as s:
        res = s.execute(
            update(Job)
            .where(Job.status == JobStatus.RUNNING.value)
            .values(status=JobStatus.RETRYING.value, next_run_at=utcnow())
        )
        return res.rowcount


async def start_workers(count: int | None = None) -> None:
    global _shutdown
    _shutdown = asyncio.Event()
    recovered = await asyncio.to_thread(recover_orphans)
    if recovered:
        logger.warning(f"recovered {recovered} orphaned job(s) from previous run")
    n = count or settings.job_worker_count
    for i in range(n):
        t = asyncio.create_task(_worker_loop(i), name=f"job-worker-{i}")
        _worker_tasks.append(t)


async def stop_workers() -> None:
    _shutdown.set()
    for t in _worker_tasks:
        try:
            await asyncio.wait_for(t, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            t.cancel()
    _worker_tasks.clear()


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

    pass


async def _worker_loop(worker_idx: int) -> None:
    logger.info(f"job worker #{worker_idx} started")
    while not _shutdown.is_set():
        job_ctx = await _claim_next()
        if not job_ctx:
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=settings.job_poll_interval_seconds)
                break
            except TimeoutError:
                continue
        await _execute(job_ctx)
    logger.info(f"job worker #{worker_idx} stopped")


async def _claim_next() -> JobContext | None:
    """Atomically claim the highest-priority due job.

    Uses a single UPDATE whose target id comes from a correlated subquery;
    SQLite/Postgres serialize the statement, so two workers can never claim
    the same row (the loser's outer status check yields rowcount 0).
    """
    def claim():
        now = utcnow()
        with session_scope() as s:
            cid = s.scalar(
                select(Job.id)
                .where(
                    Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRYING.value]),
                    Job.next_run_at <= now,
                )
                .order_by(Job.priority.asc(), Job.next_run_at.asc())
                .limit(1)
            )
            if not cid:
                return None
            # Conditional update = atomic claim. Concurrent workers serialize
            # at the DB write lock; the loser sees rowcount 0 and moves on.
            res = s.execute(
                update(Job)
                .where(Job.id == cid, Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRYING.value]))
                .values(status=JobStatus.RUNNING.value, started_at=now)
            )
            if res.rowcount != 1:
                return None
            job = s.get(Job, cid)
            ctx = JobContext(
                job_id=job.id,
                type=job.type,
                workspace_id=job.workspace_id,
                cycle_id=job.cycle_id,
                payload=dict(job.payload or {}),
                attempt=job.retry_count + 1,
                cancelled=lambda jid=job.id: _is_cancelled(jid),
            )
            return ctx

    return await asyncio.to_thread(claim)


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
    with session_scope() as s:
        run = AgentRun(
            workspace_id=workspace_id,
            agent_key=agent_key,
            task_type=task_type,
            job_id=job_id,
            cycle_id=cycle_id,
            input_summary=input_summary[:2000],
        )
        s.add(run)
        s.flush()
        return run.id


def finish_agent_run(run_id: str, status="COMPLETED", output_summary="", cost_usd=0.0, error="", steps=None):
    import datetime as dt

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
