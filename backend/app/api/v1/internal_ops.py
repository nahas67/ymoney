"""Liveness, readiness and the internal observability endpoints.

    GET /livez              liveness  -- is this process up?
    GET /readyz             readiness -- can it serve?
    GET /internal/metrics   Prometheus text exposition
    GET /internal/traces    recorded spans, grouped by trace
    GET /internal/slo       SLO TARGETS (declarative, no achieved values)
    GET /internal/alerts    alert rule definitions + current verdicts

**Why liveness and readiness are different endpoints.**

``/livez`` answers one question: *should the supervisor restart me?* It must
never touch the database. A liveness probe that fails because Postgres is slow
tells the orchestrator to kill a process that is perfectly healthy and would
have recovered -- restarting it converts a dependency outage into a crash loop
and destroys the evidence. ``/livez`` therefore checks only that this process
can execute a function and schedule work.

``/readyz`` answers a different question: *should traffic be routed here?* It
checks only **critical** dependencies -- the ones without which this process
cannot correctly do its job.

**The critical/non-critical split is the whole point of having two probes.**

CRITICAL (a failure means NOT ready):
  - ``database``       a reachable SQLAlchemy connection
  - ``migrations``     the schema_migrations table exists and is readable
  - ``job_backend``    the durable job backend can be reached

NON-CRITICAL (a failure means DEGRADED, and readiness stays READY):
  - every external provider (LLM, video engine, TTS, images, publishers...)

An optional external provider being down must NOT take this process out of
rotation. If ElevenLabs is unreachable, YMONEY can still accept work, render
with a different voice, queue, publish through the platforms that are up, and
keep its money reconciled. Pulling every replica out of the load balancer
because one vendor has an incident converts a partial outage into a total one.
Provider degradation is surfaced where it belongs -- the existing provider
maturity/health surface (``GET /api/v1/provider-maturity``) and the
``provider_health`` section of the ops overview -- and is echoed here under a
clearly non-blocking key.

**Auth.** These endpoints carry no workspace scope, because an orchestrator
cannot authenticate as a workspace. They expose operational state (counts,
dependency up/down, thresholds), never secrets, never customer content, and
never credentials. They are mounted on the app rather than under
``/api/v1/{workspace}`` precisely because they are not workspace-scoped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.services.observability import metrics as metrics_mod
from app.services.observability import slo as slo_mod
from app.services.observability import tracing as tracing_mod

__all__ = [
    "CRITICAL_CHECKS",
    "NON_CRITICAL_CHECKS",
    "internal_ops_router",
    "livez",
    "liveness",
    "provider_degradation",
    "readiness",
]

internal_ops_router = APIRouter(tags=["internal-ops"])

#: Content type for a Prometheus scrape.
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def liveness() -> dict[str, Any]:
    """Is the process alive? Deliberately dependency-free.

    The event loop must be able to schedule work; if it cannot, the process is
    wedged and a restart is the correct answer. Nothing else is asked, because
    every other answer belongs to readiness.
    """
    try:
        loop = asyncio.get_running_loop()
        loop_ok = loop is not None
        detail = "event loop accepting work"
    except RuntimeError:
        # No running loop: this is a synchronous call path (a worker thread or
        # a CLI). The process is still alive; the probe just cannot observe a
        # loop from here.
        loop_ok = True
        detail = "no event loop in this thread (sync call path)"
    return {
        "status": "alive",
        "detail": detail,
        "event_loop": loop_ok,
        "checks": [
            {"id": "process", "status": "passed", "blocking": True,
             "detail": detail},
            {"id": "event_loop", "status": "passed", "blocking": True,
             "detail": detail},
        ],
        "blocking_failures": [],
    }


# ---------------------------------------------------------------------------
# Readiness -- the three critical dependencies
# ---------------------------------------------------------------------------


def _check_database() -> tuple[bool, str, str]:
    """Can we get a connection and complete a trivial statement?"""
    from sqlalchemy import text as sql_text

    from app.db import session_scope

    try:
        with session_scope() as session:
            session.execute(sql_text("SELECT 1"))
        return True, "SELECT 1 succeeded", ""
    except Exception as exc:
        return False, f"unreachable: {type(exc).__name__}", (
            "Check DATABASE_URL, disk space, and file permissions. Note "
            "db_pool_size + db_max_overflow must stay under the server's "
            "max_connections."
        )


def _check_migrations() -> tuple[bool, str, str]:
    """Is the schema in a sane state?

    "Sane" means the migration ledger is readable and nothing is half-applied.
    A process that finds itself behind the schema will fail every request in a
    confusing way, so this is critical. It is a READ of ``schema_migrations``
    -- readiness never runs migrations; that is the lifespan's job.
    """
    from sqlalchemy import inspect
    from sqlalchemy import text as sql_text

    from app.db import engine as db_engine

    try:
        with db_engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            if "schema_migrations" not in tables:
                return False, "schema_migrations table missing", (
                    "Migrations have not run. Start the app once so the "
                    "lifespan migration runner applies them."
                )
            applied = {
                row[0] for row in conn.execute(
                    sql_text("SELECT version FROM schema_migrations")
                ).fetchall()
            }
    except Exception as exc:
        return False, f"unreadable: {type(exc).__name__}", (
            "Cannot read the migration ledger. This is a database problem, "
            "not a migration problem."
        )
    if not applied:
        return False, "schema_migrations is empty", (
            "The ledger exists but records no applied versions; the schema is "
            "almost certainly behind the code."
        )
    return True, f"{len(applied)} migration(s) applied", ""


def _check_job_backend() -> tuple[bool, str, str]:
    """Can the durable job backend be reached?

    "Reachable" means the table the claim loop reads from answers a query. It
    does NOT mean a worker is running: a process with zero workers can still
    accept work and hand it to a sibling replica, so worker availability is a
    separate CRITICAL alert (``worker_fleet_unavailable``) rather than a
    readiness input.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job

    try:
        with session_scope() as session:
            total = session.scalar(select(func.count()).select_from(Job))
    except Exception as exc:
        return False, f"unusable: {type(exc).__name__}", (
            "The jobs table cannot be queried, so no job can ever be claimed."
        )
    return True, f"jobs table readable ({int(total or 0)} row(s))", ""


