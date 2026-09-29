"""Persistence helpers for lip-sync job rows (table `lipsync_jobs`, 0022).

All status writes are conditional on the row still being active, so a
late worker write can never clobber a CANCELLED/TIMEOUT/TERMINAL row.
Cost (gpu_seconds + USD) always lands on the job row; it is mirrored into a
shared `production_costs` table only if such a model exists.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select, update

from app.db import session_scope
from app.engine.lipsync.base import (
    ACTIVE_STATUSES,
    JOB_CANCELLED,
    JOB_QUEUED,
    JOB_RUNNING,
    TERMINAL_STATUSES,
)
from app.models.base import utcnow
from app.models.lipsync import LipSyncJob

# ---------------------------------------------------------------------------
# Row lifecycle
# ---------------------------------------------------------------------------


def create_job_row(
    db,
    *,
    workspace_id: str,
    provider: str,
    video_ref: str,
    audio_ref: str,
    opts: dict | None = None,
) -> LipSyncJob:
    row = LipSyncJob(
        workspace_id=workspace_id,
        provider=provider,
        status=JOB_QUEUED,
        video_ref=video_ref[:1024],
        audio_ref=audio_ref[:1024],
        opts_json=dict(opts or {}),
    )
    db.add(row)
    db.flush()
    return row


def get_job_row(db, workspace_id: str, job_id: str) -> LipSyncJob | None:
    row = db.get(LipSyncJob, job_id)
    if row is None or row.workspace_id != workspace_id:
        return None
    return row


def list_job_rows(db, workspace_id: str, limit: int = 100) -> list[LipSyncJob]:
    return list(
        db.scalars(
            select(LipSyncJob)
            .where(LipSyncJob.workspace_id == workspace_id)
            .order_by(LipSyncJob.created_at.desc())
            .limit(min(limit, 500))
        ).all()
    )


def job_dto(row: LipSyncJob) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "provider": row.provider,
        "status": row.status,
        "progress": round(float(row.progress or 0.0), 3),
        "error": row.error or "",
        "result_asset_ref": row.result_asset_ref or "",
        "cost": dict(row.cost_json or {}),
        "video_ref": row.video_ref or "",
        "audio_ref": row.audio_ref or "",
        "opts": dict(row.opts_json or {}),
        "adapter_job_id": row.adapter_job_id or "",
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
        "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
        "completed_at": row.completed_at.isoformat() + "Z" if row.completed_at else None,
    }


# ---------------------------------------------------------------------------
# Worker-side writes (own sessions; safe from the worker thread)
# ---------------------------------------------------------------------------


def mark_running(job_id: str, provider: str = "") -> bool:
    with session_scope() as s:
        res = s.execute(
            update(LipSyncJob)
            .where(LipSyncJob.id == job_id, LipSyncJob.status == JOB_QUEUED)
            .values(
                status=JOB_RUNNING,
                progress=0.0,
                started_at=utcnow(),
                error="",
                **({"provider": provider} if provider else {}),
            )
        )
        return res.rowcount == 1


def update_progress(job_id: str, progress: float) -> None:
    value = max(0.0, min(0.99, float(progress)))
    with session_scope() as s:
        s.execute(
            update(LipSyncJob)
            .where(LipSyncJob.id == job_id, LipSyncJob.status == JOB_RUNNING)
            .values(progress=value)
        )


def row_status(job_id: str) -> str | None:
    with session_scope() as s:
        row = s.get(LipSyncJob, job_id)
        return row.status if row else None


def set_adapter_job(job_id: str, adapter_job_id: str) -> None:
    """Link the durable row to the adapter-side job id."""
    with session_scope() as s:
        s.execute(
            update(LipSyncJob)
            .where(LipSyncJob.id == job_id)
            .values(adapter_job_id=(adapter_job_id or "")[:80])
        )


def finish_job(
    job_id: str,
    status: str,
    *,
    error: str = "",
    asset_ref: str = "",
    cost: dict | None = None,
    progress: float | None = None,
) -> bool:
    """Terminal write, conditional on the row still being active."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"finish_job requires a terminal status, got {status}")
    values: dict[str, Any] = {
        "status": status,
        "error": (error or "")[:4000],
        "completed_at": utcnow(),
    }
    if asset_ref:
        values["result_asset_ref"] = asset_ref[:1024]
    if cost is not None:
        values["cost_json"] = dict(cost)
    if progress is not None:
        values["progress"] = max(0.0, min(1.0, float(progress)))
    with session_scope() as s:
        res = s.execute(
            update(LipSyncJob)
            .where(LipSyncJob.id == job_id, LipSyncJob.status.in_(tuple(ACTIVE_STATUSES)))
            .values(**values)
        )
        return res.rowcount == 1


