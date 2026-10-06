"""Auth endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.models import User, Workspace, WorkspaceMember
from app.services.auth_service import (
    authenticate,
    get_current_user,
    issue_tokens,
    register_user,
    rotate_refresh_token,
)
from app.services.capabilities import (
    MeResponse,
    WorkspaceCapability,
    capabilities_for_role,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(default="", max_length=120)


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=200)


class RefreshBody(BaseModel):
    refresh_token: str


@router.post("/register", summary="Create an account")
def register(body: RegisterBody, request: Request, db=Depends(get_db)):
    user = register_user(db, body.email, body.password, body.display_name)
    # Keep registration, workspace bootstrap, membership, and token issuance
    # in one transaction. A partial commit here creates an account that cannot
    # complete onboarding if workspace creation later fails.
    db.flush()
    # Bootstrap a personal workspace so onboarding is instant.
    ws = Workspace(
        name=f"{user.display_name}'s workspace",
        slug=f"ws-{user.id[:8]}",
        settings_json={"scoring_weights": {}, "automation": "FULL_AUTOPILOT"},
    )
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceMember.ROLE_OWNER))
    tokens = issue_tokens(db, user, request.headers.get("user-agent", ""))
    db.commit()
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}, **tokens, "workspace": {"id": ws.id, "name": ws.name}}


@router.post("/login", summary="Exchange credentials for tokens")
def login(body: LoginBody, request: Request, db=Depends(get_db)):
    user = authenticate(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    tokens = issue_tokens(db, user, request.headers.get("user-agent", ""))
    default_ws = db.scalar(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .limit(1)
    )
    db.commit()
    return {
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name},
        **tokens,
        "workspace": {"id": default_ws.id, "name": default_ws.name} if default_ws else None,
    }


@router.post("/refresh", summary="Rotate refresh token")
def refresh(body: RefreshBody, db=Depends(get_db)):
    result = rotate_refresh_token(db, body.refresh_token)
    if not result:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    db.commit()
    return result


@router.get("/me", summary="Current user profile", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db=Depends(get_db)):
    rows = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
    ).all()

    workspaces = [
        WorkspaceCapability(
            id=w.id,
            name=w.name,
            slug=w.slug,
            # The raw stored role. The client branches on `capabilities`, never
            # on this string, so adding a role cannot silently change behaviour.
            role=str(role),
            capabilities=capabilities_for_role(role),
        )
        for w, role in rows
    ]

    # Union across memberships, sorted so the UI list is stable.
    union: set[str] = set()
    for ws in workspaces:
        union.update(ws.capabilities)

    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name or None,
        is_superuser=bool(user.is_superuser),
        workspaces=workspaces,
        capabilities=sorted(union),
    )



