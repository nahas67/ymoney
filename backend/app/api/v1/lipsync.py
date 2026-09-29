"""Lip-sync jobs + speaker-aware dubbing plans (Work 07 Lane B).

Endpoints (all workspace-scoped, 404 on cross-workspace access):

    POST /workspaces/{ws}/lipsync/jobs              submit a lip-sync job
    GET  /workspaces/{ws}/lipsync/jobs              list jobs
    GET  /workspaces/{ws}/lipsync/jobs/{id}         job detail
    POST /workspaces/{ws}/lipsync/jobs/{id}/cancel  cancel a queued/running job
    GET  /workspaces/{ws}/lipsync/health            provider + queue health
    POST /workspaces/{ws}/dubbing/plans             build/analyze a plan
    GET  /workspaces/{ws}/dubbing/plans             list saved plans

Submit fails closed (503 + remediation) when no lip-sync backend is ready;
health never requires a GPU.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.engine.dubbing.plan import PlanError, build_plan, fit_plan, list_plans, save_plan
from app.engine.lipsync import rows as job_rows
from app.engine.lipsync import service as lipsync_service
from app.models import Workspace
from app.services.auth_service import require_workspace_role

lipsync_router = APIRouter(prefix="/workspaces/{workspace_id}/lipsync", tags=["lipsync"])
dubbing_plans_router = APIRouter(prefix="/workspaces/{workspace_id}/dubbing/plans",
                                 tags=["dubbing"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LipSyncJobCreate(BaseModel):
    video_ref: str = Field(min_length=1, max_length=1024)
    audio_ref: str = Field(min_length=1, max_length=1024)
    provider: str = Field(default="", max_length=40)
    opts: dict = Field(default_factory=dict)


class PlanCueIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(default="", max_length=2000)
    index: int | None = None
    speaker_id: str = Field(default="", max_length=64)
    source_voice: str = Field(default="", max_length=200)
    target_voice: str = Field(default="", max_length=200)
    target_text: str = Field(default="", max_length=2000)


class DubbingPlanCreate(BaseModel):
    target_lang: str = Field(min_length=2, max_length=16)
    cues: list[PlanCueIn] = Field(min_length=1, max_length=2000)
    voice_map: dict[str, str] = Field(default_factory=dict)
    glossary: dict[str, str] = Field(default_factory=dict)
    pronunciation_rules: dict[str, str] = Field(default_factory=dict)
    source_ref: str = Field(default="", max_length=512)
    # measured audio seconds per cue index (JSON object keys are strings)
    durations: dict[str, float] = Field(default_factory=dict)
    analyze: bool = True


# ---------------------------------------------------------------------------
# Lip-sync jobs
# ---------------------------------------------------------------------------


def _raise_submit(exc: lipsync_service.SubmitRejected) -> None:
    raise HTTPException(
        status_code=exc.http_status,
        detail={"message": str(exc), "remediation": exc.remediation},
    )


@lipsync_router.post("/jobs", summary="Submit a lip-sync job (fails closed when unavailable)")
def submit_job(
    body: LipSyncJobCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        return lipsync_service.submit_job(
            db,
            ws.id,
            video_ref=body.video_ref,
            audio_ref=body.audio_ref,
            opts=body.opts,
            provider_name=body.provider,
        )
    except lipsync_service.SubmitRejected as exc:
        _raise_submit(exc)


@lipsync_router.get("/jobs", summary="List workspace lip-sync jobs")
def list_jobs(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    items = lipsync_service.list_jobs(db, ws.id)
    return {"total": len(items), "items": items}


@lipsync_router.get("/jobs/{job_id}", summary="Lip-sync job detail")
def get_job(
    job_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    try:
        row = lipsync_service.get_job(db, ws.id, job_id)
    except lipsync_service.JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job_rows.job_dto(row)


@lipsync_router.post("/jobs/{job_id}/cancel", summary="Cancel a queued/running job")
def cancel_job(
    job_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        return lipsync_service.cancel_job(db, ws.id, job_id)
    except lipsync_service.JobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except lipsync_service.JobStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@lipsync_router.get("/health", summary="Lip-sync provider + queue health")
def health(
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    return lipsync_service.health()


# ---------------------------------------------------------------------------
# Dubbing plans
# ---------------------------------------------------------------------------


def _plan_dto(row) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "source_ref": row.source_ref or "",
        "target_language": row.target_language or "",
        "status": row.status,
        "needs_review": bool(row.needs_review),
        "review_count": int(row.review_count or 0),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "plan": dict(row.plan_json or {}),
    }


@dubbing_plans_router.post("", summary="Build (and optionally analyze) a dubbing plan")
def create_plan(
    body: DubbingPlanCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        plan = build_plan(
            [c.model_dump() for c in body.cues],
            body.target_lang,
            body.voice_map,
            body.glossary,
            source_ref=body.source_ref,
            pronunciation_rules=body.pronunciation_rules,
        )
        if body.analyze and body.durations:
            durations: dict[int, float] = {}
            for key, value in body.durations.items():
                try:
                    durations[int(key)] = float(value)
                except (TypeError, ValueError) as exc:
                    raise PlanError(
                        f"durations key {key!r} must be a cue index (integer)"
                    ) from exc
            plan = fit_plan(plan, durations)
        row = save_plan(db, ws.id, plan, source_ref=body.source_ref)
    except PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "status": row.status,
        "needs_review": bool(row.needs_review),
        "summary": plan.summary(),
        "plan": plan.model_dump(mode="json"),
    }


@dubbing_plans_router.get("", summary="List saved dubbing plans")
def list_plans_route(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = list_plans(db, ws.id)
    return {"total": len(rows), "items": [_plan_dto(r) for r in rows]}


__all__ = ["dubbing_plans_router", "lipsync_router"]
