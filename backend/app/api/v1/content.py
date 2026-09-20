"""Opportunity + content + video + campaign + calendar endpoints."""

from __future__ import annotations

import time
from datetime import datetime, timezone, UTC

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.db import get_db
from app.models import (
    Campaign,
    ContentItem,
    Cycle,
    Job,
    Opportunity,
    QualityCheck,
    ScheduleEntry,
    Video,
    VideoVariant,
    Workspace,
)
from app.services.auth_service import require_workspace_role

router = APIRouter(prefix="/workspaces/{workspace_id}/opportunities", tags=["opportunities"])


@router.get("", summary="List opportunities with explainable scores")
def list_opportunities(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = select(Opportunity).where(Opportunity.workspace_id == ws.id).order_by(Opportunity.score.desc())
    count_q = select(func.count()).select_from(Opportunity).where(Opportunity.workspace_id == ws.id)
    if status == "selected":
        q = q.where(Opportunity.selected.is_(True))
        count_q = count_q.where(Opportunity.selected.is_(True))
    elif status == "skipped":
        skipped = Opportunity.skipped_reason != "", Opportunity.skipped_reason.is_not(None)
        q = q.where(*skipped)
        count_q = count_q.where(*skipped)
    elif status == "available":
        available = Opportunity.selected.is_(False), func.coalesce(Opportunity.skipped_reason, "") == ""
        q = q.where(*available)
        count_q = count_q.where(*available)
    total = db.scalar(count_q)
    rows = db.scalars(q.offset(offset).limit(limit)).all()
    items = []
    for o in rows:
        raw = o.raw_payload or {}
        items.append(
            {
                "id": o.id,
                "topic": o.topic,
                "source": o.source,
                "score": o.score,
                "components": o.components_json or {},
                "recommendation": o.recommendation,
                "lifecycle": o.lifecycle or "UNKNOWN",
                "confidence": o.confidence,
                "virality": getattr(o, "virality", 0.0) or 0.0,
                "selected": o.selected,
                "skipped_reason": o.skipped_reason or "",
                # discovery metadata for the Trend Center (None when absent)
                "source_url": raw.get("_source_url") or raw.get("url") or None,
                "velocity": raw.get("_velocity_hint"),
                "volume": raw.get("_volume_hint"),
                "created_at": o.created_at.isoformat() + "Z",
            }
        )
    return {"total": total or 0, "items": items}


@router.post("/{opportunity_id}/select", summary="Manually select an opportunity for production")
def select_opportunity(
    opportunity_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    opp = db.get(Opportunity, opportunity_id)
    if not opp or opp.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="opportunity not found")
    if opp.selected:
        raise HTTPException(status_code=409, detail="opportunity already selected")
    if opp.skipped_reason:
        raise HTTPException(status_code=409, detail="opportunity was skipped")
    content = ContentItem(workspace_id=ws.id, opportunity_id=opp.id, topic=opp.topic, status="IDEA")
    opp.selected = True
    db.add(content)
    db.commit()
    return {"content_id": content.id}


@router.post("/{opportunity_id}/skip", summary="Skip an opportunity")
def skip_opportunity(
    opportunity_id: str,
    body: SkipBody | None = None,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    opp = db.get(Opportunity, opportunity_id)
    if not opp or opp.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="opportunity not found")
    if opp.selected:
        raise HTTPException(status_code=409, detail="opportunity already selected")
    opp.skipped_reason = (body.reason if body else "") or "skipped by user"
    db.commit()
    return {"skipped": True}


class SkipBody(BaseModel):
    reason: str = Field(default="", max_length=300)


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

content_router = APIRouter(prefix="/workspaces/{workspace_id}/content", tags=["content"])


@content_router.get("", summary="Search/filter the content library")
def list_content(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
    status: str | None = None,
    campaign_id: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    q = select(ContentItem).where(ContentItem.workspace_id == ws.id).order_by(ContentItem.created_at.desc())
    count_q = select(func.count()).select_from(ContentItem).where(ContentItem.workspace_id == ws.id)
    if status:
        q = q.where(ContentItem.status == status.upper())
        count_q = count_q.where(ContentItem.status == status.upper())
    if campaign_id:
        q = q.where(ContentItem.campaign_id == campaign_id)
        count_q = count_q.where(ContentItem.campaign_id == campaign_id)
    if search:
        like = f"%{search.lower()}%"
        q = q.where(func.lower(ContentItem.topic).like(like))
        count_q = count_q.where(func.lower(ContentItem.topic).like(like))
    total = db.scalar(count_q)
    rows = db.scalars(q.offset(offset).limit(limit)).all()
    return {
        "total": total or 0,
        "items": [_serialize_content(db, c) for c in rows],
    }


def _serialize_content(db, c: ContentItem) -> dict:
    video_row = None
    best_variant = db.scalar(
        select(VideoVariant).where(VideoVariant.content_item_id == c.id, VideoVariant.selected.is_(True))
    )
    if best_variant:
        v = db.scalar(select(Video).where(Video.variant_id == best_variant.id))
        qc = db.scalar(
            select(QualityCheck).where(QualityCheck.video_id == v.id).order_by(QualityCheck.created_at.desc())
        ) if v else None
        video_row = {
            "id": v.id,
            "status": v.status,
            "engine": v.engine,
            "file_path": v.file_path,
                "thumbnail_path": getattr(v, "thumbnail_path", "") or "",
            "progress": v.progress or 0,
            "duration_seconds": v.duration_seconds,
            "error": (v.error or "")[:200],
            "quality": qc.overall if qc else None,
            "quality_passed": qc.passed if qc else None,
            "quality_notes": qc.notes if qc else "",
            "quality_components": qc.components_json or {} if qc else {},
            "aspect_ratio": v.aspect_ratio,
        } if v else None
    return {
        "id": c.id,
        "topic": c.topic,
        "status": c.status,
        "campaign_id": c.campaign_id,
        "cycle_id": c.cycle_id,
        "strategy": c.strategy_json or {},
        "error": c.error,
        "video": video_row,
        "variants_count": db.scalar(select(func.count()).select_from(VideoVariant).where(VideoVariant.content_item_id == c.id)) or 0,
        "created_at": c.created_at.isoformat() + "Z",
    }


@content_router.get("/{content_id}", summary="Full content item detail")
def content_detail(content_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    c = db.get(ContentItem, content_id)
    if not c or c.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="content not found")
    variants = db.scalars(select(VideoVariant).where(VideoVariant.content_item_id == content_id)).all()
    out = _serialize_content(db, c)
    out["research"] = c.research_json or {}
    out["tags"] = c.tags_json or []
    out["variants"] = [
        {
            "id": v.id,
            "label": v.label,
            "hook": v.hook[:200],
            "script": v.script,
            "predicted_score": v.predicted_score,
            "selected": v.selected,
            "metadata": v.metadata_json or {},
        }
        for v in variants
    ]
    return out


class ContentActionBody(BaseModel):
    action: str = Field(pattern="^(approve|reject|retry|skip)$")
    reason: str = Field(default="", max_length=500)


@content_router.get("/{content_id}/timeline", summary="Chronological event history for this item")
def content_timeline(content_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    from app.models import EventLog, Job, QualityCheck

    c = db.get(ContentItem, content_id)
    if not c or c.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="content not found")

    timeline: list[dict] = []

    def add(ts, kind, label, detail=""):
        if ts:
            timeline.append({
                "at": ts.isoformat() + "Z",
                "kind": kind,
                "label": label,
                "detail": detail[:200],
            })

    add(c.created_at, "idea", "Idea created", c.topic)
    opp = db.get(Opportunity, c.opportunity_id) if c.opportunity_id else None
    if opp:
        add(opp.created_at, "trend_selected", f"Trend selected ({opp.source})",
            f"score {opp.score:.0f} · {opp.lifecycle}")

    events = db.scalars(
        select(EventLog).where(EventLog.workspace_id == ws.id).order_by(EventLog.created_at.desc()).limit(400)
    ).all()
    for e in events:
        data = e.data_json or {}
        if data.get("content_id") == content_id:
            add(e.created_at, e.kind, e.message)

    jobs = db.scalars(
        select(Job).where(Job.cycle_id == c.cycle_id).order_by(Job.created_at.asc()).limit(100)
    ).all() if c.cycle_id else []
    for j in jobs:
        if j.status in ("COMPLETED", "DEAD") and j.completed_at:
            detail = (j.result or {}).get("duration_ms")
            label = f"{j.type} {'completed' if j.status == 'COMPLETED' else 'failed permanently'}"
            add(j.completed_at, f"job.{j.type}", label,
                f"{detail}ms" if detail else (j.last_error or "")[:120])

    variant = db.scalar(
        select(VideoVariant).where(VideoVariant.content_item_id == content_id, VideoVariant.selected.is_(True))
    )
    if variant:
        video = db.scalar(select(Video).where(Video.variant_id == variant.id))
        if video:
            add(video.created_at, "rendering", "Video rendered",
                f"engine={video.engine} · {video.aspect_ratio}")
            for qc in db.scalars(
                select(QualityCheck).where(QualityCheck.video_id == video.id).order_by(QualityCheck.created_at.asc())
            ).all():
                add(qc.created_at, "qc", "Quality check",
                    f"{'PASSED' if qc.passed else 'REJECTED'} at {qc.overall:.0f}/100")

    timeline.sort(key=lambda t: t["at"])
    return {"items": timeline}


@content_router.post("/{content_id}/actions", summary="Human override actions")
def content_action(
    content_id: str,
    body: ContentActionBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.models.base import can_transition

    c = db.get(ContentItem, content_id)
    if not c or c.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="content not found")
    targets = {"approve": "APPROVED", "retry": "PRODUCTION", "skip": "SKIPPED"}
    if body.action == "approve":
        target = "APPROVED" if c.status == "QC" else ("PUBLISHED" if c.status == "SCHEDULED" else "APPROVED")
    else:
        target = targets[body.action]
    if not can_transition(c.status, target):
        raise HTTPException(status_code=409, detail=f"cannot {body.action} from status {c.status}")
    c.status = target
    if body.action == "retry":
        c.error = ""
    db.commit()
    return {"status": c.status}


# ---------------------------------------------------------------------------
# Cost estimation (Sprint 1 #5: cost chip on generate/schedule actions)
# ---------------------------------------------------------------------------


class EstimateBody(BaseModel):
    topic: str = Field(default="", max_length=300)
    script: str = Field(default="", max_length=20000)
    video_count: int = Field(default=1, ge=1, le=5)


@content_router.post("/estimate-cost", summary="Estimate render cost for a would-be video")
def estimate_cost(body: EstimateBody, ws: Workspace = Depends(require_workspace_role("viewer"))):
    """Engine-aware flat estimate (MPT exposes no monetary cost, so the
    estimate is the configured per-render value times video_count). Safe on
    any engine: unreachable engines still return the configuration estimate.
    """
    from app.core.config import settings as app_settings
    from app.providers.video_engine.base import RenderRequest
    from app.providers.video_engine.factory import get_video_engine
    from app.services import provider_settings as ps

    req = RenderRequest(
        subject=body.topic or "estimate",
        script=body.script or "estimate",
        video_count=body.video_count,
        workspace_id=ws.id,
    )
    per_render = app_settings.mpt_estimated_render_cost_usd
    try:
        with ps.workspace_scope(ws.id):
            engine = get_video_engine(ws.id)
            per_render = float(engine.estimate_cost(req))
    except Exception:
        pass  # configuration estimate is the honest floor
    return {
        "per_video_usd": round(per_render, 4),
        "video_count": body.video_count,
        "total_usd": round(per_render * body.video_count, 4),
        "engine": app_settings.video_engine,
        "is_estimate": True,
    }


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

videos_router = APIRouter(prefix="/workspaces/{workspace_id}/videos", tags=["videos"])


@videos_router.get("", summary="List rendered videos")
def list_videos(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db), limit: int = Query(default=50, le=200)):
    rows = db.scalars(
        select(Video).where(Video.workspace_id == ws.id).order_by(Video.created_at.desc()).limit(limit)
    ).all()
    items = []
    for v in rows:
        qc = db.scalar(select(QualityCheck).where(QualityCheck.video_id == v.id).order_by(QualityCheck.created_at.desc()))
        items.append(
            {
                "id": v.id,
                "engine": v.engine,
                "status": v.status,
                "file_path": v.file_path,
                "thumbnail_path": getattr(v, "thumbnail_path", "") or "",
                "aspect_ratio": v.aspect_ratio,
                "resolution": v.resolution,
                "quality": qc.overall if qc else None,
                "passed": qc.passed if qc else None,
                "quality_notes": qc.notes if qc else "",
                "quality_components": qc.components_json or {} if qc else {},
                "created_at": v.created_at.isoformat() + "Z",
            }
        )
    return {"items": items}


@videos_router.get("/{video_id}", summary="Get one video incl. quality checks")
def get_video(video_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    v = db.get(Video, video_id)
    if not v or v.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="video not found")
    checks = db.scalars(select(QualityCheck).where(QualityCheck.video_id == v.id)).all()
    return {
        "id": v.id,
        "engine": v.engine,
        "engine_task_id": v.engine_task_id,
        "status": v.status,
        "file_path": v.file_path,
                "thumbnail_path": getattr(v, "thumbnail_path", "") or "",
        "aspect_ratio": v.aspect_ratio,
        "resolution": v.resolution,
        "params": v.params_json or {},
        "error": v.error,
        "quality_checks": [
            {
                "overall": q.overall,
                "passed": q.passed,
                "components": q.components_json or {},
                "notes": q.notes or "",
                "reviewer": q.reviewer,
                "created_at": q.created_at.isoformat() + "Z",
            }
            for q in checks
        ],
        "created_at": v.created_at.isoformat() + "Z",
    }


@videos_router.get("/{video_id}/thumbnail", summary="Poster frame for this video")
def video_thumbnail(video_id: str, request: Request, workspace_id: str, token: str | None = None, db=Depends(get_db)):

    from fastapi.responses import FileResponse

    from app.services.auth_service import resolve_workspace

    ws = resolve_workspace(request, db, workspace_id, token)
    v = db.get(Video, video_id)
    if not v or v.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="video not found")
    from app.services.storage import managed_path

    path = managed_path(ws.id, v.thumbnail_path)
    if path and path.exists():
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="thumbnail not available")


