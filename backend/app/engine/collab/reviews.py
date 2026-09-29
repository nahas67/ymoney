"""Version-bound review threads (Work 11 Lane R) -- contracts §5.

One review is one target under review, bound to the EXACT version that was on
the table when the review was requested. Approval is therefore never
"last-write-wins":

  * **Exact-version binding** -- for versioned targets (``timeline_version`` and
    ``content`` whose current timeline is the subject) creation captures
    ``bound_version = str(tip.version)`` and
    ``bound_manifest_hash = manifest_hash_of(tip.tracks_json)`` using lane D's
    canonical resolvers (``engine/timeline.py::tip_version`` /
    ``manifest_hash_of`` -- never re-derived here). Non-versioned targets bind
    NULL and their ``stale`` flag is informational only.
  * **Approve re-verifies the binding** -- ``record_decision("APPROVE")``
    recomputes the current tip hash; a mismatch raises ``StaleReviewError``,
    which routes answer as 409 with ``stale: true``. The review STAYS
    IN_REVIEW (flagged stale) -- an approver must re-request on the new
    version rather than silently inheriting the old approval intent.
  * **Staleness on edit** -- ``refresh_staleness`` recomputes tip != bound and
    runs lazily on every review GET/list. The APPROVED state is never deleted
    (history is append-only); consumers must read ``approval_valid``, which is
    ``state == APPROVED and not stale``.

State machine (contracts §5)::

    DRAFT            -> IN_REVIEW
    IN_REVIEW        -> APPROVED | REJECTED | CHANGES_REQUESTED | CANCELLED
    CHANGES_REQUESTED-> IN_REVIEW          (re-request rebinds the version)
    APPROVED | REJECTED | CANCELLED        terminal

Illegal transitions raise :class:`ReviewTransitionError` (routes answer 409).
Every transition appends an activity event (contracts §9 kinds) through
``record_event``; actor/target/version/project_id ride in ``data``.

Engine layer: no FastAPI, no HTTPException. Domain errors are typed
(``ReviewTransitionError`` / ``StaleReviewError`` / ``ValueError``) and the API
maps them to 409 / 409+stale / 422. Workspace scoping is enforced by every
query here -- a foreign review id reads as missing, never as a leak.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine import timeline as tl
from app.engine.timeline import TimelineValidationError
from app.models import (
    ContentTimeline,
    Review,
    ReviewAssignment,
    ReviewDecision,
    RevisionRequest,
    Workspace,
)
from app.models.base import utcnow

logger = logging.getLogger("ymoney.collab")

# review target vocabulary (contracts §5). VERSIONED_TARGETS bind an exact
# version; the rest bind NULL and treat staleness as informational only.
TARGET_TYPES: tuple[str, ...] = (
    "project",
    "content",
    "timeline_version",
    "campaign",
    "localization",
    "ugc_asset",
)
VERSIONED_TARGET_TYPES: frozenset[str] = frozenset({"timeline_version", "content"})

# review states
DRAFT = "DRAFT"
IN_REVIEW = "IN_REVIEW"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
REVIEW_STATES: tuple[str, ...] = (
    DRAFT, IN_REVIEW, CHANGES_REQUESTED, APPROVED, REJECTED, CANCELLED,
)
TERMINAL_STATES: frozenset[str] = frozenset({APPROVED, REJECTED, CANCELLED})

# legal transitions (contracts §5). CHANGES_REQUESTED -> IN_REVIEW is the
# re-request path: it rebinds bound_version/bound_manifest_hash to the tip.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # contracts §5 enumerates exactly these edges: DRAFT -> IN_REVIEW only;
    # cancelling is an IN_REVIEW action (DRAFT -> CANCELLED is 409).
    DRAFT: frozenset({IN_REVIEW}),
    IN_REVIEW: frozenset({APPROVED, REJECTED, CHANGES_REQUESTED, CANCELLED}),
    CHANGES_REQUESTED: frozenset({IN_REVIEW}),
    APPROVED: frozenset(),
    REJECTED: frozenset(),
    CANCELLED: frozenset(),
}

# decision vocabulary -> the review state it drives
DECISIONS: dict[str, str] = {
    "APPROVE": APPROVED,
    "REJECT": REJECTED,
    "REQUEST_CHANGES": CHANGES_REQUESTED,
}


class ReviewTransitionError(ValueError):
    """Illegal state-machine move (routes answer 409 with a short detail)."""


class StaleReviewError(ValueError):
    """APPROVE on a review whose bound target moved (routes answer 409 + stale).

    Carries the review id and the recomputed tip so the API can attach
    ``stale: true`` to the 409 body without re-querying.
    """

    def __init__(self, message: str, *, review_id: str, tip_version: str | None = None) -> None:
        super().__init__(message)
        self.review_id = review_id
        self.tip_version = tip_version


class ReviewNotFoundError(LookupError):
    """Review id not in this workspace (routes answer 404, never 403)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


