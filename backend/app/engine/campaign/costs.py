"""Campaign cost tracking over the shared cost ledger."""

from __future__ import annotations


def track_campaign_cost(
    session,
    ws,
    campaign_id: str,
    category: str,
    amount: float,
    detail: dict | None = None,
) -> float:
    """Record spend on the cost ledger and bump the plan total.

    The row is written in the caller's session: ``services.cost.track_cost``
    opens its own session, which deadlocks on SQLite while the stage
    transaction holds the write lock (same precedent as
    ``engine/longform/stages_voice_timeline.py``). Fields mirror
    ``track_cost`` exactly (rounded amount, provider ``campaign``,
    campaign_id in the detail).
    """
    from app.models import CostEntry
    from app.models.campaign import CampaignPlan

    ws_id = getattr(ws, "id", ws)
    amount = float(amount or 0.0)
    if amount > 0:
        session.add(CostEntry(
            workspace_id=ws_id, category=category,
            amount_usd=round(amount, 6), provider="campaign",
            detail_json={"campaign_id": campaign_id, **(detail or {})},
        ))
        session.flush()
    plan = session.query(CampaignPlan).filter(
        CampaignPlan.campaign_id == campaign_id,
        CampaignPlan.workspace_id == ws_id,
    ).one_or_none()
    if plan is None:
        return 0.0
    plan.cost_usd = float(plan.cost_usd or 0.0) + amount
    session.flush()
    return float(plan.cost_usd)
