"""Reviews API (Work 11 Lane R) -- contracts §5.

Canonical surface, mounted once in ``api/v1/__init__.py``::

    POST   /workspaces/{ws}/reviews                              create (DRAFT)
    GET    /workspaces/{ws}/reviews                              list + filters
    GET    /workspaces/{ws}/reviews/{id}                         detail + caps
    POST   /workspaces/{ws}/reviews/{id}/submit                  DRAFT|CR -> IN_REVIEW
    POST   /workspaces/{ws}/reviews/{id}/decisions                APPROVE|REJECT|REQUEST_CHANGES
    POST   /workspaces/{ws}/reviews/{id}/cancel                  -> CANCELLED
    POST   /workspaces/{ws}/reviews/{id}/assignments             add a reviewer

This module also carries the **revisions** routes (contracts §7) on their own
canonical prefix, so the review/revision lifecycle stays in one file:

    POST   /workspaces/{ws}/revisions                            open a request
    GET    /workspaces/{ws}/revisions                            list + filters
    GET    /workspaces/{ws}/revisions/{id}                       detail
    POST   /workspaces/{ws}/revisions/{id}/state                 explicit state move

Floors are the existing ``require_workspace_role`` dependencies:

  * reads -> ``viewer``;
  * collaboration writes (comments) -> ``viewer``. This is a deliberate
    Work 11 decision, documented in ``services/project_auth``: collaboration
    capabilities (comment / request_revision / approve) fall through to the
    route floor. Kept as-is by Work 11.5 (see the production gap matrix,
    B-F4) because raising it would contradict the locked Work 11 matrix.
  * governance writes (review create/submit/decision/cancel, assignments,
    revision create/state) -> ``member``. Work 11.5 B-F2 raised these from
    ``viewer``: opening, deciding and moving a review is a governance action,
    not observation, and a workspace viewer must not be able to drive it.

Project capabilities come from ``services/project_auth`` (contracts §3) and
only NARROW the floor.

RBAC matrix locked by tests (contracts §5): workspace floor ``member`` for
create, project scope = the ``comment`` capability:

  * viewer POST -> 403, member POST -> 201;
  * project VIEWER POST -> 403, project REVIEWER POST -> 201;
  * viewer GET -> 200.

``GET /{id}`` returns a ``capabilities`` object (``can_approve``,
``can_request_changes``, ...) computed via the boolean ``can()`` twin -- the
frontend (FE-A) depends on it to show/hide action buttons.

Error hygiene: domain ``ReviewTransitionError``/``RevisionTransitionError`` ->
409, ``StaleReviewError`` -> 409 with ``stale: true`` in the body, ``ValueError``
-> 422, missing/foreign ids -> 404 (never 403), anything else -> generic 500
``{"detail": "internal error"}`` logged on ``ymoney.collab`` with no echo
(``api/v1/knowledge.py::_guard`` pattern).
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
from app.engine.collab import reviews as engine
from app.engine.collab import revisions as rev_engine
from app.models import Review, User, Workspace
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability, can, load_project

reviews_router = APIRouter(prefix="/workspaces/{workspace_id}/reviews", tags=["reviews"])
revisions_router = APIRouter(prefix="/workspaces/{workspace_id}/revisions", tags=["revisions"])
logger = logging.getLogger("ymoney.collab")


# ---------------------------------------------------------------------------
# error policy
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body (contracts §5).

    HTTPException passes through; the review state machine maps to 409;
    StaleReviewError maps to 409 + ``stale: true``; ValueError -> 422; the rest
    is a logged, non-echoing 500.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except engine.StaleReviewError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"error": _short(exc), "stale": True,
                            "tip_version": exc.tip_version},
                ) from None
            except (engine.ReviewTransitionError, rev_engine.RevisionTransitionError) as exc:
                raise HTTPException(status_code=409, detail=_short(exc)) from None
            except ValueError as exc:
                raise HTTPException(status_code=value_error, detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception("reviews route failed: %s", getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """assert_capability at the route edge (ValueError -> 422)."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


