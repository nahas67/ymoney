"""Redis dispatch accelerator for the durable job queue (E7 scale).

Design: the database stays the source of truth (jobs table, retries, dead
letters). When JOB_QUEUE=redis and Redis is reachable, enqueue also LPUSHes
the job id and workers BRPOP instead of polling — lower latency, less DB
pressure. Any Redis failure degrades to plain DB polling (logged, 60s
cooldown) — Redis is acceleration, never a dependency.

redis-py is an optional import: without it (or without a server) every
function safely no-ops and the queue runs local-only.
"""

from __future__ import annotations

import time

from loguru import logger

from app.core.config import settings

_KEY = "ymoney:jobs:ready"
_unavailable_until: float = 0.0


def configured() -> bool:
    return (settings.job_queue or "local").lower() == "redis"


def _get_client():
    import redis  # type: ignore

    return redis.Redis.from_url(settings.redis_url, socket_timeout=5)


def _cooling_down() -> bool:
    return time.monotonic() < _unavailable_until


def _cooldown() -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + 60.0


def push(job_id: str) -> bool:
    """Best-effort dispatch signal. Never raises into enqueue()."""
    if not configured() or _cooling_down():
        return False
    try:
        _get_client().lpush(_KEY, job_id)
        return True
    except Exception as exc:
        logger.warning(f"[queue] redis push failed, DB polling continues: {exc}")
        _cooldown()
        return False


def pop(timeout: float) -> str | None:
    """Blocking pop of one job id; None on timeout/unavailable."""
    if not configured() or _cooling_down():
        return None
    try:
        item = _get_client().brpop(_KEY, timeout=max(1, int(timeout)))
    except Exception as exc:
        logger.warning(f"[queue] redis pop failed, DB polling continues: {exc}")
        _cooldown()
        return None
    if not item:
        return None
    _raw = item[1]
    return _raw.decode() if isinstance(_raw, bytes) else str(_raw)


def ping() -> bool | None:
    """None when redis dispatch isn't configured; else reachability."""
    if not configured():
        return None
    try:
        return bool(_get_client().ping())
    except Exception:
        return False


def cuda_present() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except (ImportError, ValueError):
        return False


__all__ = ["configured", "cuda_present", "ping", "pop", "push"]
