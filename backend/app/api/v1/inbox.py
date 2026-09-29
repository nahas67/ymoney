"""Unified social inbox API (Work 09 — Lane D).

Workspace-scoped surface over ``app.models.community``: platform registry,
interaction browsing (filters + keyset pagination), conversation threads,
AI classify / draft / approve / reject / send, bulk triage, opportunities,
insights, sync jobs and the community autonomy configuration.

Mounting — one router, two prefixes (see ``api/v1/__init__.py``):

    /workspaces/{workspace_id}/inbox/...   canonical (``wsApi`` + path RBAC)
    /inbox/... ?workspace_id=...           literal contract path from the brief

Both resolve ``workspace_id`` through the same ``require_workspace_role``
dependency (path param when present, query param otherwise), so RBAC,
isolation and 404-on-foreign-id behaviour are identical on both.

Cross-lane imports (Lane A ``app.engine.platform_registry``, Lane C
``app.engine.community.*``) are resolved LAZILY inside each endpoint. A module
that is not there yet (the lanes land in parallel) yields an honest 503 /
empty shape — never an import error at module load, never a fabricated value.
"""

from __future__ import annotations

import base64
import importlib
import inspect
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db import escape_like, get_db
from app.models import (
    CommunityAction,
    CommunityInsight,
    CommunityOpportunity,
    Conversation,
    PublishedPost,
    SocialInteraction,
    User,
    Workspace,
)
from app.models.base import utcnow
from app.models.community import (
    AUTONOMY_MODES,
    CLASSIFICATION_LABELS,
    INTERACTION_STATUSES,
    OPPORTUNITY_STATES,
    PRIORITIES,
)
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.jobs import enqueue

inbox_router = APIRouter(prefix="/inbox", tags=["inbox"])

# bulk triage is capped and never reaches the send path
MAX_BULK_IDS = 100
_PAGE_CHUNKS = 40  # bounded keyset walk when a post-filter is active

# conservative default: drafts only, nothing auto-sends, small caps
DEFAULT_AUTONOMY: dict[str, Any] = {
    "mode": "DRAFT_ONLY",
    "classes": [],
    "caps": {"daily_replies": 20, "hourly_replies": 5},
}

# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class ReadBody(BaseModel):
    read: bool = True


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    send: bool = False
    # optional: anchor the draft to a specific interaction of this conversation
    interaction_id: str | None = Field(default=None, max_length=36)


class DraftBody(BaseModel):
    regenerate: bool = False


class SendBody(BaseModel):
    text: str | None = Field(default=None, max_length=4000)


class BulkBody(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: Literal["read", "unread", "spam", "ignore"]


class AutonomyUpdate(BaseModel):
    mode: str = "DRAFT_ONLY"
    classes: list[str] = Field(default_factory=list)
    caps: dict[str, int] = Field(default_factory=dict)


class SyncBody(BaseModel):
    platform: str | None = Field(default=None, max_length=30)
    account_id: str | None = Field(default=None, max_length=36)


# ---------------------------------------------------------------------------
# cross-lane engine resolution (lazy, honest)
# ---------------------------------------------------------------------------

_CLASSIFY_FN = (
    ("app.engine.community.classify", "classify_interaction"),
    ("app.engine.community.classification", "classify_interaction"),
    ("app.engine.community.classify", "classify"),
    ("app.engine.community", "classify_interaction"),
)
_DRAFT_FN = (
    # the landed names first: plan_reply runs the full policy gate (brand +
    # escalation + autonomy), generate_reply is the text-level fallback
    ("app.engine.community.policy", "plan_reply"),
    ("app.engine.community.draft", "generate_reply"),
    ("app.engine.community.draft", "draft_reply"),
    ("app.engine.community.draft", "draft_action"),
    ("app.engine.community.policy", "draft_reply"),
    ("app.engine.community", "draft_reply"),
    ("app.engine.community", "draft_action"),
)
_APPROVE_FN = (
    ("app.engine.community.policy", "approve_action"),
    ("app.engine.community.actions", "approve_action"),
    ("app.engine.community", "approve_action"),
)
_SEND_FN = (
    ("app.engine.community.policy", "send_action"),
    ("app.engine.community.actions", "send_action"),
    ("app.engine.community", "send_action"),
)
_METRICS_FN = (
    ("app.engine.community.analytics", "compute_community_metrics"),
    ("app.engine.community.metrics", "compute_community_metrics"),
    ("app.engine.community", "compute_community_metrics"),
)
_CONVERT_FN = (
    ("app.engine.community.opportunity", "convert_idea"),
    ("app.engine.community.opportunities", "convert_opportunity"),
    ("app.engine.community.opportunities", "convert"),
    ("app.engine.community", "convert_opportunity"),
    ("app.engine.community", "convert_opportunity_to_idea"),
)
_FEEDBACK_FN = (
    ("app.engine.community.insight", "community_content_feedback"),
    ("app.engine.community.insights", "community_content_feedback"),
    ("app.engine.community.feedback", "community_content_feedback"),
    ("app.engine.community", "community_content_feedback"),
)


def _engine_fn(*candidates: tuple[str, str]) -> Callable[..., Any] | None:
    """First importable candidate wins; None means "lane not landed yet"."""
    for mod_name, attr in candidates:
        try:
            mod: Any = importlib.import_module(mod_name)
        except Exception:  # noqa: S112 - sibling lanes land in parallel
            continue
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="community engine not available")


