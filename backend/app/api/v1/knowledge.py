"""Knowledge API (Work 10 Lane D).

Workspace-scoped surface over the knowledge engine: typed memories, the
knowledge graph, source connectors, community signals and ranked retrieval.

Mounting — one router, two prefixes (see ``api/v1/__init__.py``):

    /workspaces/{workspace_id}/knowledge/...   canonical (``wsApi`` + path RBAC)
    /knowledge/... ?workspace_id=...           literal contract path

Both resolve ``workspace_id`` through the same ``require_workspace_role``
dependency (path param when present, query param otherwise), so RBAC,
isolation and 404-on-foreign-id behaviour are identical on both.

Error policy (contract): short ``HTTPException`` details, 422 for
enum-ish validation (type/status/node_type/kind), 404 for foreign or
missing ids, and a generic ``internal error`` 500 that never echoes an
exception — the traceback is logged on ``ymoney.knowledge`` instead.

Redaction boundary: connector ``config_json`` is NEVER serialized here.
Every connector response goes through ``app.engine.sources.describe_connector``
— the single helper that strips token|key|secret|password|credential keys
and reports ``has_credentials`` instead.
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine.community.insight import community_content_feedback
from app.engine.knowledge.community_bridge import CONFIDENCE_MAP, promote_insights_to_memory
from app.engine.knowledge.freshness import ALL_STATUSES, freshness_band
from app.engine.knowledge.graph import NODE_TYPES, KnowledgeGraphProvider
from app.engine.knowledge.memory import TYPES, GlobalMemory
from app.engine.knowledge.retrieval import MemoryRetriever
from app.engine.sources import (
    CONNECTOR_CATALOG,
    SourceError,
    create_connector,
    describe_connector,
    enqueue_source_sync,
)
from app.models import (
    CommunityInsight,
    Job,
    SourceConnector,
    SourceDocument,
    User,
    Workspace,
)
from app.services.auth_service import get_current_user, require_workspace_role

knowledge_router = APIRouter(prefix="/knowledge", tags=["knowledge"])
logger = logging.getLogger("ymoney.knowledge")

# same dedupe set Lane C uses (engine/sources/sync._ACTIVE_JOB_STATUSES):
# only jobs that will still run block a re-sync, completed history never does
_ACTIVE_SYNC_STATUSES = ("QUEUED", "RUNNING")

# documents endpoint: hard page cap (the contract's 1..200 window)
_MAX_DOC_LIMIT = 200
# content is served preview-sized only — never the multi-MiB stored body
_DOC_CONTENT_CHARS = 2000


# ---------------------------------------------------------------------------
# error policy (contract: short details, no exception echo)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate service errors (never a stack)."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 404):
    """Uniform error policy for one route body.

    * ``HTTPException`` passes through untouched (401/403/404/409/422).
    * ``ValueError`` — the memory/graph service family — becomes
      ``value_error`` with a short, single-line detail: a foreign id reads
      as 404 on the action routes, a bad body value as 422 on the store route.
    * ``SourceError`` — the connector family (safe messages by design) —
      becomes 422.
    * anything else is logged on ``ymoney.knowledge`` and surfaced as a
      generic 500 that echoes nothing back to the client.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error, detail=_short(exc)) from None
            except SourceError as exc:
                raise HTTPException(status_code=422, detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 — deliberate catch-all at the API edge
                logger.exception("knowledge route failed: %s", getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class MemoryCreateBody(BaseModel):
    type: str = Field(min_length=1, max_length=40)
    content: str = Field(min_length=1, max_length=20000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    scope: str = Field(default="", max_length=120)
    brand_id: str | None = Field(default=None, max_length=36)
    topic: str = Field(default="", max_length=200)
    platform: str = Field(default="", max_length=40)
    origin: str = Field(default="", max_length=80)
    evidence_ids: list[Any] | None = Field(default=None, max_length=200)


class SupersedeBody(BaseModel):
    replacement_id: str = Field(min_length=1, max_length=36)


class SourceCreateBody(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_connector(db: Session, workspace_id: str, connector_id: str) -> SourceConnector:
    """Workspace-scoped fetch; foreign/missing ids read as 404."""
    row = db.scalars(
        select(SourceConnector).where(
            SourceConnector.workspace_id == workspace_id,
            SourceConnector.id == str(connector_id or ""),
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="source connector not found")
    return row


def _source_dto(db: Session, workspace_id: str, row: SourceConnector) -> dict:
    """Redacted connector row (+ cursor for the UI) — config_json never leaves."""
    dto = describe_connector(db, workspace_id, row)
    dto["last_cursor"] = str(row.last_cursor or "")
    return dto


def _document_dto(row: SourceDocument) -> dict:
    """Fields the documents table reads + identity/state/provenance columns."""
    return {
        "id": row.id,
        "connector_id": row.connector_id,
        "remote_id": row.remote_id,
        "title": str(row.title or ""),
        "mime_type": str(row.mime_type or ""),
        "author": str(row.author or ""),
        "state": str(row.state or "active"),
        "checksum": str(row.checksum or ""),
        "remote_created_at": _iso(row.remote_created_at),
        "remote_updated_at": _iso(row.remote_updated_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "content": str(row.content or "")[:_DOC_CONTENT_CHARS],
    }


def _inflight_source_sync(db: Session, workspace_id: str, connector_id: str) -> str | None:
    """Active SOURCE_SYNC job for this connector (mirrors Lane C dedupe)."""
    jobs = db.scalars(
        select(Job).where(
            Job.type == "SOURCE_SYNC",
            Job.workspace_id == workspace_id,
            Job.status.in_(_ACTIVE_SYNC_STATUSES),
        )
        .order_by(Job.created_at.desc())
    ).all()
    for job in jobs:
        if str((job.payload or {}).get("connector_id") or "") == connector_id:
            return str(job.id)
    return None


# ---------------------------------------------------------------------------
# endpoints — memories
# ---------------------------------------------------------------------------


@knowledge_router.get("/memories", summary="Filtered workspace memories")
@_guard()
def list_memories(
    type: str | None = Query(default=None, max_length=40),  # noqa: A002 - contract name
    status: str | None = Query(default=None, max_length=20),
    scope: str | None = Query(default=None, max_length=120),
    topic: str | None = Query(default=None, max_length=200),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    if type and type not in TYPES:
        raise HTTPException(status_code=422, detail=f"unknown memory type {type!r}")
    if status and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"unknown status {status!r}")
    items = GlobalMemory.list(
        db,
        ws.id,
        type=type,
        status=status,
        scope=scope,
        topic=topic,
        q=q,
        limit=max(1, min(int(limit), 200)),
    )
    return {"items": items}


@knowledge_router.post("/memories", summary="Store one typed memory")
@_guard(value_error=422)
def create_memory(
    body: MemoryCreateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    if body.type not in TYPES:
        raise HTTPException(status_code=422, detail=f"unknown memory type {body.type!r}")
    confidence = 0.5 if body.confidence is None else float(body.confidence)
    return GlobalMemory.store(
        db,
        ws.id,
        type=body.type,
        content=body.content,
        confidence=confidence,
        scope=body.scope,
        brand_id=body.brand_id,
        topic=body.topic,
        platform=body.platform,
        evidence_ids=body.evidence_ids,
        origin=body.origin,
    )


@knowledge_router.post("/memories/{memory_id}/verify", summary="Record a human verification")
@_guard()
def verify_memory(
    memory_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return GlobalMemory.verify(db, ws.id, memory_id, user_id=user.id)


@knowledge_router.post("/memories/{memory_id}/disable", summary="Disable a memory (idempotent)")
@_guard()
def disable_memory(
    memory_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    return GlobalMemory.disable(db, ws.id, memory_id)


@knowledge_router.post("/memories/{memory_id}/supersede", summary="Supersede a memory")
@_guard()
def supersede_memory(
    memory_id: str,
    body: SupersedeBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    return GlobalMemory.supersede(db, ws.id, memory_id, replacement_id=body.replacement_id)


# ---------------------------------------------------------------------------
# endpoints — graph
# ---------------------------------------------------------------------------


@knowledge_router.get("/graph", summary="Knowledge-graph nodes + internal edges")
@_guard()
def get_graph(
    node_type: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=200, ge=1, le=500),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    if node_type and node_type not in NODE_TYPES:
        raise HTTPException(status_code=422, detail=f"unknown node_type {node_type!r}")
    return KnowledgeGraphProvider().list_nodes(
        db, ws.id, node_type=node_type or "", limit=limit
    )


# ---------------------------------------------------------------------------
# endpoints — source connectors
# ---------------------------------------------------------------------------


@knowledge_router.get("/sources", summary="Registered source connectors (redacted)")
@_guard()
def list_sources(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(SourceConnector)
        .where(SourceConnector.workspace_id == ws.id)
        .order_by(SourceConnector.created_at.asc(), SourceConnector.id.asc())
    ).all()
    return {"items": [_source_dto(db, ws.id, row) for row in rows]}


@knowledge_router.post("/sources", status_code=201, summary="Register a source connector")
@_guard(value_error=422)
def create_source(
    body: SourceCreateBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    kind = str(body.kind or "").strip().lower()
    if kind not in CONNECTOR_CATALOG:
        raise HTTPException(status_code=422, detail=f"unknown connector kind {body.kind!r}")
    name = str(body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be empty")

    # factory validates the kind (catalog-only kinds refuse with SourceError);
    # the instance reports its honest health — UNAVAILABLE without config is
    # a valid registration state, never a fabricated AVAILABLE.
    connector = create_connector(kind, dict(body.config or {}))
    health = connector.health()

    existing = db.scalars(
        select(SourceConnector).where(
            SourceConnector.workspace_id == ws.id,
            SourceConnector.kind == kind,
            SourceConnector.name == name,
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="connector name already used for this kind")

    row = SourceConnector(
        workspace_id=ws.id,
        kind=kind,
        name=name,
        status=str(health.get("status") or "UNAVAILABLE"),
        unavailable_reason=str(health.get("reason") or "")[:200],
        config_json=dict(body.config or {}),
    )
    db.add(row)
    db.flush()
    return _source_dto(db, ws.id, row)


@knowledge_router.post("/sources/{connector_id}/sync", summary="Queue a SOURCE_SYNC job")
@_guard()
def sync_source(
    connector_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    row = _load_connector(db, ws.id, connector_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="connector is disabled")
    inflight = _inflight_source_sync(db, ws.id, row.id)
    if inflight:  # honest join: the job already exists, we created nothing
        return {"job_id": inflight, "queued": False}
    job_id = enqueue_source_sync(db, ws.id, row.id)
    if not job_id:
        # enqueue only returns None when jobs idempotency dedupes (Lane C
        # does not use it) — re-read the queue rather than invent an id
        fallback = _inflight_source_sync(db, ws.id, row.id)
        if not fallback:
            raise HTTPException(status_code=500, detail="internal error")
        return {"job_id": fallback, "queued": False}
    return {"job_id": job_id, "queued": True}


@knowledge_router.post("/sources/{connector_id}/disconnect", summary="Disconnect a connector")
@_guard()
def disconnect_source(
    connector_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    row = _load_connector(db, ws.id, connector_id)
    if row.enabled or row.status != "DISABLED":
        row.enabled = False
        row.status = "DISABLED"
        db.flush()
    return _source_dto(db, ws.id, row)


@knowledge_router.get(
    "/sources/{connector_id}/documents", summary="Documents ingested by one connector"
)
@_guard()
def list_documents(
    connector_id: str,
    limit: int = Query(default=50),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_connector(db, ws.id, connector_id)
    # state is deliberately NOT filtered: deleted rows stay visible so the
    # UI can show them honestly instead of pretending they never existed
    docs = db.scalars(
        select(SourceDocument)
        .where(
            SourceDocument.workspace_id == ws.id,
            SourceDocument.connector_id == row.id,
        )
        .order_by(SourceDocument.updated_at.desc(), SourceDocument.id.asc())
        .limit(max(1, min(int(limit), _MAX_DOC_LIMIT)))
    ).all()
    return {"items": [_document_dto(doc) for doc in docs]}


# ---------------------------------------------------------------------------
# endpoints — community signals + retrieval
# ---------------------------------------------------------------------------


@knowledge_router.get("/community-signals", summary="Repeated community themes")
@_guard()
def community_signals(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    insights = community_content_feedback(db, ws.id)

    # freshness band per insight age (freshness helpers read created_at)
    bands: dict[str, str] = {}
    ids = [str(item.get("insight_id") or "") for item in insights]
    if ids:
        rows = db.scalars(
            select(CommunityInsight).where(
                CommunityInsight.workspace_id == ws.id,
                CommunityInsight.id.in_(ids),
            )
        ).all()
        bands = {str(row.id): freshness_band(row) for row in rows}

    items: list[dict] = []
    for item in insights:
        insight_id = str(item.get("insight_id") or "")
        label = str(item.get("confidence") or "low")
        platforms = [str(p) for p in (item.get("platforms") or [])]
        interaction_ids = [str(i) for i in (item.get("interaction_ids") or [])]
        items.append(
            {
                "id": insight_id,
                "insight_id": insight_id,
                "topic": item.get("topic") or "",
                "topic_key": item.get("topic_key") or "",
                "evidence_count": int(item.get("evidence_count") or 0),
                "interaction_count": len(interaction_ids),
                "interaction_ids": interaction_ids,
                "sample_text": item.get("sample_text") or "",
                "suggested_followup": item.get("suggested_followup") or "",
                "suggested_action": item.get("suggested_action") or "",
                "platform": platforms[0] if platforms else "",
                "platforms": platforms,
                "scope": ",".join(platforms),
                # numeric for the UI badge; the original label survives as a
                # sibling key (low→0.3 · medium→0.6 · high→0.9, as promotion)
                "confidence": CONFIDENCE_MAP.get(label.strip().lower(), 0.3),
                "confidence_label": label,
                "freshness": bands.get(insight_id, ""),
                "meets_threshold": bool(item.get("meets_threshold")),
                "low_confidence": bool(item.get("low_confidence")),
                "threshold": int(item.get("threshold") or 0),
            }
        )
    return {"items": items}


@knowledge_router.post("/promote-insights", summary="Promote threshold insights to memory")
@_guard()
def promote_insights(
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    entries = promote_insights_to_memory(db, ws.id)
    promoted = sum(1 for entry in entries if entry.get("promoted"))
    return {"promoted": promoted, "items": entries}


@knowledge_router.get("/retrieve", summary="Ranked memory recall for one task")
@_guard()
def retrieve_memories(
    task: str = Query(default="", max_length=500),
    topic: str = Query(default="", max_length=200),
    platform: str = Query(default="", max_length=40),
    max_results: int = Query(default=10),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    return MemoryRetriever().retrieve(
        db,
        ws.id,
        task=task,
        topic=topic,
        platform=platform,
        max_results=max(1, min(int(max_results), 50)),
    )


# ---------------------------------------------------------------------------
# job handler registration (Lane C exports it; guarded so the API never
# fails to import because a sibling module is mid-landing)
# ---------------------------------------------------------------------------


def _bootstrap_source_jobs() -> None:
    """Register the SOURCE_SYNC handler idempotently (Work 10 Lane D).

    Each target is imported and invoked inside its own guard: one missing
    or half-landed sibling module can never break the API import. The
    register function skips duplicates, so re-running this (module reload,
    direct test call) is safe.
    """
    for _mod, _fn in (
        ("app.engine.sources", "register_source_jobs"),
    ):
        with suppress(Exception):  # noqa: S110 - sibling lanes land in parallel
            _module = importlib.import_module(_mod)
            getattr(_module, _fn)()


_bootstrap_source_jobs()

__all__ = ["knowledge_router"]
