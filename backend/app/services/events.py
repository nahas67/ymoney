"""Event service: durable activity feed + in-process pub/sub for SSE."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app.db import session_scope
from app.models import EventLog

_subs: dict[str | None, set[asyncio.Queue]] = defaultdict(set)
_lock = asyncio.Lock()


async def subscribe(workspace_id: str | None) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    async with _lock:
        _subs[workspace_id].add(q)
    return q


async def unsubscribe(workspace_id: str | None, q: asyncio.Queue) -> None:
    async with _lock:
        _subs[workspace_id].discard(q)


def record_event(
    workspace_id: str | None,
    kind: str,
    message: str,
    level: str = "info",
    source: str = "system",
    data: dict | None = None,
) -> dict:
    """Persist an event and fan it out to live subscribers.

    Safe to call from sync code; the fan-out is scheduled when a loop exists.
    """
    from app.core.request_context import request_id as _rid

    rid = _rid()
    payload = {
        "kind": kind,
        "message": message,
        "level": level,
        "source": source,
        "workspace_id": workspace_id,
        "data": data or {},
        "request_id": rid,
    }
    with session_scope() as session:
        row = EventLog(
            workspace_id=workspace_id,
            kind=kind,
            message=message,
            level=level,
            source=source,
            data_json=data or {},
            request_id=rid,
        )
        session.add(row)
        session.flush()
        payload["id"] = row.id
        payload["created_at"] = row.created_at.isoformat() + "Z"

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and not loop.is_closed():
        loop.create_task(_broadcast(payload))

    # Telegram remote-control fan-out (best-effort; never breaks the caller)
    try:
        from app.services import telegram_service

        telegram_service.on_event(workspace_id, kind, message, level)
    except Exception:  # pragma: no cover — telemetry must never break the pipeline
        pass

    # Outbound webhooks (best-effort; delivery retries on the job queue)
    try:
        from app.services import webhooks as _webhooks

        _webhooks.enqueue_for_event(
            workspace_id, kind, payload.get("id", ""),
            message=message, level=level, source=source,
            data=data or {}, created_at=payload.get("created_at", ""),
        )
    except Exception:  # pragma: no cover
        pass
    return payload


async def _broadcast(payload: dict) -> None:
    ws = payload.get("workspace_id")
    async with _lock:
        targets = list(_subs.get(ws, set())) + list(_subs.get(None, set()))
    for q in targets:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
