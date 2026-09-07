"""Persistent memory service (spec #25).

Targeted retrieval only: callers must filter by type/scope/query — the API
never returns the whole store. Expired records are excluded automatically and
purged lazily on write.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.db import session_scope
from app.models import MemoryRecord
from app.models.base import utcnow

MAX_RETRIEVE = 50


def store(
    workspace_id: str,
    *,
    content: str,
    type: str = MemoryRecord.TYPE_SEMANTIC,
    source: str = "",
    confidence: float = 0.5,
    importance: float = 0.5,
    scope: str = "",
    related: dict | None = None,
    ttl_hours: float | None = None,
) -> str:
    """Persist one memory record; returns its id. Validates type."""
    if type not in MemoryRecord.VALID_TYPES:
        raise ValueError(f"invalid memory type: {type}")
    with session_scope() as s:
        rec = MemoryRecord(
            workspace_id=workspace_id,
            type=type,
            content=content,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
            importance=max(0.0, min(1.0, importance)),
            scope=scope[:120],
            related_json=related or {},
            expires_at=(utcnow() + timedelta(hours=ttl_hours)) if ttl_hours else None,
        )
        s.add(rec)
        s.flush()
        _purge_expired(s, workspace_id)
        return rec.id


def retrieve(
    workspace_id: str,
    *,
    type: str | None = None,
    scope: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Targeted retrieval: filter by type and/or scope, optional keyword match
    over content. Ordered by importance then recency. Expired records are
    never returned. Hard-capped so a caller cannot dump the store."""
    limit = max(1, min(int(limit), MAX_RETRIEVE))
    now = utcnow()
    with session_scope() as s:
        q = select(MemoryRecord).where(
            MemoryRecord.workspace_id == workspace_id,
            (MemoryRecord.expires_at.is_(None)) | (MemoryRecord.expires_at > now),
        )
        if type:
            q = q.where(MemoryRecord.type == type)
        if scope:
            q = q.where(MemoryRecord.scope == scope)
        if query:
            needle = f"%{query.lower()}%"
            q = q.where(MemoryRecord.content.ilike(needle))
        rows = s.scalars(
            q.order_by(MemoryRecord.importance.desc(), MemoryRecord.created_at.desc()).limit(limit)
        ).all()
        return [_to_dict(r) for r in rows]


def style_context(workspace_id: str, *, limit: int = 4) -> list[dict]:
    """Style-shaping memories (preference + strategic) that should influence
    content generation — format constraints, tone, brand voice."""
    try:
        prefs = retrieve(workspace_id, type="preference", limit=limit)
        strat = retrieve(workspace_id, type="strategic", limit=limit)
        seen: set[str] = set()
        out: list[dict] = []
        for m in prefs + strat:
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
        return out[:limit]
    except Exception:
        return []


def retrieve_for_topic(workspace_id: str, topic: str, *, limit: int = 5) -> list[dict]:
    """Topic-aware retrieval: try the full topic string first, then fall back to
    individual keywords. Shared by the decision engine and research agent so
    both use identical, explainable matching."""
    topic = (topic or "").strip()
    if not topic:
        return []
    hits = retrieve(workspace_id, type="semantic", query=topic[:120], limit=limit)
    if hits:
        return hits
    from app.engine.decision import _tokens

    for tok in _tokens(topic.lower()):
        hits = retrieve(workspace_id, type="semantic", query=tok, limit=limit)
        if hits:
            break
    return hits


def _purge_expired(s, workspace_id: str) -> None:
    now = utcnow()
    expired = s.scalars(
        select(MemoryRecord).where(
            MemoryRecord.workspace_id == workspace_id,
            MemoryRecord.expires_at.is_not(None),
            MemoryRecord.expires_at <= now,
        )
    ).all()
    for r in expired:
        s.delete(r)


def _to_dict(r: MemoryRecord) -> dict:
    return {
        "id": r.id,
        "type": r.type,
        "content": r.content,
        "source": r.source,
        "confidence": round(r.confidence, 2),
        "importance": round(r.importance, 2),
        "scope": r.scope,
        "related": r.related_json or {},
        "expires_at": r.expires_at.isoformat() + "Z" if r.expires_at else None,
        "created_at": r.created_at.isoformat() + "Z",
    }
