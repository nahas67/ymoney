"""Comments API (Work 11 Lane R) -- contracts §6.

Canonical surface, mounted once in ``api/v1/__init__.py``::

    POST /workspaces/{ws}/comments                    add (or reply)
    GET  /workspaces/{ws}/comments?target_type=&target_id=
    POST /workspaces/{ws}/comments/{id}/resolve       root comments only
    POST /workspaces/{ws}/comments/{id}/reopen        root comments only

Floors are the existing ``require_workspace_role`` dependencies. Project
scoping uses the ``comment`` capability from ``services/project_auth``
(contracts §3): a project-scoped write/resolve requires it; unlinked targets
fall through to the workspace floor (viewer for writes, per §6).

Anchor validation (contracts §6): ``timestamp`` requires ``t_start`` (>= 0);
``time_range`` requires ``t_start`` + ``t_end`` with ``t_end > t_start``; both
answer 422. Mentions must be workspace members (422). Replies must target the
same target as their parent (422) and are FLAT under the root.

Comments NEVER mutate content: the handlers write only the ``comments`` row.

Error hygiene: ValueError -> 422, missing/foreign ids -> 404 (never 403),
anything else -> generic 500 ``{"detail": "internal error"}`` logged on
``ymoney.collab`` with no echo.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine.collab import comments as engine
from app.models import User, Workspace
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability

comments_router = APIRouter(prefix="/workspaces/{workspace_id}/comments", tags=["comments"])
logger = logging.getLogger("ymoney.collab")


def _short(exc: BaseException, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body (contracts §6)."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error, detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception("comments route failed: %s", getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


class CommentCreateBody(BaseModel):
    target_type: Literal[
        "timeline", "timestamp", "time_range", "scene", "timeline_item", "caption", "asset"
    ]
    target_id: str = Field(min_length=1, max_length=36)
    body: str = Field(min_length=1, max_length=8000)
    anchor: dict | None = None
    parent_id: str | None = None
    mentions: list[str] = Field(default_factory=list)
    project_id: str | None = None


@comments_router.post("", status_code=201, summary="Add a comment (or a thread reply)")
@_guard()
def add_comment(
    body: CommentCreateBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a comment. Floor viewer + project ``comment`` cap when scoped."""
    kwargs: dict[str, Any] = (
        {"project_id": body.project_id}
        if body.project_id
        else {"target_type": body.target_type, "target_id": body.target_id}
    )
    _check(db, ws, user, capability="comment", **kwargs)
    try:
        return engine.add_comment(
            db, ws,
            target_type=body.target_type,
            target_id=body.target_id,
            body=body.body,
            author=user.id,
            anchor=body.anchor,
            parent_id=body.parent_id,
            mentions=body.mentions,
            project_id=body.project_id,
        )
    except engine.CommentNotFoundError:
        # foreign target or foreign parent comment id -> 404 (never 500/403)
        raise HTTPException(status_code=404, detail="comment not found") from None


@comments_router.get("", summary="List comments for a target")
@_guard()
def list_comments(
    target_type: str = Query(...),
    target_id: str = Query(..., min_length=1, max_length=36),
    include_resolved: bool = Query(default=False),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        items = engine.list_comments(
            db, ws, target_type=target_type, target_id=target_id,
            include_resolved=include_resolved,
        )
    except engine.CommentNotFoundError:
        raise HTTPException(status_code=404, detail="target not found") from None
    return {"items": items}


def _require_root_for_write(db: Session, ws: Workspace, comment_id: str):
    try:
        return engine.get_comment(db, ws, comment_id)
    except engine.CommentNotFoundError:
        raise HTTPException(status_code=404, detail="comment not found") from None


@comments_router.post("/{comment_id}/resolve", summary="Resolve a ROOT comment")
@_guard()
def resolve_comment(
    comment_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Resolve a root comment (author or a ``comment``-cap holder may resolve).

    Does NOT touch the target's content.
    """
    row = _require_root_for_write(db, ws, comment_id)
    if row.author_id != user.id:
        kwargs: dict[str, Any] = (
            {"project_id": row.project_id}
            if row.project_id
            else {"target_type": row.target_type, "target_id": row.target_id}
        )
        _check(db, ws, user, capability="comment", **kwargs)
    return engine.resolve_comment(db, ws, comment_id, user=user.id)


@comments_router.post("/{comment_id}/reopen", summary="Reopen a ROOT comment")
@_guard()
def reopen_comment(
    comment_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Reopen a root comment (author or a ``comment``-cap holder may reopen)."""
    row = _require_root_for_write(db, ws, comment_id)
    if row.author_id != user.id:
        kwargs: dict[str, Any] = (
            {"project_id": row.project_id}
            if row.project_id
            else {"target_type": row.target_type, "target_id": row.target_id}
        )
        _check(db, ws, user, capability="comment", **kwargs)
    return engine.reopen_comment(db, ws, comment_id, user=user.id)
