"""Export center API (Work 11 Lane X) -- contracts §11.

Canonical surface, mounted once in ``api/v1/__init__.py``::

    GET  /workspaces/{ws}/exports/formats            viewer
    GET  /workspaces/{ws}/exports/profiles           viewer
    POST /workspaces/{ws}/exports/profiles           admin
    PUT  /workspaces/{ws}/exports/profiles/{id}      admin (409 on a builtin)
    GET  /workspaces/{ws}/exports                    viewer
    POST /workspaces/{ws}/exports                    member + `export` cap
    GET  /workspaces/{ws}/exports/{export_id}        viewer
    POST /workspaces/{ws}/exports/{export_id}/retry  member
    POST /workspaces/{ws}/exports/{export_id}/cancel member
    GET  /workspaces/{ws}/exports/{export_id}/download  viewer

Floors are the existing ``require_workspace_role`` dependencies; the
project-scoped ``export`` capability comes from ``services/project_auth``
(contracts §3) and only NARROWS the floor, never widens it.

The **export row shape is LOCKED** for the frontend (contracts §11)::

    {id, format, profile: {name, preset}, target: {type, id}, state,
     progress, verification: {complete, checks} | null,
     artifact: {url, size, checksum} | null, error: str | null,
     attempt, created_at, finished_at}

``list_formats`` (``engine/exporter/formats.py``) is the ONLY source of
format availability exposed here -- the API never guesses a capability and
never reports a format as available that the probe did not confirm.

Error policy: short ``HTTPException`` details; domain ``ValueError`` maps to
422 (validation) or 404 (missing/foreign id) or 409 (state conflicts);
anything else is logged on ``ymoney.export`` and surfaced as a generic
``{"detail": "internal error"}`` that echoes nothing (``projects.py::_guard``
pattern; this lane's logger namespace is ``ymoney.export``). Every route is
workspace-scoped: a foreign id reads as 404, never 403.
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine.exporter import formats as exporter_formats
from app.engine.exporter import jobs as exporter_jobs
from app.engine.exporter import profiles as exporter_profiles
from app.engine.exporter import verify as exporter_verify
from app.models import ExportProfile, MediaAsset, User, Workspace
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.events import record_event
from app.services.project_auth import assert_capability

exports_router = APIRouter(prefix="/workspaces/{workspace_id}/exports",
                           tags=["exports"])
logger = logging.getLogger("ymoney.export")

#: target types the exporter can resolve (engine/exporter/jobs.py)
EXPORT_TARGET_TYPES: tuple[str, ...] = ("timeline", "video", "asset")


# ---------------------------------------------------------------------------
# error policy (contracts §11: short details, no exception echo)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors (never a stack)."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body.

    ``HTTPException`` passes through untouched (401/403/404/409/422);
    ``ValueError`` becomes ``value_error`` with a short detail; anything else
    is logged on ``ymoney.collab`` and surfaced as a generic 500.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error,
                                    detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception("exports route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """assert_capability at the route edge; bad args -> 422, 404/403 pass through."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class ProfileCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: Clone a builtin preset by name (config is then optional).
    preset: str | None = Field(default=None, max_length=32)
    #: Fully custom config; wins over ``preset`` when both are given.
    config: dict | None = None


class ProfileUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict | None = None


class ExportCreateBody(BaseModel):
    profile_id: str = Field(min_length=1, max_length=36)
    format: str = Field(min_length=1, max_length=16)
    target_type: Literal["timeline", "video", "asset"] = "timeline"
    target_id: str = Field(min_length=1, max_length=36)
    project_id: str | None = Field(default=None, max_length=36)


# ---------------------------------------------------------------------------
# DTOs -- the export row shape is LOCKED (contracts §11)
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return (value.isoformat() + "Z") if value else None


def _profile_brief(row: ExportProfile | None) -> dict:
    if row is None:
        return {"name": "", "preset": ""}
    return {"name": str(row.name or ""), "preset": str(row.preset or "")}


