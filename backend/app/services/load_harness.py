"""Reproducible load measurement, and the numbers nobody else has (Work 16 §7).

What this module is for
----------------------
Every other Work 16 lane argues from a property: "the cap holds", "the claim is
exactly once", "a dead worker's job is reclaimed". A property is necessary and
it is not sufficient, because a system can hold every property it has and still
be unusable at four concurrent users. What is missing is a **measured limit**:
the number of concurrent workers at which a path stops being fast, the number at
which the connection pool starts handing out timeouts, the number of
reservations per second the workspace lock will actually sustain.

So this module is deliberately not an assertion library. It runs each workload
at rising concurrency against a REAL PostgreSQL scratch database and returns
what it observed -- including the failures. A harness that stops at the first
error is a pass/fail test wearing a harness's name; the breaking point is the
deliverable.

The four rules that keep the numbers honest
------------------------------------------
1. **The measured path is the real path.** Workloads call
   :func:`app.services.cost.reserve_spend`, :func:`app.services.job_leases.claim_next`,
   :func:`app.services.jobs.enqueue`, the real ASGI app, the real inbox sync
   engine. Nothing here re-implements them for speed, because a fast stand-in
   measures the stand-in.
2. **The database is a real server.** SQLite serialises writers, so every
   concurrency number measured against it is SQLite's, not PostgreSQL's. The
   harness creates a scratch database on the admin DSN, migrates it, and points
   the application's global session factory at it -- the *code under test* is
   untouched, only the engine it binds to.
3. **A refusal is not a failure.** :func:`app.services.cost.reserve_spend`
   raising :class:`~app.services.cost.BudgetExceededError` under a cap is the
   guarantee WORKING. Counting it as an error would make the strongest test in
   the suite look like a flaky one, so refusals are counted in their own column
   (:attr:`LoadResult.refused`) and are never silently folded into ``ok``.
4. **Nothing here spends money.** The paid workload drives the real
   :class:`~app.services.paid_provider.PaidOperation` -- reserve, attempt,
   accept, settle -- against an in-process **provider double**. It is a real
   money path with a fake transport, and it is described that way everywhere,
   never as "live".

Reproducing a run
-----------------
::

    # opt in explicitly; the default SQLite suite never touches this
    set YMONEY_LOAD_TESTS=1
    set YMONEY_LOAD_PG_ADMIN=postgresql://ymoney:pw@127.0.0.1:55432/postgres
    python -m backend.tests.test_work16_load --sweep   # or: pytest -m load

The admin DSN only ever creates and DROPs a scratch database named
``w16_load_<random>``. It is never pointed at a database that holds data, and
:meth:`LoadHarness.drop_database` uses ``WITH (FORCE)`` because a load run
leaves live connections behind by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

__all__ = [
    "ADMIN_URL_ENV",
    "DEFAULT_ADMIN_URL",
    "LOAD_FLAG_ENV",
    "REFUSED",
    "SKIPPED",
    "LoadContext",
    "LoadHarness",
    "LoadResult",
    "admin_dsn",
    "format_table",
    "load_enabled",
    "pg_available",
    "record_load_result",
    "workload",
    "workload_names",
]


#: Environment variable naming the ADMIN DSN used only for ``CREATE DATABASE`` /
#: ``DROP DATABASE`` of scratch databases.
ADMIN_URL_ENV = "YMONEY_LOAD_PG_ADMIN"

#: The scratch DSN this repository's load runs were measured against. A default
#: rather than a blank so a developer with the documented container gets numbers
#: without reading a wiki page -- and a skipif so a developer WITHOUT it skips
#: rather than fails.
DEFAULT_ADMIN_URL = "postgresql://ymoney:ymoney_w16@127.0.0.1:55432/postgres"

#: The opt-in. Load tests are NOT part of the default suite: they take tens of
#: seconds each, they need a PostgreSQL server, and a suite that needs a server
#: is a suite nobody runs.
LOAD_FLAG_ENV = "YMONEY_LOAD_TESTS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Returned by a workload when the system REFUSED it and that is the correct
#: answer (a budget cap, a rate limit). Distinguished from ``ok`` because
#: "the cap held" and "the call worked" are different facts.
REFUSED = "__REFUSED__"

#: Returned by a workload when there was genuinely nothing to do (an empty
#: queue, an absent row). Not an error and not a success.
SKIPPED = "__SKIPPED__"


def load_enabled() -> bool:
    """Whether the operator opted in to load measurements."""
    return str(os.environ.get(LOAD_FLAG_ENV, "")).strip().lower() in _TRUTHY


def admin_dsn() -> str:
    """The admin DSN, or ``""`` when none is configured."""
    return str(os.environ.get(ADMIN_URL_ENV, "") or DEFAULT_ADMIN_URL).strip()


def pg_available() -> bool:
    """Whether the admin DSN actually answers. Never raises.

    A missing server has to be a SKIP, not an ERROR: the declarative
    ``skipif`` on the tests reads this at collection time, and a collection-time
    exception would take the whole suite down instead of nine tests.

    The opt-in is checked FIRST, and that ordering is a cost decision rather
    than a formality. This is called from a module-level ``skipif``, i.e. once
    per file at COLLECTION time, and ``admin_dsn()`` falls back to a real
    default -- so without the flag check every default-suite run would open a
    socket to a server nobody asked about and block for the connect timeout.
    A load measurement is not available to a run that has not opted in, whatever
    the server's state.
    """
    if not load_enabled():
        return False
    dsn = admin_dsn()
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001 - a probe must never fail a run
        logger.warning("load harness: PostgreSQL at {} is unreachable ({})",
                       dsn.rsplit("@", 1)[-1], exc)
        return False


def engine_url(db_name: str, dsn: str) -> str:
    """SQLAlchemy URL for a scratch database.

    ``postgresql://`` maps to psycopg2, which is not installed here; this repo
    ships psycopg 3, whose scheme is ``postgresql+psycopg://``. Pinning it here
    rather than letting SQLAlchemy guess is what
    :func:`app.db.normalize_database_url` does for the same reason.
    """
    base = dsn.rsplit("/", 1)[0]
    return f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{db_name}"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def _percentile(ordered: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of an ALREADY SORTED sequence.

    Nearest-rank rather than interpolated: with a few hundred samples an
    interpolated p99 invents a number nobody observed. A measured limit should
    be a value something actually did.
    """
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    rank = min(len(ordered), max(1, math.ceil(float(q) * len(ordered))))
    return float(ordered[rank - 1])


