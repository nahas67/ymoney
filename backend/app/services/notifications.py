"""Internal notifications (Work 11 Lane L) -- contracts §10.

One row per (workspace, user, kind) in the append-only ``notifications``
table. Rows are written ONLY through :func:`notify` (direct) or
:func:`on_event` (event fan-out), and are only ever *marked* read -- the
ledger and the inbox are both append-only histories.

Event -> notification mapping (``on_event``)::

    REVIEW_ASSIGNED   -> review.assigned         review creator + assignees
                                                  + target owner
    CHANGES_REQUESTED -> review.changes_requested  same recipient set
    APPROVED          -> review.approved           same recipient set
    COMMENT_ADDED     -> comment.mention           mentioned users
    EXPORT_COMPLETED  -> export.completed          job creator
    EXPORT_FAILED     -> export.failed             job creator

Wiring contract (contracts §6, final decision): ``record_event`` is the
single emission primitive for every lane; this module is the SUBSCRIBER
side. Lanes R and X call :func:`on_event` from their own emission points
through a lazy ``try/except ImportError`` import, so a missing sibling can
never break an emission. Nothing here imports a sibling lane's engine, so
the mapping + recipient logic is testable on its own.

Self-notification is suppressed (the actor is never notified about their
own action) and unknown kinds are a no-op that returns 0.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    Comment,
    ExportJob,
    Notification,
    Review,
    ReviewAssignment,
    WorkspaceMember,
)

logger = logging.getLogger("ymoney.collab")

#: notification kinds (contracts §10) -- the closed vocabulary.
KIND_REVIEW_ASSIGNED = "review.assigned"
KIND_COMMENT_MENTION = "comment.mention"
KIND_CHANGES_REQUESTED = "review.changes_requested"
KIND_REVIEW_APPROVED = "review.approved"
KIND_EXPORT_COMPLETED = "export.completed"
KIND_EXPORT_FAILED = "export.failed"

NOTIFICATION_KINDS: tuple[str, ...] = (
    KIND_REVIEW_ASSIGNED,
    KIND_COMMENT_MENTION,
    KIND_CHANGES_REQUESTED,
    KIND_REVIEW_APPROVED,
    KIND_EXPORT_COMPLETED,
    KIND_EXPORT_FAILED,
)

#: ledger event kind -> notification kind (contracts §9 + §10).
EVENT_KIND_MAP: dict[str, str] = {
    "REVIEW_ASSIGNED": KIND_REVIEW_ASSIGNED,
    "CHANGES_REQUESTED": KIND_CHANGES_REQUESTED,
    "APPROVED": KIND_REVIEW_APPROVED,
    "COMMENT_ADDED": KIND_COMMENT_MENTION,
    "EXPORT_COMPLETED": KIND_EXPORT_COMPLETED,
    "EXPORT_FAILED": KIND_EXPORT_FAILED,
}

#: event kinds whose recipients are the review-side trio.
_REVIEW_EVENTS = frozenset(
    {"REVIEW_ASSIGNED", "CHANGES_REQUESTED", "APPROVED"}
)
_EXPORT_EVENTS = frozenset({"EXPORT_COMPLETED", "EXPORT_FAILED"})

#: notification kind -> ledger event kind (the reverse of EVENT_KIND_MAP).
#: Emitters speak two dialects: lanes R/press fan out the ledger kind
#: (``EXPORT_COMPLETED``) while lane X's exporter fans out the
#: notification kind (``export.completed``). Both name the same recipient
#: set, so the dialect must never be able to drop an inbox row silently.
_NOTIFICATION_KIND_TO_EVENT: dict[str, str] = {
    notification: event for event, notification in EVENT_KIND_MAP.items()
}


def canonical_event_kind(kind: str) -> str:
    """Normalize a fan-out kind to its ledger spelling (unknown -> unchanged)."""
    if kind in EVENT_KIND_MAP:
        return kind
    return _NOTIFICATION_KIND_TO_EVENT.get(kind, kind)


MAX_LIMIT = 200
DEFAULT_LIMIT = 50


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def notification_dto(row: Notification) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "user_id": row.user_id,
        "kind": row.kind,
        "payload": dict(row.payload_json or {}),
        "read_at": (row.read_at.isoformat() + "Z") if row.read_at else None,
        "read": row.read_at is not None,
        "created_at": (row.created_at.isoformat() + "Z") if row.created_at else "",
    }


def _as_ids(value: Any) -> list[str]:
    """Coerce a mentions/assignees field into a de-duplicated id list."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _review_recipients(db: Session, workspace_id: str, data: dict) -> list[str]:
    """Review creator + every assignee + the target's owner.

    The review row is the source of truth; the event payload only
    supplies the ``review_id`` hint. The row is resolved WITH the
    workspace filter, so a review id belonging to another workspace
    resolves to nothing and contributes no recipients.
    """
    out: list[str] = []
    review_id = str(data.get("review_id") or "")
    if review_id:
        review = db.scalar(
            select(Review).where(
                Review.id == review_id, Review.workspace_id == workspace_id
            )
        )
        if review is not None:
            out.append(review.created_by)
            out.extend(
                row.user_id
                for row in db.scalars(
                    select(ReviewAssignment).where(
                        ReviewAssignment.review_id == review.id
                    )
                ).all()
            )
    out.extend(_as_ids(data.get("assignee_user_id")))
    out.extend(_as_ids(data.get("assignee_user_ids")))
    out.extend(_as_ids(data.get("reviewer_user_id")))
    target = data.get("target") or {}
    if str(target.get("type") or "") == "user":
        out.extend(_as_ids(target.get("id")))
    return out


