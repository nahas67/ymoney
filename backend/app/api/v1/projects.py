"""Projects API (Work 11 Lane F) -- contracts §4.

Canonical surface, mounted once in ``api/v1/__init__.py``:

    POST   /workspaces/{ws}/projects                              member floor
    GET    /workspaces/{ws}/projects                              viewer floor
    GET    /workspaces/{ws}/projects/{project_id}                 viewer + view_project
    PATCH  /workspaces/{ws}/projects/{project_id}                 viewer + edit_project
    POST   .../members                                            viewer + manage_collaborators
    DELETE .../members/{user_id}                                  viewer + manage_collaborators OR self
    GET    .../targets                                            viewer + edit_project
    POST   .../targets                                            viewer + edit_project
    DELETE .../targets/{target_type}/{target_id}                  viewer + edit_project
    POST   .../transfer                                           viewer + manage_collaborators (OWNER)

Floors are the existing ``require_workspace_role`` dependencies; project
capabilities come from ``services/project_auth`` (contracts §3). Both have to
pass -- a project role narrows the workspace floor, it never widens it.

Response shapes: single -> dict, lists -> ``{"items": [...]}``. Project dict
keys: ``id, workspace_id, name, description, status, created_by,
created_at, updated_at`` plus ``member_count`` / ``target_count`` /
``members`` / ``targets`` on the detail endpoint.

Error policy (contracts §4): short ``HTTPException`` details; domain
``ValueError`` -> 409 on the member/transfer routes (last-owner and
wrong-role transfer conflicts); a missing project member (DELETE member,
transfer target that is not a member) -> 404 ``"member not found"``; an
unknown capability or body enum -> 422; anything else is logged on
``ymoney.collab`` and surfaced as ``{"detail": "internal error"}`` with no
exception echo (``api/v1/knowledge.py::_guard`` pattern).

Mutations ``db.commit()`` BEFORE ``record_event``: the route session holds
the SQLite write lock until commit and ``record_event`` opens its own
session (same ordering as ``api/v1/timelines.py``).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Campaign,
    ContentItem,
    ContentTimeline,
    LocalizedContent,
    MediaAsset,
    Project,
    ProjectMember,
    ProjectTarget,
    User,
    Workspace,
    WorkspaceMember,
)
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.events import record_event
from app.services.project_auth import (
    add_member,
    assert_capability,
    load_project,
    remove_member,
    require_project_capability,
)

projects_router = APIRouter(prefix="/workspaces/{workspace_id}/projects", tags=["projects"])
logger = logging.getLogger("ymoney.collab")

# project_targets.target_type -> the workspace-scoped row that must exist
# before a link is created. ugc_asset resolves to MediaAsset: UGC renders are
# stored as workspace media assets (models/ugc.py keeps only a render ref).
TARGET_MODELS: dict[str, type] = {
    "content": ContentItem,
    "campaign": Campaign,
    "timeline": ContentTimeline,
    "localization": LocalizedContent,
    "ugc_asset": MediaAsset,
}


# ---------------------------------------------------------------------------
# error policy (contracts §4: short details, no exception echo)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors (never a stack)."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 404):
    """Uniform error policy for one route body.

    * ``HTTPException`` passes through untouched (401/403/404/409/422).
    * ``ValueError`` becomes ``value_error`` with a short, single-line detail
      (409 on the member/transfer routes where the domain error is
      last-owner/transfer conflict, 404 elsewhere).
    * anything else is logged on ``ymoney.collab`` and surfaced as a generic
      500 that echoes nothing back to the client.
    """

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
                logger.exception("projects route failed: %s", getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """assert_capability at the route edge.

    Unknown capability / bad argument combination -> 422 (enum-ish
    validation, knowledge.py policy); 404/403 details pass through as-is.
    """
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class ProjectCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)


class ProjectUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)


class MemberBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=36)
    role: Literal["OWNER", "ADMIN", "EDITOR", "REVIEWER", "VIEWER"]


class TargetBody(BaseModel):
    target_type: Literal["content", "campaign", "timeline", "localization", "ugc_asset"]
    target_id: str = Field(min_length=1, max_length=36)


class TransferBody(BaseModel):
    to_user_id: str = Field(min_length=1, max_length=36)


class DuplicateProjectBody(BaseModel):
    """Optional overrides for a duplication. Omitted keys come from the source."""

    name: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


def _project_dto(row: Project, *, member_count: int | None = None,
                 target_count: int | None = None) -> dict:
    dto = {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": str(row.name or ""),
        "description": str(row.description or ""),
        "status": str(row.status or "ACTIVE"),
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if member_count is not None:
        dto["member_count"] = member_count
    if target_count is not None:
        dto["target_count"] = target_count
    return dto


def _member_dto(row: ProjectMember) -> dict:
    return {"user_id": row.user_id, "role": row.role, "created_at": _iso(row.created_at)}


def _target_dto(row: ProjectTarget) -> dict:
    return {
        "target_type": row.target_type,
        "target_id": row.target_id,
        "created_at": _iso(row.created_at),
    }


def _count(db: Session, model, **filters) -> int:
    q = select(func.count()).select_from(model)
    for column, value in filters.items():
        q = q.where(getattr(model, column) == value)
    return int(db.scalar(q) or 0)


def _workspace_user(db: Session, workspace_id: str, user_id: str) -> None:
    """404 unless the user is a member of this workspace (foreign ids stay 404)."""
    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail="user not found")


def _target_exists(db: Session, workspace_id: str, target_type: str, target_id: str) -> bool:
    model = TARGET_MODELS.get(target_type)
    if model is None:
        return False
    row = db.scalar(select(model.id).where(model.workspace_id == workspace_id, model.id == target_id))
    return row is not None


def _emit(ws_id: str, kind: str, message: str, *, user_id: str, project_id: str,
          **extra) -> None:
    """Activity ledger event (contracts §9 kinds) -- actor rides in data."""
    data: dict[str, Any] = {"actor": user_id, "project_id": project_id}
    data.update(extra)
    record_event(ws_id, kind, message, level="info", source="collab", data=data)


# ---------------------------------------------------------------------------
# endpoints -- projects
# ---------------------------------------------------------------------------


@projects_router.post("", status_code=201, summary="Create a project (creator becomes OWNER)")
@_guard()
def create_project(
    body: ProjectCreateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    row = Project(
        workspace_id=ws.id,
        name=name,
        description=(body.description or "").strip(),
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    db.add(ProjectMember(project_id=row.id, user_id=user.id, role="OWNER"))
    db.commit()
    _emit(ws.id, "PROJECT_CREATED", f"Project '{name[:60]}' created",
          user_id=user.id, project_id=row.id)
    return _project_dto(row)


@projects_router.get("", summary="List workspace projects")
@_guard()
def list_projects(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Project)
        .where(Project.workspace_id == ws.id)
        .order_by(Project.created_at.desc())
    ).all()
    return {"items": [_project_dto(row) for row in rows]}


@projects_router.get("/{project_id}", summary="Project detail (members + targets)")
@_guard()
def get_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "view_project")),
    db: Session = Depends(get_db),
):
    members = db.scalars(
        select(ProjectMember)
        .where(ProjectMember.project_id == project.id)
        .order_by(ProjectMember.created_at)
    ).all()
    targets = db.scalars(
        select(ProjectTarget)
        .where(ProjectTarget.project_id == project.id)
        .order_by(ProjectTarget.created_at)
    ).all()
    dto = _project_dto(
        project,
        member_count=_count(db, ProjectMember, project_id=project.id),
        target_count=_count(db, ProjectTarget, project_id=project.id),
    )
    dto["members"] = [_member_dto(row) for row in members]
    dto["targets"] = [_target_dto(row) for row in targets]
    return dto


@projects_router.patch("/{project_id}", summary="Update project name/description")
@_guard()
def update_project(
    project_id: str,
    body: ProjectUpdateBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "edit_project")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    changed = False
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        if name != project.name:
            project.name = name
            changed = True
    if body.description is not None and body.description.strip() != (project.description or ""):
        project.description = body.description.strip()
        changed = True
    if changed:
        db.commit()
        _emit(ws.id, "PROJECT_UPDATED", f"Project '{project.name[:60]}' updated",
              user_id=user.id, project_id=project.id)
    return _project_dto(project)


# ---------------------------------------------------------------------------
# endpoints -- members
# ---------------------------------------------------------------------------


@projects_router.post("/{project_id}/members", status_code=201, summary="Add or re-role a member")
@_guard(value_error=409)
def add_project_member(
    project_id: str,
    body: MemberBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(
        require_project_capability("project_id", "manage_collaborators")
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _workspace_user(db, ws.id, body.user_id)  # foreign/unknown user -> 404
    row = add_member(db, project.id, body.user_id, body.role, by_user=user.id)
    db.commit()
    _emit(ws.id, "PROJECT_MEMBER_ADDED",
          f"Member {body.user_id[:8]} added to '{project.name[:40]}' as {row.role}",
          user_id=user.id, project_id=project.id,
          member_user_id=body.user_id, role=row.role)
    return _member_dto(row)


@projects_router.delete("/{project_id}/members/{user_id}", summary="Remove a member (or leave)")
@_guard(value_error=409)
def remove_project_member(
    project_id: str,
    user_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = load_project(db, ws, project_id)  # foreign/missing -> 404
    if user_id != user.id:
        # self-leave needs no cap; removing somebody else needs OWNER
        _check(db, ws, user, capability="manage_collaborators", project_id=project.id)
    remove_member(db, project.id, user_id, by_user=user.id)
    db.commit()
    _emit(ws.id, "PROJECT_MEMBER_REMOVED",
          f"Member {user_id[:8]} removed from '{project.name[:40]}'",
          user_id=user.id, project_id=project.id, member_user_id=user_id)
    return {"ok": True, "user_id": user_id}


# ---------------------------------------------------------------------------
# endpoints -- targets
# ---------------------------------------------------------------------------


@projects_router.get("/{project_id}/targets", summary="Linked targets of a project")
@_guard()
def list_targets(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "edit_project")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(ProjectTarget)
        .where(ProjectTarget.project_id == project.id)
        .order_by(ProjectTarget.created_at)
    ).all()
    return {"items": [_target_dto(row) for row in rows]}


@projects_router.post("/{project_id}/targets", status_code=201, summary="Link a target")
@_guard()
def add_target(
    project_id: str,
    body: TargetBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "edit_project")),
    db: Session = Depends(get_db),
):
    # target must exist in THIS workspace else 404 (foreign ids stay 404)
    if not _target_exists(db, ws.id, body.target_type, body.target_id):
        raise HTTPException(status_code=404, detail="target not found")
    row = db.scalar(
        select(ProjectTarget).where(
            ProjectTarget.project_id == project.id,
            ProjectTarget.target_type == body.target_type,
            ProjectTarget.target_id == body.target_id,
        )
    )
    if row is None:  # idempotent: re-linking an existing edge returns it
        row = ProjectTarget(
            project_id=project.id,
            target_type=body.target_type,
            target_id=body.target_id,
        )
        db.add(row)
        db.flush()
    db.commit()
    return _target_dto(row)


@projects_router.delete(
    "/{project_id}/targets/{target_type}/{target_id}", summary="Unlink a target"
)
@_guard()
def remove_target(
    project_id: str,
    target_type: str,
    target_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "edit_project")),
    db: Session = Depends(get_db),
):
    row = db.scalar(
        select(ProjectTarget).where(
            ProjectTarget.project_id == project.id,
            ProjectTarget.target_type == target_type,
            ProjectTarget.target_id == target_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="target not found")
    db.delete(row)
    db.commit()
    return {"ok": True, "target_type": target_type, "target_id": target_id}


# ---------------------------------------------------------------------------
# endpoints -- ownership transfer
# ---------------------------------------------------------------------------


@projects_router.post("/{project_id}/transfer", summary="Transfer ownership (OWNER only)")
@_guard(value_error=409)
def transfer_ownership(
    project_id: str,
    body: TransferBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(
        require_project_capability("project_id", "manage_collaborators")
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _workspace_user(db, ws.id, body.to_user_id)  # foreign/unknown user -> 404
    target = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == body.to_user_id,
        )
    )
    if target is None:
        # not a project member at all -> same 404 wording as DELETE .../members
        # (the user may be a workspace member; the project membership is the
        # missing resource). A member with the wrong role is a CONFLICT -> 409.
        raise HTTPException(status_code=404, detail="member not found")
    if target.role not in ("OWNER", "ADMIN"):
        raise ValueError("target must be a project OWNER or ADMIN")
    previous_owners = [
        row
        for row in db.scalars(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id, ProjectMember.role == "OWNER"
            )
        ).all()
    ]
    if not (len(previous_owners) == 1 and previous_owners[0].user_id == body.to_user_id):
        # promote first so the last-owner rule can never strand the project
        add_member(db, project.id, body.to_user_id, "OWNER", by_user=user.id)
        for row in previous_owners:
            if row.user_id != body.to_user_id:
                row.role = "ADMIN"
        db.commit()
        _emit(ws.id, "PROJECT_TRANSFERRED",
              f"Ownership of '{project.name[:60]}' transferred to {body.to_user_id[:8]}",
              user_id=user.id, project_id=project.id, to_user_id=body.to_user_id)
    return _project_dto(project)


# ---------------------------------------------------------------------------
# endpoints -- duplication (Work 15.6 §9)
# ---------------------------------------------------------------------------
#
# Duplicate copies CONFIGURATION, not rows. It creates one Project row, copies an
# allowlist of settings through ``services.settings_reuse.duplicate_settings``,
# and links no targets -- a duplicated project pointing at the old project's
# content, campaign or timeline is the failure this route exists to prevent.
# It deliberately does not copy members either: membership is granted, not
# inherited, and a silent member copy would hand access to somebody who never
# accepted it.


@projects_router.post("/{project_id}/duplicate", status_code=201,
                      summary="Duplicate a project's settings into a new project")
@_guard()
def duplicate_project(
    project_id: str,
    body: DuplicateProjectBody | None = None,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "view_project")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.settings_reuse import (
        PROJECT_SETTINGS_KEY,
        duplicate_settings,
        load_project_settings,
        provenance_note,
        settings_fingerprint,
        store_project_settings,
    )

    body = body or DuplicateProjectBody()
    source_settings = load_project_settings(ws.settings_json or {}, project.id)
    copied, report = duplicate_settings(source_settings)

    name = (body.name or "").strip() or f"{str(project.name or 'Project')[:140]} (copy)"
    row = Project(
        workspace_id=ws.id,
        name=name[:160],
        description=(body.description if body.description.strip()
                     else str(project.description or ""))[:4000],
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    db.add(ProjectMember(project_id=row.id, user_id=user.id, role="OWNER"))

    # The copy carries a provenance note whose ids are explicitly non-shared, so
    # "where did this configuration come from" is answerable without any id in
    # the block being followable, editable, or writable through.
    copied = {**copied, "provenance": provenance_note(project.id)}
    ws.settings_json = store_project_settings(ws.settings_json or {}, row.id, copied)
    db.commit()
    _emit(ws.id, "PROJECT_DUPLICATED",
          f"Settings of '{project.name[:40]}' duplicated into '{row.name[:40]}'",
          user_id=user.id, project_id=row.id, source_project_id=project.id,
          copied_sections=report["copied_sections"],
          settings_fingerprint=settings_fingerprint(copied))
    return {
        "project": _project_dto(row),
        "settings": copied,
        "settings_report": report,
        "copied_targets": 0,
        "copied_members": 1,
        "settings_namespace": PROJECT_SETTINGS_KEY,
    }