class ThumbnailBody(BaseModel):
    at_seconds: float = Field(default=1.0, ge=0, le=600)
    cover_index: int | None = Field(default=None, ge=0, le=20,
                                    description="pick a generated cover candidate instead of a timestamp")


@videos_router.post("/{video_id}/thumbnail", summary="Regenerate poster frame at a timestamp")
def remake_thumbnail(video_id: str, body: ThumbnailBody, ws: Workspace = Depends(require_workspace_role("member")), db=Depends(get_db)):
    v = db.get(Video, video_id)
    if not v or v.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="video not found")
    from app.services.storage import LocalStorage, get_storage, managed_path

    src = managed_path(ws.id, v.file_path)
    if not src or not src.exists():
        raise HTTPException(status_code=404, detail="video file not found on disk")
    if body.cover_index is not None:
        cand = LocalStorage.cover_path_for(str(src), body.cover_index)
        path = managed_path(ws.id, str(cand))
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail="cover candidate not found — generate covers first")
        v.thumbnail_path = str(path)
        db.commit()
        return {"thumbnail_path": v.thumbnail_path}
    thumb = get_storage().extract_thumbnail(str(src), at_seconds=body.at_seconds)
    if not thumb:
        raise HTTPException(status_code=503, detail="ffmpeg unavailable for thumbnails")
    v.thumbnail_path = thumb
    db.commit()
    return {"thumbnail_path": thumb}