def _export_dto(db: Session, workspace_id: str, row, base: str) -> dict:
    """The contracts §11 export row. Keys and nesting are frontend-locked."""
    profile = db.get(ExportProfile, row.profile_id) if row.profile_id else None
    verification = row.verification_json if isinstance(row.verification_json, dict) else None
    verification_dto = None
    if verification:
        verification_dto = {
            "complete": bool(verification.get("complete")),
            "checks": list(verification.get("checks") or []),
        }
    artifact = None
    if row.artifact_asset_id:
        asset = db.get(MediaAsset, row.artifact_asset_id)
        if asset is not None and asset.workspace_id == workspace_id:
            size = int(asset.file_size or 0)
            artifact = {"url": f"{base}/{row.id}/download",
                        "size": size,
                        "checksum": str(row.checksum or asset.checksum or "")}
    return {
        "id": row.id,
        "format": str(row.format or ""),
        "profile": _profile_brief(profile),
        "target": {"type": str(row.target_type or ""), "id": str(row.target_id or "")},
        "state": str(row.state or "QUEUED"),
        "progress": int(row.progress or 0),
        "verification": verification_dto,
        "artifact": artifact,
        "error": (str(row.error) if row.error else None),
        "attempt": int(row.attempt or 0),
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
    }


def _profile_dto(row: ExportProfile) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": str(row.name or ""),
        "preset": str(row.preset or ""),
        "config": dict(row.config_json or {}),
        "is_builtin": bool(row.is_builtin),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


# ---------------------------------------------------------------------------
# endpoints -- formats + profiles
# ---------------------------------------------------------------------------


