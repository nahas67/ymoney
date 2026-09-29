"""Project-level RBAC (Work 11 Lane F) -- contracts §3.

ONE auth system: workspace roles stay ``{viewer: 0, member: 1, admin: 2,
owner: 3}`` (the same order dict as ``auth_service.py:148`` -- duplicated
locally below only because auth_service keeps it inside the
``require_workspace_role`` closure; it is never redefined with different
values). Project roles only NARROW what a workspace role already allows;
they never expand it. Workspace admins (and superusers) bypass project
gating entirely -- governance.

Capability matrix (contracts §3, ``role -> allowed caps``)::

    capability          OWNER  ADMIN  EDITOR  REVIEWER  VIEWER
    view_project          x      x      x        x        x
    edit_project          x      x
    edit_timeline         x      x      x
    comment               x      x      x        x
    request_revision      x      x               x
    approve               x      x               x
    export                x      x      x
    publish               x      x
    manage_collaborators  x

Error policy (decided here, enforced by routes):
    * foreign project / target (another workspace) -> HTTP **404**
      (``"project not found"`` / ``"target not found"``) -- never 403, so an
      id from another workspace is indistinguishable from a missing one.
    * unknown ``capability`` -> ``ValueError``; routes map it to **422**
      (enum-ish validation, same policy as ``api/v1/knowledge.py``).
    * linked target/project + missing membership or missing cap -> HTTP
      **403** ``"insufficient project role"``.
    * target NOT linked to any project -> **no raise** (fallback): every
      capability -- collaboration and mutation alike -- falls through to the
      route's own ``require_workspace_role`` floor, which stays the
      enforcement line. Collaboration caps (view_project/comment/
      request_revision/approve) therefore pass for any workspace member
      (their floor is ``viewer``), while mutation caps (edit_*/publish/
      export/manage_collaborators) still need the route's member/admin floor.

Resolution order for an explicit ``project_id`` is resolve-then-bypass: the
workspace-scoped 404 is checked BEFORE the workspace-admin bypass, so even a
workspace admin can never act on (or probe) a project id from another
workspace.

Last-owner rules: the final OWNER of a project can neither be demoted nor
removed (``ValueError`` -> routes answer 409). The repair path is granting
OWNER to somebody else, which is never blocked. Ownership transfer promotes
the target first, then demotes every other OWNER to ADMIN.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Project,
    ProjectMember,
    ProjectTarget,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services.auth_service import get_current_user, require_workspace_role

logger = logging.getLogger("ymoney.collab")

# project role -> order (contracts §3: VIEWER < REVIEWER < EDITOR < ADMIN < OWNER)
PROJECT_ROLE_ORDER = {"VIEWER": 0, "REVIEWER": 1, "EDITOR": 2, "ADMIN": 3, "OWNER": 4}
PROJECT_ROLES: tuple[str, ...] = tuple(PROJECT_ROLE_ORDER)

# capability -> project roles that carry it (contracts §3 matrix, verbatim)
CAPABILITIES: dict[str, set[str]] = {
    "view_project": {"OWNER", "ADMIN", "EDITOR", "REVIEWER", "VIEWER"},
    "edit_project": {"OWNER", "ADMIN"},
    "edit_timeline": {"OWNER", "ADMIN", "EDITOR"},
    "comment": {"OWNER", "ADMIN", "EDITOR", "REVIEWER"},
    "request_revision": {"OWNER", "ADMIN", "REVIEWER"},
    "approve": {"OWNER", "ADMIN", "REVIEWER"},
    "export": {"OWNER", "ADMIN", "EDITOR"},
    "publish": {"OWNER", "ADMIN"},
    "manage_collaborators": {"OWNER"},
}

# caps a workspace member gets for a target that is NOT linked to a project
# (contracts §3 fallback: comment/request_revision/approve/view_project).
COLLABORATION_CAPS: frozenset[str] = frozenset(
    {"view_project", "comment", "request_revision", "approve"}
)

# Workspace role order -- SAME dict as auth_service.py:148
# (require_workspace_role / resolve_workspace). Duplicated locally because
# auth_service builds it inside its factory closure.
_WS_ROLE_ORDER = {
    WorkspaceMember.ROLE_VIEWER: 0,
    WorkspaceMember.ROLE_MEMBER: 1,
    WorkspaceMember.ROLE_ADMIN: 2,
    WorkspaceMember.ROLE_OWNER: 3,
}
_WS_ADMIN_ORDER = _WS_ROLE_ORDER[WorkspaceMember.ROLE_ADMIN]


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def workspace_membership(db: Session, ws: Workspace, user: User) -> WorkspaceMember | None:
    """The user's workspace membership row (None for non-members)."""
    return db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user.id,
        )
    )


def is_workspace_admin(db: Session, ws: Workspace, user: User) -> bool:
    """Workspace admin+ (or superuser) => governance bypass of project caps."""
    if getattr(user, "is_superuser", False):
        return True
    member = workspace_membership(db, ws, user)
    if member is None:
        return False
    return _WS_ROLE_ORDER.get(member.role, -1) >= _WS_ADMIN_ORDER


def project_role(db: Session, project_id: str, user_id: str) -> str | None:
    """Project role of a user, or None when they are not a member."""
    return db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )


