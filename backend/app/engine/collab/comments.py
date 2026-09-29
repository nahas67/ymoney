"""Anchored, threaded comments that NEVER mutate content (Work 11 Lane R) --
contracts §6.

A comment is a discussion entry pinned to a target
(``timeline | timestamp | time_range | scene | timeline_item | caption | asset``)
with an optional anchor. The hard rule: **writing or resolving a comment never
touches the target's content.** ``version_ref`` records WHICH timeline version
was on screen when the comment was written, purely as context -- it is never a
handle for a write. A test asserts ``tracks_json`` bytes are unchanged after
add/resolve.

Anchors:
  * ``timestamp``  -> requires ``t_start`` (float seconds, >= 0)
  * ``time_range`` -> requires ``t_start`` AND ``t_end`` with ``t_end > t_start``
  Invalid anchors raise ``ValueError`` (routes answer 422).

Threads: replies carry ``parent_id`` and MUST target the same
``(target_type, target_id)`` as the parent (else ``ValueError`` -> 422). The
thread is FLAT under the root: a reply to a reply is re-parented onto the root,
so depth is always exactly 1 (the accepted choice per contracts §6). Only ROOT
comments can be resolved/reopened; resolving a reply is a no-op refusal.

Mentions: every mentioned user must be a workspace member, else
``ValueError`` -> 422. Mentioned users are stored in ``mentions_json``.

Engine layer: no FastAPI. Comments from another workspace read as missing (the
API answers 404).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Comment, Workspace, WorkspaceMember
from app.models.base import utcnow

logger = logging.getLogger("ymoney.collab")

# comment target vocabulary (contracts §6)
TARGET_TYPES: tuple[str, ...] = (
    "timeline", "timestamp", "time_range", "scene", "timeline_item", "caption", "asset",
)
# targets whose anchor must carry a playhead position
TIME_TARGETS: frozenset[str] = frozenset({"timestamp", "time_range"})

MAX_MENTIONS = 50
MAX_BODY_LEN = 8000


class CommentNotFoundError(LookupError):
    """Comment id not in this workspace (routes answer 404)."""


def _ensure_target_visible(db: Session, ws: Workspace,
                           target_type: str, target_id: str) -> None:
    """A target that EXISTS in another workspace reads as missing (contracts §6:
    cross-ws target -> 404, never a leak). Unknown / virtual targets (timestamp,
    time_range, timeline_item, caption -- no workspace-scoped row) stay
    permissive; the DB-backed types resolve their row and 404 when foreign."""
    from app.models import ContentTimeline, MediaAsset, Scene

    model = {"timeline": ContentTimeline, "asset": MediaAsset,
             "scene": Scene}.get(target_type)
    if model is None:
        return
    row = db.get(model, str(target_id or ""))
    if row is not None and getattr(row, "workspace_id", None) != ws.id:
        raise CommentNotFoundError(target_id)


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


def _emit(ws_id: str, kind: str, message: str, *, actor: str, comment: Comment,
          **extra: Any) -> None:
    """Activity ledger event (contracts §9) + best-effort inbox fan-out.

    The same ``(kind, data)`` pair feeds lane L's notification service, which
    resolves mention recipients from ``mentions``/``comment_id``. The import is
    lazy + guarded: a missing or broken service degrades to "event only".
    """
    from app.services.events import record_event

    data: dict[str, Any] = {
        "actor": actor,
        "target": {"type": comment.target_type, "id": comment.target_id},
        "version": comment.version_ref,
        "project_id": comment.project_id,
        "comment_id": comment.id,
        "parent_id": comment.parent_id,
    }
    data.update(extra)
    record_event(ws_id, kind, message, level="info", source="collab", data=data)
    _notify(ws_id, kind, data)


def _notify(ws_id: str, kind: str, data: dict) -> None:
    """Best-effort notification fan-out (lane L owns the service).

    Tries the contract's ``notify_for_event`` first, then the name lane L
    actually shipped (``on_event``). Never raises.
    """
    try:  # pragma: no cover -- depends on lane L landing
        from app.services import notifications as notif
    except ImportError:
        logger.debug("notifications service unavailable; event-only for %s", kind)
        return
    hook = getattr(notif, "notify_for_event", None) or getattr(notif, "on_event", None)
    if hook is None:  # pragma: no cover -- service present but no entry point
        return
    try:  # pragma: no cover -- telemetry must never break the caller
        from app.db import session_scope

        with session_scope() as s:
            hook(s, ws_id, kind, data)
    except Exception:  # noqa: BLE001
        logger.exception("notification fan-out failed for %s", kind)


def _validate_anchor(target_type: str, anchor: dict | None) -> dict:
    """Validate + normalize the anchor for a target (ValueError -> 422).

    ``timestamp`` requires ``t_start``; ``time_range`` additionally requires
    ``t_end`` strictly greater than ``t_start``. All values are finite,
    non-negative seconds. Other anchor keys (scene_id/item_id/caption_id/
    asset_id) are copied through untouched.
    """
    cleaned = dict(anchor or {})
    if target_type in TIME_TARGETS:
        if "t_start" not in cleaned or cleaned.get("t_start") is None:
            raise ValueError(f"anchor for '{target_type}' requires t_start")
        try:
            t_start = float(cleaned["t_start"])
        except (TypeError, ValueError):
            raise ValueError("anchor t_start must be a number") from None
        if t_start < 0:
            raise ValueError("anchor t_start must be >= 0")
        cleaned["t_start"] = t_start
        if target_type == "time_range":
            if cleaned.get("t_end") is None:
                raise ValueError("anchor for 'time_range' requires t_end")
            try:
                t_end = float(cleaned["t_end"])
            except (TypeError, ValueError):
                raise ValueError("anchor t_end must be a number") from None
            if t_end <= t_start:
                raise ValueError("anchor t_end must be greater than t_start")
            cleaned["t_end"] = t_end
    return cleaned


def _validate_mentions(db: Session, ws: Workspace, mentions: list[str] | None) -> list[str]:
    """Every mentioned user must be a workspace member (else ValueError -> 422)."""
    if not mentions:
        return []
    if len(mentions) > MAX_MENTIONS:
        raise ValueError(f"too many mentions (max {MAX_MENTIONS})")
    seen: list[str] = []
    for raw in mentions:
        user_id = str(raw or "").strip()
        if not user_id or user_id in seen:
            continue
        member = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ws.id,
                WorkspaceMember.user_id == user_id,
            )
        )
        if member is None:
            raise ValueError("mentions must be workspace members")
        seen.append(user_id)
    return seen


def get_comment(db: Session, ws: Workspace, comment_id: str) -> Comment:
    """Workspace-scoped fetch; foreign/missing ids raise (routes answer 404)."""
    row = db.get(Comment, str(comment_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise CommentNotFoundError(comment_id)
    return row


def comment_to_dict(row: Comment) -> dict:
    """Public comment shape (used by the comments API)."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "project_id": row.project_id,
        "parent_id": row.parent_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "anchor": dict(row.anchor_json or {}),
        "body": str(row.body or ""),
        "author_id": row.author_id,
        "mentions": list(row.mentions_json or []),
        "version_ref": row.version_ref,
        "resolved_at": _iso(row.resolved_at),
        "resolved_by": row.resolved_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _timeline_version_ref(db: Session, target_type: str, target_id: str) -> str | None:
    """Version of the target's current tip -- context only, never a write handle.

    Recorded so a reader can see WHICH version a comment was written against.
    Deliberately read-only: nothing in this module writes timeline rows.
    """
    from app.engine.timeline import tip_version

    if target_type == "timeline":
        tip = tip_version(db, str(target_id))
        return str(tip.version or "") if tip is not None else None
    return None


