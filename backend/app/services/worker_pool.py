"""Worker pools: which class of work a worker will touch, and how to stop.

Work 16 §3. One undifferentiated pool is fine right up until a single job runs
for thirty minutes. Then it is not fine, because the worker holding that render
is a worker that is not reading the inbox sync, not publishing, not running the
planner, and not answering the twenty small jobs that arrive in the meantime.
The queue is at its busiest exactly when a long job starts, so the long job is
guaranteed to overlap the peak.

The fix is separation, and the discipline is *not to build microservices*. Every
class here runs as a task inside the same process, against the same database,
with the same handler registry. What changes is only which rows a given worker
is willing to claim. That is the whole point: the expensive property of a
distributed queue -- a worker that cannot be blocked by its neighbours -- is
achievable with a WHERE clause and a per-class limit, and building a service
boundary to get it would add a network hop, a second deployment, a second set
of failure modes and a second thing to page somebody about.

The classes, and what each one is protecting:

``SMALL``
    Latency-sensitive, cheap, must-never-wait: the planner's bookkeeping, inbox
    sync, retention sweeps, small agent runs. The default, and the class that
    would otherwise be starved by everything below.
``CPU``
    Local compute (transcode, audio analysis, hashing).
``IO``
    Waiting on the network: connector syncs, analytics collection, exports.
``RENDER``
    Video/lip-sync/avatar/b-roll. Minutes, not seconds, and usually money.
``GPU``
    GPU-gated work, which additionally requires ``settings.gpu_worker``.
``PUBLISH``
    Anything that can reach an audience. Isolated because it is the one class
    whose mistakes are public and irreversible.
``INTELLIGENCE``
    LLM and enrichment calls: slow, token-spending, and safe to defer.

Draining is the other half. Shutdown is not "stop now": a worker sets
``drain``, which means *stop claiming, finish what you hold, and say so*.
In-flight work gets a bounded deadline. Past the deadline the remaining jobs
are **not cancelled** -- their leases are simply left to lapse so the recovery
sweep reclaims them on another process. Cancelling a paid render mid-flight is
the worst of the available outcomes: the money is committed and the artifact
never lands.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum

from loguru import logger

from app.services import job_leases

__all__ = [
    "PoolPlan",
    "Workload",
    "WORKLOADS",
    "WorkerPool",
    "classify_workload",
    "drain",
    "parse_pools",
    "reclaim_now",
    "running",
    "start",
]


class Workload(StrEnum):
    """One class of work. The unit of starvation prevention."""

    SMALL = "SMALL"
    CPU = "CPU"
    IO = "IO"
    RENDER = "RENDER"
    GPU = "GPU"
    PUBLISH = "PUBLISH"
    INTELLIGENCE = "INTELLIGENCE"

    def __str__(self) -> str:
        return self.value


WORKLOADS: tuple[Workload, ...] = tuple(Workload)

#: Substrings that route a job type to a class. Ordered: first match wins, so
#: the specific tokens come before the general ones. This is a HINT table, not a
#: policy engine -- a producer that disagrees passes ``payload["workload"]`` and
#: overrides the whole thing.
_ROUTE_HINTS: tuple[tuple[Workload, tuple[str, ...]], ...] = (
    (Workload.GPU, ("gpu",)),
    (Workload.RENDER, ("render", "video", "lipsync", "lip_sync", "avatar",
                       "broll", "b_roll", "motion", "dubbing", "tts")),
    (Workload.PUBLISH, ("publish", "upload", "distribution")),
    (Workload.INTELLIGENCE, ("intel", "research", "script", "enrich",
                             "llm", "knowledge", "score", "analyz")),
    (Workload.IO, ("sync", "inbox", "export", "collect", "fetch", "analytics")),
    (Workload.CPU, ("transcod", "analyse_", "analyze_", "embed", "resize")),
)


def classify_workload(job_type: str, payload: dict | None = None) -> Workload:
    """Which class a job belongs to.

    Resolution order, strongest first:

    1. ``payload["workload"]`` -- the producer's explicit word. Nothing
       overrides it, including the hint table, because the producer is the only
       party that knows what the work will actually do.
    2. ``payload["requires_gpu"]`` -- the GPU gate is already a first-class flag
       in the queue (``jobs`` reads it today), so honour it directly instead of
       hoping the type name mentions a GPU.
    3. The hint table against the lowercased type.
    4. ``SMALL``, which is the class that must never be starved, so an
       unclassified job lands where it cannot block the important ones.
    """
    body = payload if isinstance(payload, dict) else {}
    explicit = str(body.get("workload") or "").strip().upper()
    if explicit:
        try:
            return Workload(explicit)
        except ValueError:
            logger.warning("job declared an unknown workload %r; ignoring it",
                           body.get("workload"))
    if body.get("requires_gpu"):
        return Workload.GPU
    token = str(job_type or "").lower()
    for workload, needles in _ROUTE_HINTS:
        if any(needle in token for needle in needles):
            return workload
    return Workload.SMALL


# ---------------------------------------------------------------------------
# Pool sizing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolPlan:
    """How many worker slots this process runs, per class.

    The key is a :class:`Workload`, or ``None`` for an *undifferentiated* slot
    that takes any class. ``None`` exists for the legacy call
    ``start_workers(count=N)``: a caller that asks for "N workers" gets exactly
    the pre-Work-16 pool, so nothing that sized its concurrency by argument
    silently changes behaviour by upgrading.
    """

    slots: dict = field(default_factory=dict)

    def count(self, workload: Workload | str) -> int:
        return int(self.slots.get(Workload(workload), 0))

    @property
    def total(self) -> int:
        return sum(self.slots.values())

    def as_text(self) -> str:
        return ", ".join(
            f"{'ANY' if k is None else k}={v}"
            for k, v in sorted(self.slots.items(), key=lambda kv: str(kv[0])))


def default_plan(count: int) -> PoolPlan:
    """A pool that cannot starve anything, with no configuration at all.

    ``job_worker_count`` SMALL slots (the class that must never wait) plus one
    slot for every other class. The extra slots cost one indexed SELECT per poll
    interval each, and they are the whole point: in one undifferentiated pool a
    long render occupies a quarter of the workers and the rest must still cover
    everything else, which is the arithmetic that fails the moment a second
    render starts.

    GPU gets a slot only when ``gpu_worker`` is set. A pool that allocated GPU
    capacity on a CPU-only process would accept work it cannot run.
    """
    from app.core.config import settings

    slots: dict = {Workload.SMALL: max(1, int(count))}
    for workload in (Workload.RENDER, Workload.IO, Workload.PUBLISH,
                     Workload.INTELLIGENCE, Workload.CPU):
        slots[workload] = 1
    if settings.gpu_worker:
        slots[Workload.GPU] = 1
    return PoolPlan(slots=slots)


def parse_pools(spec: str | None, *, default_count: int) -> PoolPlan:
    """Parse ``"SMALL=4,RENDER=2,GPU=1"``.

    An empty spec means :func:`default_plan` -- every class covered -- and not
    the pre-16 single pool. That default is the requirement rather than a
    convenience: "a render must not starve the planner" has to hold on a
    deployment nobody has configured.
    """
    from app.core.config import settings

    raw = str(spec or "").strip()
    if not raw:
        return default_plan(default_count)
    slots: dict = {}
    for chunk in raw.split(","):
        name, _, value = chunk.partition("=")
        token = name.strip().upper()
        try:
            count = max(0, int(value))
        except (TypeError, ValueError):
            logger.warning("worker pool entry %r is not a count; ignoring",
                           chunk.strip())
            continue
        if not count:
            continue
        if token == "ANY":
            # Explicit opt-in to the undifferentiated pool.
            slots[None] = count
            continue
        try:
            slots[Workload(token)] = count
        except ValueError:
            logger.warning("worker pool config names unknown workload %r; "
                           "ignoring that entry", name.strip())
    if not slots:
        logger.warning("no usable worker pool in %r; falling back to the "
                       "default plan", raw)
        return default_plan(default_count)
    if Workload.GPU in slots and not settings.gpu_worker:
        logger.warning("worker pool allocates GPU slots but gpu_worker is "
                       "false; GPU jobs will be deferred")
    return PoolPlan(slots=slots)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


#: How many consecutive ``CONTENDED`` polls a slot will retry before it stops
#: believing itself and falls back to the poll-interval sleep.
#:
#: This is a safety rail, not the mechanism. ``CONTENDED`` is a bounded condition
#: and not a spin: every retry either wins a job, or observes strictly fewer
#: free rows than the retry before it (each retry's candidate window excludes
#: the ids the previous one lost), or is looking at rows a peer holds behind a
#: row lock that a single non-blocking statement is about to release. So the
#: rail should essentially never be reached. It exists so that a pathological
#: backend -- one that blocks instead of skipping, or a class filter that can
#: never match -- degrades into a bounded backoff instead of a hot loop.
_MAX_CONTENDED_POLLS = 8


class WorkerPool:
    """Owns this process's worker tasks, their leases, and their shutdown.

    Three moving parts, and each exists for a reason that a simpler pool would
    get wrong:

    * **one task per class slot**, each pinned to its class. A render slot can
      be busy for half an hour without touching the SMALL slots, so inbox sync
      and the planner keep their latency.
    * **one :class:`~app.services.job_leases.LeaseKeeper` thread** for the whole
      pool, heartbeating every held lease. A thread, not a task, because the
      handlers run in ``asyncio.to_thread`` and can block the loop for minutes;
      a heartbeat that shares the loop is a heartbeat that stops exactly when it
      is needed.
    * **a drain flag** that stops claiming without cancelling. See
      :func:`drain`.
    """

    def __init__(self, *, worker: str | None = None,
                 plan: PoolPlan | None = None,
                 lease_ttl: float | None = None) -> None:
        from app.core.config import settings

        self.worker = job_leases.worker_identity(settings.job_worker_identity) \
            if worker is None else worker
        self.plan = plan or parse_pools(
            settings.job_worker_pools, default_count=settings.job_worker_count)
        self.lease_ttl = float(lease_ttl if lease_ttl is not None
                               else job_leases.lease_seconds())
        self._tasks: list[asyncio.Task] = []
        self._draining = asyncio.Event()
        self._started = False
        self.keeper = job_leases.LeaseKeeper(
            worker=self.worker,
            # A third of the TTL: two consecutive missed beats still leave a
            # full TTL of slack, and the write cost is three rows per TTL.
            interval_seconds=max(1.0, self.lease_ttl / 3.0),
            ttl_seconds=self.lease_ttl)

    # -- lifecycle -------------------------------------------------------

    async def start(self, execute) -> None:
        """Run the pool. ``execute(claimed)`` is the caller's job runner."""
        if self._started:
            return
        self._started = True
        self.keeper.start()
        logger.info("job worker pool {} starting: {}", self.worker,
                    self.plan.as_text() or "(no slots)")
        index = 0
        for workload, count in sorted(self.plan.slots.items(),
                                      key=lambda kv: str(kv[0])):
            for _ in range(int(count)):
                self._tasks.append(asyncio.create_task(
                    self._slot(workload, index, execute),
                    name=f"job-worker-{workload or 'ANY'}-{index}"))
                index += 1

    async def _slot(self, workload: Workload | None, index: int, execute) -> None:
        """One worker loop, pinned to one workload class (``None`` = any)."""
        from app.core.config import settings

        name = f"{self.worker}#{index}"
        # One identity per slot, used for the claim, the CLAIMED->RUNNING
        # transition and every heartbeat. A slot whose three writes used
        # different names could never renew its own lease.
        slot_id = f"{name}:{workload or 'ANY'}"
        pinned = "" if workload is None else str(workload)
        logger.info("job worker {} started", slot_id)
        contended_streak = 0
        try:
            while not self._draining.is_set():
                poll = await asyncio.to_thread(
                    job_leases.claim_next_poll, slot_id, workload=pinned)
                claimed = poll.job
                if claimed is not None:
                    contended_streak = 0
                    job_leases.begin_run(claimed.job_id, slot_id)
                    self.keeper.track(claimed.job_id)
                    try:
                        await execute(claimed)
                    finally:
                        self.keeper.untrack(claimed.job_id)
                    continue
                # A poll that found claimable work and lost it is NOT the same
                # as a poll that found an empty queue, and treating them the
                # same is what let a pool of eight slots drain a depth-eight
                # queue as if it were one: the losers slept for a full poll
                # interval while the single slot that kept winning never slept
                # at all, because a slot that wins loops straight back round.
                # So a contended poll yields to the event loop and looks again.
                if poll.contended and contended_streak < _MAX_CONTENDED_POLLS:
                    contended_streak += 1
                    await asyncio.sleep(0)
                    continue
                contended_streak = 0
                try:
                    await asyncio.wait_for(
                        self._draining.wait(),
                        timeout=settings.job_poll_interval_seconds)
                    break
                except TimeoutError:
                    continue
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception:  # noqa: BLE001 - one slot must not kill the pool
            logger.opt(exception=True).error(
                "job worker %s died", slot_id)
        logger.info("job worker {} stopped", slot_id)

    async def drain(self, timeout: float | None = None) -> None:
        """Stop claiming, let in-flight work finish, bound the wait.

        Past ``timeout`` the remaining worker tasks are cancelled, which for a
        *blocked* handler means its job stays ``RUNNING`` with a lease that
        stops being renewed. That is deliberate: the recovery sweep then
        reclaims it exactly as it would reclaim a crashed worker, which is
        recoverable, whereas a cancelled render is money spent and no artifact.
        """
        from app.core.config import settings

        deadline = float(timeout if timeout is not None
                         else settings.job_drain_timeout_seconds)
        self._draining.set()
        if not self._tasks:
            return
        logger.info("job worker pool {} draining (deadline {:.1f}s, {} in flight)",
                    self.worker, deadline, len(self.keeper.tracked))
        done, pending = await asyncio.wait(self._tasks,
                                           timeout=max(0.0, deadline))
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await task
        self.keeper.stop()
        self._tasks.clear()
        self._started = False
        if pending:
            logger.warning(
                "drain deadline passed with {} worker(s) still busy; their "
                "leases were left to expire so the jobs are recovered, not "
                "lost ({})", len(pending), ", ".join(sorted(self.keeper.tracked)))
        else:
            logger.info("job worker pool {} drained cleanly", self.worker)

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    @property
    def in_flight(self) -> frozenset[str]:
        return self.keeper.tracked


