"""UGC projects + custom avatars (Work 07 Lane C).

Routes (every one workspace-scoped — a row from another workspace is 404,
never a hint that it exists):

    POST   /workspaces/{ws}/ugc/projects              create + run a project
    GET    /workspaces/{ws}/ugc/projects              list workspace projects
    GET    /workspaces/{ws}/ugc/projects/{id}         project detail (QC/lineage)
    POST   /workspaces/{ws}/ugc/projects/{id}/render  render (QC-gated)
    GET    /workspaces/{ws}/ugc/presets               the nine registered presets
    POST   /workspaces/{ws}/avatars                   create profile (consent pending)
    GET    /workspaces/{ws}/avatars                   list profiles
    GET    /workspaces/{ws}/avatars/{id}              profile detail
    POST   /workspaces/{ws}/avatars/{id}/authorize    consent → authorized (evidence)
    POST   /workspaces/{ws}/avatars/render            consent-gated render
    GET    /workspaces/{ws}/avatars/health            provider health + consent policy

Safety rails enforced here (not left to the UI):
  * unknown preset → 422 listing the registered presets.
  * `POST /avatars/render` rejects `consent_state != authorized` with 403
    BEFORE any provider/backend work (`AvatarConsentError` → 403).
  * rendering a project blocked by QC refuses with 409 (never silently
    ships an unreviewed cut).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.engine.avatar import AvatarConsentError, avatar_health
from app.engine.avatar.service import (
    AvatarServiceError,
    authorize_profile,
    create_profile,
    get_profile,
    list_profiles,
    profile_dto,
    render_profile_output,
)
from app.engine.ugc import (
    PRESET_DEFAULTS,
    UGC_PRESETS,
    UGCBlockedError,
    UGCError,
    UGCVideoPipeline,
)
from app.engine.ugc.assets import resolve_product_assets
from app.models import UgcProjectRow, Workspace
from app.providers.avatar import AvatarError
from app.services.auth_service import require_workspace_role

ugc_router = APIRouter(prefix="/workspaces/{workspace_id}/ugc", tags=["ugc"])
avatars_router = APIRouter(prefix="/workspaces/{workspace_id}/avatars", tags=["avatars"])


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class UgcProjectCreate(BaseModel):
    preset: str = Field(min_length=1, max_length=40)
    brief: dict = Field(default_factory=dict)
    run: bool = True                     # False → save a DRAFT without running


class UgcRenderBody(BaseModel):
    out_name: str = Field(default="ugc.mp4", max_length=120)


class AvatarCreate(BaseModel):
    name: str = Field(default="", max_length=160)
    source_asset_ref: str = Field(min_length=1, max_length=1024)
    voice_ref: str = Field(default="", max_length=1024)
    expression_preset: str = "neutral"
    motion_preset: str = "subtle"
    framing: str = "medium_closeup"
    background: str = Field(default="studio", max_length=80)
    language: str = Field(default="en", max_length=10)
    brand_association: str = Field(default="", max_length=120)
    provider: str = Field(default="", max_length=40)


class AvatarAuthorize(BaseModel):
    source: str = Field(min_length=1, max_length=400)
    authorization_evidence: dict = Field(default_factory=dict)
    granted_by: str = Field(default="", max_length=200)
    statement: str = Field(default="", max_length=1000)


class AvatarRender(BaseModel):
    profile_id: str = Field(min_length=1, max_length=36)
    audio_ref: str = Field(min_length=1, max_length=1024)
    opts: dict = Field(default_factory=dict)
    timeline: bool = True


# ---------------------------------------------------------------------------
# dto helpers
# ---------------------------------------------------------------------------


def _project_dto(row: UgcProjectRow) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "preset": row.preset,
        "status": row.status,
        "brief": dict(row.brief_json or {}),
        "timeline_id": row.timeline_id or "",
        "render_asset_ref": row.render_asset_ref or "",
        "qc": dict(row.qc_json or {}),
        "lineage": dict(row.lineage_json or {}),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else "",
    }


def _get_project(ws_id: str, project_id: str, db) -> UgcProjectRow:
    row = db.get(UgcProjectRow, project_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="ugc project not found")
    return row


# ---------------------------------------------------------------------------
# UGC projects
# ---------------------------------------------------------------------------


@ugc_router.get("/presets", summary="The nine registered UGC presets")
def list_presets(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    items = [{"key": key, **dict(PRESET_DEFAULTS.get(key) or {})}
             for key in UGC_PRESETS]
    return {"total": len(items), "items": items, "workspace_id": ws.id}


@ugc_router.post("/projects", summary="Create (and run) a UGC project")
def create_project(
    body: UgcProjectCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    if body.preset not in UGC_PRESETS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown UGC preset '{body.preset}' "
                   f"(expected one of {', '.join(UGC_PRESETS)})")
    if not isinstance(body.brief, dict):
        raise HTTPException(status_code=422, detail="brief must be an object")

    row = UgcProjectRow(workspace_id=ws.id, preset=body.preset,
                        brief_json=dict(body.brief), status="DRAFT")
    db.add(row)
    db.flush()  # id before the pipeline stages run
    # Stage commit: the pipeline fires LLM/voice work whose cost recording
    # writes from a second connection — holding this write transaction open
    # across those calls deadlocks SQLite (5s busy-wait per attempt).
    db.commit()

    if not body.run:
        return {"project": _project_dto(row), "ran": False}

    pipeline = UGCVideoPipeline(db, ws.id, row)
    try:
        result = pipeline.run(render=False)
    except UGCError as exc:
        db.commit()  # status=FAILED + lineage.error already written by run()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)

    from app.services.events import record_event

    record_event(
        ws.id, "ugc.project_created",
        f"UGC project '{row.preset}' generated (QC {result['qc']['status']})",
        level="info" if result["qc"]["status"] in ("PASS", "PASS_WITH_WARNINGS")
        else "warning",
        source="studio",
        data={"project_id": row.id, "preset": row.preset,
              "timeline_id": result.get("timeline_id"),
              "qc_status": result["qc"]["status"]})
    return {"project": _project_dto(row), "ran": True,
            "timeline_id": result.get("timeline_id"),
            "qc": result["qc"], "script": result.get("script", "")}


@ugc_router.get("/projects", summary="List workspace UGC projects")
def list_projects(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = db.scalars(
        select(UgcProjectRow)
        .where(UgcProjectRow.workspace_id == ws.id)
        .order_by(UgcProjectRow.created_at.desc())
        .limit(100)
    ).all()
    return {"total": len(rows), "items": [_project_dto(r) for r in rows]}


@ugc_router.get("/projects/{project_id}", summary="UGC project detail (QC + lineage)")
def get_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get_project(ws.id, project_id, db)
    out = _project_dto(row)
    resolved, unresolved = resolve_product_assets(
        db, ws.id, (row.brief_json or {}).get("product_assets") or [])
    out["product_assets"] = {"resolved": resolved, "unresolved": unresolved}
    out["open_in_editor"] = bool(row.timeline_id)
    return out


@ugc_router.post("/projects/{project_id}/render",
                 summary="Render the generated timeline (QC-gated)")
def render_project(
    project_id: str,
    body: UgcRenderBody | None = None,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    row = _get_project(ws.id, project_id, db)
    pipeline = UGCVideoPipeline(db, ws.id, row)
    try:
        out = pipeline.render(out_name=(body.out_name if body else "ugc.mp4")
                              or "ugc.mp4")
    except UGCBlockedError as exc:
        db.commit()  # the QC gate updated status/qc — keep the refusal durable
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UGCError as exc:
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()

    from app.services.events import record_event

    record_event(ws.id, "ugc.rendered",
                 f"UGC project '{row.preset}' rendered",
                 level="success", source="studio",
                 data={"project_id": row.id, "asset_id": out.get("asset_id"),
                       "timeline_id": out.get("timeline_id")})
    return {"project": _project_dto(row), "render": out}


# ---------------------------------------------------------------------------
# avatars (static routes first so /health and /render are never shadowed)
# ---------------------------------------------------------------------------


@avatars_router.get("/health", summary="Avatar provider health + consent policy")
def get_avatar_health(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    health = avatar_health()
    health["workspace_id"] = ws.id
    return health


@avatars_router.post("/render", summary="Render an avatar (authorized consent only)")
def render_avatar_route(
    body: AvatarRender,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        get_profile(db, ws.id, body.profile_id)
    except AvatarServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return render_profile_output(
            db, ws.id, body.profile_id,
            audio_ref=body.audio_ref, opts=dict(body.opts),
            timeline=bool(body.timeline))
    except AvatarConsentError as exc:
        # consent_state != authorized → reject BEFORE any provider/backend work
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AvatarServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AvatarError as exc:
        # backend not configured/ready → 503 with remediation text
        raise HTTPException(
            status_code=503,
            detail=f"{exc} — configure an avatar backend "
                   f"(Settings → Connections) before rendering") from exc


@avatars_router.post("", summary="Create an avatar profile (consent starts pending)")
def create_avatar(
    body: AvatarCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        row = create_profile(db, ws.id, body.model_dump())
    except AvatarServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)

    from app.services.events import record_event

    record_event(ws.id, "avatar.created",
                 f"Avatar profile '{row.name or row.id}' created (consent pending)",
                 level="info", source="studio",
                 data={"profile_id": row.id})
    return profile_dto(row)


@avatars_router.get("", summary="List workspace avatar profiles")
def list_avatars(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = list_profiles(db, ws.id)
    return {"total": len(rows), "items": [profile_dto(r) for r in rows]}


@avatars_router.get("/{profile_id}", summary="Avatar profile detail")
def get_avatar(
    profile_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    try:
        row = get_profile(db, ws.id, profile_id)
    except AvatarServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return profile_dto(row)


@avatars_router.post("/{profile_id}/authorize",
                     summary="Authorize a portrait (source + evidence required)")
def authorize_avatar(
    profile_id: str,
    body: AvatarAuthorize,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        get_profile(db, ws.id, profile_id)
    except AvatarServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        row = authorize_profile(
            db, ws.id, profile_id,
            source=body.source,
            authorization_evidence=dict(body.authorization_evidence),
            granted_by=body.granted_by, statement=body.statement)
    except AvatarServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()

    from app.services.events import record_event

    record_event(ws.id, "avatar.authorized",
                 f"Avatar profile '{row.name or profile_id}' authorized for rendering",
                 level="warning", source="studio",
                 data={"profile_id": profile_id})
    return profile_dto(row)


__all__ = ["avatars_router", "ugc_router"]
