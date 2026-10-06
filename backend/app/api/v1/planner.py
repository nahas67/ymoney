"""Work 15 planner API -- the editorial operating system's control surface.

Three view groups, matching the three views in the work order:

    ``GET  /planner/signals``        observed signals + their evidence
    ``GET  /planner/opportunities``  scored opportunities + the WHY
    ``GET  /planner/plans``          plans and their items
    ``GET  /planner/calendar``       the placed schedule + capacity
    ``GET  /planner/feedback``       measured outcomes + derived lessons
    ``GET  /planner/policy``         the autonomy table, as data

and the operator actions:

    ``POST /planner/signals``        ingest an observation
    ``POST /planner/plan``           run a planning cycle
    ``POST /planner/items/{id}/...`` approve | reject | research_more |
                                     campaign | schedule
    ``POST /planner/capacity``       declare what the workspace can produce

Two properties the API deliberately preserves:

**AI recommendation is never shown as measured evidence.** Every opportunity
carries ``basis`` (OBSERVED / INFERRED / RECOMMENDED) and the UI is expected to
render the AI's suggestion in a separate lane. The payload keeps the two apart
structurally rather than relying on a label.

**No endpoint can publish.** Scheduling writes a ``ScheduleEntry`` through the
existing store; turning that into a publication remains the existing approval
path. The planner's actions stop at SCHEDULED.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.schemas.responses import OpportunityListOut
from app.db import get_db
from app.engine.planning.autonomy import (
    AutonomyMode,
    AutonomyPolicy,
    AutonomyRefused,
    PlanningAction,
    assert_may_advance,
    describe_autonomy,
)
from app.engine.planning.calendar import (
    CalendarRequest,
    build_schedule_entries,
    place_request,
)
from app.engine.planning.capacity import (
    check_capacity,
    get_capacity,
    load_ledger,
    upset_capacity,
)
from app.engine.planning.engine import (
    ContentPlanningEngine,
    PlanningInputs,
    consult_memory,
)
from app.engine.planning.feedback import collect_feedback, record_outcome
from app.engine.planning.opportunities import FORBIDDEN_CLAIMS
from app.engine.planning.orchestration import run_orchestration
from app.engine.planning.signals import (
    SIGNAL_SOURCES,
    SignalIngest,
    claim_signals,
    ingest_signal,
)
from app.models import Workspace
from app.models.content import (
    ContentItem,
    Opportunity,
    ScheduleEntry,
)
from app.models.planning import (
    AUTONOMY_MODES,
    EditorialPlan,
    EditorialPlanItem,
    ProductionCapacity,
)
from app.services.auth_service import require_workspace_role

# NOTE: ``Opportunity`` lives in app.models.content (Work 05/09 owns it) and is
# imported from there deliberately: the planner EXTENDS that table with the
# Work 15 provenance columns rather than shadowing it, so there is exactly one
# Opportunity model.

planner_router = APIRouter(
    prefix="/workspaces/{workspace_id}/planner",
    tags=["planner-work15"],
)


def _policy(mode: str, allowed: list[str] | None = None,
            spend: float = 0.0) -> AutonomyPolicy:
    """Build a policy from request input, refusing an unknown mode."""
    try:
        resolved = AutonomyMode(str(mode or "RECOMMEND"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown autonomy {mode!r}; pick from "
                   f"{[str(m) for m in AUTONOMY_MODES]}") from exc
    actions: set[PlanningAction] = set()
    for name in (allowed or []):
        try:
            actions.add(PlanningAction(str(name)))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"unknown action {name!r}; pick from "
                       f"{[str(a) for a in PlanningAction]}") from exc
    return AutonomyPolicy(mode=resolved, allowed_actions=frozenset(actions),
                          max_daily_spend_usd=float(spend or 0.0))


def _assert_spend_allowed(workspace_id: str, estimated_usd: float, *,
                          ceiling_usd: float = 0.0) -> None:
    """Hold a planner action to its declared budget ceiling.

    ``max_daily_spend_usd`` was accepted on every request body and checked
    nowhere, so a caller could name a ceiling it was never held to.

    A POSITIVE ceiling is enforced. Zero means "no ceiling was declared", not
    "zero spend allowed" -- defaulting to zero and refusing everything would
    make the whole planner unusable, which is why the default is an explicit
    absence rather than a number.
    """
    if ceiling_usd <= 0 or estimated_usd <= 0:
        return
    if estimated_usd > ceiling_usd:
        raise HTTPException(
            status_code=403,
            detail=(f"budget refused this action: ${estimated_usd:.2f} exceeds "
                    f"the declared ceiling of ${ceiling_usd:.2f}"))


# ===========================================================================
# request bodies
# ===========================================================================


class _IngestRequest(BaseModel):
    source: str = Field(..., description=f"one of {list(SIGNAL_SOURCES)}")
    topic: str = Field(..., min_length=1, max_length=400)
    external_ref: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    scope: str = "workspace"
    confidence: float = 0.5
    evidence_verified: bool = False
    payload: dict = Field(default_factory=dict)


class _PlanRequest(BaseModel):
    horizon_days: int = 30
    goals: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    budget_usd: float = 0.0
    spent_usd: float = 0.0
    autonomy: str = "RECOMMEND"
    allowed_actions: list[str] = Field(default_factory=list)
    locale: str = ""
    constraints: dict = Field(default_factory=dict)
    series: dict[str, str] = Field(default_factory=dict)
    #: When true, run a dry planning pass WITHOUT writing a plan row, so the
    #: operator can preview before committing.
    preview: bool = False


class _CapacityRequest(BaseModel):
    locale: str = ""
    longform_per_week: float = 0.0
    shorts_per_day: float = 0.0
    ugc_per_day: float = 0.0
    localization_per_day: float = 0.0
    render_hours_per_day: float = 0.0
    review_slots_per_day: float = 0.0
    notes: str = ""


class _ActionRequest(BaseModel):
    autonomy: str = "APPROVAL"
    allowed_actions: list[str] = Field(default_factory=list)
    reason: str = ""
    max_daily_spend_usd: float = 0.0


# ===========================================================================
# signals
# ===========================================================================


@planner_router.get("/signals", summary="Observed signals with their evidence")
def list_signals(ws: Workspace = Depends(require_workspace_role("viewer")),
                 db: Session = Depends(get_db)) -> dict:
    """Every signal, refreshed for freshness, with its evidence attached.

    ``velocity`` is ``null`` for a topic seen once, because a single
    observation has no measurable rate. Read-only: a viewer listing signals
    must not rewrite their freshness or status.
    """
    signals = claim_signals(db, ws.id, persist=False)
    db.rollback()
    return {
        "workspace_id": ws.id,
        "count": len(signals),
        "sources": list(SIGNAL_SOURCES),
        "signals": [
            {
                "id": s.id,
                "source": s.source,
                "topic": s.topic,
                "topic_key": s.topic_key,
                "external_ref": s.external_ref,
                "observed_at": s.observed_at.isoformat(),
                "freshness": s.freshness,
                "scope": s.scope,
                "confidence": s.confidence,
                "status": s.status,
                "evidence_ids": s.evidence_ids,
                "recurrence": s.recurrence,
                "velocity": s.velocity,
                "usable_as_demand": s.is_usable,
            }
            for s in signals
        ],
        "note": ("a signal is an observation, not a demand estimate; velocity "
                 "is null until the same topic is observed twice"),
    }


@planner_router.post("/signals", summary="Ingest one observation")
def add_signal(body: _IngestRequest,
               ws: Workspace = Depends(require_workspace_role("member")),
               db: Session = Depends(get_db)) -> dict:
    """Record a signal. Idempotent on ``(source, topic, external_ref)``.

    A re-delivered item refreshes the existing signal instead of creating a
    second one, so a source that re-syncs cannot manufacture recurrence.
    """
    try:
        result = ingest_signal(db, ws.id, SignalIngest(
            source=body.source, topic=body.topic,
            external_ref=body.external_ref,
            evidence_ids=body.evidence_ids,
            observed_at=body.observed_at, scope=body.scope,
            confidence=body.confidence,
            evidence_verified=body.evidence_verified, payload=body.payload))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return result


# ===========================================================================
# opportunities
# ===========================================================================


@planner_router.get("/opportunities",
                    summary="Scored opportunities, with the WHY",
                    responses={200: {"model": OpportunityListOut}})
def list_opportunities(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    basis: str = "",
    verdict: str = "",
) -> dict:
    """Opportunities with their evidence, factor scores and provenance.

    ``scoring.factors[*].measured`` is the field the UI must use: a factor with
    ``measured: false`` contributed 0 because the data does not exist, not
    because the demand is zero.
    """
    query = select(Opportunity).where(Opportunity.workspace_id == ws.id)
    if basis:
        query = query.where(Opportunity.basis == basis)
    if verdict:
        query = query.where(Opportunity.dedupe_verdict == verdict)
    rows = db.scalars(query.order_by(
        Opportunity.score.desc()).limit(200)).all()
    return {
        "workspace_id": ws.id,
        "count": len(rows),
        "opportunities": [
            {
                "id": row.id,
                "topic": row.topic,
                "basis": row.basis,
                "basis_meaning": {
                    "OBSERVED": "the demand itself was seen in evidence",
                    "INFERRED": "derived from evidence by scoring",
                    "RECOMMENDED": "an AI suggestion with no measurement",
                }.get(row.basis, ""),
                "angle": row.angle,
                "audience": row.audience,
                "platforms": row.platforms,
                "format": row.format,
                "score": row.score,
                "confidence": row.confidence,
                "freshness": row.freshness,
                "brand_fit": row.brand_fit,
                "evidence": row.evidence,
                "competition_evidence": row.competition_evidence,
                "estimated_effort_hours": row.estimated_effort_hours,
                "estimated_cost_usd": row.estimated_cost_usd,
                "dedupe_verdict": row.dedupe_verdict,
                "dedupe_reason": row.dedupe_reason,
                "plan_item_id": row.plan_item_id,
                "scoring": row.components_json,
                "why": _why_text(row.components_json),
            }
            for row in rows
        ],
        "forbidden_claims": list(FORBIDDEN_CLAIMS),
        "note": ("the planner never asserts virality, revenue or a success "
                 "probability; an AI RECOMMENDED idea is capped so it cannot "
                 "outrank measured demand"),
    }


def _why_text(scoring: dict) -> str:
    """A one-line human WHY, built from the stored factor record."""
    factors = (scoring or {}).get("factors") or {}
    measured = [f for f in factors.values() if f.get("measured")]
    if not measured:
        return "no factor was measurable: this is structure, not evidence"
    top = sorted(measured, key=lambda f: -f.get("contribution", 0.0))[:3]
    return "; ".join(f"{f['factor']} {f['why']}" for f in top)


# ===========================================================================
# plans
# ===========================================================================


@planner_router.post("/plan", summary="Run a planning cycle")
def run_plan(body: _PlanRequest,
              ws: Workspace = Depends(require_workspace_role("member")),
              db: Session = Depends(get_db)) -> dict:
    """Plan a horizon. The autonomy mode decides what actually gets written.

    ``RECOMMEND`` (and ``DISABLED``) return suggestions with no persistence;
    ``APPROVAL`` and ``AUTONOMOUS`` may write plan items. Nothing here can
    publish.
    """
    policy = _policy(body.autonomy, body.allowed_actions)
    inputs = PlanningInputs(
        workspace_id=ws.id, horizon_days=body.horizon_days, goals=body.goals,
        platforms=body.platforms, budget_usd=body.budget_usd,
        spent_usd=body.spent_usd, autonomy=policy, constraints=body.constraints,
        locale=body.locale, series=body.series)
    engine = ContentPlanningEngine(db, ws.id)
    if body.preview:
        # a dry run: same computation, nothing written
        result = engine.plan(inputs)
        db.rollback()
    else:
        result = engine.plan(inputs)
        db.commit()
    payload = result.to_dict()
    payload["autonomy"] = str(policy.mode)
    payload["preview"] = body.preview
    payload["publishes"] = False
    return payload


@planner_router.get("/plans", summary="Plans and their items")
def list_plans(ws: Workspace = Depends(require_workspace_role("viewer")),
               db: Session = Depends(get_db)) -> dict:
    plans = db.scalars(select(EditorialPlan).where(
        EditorialPlan.workspace_id == ws.id).order_by(
        EditorialPlan.created_at.desc()).limit(50)).all()
    out: list[dict[str, Any]] = []
    for plan in plans:
        items = db.scalars(select(EditorialPlanItem).where(
            EditorialPlanItem.plan_id == plan.id).order_by(
            EditorialPlanItem.priority.desc())).all()
        out.append({
            "id": plan.id,
            "horizon_days": plan.horizon_days,
            "goals": plan.goals,
            "platforms": plan.platforms,
            "budget_usd": plan.budget_usd,
            "spent_usd": plan.spent_usd,
            "budget_remaining": plan.budget_remaining,
            "autonomy": plan.autonomy,
            "constraints": plan.constraints,
            "status": plan.status,
            "item_count": len(items),
            "items": [_item_dict(item) for item in items],
        })
    return {"workspace_id": ws.id, "count": len(out), "plans": out}


def _item_dict(item: EditorialPlanItem) -> dict:
    return {
        "id": item.id,
        "opportunity_id": item.opportunity_id,
        "campaign_id": item.campaign_id,
        "schedule_entry_id": item.schedule_entry_id,
        "content_format": item.content_format,
        "angle": item.angle,
        "platforms": item.platforms,
        "priority": item.priority,
        "target_date": item.target_date.isoformat() if item.target_date else "",
        "estimated_cost_usd": item.estimated_cost_usd,
        "dependencies": item.dependencies,
        "status": item.status,
        "blocked_reason": item.blocked_reason,
        "why": item.why,
    }


# ===========================================================================
# calendar
# ===========================================================================


@planner_router.get("/calendar", summary="Placed schedule + remaining capacity")
def calendar(ws: Workspace = Depends(require_workspace_role("viewer")),
             db: Session = Depends(get_db),
             days: int = 30) -> dict:
    """The placed entries and how much capacity is left.

    Every entry here is a canonical ``ScheduleEntry`` -- the planner did not
    create a second schedule.

    The window starts at the BEGINNING OF TODAY, not at "now": an entry placed
    for a slot that has already passed today is still on the calendar (it is
    due, or overdue), and hiding it would make a scheduled item look unscheduled
    for the rest of the day.
    """
    start = datetime.combine(date.today(), time.min)
    entries = db.scalars(select(ScheduleEntry).where(
        ScheduleEntry.workspace_id == ws.id,
        ScheduleEntry.run_at >= start,
        ScheduleEntry.run_at < start + timedelta(days=max(1, days)),
        ScheduleEntry.status != "CANCELLED").order_by(
        ScheduleEntry.run_at.asc())).all()
    items = db.scalars(select(EditorialPlanItem).where(
        EditorialPlanItem.workspace_id == ws.id)).all()
    ledger = load_ledger(db, ws.id, horizon_days=days)
    capacity = get_capacity(db, ws.id)
    # `days` is the horizon being asked about, so the rates must be scaled to
    # it. Omitting it reported a 30-day allowance under a `?days=7` query --
    # a 5x overstatement of the room the operator actually has.
    remaining = (capacity.remaining(ledger.committed, horizon_days=days)
                 if capacity is not None else {})
    return {
        "workspace_id": ws.id,
        "days": days,
        "entries": [
            {"id": e.id, "platform": e.platform, "run_at": e.run_at.isoformat(),
             "status": e.status, "content_item_id": e.content_item_id,
             "campaign_id": e.campaign_id}
            for e in entries
        ],
        "capacity": {
            "declared": bool(capacity is not None and not capacity.is_unbounded),
            "locale": capacity.locale if capacity is not None else "",
            "longform_per_week": capacity.longform_per_week if capacity else 0.0,
            "shorts_per_day": capacity.shorts_per_day if capacity else 0.0,
            "ugc_per_day": capacity.ugc_per_day if capacity else 0.0,
            "localization_per_day": (capacity.localization_per_day
                                     if capacity else 0.0),
            "render_hours_per_day": (capacity.render_hours_per_day
                                     if capacity else 0.0),
            "review_slots_per_day": (capacity.review_slots_per_day
                                     if capacity else 0.0),
            "notes": capacity.notes if capacity else "",
        },
        "committed": ledger.to_dict(),
        "remaining": remaining,
        "plan_item_count": len(items),
        "note": ("run times come from the platform's seed windows unless a "
                 "measured engagement window exists; no statistically optimal "
                 "time is claimed from zero observations"),
    }


# ===========================================================================
# operator actions
# ===========================================================================


def _item_or_404(db: Session, ws: Workspace, item_id: str) -> EditorialPlanItem:
    item = db.get(EditorialPlanItem, item_id)
    if item is None or item.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="plan item not found")
    return item


@planner_router.post("/items/{item_id}/approve", summary="Approve a plan item")
def approve_item(item_id: str, body: _ActionRequest,
                 ws: Workspace = Depends(require_workspace_role("member")),
                 db: Session = Depends(get_db)) -> dict:
    item = _item_or_404(db, ws, item_id)
    result = ContentPlanningEngine(db, ws.id).approve_item(item.id)
    db.commit()
    return result


@planner_router.post("/items/{item_id}/reject", summary="Reject a plan item")
def reject_item(item_id: str, body: _ActionRequest,
                ws: Workspace = Depends(require_workspace_role("member")),
                db: Session = Depends(get_db)) -> dict:
    if not body.reason:
        raise HTTPException(status_code=422,
                            detail="a rejection needs a reason")
    item = _item_or_404(db, ws, item_id)
    result = ContentPlanningEngine(db, ws.id).reject_item(item.id, body.reason)
    db.commit()
    return result


@planner_router.post("/items/{item_id}/research_more",
                     summary="Send an item back for more evidence")
def research_more(item_id: str, body: _ActionRequest,
                  ws: Workspace = Depends(require_workspace_role("member")),
                  db: Session = Depends(get_db)) -> dict:
    item = _item_or_404(db, ws, item_id)
    result = ContentPlanningEngine(db, ws.id).request_more_research(
        item.id, body.reason)
    db.commit()
    return result


@planner_router.post("/items/{item_id}/campaign",
                     summary="Create the campaign DRAFT for an item")
def create_campaign(item_id: str, body: _ActionRequest,
                    ws: Workspace = Depends(require_workspace_role("member")),
                    db: Session = Depends(get_db)) -> dict:
    """Advance the trend -> campaign flow for one item.

    Idempotent and resumable: re-running it reuses the existing campaign draft.
    """
    item = _item_or_404(db, ws, item_id)
    policy = _policy(body.autonomy, body.allowed_actions,
                     body.max_daily_spend_usd)
    # Only an AUTONOMY refusal is a 403. Catching bare Exception here reported
    # every internal bug as "forbidden", which hides real defects behind a
    # permission error.
    try:
        assert_may_advance(policy, PlanningAction.CREATE_CAMPAIGN_DRAFT)
    except AutonomyRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # budget BEFORE the action, per the estimate -> assert -> act order (§12)
    _assert_spend_allowed(ws.id, float(item.estimated_cost_usd or 0.0),
                          ceiling_usd=body.max_daily_spend_usd)
    result = run_orchestration(db, ws.id, item.id, policy=policy)
    db.commit()
    return result.to_dict()


@planner_router.post("/items/{item_id}/schedule", summary="Place an item")
def schedule_item(item_id: str, body: _ActionRequest,
                  ws: Workspace = Depends(require_workspace_role("member")),
                  db: Session = Depends(get_db)) -> dict:
    """Place an item on the calendar and write a canonical ScheduleEntry.

    This stops at SCHEDULED. It does not publish and does not enqueue a job;
    the existing Scheduler agent and publication approval path own both.
    """
    item = _item_or_404(db, ws, item_id)
    policy = _policy(body.autonomy, body.allowed_actions,
                     body.max_daily_spend_usd)
    # budget BEFORE the action, per the estimate -> assert -> act order (§12)
    _assert_spend_allowed(ws.id, float(item.estimated_cost_usd or 0.0),
                         ceiling_usd=body.max_daily_spend_usd)
    engine = ContentPlanningEngine(db, ws.id)
    try:
        # ScheduleEntry.content_item_id is a ContentItem key -- the canonical
        # Scheduler stores one there and keys its own idempotency on it. Writing
        # the item's OPPORTUNITY id into that column would put a foreign key
        # type into the shared store, so the real ContentItem is resolved
        # instead, and the column is left NULL when nothing was produced yet.
        content_item_id = ""
        if item.opportunity_id:
            content_item_id = db.scalar(select(ContentItem.id).where(
                ContentItem.workspace_id == ws.id,
                ContentItem.opportunity_id == item.opportunity_id
            ).limit(1)) or ""
        if not content_item_id and item.campaign_id:
            content_item_id = db.scalar(select(ContentItem.id).where(
                ContentItem.workspace_id == ws.id,
                ContentItem.campaign_id == item.campaign_id
            ).limit(1)) or ""

        placement = None
        for platform in (item.platforms or ["threads"]):
            placement = place_request(db, ws.id, CalendarRequest(
                plan_item_id=item.id, content_id=content_item_id,
                campaign_id=item.campaign_id or "", platform=platform,
                content_format=item.content_format,
                target_date=(item.target_date.date()
                             if item.target_date else date.today()),
                locale=(ws.timezone or "UTC")))
            if not placement.is_blocked:
                break
        result = engine.schedule_item(item.id, policy, placement=placement)
    except AutonomyRefused as exc:
        # an autonomy refusal is a 403; anything else is an internal defect and
        # must surface as one rather than masquerading as a permission error
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    if placement is not None and not placement.is_blocked:
        entries = build_schedule_entries(db, ws.id, item.plan_id, [placement])
        entry = entries[0] if entries else {}
        result["schedule_entry"] = entry
        result["evidence_backed_slot"] = placement.evidence_backed
        result["why_scheduled"] = placement.reason
        # Record the link BACK onto the plan item. Without it the item reported
        # schedule_entry_id: null forever, and a dependent item could never see
        # its dependency as satisfied.
        if entry.get("entry_id"):
            item.schedule_entry_id = entry["entry_id"]
            db.flush()
            result["schedule_entry_id"] = entry["entry_id"]
    db.commit()
    result["publishes"] = False
    return result


@planner_router.post("/capacity", summary="Declare production capacity")
def set_capacity(body: _CapacityRequest,
                 ws: Workspace = Depends(require_workspace_role("admin")),
                 db: Session = Depends(get_db)) -> dict:
    row = upset_capacity(
        db, ws.id, locale=body.locale,
        longform_per_week=body.longform_per_week,
        shorts_per_day=body.shorts_per_day, ugc_per_day=body.ugc_per_day,
        localization_per_day=body.localization_per_day,
        render_hours_per_day=body.render_hours_per_day,
        review_slots_per_day=body.review_slots_per_day, notes=body.notes)
    ledger = load_ledger(db, ws.id, locale=body.locale)
    db.commit()
    return {"workspace_id": ws.id, "locale": row.locale,
            "is_unbounded": row.is_unbounded,
            "declared": {k: v for k, v in row.remaining().items()},
            "declared_note": "room over a 30-day horizon; a null pool is "
                             "undeclared (unbounded), not zero",
            "committed": ledger.to_dict()}


@planner_router.post("/capacity/check", summary="Would this batch fit?")
def check(body_capacity: _CapacityRequest,
          ws: Workspace = Depends(require_workspace_role("viewer")),
          db: Session = Depends(get_db)) -> dict:
    """Dry-run the capacity gate against a hypothetical batch.

    Takes a HYPOTHETICAL capacity in the request body and answers "would this
    fit" WITHOUT touching the stored row. It builds the candidate in memory
    rather than calling ``upset_capacity``: that helper persists, and using it
    here would create/overwrite the real row with all-zero defaults -- a
    viewer writing to the workspace, and any interleaved commit wiping the
    operator's declared limits.
    """
    # an all-zero request means "use what is actually declared"
    hypothetical = ProductionCapacity(
        workspace_id=ws.id, locale=body_capacity.locale or "",
        longform_per_week=body_capacity.longform_per_week,
        shorts_per_day=body_capacity.shorts_per_day,
        ugc_per_day=body_capacity.ugc_per_day,
        localization_per_day=body_capacity.localization_per_day,
        render_hours_per_day=body_capacity.render_hours_per_day,
        review_slots_per_day=body_capacity.review_slots_per_day)
    if hypothetical.is_unbounded:
        hypothetical = get_capacity(db, ws.id, locale=body_capacity.locale or "")
    ledger = load_ledger(db, ws.id, locale=body_capacity.locale or "")
    # The batch is the capacity LIMIT itself, so asking "does N fit in a limit
    # of N" could never report a shortfall. The batch is one item PER declared
    # daily rate, checked over a 30-day horizon -- i.e. "can this workspace
    # actually produce a full horizon of work?".
    horizon = 30
    declared = hypothetical.RATE_UNITS and float(
        hypothetical.shorts_per_day or 0.0)
    batch_size = int(declared * horizon) if declared else 0
    decision = check_capacity(hypothetical, ledger, items=[
        {"content_format": "SHORT", "estimated_cost_usd": 1.2}] * batch_size,
        horizon_days=horizon)
    db.rollback()
    return decision.to_dict()


# ===========================================================================
# policy + feedback
# ===========================================================================


@planner_router.get("/policy", summary="The autonomy table, as data")
def policy(ws: Workspace = Depends(require_workspace_role("viewer"))) -> dict:
    return {
        "modes": list(AUTONOMY_MODES),
        "actions": [str(a) for a in PlanningAction],
        "table": describe_autonomy(),
        "publishes": False,
        "note": ("planning autonomy never implies publishing autonomy; no "
                 "planning mode can publish"),
    }


@planner_router.get("/memory", summary="What the planner already knows")
def memory(ws: Workspace = Depends(require_workspace_role("viewer")),
           db: Session = Depends(get_db),
           topic: str = "") -> dict:
    brief = consult_memory(db, ws.id, topic=topic)
    return {"workspace_id": ws.id, **brief.to_dict(),
            "memories": brief.memories}


@planner_router.get("/feedback/{item_id}",
                    summary="Measured outcome for a plan item")
def feedback(item_id: str,
             ws: Workspace = Depends(require_workspace_role("viewer")),
             db: Session = Depends(get_db)) -> dict:
    record = collect_feedback(db, ws.id, item_id)
    return record.to_dict()


@planner_router.post("/feedback/{item_id}",
                     summary="Record an outcome; derive a lesson if warranted")
def record(item_id: str, body: _ActionRequest,
           ws: Workspace = Depends(require_workspace_role("member")),
           db: Session = Depends(get_db)) -> dict:
    """Write the factual outcome, and a generalised lesson only when warranted.

    A lesson needs at least ``MIN_SAMPLE`` measured items and an effect size
    above ``MIN_EFFECT``; below that the endpoint returns ``lesson: null`` rather
    than training a global rule on one post.

    The item is workspace-checked first. Without that, a caller could name
    another workspace's plan-item id and write a fabricated outcome into their
    OWN memory -- which the planner then consults as trusted §4 knowledge.
    """
    _item_or_404(db, ws, item_id)
    out = record_outcome(db, ws.id, item_id)
    db.commit()
    return out