def subject_timeline(db: Session, target_type: str, target_id: str) -> ContentTimeline | None:
    """The timeline row whose manifest this target binds to (None if none).

    ``timeline_version`` -> the target IS the timeline; ``content`` -> the
    content item's current timeline (highest version across its family, newest
    row on a tie). Both go through lane D's ``tip_version`` so one resolver
    answers for every caller.
    """
    if target_type == "timeline_version":
        return tl.tip_version(db, str(target_id))
    if target_type == "content":
        from app.models import ContentItem

        row = db.get(ContentItem, str(target_id))
        if row is None:
            return None
        latest = db.scalar(
            select(ContentTimeline)
            .where(ContentTimeline.content_item_id == str(target_id))
            .order_by(ContentTimeline.version.desc(), ContentTimeline.created_at.desc())
        )
        if latest is None:
            return None
        return tl.tip_version(db, latest.id)
    return None


def _binding(db: Session, target_type: str, target_id: str) -> tuple[str | None, str | None]:
    """(bound_version, bound_manifest_hash) for a target -- (None, None) when
    the target is not versioned or has no timeline yet."""
    if target_type not in VERSIONED_TARGET_TYPES:
        return None, None
    tip = subject_timeline(db, target_type, target_id)
    if tip is None:
        return None, None
    try:
        manifest = tl.manifest_hash_of(tip.tracks_json or {})
    except TimelineValidationError:
        # An invalid doc cannot be hashed by render_manifest; bind the version
        # number only so the review still records WHICH version was reviewed.
        return str(tip.version or ""), None
    return str(tip.version or ""), manifest


def current_binding(db: Session, target_type: str, target_id: str) -> tuple[str | None, str | None]:
    """Recompute the live (version, manifest_hash) for a target right now."""
    return _binding(db, target_type, target_id)


