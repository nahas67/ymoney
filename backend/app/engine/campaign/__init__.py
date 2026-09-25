from __future__ import annotations

"""Master-to-shorts campaign engine package (Work 04).

Lane A: plan, diversity, repair, hooks, shorts, qc, costs (+ models/migration).
Lane B: platforms, variants, metadata, covers, publish flow.
Lane C: analytics.
"""

from app.engine.campaign import (
    analytics,
    costs,
    covers,
    diversity,
    hooks,
    metadata,
    plan,
    platforms,
    publish_flow,
    qc,
    repair,
    shorts,
    variants,
)

__all__ = [
    "analytics", "costs", "covers", "diversity", "hooks", "metadata",
    "plan", "platforms", "publish_flow", "qc", "repair", "shorts", "variants",
]