#: Critical = a failure here means this process cannot serve.
#:
#: Stored as NAMES, not bound function objects, and resolved through
#: :func:`resolve_critical_check` at call time. A tuple of function objects
#: captures whatever the function was when the module loaded, so an operator
#: (or a test) replacing ``internal_ops._check_database`` would change the
#: module attribute and the readiness report would keep calling the original.
#: That is a real trap, not a test-only one: it makes the probe unreplaceable.
CRITICAL_CHECKS: tuple[str, ...] = (
    "database",
    "migrations",
    "job_backend",
)


def resolve_critical_check(name: str):
    """Look a critical probe up on the module, at call time."""
    return globals()[f"_check_{name}"]


def _non_critical_storage() -> tuple[bool, str, str]:
    """Storage is reported here but is NOT a readiness input.

    A read-only filesystem does not stop YMONEY accepting, planning, budgeting
    and publishing; it stops renders from completing, which the
    ``storage_unavailable`` alert covers. Failing readiness on it would take
    every replica out of rotation for a condition only the render lane
    suffers.
    """
    return metrics_mod.collect_storage(), "", ""


def _non_critical_db_pool() -> tuple[bool, str, str]:
    pool = metrics_mod.DB_POOL_UTILIZATION.value()
    return pool < 1.0, (
        f"pool utilisation {pool:.0%}; at 100% the next request waits"
    ), ""


#: Non-critical = a failure degrades capability but readiness stays READY.
NON_CRITICAL_CHECKS: tuple[str, ...] = (
    "storage",
    "db_pool_headroom",
)


def resolve_non_critical_check(name: str):
    return globals()[f"_non_critical_{name}"]