@exports_router.get("/formats", summary="Format availability (honest probe)")
@_guard()
def list_export_formats(
    target_type: str | None = Query(default=None, max_length=32),
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    # `list_formats` is the ONLY source of availability the API exposes.
    return {"items": exporter_formats.list_formats(target_type=target_type)}


@exports_router.get("/profiles", summary="Builtin + workspace export profiles")
@_guard()
def list_export_profiles(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    exporter_profiles.seed_builtins(db)  # lazy first-GET seeding (contracts §11)
    db.commit()
    rows = exporter_profiles.list_profiles(db, ws.id)
    return {"items": [_profile_dto(row) for row in rows]}


@exports_router.post("/profiles", status_code=201, summary="Create a profile")
@_guard()
def create_export_profile(
    body: ProfileCreateBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = exporter_profiles.create_profile(
        db, ws.id, name=body.name, preset=body.preset, config=body.config,
        created_by=user.id)
    db.commit()
    return _profile_dto(row)


@exports_router.put("/profiles/{profile_id}", summary="Update a workspace profile")
@_guard(value_error=409)
def update_export_profile(
    profile_id: str,
    body: ProfileUpdateBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    try:
        row = exporter_profiles.load_profile(db, ws.id, profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="profile not found") from None
    if row.is_builtin:
        # Builtins are global rows shared by every workspace: POST a clone
        # from the preset instead of mutating everybody's config.
        raise HTTPException(
            status_code=409,
            detail="builtin profiles are read-only; POST a new profile cloning this preset")
    # Config validation is a 422, not the 409 the guard maps other ValueErrors
    # to -- a malformed override must read as "bad input", not "conflict".
    try:
        exporter_profiles.update_profile(db, row, name=body.name, config=body.config)
    except exporter_profiles.ExportValidationError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None
    db.commit()
    return _profile_dto(row)


# ---------------------------------------------------------------------------
# endpoints -- exports
# ---------------------------------------------------------------------------


@exports_router.post("", status_code=201, summary="Queue an export")
@_guard()
def create_export(
    body: ExportCreateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.target_type not in EXPORT_TARGET_TYPES:
        raise HTTPException(status_code=422, detail="unsupported target_type")
    # project-scoped exports narrow on the `export` capability; unlinked
    # targets fall through to this route's member floor (contracts §3).
    _check(db, ws, user, capability="export", project_id=body.project_id,
           target_type=body.target_type, target_id=body.target_id)
    try:
        result = exporter_jobs.enqueue_export(
            db, ws.id, profile_id=body.profile_id, fmt=body.format,
            target_type=body.target_type, target_id=body.target_id,
            user_id=user.id)
    except exporter_jobs.ExportJobError as exc:
        message = _short(exc)
        status = 404 if "not found" in message else 422
        raise HTTPException(status_code=status, detail=message) from None
    db.commit()
    _emit(ws.id, "EXPORT_CREATED",
          f"Export {result['export_id'][:8]} queued as {body.format.upper()}",
          user_id=user.id, export_id=result["export_id"], format=body.format,
          target={"type": body.target_type, "id": body.target_id},
          project_id=body.project_id)
    return result


@exports_router.get("", summary="List exports")
@_guard()
def list_exports(
    state: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    base = f"/api/v1/workspaces/{ws.id}/exports"
    rows = exporter_jobs.list_exports(db, ws.id, state=state, limit=limit)
    return {"items": [_export_dto(db, ws.id, row, base) for row in rows]}


@exports_router.get("/{export_id}", summary="Export detail (state + verification)")
@_guard()
def get_export(
    export_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        row = exporter_jobs.get_export(db, ws.id, export_id)
    except exporter_jobs.ExportJobError:
        raise HTTPException(status_code=404, detail="export not found") from None
    base = f"/api/v1/workspaces/{ws.id}/exports"
    return _export_dto(db, ws.id, row, base)


@exports_router.post("/{export_id}/retry", summary="Retry a FAILED/CANCELLED export")
@_guard(value_error=409)
def retry_export(
    export_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        row = exporter_jobs.get_export(db, ws.id, export_id)
    except exporter_jobs.ExportJobError:
        raise HTTPException(status_code=404, detail="export not found") from None
    _check(db, ws, user, capability="export", target_type=row.target_type,
           target_id=row.target_id)
    return exporter_jobs.retry_export(db, ws.id, row.id)


@exports_router.post("/{export_id}/cancel", summary="Cancel a QUEUED/RUNNING export")
@_guard(value_error=409)
def cancel_export(
    export_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        row = exporter_jobs.get_export(db, ws.id, export_id)
    except exporter_jobs.ExportJobError:
        raise HTTPException(status_code=404, detail="export not found") from None
    _check(db, ws, user, capability="export", target_type=row.target_type,
           target_id=row.target_id)
    return exporter_jobs.cancel_export(db, ws.id, row.id)


@exports_router.get("/{export_id}/download", summary="Stream the export artifact")
@_guard()
def download_export(
    export_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        row = exporter_jobs.get_export(db, ws.id, export_id)
    except exporter_jobs.ExportJobError:
        raise HTTPException(status_code=404, detail="export not found") from None
    asset = db.get(MediaAsset, row.artifact_asset_id) if row.artifact_asset_id else None
    if asset is None or asset.workspace_id != ws.id or not asset.storage_key:
        raise HTTPException(status_code=404, detail="artifact not found")
    path = exporter_verify.resolve_artifact_path(ws.id, asset.storage_key)
    if path is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    spec = exporter_formats.get_format(row.format)
    filename = path.name
    return FileResponse(
        path, media_type=spec.media_type, filename=filename,
        headers={"X-Content-SHA256": str(row.checksum or asset.checksum or "")},
    )


# ---------------------------------------------------------------------------
# activity ledger + job handler registration
# ---------------------------------------------------------------------------


def _emit(ws_id: str, kind: str, message: str, *, user_id: str, **extra) -> None:
    """Activity ledger event (contracts §9 kinds)."""
    data: dict[str, Any] = {"actor": user_id}
    data.update(extra)
    record_event(ws_id, kind, message, level="info", source="exports", data=data)


def _bootstrap_export_jobs() -> None:
    """Register the EXPORT_BUILD handler idempotently (contracts §11).

    Mirrors ``api/v1/knowledge.py::_bootstrap_source_jobs``: the target is
    imported and invoked inside its own guard, so one missing or half-landed
    sibling module can never break the API import. ``register_export_jobs``
    skips duplicates, so re-running this is safe.
    """
    for _mod, _fn in (
        ("app.engine.exporter.jobs", "register_export_jobs"),
    ):
        with suppress(Exception):  # noqa: S110 - sibling lanes land in parallel
            _module = importlib.import_module(_mod)
            getattr(_module, _fn)()


_bootstrap_export_jobs()

__all__ = ["exports_router"]