# ---------------------------------------------------------------------------
# Process-wide convenience, and the recovery tick
# ---------------------------------------------------------------------------

_pool: WorkerPool | None = None
_reclaim_task: asyncio.Task | None = None


def running() -> WorkerPool | None:
    """The process-wide pool, if one is running."""
    return _pool


async def start(execute, *, worker: str | None = None,
                plan: PoolPlan | None = None) -> WorkerPool:
    """Start the process-wide pool and its recovery sweep."""
    global _pool
    _pool = WorkerPool(worker=worker, plan=plan)
    await _pool.start(execute)
    await _start_reclaim_sweep()
    return _pool


async def drain(timeout: float | None = None) -> None:
    """Drain the process-wide pool if there is one."""
    global _pool, _reclaim_task
    if _reclaim_task is not None:
        _reclaim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _reclaim_task
        _reclaim_task = None
    if _pool is not None:
        await _pool.drain(timeout)
        _pool = None


async def _start_reclaim_sweep() -> None:
    """Reclaim expired leases on a timer.

    A timer rather than only on boot, because the worker that dies is often
    not this process and nobody will restart anything: without a sweep, a
    crashed worker's jobs sit ``RUNNING`` until an unrelated deploy happens to
    boot the API. The sweep is cheap when there is nothing to do -- one indexed
    query on ``(status, lease_expires_at)`` per tick.
    """
    global _reclaim_task
    from app.core.config import settings

    if _reclaim_task is not None:
        return
    interval = max(1.0, float(settings.job_reclaim_interval_seconds))

    async def _sweep() -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                reclaimed = await asyncio.to_thread(_reclaim_once)
                if reclaimed:
                    logger.warning("lease recovery reclaimed {} job(s)",
                                   reclaimed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sweep failure must not kill the pool
                logger.opt(exception=True).error("lease recovery sweep failed")

    _reclaim_task = asyncio.create_task(_sweep(), name="job-lease-reclaim")


def _reclaim_once() -> int:
    return len(job_leases.reclaim_expired())


def reclaim_now() -> int:
    """Run one recovery sweep synchronously. For startup and for operators."""
    return _reclaim_once()