def request_cancel(db, workspace_id: str, job_id: str) -> LipSyncJob | None:
    """API-side cancel: only active rows move to CANCELLED."""
    row = get_job_row(db, workspace_id, job_id)
    if row is None:
        return None
    if row.status not in ACTIVE_STATUSES:
        return row
    row.status = JOB_CANCELLED
    row.error = (row.error or "") or "cancelled by request"
    row.completed_at = utcnow()
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Events + cost
# ---------------------------------------------------------------------------


def emit(workspace_id: str | None, kind: str, message: str, level: str = "info",
         data: dict | None = None) -> None:
    """Progress/telemetry events — never allowed to break a job."""
    if not workspace_id:
        return
    try:
        from app.services.events import record_event

        record_event(workspace_id, kind, message, level=level, source="lipsync",
                     data=data or {})
    except Exception:  # pragma: no cover - telemetry must be non-fatal
        logger.debug(f"lipsync event '{kind}' dropped")


def _production_cost_model():
    """Gracefully locate a shared `production_costs` ORM model, if any exists."""
    import importlib

    for modname in ("app.models.production_costs", "app.models.production",
                    "app.models.costs"):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        for obj in vars(mod).values():
            if isinstance(obj, type) and getattr(obj, "__tablename__", None) == "production_costs":
                return obj
    try:
        from app.db import Base

        for obj in Base.registry._class_registry.values():  # internal SA registry
            if isinstance(obj, type) and getattr(obj, "__tablename__", None) == "production_costs":
                return obj
    except Exception:  # pragma: no cover
        pass
    return None


def mirror_cost(workspace_id: str, job_id: str, cost: dict, provider: str) -> bool:
    """Mirror cost into a shared production_costs table when one is registered.

    Returns True when mirrored; otherwise the job row remains the single
    source of truth (cost_json) — never a hard failure.
    """
    if not cost:
        return False
    model = _production_cost_model()
    if model is None:
        return False
    try:
        columns = {c.name for c in model.__table__.columns}
        candidate = {
            "workspace_id": workspace_id,
            "provider": provider,
            "category": "lipsync",
            "kind": "lipsync",
            "job_id": job_id,
            "source_id": job_id,
            "ref_id": job_id,
            "amount_usd": float(cost.get("cost_usd") or 0.0),
            "cost_usd": float(cost.get("cost_usd") or 0.0),
            "gpu_seconds": float(cost.get("gpu_seconds") or 0.0),
            "detail_json": dict(cost),
            "metadata_json": dict(cost),
        }
        values = {k: v for k, v in candidate.items() if k in columns}
        if "workspace_id" not in values:
            return False
        if not ({"amount_usd", "cost_usd"} & values.keys()):
            return False
        with session_scope() as s:
            s.add(model(**values))
        return True
    except Exception as exc:  # pragma: no cover - schema drift must not fail jobs
        logger.debug(f"production_costs mirror skipped: {exc}")
        return False


__all__ = [
    "create_job_row",
    "emit",
    "finish_job",
    "get_job_row",
    "job_dto",
    "list_job_rows",
    "mark_running",
    "mirror_cost",
    "request_cancel",
    "row_status",
    "set_adapter_job",
    "update_progress",
]