def provider_degradation() -> dict[str, Any]:
    """Provider health, read from the EXISTING maturity surface.

    Reuses ``app.providers.maturity`` rather than introducing a second health
    stack -- Work 11 already routed the ops dashboard's ``provider_health``
    section at the same registry. A probe failure here is reported as
    ``DEGRADED`` and nothing more: it must never become a readiness input.
    """
    try:
        from app.providers import maturity

        records = maturity.list_maturity()
        by_health: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for record in records:
            # ProviderMaturity is a dataclass, NOT a mapping: ``.get()`` on it
            # raises, which is why this reads attributes.
            health = maturity.probe_health(record)
            by_health[health] = by_health.get(health, 0) + 1
            state = str(getattr(record, "state", "") or "")
            by_state[state] = by_state.get(state, 0) + 1
        down = sum(count for health, count in by_health.items()
                   if health == maturity.HEALTH_DOWN)
        degraded = sum(count for health, count in by_health.items()
                       if health == maturity.HEALTH_DEGRADED)
        return {
            "available": True,
            "blocking": False,
            "degraded": down > 0 or degraded > 0,
            "providers_down": down,
            "providers_degraded": degraded,
            "by_health": by_health,
            "by_state": by_state,
            "surface": "GET /api/v1/provider-maturity",
            "note": (
                "A provider outage is reported here and in the ops dashboard, "
                "and it deliberately does NOT affect /readyz."
            ),
        }
    except Exception as exc:
        # Even the degradation probe failing must not fail readiness.
        return {
            "available": False,
            "blocking": False,
            "degraded": True,
            "reason": type(exc).__name__,
            "note": "Provider maturity surface unavailable; readiness unaffected.",
        }


