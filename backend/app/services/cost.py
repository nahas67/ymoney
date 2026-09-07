"""Cost accounting with budget enforcement."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import CostEntry
from app.models.base import utcnow

# Rough public price table (USD per 1M tokens). Configurable via data file.
_PRICES_PATH = Path(__file__).resolve().parent.parent / "data" / "model_prices.json"


def load_prices() -> dict:
    try:
        return json.loads(_PRICES_PATH.read_text())
    except Exception:
        return {
            "default": {"input": 0.15, "output": 0.60},
            "gpt-4o-mini": {"input": 0.15, "output": 0.60},
            "gpt-4o": {"input": 2.50, "output": 10.00},
        }


def estimate_llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = load_prices()
    p = prices.get(model, prices["default"])
    return (prompt_tokens / 1e6) * p["input"] + (completion_tokens / 1e6) * p["output"]


def track_cost(
    workspace_id: str,
    category: str,
    amount_usd: float,
    provider: str = "",
    cycle_id: str | None = None,
    detail: dict | None = None,
    is_estimate: bool = False,
) -> float:
    if amount_usd <= 0:
        return 0.0
    with session_scope() as s:
        s.add(
            CostEntry(
                workspace_id=workspace_id,
                category=category,
                amount_usd=round(amount_usd, 6),
                provider=provider,
                cycle_id=cycle_id,
                detail_json=detail or {},
                is_estimate=is_estimate,
            )
        )
    logger.debug(f"cost[{category}] ${amount_usd:.4f} ws={workspace_id}")
    return amount_usd


def spent_since(workspace_id: str, hours: float = 24.0) -> float:
    since = utcnow() - timedelta(hours=hours)
    with session_scope() as s:
        total = s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == workspace_id,
                CostEntry.created_at >= since,
            )
        )
    return float(total or 0.0)


def _workspace_budget_limits(workspace_id: str) -> tuple[float, float]:
    """Workspace safety budgets, falling back to global settings.

    Safety Center values live in ``Workspace.settings_json["safety"]``; the
    decision engine already honors them, so budget enforcement must read the
    same source or a workspace's configured caps are silently ignored here.
    A lookup failure falls back to global defaults rather than blocking spend.
    """
    from app.models import Workspace

    daily = float(settings.daily_budget_usd)
    per_video = float(settings.per_video_budget_usd)
    try:
        with session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            if ws and ws.settings_json:
                safety = ws.settings_json.get("safety") or {}
                daily = float(safety.get("daily_budget_usd", daily))
                per_video = float(safety.get("per_video_budget_usd", per_video))
    except (TypeError, ValueError):
        logger.warning(f"invalid workspace budget config for {workspace_id}; using defaults")
    return daily, per_video


def budget_available(workspace_id: str) -> tuple[bool, float]:
    """Returns (within_budget, remaining_daily_budget)."""
    spent = spent_since(workspace_id, hours=24.0)
    daily, _ = _workspace_budget_limits(workspace_id)
    remaining = daily - spent
    return remaining > 0, remaining


def assert_can_spend(workspace_id: str, estimated_usd: float) -> None:
    ok, remaining = budget_available(workspace_id)
    if not ok:
        raise BudgetExceededError(f"daily budget exhausted (${remaining:.2f} left)")
    _, per_video = _workspace_budget_limits(workspace_id)
    if estimated_usd > per_video:
        raise BudgetExceededError(
            f"estimated video cost ${estimated_usd:.2f} exceeds per-video budget "
            f"${per_video:.2f}"
        )


class BudgetExceededError(Exception):
    pass