class CoversBody(BaseModel):
    count: int = Field(default=3, ge=1, le=5)
    timestamps: list[float] | None = Field(default=None, description="explicit seconds; else spread across duration")


@videos_router.post("/{video_id}/covers", summary="Generate cover candidates for side-by-side compare")
def make_covers(video_id: str, body: CoversBody, ws: Workspace = Depends(require_workspace_role("member")), db=Depends(get_db)):
    v = db.get(Video, video_id)
    if not v or v.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="video not found")
    from app.services.storage import get_storage, managed_path, probe_metadata

    src = managed_path(ws.id, v.file_path)
    if not src or not src.exists():
        raise HTTPException(status_code=404, detail="video file not found on disk")
    if body.timestamps:
        stamps = [max(0.0, t) for t in body.timestamps[:5]]
    else:
        dur = (probe_metadata(src).get("duration_seconds") or 30.0)
        stamps = [round(dur * f, 2) for f in (0.08, 0.35, 0.65, 0.85, 0.95)][: body.count]
    covers = get_storage().extract_covers(str(src), stamps)
    if not covers:
        raise HTTPException(status_code=503, detail="ffmpeg unavailable for covers")
    return {"covers": [
        {**c, "url": f"/api/v1/workspaces/{ws.id}/videos/{video_id}/covers/{c['index']}/file"}
        for c in covers
    ]}


