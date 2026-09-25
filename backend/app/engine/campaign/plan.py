"""Derivation planning: master video -> campaign plan row.

Uses the existing Campaign row (models/content.py) — this module never
creates Campaigns. Idempotent per campaign_id: re-planning updates the
mutable fields instead of duplicating rows.
"""

from __future__ import annotations


class PlanError(ValueError):
    pass


def build_derivation_plan(
    session,
    campaign_id: str,
    master_content_id: str,
    platforms: list[str],
    desired_shorts: int = 8,
    *,
    goal: str = "",
    duration_min: float = 20.0,
    duration_max: float = 55.0,
    diversity_config: dict | None = None,
    posting_window: dict | None = None,
    frequency: str = "daily",
) -> object:
    """Create (or refresh) the CampaignPlan for a campaign. Returns the row."""
    from app.models import Campaign, ContentItem
    from app.models.campaign import CampaignPlan

    if not platforms:
        raise PlanError("at least one target platform is required")
    if desired_shorts < 1:
        raise PlanError("desired_shorts must be >= 1")
    if duration_max <= duration_min:
        raise PlanError("duration_max must exceed duration_min")

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise PlanError(f"campaign '{campaign_id}' not found")
    master = session.get(ContentItem, master_content_id)
    if master is None or master.workspace_id != campaign.workspace_id:
        raise PlanError(f"master content '{master_content_id}' not found")

    normalized_platforms = sorted({str(p).strip().lower() for p in platforms if str(p).strip()})
    if not normalized_platforms:
        raise PlanError("at least one target platform is required")

    plan = session.query(CampaignPlan).filter(
        CampaignPlan.campaign_id == campaign_id,
    ).one_or_none()
    if plan is None:
        plan = CampaignPlan(
            workspace_id=campaign.workspace_id,
            campaign_id=campaign_id,
            master_content_id=master_content_id,
        )
        session.add(plan)

    plan.master_content_id = master_content_id
    plan.goal = goal or campaign.goal or ""
    plan.target_platforms = normalized_platforms
    plan.desired_shorts = int(desired_shorts)
    plan.duration_min = float(duration_min)
    plan.duration_max = float(duration_max)
    plan.diversity_config = dict(diversity_config or {})
    plan.posting_window = dict(posting_window or {})
    plan.frequency = frequency
    if plan.status == "FAILED":
        plan.status = "DRAFT"
        plan.error = ""
    session.flush()
    return plan