def _call_engine(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Call a cross-lane function passing only the parameters it declares."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(**kwargs)
    return fn(**{k: v for k, v in kwargs.items() if k in params})


def _run_engine(fn: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return _call_engine(fn, **kwargs)
    except HTTPException:
        raise
    except Exception:  # engine present but failing — surface it honestly
        # never echo internals to the client (they may carry query strings or
        # provider context); the full traceback goes to the server log instead
        logging.getLogger("ymoney.inbox").exception(
            "community engine call failed: %s", getattr(fn, "__name__", fn)
        )
        raise HTTPException(
            status_code=500, detail="community engine error"
        ) from None


def _denied_reason(result: Any) -> str | None:
    """Policy gate answer that refused the action, rendered as a reason string."""
    if not isinstance(result, dict):
        return None
    denied = result.get("allowed") is False or result.get("blocked") is True or (
        result.get("denied") is True
        # send_action refuses with {"sent": False, "reason": ...}
        or result.get("sent") is False
    )
    if not denied:
        return None
    reasons = result.get("reasons")
    if isinstance(reasons, (list, tuple)) and reasons:
        return "; ".join(str(r) for r in reasons)
    for key in ("reason", "error", "detail", "message"):
        if result.get(key):
            return str(result[key])
    return "blocked by community policy"


# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


def _interaction_dto(row: SocialInteraction) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "platform": row.platform,
        "account_id": row.account_id,
        "remote_id": row.remote_id,
        "kind": row.kind,
        "text": row.text or "",
        "author_remote_id": row.author_remote_id or "",
        "author_name": row.author_name or "",
        "parent_interaction_id": row.parent_interaction_id,
        "thread_id": row.thread_id or "",
        "conversation_id": row.conversation_id,
        "post_remote_id": row.post_remote_id or "",
        "published_post_id": row.published_post_id,
        "content_item_id": row.content_item_id,
        "campaign_id": row.campaign_id,
        "status": row.status,
        "unread": row.status == "unread",
        "moderation_state": row.moderation_state,
        "sentiment": row.sentiment or "",
        "intent": row.intent or "",
        "priority": row.priority,
        "is_question": bool(row.is_question),
        "classifications": list(row.classifications_json or []),
        "moderation": list(row.moderation_json or []),
        "remote_created_at": _iso(row.remote_created_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _action_badge(row: CommunityAction) -> str:
    """AI DRAFT | HUMAN EDITED | AUTO SENT — origin + state, for the UI chip."""
    if row.origin == "human_edited":
        return "HUMAN EDITED"
    if row.state == "sent":
        return "AUTO SENT" if row.origin == "auto" else "SENT"
    return "AI DRAFT"


def _action_dto(row: CommunityAction) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "interaction_id": row.interaction_id,
        "conversation_id": row.conversation_id,
        "account_id": row.account_id,
        "platform": row.platform,
        "action_type": row.action_type,
        "mode": row.mode,
        "state": row.state,
        "origin": row.origin,
        "badge": _action_badge(row),
        "draft_text": row.draft_text or "",
        "final_text": row.final_text or "",
        "brand_check": dict(row.brand_check_json or {}),
        "approval_user_id": row.approval_user_id,
        "rejected_user_id": row.rejected_user_id,
        "is_mock": bool(row.is_mock),
        "remote_reply_id": row.remote_reply_id or "",
        "error": row.error or "",
        "sent_at": _iso(row.sent_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _conversation_dto(
    row: Conversation,
    *,
    unread_count: int = 0,
    last: SocialInteraction | None = None,
) -> dict:
    preview = None
    if last is not None:
        preview = {
            "id": last.id,
            "kind": last.kind,
            "platform": last.platform,
            "author_name": last.author_name or "",
            "status": last.status,
            "text": (last.text or "")[:240],
            "created_at": _iso(last.created_at),
        }
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "platform": row.platform,
        "account_id": row.account_id,
        "thread_key": row.thread_key or "",
        "title": row.title or "",
        "participant_remote_id": row.participant_remote_id or "",
        "participant_name": row.participant_name or "",
        "published_post_id": row.published_post_id,
        "last_interaction_at": _iso(row.last_interaction_at),
        "unread_count": int(unread_count),
        "status": row.status,
        "priority": row.priority,
        "last_interaction": preview,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _opportunity_dto(row: CommunityOpportunity) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "opportunity_type": row.opportunity_type,
        "title": row.title or "",
        "detail": row.detail or "",
        "evidence_count": int(row.evidence_count or 0),
        "source_interaction_ids": list(row.source_interaction_ids or []),
        "confidence": row.confidence,
        "state": row.state,
        "converted_idea_json": dict(row.converted_idea_json or {}),
        "converted_opportunity_id": row.converted_opportunity_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _insight_dto(row: CommunityInsight) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "topic_key": row.topic_key or "",
        "topic": row.topic or "",
        "representative_text": row.representative_text or "",
        "evidence_count": int(row.evidence_count or 0),
        "source_interaction_ids": list(row.source_interaction_ids or []),
        "platforms": list(row.platforms or []),
        "related_content_ids": list(row.related_content_ids or []),
        "confidence": row.confidence,
        "state": row.state,
        "opportunity_id": row.opportunity_id,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


# ---------------------------------------------------------------------------
# lookup helpers (workspace-scoped: foreign id is 404, never a hint)
# ---------------------------------------------------------------------------


def _interaction_or_404(db: Session, ws_id: str, row_id: str) -> SocialInteraction:
    row = db.scalar(
        select(SocialInteraction).where(
            SocialInteraction.id == row_id, SocialInteraction.workspace_id == ws_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="interaction not found")
    return row


def _conversation_or_404(db: Session, ws_id: str, row_id: str) -> Conversation:
    row = db.scalar(
        select(Conversation).where(
            Conversation.id == row_id, Conversation.workspace_id == ws_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return row


def _action_or_404(db: Session, ws_id: str, row_id: str) -> CommunityAction:
    row = db.scalar(
        select(CommunityAction).where(
            CommunityAction.id == row_id, CommunityAction.workspace_id == ws_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="action not found")
    return row


def _opportunity_or_404(db: Session, ws_id: str, row_id: str) -> CommunityOpportunity:
    row = db.scalar(
        select(CommunityOpportunity).where(
            CommunityOpportunity.id == row_id, CommunityOpportunity.workspace_id == ws_id
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    return row


# ---------------------------------------------------------------------------
# keyset pagination (created_at desc, id desc as the tie-break)
# ---------------------------------------------------------------------------


def _encode_cursor(row: SocialInteraction) -> str:
    raw = f"{row.created_at.isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(token: str) -> tuple[datetime, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        stamp, row_id = base64.urlsafe_b64decode(padded.encode("ascii")).decode(
            "utf-8"
        ).rsplit("|", 1)
        return datetime.fromisoformat(stamp), row_id
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid pagination cursor") from exc


def _page_interactions(
    db: Session,
    ws_id: str,
    *,
    criteria: list[Any],
    post_filter: Callable[[SocialInteraction], bool],
    cursor: str | None,
    limit: int,
) -> tuple[list[SocialInteraction], str | None]:
    """Keyset page; walks bounded chunks when a post-filter drops rows."""
    out: list[SocialInteraction] = []
    start = _decode_cursor(cursor) if cursor else None
    last: SocialInteraction | None = None
    for _ in range(_PAGE_CHUNKS):
        stmt = select(SocialInteraction).where(SocialInteraction.workspace_id == ws_id)
        if criteria:
            stmt = stmt.where(*criteria)
        if start is not None:
            stamp, row_id = start
            stmt = stmt.where(
                or_(
                    SocialInteraction.created_at < stamp,
                    and_(SocialInteraction.created_at == stamp, SocialInteraction.id < row_id),
                )
            )
        stmt = stmt.order_by(SocialInteraction.created_at.desc(), SocialInteraction.id.desc())
        rows = list(db.scalars(stmt.limit(limit + 1)).all())
        if not rows:
            return out, None
        exhausted = True
        for row in rows:
            last = row
            if not post_filter(row):
                continue
            out.append(row)
            if len(out) >= limit:
                exhausted = False
                break
        if not exhausted:
            return out, _encode_cursor(last)
        if len(rows) <= limit:
            return out, None
        start = _decode_cursor(_encode_cursor(last))
    return out, (_encode_cursor(last) if last else None)


def _matches_classification(row: SocialInteraction, label: str) -> bool:
    wanted = label.upper()
    for entry in row.classifications_json or []:
        if isinstance(entry, str):
            if entry.upper() == wanted:
                return True
        elif isinstance(entry, dict) and str(entry.get("label", "")).upper() == wanted:
            return True
    return False


def _validated_labels(raw: list) -> list:
    """Write-time label validation (Work 10 Section 0).

    Only entries whose label is in the typed vocabulary
    (``CLASSIFICATION_LABELS`` ≡ ``classify.LABEL_SET``) are persisted;
    strings and ``{"label": ...}`` dicts are both accepted, everything else
    is dropped before it reaches the row. The read-time ``labels_of`` filter
    remains authoritative for display — this is defense in depth so a
    misbehaving engine can never write an unknown label to the DB.
    """
    valid = set(CLASSIFICATION_LABELS)
    out: list = []
    for entry in raw or []:
        if isinstance(entry, str):
            if entry.strip().upper() in valid:
                out.append(entry)
        elif (isinstance(entry, dict)
              and str(entry.get("label", "")).strip().upper() in valid):
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# read / bulk state transitions (safe statuses only — never a send)
# ---------------------------------------------------------------------------


def _set_read(row: SocialInteraction, read: bool) -> bool:
    """Toggle read state. Workflow states (classified/drafted/replied/...) win;
    spam and ignored are never flipped back into the unread queue."""
    if row.status in ("spam", "ignored"):
        return False
    if read:
        if row.status == "unread":
            row.status = "read"
            return True
        return False
    if row.status == "read":
        row.status = "unread"
        return True
    return False


def _mark_spam(row: SocialInteraction) -> bool:
    changed = False
    if row.status != "spam":
        row.status = "spam"
        changed = True
    if row.moderation_state != "review":
        row.moderation_state = "review"
        changed = True
    trail = list(row.moderation_json or [])
    if not any(
        isinstance(m, dict) and m.get("rule") == "bulk_mark_spam" for m in trail
    ):
        trail.append(
            {
                "verdict": "BLOCK_ACTION",
                "rule": "bulk_mark_spam",
                "reason": "operator bulk-marked this interaction as spam",
                "provider": "human",
            }
        )
        row.moderation_json = trail
        changed = True
    return changed


def _mark_ignored(row: SocialInteraction) -> bool:
    if row.status in ("ignored", "spam"):
        return False
    row.status = "ignored"
    return True


def _apply_bulk(row: SocialInteraction, action: str) -> bool:
    if action == "read":
        return _set_read(row, True)
    if action == "unread":
        return _set_read(row, False)
    if action == "spam":
        return _mark_spam(row)
    return _mark_ignored(row)


# ---------------------------------------------------------------------------
# autonomy config (workspace.settings_json["community"])
# ---------------------------------------------------------------------------


def _autonomy_config(ws: Workspace) -> dict:
    stored = (ws.settings_json or {}).get("community")
    merged = dict(DEFAULT_AUTONOMY)
    if isinstance(stored, dict):
        merged.update(stored)
        merged.setdefault("mode", DEFAULT_AUTONOMY["mode"])
        merged.setdefault("classes", list(DEFAULT_AUTONOMY["classes"]))
        merged.setdefault("caps", dict(DEFAULT_AUTONOMY["caps"]))
    return merged


def _validate_autonomy(body: AutonomyUpdate) -> None:
    if body.mode not in AUTONOMY_MODES:
        raise HTTPException(
            status_code=422, detail=f"mode must be one of {list(AUTONOMY_MODES)}"
        )
    allowed = set(CLASSIFICATION_LABELS)
    unknown = [c for c in body.classes if c not in allowed]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown classification labels {unknown}; "
            f"allowed: {list(CLASSIFICATION_LABELS)}",
        )
    if len(body.classes) != len(set(body.classes)):
        raise HTTPException(status_code=422, detail="classes must be unique")
    for key, value in body.caps.items():
        if not str(key).strip():
            raise HTTPException(status_code=422, detail="cap names must be non-empty")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HTTPException(
                status_code=422, detail=f"cap {key} must be a positive integer"
            )


def _store_autonomy(ws: Workspace, body: AutonomyUpdate) -> dict:
    settings = dict(ws.settings_json or {})
    community = dict(settings.get("community") or {})
    community.update({k: v for k, v in body.model_dump().items() if k in body.model_fields_set})
    settings["community"] = community
    ws.settings_json = settings
    return _autonomy_config(ws)


# ---------------------------------------------------------------------------
# platforms (Lane A registry, honest fallback)
# ---------------------------------------------------------------------------


def _extract_specs(raw: Any) -> list:
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, dict):
        for key in ("platforms", "items", "specs", "entries", "registry"):
            value = raw.get(key)
            if isinstance(value, list):
                return list(value)
        specs = []
        for name, spec in raw.items():
            if isinstance(spec, dict):
                entry = {"key": name, **spec}
            else:
                entry = {"key": name, "value": spec}
            specs.append(entry)
        return specs
    for attr in ("specs", "platforms", "entries", "list_platforms"):
        value = getattr(raw, attr, None)
        if value is None:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: S112 - registry internals are not ours
                continue
        if isinstance(value, (list, dict)):
            return _extract_specs(value)
    return []


def _platforms_payload(db: Session, ws_id: str) -> dict:
    try:
        mod = importlib.import_module("app.engine.platform_registry")
        getter = getattr(mod, "get_registry")
    except Exception as exc:  # Lane A lands in parallel — honest fallback
        return {
            "available": False,
            "platforms": [],
            "capabilities": {},
            "error": "platform registry not available",
            "detail": str(exc),
        }
    try:
        raw = getter()
    except Exception as exc:  # never break the inbox on a registry error
        return {
            "available": False,
            "platforms": [],
            "capabilities": {},
            "error": "platform registry failed",
            "detail": str(exc),
        }
    specs = _extract_specs(raw)
    capabilities: dict[str, Any] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        key = spec.get("key") or spec.get("name") or spec.get("id") or spec.get("platform")
        if key:
            capabilities[str(key)] = dict(
                spec.get("capabilities") or spec.get("declared_capabilities") or {}
            )
    observed = list(
        db.scalars(
            select(SocialInteraction.platform)
            .where(SocialInteraction.workspace_id == ws_id)
            .distinct()
            .order_by(SocialInteraction.platform)
        ).all()
    )
    return {
        "available": True,
        "platforms": specs,
        "capabilities": capabilities,
        "observed": observed,
    }


# ---------------------------------------------------------------------------
# metrics / suggestions (Lane C, honest empty shape)
# ---------------------------------------------------------------------------

_EMPTY_METRICS = {
    "available": False,
    "metrics": {},
    "kpis": [],
    "error": "community engine not available",
}


def _normalize_metrics(result: Any) -> dict:
    payload: dict[str, Any] = {"available": True, "metrics": {}, "kpis": [], "totals": {}}
    if isinstance(result, dict):
        payload["metrics"] = dict(result.get("metrics") or {})
        payload["kpis"] = list(result.get("kpis") or [])
        payload["totals"] = dict(result.get("totals") or {})
        if not payload["metrics"] and not payload["kpis"] and not payload["totals"]:
            payload["metrics"] = {
                k: v for k, v in result.items() if isinstance(v, (int, float, str, bool))
            }
    elif isinstance(result, list):
        payload["kpis"] = list(result)
    elif isinstance(result, (int, float)):
        payload["metrics"] = {"value": result}
    return payload


def _normalize_suggestions(result: Any) -> list:
    if isinstance(result, list):
        return list(result)
    if isinstance(result, dict):
        for key in ("suggestions", "items", "ideas"):
            if isinstance(result.get(key), list):
                return list(result[key])
    return []


# ---------------------------------------------------------------------------
# endpoints — platforms
# ---------------------------------------------------------------------------


@inbox_router.get("/platforms", summary="Platform registry specs + capabilities")
def list_platforms(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    return _platforms_payload(db, ws.id)


# ---------------------------------------------------------------------------
# endpoints — interactions
# ---------------------------------------------------------------------------


@inbox_router.get("/interactions", summary="List platform interactions (filtered)")
def list_interactions(
    platform: str | None = Query(default=None, max_length=30),
    classification: str | None = Query(default=None, max_length=30),
    status: str | None = Query(default=None, max_length=20),
    priority: str | None = Query(default=None, max_length=10),
    unread: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    conversation_id: str | None = Query(default=None, max_length=36),
    cursor: str | None = Query(default=None, max_length=400),
    limit: int = Query(default=25, ge=1, le=100),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    if status and status not in INTERACTION_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {list(INTERACTION_STATUSES)}",
        )
    if priority and priority not in PRIORITIES:
        raise HTTPException(
            status_code=422, detail=f"priority must be one of {list(PRIORITIES)}"
        )
    if classification and classification not in CLASSIFICATION_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"classification must be one of {list(CLASSIFICATION_LABELS)}",
        )

    criteria: list[Any] = []
    if platform:
        criteria.append(SocialInteraction.platform == platform)
    if status:
        criteria.append(SocialInteraction.status == status)
    if priority:
        criteria.append(SocialInteraction.priority == priority)
    if unread is True:
        criteria.append(SocialInteraction.status == "unread")
    elif unread is False:
        criteria.append(SocialInteraction.status != "unread")
    if conversation_id:
        criteria.append(SocialInteraction.conversation_id == conversation_id)
    if search:
        like = f"%{escape_like(search.lower())}%"
        criteria.append(
            or_(
                SocialInteraction.text.ilike(like, escape="\\"),
                SocialInteraction.author_name.ilike(like, escape="\\"),
            )
        )

    post_filter = (
        (lambda row: _matches_classification(row, classification))
        if classification
        else (lambda _row: True)
    )
    rows, next_cursor = _page_interactions(
        db,
        ws.id,
        criteria=criteria,
        post_filter=post_filter,
        cursor=cursor,
        limit=limit,
    )
    return {"items": [_interaction_dto(r) for r in rows], "next_cursor": next_cursor}


@inbox_router.get("/interactions/{interaction_id}", summary="Interaction detail + thread")
def get_interaction(
    interaction_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _interaction_or_404(db, ws.id, interaction_id)

    publication = None
    if row.published_post_id:
        post = db.scalar(
            select(PublishedPost).where(
                PublishedPost.id == row.published_post_id,
                PublishedPost.workspace_id == ws.id,
            )
        )
        if post is not None:
            publication = {
                "id": post.id,
                "title": post.title or "",
                "remote_url": post.remote_url or "",
                "campaign_id": post.campaign_id,
                "platform": post.platform,
                "remote_post_id": post.remote_post_id or "",
            }

    thread: list[SocialInteraction] = []
    seen = {row.id}
    cursor = row.parent_interaction_id
    while cursor and len(thread) < 20 and cursor not in seen:
        seen.add(cursor)
        parent = db.scalar(
            select(SocialInteraction).where(
                SocialInteraction.id == cursor,
                SocialInteraction.workspace_id == ws.id,
            )
        )
        if parent is None:
            break
        thread.append(parent)
        cursor = parent.parent_interaction_id
    thread.reverse()

    actions = list(
        db.scalars(
            select(CommunityAction)
            .where(
                CommunityAction.workspace_id == ws.id,
                CommunityAction.interaction_id == row.id,
            )
            .order_by(CommunityAction.created_at.desc())
        ).all()
    )
    return {
        "interaction": _interaction_dto(row),
        "linked_publication": publication,
        "thread": [_interaction_dto(t) for t in thread],
        "actions": [_action_dto(a) for a in actions],
    }


@inbox_router.post("/interactions/{interaction_id}/read", summary="Mark read / unread")
def set_read(
    interaction_id: str,
    body: ReadBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _interaction_or_404(db, ws.id, interaction_id)
    _set_read(row, body.read)
    db.commit()
    return {"interaction": _interaction_dto(row)}


@inbox_router.post(
    "/interactions/{interaction_id}/classify",
    summary="Run AI classification (community engine)",
)
def classify_interaction(
    interaction_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _interaction_or_404(db, ws.id, interaction_id)
    fn = _engine_fn(*_CLASSIFY_FN)
    if fn is None:
        raise _unavailable()
    result = _run_engine(
        fn, db=db, session=db, workspace_id=ws.id, interaction=row, interaction_id=row.id
    )
    if isinstance(result, list):
        row.classifications_json = _validated_labels(result)
    elif isinstance(result, dict):
        labels = result.get("classifications") or result.get("labels")
        if isinstance(labels, list):
            row.classifications_json = _validated_labels(labels)
    db.commit()
    row = _interaction_or_404(db, ws.id, interaction_id)
    return {
        "interaction": _interaction_dto(row),
        "classifications": list(row.classifications_json or []),
    }


@inbox_router.post(
    "/interactions/{interaction_id}/draft",
    summary="Draft a reply + brand check (community engine)",
)
def draft_interaction(
    interaction_id: str,
    body: DraftBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _interaction_or_404(db, ws.id, interaction_id)
    fn = _engine_fn(*_DRAFT_FN)
    if fn is None:
        raise _unavailable()
    started = utcnow()
    result = _run_engine(
        fn,
        db=db,
        session=db,
        ws=ws,
        workspace_id=ws.id,
        interaction=row,
        interaction_id=row.id,
        regenerate=body.regenerate,
        # a manual draft never auto-sends, even in LOW_RISK_AUTO mode
        auto=False,
    )
    db.flush()

    action: CommunityAction | None = None
    if isinstance(result, CommunityAction):
        action = result
    elif isinstance(result, dict) and isinstance(result.get("action"), CommunityAction):
        action = result["action"]

    if action is None:
        action = db.scalar(
            select(CommunityAction)
            .where(
                CommunityAction.workspace_id == ws.id,
                CommunityAction.interaction_id == row.id,
            )
            .order_by(CommunityAction.created_at.desc())
            .limit(1)
        )
        if action is not None and action.created_at and action.created_at < started:
            action = None

    if action is None:
        text = ""
        brand_check: dict = {}
        if isinstance(result, dict):
            text = str(result.get("text") or result.get("draft_text") or "")
            brand_check = dict(result.get("brand_check") or {})
        elif isinstance(result, str):
            text = result
        if not text:
            raise HTTPException(
                status_code=500, detail="community engine returned no draft"
            )
        autonomy = _autonomy_config(ws)
        action = CommunityAction(
            workspace_id=ws.id,
            interaction_id=row.id,
            conversation_id=row.conversation_id,
            account_id=row.account_id,
            platform=row.platform,
            action_type="REPLY",
            mode=autonomy["mode"],
            state="draft",
            origin="ai",
            draft_text=text,
            brand_check_json=brand_check,
        )
        db.add(action)
        db.flush()

    db.commit()
    return {"action": _action_dto(action), "interaction": _interaction_dto(row)}


# ---------------------------------------------------------------------------
# endpoints — conversations
# ---------------------------------------------------------------------------


@inbox_router.get("/conversations", summary="Conversation list + unread + preview")
def list_conversations(
    platform: str | None = Query(default=None, max_length=30),
    status: str | None = Query(default=None, max_length=20),
    unread: bool | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    criteria: list[Any] = [Conversation.workspace_id == ws.id]
    if platform:
        criteria.append(Conversation.platform == platform)
    if status:
        criteria.append(Conversation.status == status)
    if unread is True:
        live = select(SocialInteraction.conversation_id).where(
            SocialInteraction.workspace_id == ws.id,
            SocialInteraction.status == "unread",
        )
        criteria.append(Conversation.id.in_(live))
    elif unread is False:
        live = select(SocialInteraction.conversation_id).where(
            SocialInteraction.workspace_id == ws.id,
            SocialInteraction.status == "unread",
        )
        criteria.append(~Conversation.id.in_(live))

    rows = list(
        db.scalars(
            select(Conversation)
            .where(*criteria)
            .order_by(Conversation.last_interaction_at.desc(), Conversation.created_at.desc())
            .limit(limit)
        ).all()
    )

    out = []
    for row in rows:
        unread_count = 0
        if row.id:
            unread_count = len(
                list(
                    db.scalars(
                        select(SocialInteraction.id).where(
                            SocialInteraction.workspace_id == ws.id,
                            SocialInteraction.conversation_id == row.id,
                            SocialInteraction.status == "unread",
                        )
                    ).all()
                )
            )
        last = db.scalar(
            select(SocialInteraction)
            .where(
                SocialInteraction.workspace_id == ws.id,
                SocialInteraction.conversation_id == row.id,
            )
            .order_by(SocialInteraction.created_at.desc())
            .limit(1)
        )
        out.append(_conversation_dto(row, unread_count=unread_count, last=last))
    return {"items": out}


@inbox_router.get("/conversations/{conversation_id}", summary="Conversation + thread")
def get_conversation(
    conversation_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _conversation_or_404(db, ws.id, conversation_id)
    interactions = list(
        db.scalars(
            select(SocialInteraction)
            .where(
                SocialInteraction.workspace_id == ws.id,
                SocialInteraction.conversation_id == row.id,
            )
            .order_by(SocialInteraction.created_at.asc(), SocialInteraction.id.asc())
        ).all()
    )
    unread_count = sum(1 for i in interactions if i.status == "unread")
    last = interactions[-1] if interactions else None
    return {
        "conversation": _conversation_dto(row, unread_count=unread_count, last=last),
        "interactions": [_interaction_dto(i) for i in interactions],
    }


@inbox_router.post(
    "/conversations/{conversation_id}/reply",
    summary="Queue a reply draft (send=true goes through the policy gate)",
)
def reply_conversation(
    conversation_id: str,
    body: ReplyBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv = _conversation_or_404(db, ws.id, conversation_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="reply text must not be empty")

    if body.interaction_id:
        anchor = _interaction_or_404(db, ws.id, body.interaction_id)
        if anchor.conversation_id and anchor.conversation_id != conv.id:
            raise HTTPException(status_code=404, detail="interaction not found")
    else:
        anchor = db.scalar(
            select(SocialInteraction)
            .where(
                SocialInteraction.workspace_id == ws.id,
                SocialInteraction.conversation_id == conv.id,
            )
            .order_by(SocialInteraction.created_at.desc())
            .limit(1)
        )
    if anchor is None:
        raise HTTPException(
            status_code=422, detail="conversation has no interaction to reply to"
        )

    autonomy = _autonomy_config(ws)
    action = CommunityAction(
        workspace_id=ws.id,
        interaction_id=anchor.id,
        conversation_id=conv.id,
        account_id=anchor.account_id or conv.account_id,
        platform=anchor.platform or conv.platform,
        action_type="REPLY",
        mode=autonomy["mode"],
        state="draft",
        # the operator typed this text themselves — provenance is human
        origin="human_edited",
        draft_text=text,
    )
    db.add(action)
    db.flush()

    if body.send:
        fn = _engine_fn(*_SEND_FN)
        if fn is None:
            db.rollback()
            raise _unavailable()
        result = _run_engine(
            fn,
            db=db,
            session=db,
            ws=ws,
            workspace_id=ws.id,
            action=action,
            action_id=action.id,
            interaction=anchor,
            interaction_id=anchor.id,
            text=text,
            actor=current_user.id,
        )
        reason = _denied_reason(result)
        if reason:
            db.rollback()
            raise HTTPException(status_code=403, detail=reason)

    db.commit()
    return {"action": _action_dto(action), "interaction": _interaction_dto(anchor)}


# ---------------------------------------------------------------------------
# endpoints — actions (approve / reject / send / list)
# ---------------------------------------------------------------------------


@inbox_router.get("/actions", summary="Queued / sent community actions")
def list_actions(
    state: str | None = Query(default=None, max_length=20),
    action_type: str | None = Query(default=None, max_length=24),
    interaction_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=50, ge=1, le=200),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    criteria: list[Any] = [CommunityAction.workspace_id == ws.id]
    if state:
        criteria.append(CommunityAction.state == state)
    if action_type:
        criteria.append(CommunityAction.action_type == action_type)
    if interaction_id:
        criteria.append(CommunityAction.interaction_id == interaction_id)
    rows = list(
        db.scalars(
            select(CommunityAction)
            .where(*criteria)
            .order_by(CommunityAction.created_at.desc())
            .limit(limit)
        ).all()
    )
    return {"items": [_action_dto(r) for r in rows]}


@inbox_router.post("/actions/{action_id}/approve", summary="Approve a queued action")
def approve_action(
    action_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _action_or_404(db, ws.id, action_id)
    fn = _engine_fn(*_APPROVE_FN)
    if fn is None:
        raise _unavailable()
    result = _run_engine(
        fn,
        db=db,
        session=db,
        ws=ws,
        workspace_id=ws.id,
        action=row,
        action_id=row.id,
        user_id=current_user.id,
        actor=current_user.id,
    )
    reason = _denied_reason(result)
    if reason:
        db.rollback()
        raise HTTPException(status_code=403, detail=reason)
    # the gate may only record an audit — make sure the state actually advances
    if row.state in ("draft", "pending_approval"):
        row.state = "approved"
        row.approval_user_id = current_user.id
    db.commit()
    return {"action": _action_dto(row)}


@inbox_router.post("/actions/{action_id}/reject", summary="Reject a queued action")
def reject_action(
    action_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _action_or_404(db, ws.id, action_id)
    if row.state == "sent":
        raise HTTPException(status_code=409, detail="action already sent")
    row.state = "rejected"
    row.rejected_user_id = current_user.id
    db.commit()
    return {"action": _action_dto(row)}


@inbox_router.post(
    "/actions/{action_id}/send",
    summary="Policy-gated send (edited text becomes origin=human_edited)",
)
def send_action(
    action_id: str,
    body: SendBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _action_or_404(db, ws.id, action_id)
    if row.state in ("sent", "rejected", "blocked"):
        raise HTTPException(status_code=409, detail=f"action is {row.state}")

    text = body.text if body.text is not None else (row.final_text or row.draft_text)
    if not (text or "").strip():
        raise HTTPException(status_code=422, detail="send text must not be empty")

    # persist the human edit FIRST so a policy rejection never loses the typing
    edited = text != (row.draft_text or "")
    if edited:
        row.origin = "human_edited"
    row.final_text = text
    db.commit()

    fn = _engine_fn(*_SEND_FN)
    if fn is None:
        raise _unavailable()
    result = _run_engine(
        fn,
        db=db,
        session=db,
        ws=ws,
        workspace_id=ws.id,
        action=row,
        action_id=row.id,
        interaction_id=row.interaction_id,
        text=text,
        actor=current_user.id,
    )
    reason = _denied_reason(result)
    if reason:
        raise HTTPException(status_code=403, detail=reason)
    db.commit()
    row = _action_or_404(db, ws.id, action_id)
    return {"action": _action_dto(row), "result": result if isinstance(result, dict) else {}}


@inbox_router.post("/bulk", summary="Bulk triage (read/unread/spam/ignore — never a send)")
def bulk_action(
    body: BulkBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    rows = list(
        db.scalars(
            select(SocialInteraction).where(
                SocialInteraction.workspace_id == ws.id,
                SocialInteraction.id.in_(body.ids),
            )
        ).all()
    )
    if len(rows) != len(set(body.ids)):
        raise HTTPException(status_code=404, detail="interaction not found")
    updated = sum(1 for row in rows if _apply_bulk(row, body.action))
    db.commit()
    return {
        "action": body.action,
        "requested": len(body.ids),
        "updated": updated,
    }


# ---------------------------------------------------------------------------
# endpoints — analytics / opportunities / insights / sync / autonomy
# ---------------------------------------------------------------------------


@inbox_router.get("/analytics", summary="Community metrics (community engine)")
def community_analytics(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    fn = _engine_fn(*_METRICS_FN)
    if fn is None:
        return dict(_EMPTY_METRICS)
    result = _run_engine(fn, db=db, session=db, workspace_id=ws.id)
    return _normalize_metrics(result)


@inbox_router.get("/opportunities", summary="Lead / partnership / request signals")
def list_opportunities(
    state: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    if state and state not in OPPORTUNITY_STATES:
        raise HTTPException(
            status_code=422, detail=f"state must be one of {list(OPPORTUNITY_STATES)}"
        )
    criteria: list[Any] = [CommunityOpportunity.workspace_id == ws.id]
    if state:
        criteria.append(CommunityOpportunity.state == state)
    rows = list(
        db.scalars(
            select(CommunityOpportunity)
            .where(*criteria)
            .order_by(CommunityOpportunity.created_at.desc())
            .limit(limit)
        ).all()
    )
    return {"items": [_opportunity_dto(r) for r in rows]}


@inbox_router.post(
    "/opportunities/{opportunity_id}/convert",
    summary="Convert an opportunity into a content idea",
)
def convert_opportunity(
    opportunity_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _opportunity_or_404(db, ws.id, opportunity_id)
    fn = _engine_fn(*_CONVERT_FN)
    if fn is None:
        raise _unavailable()
    result = _run_engine(
        fn,
        db=db,
        session=db,
        workspace_id=ws.id,
        opportunity=row,
        opportunity_id=row.id,
    )
    reason = _denied_reason(result)
    if reason:
        db.rollback()
        raise HTTPException(status_code=403, detail=reason)
    if row.state != "converted" and result:
        if not row.converted_idea_json and isinstance(result, dict):
            row.converted_idea_json = dict(result)
        row.state = "converted"
    db.commit()
    return {
        "opportunity": _opportunity_dto(row),
        "result": result if isinstance(result, dict) else {},
    }


@inbox_router.post("/opportunities/{opportunity_id}/dismiss", summary="Dismiss an opportunity")
def dismiss_opportunity(
    opportunity_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _opportunity_or_404(db, ws.id, opportunity_id)
    row.state = "dismissed"
    db.commit()
    return {"opportunity": _opportunity_dto(row)}


@inbox_router.get("/insights", summary="Aggregated insights + content suggestions")
def list_insights(
    limit: int = Query(default=50, ge=1, le=200),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    rows = list(
        db.scalars(
            select(CommunityInsight)
            .where(CommunityInsight.workspace_id == ws.id)
            .order_by(CommunityInsight.evidence_count.desc(), CommunityInsight.created_at.desc())
            .limit(limit)
        ).all()
    )
    fn = _engine_fn(*_FEEDBACK_FN)
    suggestions: list = []
    if fn is not None:
        suggestions = _normalize_suggestions(
            _run_engine(fn, db=db, session=db, workspace_id=ws.id)
        )
    return {
        "items": [_insight_dto(r) for r in rows],
        "suggestions": suggestions,
        "suggestions_available": fn is not None,
    }


@inbox_router.post("/sync", summary="Enqueue a community sync job")
def sync_inbox(
    body: SyncBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    payload: dict[str, Any] = {}
    if body.platform:
        payload["platform"] = body.platform
    if body.account_id:
        payload["account_id"] = body.account_id
    job_id = enqueue("COMMUNITY_SYNC", payload, workspace_id=ws.id)
    return {"job_id": job_id, "type": "COMMUNITY_SYNC", "payload": payload}


@inbox_router.get("/autonomy", summary="Community autonomy configuration")
def get_autonomy(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    return {
        "autonomy": _autonomy_config(ws),
        "modes": list(AUTONOMY_MODES),
        "classes": list(CLASSIFICATION_LABELS),
        "defaults": dict(DEFAULT_AUTONOMY),
    }


@inbox_router.put("/autonomy", summary="Update community autonomy (admin only)")
def put_autonomy(
    body: AutonomyUpdate,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    _validate_autonomy(body)
    autonomy = _store_autonomy(ws, body)
    db.commit()
    return {"autonomy": autonomy, "modes": list(AUTONOMY_MODES)}


# ---------------------------------------------------------------------------
# job handler registration (Lane B/C export these; guarded so the API never
# fails to import because a sibling lane is mid-landing)
# ---------------------------------------------------------------------------


def _bootstrap_community_jobs() -> None:
    """Register the community job handlers idempotently (Work 10 Section 0).

    Each target is imported and invoked inside its own guard: one missing or
    half-landed sibling module can never break the API import, and the other
    target still registers. The register functions themselves skip duplicates,
    so re-running this (module reload, direct test call) is safe.
    """
    for _mod, _fn in (
        ("app.engine.community.sync", "register_community_sync_jobs"),
        ("app.engine.community.jobs", "register_community_jobs"),
    ):
        try:
            _module = importlib.import_module(_mod)
            getattr(_module, _fn)()
        except Exception:  # noqa: S110 - sibling lanes land in parallel
            pass


_bootstrap_community_jobs()

__all__ = ["inbox_router"]