@videos_router.get("/{video_id}/covers/{index}/file", summary="Serve one cover candidate")
def cover_file(video_id: str, index: int, request: Request, workspace_id: str, token: str | None = None, db=Depends(get_db)):
    from fastapi.responses import FileResponse

    from app.services.auth_service import resolve_workspace

    ws = resolve_workspace(request, db, workspace_id, token)
    v = db.get(Video, video_id)
    if not v or v.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="video not found")
    from app.services.storage import LocalStorage, managed_path

    src = managed_path(ws.id, v.file_path)
    if not src:
        raise HTTPException(status_code=404, detail="video file not found on disk")
    cand = managed_path(ws.id, str(LocalStorage.cover_path_for(str(src), index)))
    if not cand or not cand.exists():
        raise HTTPException(status_code=404, detail="cover candidate not found")
    return FileResponse(cand, media_type="image/jpeg", filename=cand.name)


@videos_router.get("/{video_id}/file", summary="Stream the rendered file (mock artifacts served as JSON)")
def video_file(video_id: str, request: Request, workspace_id: str, token: str | None = None, db=Depends(get_db)):
    from fastapi.responses import FileResponse, PlainTextResponse

    from app.services.auth_service import resolve_workspace

    ws = resolve_workspace(request, db, workspace_id, token)
    v = db.get(Video, video_id)
    if not v or v.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="video not found")
    from app.services.storage import managed_path, mock_render_spec_path

    if v.file_path.startswith("mock:"):
        artifact = mock_render_spec_path(v.file_path)
        if artifact and artifact.exists():
            try:
                return PlainTextResponse(artifact.read_text(), media_type="application/json")
            except OSError:
                pass
        raise HTTPException(status_code=404, detail="mock artifact missing")

    path = managed_path(ws.id, v.file_path)
    if path and path.exists():
        return FileResponse(path, media_type="video/mp4", filename=path.name)
    raise HTTPException(status_code=404, detail="video file not found on disk")


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

cycles_router = APIRouter(prefix="/workspaces/{workspace_id}/cycles", tags=["cycles"])


@cycles_router.get("", summary="List recent cycles")
def list_cycles(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db), limit: int = Query(default=20, le=100)):
    rows = db.scalars(select(Cycle).where(Cycle.workspace_id == ws.id).order_by(Cycle.number.desc()).limit(limit)).all()
    return {
        "items": [
            {
                "id": cy.id,
                "number": cy.number,
                "stage": cy.stage,
                "status": cy.status,
                "cost_usd": cy.cost_usd,
                "topic": next((cy.summary_json or {}).get(k, {}).get("opportunity") for k in ("select",) if k in (cy.summary_json or {})),
                "started_at": cy.started_at.isoformat() + "Z" if cy.started_at else None,
                "finished_at": cy.finished_at.isoformat() + "Z" if cy.finished_at else None,
                "error": cy.error,
            }
            for cy in rows
        ]
    }


@cycles_router.get("/{cycle_id}", summary="Full cycle execution detail: stage jobs + agent runs with step traces")
def cycle_detail(
    cycle_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    cy = db.get(Cycle, cycle_id)
    if not cy or cy.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="cycle not found")

    from app.models.ops import AgentRun

    jobs = db.scalars(
        select(Job).where(Job.cycle_id == cy.id).order_by(Job.created_at.asc())
    ).all()
    job_ids = [j.id for j in jobs]
    runs = (
        db.scalars(
            select(AgentRun)
            .where(AgentRun.cycle_id == cy.id)
            .order_by(AgentRun.created_at.asc())
        ).all()
        if hasattr(AgentRun, "cycle_id")
        else []
    )

    def run_dto(r: AgentRun) -> dict:
        steps = (r.steps_json or {}).get("steps", []) if isinstance(r.steps_json, dict) else []
        return {
            "id": r.id,
            "agent_key": r.agent_key,
            "task_type": r.task_type,
            "status": r.status,
            "duration_ms": r.duration_ms,
            "cost_usd": r.cost_usd,
            "error": r.error,
            "input_summary": r.input_summary,
            "output_summary": r.output_summary,
            "steps": steps,
        }

    def job_dto(j: Job) -> dict:
        linked = [run_dto(r) for r in runs if r.job_id == j.id]
        return {
            "id": j.id,
            "type": j.type,
            "status": j.status,
            "stage": (j.type.split(".", 1)[1].upper() if "." in (j.type or "") else ""),
            "retry_count": j.retry_count,
            "last_error": j.last_error,
            "created_at": j.created_at.isoformat() + "Z",
            "started_at": j.started_at.isoformat() + "Z" if j.started_at else None,
            "finished_at": j.completed_at.isoformat() + "Z" if j.completed_at else None,
            "result": j.result or {},
            "agent_runs": linked,
        }

    stages: dict[str, list] = {}
    for j in jobs:
        dto = job_dto(j)
        stages.setdefault(dto["stage"] or "OTHER", []).append(dto)

    summary = cy.summary_json or {}
    decision = summary.get("decision") or summary.get("select", {}).get("decision")
    return {
        "id": cy.id,
        "number": cy.number,
        "stage": cy.stage,
        "status": cy.status,
        "cost_usd": cy.cost_usd,
        "error": cy.error,
        "topic": next((summary.get(k, {}).get("opportunity") for k in ("select",) if k in summary), None),
        "decision": decision,
        "started_at": cy.started_at.isoformat() + "Z" if cy.started_at else None,
        "finished_at": cy.finished_at.isoformat() + "Z" if cy.finished_at else None,
        "stages": stages,
        "agent_runs": [run_dto(r) for r in runs],
    }


# ---------------------------------------------------------------------------
# Campaigns & calendar
# ---------------------------------------------------------------------------

campaigns_router = APIRouter(prefix="/workspaces/{workspace_id}/campaigns", tags=["campaigns"])


class CampaignBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=2000)
    target_videos: int = Field(default=0, ge=0)
    videos_per_day: float = Field(default=0, ge=0, le=100)
    platforms: list[str] = Field(default_factory=list)
    automation_level: str = Field(default="SEMI_AUTONOMOUS")
    budget_daily_usd: float | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


@campaigns_router.get("")
def list_campaigns(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(select(Campaign).where(Campaign.workspace_id == ws.id).order_by(Campaign.created_at.desc())).all()
    return {
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "goal": c.goal,
                "status": c.status,
                "target_videos": c.target_videos,
                "videos_per_day": c.videos_per_day,
                "platforms": c.platforms_json or [],
                "automation_level": c.automation_level,
                "budget_daily_usd": c.budget_daily_usd,
                "starts_at": c.starts_at.isoformat() + "Z" if c.starts_at else None,
                "ends_at": c.ends_at.isoformat() + "Z" if c.ends_at else None,
            }
            for c in rows
        ]
    }


@campaigns_router.post("", status_code=201)
def create_campaign(body: CampaignBody, ws: Workspace = Depends(require_workspace_role("admin")), db=Depends(get_db)):
    allowed_platforms = {"youtube", "tiktok", "facebook", "instagram"}
    bad = set(body.platforms) - allowed_platforms
    if bad:
        raise HTTPException(status_code=400, detail=f"unsupported platforms: {bad}")
    row = Campaign(
        workspace_id=ws.id,
        name=body.name,
        goal=body.goal,
        target_videos=body.target_videos,
        videos_per_day=body.videos_per_day,
        platforms_json=[p for p in body.platforms if p in allowed_platforms],
        automation_level=body.automation_level,
        budget_daily_usd=body.budget_daily_usd,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
    )
    db.add(row)
    db.commit()
    return {"id": row.id}


calendar_router = APIRouter(prefix="/workspaces/{workspace_id}/calendar", tags=["calendar"])


def _validate_run_at(v: datetime) -> datetime:
    """Reject naive or past datetimes for schedule entries."""
    if v.tzinfo is None:
        raise ValueError(
            "run_at must be an explicit timezone-aware ISO datetime (e.g. '2026-09-08T12:00:00Z'), "
            "not a naive local-time value."
        )
    if v < datetime.now(UTC):
        raise ValueError("run_at cannot be in the past")
    return v


class ScheduleBody(BaseModel):
    content_item_id: str
    campaign_id: str | None = None
    platform: str
    run_at: datetime

    @field_validator("run_at")
    @classmethod
    def _tz_check(cls, v: datetime) -> datetime:
        return _validate_run_at(v)


@calendar_router.get("")
def list_schedule(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    # Everything still meaningful to an operator: upcoming, in-flight
    # (DISPATCHING/QUEUED) and failed entries. DONE entries disappear once the
    # post exists; CANCELLED entries are terminal and hidden.
    rows = db.scalars(
        select(ScheduleEntry)
        .where(
            ScheduleEntry.workspace_id == ws.id,
            ScheduleEntry.status.in_(["PENDING", "DISPATCHING", "QUEUED", "FAILED"]),
        )
        .order_by(ScheduleEntry.run_at.asc())
        .limit(200)
    ).all()
    return {
        "items": [
            {
                "id": e.id,
                "platform": e.platform,
                "run_at": e.run_at.isoformat() + "Z",
                "content_item_id": e.content_item_id,
                "campaign_id": e.campaign_id,
                "status": e.status,
            }
            for e in rows
        ]
    }


@calendar_router.post("", status_code=201)
def add_schedule(body: ScheduleBody, ws: Workspace = Depends(require_workspace_role("admin")), db=Depends(get_db)):
    content = db.get(ContentItem, body.content_item_id)
    if not content or content.workspace_id != ws.id:
        raise HTTPException(status_code=422, detail="content_item_id not found in this workspace")
    entry = ScheduleEntry(
        workspace_id=ws.id,
        content_item_id=body.content_item_id,
        campaign_id=body.campaign_id,
        platform=body.platform,
        run_at=body.run_at,
    )
    db.add(entry)
    db.commit()
    return {"id": entry.id}


class RescheduleBody(BaseModel):
    run_at: datetime

    @field_validator("run_at")
    @classmethod
    def _tz_check(cls, v: datetime) -> datetime:
        return _validate_run_at(v)


@calendar_router.patch("/{entry_id}")
def reschedule(
    entry_id: str,
    body: RescheduleBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    entry = db.get(ScheduleEntry, entry_id)
    if not entry or entry.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="schedule entry not found")
    if entry.status not in ("PENDING", "FAILED"):
        raise HTTPException(status_code=409, detail=f"cannot reschedule a {entry.status} entry")
    entry.run_at = body.run_at
    if entry.status == "FAILED":
        # Operator retry: re-arm a permanently failed publish with a new time.
        entry.status = "PENDING"
    db.commit()
    return {"id": entry.id, "run_at": entry.run_at.isoformat() + "Z"}


@calendar_router.delete("/{entry_id}")
def cancel_schedule(entry_id: str, ws: Workspace = Depends(require_workspace_role("admin")), db=Depends(get_db)):
    entry = db.get(ScheduleEntry, entry_id)
    if not entry or entry.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="schedule entry not found")
    if entry.status not in ("PENDING", "FAILED"):
        raise HTTPException(status_code=409, detail=f"cannot cancel a {entry.status} entry")
    entry.status = "CANCELLED"
    db.commit()
    return {"cancelled": True}


