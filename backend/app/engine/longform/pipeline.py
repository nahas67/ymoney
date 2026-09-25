"""Long-form stage pipeline: durable chained jobs over LongFormProject rows.

Each stage is idempotent (re-running overwrites its own artifacts). The job
handler runs ONE stage then enqueues the next — no giant synchronous function,
no while-loops in request processes. REVIEW autonomy pauses at checkpoints;
MANUAL advances only via explicit API calls; CANCELLED stops the chain.
"""

from __future__ import annotations

from app.models.longform import STAGES

# stages that pause a REVIEW-autonomy project for operator approval
REVIEW_CHECKPOINTS = ("SCRIPT", "RENDER")

# stage -> artifact description for progress UI (completed/total units)
STAGE_UNITS = {
    "RESEARCH": "subtopics", "STRATEGY": "strategy", "OUTLINE": "chapters",
    "SCRIPT": "segments", "VERIFY": "claims", "SCENE_PLAN": "scenes",
    "ASSET_PLAN": "assets", "ASSET_ACQUIRE": "assets", "VOICE": "segments",
    "TIMELINE": "timeline", "QC": "checks", "RENDER": "chunks",
    "METADATA": "artifacts",
}


class LongFormError(Exception):
    pass


def next_stage(current: str) -> str | None:
    try:
        i = STAGES.index(current)
    except ValueError:
        return None
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def _pending_stage(session, project) -> str | None:
    """First incomplete stage from CREATED forward (idempotent resume)."""
    from app.models import LongFormChapter
    from app.models.assets import Scene

    research = project.research_json or {}
    if not research.get("subtopics"):
        return "RESEARCH"
    if not (project.strategy_json or {}).get("purpose"):
        return "STRATEGY"
    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).all()
    if not chapters:
        return "OUTLINE"
    if not (project.script_json or {}).get("segments_total"):
        return "SCRIPT"
    if not (project.fact_report_json or {}).get("checked") and \
            not (project.fact_report_json or {}).get("verdicts"):
        return "VERIFY"
    ch_ids = [c.id for c in chapters]
    scenes = session.query(Scene).filter(
        Scene.workspace_id == project.workspace_id,
        Scene.chapter_id.in_(ch_ids)).all() if ch_ids else []
    if not scenes:
        return "SCENE_PLAN"
    if not (project.asset_plan_json or {}).get("scenes"):
        return "ASSET_PLAN"
    if any(not (sc.assets_json or []) for sc in scenes):
        return "ASSET_ACQUIRE"
    if not (project.voice_json or {}).get("segments"):
        return "VOICE"
    if not project.timeline_id:
        return "TIMELINE"
    if not (project.qc_json or {}).get("result"):
        return "QC"
    if not (project.render_json or {}).get("asset_id"):
        return "RENDER"
    if not (project.metadata_json or {}).get("titles"):
        return "METADATA"
    return "DONE"