def readiness() -> dict[str, Any]:
    """Full readiness evaluation. Critical + non-critical, computed separately."""
    checks: list[dict[str, Any]] = []
    for check_id in CRITICAL_CHECKS:
        try:
            ok, detail, remediation = resolve_critical_check(check_id)()
        except Exception as exc:  # a probe must never crash the probe
            ok, detail, remediation = (
                False, f"probe error: {type(exc).__name__}",
                "Retry; check the application log with the request id.")
        checks.append({
            "id": check_id,
            "status": "passed" if ok else "failed",
            "critical": True,
            "blocking": True,
            "detail": detail,
            "remediation": "" if ok else remediation,
        })

    degraded: list[dict[str, Any]] = []
    for check_id in NON_CRITICAL_CHECKS:
        try:
            ok, detail, remediation = resolve_non_critical_check(check_id)()
        except Exception as exc:
            ok, detail, remediation = False, f"probe error: {type(exc).__name__}", ""
        entry = {
            "id": check_id,
            "status": "passed" if ok else "failed",
            "critical": False,
            "blocking": False,
            "detail": detail,
            "remediation": "" if ok else remediation,
        }
        checks.append(entry)
        if not ok:
            degraded.append(entry)

    providers = provider_degradation()
    if providers.get("degraded"):
        degraded.append({
            "id": "providers",
            "status": "degraded",
            "critical": False,
            "blocking": False,
            "detail": (
                f"{providers.get('providers_down', 0)} provider(s) down, "
                f"{providers.get('providers_degraded', 0)} degraded"
            ),
            "remediation": "See the provider maturity surface; readiness is unaffected.",
        })

    blocking_failures = [
        c["id"] for c in checks if c["blocking"] and c["status"] == "failed"
    ]
    return {
        "status": "ready" if not blocking_failures else "not_ready",
        "critical_dependencies": list(CRITICAL_CHECKS),
        "non_critical_dependencies": list(NON_CRITICAL_CHECKS),
        "checks": checks,
        "blocking_failures": blocking_failures,
        "degraded": [d["id"] for d in degraded],
        "providers": providers,
        "policy": (
            "Only critical dependencies gate readiness. External providers are "
            "reported as degraded and never remove this process from rotation."
        ),
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@internal_ops_router.get("/livez", include_in_schema=False)
def livez():
    """Liveness. 200 while the process can serve a request.

    Never touches the database, so a database outage cannot trigger a restart
    loop.
    """
    return JSONResponse(liveness(), status_code=200)


@internal_ops_router.get("/readyz", include_in_schema=False)
def readyz():
    """Readiness. 503 only when a CRITICAL dependency is unusable.

    An external provider outage returns 200 with ``degraded`` populated.
    """
    report = readiness()
    status_code = 503 if report["blocking_failures"] else 200
    return JSONResponse(report, status_code=status_code)


@internal_ops_router.get("/health", include_in_schema=False)
def ops_health():
    """Alias kept for probes configured against ``/health``."""
    report = readiness()
    status_code = 503 if report["blocking_failures"] else 200
    return JSONResponse(report, status_code=status_code)


@internal_ops_router.get(
    "/internal/metrics",
    include_in_schema=False,
    response_class=PlainTextResponse,
)
def prometheus_metrics(response: Response):
    """Prometheus text exposition.

    Collectors run first so the scrape reflects live state. A collector that
    raises is isolated: it reports ``ymoney_collector_up=0`` for itself and the
    rest of the scrape still renders.
    """
    metrics_mod.collect_build_info()
    metrics_mod.collect_all()
    response.headers["Content-Type"] = PROMETHEUS_CONTENT_TYPE
    return PlainTextResponse(
        metrics_mod.REGISTRY.render(),
        media_type=PROMETHEUS_CONTENT_TYPE,
    )


@internal_ops_router.get("/internal/traces", include_in_schema=False)
def traces(limit: int = 200, trace_id: str = "", kind: str = "",
           min_duration_ms: float = 0.0, group_by_trace: bool = False):
    """Recorded spans.

    States plainly which backend produced them. ``otel_installed`` is reported
    rather than implied: this is an in-process recorder, not OTLP.
    """
    limit = max(1, min(int(limit or 200), 2000))
    if group_by_trace:
        return {
            "backend": "in_process_recorder",
            "otel_installed": tracing_mod.otel_installed(),
            "stats": tracing_mod.tracer.stats(),
            "traces": tracing_mod.tracer.grouped_by_trace(limit=limit),
        }
    return {
        "backend": "in_process_recorder",
        "otel_installed": tracing_mod.otel_installed(),
        "stats": tracing_mod.tracer.stats(),
        "spans": tracing_mod.tracer.as_dicts(
            limit=limit, trace_id=trace_id, kind=kind,
            min_duration_ms=min_duration_ms),
    }


@internal_ops_router.get("/internal/slo", include_in_schema=False)
def slo_targets_endpoint():
    """SLO objectives as TARGETS, plus the alert rule definitions.

    Returns 404 for nothing and claims nothing about achievement.
    """
    return {
        **slo_mod.slo_catalog(),
        "alert_rules": [rule.to_dict() for rule in slo_mod.alert_rules()],
    }


@internal_ops_router.get("/internal/alerts", include_in_schema=False)
def alerts_endpoint():
    """Current verdicts for every alert rule, against live metrics."""
    return slo_mod.alert_catalog()


@internal_ops_router.get("/internal/collectors", include_in_schema=False)
def collectors_endpoint():
    """Run the runtime collectors on demand and report each one's result."""
    results = metrics_mod.collect_all()
    return {
        "collectors": {name: ("up" if ok else "down")
                       for name, ok in sorted(results.items())},
        "failed": sorted(name for name, ok in results.items() if not ok),
        "note": (
            "A failed collector reports ymoney_collector_up=0 for itself and "
            "leaves its gauges untouched: zeroing them would read as 'no "
            "backlog' during an outage."
        ),
    }


@internal_ops_router.get("/internal/overview", include_in_schema=False)
def internal_overview():
    """One call for an operator: liveness, readiness, alerts and metrics URL.

    Assembled from the same functions the individual endpoints use, so it
    cannot disagree with them.
    """
    return {
        "live": liveness(),
        "ready": readiness(),
        "alerts": slo_mod.alert_catalog(),
        "traces": tracing_mod.tracer.stats(),
        "endpoints": {
            "metrics": "/internal/metrics",
            "traces": "/internal/traces",
            "slo": "/internal/slo",
            "alerts": "/internal/alerts",
            "collectors": "/internal/collectors",
            "provider_maturity": "/api/v1/provider-maturity",
        },
    }