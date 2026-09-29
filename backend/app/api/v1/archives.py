"""Project archive API (Work 11 Lane L) -- contracts §10.

    POST /workspaces/{ws}/projects/{id}/archive            {mode} -> 201
    POST /workspaces/{ws}/projects/{id}/unarchive          -> 200
    GET  /workspaces/{ws}/projects/{id}/archives
    GET  /workspaces/{ws}/projects/{id}/archives/{aid}/download

RBAC: archiving/unarchiving are ``edit_project`` (an archived project
must be restorable by somebody who can edit it); listing and download
are ``view_project``. The workspace floor is the existing
``require_workspace_role`` dependency plus the project capability from
``services/project_auth`` -- a project role narrows, never widens.

**The 409 edit gate.** Archiving flips ``project.status`` to
``ARCHIVED``; ``api/v1/projects.py::update_project`` (lane F) answers
409 for an ARCHIVED project, so a snapshot really does freeze edits
until unarchive. Reads (detail, archives, download) keep working while
archived -- archiving is not deletion.

Isolation: every lookup goes through ``engine/archive.load_project`` /
``get_archive``, which scope by ``workspace_id`` and raise
``ArchiveError`` (mapped to 404) for a foreign id. Never 403.

Downloads stream the rebuilt bytes with an explicit media type and a
``Content-Disposition`` filename; the response is not cached publicly.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine import archive as archive_engine
from app.models import Project, ProjectArchive, User, Workspace
from app.services import activity as activity_service
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import require_project_capability

archives_router = APIRouter(
    prefix="/workspaces/{workspace_id}/projects", tags=["archives"]
)
logger = logging.getLogger("ymoney.collab")


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """HTTPException passes through; ArchiveError maps per ``status``."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except archive_engine.ArchiveError as exc:
            detail = " ".join(str(exc).split()) or "archive error"
            code = 409 if "not archived" in detail or "nothing to download" in detail else 404
            raise HTTPException(status_code=code, detail=detail) from None
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=" ".join(str(exc).split())[:180] or "invalid request"
            ) from None
        except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
            logger.exception("archive route failed: %s", getattr(fn, "__name__", fn))
            raise HTTPException(status_code=500, detail="internal error") from None

    return run


class ArchiveBody(BaseModel):
    mode: Literal["MANIFEST_ONLY", "PORTABLE_ARCHIVE"] = "PORTABLE_ARCHIVE"
    note: str = Field(default="", max_length=500)


@archives_router.post(
    "/{project_id}/archive", status_code=201, summary="Archive a project (freezes edits)"
)
@_guard
def archive_project(
    project_id: str,
    body: ArchiveBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "edit_project")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dto = archive_engine.create_archive(db, ws.id, project.id, mode=body.mode, user=user)
    db.commit()
    activity_service.emit(
        ws.id,
        "ARCHIVE_CREATED",
        message=f"Project '{project.name[:60]}' archived ({body.mode})",
        actor=user.id,
        target={"type": "project", "id": project.id},
        project_id=project.id,
        data={"mode": body.mode, "archive_id": dto["id"], "size_bytes": dto["size_bytes"],
              "checksum": dto["checksum"], "media_included": False},
    )
    return dto


@archives_router.post(
    "/{project_id}/unarchive", summary="Restore an archived project to ACTIVE"
)
@_guard
def unarchive_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "edit_project")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = archive_engine.unarchive(db, ws.id, project.id)
    db.commit()
    activity_service.emit(
        ws.id,
        "PROJECT_UPDATED",
        message=f"Project '{project.name[:60]}' unarchived",
        actor=user.id,
        target={"type": "project", "id": project.id},
        project_id=project.id,
        data={"status": "ACTIVE"},
    )
    return result


@archives_router.get(
    "/{project_id}/archives", summary="Archives of a project (newest first)"
)
@_guard
def list_archives(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "view_project")),
    db: Session = Depends(get_db),
):
    from sqlalchemy import select

    rows = db.scalars(
        select(ProjectArchive)
        .where(
            ProjectArchive.workspace_id == ws.id,
            ProjectArchive.project_id == project.id,
        )
        .order_by(ProjectArchive.created_at.desc())
    ).all()
    return {"items": [archive_engine.archive_dto(row) for row in rows]}


@archives_router.get(
    "/{project_id}/archives/{archive_id}/download",
    summary="Download an archive (JSON manifest or portable zip)",
)
@_guard
def download_archive(
    project_id: str,
    archive_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    project: Project = Depends(require_project_capability("project_id", "view_project")),
    db: Session = Depends(get_db),
):
    # project ownership is proven by the capability dependency above;
    # the archive row must additionally belong to THIS project.
    row = archive_engine.get_archive(db, ws.id, archive_id)
    if row.project_id != project.id:
        raise HTTPException(status_code=404, detail="archive not found")
    payload, filename, media_type = archive_engine.download_bytes(db, ws.id, archive_id)
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Checksum": row.checksum or "",
        },
    )


__all__ = ["ArchiveBody", "archives_router"]