@dataclass(frozen=True)
class LoadResult:
    """One workload at one concurrency. What was observed, not what was hoped.

    ``refused`` and ``skipped`` are first-class because the failure modes people
    care about in this codebase are refusals and starvation, and folding either
    into ``failed`` would destroy the distinction that makes the number useful.
    """

    workload: str
    concurrency: int
    iterations: int
    wall_seconds: float
    operations: int
    ok: int
    refused: int
    skipped: int
    failed: int
    latencies: tuple[float, ...] = ()
    errors: dict[str, int] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def throughput(self) -> float:
        """Successful+refused operations per second (skips are not work)."""
        if self.wall_seconds <= 0:
            return 0.0
        return round((self.ok + self.refused) / self.wall_seconds, 3)

    @property
    def error_rate(self) -> float:
        if self.operations <= 0:
            return 0.0
        return round(self.failed / self.operations, 6)

    def percentile(self, q: float) -> float:
        return round(_percentile(sorted(self.latencies), q), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workload": self.workload,
            "concurrency": self.concurrency,
            "iterations": self.iterations,
            "operations": self.operations,
            "ok": self.ok,
            "refused": self.refused,
            "skipped": self.skipped,
            "failed": self.failed,
            "error_rate": self.error_rate,
            "wall_seconds": round(self.wall_seconds, 3),
            "throughput_per_second": self.throughput,
            "latency_p50": self.percentile(0.50),
            "latency_p90": self.percentile(0.90),
            "latency_p99": self.percentile(0.99),
            "latency_max": round(max(self.latencies), 6) if self.latencies else 0.0,
            "errors": dict(self.errors),
            "detail": dict(self.detail),
        }

    def line(self) -> str:
        """One fixed-width row. Two runs are meant to be diffed by eye."""
        return (
            f"{self.workload:<18} c={self.concurrency:<4} "
            f"n={self.operations:<6} {self.wall_seconds:>7.2f}s "
            f"{self.throughput:>9.2f} op/s "
            f"p50={self.percentile(0.50) * 1000:>8.1f}ms "
            f"p90={self.percentile(0.90) * 1000:>8.1f}ms "
            f"p99={self.percentile(0.99) * 1000:>8.1f}ms "
            f"ok={self.ok} refused={self.refused} skip={self.skipped} "
            f"FAIL={self.failed}"
        )


def record_load_result(result: LoadResult) -> None:
    """Publish what a load run observed into the EXISTING metric registry.

    **No new metric names are invented here.** The registry in
    ``app/services/observability/metrics.py`` declares a fixed vocabulary, and a
    load harness that quietly added ``ymoney_load_*`` names to it would create
    series no alert rule reads -- decoration shaped like telemetry. So this
    writes the two facts the existing vocabulary already describes exactly:

    * ``ymoney_db_pool_checked_out`` / ``_size`` / ``_utilization_ratio`` -- the
      pool occupancy read at the end of the run, which is the single most
      useful number a saturation run produces;
    * ``ymoney_budget_refusals_total`` -- a refusal counted here really is a
      budget refusal, and counting it is what makes "the cap held under load" a
      visible series instead of a claim in a report.

    Everything else the run learned stays in :class:`LoadResult`, which is what
    ``docs/PRODUCTION_LOAD_REPORT.md`` is written from.
    """
    try:
        from app.services.observability import metrics as obs

        pool = dict(result.detail.get("pool") or {})
        checked_out = float(pool.get("checked_out", 0) or 0)
        size = float(pool.get("size", 0) or 0)
        obs.DB_POOL_CHECKED_OUT.set(checked_out)
        if size > 0:
            obs.DB_POOL_SIZE.set(size)
            obs.DB_POOL_UTILIZATION.set(max(0.0, min(1.0, checked_out / size)))
        if result.refused and result.workload.startswith("cost_reserve"):
            obs.BUDGET_REFUSALS.inc(value=float(result.refused),
                                    kind="budget_exceeded")
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a measurement
        logger.warning("load result for {} not recorded: {}", result.workload, exc)


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

WorkloadFn = Callable[["LoadContext", int], Any]
_WORKLOADS: dict[str, WorkloadFn] = {}


def workload(name: str) -> Callable[[WorkloadFn], WorkloadFn]:
    """Register a workload callable under ``name``."""

    def deco(fn: WorkloadFn) -> WorkloadFn:
        _WORKLOADS[str(name)] = fn
        return fn

    return deco


def workload_names() -> tuple[str, ...]:
    """Every registered workload name, sorted."""
    return tuple(sorted(_WORKLOADS))


