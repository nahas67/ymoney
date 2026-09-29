"""API v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import auth, autopilot, content, workspaces
from app.api.v1.api_keys import router as api_keys_router
from app.api.v1.brands import brand_router
from app.api.v1.campaigns import campaign_flows_router, campaign_shorts_router
from app.api.v1.connections import connections_router
from app.api.v1.content import (
    assets_router,
    calendar_router,
    campaigns_router,
    content_router,
    cycles_router,
    videos_router,
)
from app.api.v1.creative import creative_router
from app.api.v1.experiments import experiments_router
from app.api.v1.inbox import inbox_router
from app.api.v1.intelligence_decisions import router as intelligence_decisions_router
from app.api.v1.intelligence_evidence import (
    intelligence_evidence_router,
)
from app.api.v1.intelligence_routing import intelligence_router as intelligence_routing_router
from app.api.v1.knowledge import knowledge_router
from app.api.v1.lessons import lessons_router
from app.api.v1.lipsync import dubbing_plans_router, lipsync_router
from app.api.v1.live import router as live_router
from app.api.v1.localization import localization_router
from app.api.v1.longform import longform_router
from app.api.v1.misc import (
    activity_router,
    agents_router,
    analytics_router,
    costs_router,
    jobs_router,
    logs_router,
    memory_router,
    public_router,
    publishing_router,
    system_router,
)
from app.api.v1.performance import performance_router
from app.api.v1.safety import (
    cost_intel_router,
    decision_router,
    safety_router,
)
from app.api.v1.telegram import router as telegram_router
from app.api.v1.timelines import timelines_router
from app.api.v1.ugc import avatars_router, ugc_router
from app.api.v1.webhooks import router as webhooks_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(workspaces.router)
api_router.include_router(autopilot.router)
api_router.include_router(content.router)
api_router.include_router(content_router)
api_router.include_router(videos_router)
api_router.include_router(cycles_router)
api_router.include_router(campaigns_router)
api_router.include_router(campaign_flows_router)
api_router.include_router(campaign_shorts_router)
api_router.include_router(intelligence_decisions_router)
api_router.include_router(calendar_router)
api_router.include_router(assets_router)
api_router.include_router(publishing_router)
api_router.include_router(analytics_router)
api_router.include_router(memory_router)
api_router.include_router(performance_router)
api_router.include_router(agents_router)
api_router.include_router(activity_router)
api_router.include_router(system_router)
api_router.include_router(public_router)
api_router.include_router(logs_router)
api_router.include_router(jobs_router)
api_router.include_router(costs_router)
api_router.include_router(safety_router)
api_router.include_router(decision_router)
api_router.include_router(connections_router)
api_router.include_router(telegram_router)
api_router.include_router(longform_router)
api_router.include_router(localization_router)
api_router.include_router(timelines_router)
api_router.include_router(intelligence_routing_router)
api_router.include_router(lessons_router)
api_router.include_router(live_router)
api_router.include_router(experiments_router)
# intelligence evidence (browser runs + verification ledger, Work 05 Lane C)
api_router.include_router(intelligence_evidence_router)
# workspace API keys (third-party auth, separate from user JWT)
api_router.include_router(api_keys_router)
api_router.include_router(webhooks_router)
# cost intelligence lives under the same /costs prefix as the summary router
api_router.include_router(cost_intel_router)
# lip-sync jobs + speaker-aware dubbing plans (Work 07 Lane B)
api_router.include_router(lipsync_router)
api_router.include_router(dubbing_plans_router)
# UGC projects (9 presets) + consent-gated avatars (Work 07 Lane C)
api_router.include_router(ugc_router)
api_router.include_router(avatars_router)
# brand identities + BrandDNA + effective policy + consistency verifier
# (Work 08 Lane A; the Work 01 white-label /brand routes stay untouched)
api_router.include_router(brand_router)
# Creative Director: NL → typed commands → preview → versioned apply/undo
# (Work 08 Lane B)
api_router.include_router(creative_router)
# Unified social inbox (Work 09 Lane D): one router, two mounts —
#   /workspaces/{workspace_id}/inbox/...  canonical (wsApi + path RBAC)
#   /inbox/... ?workspace_id=...          literal contract path
api_router.include_router(inbox_router, prefix="/workspaces/{workspace_id}")
api_router.include_router(inbox_router)
# Knowledge surface (Work 10 Lane D): one router, two mounts —
#   /workspaces/{workspace_id}/knowledge/...  canonical (wsApi + path RBAC)
#   /knowledge/... ?workspace_id=...          literal contract path
api_router.include_router(knowledge_router, prefix="/workspaces/{workspace_id}")
api_router.include_router(knowledge_router)
