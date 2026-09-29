"""Revision requests: an explicit OPEN -> ADDRESSED | DISMISSED lifecycle
(Work 11 Lane R) -- contracts §7.

A revision request is the reviewer's *ask list* attached to a target (usually
one opened automatically by a REQUEST_CHANGES decision). Each row carries an
``items_json`` payload::

    [{kind, description, anchor}, ...]

where ``kind`` is one of ``trim | timing | asset_swap | text | voice | caption
| brand | other`` and ``description`` is non-empty (both validated -> 422 by the
API; the engine raises ``ValueError``).

**NO auto-transition (contracts §7).** Editing a timeline NEVER flips
``OPEN -> ADDRESSED``. The whole point of the feature is that a human reviewer
decides whether a change actually addressed the ask -- inferring "fixed" from
"the file changed" is exactly the false-positive this lifecycle exists to avoid.
State moves only through :func:`set_state`; ``ADDRESSED`` records
``resolved_by``/``resolved_at``.

Engine layer: no FastAPI. ``ValueError`` -> 422 at the route edge; a revision
row from another workspace reads as missing (the API answers 404).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RevisionRequest, Workspace
from app.models.base import utcnow

logger = logging.getLogger("ymoney.collab")

# revision item kinds (contracts §7, verbatim)
ITEM_KINDS: tuple[str, ...] = (
    "trim", "timing", "asset_swap", "text", "voice", "caption", "brand", "other",
)

# revision states
OPEN = "OPEN"
ADDRESSED = "ADDRESSED"
DISMISSED = "DISMISSED"
REVISION_STATES: tuple[str, ...] = (OPEN, ADDRESSED, DISMISSED)

# legal explicit transitions (contracts §7). Nothing moves on its own.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    OPEN: frozenset({ADDRESSED, DISMISSED}),
    ADDRESSED: frozenset({OPEN}),          # reopen
    DISMISSED: frozenset({OPEN}),          # reopen
}

MAX_ITEMS = 100
MAX_DESCRIPTION = 2000


class RevisionNotFoundError(LookupError):
    """Revision id not in this workspace (routes answer 404)."""


class RevisionTransitionError(ValueError):
    """Illegal revision state move (routes answer 409)."""


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


def _validate_item(raw: Any, index: int) -> dict:
    """Normalize one ask item; ValueError -> 422 (kind enum + description)."""
    if not isinstance(raw, dict):
        raise ValueError(f"item {index} must be an object")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ITEM_KINDS:
        raise ValueError(
            f"item {index} kind must be one of {', '.join(ITEM_KINDS)}"
        )
    description = " ".join(str(raw.get("description") or "").split())
    if not description:
        raise ValueError(f"item {index} description is required")
    anchor = raw.get("anchor")
    if anchor is not None and not isinstance(anchor, dict):
        raise ValueError(f"item {index} anchor must be an object")
    return {
        "kind": kind,
        "description": description[:MAX_DESCRIPTION],
        "anchor": dict(anchor or {}),
    }


def _emit(ws_id: str, kind: str, message: str, *, actor: str, revision: RevisionRequest,
          **extra: Any) -> None:
    """Activity ledger event (contracts §9)."""
    from app.services.events import record_event

    data: dict[str, Any] = {
        "actor": actor,
        "target": {"type": revision.target_type, "id": revision.target_id},
        "project_id": revision.project_id,
        "review_id": revision.review_id,
        "revision_id": revision.id,
    }
    data.update(extra)
    record_event(ws_id, kind, message, level="info", source="collab", data=data)


def get_revision(db: Session, ws: Workspace, revision_id: str) -> RevisionRequest:
    """Workspace-scoped fetch; foreign/missing ids raise (routes answer 404)."""
    row = db.get(RevisionRequest, str(revision_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise RevisionNotFoundError(revision_id)
    return row


def revision_to_dict(row: RevisionRequest) -> dict:
    """Public revision shape (used by the revisions API + review detail)."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "project_id": row.project_id,
        "review_id": row.review_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "state": row.state,
        "items": list(row.items_json or []),
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "resolved_at": _iso(row.resolved_at),
        "resolved_by": row.resolved_by,
    }


def create_revisions(
    db: Session,
    ws: Workspace,
    *,
    items: list[dict],
    target_type: str,
    target_id: str,
    created_by: str,
    review_id: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Open one OPEN revision request carrying the validated ask list.

    Validates every item (kind enum + non-empty description) BEFORE writing, so
    a bad item never leaves a half-created row behind.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"too many items (max {MAX_ITEMS})")
    normalized = [_validate_item(raw, i) for i, raw in enumerate(items)]
    row = RevisionRequest(
        workspace_id=ws.id,
        project_id=project_id,
        review_id=review_id,
        target_type=target_type,
        target_id=str(target_id),
        state=OPEN,
        items_json=normalized,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _emit(ws.id, "REVISION_REQUESTED", f"Revision request opened ({len(normalized)} item(s))",
          actor=created_by, revision=row, item_count=len(normalized))
    return [revision_to_dict(row)]


def set_state(db: Session, ws: Workspace, revision_id: str, *, state: str, user: str) -> dict:
    """Explicit state move (the ONLY way a revision changes state).

    ``OPEN -> ADDRESSED`` records ``resolved_by``/``resolved_at``; reopening
    (``ADDRESSED|DISMISSED -> OPEN``) clears them. Illegal moves raise
    :class:`RevisionTransitionError` (routes answer 409).
    """
    target_state = str(state or "").strip().upper()
    if target_state not in REVISION_STATES:
        raise ValueError(f"unknown revision state {state!r}")
    row = get_revision(db, ws, revision_id)
    if target_state == row.state:
        return revision_to_dict(row)  # idempotent re-entry is a no-op
    allowed = ALLOWED_TRANSITIONS.get(row.state, frozenset())
    if target_state not in allowed:
        raise RevisionTransitionError(
            f"illegal revision transition {row.state} -> {target_state}"
        )
    previous = row.state
    row.state = target_state
    if target_state == ADDRESSED:
        row.resolved_at = utcnow()
        row.resolved_by = user
    else:  # reopened to OPEN
        row.resolved_at = None
        row.resolved_by = None
    db.commit()
    db.refresh(row)
    _emit(ws_id=ws.id, kind="REVISION_UPDATED",
          message=f"Revision {previous} -> {target_state}",
          actor=user, revision=row, previous_state=previous, state=target_state)
    return revision_to_dict(row)


def list_revisions(
    db: Session,
    ws: Workspace,
    *,
    state: str | None = None,
    target_id: str | None = None,
    target_type: str | None = None,
    review_id: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """List workspace revisions (newest first). Read-only -- never mutates."""
    q = select(RevisionRequest).where(RevisionRequest.workspace_id == ws.id)
    if state:
        q = q.where(RevisionRequest.state == str(state).upper())
    if target_id:
        q = q.where(RevisionRequest.target_id == target_id)
    if target_type:
        q = q.where(RevisionRequest.target_type == target_type)
    if review_id:
        q = q.where(RevisionRequest.review_id == review_id)
    if project_id:
        q = q.where(RevisionRequest.project_id == project_id)
    rows = db.scalars(q.order_by(RevisionRequest.created_at.desc())).all()
    return [revision_to_dict(row) for row in rows]
