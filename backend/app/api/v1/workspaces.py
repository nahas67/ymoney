"""Workspace endpoints: CRUD, members, settings, trend sources, agent configs."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.models import TrendSource, User, Workspace, WorkspaceMember
from app.services.auth_service import get_current_user, require_workspace_role

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}$")


class WorkspaceBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    niche: str = Field(default="", max_length=200)
    brand_voice: str = Field(default="", max_length=4000)


class SettingsBody(BaseModel):
    settings: dict = Field(default_factory=dict)


class TrendSourceBody(BaseModel):
    kind: str
    name: str = Field(default="", max_length=120)
    enabled: bool = True
    priority: int = Field(default=50, ge=0, le=1000)
    config: dict = Field(default_factory=dict)


class AgentConfigBody(BaseModel):
    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=120)
    prompt_override: str | None = Field(default=None, max_length=8000)
    timeout_seconds: int | None = Field(default=None, ge=10, le=3600)
    cost_limit_usd: float | None = None


def _serialize_ws(ws: Workspace) -> dict:
    return {
        "id": ws.id,
        "name": ws.name,
        "slug": ws.slug,
        "niche": ws.niche,
        "brand_voice": ws.brand_voice,
        "language": ws.language,
        "timezone": ws.timezone,
        "settings": ws.settings_json or {},
        "created_at": ws.created_at.isoformat() + "Z",
    }


@router.get("", summary="List my workspaces")
def list_workspaces(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = db.scalars(
        select(Workspace).join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id).where(
            WorkspaceMember.user_id == user.id
        )
    ).all()
    return {"items": [_serialize_ws(w) for w in rows]}


@router.post("", summary="Create a workspace")
def create_workspace(body: WorkspaceBody, user: User = Depends(get_current_user), db=Depends(get_db)):
    base_slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:60] or "workspace"
    slug = base_slug
    n = 1
    while db.scalar(select(Workspace).where(Workspace.slug == slug)):
        slug = f"{base_slug}-{n}"
        n += 1
    ws = Workspace(name=body.name, slug=slug, niche=body.niche, brand_voice=body.brand_voice)
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceMember.ROLE_OWNER))
    db.commit()
    return _serialize_ws(ws)


@router.get("/{workspace_id}", summary="Get workspace")
def get_workspace(
    workspace_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    return _serialize_ws(ws)


@router.patch("/{workspace_id}", summary="Update workspace")
def update_workspace(
    workspace_id: str,
    body: WorkspaceBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    ws.name = body.name or ws.name
    ws.niche = body.niche
    ws.brand_voice = body.brand_voice
    db.commit()
    return _serialize_ws(ws)


@router.get("/{workspace_id}/members", summary="List workspace members")
def list_members(workspace_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in rows]) if rows else select(User).where(False))).all()}
    return {
        "items": [
            {"user_id": m.user_id, "role": m.role, "email": users.get(m.user_id).email if m.user_id in users else ""}
            for m in rows
        ]
    }


# -- settings ----------------------------------------------------------------


@router.get("/{workspace_id}/settings", summary="Get workspace settings")
def get_settings(workspace_id: str, ws: Workspace = Depends(require_workspace_role("viewer"))):
    return {"settings": ws.settings_json or {}}


@router.put("/{workspace_id}/settings", summary="Update workspace settings (merged)")
def put_settings(
    workspace_id: str,
    body: SettingsBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    merged = dict(ws.settings_json or {})
    for k, v in (body.settings or {}).items():
        merged[k] = v
    ws.settings_json = merged
    db.commit()
    return {"settings": merged}


# -- trend sources ------------------------------------------------------------


@router.get("/{workspace_id}/trend-sources")
def list_trend_sources(workspace_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(select(TrendSource).where(TrendSource.workspace_id == workspace_id)).all()
    return {
        "items": [
            {
                "id": t.id,
                "kind": t.kind,
                "name": t.name,
                "enabled": t.enabled,
                "priority": t.priority,
                "config": t.config_json or {},
            }
            for t in rows
        ]
    }


@router.post("/{workspace_id}/trend-sources")
def add_trend_source(
    workspace_id: str,
    body: TrendSourceBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.providers.trends import REGISTRY

    if body.kind not in REGISTRY:
        raise HTTPException(status_code=400, detail=f"unknown source kind '{body.kind}'")
    row = TrendSource(
        workspace_id=ws.id,
        kind=body.kind,
        name=body.name or REGISTRY[body.kind].name,
        enabled=body.enabled,
        priority=body.priority,
        config_json=body.config,
    )
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.delete("/{workspace_id}/trend-sources/{source_id}")
def delete_trend_source(
    workspace_id: str,
    source_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    row = db.get(TrendSource, source_id)
    if not row or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="trend source not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}


# -- agents configuration moved to /workspaces/{id}/agents/config (misc.py) ---