def _mentioned_recipients(db: Session, workspace_id: str, data: dict) -> list[str]:
    """Users named by a comment. Falls back to the comment row's own
    ``mentions_json`` when the event payload omits them (lane R may
    include only the comment id). The fallback lookup is workspace-scoped
    like every other row read here."""
    out = _as_ids(data.get("mentions")) + _as_ids(data.get("mentioned_user_ids"))
    comment_id = str(data.get("comment_id") or "")
    if not out and comment_id:
        row = db.scalar(
            select(Comment).where(
                Comment.id == comment_id, Comment.workspace_id == workspace_id
            )
        )
        if row is not None:
            out = _as_ids(row.mentions_json)
    return out


def _export_recipients(db: Session, workspace_id: str, data: dict) -> list[str]:
    """The user who queued the export (workspace-scoped row read)."""
    out = _as_ids(data.get("created_by")) + _as_ids(data.get("user_id"))
    export_id = str(data.get("export_id") or "")
    if export_id:
        job = db.scalar(
            select(ExportJob).where(
                ExportJob.id == export_id, ExportJob.workspace_id == workspace_id
            )
        )
        if job is not None:
            out.append(job.created_by)
    return out


def _workspace_user_ids(db: Session, workspace_id: str) -> set[str]:
    """User ids that are members of THIS workspace.

    This is the authorization boundary for an inbox row: a user who is
    not a member here has no business holding a notification for this
    workspace, whichever id an event payload happens to name. It also
    subsumes the "does this user exist" check -- a member is a row in
    ``users`` by construction.
    """
    return {
        str(value)
        for value in db.scalars(
            select(WorkspaceMember.user_id).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        ).all()
    }


def resolve_recipients(
    db: Session, workspace_id: str, kind: str, data: dict
) -> list[str]:
    """Recipient user ids for one ledger event (de-duplicated, in order).

    Every candidate is filtered to a member of ``workspace_id``, so an
    event payload naming a foreign user cannot create a cross-workspace
    inbox row (the repo's hard isolation rule, applied to the write side
    as well as the read side).
    """
    kind = canonical_event_kind(str(kind or ""))
    if kind in _REVIEW_EVENTS:
        candidates = _review_recipients(db, workspace_id, data)
    elif kind == "COMMENT_ADDED":
        candidates = _mentioned_recipients(db, workspace_id, data)
    elif kind in _EXPORT_EVENTS:
        candidates = _export_recipients(db, workspace_id, data)
    else:
        return []

    # Contracts 10 addresses export receipts to the JOB CREATOR, and lane
    # X's fan-out sets ``actor = created_by`` -- self-suppression would
    # silence exactly the one recipient the contract names, so it applies
    # to review/mention actions only, never to an export receipt.
    actor = "" if kind in _EXPORT_EVENTS else str(data.get("actor") or "")
    members = _workspace_user_ids(db, workspace_id)
    out: list[str] = []
    for user_id in candidates:
        if not user_id or user_id == actor:
            continue  # never notify the actor about their own action
        if user_id not in members:
            continue  # non-member / deleted / foreign -> no inbox row
        if user_id not in out:
            out.append(user_id)
    return out


# ---------------------------------------------------------------------------
# write side (append-only)
# ---------------------------------------------------------------------------


def notify(
    db: Session,
    workspace_id: str,
    user_id: str,
    kind: str,
    payload: dict | None = None,
) -> dict:
    """Append one inbox row and return its DTO.

    Raises ``ValueError`` for a kind outside the closed vocabulary so a
    typo can never create an unreadable inbox entry (routes map that to
    422). Does NOT commit -- the caller's session owns the transaction.
    """
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"unknown notification kind {kind!r}")
    ws_id = str(workspace_id or "").strip()
    target_user = str(user_id or "").strip()
    if not ws_id or not target_user:
        raise ValueError("workspace_id and user_id are required")
    row = Notification(
        workspace_id=ws_id,
        user_id=target_user,
        kind=kind,
        payload_json=dict(payload or {}),
    )
    db.add(row)
    db.flush()
    return notification_dto(row)


