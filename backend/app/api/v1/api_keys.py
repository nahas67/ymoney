"""Workspace-scoped third-party API keys (separate from user JWT auth).

Keys look like `ym_<urlsafe>`; only the sha256 hash is stored (see
core.security.hash_token — same pattern as refresh tokens). The plaintext is
returned once at mint time and never again. Key auth is accepted on the
`/me` proof endpoint via `Authorization: Bearer ym_...` or `X-API-Key`;
existing JWT paths are untouched.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core import security
from app.db import get_db
from app.models import Workspace, WorkspaceApiKey
from app.models.base import utcnow
from app.services.auth_service import require_workspace_role

router = APIRouter(prefix="/workspaces/{workspace_id}/api-keys", tags=["api-keys"])

_KEY_PREFIX = "ym_"
_ROLES = ("viewer", "member", "admin")


def _new_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


class MintKeyBody(BaseModel):
    name: str = Field(default="", max_length=120)
    role: str = Field(default="member", max_length=20)


def _public(row: WorkspaceApiKey) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "role": row.role,
        "revoked": bool(row.revoked),
        "last_used_at": row.last_used_at.isoformat() + "Z" if row.last_used_at else None,
        "created_at": row.created_at.isoformat() + "Z",
    }


@router.post("", summary="Mint a scoped API key (plaintext shown once)")
def mint_key(
    workspace_id: str,
    body: MintKeyBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    role = (body.role or "member").strip().lower()
    if role not in _ROLES:
        raise HTTPException(status_code=422, detail="role must be viewer|member|admin")
    raw = _new_key()
    row = WorkspaceApiKey(
        workspace_id=ws.id,
        name=(body.name or "").strip()[:120],
        prefix=raw[:12],
        key_hash=security.hash_token(raw),
        role=role,
    )
    db.add(row)
    db.commit()
    from app.services.events import record_event

    record_event(ws.id, "api_key.minted", f"API key '{row.name or row.prefix}' minted (role {role})",
                 level="info", source="security", data={"key_id": row.id, "role": role})
    return {"id": row.id, "api_key": raw, **_public(row)}


@router.get("", summary="List API key metadata (never hashes)")
def list_keys(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(
        select(WorkspaceApiKey).where(WorkspaceApiKey.workspace_id == ws.id).order_by(WorkspaceApiKey.created_at.desc())
    ).all()
    return {"items": [_public(r) for r in rows]}


@router.post("/{key_id}/revoke", summary="Revoke an API key")
def revoke_key(
    workspace_id: str,
    key_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    row = db.get(WorkspaceApiKey, key_id)
    if not row or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="API key not found")
    row.revoked = True
    db.commit()
    from app.services.events import record_event

    record_event(ws.id, "api_key.revoked", f"API key '{row.name or row.prefix}' revoked",
                 level="warning", source="security", data={"key_id": row.id})
    return {"revoked": True}


def resolve_api_key(request: Request, db, workspace_id: str) -> WorkspaceApiKey:
    """Key-only auth: Bearer ym_... or X-API-Key. 401 unless a live key matches."""
    raw = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw = auth.split(" ", 1)[1].strip()
    if not raw.startswith(_KEY_PREFIX):
        raw = (request.headers.get("x-api-key", "") or "").strip()
    if not raw.startswith(_KEY_PREFIX):
        raise HTTPException(status_code=401, detail="API key required (Bearer ym_... or X-API-Key)")
    candidate_hash = security.hash_token(raw)
    rows = db.scalars(select(WorkspaceApiKey).where(WorkspaceApiKey.prefix == raw[:12])).all()
    row = next((r for r in rows if hmac.compare_digest(r.key_hash, candidate_hash)), None)
    if row is None or row.revoked:
        raise HTTPException(status_code=401, detail="invalid or revoked API key")
    if row.workspace_id != workspace_id:
        raise HTTPException(status_code=403, detail="API key not valid for this workspace")
    row.last_used_at = utcnow()
    db.commit()
    return row


@router.get("/me", summary="Prove key auth (key-only, no JWT)")
def key_me(workspace_id: str, request: Request, db=Depends(get_db)):
    row = resolve_api_key(request, db, workspace_id)
    return {"workspace_id": row.workspace_id, "role": row.role, "prefix": row.prefix, "name": row.name, "key_id": row.id}