@calendar_router.get("/best-times", summary="Best publish hours from measured history")
def best_times(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """Hour-of-day (workspace timezone-naive UTC) ranked by average views.

    Falls back to generic evening hours when fewer than 3 measured posts exist.
    """
    from app.models import PostMetric, PublishedPost

    posts = db.scalars(
        select(PublishedPost).where(PublishedPost.workspace_id == ws.id)
    ).all()
    if not posts:
        return {"items": [{"hour": h, "avg_views": 0, "posts": 0} for h in (18, 12, 20)],
                "measured": False}
    metrics = {}
    for m in db.scalars(
        select(PostMetric).where(PostMetric.post_id.in_([p.id for p in posts])).order_by(PostMetric.captured_at.asc())
    ):
        metrics[m.post_id] = m
    buckets: dict[int, list[int]] = {}
    for p in posts:
        m = metrics.get(p.id)
        if not m or not p.published_at:
            continue
        buckets.setdefault(p.published_at.hour, []).append(m.views)
    ranked = sorted(
        ((h, sum(v) / len(v), len(v)) for h, v in buckets.items()),
        key=lambda t: t[1],
        reverse=True,
    )[:5]
    if len(ranked) < 1:
        return {"items": [{"hour": h, "avg_views": 0, "posts": 0} for h in (18, 12, 20)],
                "measured": False}
    return {
        "items": [{"hour": h, "avg_views": round(avg), "posts": n} for h, avg, n in ranked],
        "measured": sum(n for _, _, n in ranked) >= 3,
    }


# ---------------------------------------------------------------------------
# Campaign progress detail
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Asset library — real files produced by the system
# ---------------------------------------------------------------------------

assets_router = APIRouter(prefix="/workspaces/{workspace_id}/assets", tags=["assets"])


@assets_router.get("")
def list_assets(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    """All video artifacts this workspace has produced, plus uploaded files.

    Real data only: rows come from the videos/variants tables plus the
    workspace uploads directory (operator-supplied files).
    """
    from pathlib import Path as _P

    rows = db.execute(
        select(Video, VideoVariant, ContentItem)
        .join(VideoVariant, Video.variant_id == VideoVariant.id)
        .join(ContentItem, VideoVariant.content_item_id == ContentItem.id)
        .where(Video.workspace_id == ws.id)
        .order_by(Video.created_at.desc())
        .limit(200)
    ).all()
    items = []
    for video, variant, content in rows:
        size = None
        if video.file_path and not video.file_path.startswith("mock:"):
            try:
                size = _P(video.file_path).stat().st_size
            except OSError:
                size = None
        items.append(
            {
                "id": video.id,
                "type": "video",
                "title": content.topic[:120],
                "engine": video.engine,
                "status": video.status,
                "aspect_ratio": video.aspect_ratio,
                "resolution": video.resolution,
                "size_bytes": size,
                "is_mock": video.engine == "mock" or video.file_path.startswith("mock:"),
                "variant_label": variant.label,
                "video_id": video.id,
                "created_at": video.created_at.isoformat() + "Z",
            }
        )
    upload_dir = _P("data/videos") / ws.id / "uploads"
    if upload_dir.exists():
        for f in sorted(upload_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            if not f.is_file():
                continue
            items.append({
                "id": f"upload:{f.name}",
                "type": "upload",
                "title": f.name,
                "engine": "upload",
                "status": "READY",
                "size_bytes": f.stat().st_size,
                "is_mock": False,
                "video_id": None,
                "created_at": "",
            })
    return {
        "items": items,
        "capabilities": {
            "upload": True,
            "note": "Operators can upload MP4/JPG/PNG assets for manual use.",
        },
    }


@assets_router.post("/upload", summary="Upload an MP4/image asset")
def upload_asset(
    ws: Workspace = Depends(require_workspace_role("member")),
    file: UploadFile = File(...),
):
    """Operator-supplied asset stored under the workspace boundary."""
    import pathlib as _pl

    up = file
    if up is None:
        raise HTTPException(status_code=400, detail="file is required (multipart 'file')")
    data = up.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="file is empty")
    if len(data) > 500 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file exceeds 500MB limit")
    suffix = _pl.Path(up.filename or "upload").suffix.lower()
    if suffix not in (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".wav", ".mp3"):
        raise HTTPException(status_code=400, detail=f"unsupported file type: {suffix or 'unknown'}")
    from app.services.storage import get_storage

    safe = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in (up.filename or "upload"))[:120]
    stored = get_storage().save_media(ws.id, data=data, filename=f"uploads/{safe}")
    return {"path": stored, "size_bytes": len(data)}


# ---------------------------------------------------------------------------
# Image generation — scene images via the image provider layer
# ---------------------------------------------------------------------------


class ImageGenBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=600)
    size: str = Field(default="1024x576", pattern=r"^\d{2,4}x\d{2,4}$")
    n: int = Field(default=1, ge=1, le=4)


@assets_router.get("/images/status", summary="Image generation availability")
def image_status(ws: Workspace = Depends(require_workspace_role("viewer"))):
    from app.providers.images import image_provider_status
    from app.services import provider_settings as ps

    with ps.workspace_scope(ws.id):
        return image_provider_status()


@assets_router.post("/images/generate", summary="Generate scene images")
def generate_images(
    body: ImageGenBody,
    ws: Workspace = Depends(require_workspace_role("member")),
):
    """Generate images via the configured provider and store them as workspace
    assets. Mock provider results are labeled is_mock=true."""
    from app.providers.images import ImageProviderError, get_image_provider
    from app.services import provider_settings as ps
    from app.services.storage import get_storage

    try:
        with ps.workspace_scope(ws.id):
            provider = get_image_provider()
            blobs = provider.generate(body.prompt, size=body.size, n=body.n)
    except ImageProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    stored_paths = []
    for i, blob in enumerate(blobs):
        stored = get_storage().save_media(
            ws.id, data=blob,
            filename=f"gen_{int(time.time())}_{i}.png",
        )
        stored_paths.append(stored)
    return {
        "provider": provider.name,
        "is_mock": provider.is_mock,
        "images": stored_paths,
    }


# ---------------------------------------------------------------------------
# Clip repurposing — long-form sources cut into vertical shorts
# ---------------------------------------------------------------------------