def run_stage(project_id: str, workspace_id: str, job_ctx=None) -> dict:
    """Execute the project's current pending stage synchronously.

    Advances project.stage on success. Returns {stage, status, progress}.
    Raises LongFormError (fail loudly, never fake percentages).
    """
    from app.db import session_scope
    from app.models import LongFormProject

    with session_scope() as s:
        project = s.get(LongFormProject, project_id)
        if project is None or project.workspace_id != workspace_id:
            raise LongFormError(f"project '{project_id}' not found")
        if project.status == "CANCELLED":
            return {"stage": project.stage, "status": "CANCELLED", "progress": _progress(project)}
        stage = _pending_stage(s, project)
        if job_ctx is not None:
            # explicit stage routing (regenerate flows): run exactly the
            # requested stage, then continue downstream from there
            requested = (job_ctx.payload or {}).get("stage")
            if requested and requested not in ("NEXT",) and requested in STAGES:
                stage = requested
        if stage is None or stage == "DONE":
            project.status = "COMPLETE"
            s.commit()
            return {"stage": "DONE", "status": "COMPLETE", "progress": _progress(project)}
        if (project.autonomy == "MANUAL" and job_ctx is not None):
            # jobs never auto-advance MANUAL projects; operators advance stages
            project.status = "WAITING_REVIEW"
            s.commit()
            return {"stage": stage, "status": "WAITING_REVIEW", "progress": _progress(project)}
        from app.engine.longform import stages as stage_mod

        fn = getattr(stage_mod, f"stage_{stage.lower()}", None)
        if fn is None:
            raise LongFormError(f"no runner for stage '{stage}'")
        if job_ctx is not None:
            from app.services import jobs as _jobs

            _jobs.check_cancelled(job_ctx)
        summary = fn(s, project, job_ctx)
        project.stage = stage
        project.status = "RUNNING"
        if project.autonomy == "REVIEW" and stage in REVIEW_CHECKPOINTS:
            project.status = "WAITING_REVIEW"
        s.commit()
        progress = _progress(project)
        _record(s, project, stage, summary, progress)
        return {"stage": stage, "status": project.status, "progress": progress,
                "summary": summary}


def advance(project_id: str, workspace_id: str) -> dict:
    """Operator resume: unblock REVIEW/MANUAL gates and run the next stage."""
    from app.db import session_scope
    from app.models import LongFormProject

    with session_scope() as s:
        project = s.get(LongFormProject, project_id)
        if project is None or project.workspace_id != workspace_id:
            raise LongFormError(f"project '{project_id}' not found")
        if project.status not in ("WAITING_REVIEW", "DRAFT", "RUNNING", "FAILED"):
            raise LongFormError(f"cannot advance from status '{project.status}'")
        project.status = "RUNNING"
        s.commit()
    return run_stage(project_id, workspace_id, job_ctx=None)


def chain_next(project_id: str, workspace_id: str, status: str, stage: str) -> None:
    """Enqueue the following stage unless paused/failed/complete/MANUAL."""
    if status in ("WAITING_REVIEW", "CANCELLED", "FAILED", "COMPLETE"):
        return
    nxt = next_stage(stage)
    if not nxt or nxt == "DONE":
        return
    from app.db import session_scope
    from app.models import LongFormProject
    from app.services import jobs as _jobs

    with session_scope() as s:
        project = s.get(LongFormProject, project_id)
        if project is None or project.autonomy == "MANUAL":
            return
    _jobs.enqueue("longform.stage",
                  {"project_id": project_id, "stage": nxt},
                  workspace_id=workspace_id, priority=50,
                  idempotency_key=f"longform-{project_id}-{nxt}")


def _progress(project) -> dict:
    info = dict(project.stage_progress_json or {})
    return {"stage": project.stage, "status": project.status,
            "units": info, "cost_usd": round(project.cost_usd or 0.0, 4)}


def _record(session, project, stage: str, summary: str, progress: dict) -> None:
    from app.services.events import record_event

    record_event(project.workspace_id, f"longform.{stage.lower()}",
                 f"Long-form {project.topic[:50]}: {stage} — {summary[:120]}",
                 level="info", source="longform",
                 data={"project_id": project.id, "stage": stage})


def handle_stage_job(ctx) -> dict:
    """Durable job entry: run the requested stage, then chain the next."""
    from app.services import jobs as _jobs

    payload = ctx.payload or {}
    project_id = payload.get("project_id", "")
    _jobs.check_cancelled(ctx)
    out = run_stage(project_id, ctx.workspace_id or "", job_ctx=ctx)
    try:
        pct = _stage_fraction(out["stage"])
        ctx.report_progress(pct)
    except Exception:
        pass
    chain_next(project_id, ctx.workspace_id or "", out["status"], out["stage"])
    return out


def _stage_fraction(stage: str) -> float:
    try:
        return round(100.0 * STAGES.index(stage) / (len(STAGES) - 1), 1)
    except ValueError:
        return 0.0
