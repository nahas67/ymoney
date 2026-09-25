"""YMONEY intelligence package (Work 05, Lane B).

Provider-independent intelligence layer: secret sanitization (shared by all
lanes), model routing, and context budgeting. Claude tooling is optional;
everything here works fully offline.
"""

from app.engine.intelligence.context_budget import (
    PREDEFINED_PINNED_CATEGORIES,
    BudgetResult,
    Category,
    ContextBudgetManager,
    ContextItem,
    ContextReference,
    classify,
    estimate_tokens,
)
from app.engine.intelligence.router import (
    LOCAL_TIERS,
    REMOTE_TIERS,
    TIERS,
    LLMRoutingError,
    ModelCapability,
    ModelCapabilityRegistry,
    ModelRouter,
    PrivacyRefusal,
    RouteRequest,
    RoutingDecision,
    default_router,
    get_intelligence_settings,
    router_health,
    routing_log,
)
from app.engine.intelligence.sanitize import (
    REDACTED,
    SecretLeakError,
    assert_no_secrets,
    redact_secrets,
)

__all__ = [
    "PREDEFINED_PINNED_CATEGORIES",
    "LOCAL_TIERS",
    "REDACTED",
    "REMOTE_TIERS",
    "TIERS",
    "BudgetResult",
    "Category",
    "ContextBudgetManager",
    "ContextItem",
    "ContextReference",
    "LLMRoutingError",
    "ModelCapability",
    "ModelCapabilityRegistry",
    "ModelRouter",
    "PrivacyRefusal",
    "RouteRequest",
    "RoutingDecision",
    "SecretLeakError",
    "assert_no_secrets",
    "classify",
    "default_router",
    "estimate_tokens",
    "get_intelligence_settings",
    "redact_secrets",
    "router_health",
    "routing_log",
]