def on_event(db: Session, workspace_id: str, kind: str, data: dict | None = None) -> int:
    """Fan one ledger event out to its recipient set. Returns rows created.

    Never raises for a data problem: a notification is an inbox row, not
    a correctness requirement for the action that triggered it, so
    unknown kinds / unresolvable recipients return 0 and genuine faults
    are logged rather than propagated into the caller's transaction.
    """
    payload = dict(data or {})
    kind = canonical_event_kind(str(kind or ""))
    notification_kind = EVENT_KIND_MAP.get(kind)
    if notification_kind is None:
        return 0
    try:
        recipients = resolve_recipients(db, str(workspace_id or ""), kind, payload)
    except Exception:  # noqa: BLE001 -- a lookup must not break the emitter
        logger.exception("notification recipient resolution failed: kind=%s", kind)
        return 0
    created = 0
    for user_id in recipients:
        try:
            notify(db, workspace_id, user_id, notification_kind, payload)
        except Exception:  # noqa: BLE001 -- one bad row never blocks the rest
            logger.exception("notification write failed: kind=%s", kind)
            continue
        created += 1
    if created:
        logger.debug("notifications created: kind=%s count=%d", kind, created)
    return created


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------


def list_notifications(
    db: Session,
    workspace_id: str,
    user_id: str,
    *,
    unread_only: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """The user's OWN rows, newest first. Never another user's inbox."""
    stmt = select(Notification).where(
        Notification.workspace_id == workspace_id,
        Notification.user_id == user_id,
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    capped = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    # Tie-break on the insert order: two rows can share a timestamp (see
    # services/activity.py::newest_first_tiebreak for the Windows clock
    # granularity reason), and a random uuid must not reorder the feed.
    tiebreak = (
        text("rowid DESC")
        if db.get_bind().dialect.name == "sqlite"
        else Notification.id.desc()
    )
    stmt = stmt.order_by(Notification.created_at.desc(), tiebreak).limit(capped)
    return [notification_dto(row) for row in db.scalars(stmt).all()]


def unread_count(db: Session, workspace_id: str, user_id: str) -> int:
    from sqlalchemy import func

    total = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.workspace_id == workspace_id,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
    return int(total or 0)


def mark_read(
    db: Session,
    workspace_id: str,
    user_id: str,
    notification_id: str | None = None,
    *,
    all: bool = False,
) -> int:
    """Mark one row (or the whole inbox) read. Returns rows CHANGED.

    Idempotent by design: a notification that is already read is still
    found, it simply contributes 0 to the count -- so a double-click or
    a retried request is a 200, not a 404. ``notification_exists`` is the
    separate question "is this row mine?", which is what the route turns
    into 404.

    A foreign/unknown id matches nothing -- a notification belonging to
    another workspace or another user is simply not in the filter, so the
    caller cannot learn that it exists.
    """
    from app.models.base import utcnow

    scope = select(Notification).where(
        Notification.workspace_id == workspace_id,
        Notification.user_id == user_id,
    )
    if not all:
        scope = scope.where(Notification.id == str(notification_id or ""))
    rows = db.scalars(scope).all()
    pending = [row for row in rows if row.read_at is None]
    now = utcnow()
    for row in pending:
        row.read_at = now
    if pending:
        db.flush()
    return len(pending)


def notification_exists(
    db: Session,
    workspace_id: str,
    user_id: str,
    notification_id: str,
) -> bool:
    """Is this notification id one of THIS user's rows?"""
    return (
        db.scalar(
            select(Notification.id).where(
                Notification.id == str(notification_id or ""),
                Notification.workspace_id == workspace_id,
                Notification.user_id == user_id,
            )
        )
        is not None
    )


__all__ = [
    "EVENT_KIND_MAP",
    "KIND_CHANGES_REQUESTED",
    "KIND_COMMENT_MENTION",
    "KIND_EXPORT_COMPLETED",
    "KIND_EXPORT_FAILED",
    "KIND_REVIEW_ASSIGNED",
    "KIND_REVIEW_APPROVED",
    "NOTIFICATION_KINDS",
    "list_notifications",
    "mark_read",
    "notification_dto",
    "notification_exists",
    "notify",
    "on_event",
    "resolve_recipients",
    "unread_count",
]
