"""Browser runs + verification ledger APIs (Work 05 Lane C).

- POST   /workspaces/{id}/intelligence/browser/runs
- GET    /workspaces/{id}/intelligence/browser/runs/{run_id}
- POST   /workspaces/{id}/intelligence/browser/runs/{run_id}/cancel
- POST   /workspaces/{id}/intelligence/verification/check
- GET    /workspaces/{id}/intelligence/verification/ledger
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_db
from app.engine.intelligence import browser as browser_engine
from app.engine.intelligence import ledger as ledger_engine
from app.engine.intelligence import verifier as verifier_engine
from app.models import BrowserRun, Workspace
from app.services.auth_service import require_workspace_role

logger = logging.getLogger("ymoney.intelligence")

intelligence_evidence_router = APIRouter(
    prefix="/workspaces/{workspace_id}/intelligence", tags=["intelligence"],
)


def _intel_settings(ws: Workspace) -> dict:
    merged = {**browser_engine.default_intelligence_settings(),
              **(dict((ws.settings_json or {}).get("intelligence", {})))}
    return merged


def _require_browser_enabled(ws: Workspace) -> dict:
    intel = _intel_settings(ws)
    if not intel.get("browser_enabled", True):
        raise HTTPException(status_code=403, detail="browser intelligence is disabled for this workspace")
    if str(intel.get("privacy_mode", "STANDARD")).upper() == "LOCAL_ONLY":
        raise HTTPException(status_code=403, detail="LOCAL_ONLY privacy mode blocks remote browser fetch")
    return intel


def _run_dto(row: BrowserRun) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "goal": row.goal,
        "status": row.status,
        "steps": row.steps_json or [],
        "evidence": row.evidence_json or {},
        "cost_usd": row.cost_usd,
    }


def _record_dto(row) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "subject_id": row.subject_id,
        "execution_status": row.execution_status,
        "verification_status": row.verification_status,
        "checks": row.checks_json or [],
        "digest": row.digest,
        "prev_digest": row.prev_digest,
        "created_at": row.created_at.isoformat() + "Z",
    }


class BrowserRunCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    start_urls: list[str] = Field(default_factory=list, max_length=10)
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    max_steps: int = Field(default=10, ge=1, le=50)


class VerificationCheckBody(BaseModel):
    kind: str = Field(min_length=1, max_length=30)
    subject_id: str = Field(min_length=1, max_length=36)
    expectations: dict = Field(default_factory=dict)


@intelligence_evidence_router.post("/browser/runs")
def create_browser_run(
    body: BrowserRunCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    intel = _require_browser_enabled(ws)
    allowed = list(body.allowed_domains or intel.get("allowed_domains") or [])
    policy = browser_engine.BrowserPolicy(
        allowed_domains=allowed,
        denied_domains=list(intel.get("denied_domains") or []),
        max_steps=min(body.max_steps, 50),
    )
    row = BrowserRun(workspace_id=ws.id, goal=body.goal[:2000], status="RUNNING",
                     steps_json=[], evidence_json={}, cost_usd=0.0)
    db.add(row)
    db.commit()
    try:
        agent = browser_engine.BrowserIntelligenceAgent(
            policy=policy, backend=browser_engine.RecordingBackend())
        result = agent.run(body.goal, body.start_urls or [])
    except (browser_engine.DomainDeniedError, browser_engine.BrowserError) as exc:
        row.status = "ABORTED"
        row.steps_json = []
        row.evidence_json = {"error": str(exc)[:300]}
        db.commit()
        return {**_run_dto(row), "stop_reason": str(exc)[:300]}
    row.status = result["status"]
    row.steps_json = result["steps"]
    row.evidence_json = result["evidence"]
    row.cost_usd = result["cost_usd"]
    db.commit()
    # W11.5 E-F2 (HIGH): the run's cost lived only in a local counter and in
    # BrowserRun.cost_usd, so it never reached the CostEntry ledger that the
    # budget gates read. Ledger it after the commit (track_cost opens its own
    # session) so daily caps account for browser research.
    if float(row.cost_usd or 0.0) > 0:
        try:
            from app.services.cost import track_cost

            track_cost(ws.id, "browser", float(row.cost_usd),
                       provider="browser", detail={"browser_run_id": row.id})
        except Exception as exc:  # noqa: BLE001 — never fail a completed run
            logger.warning("browser cost ledger failed for run %s: %s", row.id, exc)
    return {**_run_dto(row), "stop_reason": result.get("stop_reason", "")}


@intelligence_evidence_router.get("/browser/runs/{run_id}")
def get_browser_run(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = db.get(BrowserRun, run_id)
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="browser run not found")
    return _run_dto(row)


@intelligence_evidence_router.post("/browser/runs/{run_id}/cancel")
def cancel_browser_run(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    row = db.get(BrowserRun, run_id)
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="browser run not found")
    cancelled = False
    if row.status in ("QUEUED", "RUNNING"):
        row.status = "CANCELLED"
        db.commit()
        cancelled = True
    return {**_run_dto(row), "cancelled": cancelled}


@intelligence_evidence_router.post("/verification/check")
def run_verification_check(
    body: VerificationCheckBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    contract = verifier_engine.CompletionContract(
        kind=body.kind, subject_id=body.subject_id,
        expectations=dict(body.expectations or {}))
    row = verifier_engine.verify(db, ws.id, contract)
    return _record_dto(row)


@intelligence_evidence_router.get("/verification/ledger")
def get_verification_ledger(
    kind: str | None = Query(default=None, max_length=30),
    status: str | None = Query(default=None, max_length=30),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = ledger_engine.list_records(db, ws.id, kind=kind, status=status)
    return {"items": [_record_dto(r) for r in rows],
            "chain": ledger_engine.verify_chain(db, ws.id)}
