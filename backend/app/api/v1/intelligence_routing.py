"""Intelligence routing + context budget APIs (Work 05, Lane B).

- POST /routing/route          route a task to a model tier (model + reason)
- GET  /routing/log            observable routing decisions for this workspace
- GET  /routing/health         provider-slot health
- POST /context/budget         run reversible context filtering (metrics + refs)
- POST /context/recall/{ref}   restore the exact original content

Workspace isolation: the context store is namespaced per workspace, so a
reference created in workspace A recalls as 404 from workspace B.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.engine.intelligence.context_budget import ContextBudgetManager
from app.engine.intelligence.router import (
    LLMRoutingError,
    PrivacyRefusal,
    RouteRequest,
    default_router,
)
from app.engine.intelligence.sanitize import redact_secrets
from app.models import Workspace, WorkspaceMember
from app.services.auth_service import get_current_user

intelligence_router = APIRouter(
    prefix="/workspaces/{workspace_id}/intelligence", tags=["intelligence"]
)

_MAX_WORKSPACE_STORES = 500
_BUDGET_STORES: dict[str, ContextBudgetManager] = {}


def _workspace_or_404(minimum_role: str):
    """Membership dependency that masks cross-workspace access as 404.

    Same contract as the decisions surface (Lane A): unknown workspace,
    non-member, or insufficient role never leaks existence — outsiders read
    404, members without the role read 403.
    """
    order = {
        WorkspaceMember.ROLE_VIEWER: 0,
        WorkspaceMember.ROLE_MEMBER: 1,
        WorkspaceMember.ROLE_ADMIN: 2,
        WorkspaceMember.ROLE_OWNER: 3,
    }

    def dependency(
        workspace_id: str,
        user=Depends(get_current_user),
        db=Depends(get_db),
    ) -> Workspace:
        ws = db.get(Workspace, workspace_id)
        if not ws:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
        member = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id
            )
        )
        if member is None and not user.is_superuser:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
        if member is not None and order[member.role] < order[minimum_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return ws

    return dependency


def _budget_store(workspace_id: str) -> ContextBudgetManager:
    store = _BUDGET_STORES.get(workspace_id)
    if store is None:
        if len(_BUDGET_STORES) >= _MAX_WORKSPACE_STORES:
            _BUDGET_STORES.pop(next(iter(_BUDGET_STORES)))
        store = ContextBudgetManager()
        _BUDGET_STORES[workspace_id] = store
    return store


class RouteBody(BaseModel):
    task_type: str = "general"
    modality: str = "text"
    complexity: float = Field(default=0.5, ge=0.0, le=1.0)
    context_tokens: int = Field(default=0, ge=0)
    latency_sensitive: bool = False
    quality_required: str = "standard"
    budget_usd: float | None = None
    tier: str | None = None
    require_remote: bool = False


class BudgetItemBody(BaseModel):
    content: str = ""
    category: str | None = None
    pinned: bool | None = None


class BudgetBody(BaseModel):
    items: list[BudgetItemBody] = Field(default_factory=list)
    max_tokens: int = Field(default=4000, ge=1, le=500000)


@intelligence_router.post("/routing/route")
def route_task(body: RouteBody, ws: Workspace = Depends(_workspace_or_404("member"))):
    router = default_router()
    request = RouteRequest(
        task_type=body.task_type,
        modality=body.modality,
        complexity=body.complexity,
        context_tokens=body.context_tokens,
        latency_sensitive=body.latency_sensitive,
        quality_required=body.quality_required,
        budget_usd=body.budget_usd,
        tier=body.tier,
        require_remote=body.require_remote,
        workspace_id=ws.id,
        workspace_settings=ws.settings_json or {},
    )
    try:
        decision = router.route(request)
    except PrivacyRefusal as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LLMRoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return decision.to_dict()


@intelligence_router.get("/routing/log")
def routing_log(ws: Workspace = Depends(_workspace_or_404("viewer"))):
    return {"entries": redact_secrets(default_router().log(ws.id))}


@intelligence_router.get("/routing/health")
def routing_health(ws: Workspace = Depends(_workspace_or_404("viewer"))):
    return {"providers": default_router().health()}


@intelligence_router.post("/context/budget")
def run_budget(body: BudgetBody, ws: Workspace = Depends(_workspace_or_404("member"))):
    manager = _budget_store(ws.id)
    items = [
        manager.add(item.content, category=item.category, pinned=item.pinned)
        for item in body.items
    ]
    result = manager.budget(items, max_tokens=body.max_tokens)
    payload = result.to_dict()
    # References are served over the API: scrub summaries so credential-like
    # input can never leak through a stub. Originals stay in process memory
    # only (required for reversible recall) and are never logged.
    payload["references"] = redact_secrets(payload["references"])
    return payload


@intelligence_router.post("/context/recall/{ref_id}")
def recall_context(ref_id: str, ws: Workspace = Depends(_workspace_or_404("viewer"))):
    manager = _budget_store(ws.id)
    try:
        item = manager.recall(ref_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="context reference not found") from None
    return {
        "ref_id": item.id,
        "content": item.content,
        "category": item.category.value,
        "tokens": item.tokens,
    }
