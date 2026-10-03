"""Intelligence decision endpoints (Work 05, Lane A).

Provider-independent DecisionEngine surface: typed primitives, audit log
and shadow report. All routes mask cross-workspace access as 404.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.models import Workspace, WorkspaceMember
from app.services.auth_service import get_current_user

logger = logging.getLogger("ymoney.intelligence")

router = APIRouter(prefix="/workspaces/{workspace_id}/intelligence", tags=["intelligence"])

_DECISION_KINDS = ("boolean", "choose", "rank", "classify", "verify")


class DecisionBody(BaseModel):
    input: dict = Field(default_factory=dict)
    provider: str = Field(default="")


def _workspace_or_404(minimum_role: str):
    order = {
        WorkspaceMember.ROLE_VIEWER: 0,
        WorkspaceMember.ROLE_MEMBER: 1,
        WorkspaceMember.ROLE_ADMIN: 2,
        WorkspaceMember.ROLE_OWNER: 3,
    }

    def dependency(workspace_id: str, user=Depends(get_current_user), db=Depends(get_db)) -> Workspace:
        ws = db.get(Workspace, workspace_id)
        if not ws:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
        member = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id
            )
        )
        if member is None and not user.is_superuser:
            # Mask existence: cross-workspace access reads as 404.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
        if member is not None and order[member.role] < order[minimum_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return ws

    return dependency


def _engine_for(ws: Workspace):
    from app.engine.intelligence.decision import DecisionEngine, get_intelligence_settings

    settings = get_intelligence_settings(ws.settings_json or {})
    return DecisionEngine(
        ws.id,
        mode=settings["decision_mode"],
        provider_preference=settings["provider_preference"],
    )


def _record_dto(row) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "mode": row.mode,
        "requested_provider": row.requested_provider,
        "actual_provider": row.actual_provider,
        "model": row.model,
        "latency_ms": row.latency_ms,
        "cost_usd": row.cost_usd,
        "fallback_reason": row.fallback_reason,
        "input": row.input_json or {},
        "output": row.output_json or {},
        "agree": row.agree,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/decisions/{kind}")
def run_decision(
    kind: str,
    body: DecisionBody,
    ws: Workspace = Depends(_workspace_or_404("member")),
) -> dict:
    if kind not in _DECISION_KINDS:
        raise HTTPException(status_code=404, detail=f"unknown decision kind {kind!r}")
    engine = _engine_for(ws)
    fn = getattr(engine, kind)
    output, record = fn(dict(body.input or {}), provider=body.provider or "")
    # W11.5 E-F2 (HIGH): an LLM-backed decision recorded cost_usd on the audit
    # record but never wrote a CostEntry, so the daily budget gate could not see
    # it. Ledger it after the fact (deterministic/local decisions cost 0 and are
    # skipped by track_cost's own <=0 guard).
    if float(record.cost_usd or 0.0) > 0:
        try:
            from app.services.cost import track_cost

            track_cost(ws.id, "decision_engine", float(record.cost_usd),
                       provider=record.actual_provider or record.requested_provider,
                       detail={"kind": kind, "model": record.model})
        except Exception as exc:  # noqa: BLE001 — never fail a decision on ledger write
            logger.warning("decision cost ledger failed (%s): %s", kind, exc)
    return {
        "kind": kind,
        "mode": record.mode,
        "requested_provider": record.requested_provider,
        "actual_provider": record.actual_provider,
        "model": record.model,
        "latency_ms": record.latency_ms,
        "cost_usd": record.cost_usd,
        "fallback_reason": record.fallback_reason,
        "output": output,
        "shadow": record.shadow,
    }


@router.get("/decisions/log")
def decision_log(
    ws: Workspace = Depends(_workspace_or_404("viewer")),
    db=Depends(get_db),
    kind: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    from app.models.intelligence import DecisionRecordRow

    query = (
        select(DecisionRecordRow)
        .where(DecisionRecordRow.workspace_id == ws.id)
        .order_by(DecisionRecordRow.created_at.desc())
        .limit(limit)
    )
    if kind:
        query = query.where(DecisionRecordRow.kind == kind)
    rows = db.scalars(query).all()
    return {"items": [_record_dto(r) for r in rows]}


@router.get("/decisions/shadow-report")
def decision_shadow_report(
    ws: Workspace = Depends(_workspace_or_404("viewer")),
    kind: str = Query(default=""),
) -> dict:
    from app.engine.intelligence.shadow import shadow_report

    return shadow_report(ws.id, kind=kind or None)
