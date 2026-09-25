"""Long-form project endpoints (Work 03)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.engine.longform.pipeline import handle_stage_job
from app.models import LongFormChapter, LongFormProject, Workspace
from app.models.longform import AUTONOMY, BUDGETS, FORMATS
from app.services import jobs as jobs_service
from app.services.auth_service import require_workspace_role

longform_router = APIRouter(prefix="/workspaces/{workspace_id}/long-form", tags=["long-form"])


class ProjectCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=400)
    content_format: str = "EXPLAINER"
    target_duration_seconds: int = Field(default=600, ge=180, le=3600)
    target_audience: str = Field(default="", max_length=200)
    language: str = Field(default="en", max_length=20)
    tone: str = Field(default="confident, direct", max_length=60)
    aspect_ratio: str = "16:9"
    campaign_id: str | None = None
    autonomy: str = "AUTO"
    budget_strategy: str = "BALANCED"
    voice_name: str = Field(default="", max_length=120)
    pronunciation: dict = Field(default_factory=dict)


def _dto(p: LongFormProject) -> dict:
    return {
        "id": p.id, "workspace_id": p.workspace_id, "topic": p.topic,
        "content_format": p.content_format,
        "target_duration_seconds": p.target_duration_seconds,
        "target_audience": p.target_audience, "language": p.language,
        "tone": p.tone, "aspect_ratio": p.aspect_ratio,
        "campaign_id": p.campaign_id, "content_item_id": p.content_item_id,
        "timeline_id": p.timeline_id, "autonomy": p.autonomy,
        "budget_strategy": p.budget_strategy, "voice_name": p.voice_name,
        "stage": p.stage, "status": p.status,
        "progress": p.stage_progress_json or {},
        "cost_usd": round(p.cost_usd or 0.0, 4), "error": p.error or "",
        "created_at": p.created_at.isoformat() + "Z",
    }


def _get(ws_id: str, project_id: str, db) -> LongFormProject:
    row = db.get(LongFormProject, project_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="project not found")
    return row


@longform_router.post("/projects", summary="Create a long-form project")
def create_project(
    body: ProjectCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    if body.content_format not in FORMATS:
        raise HTTPException(status_code=422, detail=f"unknown format '{body.content_format}'")
    if body.autonomy not in AUTONOMY:
        raise HTTPException(status_code=422, detail=f"unknown autonomy '{body.autonomy}'")
    if body.budget_strategy not in BUDGETS:
        raise HTTPException(status_code=422, detail="unknown budget strategy")
    if body.aspect_ratio not in ("16:9", "9:16", "1:1"):
        raise HTTPException(status_code=422, detail="aspect must be 16:9, 9:16 or 1:1")
    row = LongFormProject(
        workspace_id=ws.id, topic=body.topic.strip(),
        content_format=body.content_format,
        target_duration_seconds=body.target_duration_seconds,
        target_audience=body.target_audience, language=body.language,
        tone=body.tone, aspect_ratio=body.aspect_ratio,
        campaign_id=body.campaign_id, autonomy=body.autonomy,
        budget_strategy=body.budget_strategy, voice_name=body.voice_name,
        pronunciation_json={str(k)[:60]: str(v)[:60]
                            for k, v in (body.pronunciation or {}).items()})
    db.add(row)
    db.commit()
    db.refresh(row)
    return _dto(row)


@longform_router.get("/projects", summary="List long-form projects")
def list_projects(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = db.scalars(select(LongFormProject).where(
        LongFormProject.workspace_id == ws.id
    ).order_by(LongFormProject.created_at.desc()).limit(50)).all()
    return {"total": len(rows), "items": [_dto(r) for r in rows]}


@longform_router.get("/projects/{project_id}", summary="Project detail + artifacts")
def get_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get(ws.id, project_id, db)
    chapters = db.scalars(select(LongFormChapter).where(
        LongFormChapter.project_id == row.id).order_by(LongFormChapter.index)).all()
    body = _dto(row)
    body["artifacts"] = {
        "strategy": row.strategy_json or {},
        "research": {"claims": len((row.research_json or {}).get("claims", [])),
                     "subtopics": (row.research_json or {}).get("subtopics", [])},
        "script": {"segments": (row.script_json or {}).get("segments_total", 0),
                   "words": (row.script_json or {}).get("words_total", 0)},
        "fact_report": row.fact_report_json or {},
        "voice": {"measured_seconds": (row.voice_json or {}).get("measured_seconds", 0),
                  "failed": (row.voice_json or {}).get("failed", [])},
        "qc": row.qc_json or {},
        "metadata": row.metadata_json or {},
        "render": row.render_json or {},
    }
    body["chapters"] = [{"id": c.id, "index": c.index, "title": c.title,
                         "role": c.narrative_role, "target_s": c.target_duration_seconds,
                         "measured": ((c.script_json or {}).get("measured_start"),
                                      (c.script_json or {}).get("measured_end")),
                         "segments": len((c.script_json or {}).get("segments", [])),
                         "status": c.status} for c in chapters]
    return body


@longform_router.get("/projects/{project_id}/estimate", summary="Cost estimate before execution")
def estimate_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get(ws.id, project_id, db)
    minutes = (row.target_duration_seconds or 600) / 60
    words = int(minutes * 150)
    chars = words * 6
    tts = chars * 0.0002
    per_image = 0.02 if row.budget_strategy != "ECONOMY" else 0.0
    images = 6 if row.budget_strategy == "PREMIUM" else (3 if row.budget_strategy == "BALANCED" else 0)
    return {"estimates_usd": {"tts": round(tts, 4), "llm": 0.05,
                              "images": round(images * per_image, 4),
                              "render_compute": 0.0},
            "assumptions": {"wpm": 150, "chars_per_word": 6,
                            "tts_usd_per_char": 0.0002,
                            "images_planned": images},
            "note": "Estimates only — actuals tracked per stage in cost_usd."}


@longform_router.post("/projects/{project_id}/generate", summary="Start the durable stage chain")
def generate_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    row = _get(ws.id, project_id, db)
    if row.status in ("RUNNING",):
        raise HTTPException(status_code=409, detail="project already running")
    row.status = "RUNNING"
    row.error = ""
    db.commit()
    # stage NEXT: the handler resolves the first pending stage, so retries
    # resume instead of restarting from RESEARCH
    job_id = jobs_service.enqueue(
        "longform.stage", {"project_id": row.id, "stage": "NEXT"},
        workspace_id=ws.id, priority=50,
        idempotency_key=f"longform-{row.id}-gen-{__import__('time').time_ns()}")
    return {"queued": job_id is not None, "job_id": job_id}


@longform_router.post("/projects/{project_id}/advance", summary="Advance one stage (MANUAL/REVIEW resume)")
def advance_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    _get(ws.id, project_id, db)
    job_id = jobs_service.enqueue(
        "longform.stage", {"project_id": project_id, "stage": "NEXT"},
        workspace_id=ws.id, priority=50,
        idempotency_key=f"longform-{project_id}-advance-{__import__('time').time_ns()}")
    return {"queued": job_id is not None, "job_id": job_id}


@longform_router.post("/projects/{project_id}/cancel", summary="Cancel a running project")
def cancel_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    row = _get(ws.id, project_id, db)
    row.status = "CANCELLED"
    db.commit()
    return {"status": "CANCELLED"}


@longform_router.get("/projects/{project_id}/progress", summary="Stage progress + cost")
def project_progress(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.engine.longform.pipeline import STAGES

    row = _get(ws.id, project_id, db)
    return {"project_id": row.id, "stage": row.stage, "status": row.status,
            "stages": STAGES, "units": row.stage_progress_json or {},
            "cost_usd": round(row.cost_usd or 0.0, 4), "error": row.error or ""}


class RegenerateBody(BaseModel):
    confirm: bool = False


@longform_router.post("/projects/{project_id}/repair", summary="One repair pass for missing media")
def repair_project(
    project_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    """Bounded repair: missing VISUAL clips get graphic fallbacks (recorded);
    missing narration fails loudly. Single pass — never a loop."""
    from app.engine.longform.repair import repair_timeline_assets

    row = _get(ws.id, project_id, db)
    try:
        report = repair_timeline_assets(db, row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return report


@longform_router.post("/projects/{project_id}/regenerate-timeline",
                      summary="Rebuild timeline (manual-edit protected)")
def regenerate_timeline(
    project_id: str,
    body: RegenerateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    """Rebuild the timeline from pipeline artifacts. When the timeline was
    manually edited after generation (version advanced), requires explicit
    confirm=true so human work is never silently destroyed."""
    from app.models import ContentTimeline

    row = _get(ws.id, project_id, db)
    if not row.timeline_id:
        raise HTTPException(status_code=409, detail="no timeline generated yet")
    tl = db.get(ContentTimeline, row.timeline_id)
    if tl is None or tl.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="timeline not found")
    generated_version = (row.render_json or {}).get("generated_timeline_version", 1)
    if int(tl.version or 1) != int(generated_version) and not body.confirm:
        raise HTTPException(status_code=409, detail={
            "error": "timeline was manually edited since generation",
            "timeline_version": tl.version, "generated_version": generated_version,
            "hint": "set confirm=true to rebuild (manual edits will be versioned, not deleted)",
        })
    from app.engine import timeline as tl_mod

    new_id = tl_mod.save_version(db, tl.id, label="pre-regenerate snapshot")
    db.commit()
    job_id = jobs_service.enqueue(
        "longform.stage", {"project_id": row.id, "stage": "TIMELINE"},
        workspace_id=ws.id, priority=50,
        idempotency_key=f"longform-{row.id}-regen-{__import__('time').time_ns()}")
    return {"queued": job_id is not None, "snapshot_version_id": new_id}


@jobs_service.handler("longform.stage")
def handle_longform_stage(ctx):
    payload = ctx.payload or {}
    project_id = payload.get("project_id", "")
    if not payload.get("stage") or payload.get("stage") == "NEXT":
        from app.db import session_scope
        from app.models import LongFormProject

        with session_scope() as s:
            row = s.get(LongFormProject, project_id)
            if row is None or row.workspace_id != ctx.workspace_id:
                return {"error": "project not found"}
            from app.engine.longform.pipeline import _pending_stage

            nxt = _pending_stage(s, row)
            if not nxt or nxt == "DONE":
                return {"done": True}
            ctx.payload = {**payload, "project_id": project_id, "stage": nxt}
    return handle_stage_job(ctx)
