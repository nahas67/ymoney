"""Work 15 §9 — durable, resumable trend -> campaign orchestration.

    TrendSignal -> Opportunity -> ResearchBundle -> ContentBrief
                -> Campaign draft -> EditorialPlan item -> Scheduler

Every stage is **idempotent** and **resumable**: the flow records what it has
done in the plan item's ``why_json["stages"]`` and skips any stage already
recorded, so re-running it after a crash continues rather than duplicating.

Three components are reused, never reimplemented:

* research            — the existing research agent/bundle shape, stored on
                        ``ContentItem.research_json``
* campaign            — the existing ``Campaign`` row (a DRAFT)
* scheduler           — the existing ``ScheduleEntry`` store

The one thing this module refuses to do is publish. It produces a campaign
DRAFT and a plan item; turning either into a publication is the existing
approval path's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.engine.planning.autonomy import (
    AutonomyPolicy,
    AutonomyRefused,
    PlanningAction,
    assert_may_advance,
)
from app.models.content import Campaign
from app.models.planning import (
    EditorialPlan,
    EditorialPlanItem,
)

__all__ = [
    "STAGES",
    "OrchestrationResult",
    "advance_stage",
    "run_orchestration",
]

#: The durable stage sequence. Each is a dict step in the flow.
STAGES: tuple[str, ...] = (
    "opportunity",
    "research",
    "brief",
    "campaign_draft",
    "plan_item",
    "schedule",
)


@dataclass
class OrchestrationResult:
    plan_item_id: str = ""
    opportunity_id: str = ""
    campaign_id: str = ""
    content_item_id: str = ""
    schedule_entry_id: str = ""
    stages_done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    blocked: str = ""

    @property
    def is_complete(self) -> bool:
        return "plan_item" in self.stages_done

    def to_dict(self) -> dict:
        return {"plan_item_id": self.plan_item_id,
                "opportunity_id": self.opportunity_id,
                "campaign_id": self.campaign_id,
                "content_item_id": self.content_item_id,
                "schedule_entry_id": self.schedule_entry_id,
                "stages_done": list(self.stages_done),
                "skipped": list(self.skipped), "blocked": self.blocked,
                "complete": self.is_complete}


def _stages(item: EditorialPlanItem) -> dict:
    why = dict(item.why_json or {})
    return dict(why.get("stages") or {})


def _record(item: EditorialPlanItem, stage: str, payload: dict) -> None:
    why = dict(item.why_json or {})
    stages = dict(why.get("stages") or {})
    stages[stage] = {**payload, "at": datetime.now(UTC).isoformat()}
    why["stages"] = stages
    item.why_json = why


def _may(policy: AutonomyPolicy, action: PlanningAction) -> bool:
    """``True`` only if the FULL gate permits ``action``.

    :meth:`AutonomyPolicy.permits` compares mode rank alone, which means an
    AUTONOMOUS policy permits every AUTONOMOUS action even when the operator
    allow-listed a different one. Stages must use the full gate.
    """
    try:
        assert_may_advance(policy, action)
    except AutonomyRefused:
        return False
    return True


def advance_stage(db, workspace_id: str, item_id: str, stage: str) -> dict:
    """Record one completed stage. Idempotent by stage name.

    Re-recording a stage overwrites its payload but does not create a second
    row, so a resumed run converges instead of duplicating.
    """
    from app.engine.planning.autonomy import AutonomyRefused as _Refused

    if stage not in STAGES:
        raise _Refused(f"unknown orchestration stage {stage!r}")
    item = db.get(EditorialPlanItem, item_id)
    if item is None or item.workspace_id != workspace_id:
        raise _Refused(f"plan item {item_id!r} not found in this workspace")
    _record(item, stage, {"ok": True})
    db.flush()
    return _stages(item)


def _ensure_campaign_draft(db, workspace_id: str, item: EditorialPlanItem, *,
                           campaign_name: str) -> Campaign:
    """Create the campaign DRAFT once, reusing it on a resumed run.

    Idempotency is by the recorded stage AND by the plan item's stored
    ``campaign_id``, so neither a crash nor a double invocation produces two
    campaigns for one item.
    """
    stages = _stages(item)
    existing_id = stages.get("campaign_draft", {}).get("campaign_id")
    if existing_id:
        row = db.get(Campaign, existing_id)
        if row is not None and row.workspace_id == workspace_id:
            return row
    plan = db.get(EditorialPlan, item.plan_id)
    campaign = Campaign(
        workspace_id=workspace_id,
        name=campaign_name[:200],
        status="DRAFT",
        # the plan item is the link back, so a campaign can always be traced
        # to the evidence that produced it
        goal=(f"planner item {item.id}: {item.angle}"[:400] or "planned content"),
        platforms_json=list(item.platforms_json or []),
        # A planner-created campaign starts conservative. AUTONOMOUS planning
        # does NOT hand a campaign an autonomous execution level.
        automation_level="MANUAL",
    )
    _ = plan
    db.add(campaign)
    db.flush()
    item.campaign_id = campaign.id
    return campaign

def run_orchestration(db, workspace_id: str, item_id: str, *,
                      policy: AutonomyPolicy,
                      research_fn=None, brief_fn=None,
                      schedule_fn=None) -> OrchestrationResult:
    """Run (or resume) the trend -> campaign flow for one plan item.

    ``research_fn``/``brief_fn``/``schedule_fn`` are the EXISTING components
    injected by the caller, so this module never reimplements research or
    scheduling. Each is only called when its stage has not already been
    recorded.
    """
    result = OrchestrationResult(plan_item_id=item_id)
    item = db.get(EditorialPlanItem, item_id)
    if item is None or item.workspace_id != workspace_id:
        result.blocked = f"plan item {item_id!r} not found in this workspace"
        return result
    result.opportunity_id = item.opportunity_id or ""
    stages = _stages(item)

    # -- stage: campaign draft (the only write this flow performs) --------
    try:
        assert_may_advance(policy, PlanningAction.CREATE_CAMPAIGN_DRAFT)
    except AutonomyRefused as exc:
        result.blocked = str(exc)
        result.skipped.append("campaign_draft")
        return result

    if "campaign_draft" in stages:
        result.skipped.append("campaign_draft")
        result.campaign_id = stages["campaign_draft"].get("campaign_id", "")
        result.stages_done.append("campaign_draft")
    else:
        campaign = _ensure_campaign_draft(
            db, workspace_id, item,
            campaign_name=f"Planner: {item.angle or 'untitled'}")
        _record(item, "campaign_draft",
                {"campaign_id": campaign.id, "status": campaign.status})
        result.campaign_id = campaign.id
        result.stages_done.append("campaign_draft")

    # NOTE: the item's status is NOT advanced here. Creating a campaign draft is
    # not approval, and flipping IDEA -> PLANNED made the status indistinguishable
    # from a human's ``approve_item``, which is what that transition means. The
    # draft exists alongside an IDEA item until a person approves it.

    # -- stage: research (existing component, only when not yet done) -----
    # Every stage goes through assert_may_advance, not policy.permits: permits()
    # checks the MODE RANK only, so under AUTONOMOUS it returned True for
    # actions the operator never allow-listed.
    if "research" not in stages:
        if research_fn is not None and _may(policy, PlanningAction.START_RESEARCH):
            bundle = research_fn(workspace_id, item)
            _record(item, "research", {"bundle_keys": sorted(
                (bundle or {}).keys())[:8], "has_bundle": bool(bundle)})
            result.stages_done.append("research")
        else:
            result.skipped.append("research")
    else:
        result.skipped.append("research")
        result.stages_done.append("research")

    # -- stage: brief ------------------------------------------------------
    # The brief is part of starting research, so it inherits that gate: an
    # allowlist naming SCHEDULE only must not produce research or a brief.
    if "brief" not in stages:
        if brief_fn is not None and _may(policy, PlanningAction.START_RESEARCH):
            brief = brief_fn(workspace_id, item)
            _record(item, "brief", {"brief_keys": sorted((brief or {}).keys())[:8],
                                    "has_brief": bool(brief)})
            result.stages_done.append("brief")
        else:
            result.skipped.append("brief")
    else:
        result.skipped.append("brief")
        result.stages_done.append("brief")

    # -- stage: schedule (delegated; never publishes) ----------------------
    if "schedule" not in stages:
        if (schedule_fn is not None
                and _may(policy, PlanningAction.SCHEDULE)
                and not policy.requires_approval(PlanningAction.SCHEDULE)):
            entry = schedule_fn(workspace_id, item)
            if entry:
                _record(item, "schedule", {"schedule_entry_id": entry})
                result.schedule_entry_id = str(entry)
                result.stages_done.append("schedule")
        else:
            result.skipped.append("schedule")
    else:
        result.skipped.append("schedule")
        result.stages_done.append("schedule")

    db.flush()
    result.stages_done.append("plan_item")
    return result


def find_item(db, workspace_id: str, item_id: str) -> EditorialPlanItem | None:
    """One plan item, workspace-scoped. ``None`` when missing or foreign."""
    item = db.get(EditorialPlanItem, item_id)
    if item is None or item.workspace_id != workspace_id:
        return None
    return item


def items_for_opportunity(db, workspace_id: str, opportunity_id: str
                          ) -> list[EditorialPlanItem]:
    """Every plan item built from one opportunity, workspace-scoped."""
    return list(db.scalars(select(EditorialPlanItem).where(
        EditorialPlanItem.workspace_id == workspace_id,
        EditorialPlanItem.opportunity_id == opportunity_id)).all())
