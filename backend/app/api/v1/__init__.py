"""API v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import auth, autopilot, content, workspaces
from app.api.v1.activity import activity_ledger_router
from app.api.v1.api_keys import router as api_keys_router
from app.api.v1.archives import archives_router
from app.api.v1.brands import brand_router
from app.api.v1.campaigns import campaign_flows_router, campaign_shorts_router
from app.api.v1.captions import captions_router
from app.api.v1.comments import comments_router
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
from app.api.v1.distribution import distribution_router
from app.api.v1.experiments import experiments_router
from app.api.v1.exports import exports_router
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
from app.api.v1.media_intel import media_intel_router
from app.api.v1.media_intel_audio import media_intel_audio_router
from app.api.v1.media_intel_edits import media_intel_edits_router
from app.api.v1.media_intel_faces import media_intel_faces_router
from app.api.v1.media_intel_qc import media_intel_qc_router
from app.api.v1.media_intel_reframe import media_intel_reframe_router
from app.api.v1.media_intel_speech import media_intel_speech_router
from app.api.v1.media_intel_visual import visual_router
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
from app.api.v1.music import music_router
from app.api.v1.notifications import notifications_router
from app.api.v1.ops import ops_router, retention_router
from app.api.v1.performance import performance_router
from app.api.v1.planner import planner_router
from app.api.v1.preview import voice_preview_router
from app.api.v1.projects import projects_router
from app.api.v1.providers import (
    provider_maturity_router,
    workspace_maturity_router,
)
from app.api.v1.reviews import reviews_router, revisions_router
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
# Projects + project RBAC (Work 11 Lane F): canonical only —
#   /workspaces/{workspace_id}/projects/...
api_router.include_router(projects_router)
# Reviews + comments + revisions (Work 11 Lane R): canonical only —
#   /workspaces/{workspace_id}/reviews/...
#   /workspaces/{workspace_id}/comments/...
#   /workspaces/{workspace_id}/revisions/...
api_router.include_router(reviews_router)
api_router.include_router(comments_router)
api_router.include_router(revisions_router)
# Activity ledger + notifications + archives + enterprise ops
# (Work 11 Lane L): canonical only —
#   /workspaces/{workspace_id}/activity          read-only ledger
#   /workspaces/{workspace_id}/notifications     own inbox rows
#   /workspaces/{workspace_id}/projects/...      archive/unarchive
#   /workspaces/{workspace_id}/ops/overview      admin aggregates
#   /workspaces/{workspace_id}/retention         admin policy
api_router.include_router(activity_ledger_router)
api_router.include_router(notifications_router)
api_router.include_router(archives_router)
api_router.include_router(ops_router)
api_router.include_router(retention_router)
# Export center (Work 11 Lane X): canonical only —
#   /workspaces/{workspace_id}/exports/{formats,profiles,...}
api_router.include_router(exports_router)
# Media intelligence (Work 12 Lane A): providers + runs. The other Work 12
# domains (speech, audio, edits, visual, reframe, qc) register their own
# routers as they land.
api_router.include_router(media_intel_router)
# Media intelligence - audio enhancement (Work 12 Lane C).
api_router.include_router(media_intel_audio_router)
# Media intelligence - quality control (Work 12 Lane H).
api_router.include_router(media_intel_qc_router)
# Media intelligence - face + multi-face tracking (Work 12 Lane E).
api_router.include_router(media_intel_faces_router)
# Media intelligence - silence/filler proposals + apply (Work 12 Lane D).
api_router.include_router(media_intel_edits_router)
# Media intelligence - alignment, diarization, speakers (Work 12 Lane B).
api_router.include_router(media_intel_speech_router)
# Media intelligence - segmentation masks + active-speaker mapping
# (Work 12 Lane F) and smart reframing / layouts / background tools
# (Work 12 Lane G).
api_router.include_router(visual_router)
api_router.include_router(media_intel_reframe_router)
# Work 13: caption presets, motion templates, effect/transition registries,
# CaptionMotionQC and the Work 12 evidence summary. Writes stay on the
# canonical timeline operations + CreativeDirector paths.
api_router.include_router(captions_router)
# Work 14: read-only distribution surface (verified platform profiles,
# capability badges, the variant optimization diff). No publish route here.
api_router.include_router(distribution_router)
# Work 15: the planner control surface (signals, opportunities, plans, calendar,
# capacity, autonomy policy, feedback). No publish route here: planning
# autonomy never implies publishing autonomy.
api_router.include_router(planner_router)
# Work 15.6: generated-music POLICY only (opt-in + BrandDNA taste). The
# generation itself is a pipeline stage, not an endpoint, so nothing here spends
# money.
api_router.include_router(music_router)
# Work 15.6: provider MATURITY — a read-only view of how far each LLM/TTS/
# music/video/image/avatar adapter has actually been taken. Global table plus a
# workspace-scoped view that resolves credential *presence* only (a state word,
# never a value). No route here configures, enables, or probes by default.
api_router.include_router(provider_maturity_router)
api_router.include_router(workspace_maturity_router)
# Work 15.6: voice PREVIEW flow -- choose a provider (with the reason it is or
# is not offerable), list the voices production would actually use, hear a short
# cost-guarded sample, pick one. The raw synthesis route on assets_router stays;
# this adds the provider-awareness and the spend guard around it.
api_router.include_router(voice_preview_router)