def add_comment(
    db: Session,
    ws: Workspace,
    *,
    target_type: str,
    target_id: str,
    body: str,
    author: str,
    anchor: dict | None = None,
    parent_id: str | None = None,
    mentions: list[str] | None = None,
    project_id: str | None = None,
) -> dict:
    """Add a comment (or a flat reply under a root comment).

    Writes ONLY the ``comments`` row -- the target's content is never touched.
    A reply must target the same ``(target_type, target_id)`` as its parent, and
    threads are flattened so ``parent_id`` always points at a root.
    """
    if target_type not in TARGET_TYPES:
        raise ValueError(f"unknown comment target_type {target_type!r}")
    _ensure_target_visible(db, ws, target_type, target_id)
    text = " ".join(str(body or "").split())
    if not text:
        raise ValueError("comment body is required")
    if len(text) > MAX_BODY_LEN:
        raise ValueError(f"comment body too long (max {MAX_BODY_LEN})")
    cleaned_anchor = _validate_anchor(target_type, anchor)
    cleaned_mentions = _validate_mentions(db, ws, mentions)

    root_id: str | None = None
    if parent_id:
        parent = get_comment(db, ws, parent_id)
        if parent.target_type != target_type or parent.target_id != str(target_id):
            raise ValueError("reply must target the same target as its parent")
        # flatten: a reply to a reply hangs off the ROOT (depth is always 1)
        root_id = parent.parent_id or parent.id

    row = Comment(
        workspace_id=ws.id,
        project_id=project_id,
        parent_id=root_id,
        target_type=target_type,
        target_id=str(target_id),
        anchor_json=cleaned_anchor,
        body=text,
        author_id=author,
        mentions_json=cleaned_mentions,
        version_ref=_timeline_version_ref(db, target_type, target_id),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _emit(ws.id, "COMMENT_ADDED", f"Comment on {target_type}",
          actor=author, comment=row, mentions=list(cleaned_mentions))
    return comment_to_dict(row)


def list_comments(
    db: Session,
    ws: Workspace,
    *,
    target_type: str,
    target_id: str,
    include_resolved: bool = False,
) -> list[dict]:
    """List comments for one target in this workspace, oldest first (thread order)."""
    _ensure_target_visible(db, ws, target_type, target_id)
    q = select(Comment).where(
        Comment.workspace_id == ws.id,
        Comment.target_type == target_type,
        Comment.target_id == str(target_id),
    )
    if not include_resolved:
        q = q.where(Comment.resolved_at.is_(None))
    rows = db.scalars(q.order_by(Comment.created_at)).all()
    return [comment_to_dict(row) for row in rows]


def _require_root(db: Session, ws: Workspace, comment_id: str) -> Comment:
    row = get_comment(db, ws, comment_id)
    if row.parent_id:
        raise ValueError("only root comments can be resolved or reopened")
    return row


def resolve_comment(db: Session, ws: Workspace, comment_id: str, *, user: str) -> dict:
    """Mark a ROOT comment resolved (sets resolved_at/resolved_by).

    Does NOT touch the comment's target content -- resolution is bookkeeping.
    """
    row = _require_root(db, ws, comment_id)
    row.resolved_at = row.resolved_at or utcnow()
    row.resolved_by = row.resolved_by or user
    db.commit()
    db.refresh(row)
    _emit(ws.id, "COMMENT_RESOLVED", "Comment resolved", actor=user, comment=row)
    return comment_to_dict(row)


def reopen_comment(db: Session, ws: Workspace, comment_id: str, *, user: str) -> dict:
    """Reopen a ROOT comment (clears resolved_at/resolved_by)."""
    row = _require_root(db, ws, comment_id)
    row.resolved_at = None
    row.resolved_by = None
    db.commit()
    db.refresh(row)
    _emit(ws.id, "COMMENT_REOPENED", "Comment reopened", actor=user, comment=row)
    return comment_to_dict(row)
