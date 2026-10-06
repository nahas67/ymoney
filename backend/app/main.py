"""YMONEY FastAPI application entrypoint.

Lifespan responsibilities:
- run database migrations
- start durable job workers
- recover orphaned cycles from a previous process
- graceful shutdown of workers
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

import app.engine.agents.registry
from app.api.v1 import api_router
from app.api.v1.internal_ops import internal_ops_router
from app.core.config import settings
from app.db import engine, session_scope
from app.engine import autopilot as autopilot_engine
from app.migrations.runner import run_migrations
from app.services import jobs as jobs_service
from app.services import telegram_service
from app.services import (
    webhooks as webhook_service,  # noqa: F401 (registers webhook.dispatch handler)
)
from app.services.observability import logging_setup as obs_logging
from app.services.observability import metrics as obs_metrics

# Work 12 media intelligence: registration is idempotent and currently a no-op
# (every media-intel engine runs in-process inside its route), but wiring the
# seam now means a future GPU-backed lane gets its handler for free. Never let
# it block app boot.
try:  # pragma: no cover - import-time side effect
    from app.engine.intel import jobs as intel_jobs  # noqa: F401

    intel_jobs.register_intel_jobs()
except Exception as _intel_jobs_exc:
    import logging

    logging.getLogger("ymoney.intel").warning(
        "media-intel job registration skipped: %s", _intel_jobs_exc
    )

_FILE_LOGGING_CONFIGURED = False


def _configure_file_logging() -> None:
    """Add one rotated file sink under backend/data/logs (gitignored).

    Keeps long-running server output diagnosable without unbounded root-level
    log files. Guarded so re-imports (tests, uvicorn workers) never double-add.
    """
    global _FILE_LOGGING_CONFIGURED
    if _FILE_LOGGING_CONFIGURED:
        return
    log_dir = Path(__file__).resolve().parents[1] / "data" / "logs"
    logger.add(
        log_dir / "ymoney.log",
        level=settings.log_level.upper(),
        rotation="10 MB",
        retention="14 days",
        enqueue=True,
        backtrace=True,
        diagnose=False,  # never serialize locals into the file
    )
    _FILE_LOGGING_CONFIGURED = True


_configure_file_logging()

# Work 16 §8: attach the structured, redacted JSON sink and declare the static
# metrics. ``install()`` is additive and idempotent -- it never removes
# loguru's own stderr handler, so a developer who has not opted into JSON still
# gets readable output, and a uvicorn worker importing this module twice cannot
# double-sink.
if settings.observability_enabled:
    obs_logging.install(serialize=settings.observability_json_logs)
obs_metrics.collect_build_info()

if settings.sentry_dsn:
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)
        logger.info("sentry error tracking enabled")
    except Exception as exc:
        logger.warning(f"sentry init failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_production and settings.secret_key.startswith("change-me"):
        raise RuntimeError(
            "YMONEY_SECRET_KEY must be set to a strong random value in production"
        )
    Path("data").mkdir(exist_ok=True)
    with session_scope() as session:
        applied = run_migrations(session)
    if applied:
        logger.info(f"migrations applied: {applied}")

    await jobs_service.start_workers()
    autopilot_engine.start_schedule_sweep()
    recovered = await asyncio.to_thread(_recover_cycles)
    if recovered:
        logger.info(f"recovered {recovered} in-flight cycle(s)")
    if settings.telegram_enabled:
        telegram_service.start_poller()
    try:
        yield
    finally:
        await telegram_service.stop_poller()
        await jobs_service.stop_workers()
        engine.dispose()
        logger.info("shutdown complete")


def _recover_cycles() -> int:
    return autopilot_engine.recover_stale_cycles()


def create_app() -> FastAPI:
    app = FastAPI(
        title="YMONEY",
        description="Autonomous AI short-form content operating system",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        from app.core.request_context import request_id as _rid

        rid = _rid()
        logger.opt(exception=exc).error(f"unhandled error on {request.method} {request.url.path} rid={rid}")
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "request_id": rid},
            headers={"X-Request-ID": rid} if rid else {},
        )

    @app.get("/health", include_in_schema=False)
    def root_health():
        return {"status": "ok"}

    app.include_router(api_router)
    # Work 16 §9: liveness/readiness + §8/§10 telemetry. Mounted on the app,
    # not under /api/v1, because these are process-level and carry no
    # workspace scope -- an orchestrator cannot authenticate as a workspace.
    app.include_router(internal_ops_router)

    # Work 16.5.4 §1/§2: attach the declared response contracts.
    #
    # These are REAL `response_model` values -- FastAPI validates, serialises and
    # publishes them exactly as if they were written on the decorator. They are
    # applied here, from one table, rather than as 100+ decorator edits spread
    # across the router files: a mechanical change across dozens of modules is a
    # poor trade against the syntax-damage risk, and it makes the contract set
    # impossible to review as a whole.
    #
    # It must run AFTER the routers are registered, or there are no routes to
    # attach to -- an ordering mistake that reported success while applying zero.
    #
    # Every generated model sets `extra="allow"`, so validating cannot DROP a
    # field the generator did not observe; `tests/test_work16_5_4_response_
    # filtering.py` measures that rather than assuming it.
    try:
        from app.schemas.contract_registry import apply_response_contracts

        applied = apply_response_contracts(app)
        logger.info(f"response contracts applied to {applied} routes")
    except Exception:  # pragma: no cover - startup must never fail on contracts
        logger.exception("response contracts could not be applied")

    # request logging for security audit trail (lightweight)
    @app.middleware("http")
    async def audit_middleware(request: Request, call_next):
        """Correlate, time and measure every inbound request (Work 16 §8).

        Three jobs in order, because they depend on each other:

        1. bind a request id (accepting a caller-supplied ``X-Request-ID``,
           clamped) so every log line, span and error below shares it;
        2. open a server span and close it in a ``finally``, so an exception
           is recorded on the span without being swallowed;
        3. record latency and a 5xx counter into the metric registry.

        The route label is the TEMPLATED path, never the raw URL. Emitting the
        raw path would let any client mint an unbounded series set.
        """
        from app.services.observability.tracing import SpanKind, SpanStatus, tracer

        inbound = request.headers.get("X-Request-ID")
        with obs_logging.request_scope(inbound) as rid:
            started = time.perf_counter()
            status = 500
            with tracer.start_span(
                f"{request.method} {request.url.path}",
                kind=SpanKind.SERVER,
                attributes={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                },
            ) as span:
                try:
                    response = await call_next(request)
                    status = response.status_code
                    span.attributes["status_code"] = status
                    response.headers["X-Request-ID"] = rid
                    return response
                except Exception as exc:
                    span.status = SpanStatus.ERROR
                    span.error_type = type(exc).__name__
                    raise
                finally:
                    route = _route_template(request)
                    obs_metrics.record_http_request(
                        request.method, route, status,
                        time.perf_counter() - started)
                    # A structured access line for EVERY request, not just
                    # writes. An observability stack that logs only mutations
                    # cannot answer "what did this request do", which is the
                    # question a request_id exists to answer. DEBUG so a
                    # default INFO deployment stays quiet.
                    logger.bind(
                        request_id=rid, route=route, method=request.method,
                        status=status, status_class=obs_metrics.status_class_of(status),
                        latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    ).opt(colors=False).log("DEBUG", "http request")
                    # The pre-existing security audit trail is unchanged: only
                    # non-GET writes under /api/.
                    if request.method != "GET" and request.url.path.startswith("/api/"):
                        logger.bind(audit=True, request_id=rid).info(
                            f"{request.method} {request.url.path} -> {status}"
                        )

    return app


def _route_template(request: Request) -> str:
    """The matched route's path template, e.g. ``/jobs/{job_id}``.

    Falls back to a bounded, ID-free shape when no route matched (a 404 or a
    middleware-level rejection). A raw 404 path can contain a token in a query
    string or an unbounded path segment, so it is truncated rather than used.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return f"__unmatched__:{request.url.path[:64]}"


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