def get_review(db: Session, ws: Workspace, review_id: str) -> Review:
    """Workspace-scoped review fetch; foreign/missing ids read as missing.

    Raises ``ReviewNotFound`` so routes answer 404 (never 403) for a review that
    belongs to another workspace.
    """
    row = db.get(Review, str(review_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise ReviewNotFoundError(review_id)
    return row


def _emit(ws_id: str, kind: str, message: str, *, actor: str, review: Review,
          **extra: Any) -> None:
    """Activity ledger event (contracts §9) -- actor/target/version in data.

    The SAME ``(kind, data)`` pair is fanned out to lane L's notification
    service so a review event produces inbox rows without this lane knowing the
    recipient rules. The import is lazy + guarded: notifications are a
    convenience, never a correctness requirement for the action that emitted the
    event, so a missing/broken service degrades to "event only".
    """
    from app.services.events import record_event

    data: dict[str, Any] = {
        "actor": actor,
        "target": {"type": review.target_type, "id": review.target_id},
        "version": review.bound_version,
        "project_id": review.project_id,
        "review_id": review.id,
    }
    data.update(extra)
    record_event(ws_id, kind, message, level="info", source="collab", data=data)
    _notify(ws_id, kind, data)


def _notify(ws_id: str, kind: str, data: dict) -> None:
    """Best-effort inbox fan-out (lane L owns the service).

    Tries the contract's ``notify_for_event`` first, then the name lane L
    actually shipped (``on_event``); both are optional. Never raises.
    """
    try:  # pragma: no cover -- depends on lane L landing
        from app.services import notifications as notif
    except ImportError:
        logger.debug("notifications service unavailable; event-only for %s", kind)
        return
    hook = getattr(notif, "notify_for_event", None) or getattr(notif, "on_event", None)
    if hook is None:  # pragma: no cover -- service present but no entry point
        return
    try:  # pragma: no cover -- defensive: notifications never break a request
        from app.db import session_scope

        with session_scope() as s:
            hook(s, ws_id, kind, data)
    except Exception:  # noqa: BLE001 -- telemetry must never break the caller
        logger.exception("notification fan-out failed for %s", kind)


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


def _review_dto(db: Session, row: Review, *, approval_valid: bool | None = None) -> dict:
    if approval_valid is None:
        approval_valid = row.state == APPROVED and not row.stale
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "project_id": row.project_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "title": str(row.title or ""),
        "state": row.state,
        "bound_version": row.bound_version,
        "bound_manifest_hash": row.bound_manifest_hash,
        "stale": bool(row.stale),
        "stale_detected_at": _iso(row.stale_detected_at),
        "approval_valid": approval_valid,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "closed_at": _iso(row.closed_at),
    }


def review_to_dict(db: Session, row: Review) -> dict:
    """Public review shape (public API surface, used by the reviews API)."""
    return _review_dto(db, row)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def create_review(
    db: Session,
    ws: Workspace,
    *,
    target_type: str,
    target_id: str,
    title: str,
    requested_by: str,
    project_id: str | None = None,
    reviewers: list[str] | None = None,
) -> dict:
    """Create a DRAFT review bound to the target's current version.

    Assignees (``reviewers``) are written as append-only ``ReviewAssignment``
    rows and emit ``REVIEW_ASSIGNED``. The binding is captured here so that the
    version under review is fixed from the moment the thread opens.
    """
    if target_type not in TARGET_TYPES:
        raise ValueError(f"unknown review target_type {target_type!r}")
    bound_version, bound_hash = _binding(db, target_type, target_id)
    row = Review(
        workspace_id=ws.id,
        project_id=project_id,
        target_type=target_type,
        target_id=str(target_id),
        title=(title or "").strip()[:200],
        state=DRAFT,
        bound_version=bound_version,
        bound_manifest_hash=bound_hash,
        created_by=requested_by,
    )
    db.add(row)
    db.flush()
    for reviewer_id in reviewers or []:
        _add_assignment(db, row, str(reviewer_id), by_user=requested_by)
    db.commit()
    db.refresh(row)
    _emit(ws.id, "REVIEW_REQUESTED", f"Review '{row.title[:60] or row.id[:8]}' created",
          actor=requested_by, review=row)
    return _review_dto(db, row)


def _add_assignment(db: Session, review: Review, user_id: str, *, by_user: str) -> ReviewAssignment:
    row = ReviewAssignment(review_id=review.id, user_id=user_id, assigned_by=by_user)
    db.add(row)
    db.flush()
    return row


def add_assignee(db: Session, ws: Workspace, review_id: str, *, user_id: str, by_user: str) -> dict:
    """Append a ReviewAssignment (assignments are append-only, never replaced)."""
    review = get_review(db, ws, review_id)
    row = _add_assignment(db, review, str(user_id), by_user=by_user)
    db.commit()
    _emit(ws.id, "REVIEW_ASSIGNED", f"Reviewer {str(user_id)[:8]} assigned",
          actor=by_user, review=review, assignee_user_id=str(user_id))
    return {"review_id": review.id, "user_id": row.user_id, "assigned_by": row.assigned_by,
            "created_at": _iso(row.created_at)}


def _check_transition(review: Review, new_state: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(review.state, frozenset())
    if new_state not in allowed:
        raise ReviewTransitionError(
            f"illegal review transition {review.state} -> {new_state}"
        )


def submit_for_review(db: Session, ws: Workspace, review_id: str, *, actor: str) -> dict:
    """DRAFT|CHANGES_REQUESTED -> IN_REVIEW, rebinding the current version.

    Rebinding on re-request is the point of the state machine: after edits the
    reviewer must re-request against the NEW tip, which is exactly when
    ``bound_version``/``bound_manifest_hash`` are refreshed and ``stale`` clears.
    """
    review = get_review(db, ws, review_id)
    _check_transition(review, IN_REVIEW)
    bound_version, bound_hash = _binding(db, review.target_type, review.target_id)
    review.bound_version = bound_version
    review.bound_manifest_hash = bound_hash
    review.stale = False
    review.stale_detected_at = None
    review.state = IN_REVIEW
    db.commit()
    db.refresh(review)
    _emit(ws.id, "REVIEW_REQUESTED", f"Review '{review.title[:60] or review.id[:8]}' submitted",
          actor=actor, review=review, state=IN_REVIEW)
    return _review_dto(db, review)


def record_decision(
    db: Session,
    ws: Workspace,
    review_id: str,
    *,
    user: str,
    decision: str,
    body: str = "",
    items: list[dict] | None = None,
) -> dict:
    """Apply APPROVE | REJECT | REQUEST_CHANGES from IN_REVIEW.

    APPROVE re-verifies the binding first: if the tip hash moved since the
    review was requested it raises :class:`StaleReviewError` (409 + stale) and
    the review is left IN_REVIEW with ``stale = True``. Every decision appends a
    ``ReviewDecision`` row that freezes the binding observed AT decision time.
    """
    if decision not in DECISIONS:
        raise ValueError(f"unknown review decision {decision!r}")
    review = get_review(db, ws, review_id)
    new_state = DECISIONS[decision]
    _check_transition(review, new_state)

    # binding observed right now (the decision history records this, not the
    # original bound hash -- an APPROVE only reaches here when they still match)
    now_version, now_hash = _binding(db, review.target_type, review.target_id)

    if (decision == "APPROVE" and review.bound_manifest_hash is not None
            and now_hash != review.bound_manifest_hash):
        # target moved: mark stale, stay IN_REVIEW, refuse the approval.
        # NO state change and NO 'APPROVED' event -- nothing was approved;
        # the 409 body + the review's stale flag are the signal.
        review.stale = True
        review.stale_detected_at = utcnow()
        db.commit()
        raise StaleReviewError(
            "review target changed since review was requested "
            "— re-request on the current version",
            review_id=review.id,
            tip_version=now_version,
        )

    db.add(
        ReviewDecision(
            review_id=review.id,
            user_id=user,
            decision=decision,
            bound_version=now_version,
            bound_manifest_hash=now_hash,
            body=(body or "").strip(),
        )
    )
    review.state = new_state
    if new_state in TERMINAL_STATES:
        review.closed_at = utcnow()
    if decision == "APPROVE":
        review.stale = False
        review.stale_detected_at = None
    db.commit()
    db.refresh(review)

    kind = {"APPROVE": "APPROVED", "REJECT": "REVIEW_REJECTED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED"}[decision]
    _emit(ws.id, kind, f"Review '{review.title[:60] or review.id[:8]}' -> {new_state}",
          actor=user, review=review, state=new_state, decision=decision)
    if decision == "REQUEST_CHANGES":
        _auto_revision(db, ws, review, user=user, body=body, items=items)
    return _review_dto(db, review)


def _auto_revision(
    db: Session, ws: Workspace, review: Review, *, user: str, body: str, items: list[dict] | None
) -> None:
    """REQUEST_CHANGES auto-opens a RevisionRequest (contracts §5).

    Items come from the decision body when supplied; otherwise a single
    ``other`` item carries the reviewer note so the revision is never empty.
    """
    from app.engine.collab.revisions import create_revisions

    resolved = list(items or [])
    if not resolved:
        resolved = [{"kind": "other", "description": (body or "changes requested").strip()
                     or "changes requested", "anchor": {}}]
    try:
        create_revisions(
            db, ws,
            items=resolved,
            target_type=review.target_type,
            target_id=review.target_id,
            review_id=review.id,
            project_id=review.project_id,
            created_by=user,
        )
    except ValueError:
        # an auto-revision must never break the decision that triggered it;
        # explicit POST /revisions still validates strictly (422).
        logger.exception("auto revision creation failed for review %s", review.id)
    else:
        _emit(ws.id, "REVISION_REQUESTED", "Revision request opened from review",
              actor=user, review=review, auto=True)


def cancel_review(db: Session, ws: Workspace, review_id: str, *, user: str) -> dict:
    """IN_REVIEW -> CANCELLED (terminal; contracts §5 edge list)."""
    review = get_review(db, ws, review_id)
    _check_transition(review, CANCELLED)
    review.state = CANCELLED
    review.closed_at = utcnow()
    db.commit()
    db.refresh(review)
    _emit(ws.id, "REVIEW_CANCELLED", f"Review '{review.title[:60] or review.id[:8]}' cancelled",
          actor=user, review=review, state=CANCELLED)
    return _review_dto(db, review)


def refresh_staleness(db: Session, ws: Workspace, review_id: str) -> dict:
    """Recompute the stale flag for one review (lazy, on every GET/list).

    For versioned targets: stale when the tip's manifest hash no longer equals
    the bound hash. For unversioned targets the flag is informational only and
    never flips to True here. An APPROVED review is NEVER un-approved -- it
    keeps its state and consumers read ``approval_valid`` instead.
    """
    review = get_review(db, ws, review_id)
    if review.target_type not in VERSIONED_TARGET_TYPES or review.bound_manifest_hash is None:
        return _review_dto(db, review)
    _, current_hash = _binding(db, review.target_type, review.target_id)
    stale = bool(current_hash and current_hash != review.bound_manifest_hash)
    if stale != bool(review.stale):
        review.stale = stale
        review.stale_detected_at = utcnow() if stale else None
        db.commit()
        db.refresh(review)
    return _review_dto(db, review)


def list_reviews(
    db: Session,
    ws: Workspace,
    *,
    state: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    project_id: str | None = None,
    refresh: bool = True,
) -> list[dict]:
    """List workspace reviews, newest first, refreshing staleness lazily."""
    q = select(Review).where(Review.workspace_id == ws.id)
    if state:
        q = q.where(Review.state == state)
    if target_type:
        q = q.where(Review.target_type == target_type)
    if target_id:
        q = q.where(Review.target_id == target_id)
    if project_id:
        q = q.where(Review.project_id == project_id)
    rows = db.scalars(q.order_by(Review.created_at.desc())).all()
    out: list[dict] = []
    for row in rows:
        if refresh and row.target_type in VERSIONED_TARGET_TYPES \
                and row.bound_manifest_hash is not None:
            _, current_hash = _binding(db, row.target_type, row.target_id)
            stale = bool(current_hash and current_hash != row.bound_manifest_hash)
            if stale != bool(row.stale):
                row.stale = stale
                row.stale_detected_at = utcnow() if stale else None
        out.append(_review_dto(db, row))
    if refresh:
        db.commit()
    return out


def review_detail(db: Session, ws: Workspace, review_id: str) -> dict:
    """Full review payload: the review + decisions + assignments (stale first)."""
    review = refresh_staleness(db, ws, review_id)
    decisions = [
        {
            "id": row.id,
            "user_id": row.user_id,
            "decision": row.decision,
            "bound_version": row.bound_version,
            "bound_manifest_hash": row.bound_manifest_hash,
            "body": str(row.body or ""),
            "created_at": _iso(row.created_at),
        }
        for row in db.scalars(
            select(ReviewDecision)
            .where(ReviewDecision.review_id == review["id"])
            .order_by(ReviewDecision.created_at)
        ).all()
    ]
    assignments = [
        {
            "user_id": row.user_id,
            "assigned_by": row.assigned_by,
            "created_at": _iso(row.created_at),
        }
        for row in db.scalars(
            select(ReviewAssignment)
            .where(ReviewAssignment.review_id == review["id"])
            .order_by(ReviewAssignment.created_at)
        ).all()
    ]
    detail = dict(review)
    detail["decisions"] = decisions
    detail["assignments"] = assignments
    return detail


def revisions_for_review(db: Session, review_id: str) -> list[dict]:
    """Revision requests opened by a given review (UI convenience)."""
    from app.engine.collab.revisions import revision_to_dict

    rows = db.scalars(
        select(RevisionRequest)
        .where(RevisionRequest.review_id == review_id)
        .order_by(RevisionRequest.created_at)
    ).all()
    return [revision_to_dict(row) for row in rows]