def load_project(db: Session, ws: Workspace, project_id: str) -> Project:
    """Workspace-scoped project fetch; foreign/missing ids read as 404."""
    row = db.get(Project, str(project_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="project not found")
    return row


def _linked_projects(db: Session, ws: Workspace, target_type: str, target_id: str) -> list[Project]:
    """Projects of THIS workspace linked to a polymorphic target.

    * no link rows at all      -> [] (caller applies the unlinked fallback)
    * links only in other wss  -> 404 "target not found" (no id leaking)
    * at least one link here   -> those projects (foreign siblings ignored)
    """
    links = db.scalars(
        select(ProjectTarget).where(
            ProjectTarget.target_type == target_type,
            ProjectTarget.target_id == target_id,
        )
    ).all()
    if not links:
        return []
    here: list[Project] = []
    foreign = False
    for link in links:
        project = db.get(Project, link.project_id)
        if project is None:
            continue
        if project.workspace_id == ws.id:
            here.append(project)
        else:
            foreign = True
    if not here and foreign:
        raise HTTPException(status_code=404, detail="target not found")
    return here


# ---------------------------------------------------------------------------
# the single assertion (contracts §3)
# ---------------------------------------------------------------------------


def assert_capability(
    db: Session,
    ws: Workspace,
    user: User,
    *,
    capability: str,
    project_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> None:
    """Raise unless ``user`` may exercise ``capability`` (see module docstring).

    Returns None on allow. Raises ``ValueError`` for a bad capability or a
    bad argument combination (routes map that to 422), HTTP 404 for foreign
    ids, HTTP 403 for a linked target without the required project role.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")

    if project_id:
        # explicit project wins when both project_id and target are passed
        projects = [load_project(db, ws, project_id)]
    elif target_type and target_id:
        projects = _linked_projects(db, ws, target_type, target_id)
        if not projects:
            # target NOT linked to a project: no raise -- the route's
            # require_workspace_role floor is the enforcement line (§3).
            return
    elif target_type or target_id:
        raise ValueError("target_type and target_id must be provided together")
    else:
        raise ValueError("project_id or target_type/target_id is required")

    if is_workspace_admin(db, ws, user):
        return  # governance bypass (after the 404 checks above)

    allowed = CAPABILITIES[capability]
    for project in projects:
        role = project_role(db, project.id, user.id)
        if role is not None and role in allowed:
            return
    raise HTTPException(status_code=403, detail="insufficient project role")


def can(db: Session, ws: Workspace, user: User, **kwargs) -> bool:
    """Boolean twin of assert_capability (for capability badges in responses).

    403/404 read as False; argument errors (ValueError) still raise because
    they are programming mistakes, not denials.
    """
    try:
        assert_capability(db, ws, user, **kwargs)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------------


def require_project_capability(project_id_param: str, capability: str):
    """Dependency for ``/projects/{project_id}/...`` routes.

    Resolves workspace + user through the SAME auth_service dependencies
    (``require_workspace_role("viewer")`` floor + ``get_current_user``), reads
    ``project_id_param`` from the path, applies the contracts §3 rules and
    yields the loaded :class:`Project`.

    Unknown capability -> 422; foreign/missing project -> 404; missing
    membership or cap -> 403 -- all as short details.
    """
    floor = require_workspace_role("viewer")

    def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Project:
        ws_id = str(request.path_params.get("workspace_id") or "")
        ws = floor(workspace_id=ws_id, user=user, db=db)  # one auth system
        project_id = str(request.path_params.get(project_id_param) or "")
        try:
            assert_capability(db, ws, user, capability=capability, project_id=project_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return load_project(db, ws, project_id)

    return dependency


# ---------------------------------------------------------------------------
# membership helpers (last-owner rules) -- used by the Projects API
# ---------------------------------------------------------------------------


def owner_count(db: Session, project_id: str) -> int:
    """How many OWNER rows the project has right now."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.project_id == project_id, ProjectMember.role == "OWNER")
        )
        or 0
    )


def add_member(
    db: Session,
    project_id: str,
    user_id: str,
    role: str,
    *,
    by_user: str,
) -> ProjectMember:
    """Insert or re-role a project member.

    Last-owner rule: demoting the final OWNER raises ``ValueError`` (routes
    answer 409). Granting OWNER is never blocked -- that is the repair path.
    ``by_user`` is the acting user id (audit; routes emit the activity event).
    """
    if role not in PROJECT_ROLE_ORDER:
        raise ValueError(f"unknown project role {role!r}")
    row = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if row is None:
        row = ProjectMember(project_id=project_id, user_id=user_id, role=role)
        db.add(row)
        db.flush()
        logger.debug(
            "project member added: project=%s user=%s role=%s by=%s",
            project_id, user_id, role, by_user,
        )
        return row
    if row.role == "OWNER" and role != "OWNER" and owner_count(db, project_id) <= 1:
        raise ValueError("cannot demote the final owner")
    if row.role != role:
        row.role = role
        db.flush()
    return row


def remove_member(db: Session, project_id: str, user_id: str, *, by_user: str) -> None:
    """Remove a project member (HTTP 404 when not a member).

    Last-owner rule: removing the final OWNER raises ``ValueError`` (routes
    answer 409) regardless of who asks; promote someone else first.
    """
    row = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="member not found")
    was_owner = row.role == "OWNER"
    if was_owner and owner_count(db, project_id) <= 1:
        raise ValueError("cannot remove the final owner")
    db.delete(row)
    db.flush()
    logger.debug(
        "project member removed: project=%s user=%s by=%s", project_id, user_id, by_user
    )
