"""YMONEY FastAPI application entrypoint.

Lifespan responsibilities:
- run database migrations
- start durable job workers
- recover orphaned cycles from a previous process
- graceful shutdown of workers
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

import app.engine.agents.registry
from app.api.v1 import api_router
from app.core.config import settings
from app.db import engine, session_scope
from app.engine import autopilot as autopilot_engine
from app.migrations.runner import run_migrations
from app.services import jobs as jobs_service
from app.services import telegram_service

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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        allow_headers=["Authorization", "Content-Type"],
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

    # request logging for security audit trail (lightweight)
    @app.middleware("http")
    async def audit_middleware(request: Request, call_next):
        from app.core.request_context import set_request_id as _set_rid

        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = _set_rid(rid)
        try:
            response = await call_next(request)
        finally:
            from app.core.request_context import _request_id
            _request_id.reset(token)
        response.headers["X-Request-ID"] = rid
        if request.method != "GET" and request.url.path.startswith("/api/"):
            logger.bind(audit=True, request_id=rid).info(
                f"{request.method} {request.url.path} -> {response.status_code}"
            )
        return response

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