class ClipJobBody(BaseModel):
    source: str = Field(min_length=1, max_length=2000, description="https URL or local file path")
    clip_seconds: float = Field(default=45.0, ge=5, le=180)
    max_clips: int = Field(default=5, ge=1, le=10)
    vertical: bool = True
    rank: bool = Field(default=True, description="rank viral moments before cutting (LLM w/ offline fallback)")
    caption_preset: str = Field(default="minimal", description="minimal|pop|karaoke burned-in caption style")
    face_track: bool = Field(default=False, description="face-centered crop (needs MediaPipe, else center crop)")


@assets_router.get("/repurpose/status", summary="Clip repurposing availability")
def clip_status(ws: Workspace = Depends(require_workspace_role("viewer"))):
    from app.providers.clips import get_repurposer

    return get_repurposer().status()


class MotionCardBody(BaseModel):
    kind: str = Field(default="hook", description="hook|stat|cta|lower")
    title: str = Field(min_length=1, max_length=200)
    subtitle: str = Field(default="", max_length=300)
    accent: str = Field(default="#22c55e", max_length=9)
    duration: float = Field(default=3.0, ge=1, le=10)


@assets_router.get("/motion/status", summary="Motion-graphics (HyperFrames) availability")
def motion_status(ws: Workspace = Depends(require_workspace_role("viewer"))):
    from app.providers.motion import motion_status as _status

    return _status()


@assets_router.post("/motion", summary="Render a kinetic motion-graphics card")
def render_motion_card(
    body: MotionCardBody,
    ws: Workspace = Depends(require_workspace_role("member")),
):
    """Render a hook/stat/CTA/lower-third card via HyperFrames.

    Requires the HyperFrames CLI + a working Chrome + ffmpeg; otherwise fails
    closed with remediation (503) instead of faking output.
    """
    from app.providers.motion import MotionError, render_card

    try:
        card = render_card(body.kind, body.title, ws.id, subtitle=body.subtitle,
                           accent=body.accent, duration=body.duration)
    except MotionError as exc:
        detail = str(exc)
        status = 503 if ("not installed" in detail or "not found" in detail
                          or "no working Chrome" in detail) else 400
        raise HTTPException(status_code=status, detail=detail)
    return {"path": card.path, "kind": card.kind, "duration": card.duration}


@assets_router.get("/templates", summary="List creation templates (versioned registry)")
def list_templates(ws: Workspace = Depends(require_workspace_role("viewer")),
                   module: str = ""):
    """Built-in caption/hook/motion templates, latest version each.

    Workspaces override any template via PUT /workspaces/{id}/settings with
    {"settings": {"templates": {"<module>/<id>": {payload patch, ...}}}} —
    overrides merge over the built-in and are flagged "overridden".
    """
    from app.services.templates import list_templates as _list
    from app.services.templates import workspace_overrides

    overrides = workspace_overrides(ws.id)
    items = []
    for t in _list(module):
        key = f"{t['module']}/{t['id']}"
        items.append({**t, "overridden": key in overrides})
    return {"items": items}