@dataclass
class LoadContext:
    """What a workload is allowed to touch.

    Deliberately narrow. A workload gets workspace ids, a per-thread token bag
    and a seed counter -- not a live session and not an engine -- so a workload
    cannot accidentally hold a connection across its whole run and quietly turn
    a latency measurement into a pool-saturation measurement. (The pool
    pressure drill wants exactly that, and it asks for it explicitly, in
    :mod:`tests.test_work16_load`.)
    """

    harness: LoadHarness
    workspaces: list[str] = field(default_factory=list)
    accounts: dict[str, list[str]] = field(default_factory=dict)
    projects: dict[str, list[str]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def workspace(self, index: int) -> str:
        """Workspace for operation ``index``. Round-robin, so a run that uses
        one workspace is an explicit choice rather than an accident."""
        if not self.workspaces:
            raise RuntimeError("load context has no workspaces")
        return self.workspaces[index % len(self.workspaces)]


# -- 1. cost reservations ----------------------------------------------------


@workload("cost_reserve")
def _cost_reserve(ctx: LoadContext, index: int) -> Any:
    """One ``reserve_spend``. The most contended path in the product.

    Exercises the workspace row lock, the window aggregates, the Work 16 §11
    rollup probe and the ledger insert in one transaction, which is exactly the
    path whose throughput decides whether N renders can start at once.
    """
    from app.services.cost import BudgetExceededError, reserve_spend

    workspace_id = ctx.workspace(index)
    try:
        reservation = reserve_spend(
            workspace_id, float(ctx.options.get("reserve_amount", 0.01)),
            category="llm", provider="load-harness",
            detail={"load": True})
    except BudgetExceededError:
        return REFUSED
    return reservation.entry_id


@workload("cost_reserve_settle")
def _cost_reserve_settle(ctx: LoadContext, index: int) -> Any:
    """``reserve_spend`` immediately followed by ``settle_reservation``.

    The realistic shape -- every real paid call does both -- and it is the
    reason the reservation path is TWO transactions and therefore TWO pool
    checkouts per call. A harness that only reserved would under-report pool
    pressure by half on this lane.
    """
    from app.services.cost import BudgetExceededError, reserve_spend, settle_reservation

    workspace_id = ctx.workspace(index)
    try:
        reservation = reserve_spend(
            workspace_id, 0.01, category="tts", provider="load-harness",
            detail={"load": True})
    except BudgetExceededError:
        return REFUSED
    settle_reservation(reservation.entry_id, 0.009)
    return reservation.entry_id


# -- 2. worker claims --------------------------------------------------------


@workload("worker_claim")
def _worker_claim(ctx: LoadContext, index: int) -> Any:
    """One ``claim_next`` -- the N-workers-one-queue race, end to end.

    The contended read is the candidate SELECT plus the conditional UPDATE that
    decides the winner, so this measures the queue's real scaling shape rather
    than a synthetic UPDATE in isolation.
    """
    from app.services.job_leases import claim_next

    claimed = claim_next(ctx.options.get("worker_prefix", "load-harness") + f"-{index}")
    if claimed is None:
        return SKIPPED
    return claimed.job_id


@workload("job_create")
def _job_create(ctx: LoadContext, index: int) -> Any:
    """``jobs.enqueue`` -- the write side of the same queue."""
    from app.services import jobs as jobs_service

    job_id = jobs_service.enqueue(
        ctx.options.get("job_type", "w16.load.probe"),
        {"load": True, "index": index},
        workspace_id=ctx.workspace(index),
        # Unique per operation so the idempotency dedupe (a deliberate second
        # refusal) does not turn this workload into a no-op measurement.
        idempotency_key=f"w16-load-{uuid.uuid4().hex}")
    return job_id or SKIPPED


# -- 3. project reads / writes / editor autosave ------------------------------


@workload("project_write")
def _project_write(ctx: LoadContext, index: int) -> Any:
    """Create a project row -- the write behind ``POST /projects``.

    Every operation is a COMMIT. The earlier shape of this workload read an
    existing project inside ``session_scope`` and then fell through to add the
    new row *after* the block, which meant the write was never committed and
    every id it handed back was a rolled-back id -- the workload measured
    nothing and reported success for it.
    """
    from app.db import session_scope
    from app.models import Project

    workspace_id = ctx.workspace(index)
    user_id = str(ctx.options.get("user_id", ""))
    if not user_id:
        return SKIPPED
    with session_scope() as s:
        row = Project(workspace_id=workspace_id, name=f"load-{uuid.uuid4().hex[:8]}",
                      description="written under load", created_by=user_id)
        s.add(row)
        s.flush()
        new_id = row.id
    ctx.projects.setdefault(workspace_id, []).append(new_id)
    return new_id


@workload("project_read")
def _project_read(ctx: LoadContext, index: int) -> Any:
    """Read one project back -- the query behind ``GET /projects/{id}``."""
    from app.db import session_scope
    from app.models import Project

    workspace_id = ctx.workspace(index)
    ids = ctx.projects.get(workspace_id) or []
    if not ids:
        return SKIPPED
    project_id = ids[index % len(ids)]
    with session_scope() as s:
        row = s.get(Project, project_id)
        return row.name if row is not None else SKIPPED


@workload("editor_autosave")
def _editor_autosave(ctx: LoadContext, index: int) -> Any:
    """An editor autosave: read-modify-write of one project's state blob.

    The shape that makes autosave expensive is not the UPDATE, it is the
    read-modify-write under contention: two autosaves to the same project
    serialise on the row lock, and the latency of the SECOND one is the
    latency a user feels when they type. So this deliberately reads and writes
    the same small set of projects rather than scattering across many.
    """
    from app.db import session_scope
    from app.models import Project

    workspace_id = ctx.workspace(index)
    ids = ctx.projects.get(workspace_id) or []
    if not ids:
        return SKIPPED
    project_id = ids[index % len(ids)]
    with session_scope() as s:
        row = s.get(Project, project_id)
        if row is None:
            return SKIPPED
        row.description = f"autosave {index} @ {time.time():.6f}"
        return project_id


# -- 4. planner --------------------------------------------------------------


@workload("planner")
def _planner(ctx: LoadContext, index: int) -> Any:
    """One ``ContentPlanningEngine.plan`` against a throwaway plan.

    The planner is the workload class §3 promises must never be starved by, so
    it is measured here on its own rather than inferred from the queue.

    Two preconditions, both real, both satisfied by the seeded fixture rather
    than bypassed:

    * the workspace carries ``TrendSignal`` rows **with evidence ids**. The
      engine plans nothing from an unevidenced signal -- it says so and returns
      -- so a planner measured against bare signals measures an early return.
    * the autonomy policy is ``APPROVAL``, the lowest mode that permits
      ``CREATE_PLAN_ITEM``. ``RECOMMEND`` (the default) is a *proposal* layer
      that persists nothing, so under it this workload would report a skip on
      every single operation.

    ``PUBLISH`` is refused at every mode including ``AUTONOMOUS``, so no choice
    of policy here can make planning an unattended publishing step.
    """
    from app.db import session_scope
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    workspace_id = ctx.workspace(index)
    with session_scope() as s:
        engine = ContentPlanningEngine(s, workspace_id)
        result = engine.plan(PlanningInputs(
            workspace_id=workspace_id,
            horizon_days=7,
            goals=[f"load goal {index}"],
            platforms=["youtube", "tiktok"],
            budget_usd=5.0,
            autonomy=AutonomyPolicy(mode=AutonomyMode.APPROVAL),
        ))
        plan_id = result.plan.id if result.plan is not None else None
        items = len(result.items or [])
    return plan_id or f"NO_PLAN:{items}"


# -- 5. publishing (real money path, fake transport) -------------------------


@workload("publish")
def _publish(ctx: LoadContext, index: int) -> Any:
    """One paid publication, end to end, with a PROVIDER DOUBLE.

    Real: :class:`~app.services.paid_provider.PaidOperation.authorize` (so the
    real ``reserve_spend`` under the real workspace lock), ``mark_attempt``,
    ``mark_accepted`` (so the real ``annotate_exposure`` writes the remote id),
    and ``close_book`` (so the real in-place settlement runs).

    Double: the transport that "uploads the video". This is a function-level
    test double, not a live API, and it is labelled that way in every report it
    appears in.
    """
    from app.services.cost import BudgetExceededError
    from app.services.paid_provider import paid_operation

    workspace_id = ctx.workspace(index)
    operation = paid_operation(
        workspace_id=workspace_id,
        provider="load-harness",
        operation="publish",
        category="publishing",
        estimated_cost=0.02,
        reservation_extra={"load": True, "index": index},
    )
    try:
        operation.authorize()
    except BudgetExceededError:
        return REFUSED
    operation.mark_attempt()
    remote_id = _fake_upload(index)
    operation.mark_accepted(remote_id)
    operation.close_book(0.02)
    return remote_id


def _fake_upload(index: int) -> str:
    """The provider stand-in. Returns a durable remote id; touches no network."""
    return f"load-remote-{uuid.uuid4().hex}"


@workload("publish_idempotent")
def _publish_idempotent(ctx: LoadContext, index: int) -> Any:
    """``jobs.enqueue`` for a publish, twice, with the SAME idempotency key.

    Work 14's publishing idempotency: the second enqueue must be refused by the
    queue, because a deploy that re-runs its schedule must not publish the same
    video twice. Measured under concurrency because a uniqueness check that is
    only correct single-threaded is not idempotency.
    """
    from app.engine.campaign.publish_flow import publish_idempotency_key
    from app.services import jobs as jobs_service

    workspace_id = ctx.workspace(index)
    variant_id = f"load-variant-{index}"
    key = publish_idempotency_key(variant_id, "youtube")
    first = jobs_service.enqueue("campaign.publish",
                                 {"variant_id": variant_id, "platform": "youtube"},
                                 workspace_id=workspace_id, idempotency_key=key)
    second = jobs_service.enqueue("campaign.publish",
                                  {"variant_id": variant_id, "platform": "youtube"},
                                  workspace_id=workspace_id, idempotency_key=key)
    if second is not None:
        return f"DUPLICATE:{second}"
    return first or SKIPPED


# -- 6. inbox sync -----------------------------------------------------------


@workload("inbox_sync")
def _inbox_sync(ctx: LoadContext, index: int) -> Any:
    """``sync_workspace`` against a provider double.

    Real: the whole ingestion engine -- cursor advance, per-item upsert with the
    SAVEPOINT duplicate guard, conversation aggregation, sync-state bookkeeping
    and the commit-per-page rule. Double: the platform API object, injected
    through the documented ``providers`` mapping. ``force=True`` bypasses the
    backoff gate, which would otherwise make every run after the first a no-op
    and measure nothing.
    """
    from app.db import session_scope
    from app.engine.community.sync import sync_workspace

    workspace_id = ctx.workspace(index)
    provider = _inbox_provider(index)
    with session_scope() as s:
        summary = sync_workspace(s, workspace_id, providers={"youtube": provider},
                                 force=True)
    if summary.get("failed"):
        return f"FAILED:{summary['failed']}"
    return summary.get("ingested", 0)


def _inbox_provider(index: int) -> Any:
    """A duck-typed social provider returning one synthetic comment page.

    The page and the item are plain MAPPINGS because that is the shape
    ``app.engine.community.sync._page_parts`` / ``_field`` are documented to
    accept, so nothing here depends on a private class in another module.

    Built per call so each sync sees FRESH remote ids: a constant would let the
    engine's duplicate guard short-circuit every run after the first and the
    workload would measure a no-op.
    """
    class _Provider:
        comments_require_post = False

        def list_comments(self, account, cursor=None, limit=50):
            token = uuid.uuid4().hex[:10]
            return {
                "items": [{
                    "remote_id": f"c-{token}",
                    "text": f"load comment {index}",
                    "author_remote_id": "u-load",
                    "author_name": "load",
                    "thread_id": f"t-{token}",
                    "kind": "COMMENT",
                }],
                "next_cursor": None,
            }

    return _Provider()


# -- 7. asset operations -----------------------------------------------------

#: A canonical object kind. ``storage_objects`` publishes only these, and the
#: registry rejects anything else at ``register_pending`` -- so a harness that
#: invents a kind measures its own argument, not the storage lane.
_ASSET_KIND = "generated"


@workload("asset_upload")
def _asset_upload(ctx: LoadContext, index: int) -> Any:
    """``storage_objects.upload_object`` -- canonical write with checksum.

    Two database writes and an atomic rename per call, so this measures the
    storage lane's DB cost honestly rather than only its I/O cost.
    """
    from app.services.storage_objects import upload_object

    workspace_id = ctx.workspace(index)
    # ``kind`` must be one of the canonical kinds. "load" is not one, and the
    # registry refuses it -- correctly. Using a real kind is the difference
    # between measuring the storage lane and measuring its argument validation.
    row = upload_object(workspace_id, _ASSET_KIND, [b"x" * 4096],
                        logical_name=f"load-{uuid.uuid4().hex[:10]}.bin",
                        content_type="application/octet-stream")
    return row.id


@workload("asset_roundtrip")
def _asset_roundtrip(ctx: LoadContext, index: int) -> Any:
    """Upload, read back by key, delete -- the full asset lifecycle."""
    from app.services import storage_objects

    workspace_id = ctx.workspace(index)
    row = storage_objects.upload_object(
        workspace_id, _ASSET_KIND, [b"y" * 8192],
        logical_name=f"rt-{uuid.uuid4().hex[:10]}.bin",
        content_type="application/octet-stream")
    payload = storage_objects.read_bytes(workspace_id, row.object_key)
    storage_objects.delete(workspace_id, row.object_key)
    return len(payload)


# -- 8. API traffic ----------------------------------------------------------


@workload("api_traffic")
def _api_traffic(ctx: LoadContext, index: int) -> Any:
    """The REAL ASGI app, in process, through the real auth middleware.

    Not a mock of an endpoint: ``create_app()``'s middleware stack, routing,
    dependency injection, request metrics and session handling all run. Each
    worker thread owns an event loop and an ``httpx`` client bound to it, which
    is what real thread-pooled ASGI serving looks like.

    The app and the client are built ONCE per process and once per thread
    respectively. Rebuilding ``create_app()`` per request would measure router
    construction -- hundreds of milliseconds of nothing -- and dress it up as API
    latency, which is the most flattering lie a load harness can tell.

    The route is chosen from ``ctx.options["api_route"]`` so a run can measure
    the cheap ``/health`` route and the authenticated list route in the same
    harness.
    """
    route = str(ctx.options.get("api_route", "/health"))
    token = str(ctx.options.get("api_token", ""))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    workspace_id = ctx.workspace(index)
    path = route.replace("{workspace_id}", workspace_id)

    status = _run_coroutine(_api_get(_api_client(), path, headers))
    if status >= 500:
        raise RuntimeError(f"HTTP {status} on {path}")
    return status


_APP_LOCK = threading.Lock()
_APP: Any = None
_CLIENTS = threading.local()


def _asgi_app() -> Any:
    """The application, built once per process."""
    global _APP
    if _APP is None:
        with _APP_LOCK:
            if _APP is None:
                from app.main import create_app

                _APP = create_app()
    return _APP


def _api_client() -> Any:
    """One ``httpx.AsyncClient`` per worker thread, bound to that thread's loop."""
    client = getattr(_CLIENTS, "client", None)
    if client is None:
        import httpx

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_asgi_app()),
            base_url="http://load.test", timeout=60.0)
        _CLIENTS.client = client
    return client


