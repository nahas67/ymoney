"""Creative experiment endpoints (Work 06 Lane B)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.engine.performance.experiments import (
    ExperimentError,
    analyze_experiment,
    cancel_experiment,
    create_experiment,
    start_experiment,
)
from app.models import Workspace
from app.models.experiment import Experiment
from app.services.auth_service import require_workspace_role

experiments_router = APIRouter(
    prefix="/workspaces/{workspace_id}/experiments", tags=["experiments"])


class ExperimentCreate(BaseModel):
    kind: str = "HOOK"
    hypothesis: str = Field(default="", max_length=2000)
    control: dict = Field(default_factory=dict)
    variants: list = Field(default_factory=list)
    platform: str = Field(default="", max_length=30)
    primary_metric: str = "views"
    secondary_metrics: list = Field(default_factory=list)
    minimum_sample: int = Field(default=60, ge=2)


def _dto(row: Experiment) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "hypothesis": row.hypothesis or "",
        "control": row.control_json or {},
        "variants": row.variants_json or [],
        "platform": row.platform or "",
        "primary_metric": row.primary_metric,
        "secondary_metrics": row.secondary_metrics or [],
        "minimum_sample": row.minimum_sample,
        "status": row.status,
        "result": row.result_json or {},
        "confidence": row.confidence or "",
        "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
        "ended_at": row.ended_at.isoformat() + "Z" if row.ended_at else None,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def _get(ws_id: str, experiment_id: str, db) -> Experiment:
    row = db.get(Experiment, experiment_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="experiment not found")
    return row


def _mutation(row_fn, ws_id: str, experiment_id: str, db):
    row = _get(ws_id, experiment_id, db)
    try:
        row = row_fn(db, row)
    except ExperimentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _dto(row)


@experiments_router.post("", summary="Create a creative experiment (DRAFT)")
def create(
    body: ExperimentCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    try:
        row = create_experiment(
            db, ws.id, kind=body.kind, hypothesis=body.hypothesis,
            control=body.control, variants=body.variants, platform=body.platform,
            primary_metric=body.primary_metric,
            secondary_metrics=body.secondary_metrics,
            minimum_sample=body.minimum_sample)
    except ExperimentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _dto(row)


@experiments_router.get("", summary="List workspace experiments")
def list_all(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = db.scalars(select(Experiment).where(
        Experiment.workspace_id == ws.id
    ).order_by(Experiment.created_at.desc()).limit(100)).all()
    return {"total": len(rows), "items": [_dto(r) for r in rows]}


@experiments_router.get("/{experiment_id}", summary="Experiment detail")
def get_one(
    experiment_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    return _dto(_get(ws.id, experiment_id, db))


@experiments_router.post("/{experiment_id}/start", summary="Start an experiment")
def start(
    experiment_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    return _mutation(start_experiment, ws.id, experiment_id, db)


@experiments_router.post("/{experiment_id}/cancel", summary="Cancel an experiment")
def cancel(
    experiment_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    return _mutation(cancel_experiment, ws.id, experiment_id, db)


@experiments_router.post("/{experiment_id}/analyze", summary="Analyze an experiment")
def analyze(
    experiment_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    return _mutation(analyze_experiment, ws.id, experiment_id, db)