@assets_router.get("/templates/{module}/{tid}", summary="Template detail (resolved)")
def template_detail(module: str, tid: str, ws: Workspace = Depends(require_workspace_role("viewer")),
                    version: str = ""):
    from app.services.templates import resolve_template, versions_of

    try:
        resolved = resolve_template(module, tid, ws.id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {**resolved, "versions": versions_of(module, tid)}


class DubBody(BaseModel):
    source: str = Field(min_length=1, max_length=2000, description="https URL or local file path")
    target_lang: str = Field(min_length=2, max_length=8, description="ISO-639-1 code: es, fr, de, hi, ...")
    voice: str = Field(default="", max_length=120, description="explicit TTS voice or empty for auto-match")
    bilingual: bool = True
    portrait: bool = True
    srt: str = Field(default="", max_length=60000, description="optional subtitle track (skips transcription)")


@assets_router.get("/dub/status", summary="Dubbing pipeline availability")
def dub_status(ws: Workspace = Depends(require_workspace_role("viewer"))):
    from app.providers.dubbing import dub_status as _status

    return _status()


@assets_router.post("/dub", summary="Translate and re-voice a video into another language")
def dub_video(body: DubBody, ws: Workspace = Depends(require_workspace_role("member"))):
    """Synchronous dub: transcribe/parse → LLM translate → TTS → assemble.

    Requires LLM key + target-language TTS voice + ffmpeg; otherwise fails
    closed with remediation (400/503).
    """
    from pathlib import Path as _Path

    from app.providers.clips import ClipError, get_repurposer
    from app.providers.dubbing import (
        DubError,
        assemble_dubbed,
        format_srt,
        parse_srt,
        pick_voice,
        synthesize_segments,
        to_bilingual,
        translate_segments,
    )
    from app.providers.dubbing import SrtCue as _SrtCue
    from app.services.storage import STORAGE_ROOT

    lang = body.target_lang.lower().strip()
    try:
        rep = get_repurposer()
        info = rep.acquire(body.source, ws.id)
        if body.srt.strip():
            cues = parse_srt(body.srt)
        else:
            segs = rep.transcribe_segments(info)
            cues = [_SrtCue(index=i + 1, start=s.start, end=s.end, text=s.text)
                    for i, s in enumerate(segs)]
        if not cues:
            raise DubError("no transcript available — install faster-whisper or supply an SRT track")
        translated = translate_segments([c.text for c in cues], lang, ws.id)
        voice = pick_voice(lang, body.voice)
        work_dir = STORAGE_ROOT / "_clipwork" / ws.id / "dub"
        dub_files = [(_Path(p) if p else _Path(""))
                     for p in synthesize_segments(translated, voice, work_dir)]
        bilingual = to_bilingual(cues, translated) if body.bilingual else None
        srt_path = None
        if bilingual:
            srt_path = work_dir / "bilingual.srt"
            srt_path.write_text(format_srt(bilingual), encoding="utf-8")
        built = assemble_dubbed(info.local_path, cues, dub_files, ws.id, srt_path, body.portrait)
    except ClipError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except DubError as exc:
        detail = str(exc)
        status = 503 if ("ffmpeg" in detail or "unavailable" in detail) else 400
        raise HTTPException(status_code=status, detail=detail)
    return {**built, "target_lang": lang, "voice": voice, "cues": len(cues)}


@assets_router.post("/repurpose", summary="Cut a long-form source into vertical shorts")
def repurpose_clips(
    body: ClipJobBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    """Synchronous cutting of an operator-supplied source into workspace assets.

    Download requires yt-dlp; cutting requires ffmpeg. When unavailable the
    endpoint reports the exact remediation instead of failing obscurely.
    """
    from app.providers.clips import ClipError, ViralMoment, get_repurposer

    repurposer = get_repurposer()
    try:
        source = repurposer.acquire(body.source, ws.id)
        moments = []
        if body.rank:
            segments = repurposer.transcribe_segments(source)
            base = segments if segments else [
                {"start": s, "end": e, "text": f"Segment {i + 1} of {source.title}"}
                for i, (s, e) in enumerate(repurposer.detect_scenes(source))
            ]
            moments = repurposer.rank_moments(base, body.max_clips, ws.id)
        viral = [
            ViralMoment(start=m.start, end=m.end, score=m.score, hook=m.hook,
                        reason=m.reason, text=m.text)
            for m in moments
        ]
        captions = {i + 1: (m.text or m.hook) for i, m in enumerate(viral)} or None
        clips = repurposer.cut_segments(
            source, ws.id,
            moments=viral or None,
            clip_seconds=body.clip_seconds,
            max_clips=body.max_clips,
            vertical=body.vertical,
            caption_preset=body.caption_preset,
            captions=captions,
            face_track=body.face_track,
        )
    except ClipError as exc:
        detail = str(exc)
        status = 503 if "not installed" in detail or "not available" in detail else 400
        raise HTTPException(status_code=status, detail=detail)
    return {
        "source_title": source.title,
        "source_duration": source.duration,
        "ranked": bool(viral),
        "clips": [
            {
                "path": c.path,
                "start": c.start,
                "end": c.end,
                "duration": c.duration,
                "resolution": f"{c.width}x{c.height}" if c.width and c.height else None,
                "score": c.score,
                "hook": c.hook,
                "reason": c.reason,
                "preset": c.preset,
            }
            for c in clips
        ],
    }


@campaigns_router.get("/{campaign_id}")
def campaign_detail(campaign_id: str, ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c or c.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="campaign not found")
    content_count = db.scalar(
        select(func.count()).select_from(ContentItem).where(ContentItem.campaign_id == campaign_id)
    ) or 0
    published_count = db.scalar(
        select(func.count()).select_from(ContentItem).where(
            ContentItem.campaign_id == campaign_id, ContentItem.status == "PUBLISHED"
        )
    ) or 0
    return {
        
            "id": c.id,
            "name": c.name,
            "goal": c.goal,
            "status": c.status,
            "target_videos": c.target_videos,
            "videos_per_day": c.videos_per_day,
            "platforms": c.platforms_json or [],
            "automation_level": c.automation_level,
            "budget_daily_usd": c.budget_daily_usd,
            "starts_at": c.starts_at.isoformat() + "Z" if c.starts_at else None,
            "ends_at": c.ends_at.isoformat() + "Z" if c.ends_at else None
        ,
        "progress": {"content_items": content_count, "published": published_count},
    }


# ---------------------------------------------------------------------------
# Clip repurposing
# ---------------------------------------------------------------------------


class RepurposeBody(BaseModel):
    url: str = Field(..., min_length=5, max_length=2000)
    clip_seconds: float = Field(default=45.0, ge=5, le=300)
    max_clips: int = Field(default=5, ge=1, le=20)
    vertical: bool = Field(default=True)


@content_router.post("/repurpose", summary="Repurpose a long-form URL into short-form clip drafts")
def repurpose_url(
    body: RepurposeBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    """Download + cut a source video into short segments, each becoming a
    ContentItem draft in the library. Returns the list of created items.

    Falls back to honest error responses when yt-dlp/ffmpeg are unavailable
    rather than silently producing nothing.
    """
    from app.providers.clips import ClipError, ClipRepurposer

    repurposer = ClipRepurposer()
    cap = repurposer.status()
    try:
        source = repurposer.acquire(body.url, ws.id)
    except ClipError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        clips = repurposer.cut_segments(
            source,
            ws.id,
            clip_seconds=body.clip_seconds,
            max_clips=body.max_clips,
            vertical=body.vertical,
        )
    except ClipError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    created = []
    for clip in clips:
        item = ContentItem(
            workspace_id=ws.id,
            topic=f"{source.title} — clip {len(created)+1} ({clip.start:.0f}s–{clip.end:.0f}s)",
            status="IDEA",
        )
        db.add(item)
        db.flush()  # get item.id for metadata_json
        item.tags_json = [
            "repurposed",
            f"clip-{len(created)+1}",
            f"source:{source.title[:80]}",
        ]
        item.strategy_json = {
            "source_url": body.url,
            "source_title": source.title,
            "clip_start": clip.start,
            "clip_end": clip.end,
            "clip_duration": clip.duration,
            "clip_path": clip.path,
            "vertical": body.vertical,
        }
        db.flush()
        created.append({
            "id": item.id,
            "topic": item.topic,
            "status": item.status,
            "clip_start": clip.start,
            "clip_end": clip.end,
            "clip_duration": clip.duration,
            "source_title": source.title,
        })

    db.commit()
    return {
        "items": created,
        "source": {"title": source.title, "duration": source.duration},
        "capabilities": cap,
    }

