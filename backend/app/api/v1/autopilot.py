"""Autopilot control endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.engine import autopilot
from app.models import Workspace
from app.services.auth_service import require_workspace_role

router = APIRouter(prefix="/workspaces/{workspace_id}/autopilot", tags=["autopilot"])


def _viewer():
    return Depends(require_workspace_role("viewer"))


def _admin():
    return Depends(require_workspace_role("admin"))


def naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:

        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


class StartBody(BaseModel):
    mode: str = Field(default="CONTINUOUS", pattern="^(CONTINUOUS|SINGLE_CYCLE)$")
    cycles_target: int = Field(default=0, ge=0)
    scheduled_start_at: datetime | None = None
    scheduled_stop_at: datetime | None = None
    config: dict = Field(default_factory=dict)
    override_readiness: bool = False


@router.post("/start", summary="Start the autonomous loop")
def start(body: StartBody, ws: Workspace = Depends(require_workspace_role("admin"))):
    try:
        result = autopilot.start_autopilot(
            ws.id,
            mode=body.mode,
            cycles_target=body.cycles_target,
            scheduled_start_at=naive_utc(body.scheduled_start_at),
            scheduled_stop_at=naive_utc(body.scheduled_stop_at),
            config=body.config,
            override_readiness=body.override_readiness,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result.get("blocked"):
        raise HTTPException(status_code=409, detail=result["readiness"])
    return result


@router.post("/stop", summary="Stop safely (running steps finish)")
def stop(ws: Workspace = _admin()):
    ok = autopilot.stop_autopilot(ws.id)
    if not ok:
        raise HTTPException(status_code=409, detail="no active autopilot run")
    return {"stopping": True}


@router.post("/pause", summary="Pause before the next stage")
def pause(ws: Workspace = _admin()):
    ok = autopilot.pause_autopilot(ws.id)
    if not ok:
        raise HTTPException(status_code=409, detail="not running")
    return {"paused": True}


@router.post("/resume", summary="Resume a paused run")
def resume(ws: Workspace = _admin()):
    ok = autopilot.resume_autopilot(ws.id)
    if not ok:
        raise HTTPException(status_code=409, detail="not paused")
    return {"resumed": True}


@router.post("/run-one-cycle", summary="Run exactly one FIND→…→LEARN cycle")
def run_one(ws: Workspace = _admin()):
    return autopilot.run_single_cycle(ws.id)


@router.get("/status", summary="Current autopilot state")
def status(ws: Workspace = _viewer()):
    return autopilot.get_autopilot_status(ws.id)



