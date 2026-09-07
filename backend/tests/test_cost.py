"""Cost accounting & budget enforcement tests."""

import pytest

from app.services import cost


def test_llm_cost_estimation_monotonic():
    cheap = cost.estimate_llm_cost("gpt-4o-mini", 1000, 1000)
    pricey = cost.estimate_llm_cost("gpt-4o", 1000, 1000)
    assert 0 < cheap < pricey


def test_unknown_model_uses_default_price():
    assert cost.estimate_llm_cost("totally-unknown", 1_000_000, 0) > 0


def test_track_and_query(workspace_with_user):
    ws = workspace_with_user["workspace"]
    cost.track_cost(ws, "llm", 0.25, provider="mock-test")
    spent = cost.spent_since(ws, hours=24)
    assert spent >= 0.25


def test_budget_gate_blocks_when_exhausted(workspace_with_user):
    from app.core.config import settings

    ws = workspace_with_user["workspace"]
    original = settings.daily_budget_usd
    try:
        settings.daily_budget_usd = 0.01
        cost.track_cost(ws, "video", 5.0)
        with pytest.raises(cost.BudgetExceededError):
            cost.assert_can_spend(ws, estimated_usd=0.02)
    finally:
        settings.daily_budget_usd = original


def test_per_video_cap_enforced(workspace_with_user):
    from app.core.config import settings

    original = settings.per_video_budget_usd
    try:
        settings.per_video_budget_usd = 0.05
        with pytest.raises(cost.BudgetExceededError):
            cost.assert_can_spend(workspace_with_user["workspace"], estimated_usd=0.10)
    finally:
        settings.per_video_budget_usd = original


def test_zero_cost_is_ignored(workspace_with_user):
    ws = workspace_with_user["workspace"]
    before = cost.spent_since(ws, hours=24)
    cost.track_cost(ws, "llm", 0.0)
    after = cost.spent_since(ws, hours=24)
    assert before == after


def test_workspace_safety_budget_overrides_global(workspace_with_user, db_session):
    """Safety Center caps set per workspace must bind spend enforcement."""
    from app.models import Workspace

    ws_id = workspace_with_user["workspace"]
    ws = db_session.get(Workspace, ws_id)
    ws.settings_json = {
        "safety": {"daily_budget_usd": 0.0, "per_video_budget_usd": 0.0}
    }
    db_session.commit()

    # Zero-daily workspace: nothing can be spent even though the global
    # default budget (settings.daily_budget_usd) is untouched and larger.
    cost.track_cost(ws_id, "video", 1.0)
    with pytest.raises(cost.BudgetExceededError):
        cost.assert_can_spend(ws_id, estimated_usd=0.01)