def _require_ws_floor(db: Session, ws: Workspace, user: User, role: str) -> None:
    """Explicit workspace-role floor for reviewer-side actions.

    ``assert_capability`` deliberately falls through to NO raise when a target
    is not linked to any project (contracts §3 unlinked fallback), which is
    correct for collaboration caps on unlinked content but means the
    "reviewer side" gate on ``OPEN -> ADDRESSED`` would never bite. This helper
    is the missing floor: the workspace role order mirrors
    ``auth_service.require_workspace_role`` so both stay one auth system.
    """
    from app.models import WorkspaceMember

    if getattr(user, "is_superuser", False):
        return
    order = {
        WorkspaceMember.ROLE_VIEWER: 0,
        WorkspaceMember.ROLE_MEMBER: 1,
        WorkspaceMember.ROLE_ADMIN: 2,
        WorkspaceMember.ROLE_OWNER: 3,
    }
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == ws.id,
        WorkspaceMember.user_id == user.id,
    ).first()
    if member is None or order.get(member.role, -1) < order.get(role, 0):
        raise HTTPException(status_code=403, detail="insufficient role")


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class ReviewCreateBody(BaseModel):
    target_type: Literal[
        "project", "content", "timeline_version", "campaign", "localization", "ugc_asset"
    ]
    target_id: str = Field(min_length=1, max_length=36)
    title: str = Field(default="", max_length=200)
    project_id: str | None = None
    reviewers: list[str] = Field(default_factory=list)


class DecisionBody(BaseModel):
    decision: Literal["APPROVE", "REJECT", "REQUEST_CHANGES"]
    body: str = Field(default="", max_length=8000)
    items: list[dict] | None = None


class AssignmentBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=36)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _review_or_404(db: Session, ws: Workspace, review_id: str) -> Review:
    try:
        return engine.get_review(db, ws, review_id)
    except engine.ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="review not found") from None


def _validate_assignees(db: Session, ws: Workspace, reviewers: list[str]) -> None:
    """Every assignee must be a workspace member (foreign/unknown -> 404)."""
    from app.models import WorkspaceMember

    for user_id in reviewers:
        member = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user_id,
        ).first()
        if member is None:
            raise HTTPException(status_code=404, detail="user not found")


