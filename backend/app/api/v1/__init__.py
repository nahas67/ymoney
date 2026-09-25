"""API v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import auth, autopilot, content, workspaces
from app.api.v1.api_keys import router as api_keys_router
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
from app.api.v1.intelligence_decisions import router as intelligence_decisions_router
from app.api.v1.intelligence_evidence import (
    intelligence_evidence_router,
)
from app.api.v1.intelligence_routing import intelligence_router as intelligence_routing_router
from app.api.v1.live import router as live_router
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
from app.api.v1.safety import (
    cost_intel_router,
    decision_router,
    safety_router,
)
from app.api.v1.telegram import router as telegram_router
from app.api.v1.timelines import timelines_router
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
api_router.include_router(timelines_router)
api_router.include_router(intelligence_routing_router)
api_router.include_router(live_router)
# intelligence evidence (browser runs + verification ledger, Work 05 Lane C)
api_router.include_router(intelligence_evidence_router)
# workspace API keys (third-party auth, separate from user JWT)
api_router.include_router(api_keys_router)
api_router.include_router(webhooks_router)
# cost intelligence lives under the same /costs prefix as the summary router
api_router.include_router(cost_intel_router)