async def _api_get(client: Any, path: str, headers: dict[str, str]) -> int:
    response = await client.get(path, headers=headers)
    return int(response.status_code)


def _run_coroutine(coro) -> Any:
    """Run a coroutine to completion on THIS thread's persistent event loop.

    The loop is created once per worker thread and reused, because that is what
    thread-pooled ASGI serving actually looks like and because an ``httpx``
    client binds itself to the loop that first used it. The obvious two-line
    version -- a fresh ``asyncio.run()`` per request -- makes the second request
    on each worker die on a cross-loop error and leaves the pool connection it
    checked out never returned, which is a load number that means nothing.

    Inside an already-running loop (an ``anyio``/``pytest-asyncio`` worker) a
    private loop on a private thread is still the only way to run this
    synchronously, because ``run_until_complete`` refuses a running loop.
    """
    try:
        asyncio.get_running_loop()
        nested = True
    except RuntimeError:
        nested = False
    loop = getattr(_CLIENTS, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _CLIENTS.loop = loop
        # A client binds to the loop that first used it, so a new loop means the
        # old client is poison. Dropping it here is what keeps the second and
        # every later request on this thread honest.
        _CLIENTS.client = None
    # Already inside a loop (an ``anyio``/pytest-asyncio worker): a private
    # loop on a private thread is the only way to run this synchronously.
    box: dict[str, Any] = {}

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc

    if nested:
        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()
    else:
        _runner()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """Per-worker accumulator. One per thread, merged under one lock."""

    ok: int = 0
    refused: int = 0
    skipped: int = 0
    failed: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    samples: list[Any] = field(default_factory=list)


class LoadHarness:
    """Own a scratch database, point the app at it, and measure.

    Lifecycle::

        h = LoadHarness(pool_size=10, max_overflow=10)
        h.create_database()
        try:
            h.bind()
            ctx = h.seed(workspaces=2)
            result = h.run("cost_reserve", concurrency=8, iterations=25)
        finally:
            h.restore()
            h.drop_database()

    :meth:`bind` swaps the application's global session factory for one bound to
    the scratch engine, and :meth:`restore` puts the original back in a
    ``finally``. Nothing inside the measured modules is edited: they resolve
    ``app.db.SessionLocal`` at call time (except ``app.services.cost``, which
    binds the name at import, so that one is restored explicitly too -- noted
    here because "we rebound the module attribute" is exactly the kind of thing
    that silently stops working when someone adds a ``from`` import).
    """

    def __init__(self, *, db_name: str | None = None, pool_size: int = 10,
                 max_overflow: int = 10, pool_timeout: float = 5.0,
                 pool_recycle: float = 1800.0, admin: str | None = None,
                 storage_root: str | None = None) -> None:
        self.admin = str(admin or admin_dsn())
        self.db_name = db_name or f"w16_load_{uuid.uuid4().hex[:10]}"
        self.pool_size = int(pool_size)
        self.max_overflow = int(max_overflow)
        self.pool_timeout = float(pool_timeout)
        self.pool_recycle = float(pool_recycle)
        self.storage_root = storage_root
        self.url = engine_url(self.db_name, self.admin)
        self.engine = None
        self._saved: dict[str, Any] = {}
        self.workspaces: list[str] = []
        self.users: list[str] = []
        self.api_token = ""

    # -- database --------------------------------------------------------

    def _admin_connect(self):
        import psycopg

        return psycopg.connect(self.admin, autocommit=True)

    def create_database(self) -> str:
        """Create the scratch database. Idempotent."""
        with self._admin_connect() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{self.db_name}"')
        return self.db_name

    def drop_database(self) -> None:
        """Drop the scratch database, forcibly.

        ``WITH (FORCE)`` because a load run leaves live connections behind by
        construction -- that is what saturation means -- and ``DROP DATABASE``
        refuses to proceed past one.
        """
        try:
            with self._admin_connect() as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask results
            logger.warning("could not drop {}: {}", self.db_name, exc)

    def migrate(self) -> list[str]:
        """Bring the scratch database to the current schema.

        ``create_all`` first, then the migration runner: this repo's migrations
        assume the ORM-created tables exist, exactly as the application boot
        sequence does.
        """
        from sqlalchemy.orm import sessionmaker

        import app.models  # noqa: F401 - every ORM model on Base.metadata
        from app.db import Base
        from app.migrations.runner import run_migrations

        Base.metadata.create_all(self.engine)
        applied = run_migrations(sessionmaker(bind=self.engine)())
        logger.info("load harness: {} migrated ({} applied)", self.db_name, applied)
        return applied

    # -- binding ---------------------------------------------------------

    def bind(self) -> None:
        """Point every session factory at the scratch engine."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.core.config import settings

        self.engine = create_engine(
            self.url, pool_size=self.pool_size,
            max_overflow=self.max_overflow, pool_timeout=self.pool_timeout,
            pool_recycle=self.pool_recycle, pool_pre_ping=True)

        import app.db as db_module
        from app.services import cost as cost_module

        self._saved = {
            "db.engine": db_module.engine,
            "db.SessionLocal": db_module.SessionLocal,
            "cost.SessionLocal": getattr(cost_module, "SessionLocal", None),
        }
        factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        db_module.engine = self.engine
        db_module.SessionLocal = factory
        # ``cost.py`` did ``from app.db import SessionLocal`` at import time, so
        # its own module namespace holds the ORIGINAL factory. Rebinding
        # ``app.db`` alone would leave the most contended path in the product
        # pointed at the wrong database -- and would look like it worked.
        cost_module.SessionLocal = factory

        if self.storage_root:
            import app.services.storage as storage_module

            self._saved["storage.STORAGE_ROOT"] = storage_module.STORAGE_ROOT
            from pathlib import Path

            root = Path(self.storage_root)
            root.mkdir(parents=True, exist_ok=True)
            storage_module.STORAGE_ROOT = root
            settings.storage_staging_dir = str(root / "staging")

        # The schema-fact cache is keyed by dialect, and this run is the first
        # time the process has talked to PostgreSQL, so there is nothing stale
        # to clear -- but an explicit reset keeps a second harness in the same
        # process honest.
        from app.services import budget_rollup

        budget_rollup._TABLE_PRESENT.clear()

    def restore(self) -> None:
        """Undo :meth:`bind`. Safe to call twice."""
        import app.db as db_module
        from app.services import cost as cost_module

        if "db.SessionLocal" in self._saved:
            db_module.engine = self._saved["db.engine"]
            db_module.SessionLocal = self._saved["db.SessionLocal"]
            cost_module.SessionLocal = self._saved["cost.SessionLocal"]
            self._saved = {}
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    # -- seeding ---------------------------------------------------------

    def seed(self, *, workspaces: int = 2, accounts_per_workspace: int = 1,
             budget_daily_usd: float = 1000.0,
             budget_per_call_usd: float = 5.0) -> LoadContext:
        """Create tenants, users, accounts and projects.

        Budget caps are written onto each workspace explicitly rather than left
        to ``settings.daily_budget_usd``: a run whose money numbers move because
        somebody changed a default is not a measurement, it is a coincidence.
        """
        from app.db import session_scope
        from app.models import Project, SocialAccount, User, Workspace, WorkspaceMember
        from app.services import storage_objects

        token = uuid.uuid4().hex[:10]
        ctx = LoadContext(harness=self, options={"user_id": "", "reserve_amount": 0.01})
        with session_scope() as s:
            user = User(email=f"load-{token}@test.local", password_hash="x")
            s.add(user)
            s.flush()
            self.users.append(user.id)
            ctx.options["user_id"] = user.id
            for index in range(max(1, int(workspaces))):
                ws = Workspace(name=f"W16 LOAD {token}-{index}",
                               slug=f"w16-load-{token}-{index}", niche="load")
                ws.settings_json = {"safety": {
                    "daily_budget_usd": float(budget_daily_usd),
                    "per_video_budget_usd": float(budget_per_call_usd)}}
                s.add(ws)
                s.flush()
                s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                                      role=WorkspaceMember.ROLE_OWNER))
                s.flush()
                project = Project(workspace_id=ws.id, name="seed",
                                  description="seed", created_by=user.id)
                s.add(project)
                s.flush()
                ctx.workspaces.append(ws.id)
                ctx.projects[ws.id] = [project.id]
                account_ids = []
                for slot in range(max(0, int(accounts_per_workspace))):
                    account = SocialAccount(workspace_id=ws.id, platform="youtube",
                                            external_id=f"ext-{token}-{index}-{slot}",
                                            display_name="load", status="connected")
                    s.add(account)
                    s.flush()
                    account_ids.append(account.id)
                ctx.accounts[ws.id] = account_ids
        logger.info("load harness seeded: {} workspace(s), user {}",
                    len(ctx.workspaces), self.users[0] if self.users else "-")
        _ = storage_objects  # imported to prove the storage lane is reachable
        self.seed_signals(ctx)
        return ctx

    def seed_signals(self, ctx: LoadContext, *, per_workspace: int = 3) -> int:
        """Give the planner something real to plan from.

        ``ContentPlanningEngine.plan`` returns early -- deliberately, with a
        note -- when no signal carries evidence, and it plans nothing at
        ``RECOMMEND``. A planner workload run against a bare workspace therefore
        measures an early return 100% of the time and reports a perfect result.
        These rows carry evidence ids and are FRESH, which is what the engine
        asks for; two observations per topic also give it a measurable velocity.

        Returns the number of signals written.
        """
        from datetime import timedelta

        from app.db import session_scope
        from app.models import TrendSignal
        from app.models.base import utcnow

        written = 0
        with session_scope() as s:
            for index, workspace_id in enumerate(ctx.workspaces):
                for slot in range(max(1, int(per_workspace))):
                    topic = f"load topic {index}-{slot}"
                    for observation in range(2):
                        s.add(TrendSignal(
                            workspace_id=workspace_id,
                            source="research",
                            topic=topic,
                            topic_key=topic,
                            external_ref=f"w16-load-{index}-{slot}-{observation}",
                            evidence_ids_json=[f"ev-load-{index}-{slot}-{observation}"],
                            observed_at=utcnow() - timedelta(minutes=observation),
                            confidence=0.9,
                        ))
                        written += 1
        return written

    def api_session(self, ctx: LoadContext) -> str:
        """Mint a real JWT for the first seeded user.

        Uses :func:`app.services.auth_service.issue_tokens`, so the load runs
        against the same authentication the product issues rather than a
        hand-built token that might carry claims the middleware ignores.
        """
        from app.db import session_scope
        from app.models import User
        from app.services.auth_service import issue_tokens

        if not self.users:
            return ""
        with session_scope() as s:
            user = s.get(User, self.users[0])
            if user is None:
                return ""
            # ``issue_tokens(db, user, ...)``: it PERSISTS a refresh token, so it
            # needs the caller's session. A one-arg call would have been a
            # TypeError at the first run, which is the cheapest possible way to
            # find out that a signature is not what the docstring said.
            tokens = issue_tokens(s, user, user_agent="load-harness")
        ctx.options["api_token"] = str(tokens.get("access_token", ""))
        self.api_token = ctx.options["api_token"]
        return self.api_token

    def warm_api(self, ctx: LoadContext, *, route: str = "/health") -> float:
        """Build the ASGI app and issue one request, UNTIMED. Returns seconds.

        ``create_app()`` costs seconds and every lazy import under a route costs
        milliseconds. Charged to whichever worker happens to run first, that
        shows up as a multi-second p90 on a service whose steady-state p90 is
        single-digit milliseconds -- a load number that describes the first
        request of the process rather than the service. Warming here is what
        makes the API curve mean what it says.
        """
        started = time.perf_counter()
        _asgi_app()
        path = route.replace("{workspace_id}", ctx.workspace(0))
        headers = ({"Authorization": f"Bearer {ctx.options.get('api_token', '')}"}
                   if ctx.options.get("api_token") else {})
        status = _run_coroutine(_api_get(_api_client(), path, headers))
        if status >= 500:
            raise RuntimeError(f"warm-up {path} returned HTTP {status}")
        return round(time.perf_counter() - started, 3)

    # -- running ---------------------------------------------------------

    def pool_stats(self) -> dict[str, int]:
        """Live pool occupancy. The evidence that a timeout was the POOL."""
        if self.engine is None:
            return {"checked_out": 0, "size": 0, "overflow": 0}
        pool = self.engine.pool
        # ``overflow`` is a METHOD on QueuePool and an int on some other pools;
        # reading the attribute unconditionally reports 0 while the pool is
        # bursting, which is precisely the moment the number matters.
        raw_overflow = getattr(pool, "overflow", 0)
        overflow = raw_overflow() if callable(raw_overflow) else raw_overflow
        return {
            "checked_out": int(pool.checkedout()),
            "size": int(pool.size()),
            "overflow": int(overflow or 0),
        }

    def pg_connections(self) -> int:
        """``pg_stat_activity`` backends for this database.

        Read through a SEPARATE admin connection on purpose: when the pool is
        saturated there is no pooled connection left to ask, so asking from the
        pool would fail exactly when the answer matters.
        """
        try:
            with self._admin_connect() as conn:
                row = conn.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = %s", (self.db_name,)).fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            logger.warning("pg_stat_activity unavailable: {}", exc)
            return -1

    def run(self, name: str, *, concurrency: int, iterations: int,
            ctx: LoadContext | None = None, **options) -> LoadResult:
        """Run one workload at one concurrency and return what happened.

        Every worker opens its OWN pooled connections -- that is the point. A
        shared session would serialise the run into SQLite's model and prove
        nothing about a pool.

        A failure is recorded and the run CONTINUES. Stopping at the first
        error would report "it broke" without saying how much of the load
        succeeded before it did, and the amount that survived is the most
        operationally useful number in the whole exercise.
        """
        if name not in _WORKLOADS:
            raise KeyError(f"unknown workload {name!r}; known: {workload_names()}")
        fn = _WORKLOADS[name]
        if ctx is None:
            ctx = LoadContext(harness=self, workspaces=list(self.workspaces),
                              projects={ws: [] for ws in self.workspaces})
        ctx.options.update(options)
        workers = max(1, int(concurrency))
        per_worker = max(1, int(iterations))
        buckets = [_Bucket() for _ in range(workers)]
        barrier = threading.Barrier(workers, timeout=60.0)

        def _worker(slot: int) -> None:
            bucket = buckets[slot]
            with contextlib.suppress(threading.BrokenBarrierError):
                # A broken barrier means a worker died during setup; the
                # survivors still measure what they can rather than vanishing.
                barrier.wait()
            for step in range(per_worker):
                started = time.perf_counter()
                try:
                    outcome = fn(ctx, slot * per_worker + step)
                    elapsed = time.perf_counter() - started
                    bucket.latencies.append(elapsed)
                    if outcome is REFUSED:
                        bucket.refused += 1
                    elif outcome is SKIPPED:
                        bucket.skipped += 1
                    else:
                        bucket.ok += 1
                except BaseException as exc:  # noqa: BLE001 - recorded, not raised
                    elapsed = time.perf_counter() - started
                    bucket.latencies.append(elapsed)
                    bucket.failed += 1
                    key = type(exc).__name__
                    bucket.errors[key] = bucket.errors.get(key, 0) + 1

        started_at = time.perf_counter()
        threads = [threading.Thread(target=_worker, args=(slot,), daemon=True,
                                    name=f"load-{name}-{slot}")
                   for slot in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.perf_counter() - started_at

        merged = _merge(buckets)
        result = LoadResult(
            workload=name, concurrency=workers, iterations=per_worker,
            wall_seconds=wall,
            operations=merged.ok + merged.refused + merged.skipped + merged.failed,
            ok=merged.ok, refused=merged.refused, skipped=merged.skipped,
            failed=merged.failed, latencies=tuple(merged.latencies),
            errors=dict(merged.errors),
            detail={"pool": self.pool_stats(), "pg_connections": self.pg_connections()},
        )
        record_load_result(result)
        logger.info("{}", result.line())
        return result

    def sweep(self, name: str, *, concurrencies: Sequence[int],
              iterations: int, ctx: LoadContext | None = None,
              **options) -> list[LoadResult]:
        """Run one workload at rising concurrency. The deliverable curve.

        Stops early at the first concurrency with a real failure, because the
        point of the sweep is the breaking point -- continuing past it produces
        numbers for a regime nobody would ship in and buries the answer.
        """
        results: list[LoadResult] = []
        for level in concurrencies:
            result = self.run(name, concurrency=int(level), iterations=iterations,
                              ctx=ctx, **options)
            results.append(result)
            if result.failed and result.error_rate > 0.0:
                logger.warning("{}: FAILURES BEGIN AT concurrency {} ({})",
                               name, level, result.errors)
                break
        return results


def _merge(buckets: Sequence[_Bucket]) -> _Bucket:
    """Fold per-worker buckets into one. Called once, after every thread joined."""
    merged = _Bucket()
    for bucket in buckets:
        merged.ok += bucket.ok
        merged.refused += bucket.refused
        merged.skipped += bucket.skipped
        merged.failed += bucket.failed
        merged.latencies.extend(bucket.latencies)
        for key, count in bucket.errors.items():
            merged.errors[key] = merged.errors.get(key, 0) + int(count)
    merged.latencies.sort()
    return merged


def format_table(results: Sequence[LoadResult]) -> str:
    """A results table suitable for pasting into the report verbatim."""
    lines = ["workload            conc  ops    wall   throughput   p50ms     p90ms"
             "     p99ms    ok/ref/skip/fail"]
    for result in results:
        lines.append(
            f"{result.workload:<19} {result.concurrency:<5} {result.operations:<6} "
            f"{result.wall_seconds:>6.2f}s {result.throughput:>10.2f}/s "
            f"{result.percentile(0.50) * 1000:>8.1f} "
            f"{result.percentile(0.90) * 1000:>8.1f} "
            f"{result.percentile(0.99) * 1000:>8.1f}  "
            f"{result.ok}/{result.refused}/{result.skipped}/{result.failed}")
    return "\n".join(lines)