def _capabilities(db: Session, ws: Workspace, user: User, review: Review) -> dict:
    """Capability badges for the FE (contracts §5/§12)."""
    kwargs: dict[str, Any] = (
        {"project_id": review.project_id}
        if review.project_id
        else {"target_type": review.target_type, "target_id": review.target_id}
    )
    return {
        "can_approve": can(db, ws, user, capability="approve", **kwargs),
        "can_request_changes": can(db, ws, user, capability="request_revision", **kwargs),
        "can_cancel": review.created_by == user.id
        or can(db, ws, user, capability="manage_collaborators", **kwargs),
        "can_assign": can(db, ws, user, capability="request_revision", **kwargs),
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@reviews_router.post("", status_code=201, summary="Create a review (DRAFT)")
@_guard()
def create_review(
    body: ReviewCreateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a review bound to the target's current version.

    RBAC (contracts §5, matrix locked in tests):
      * workspace floor = **member** (a workspace viewer POST -> 403, member -> 201);
      * project scope = the ``comment`` capability (OWNER|ADMIN|EDITOR|REVIEWER).
        A project VIEWER POST -> 403, a project REVIEWER POST -> 201.

    Contract note: §5's prose floated ``edit_timeline`` for the project-scoped
    cap, but that cap excludes REVIEWER (§3) and would contradict the locked
    "project REVIEWER -> 201" assertion. The test matrix wins; ``comment`` is
    the narrowest capability that admits REVIEWER *and* EDITOR ("content editors
    request review") while excluding VIEWER. The workspace floor is ``member``
    (not ``viewer``) precisely so the locked "viewer POST -> 403" case holds
    without a bespoke handler check; §3 only ever lets project roles narrow a
    workspace floor, never widen it.

    When no ``project_id`` is given the cap is resolved through the target's
    project links (unlinked targets fall through to the member floor per §3),
    so a project-scoped target is never a bypass.
    """
    if body.project_id:
        project = load_project(db, ws, body.project_id)  # foreign -> 404
        _check(db, ws, user, capability="comment", project_id=project.id)
    else:
        _check(db, ws, user, capability="comment",
               target_type=body.target_type, target_id=body.target_id)
    _validate_assignees(db, ws, body.reviewers)
    return engine.create_review(
        db, ws,
        target_type=body.target_type,
        target_id=body.target_id,
        title=body.title,
        requested_by=user.id,
        project_id=body.project_id,
        reviewers=body.reviewers,
    )


@reviews_router.get("", summary="List reviews (filters: state/target/project)")
@_guard()
def list_reviews(
    state: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    items = engine.list_reviews(
        db, ws, state=state, target_type=target_type,
        target_id=target_id, project_id=project_id,
    )
    return {"items": items}


@reviews_router.get("/{review_id}", summary="Review detail (+ decisions/assignments/caps)")
@_guard()
def get_review(
    review_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        detail = engine.review_detail(db, ws, review_id)  # refreshes staleness
    except engine.ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="review not found") from None
    row = _review_or_404(db, ws, review_id)
    detail["capabilities"] = _capabilities(db, ws, user, row)
    return detail


@reviews_router.post("/{review_id}/submit", summary="Submit for review (rebinds version)")
@_guard()
def submit_review(
    review_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    review = _review_or_404(db, ws, review_id)
    # W11.5 B-F2: submitting rebinds versions and moves the lifecycle --
    # a viewer must not drive it. Same comment capability as creating.
    if review.project_id:
        _check(db, ws, user, capability="comment", project_id=review.project_id)
    else:
        _check(db, ws, user, capability="comment",
               target_type=review.target_type, target_id=review.target_id)
    return engine.submit_for_review(db, ws, review_id, actor=user.id)


@reviews_router.post("/{review_id}/decisions", summary="Approve / Reject / Request changes")
@_guard()
def post_decision(
    review_id: str,
    body: DecisionBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    review = _review_or_404(db, ws, review_id)
    kwargs: dict[str, Any] = (
        {"project_id": review.project_id}
        if review.project_id
        else {"target_type": review.target_type, "target_id": review.target_id}
    )
    if body.decision == "REQUEST_CHANGES":
        _check(db, ws, user, capability="request_revision", **kwargs)
    else:
        _check(db, ws, user, capability="approve", **kwargs)
    return engine.record_decision(
        db, ws, review_id, user=user.id, decision=body.decision,
        body=body.body, items=body.items,
    )


@reviews_router.post("/{review_id}/cancel", summary="Cancel a review")
@_guard()
def cancel_review(
    review_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    review = _review_or_404(db, ws, review_id)
    kwargs: dict[str, Any] = (
        {"project_id": review.project_id}
        if review.project_id
        else {"target_type": review.target_type, "target_id": review.target_id}
    )
    is_creator = review.created_by == user.id
    if not is_creator:
        _check(db, ws, user, capability="manage_collaborators", **kwargs)
    return engine.cancel_review(db, ws, review_id, user=user.id)


@reviews_router.post(
    "/{review_id}/assignments", status_code=201, summary="Assign a reviewer"
)
@_guard()
def add_assignment(
    review_id: str,
    body: AssignmentBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    review = _review_or_404(db, ws, review_id)
    kwargs: dict[str, Any] = (
        {"project_id": review.project_id}
        if review.project_id
        else {"target_type": review.target_type, "target_id": review.target_id}
    )
    _check(db, ws, user, capability="request_revision", **kwargs)
    _validate_assignees(db, ws, [body.user_id])
    return engine.add_assignee(db, ws, review_id, user_id=body.user_id, by_user=user.id)


@reviews_router.get("/{review_id}/revisions", summary="Revisions opened by a review")
@_guard()
def list_review_revisions(
    review_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    _review_or_404(db, ws, review_id)
    items = engine.revisions_for_review(db, review_id)
    return {"items": items}


# ---------------------------------------------------------------------------
# revisions (contracts §7) -- mounted on their own canonical prefix
#
# These routes live in this module (not api/v1/revisions.py) to keep the
# review/revision lifecycle in one reviewed file; the mount is the same
# canonical /workspaces/{workspace_id}/revisions/... path either way.
# ---------------------------------------------------------------------------


class RevisionItem(BaseModel):
    kind: Literal["trim", "timing", "asset_swap", "text", "voice", "caption", "brand", "other"]
    description: str = Field(min_length=1, max_length=2000)
    anchor: dict | None = None


class RevisionCreateBody(BaseModel):
    items: list[RevisionItem] = Field(min_length=1)
    target_type: str = Field(min_length=1, max_length=32)
    target_id: str = Field(min_length=1, max_length=36)
    review_id: str | None = None
    project_id: str | None = None


class RevisionStateBody(BaseModel):
    state: Literal["OPEN", "ADDRESSED", "DISMISSED"]


@revisions_router.post("", status_code=201, summary="Open a revision request")
@_guard()
def create_revisions(
    body: RevisionCreateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Open a revision request with a validated ask list (kind + description).

    Bad kind or an empty description -> 422 (the engine validates every item
    BEFORE writing, so a bad item never leaves a half-created row).
    """
    kwargs: dict[str, Any] = (
        {"project_id": body.project_id}
        if body.project_id
        else {"target_type": body.target_type, "target_id": body.target_id}
    )
    _check(db, ws, user, capability="request_revision", **kwargs)
    items = rev_engine.create_revisions(
        db, ws,
        items=[item.model_dump() for item in body.items],
        target_type=body.target_type,
        target_id=body.target_id,
        created_by=user.id,
        review_id=body.review_id,
        project_id=body.project_id,
    )
    return {"items": items}


@revisions_router.get("", summary="List revision requests")
@_guard()
def list_revisions(
    state: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    review_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    items = rev_engine.list_revisions(
        db, ws, state=state, target_id=target_id, target_type=target_type,
        review_id=review_id, project_id=project_id,
    )
    return {"items": items}


def _revision_or_404(db: Session, ws: Workspace, revision_id: str):
    try:
        return rev_engine.get_revision(db, ws, revision_id)
    except rev_engine.RevisionNotFoundError:
        raise HTTPException(status_code=404, detail="revision not found") from None


@revisions_router.get("/{revision_id}", summary="Revision detail")
@_guard()
def get_revision(
    revision_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    return rev_engine.revision_to_dict(_revision_or_404(db, ws, revision_id))


@revisions_router.post(
    "/{revision_id}/state", summary="Set revision state (explicit only -- never auto)"
)
@_guard()
def set_revision_state(
    revision_id: str,
    body: RevisionStateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Explicit state move -- the ONLY way a revision changes state (contracts §7).

    ``OPEN -> ADDRESSED`` requires the ``request_revision`` cap (reviewer side)
    AND, for unlinked targets where the cap cannot resolve a project, an
    explicit ``member`` workspace floor (``_require_ws_floor``) so the
    reviewer-side gate actually bites. ``DISMISSED`` / reopen are allowed for the
    request's creator or an EDITOR+ (``edit_timeline``) holder. Editing the
    timeline never moves this state -- that is the whole point of the lifecycle.
    """
    row = _revision_or_404(db, ws, revision_id)
    kwargs: dict[str, Any] = (
        {"project_id": row.project_id}
        if row.project_id
        else {"target_type": row.target_type, "target_id": row.target_id}
    )
    if body.state == "ADDRESSED":
        _check(db, ws, user, capability="request_revision", **kwargs)
        _require_ws_floor(db, ws, user, "member")
    elif row.created_by != user.id and not can(
        db, ws, user, capability="edit_timeline", **kwargs
    ):
        raise HTTPException(status_code=403, detail="insufficient project role")
    return rev_engine.set_state(db, ws, revision_id, state=body.state, user=user.id)
