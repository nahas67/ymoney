"""GENERATED RESPONSE CONTRACTS -- Work 16.5.4 §1. DO NOT HAND-EDIT.

Regenerate with::

    backend\\.venv\\Scripts\\python scripts\\gen_response_contracts.py

WHAT THIS IS
------------
Pydantic response contracts for the 172 ordinary-JSON endpoints the rebuilt UI
calls, which previously declared no 2xx schema at all and could therefore only be
contract-tested on route and method.

HOW THEY WERE OBTAINED
---------------------
OBSERVED, not imagined. `scripts/ui_contract_observer.py` builds a real
workspace, seeds real objects, and records the actual JSON each endpoint
returns -- in BOTH an empty and a populated state.

WHY `extra="allow"` ON EVERY MODEL
----------------------------------
A snapshot cannot prove a field is absent from every unobserved state. Without
this, `response_model=` would SILENTLY DROP a field the generator missed while
every test stayed green. With it, a missed field is preserved and reported
instead. The behaviour is verified at runtime, not assumed:
`tests/test_work16_5_4_response_filtering.py`.

WHY SO FEW ENUMS
----------------
`Literal` is emitted only when every observed value is an ALL-CAPS constant that
exists in `backend/app`. Operational states (status, mode, verdict, health...)
stay `str`, because those vocabularies are expected to grow and pinning them
turns a legitimate new backend state into a startup validation failure.

These models document the shape. They do not authorise anything.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict


class _ObservedBase(BaseModel):
    """Base for every generated contract: preserve what we did not observe."""

    model_config = ConfigDict(extra="allow")


class ItemsRow2(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    workspace_id: str
    cycle_id: Any | None = None
    status: str
    priority: int
    retry_count: int
    max_retries: int
    next_run_at: str
    started_at: Any | None = None
    completed_at: Any | None = None
    last_error: str
    payload: dict | None = None
    result: dict | None = None
    created_at: str
    claimed_by: str
    claimed_at: Any | None = None
    lease_expires_at: Any | None = None
    heartbeat_at: Any | None = None
    lease_state: str
    # 20 of 20 fields were present in every observed state


class ApiV1WorkspacesWorkspaceJobs1(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceIntelligenceRoutingChains3(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    chains: list
    note: str
    # 2 of 2 fields were present in every observed state


class Totals5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    views: Any | None = None
    likes: Any | None = None
    comments: Any | None = None
    shares: Any | None = None
    followers_gained: Any | None = None
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAnalyticsOverview4(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    totals: Totals5
    posts_published: int
    content_items: int
    cost_total_usd: Any | None = None
    cost_total_unknown_exposure_rows: int
    per_platform: dict | None = None
    best_post: Any | None = None
    mock_analytics: bool
    # 8 of 8 fields were present in every observed state


class CoarseProxy7(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    label: str
    available: bool
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspacePerformanceRetention6(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    reason: str
    curve: dict | None = None
    mapping: dict | None = None
    drops: list
    rewatches: list
    coarse_proxy: CoarseProxy7
    post_id: str
    short_content_id: Any | None = None
    # 9 of 9 fields were present in every observed state


class ApiV1WorkspacesWorkspaceDistributionCapabilities8(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ByTopicRow10(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    key: str
    posts: int
    mock_posts: int
    total_views: Any | None = None
    avg_views: Any | None = None
    engagement_pct: Any | None = None
    engagement_samples: int
    # 7 of 7 fields were present in every observed state


class ByHookStyleRow11(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    key: str
    posts: int
    mock_posts: int
    total_views: Any | None = None
    avg_views: Any | None = None
    engagement_pct: Any | None = None
    engagement_samples: int
    # 7 of 7 fields were present in every observed state


class ByDurationRow12(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    key: str
    posts: int
    mock_posts: int
    total_views: Any | None = None
    avg_views: Any | None = None
    engagement_pct: Any | None = None
    engagement_samples: int
    # 7 of 7 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAnalyticsBreakdowns9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    by_topic: list[ByTopicRow10]
    by_hook_style: list[ByHookStyleRow11]
    by_duration: list[ByDurationRow12]
    posts_with_metrics: int
    mock_analytics: bool
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAnalyticsPatterns13(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspacePublishingPosts14(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaigns15(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAssetsMedia16(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class Capabilities18(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    upload: bool
    note: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAssets17(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    capabilities: Capabilities18
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAvatars19(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceMusicPolicy20(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    generate: bool
    reason: str
    brand_disabled: bool
    provider_key: str
    configured: bool
    settings_key: str
    forbidden_genres: list
    prefs: dict | None = None
    prefs_keys: list
    raw_settings: dict | None = None
    note: str
    # 12 of 12 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAssetsBrollSearch21(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class AssetsRow24(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    brand_id: str
    asset_role: str
    media_asset_id: str
    label: str
    created_at: str
    # 6 of 6 fields were present in every observed state


class BrandsRow23(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    name: str
    is_default: bool
    status: str
    dna: dict | None = None
    assets: list[AssetsRow24]
    created_at: str
    updated_at: str
    # 9 of 9 fields were present in every observed state


class ApiV1WorkspacesWorkspaceBrands22(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brands: list[BrandsRow23]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceBrandsPresets25(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    presets: list
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLessons26(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    # 1 of 1 fields were present in every observed state


class Brand28(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    app_name: str
    accent: str
    logo_path: str
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceBrand27(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: Brand28
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceMusicPrefs29(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    prefs: dict | None = None
    keys: list
    stated: bool
    note: str
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceBrandsBrandId30(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: Brand28
    # 1 of 1 fields were present in every observed state


class Asset32(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    brand_id: str
    asset_role: str
    media_asset_id: str
    label: str
    created_at: str
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceBrandsBrandIdAssets31(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset: Asset32
    # 1 of 1 fields were present in every observed state


class ForbiddenPhrases36(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class RequiredDisclaimers37(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class ApprovedColors38(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class Fonts39(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class LogoUse40(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class CaptionStyle41(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class CtaStyle42(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class VoiceApproval43(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class AvatarApproval44(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class Terminology45(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class Watermark46(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class ThumbnailConventions47(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    # 2 of 2 fields were present in every observed state


class Checks35(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    forbidden_phrases: ForbiddenPhrases36
    required_disclaimers: RequiredDisclaimers37
    approved_colors: ApprovedColors38
    fonts: Fonts39
    logo_use: LogoUse40
    caption_style: CaptionStyle41
    cta_style: CtaStyle42
    voice_approval: VoiceApproval43
    avatar_approval: AvatarApproval44
    terminology: Terminology45
    watermark: Watermark46
    thumbnail_conventions: ThumbnailConventions47
    # 12 of 12 fields were present in every observed state


class Provenance48(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    forbidden_phrases: str
    required_disclaimers: str
    logo_safe_zone: str
    approved_voices: str
    approved_avatars: str
    brand_colors: str
    caption_style: str
    cta_style: str
    tone: str
    vocabulary: str
    pronunciation_rules: str
    approved_logos: str
    watermark: str
    thumbnail_style: str
    fonts: str
    claims_policy: str
    # 16 of 16 fields were present in every observed state


class Semantic49(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    authoritative: bool
    mode: str
    # 4 of 4 fields were present in every observed state


class Report34(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    checks: Checks35
    artifact_kind: str
    workspace_id: str
    authoritative: bool
    effective_config_id: str
    provenance: Provenance48
    semantic: Semantic49
    report_type: str
    # 9 of 9 fields were present in every observed state


class ApiV1WorkspacesWorkspaceBrandsBrandIdVerify33(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    report: Report34
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaignsCampaignIdDerive50(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaignsCampaignIdGenerateMore51(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created: list
    # 1 of 1 fields were present in every observed state


class Schedule53(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created: list
    reused: list
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaignsCampaignIdSchedule52(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    schedule: Schedule53
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaignsCampaignIdPublish54(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    enqueued: list
    skipped: list
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaignsCampaignIdCancel55(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContentContentIdRegenerate56(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    lineage_version: int
    # 3 of 3 fields were present in every observed state


class Metadata58(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    platform: str
    title: str
    description: str
    caption: str
    hashtags: list
    keywords: list
    cta_kind: str
    cta_text: str
    pinned_comment: str
    cover_text: str
    # 10 of 10 fields were present in every observed state


class SafeZones59(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    top: float
    bottom: float
    left: float
    right: float
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContentContentIdPlatformVariants57(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    short_content_id: str
    platform: str
    aspect_ratio: str
    timeline_id: Any | None = None
    shares_base_timeline: bool
    metadata: Metadata58
    cover_asset_id: Any | None = None
    safe_zones: SafeZones59
    status: str
    publishing_job_id: Any | None = None
    published_post_id: Any | None = None
    # 12 of 12 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContent60(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class Progress63(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    completed: int
    total: int
    # 2 of 2 fields were present in every observed state


class Plan62(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    master_content_id: str
    goal: str
    target_platforms: list
    desired_shorts: int
    cta_kind: str
    status: str
    progress: Progress63
    # 7 of 7 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCampaignsFromMaster61(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    plan: Plan62
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCalendar64(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCosts65(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    last_24h_by_category: dict | None = None
    spent_last_24h_usd: Any | None = None
    spent_last_24h_unknown_exposure_rows: int
    daily_budget_usd: float
    per_video_budget_usd: float
    within_budget: bool
    remaining_usd: float
    # 7 of 7 fields were present in every observed state


class OpportunitiesRow67(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    topic: str
    basis: str
    basis_meaning: str
    angle: str
    audience: str
    platforms: list
    format: str
    score: float
    confidence: float
    freshness: str
    brand_fit: float
    evidence: list
    competition_evidence: dict | None = None
    estimated_effort_hours: float
    estimated_cost_usd: float
    dedupe_verdict: str
    dedupe_reason: str
    plan_item_id: Any | None = None
    scoring: dict | None = None
    why: str
    # 21 of 21 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerOpportunities66(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    count: int
    opportunities: list[OpportunitiesRow67]
    forbidden_claims: list
    note: str
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxOpportunities68(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    # 1 of 1 fields were present in every observed state


class ApiV1SystemReadiness69(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    checked_at: str
    stale_after_hours: int
    checks: list
    blocking_failures: list
    message: str
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceProviderMaturityIncidents70(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    items: list
    count: int
    unknown_exposure_count: int
    states: list
    note: str
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceReviews71(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    # 1 of 1 fields were present in every observed state


class Interaction73(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    platform: str
    account_id: str
    remote_id: str
    kind: str
    text: str
    author_remote_id: str
    author_name: str
    parent_interaction_id: Any | None = None
    thread_id: str
    conversation_id: str
    post_remote_id: str
    published_post_id: Any | None = None
    content_item_id: Any | None = None
    campaign_id: Any | None = None
    status: str
    unread: bool
    moderation_state: str
    sentiment: str
    intent: str
    priority: str
    is_question: bool
    classifications: list
    moderation: list
    remote_created_at: str
    created_at: str
    updated_at: str
    # 28 of 28 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdRead72(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    interaction: Interaction73
    # 1 of 1 fields were present in every observed state


class ClassificationsRow75(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    label: str
    provider: str
    model: str
    confidence: float
    evidence: str
    source: str
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    interaction: Interaction73
    classifications: list[ClassificationsRow75]
    # 2 of 2 fields were present in every observed state


class BrandCheck78(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    forbidden_hits: list
    required_disclaimers: list
    tone: str
    effective_config_id: str
    missing_disclaimers: list
    attempts: int
    # 7 of 7 fields were present in every observed state


class Action77(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    interaction_id: str
    conversation_id: str
    account_id: str
    platform: str
    action_type: str
    mode: str
    state: str
    origin: str
    badge: str
    draft_text: str
    final_text: str
    brand_check: BrandCheck78
    approval_user_id: Any | None = None
    rejected_user_id: Any | None = None
    is_mock: bool
    remote_reply_id: str
    error: str
    sent_at: str
    created_at: str
    updated_at: str
    # 22 of 22 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraft76(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: Action77
    interaction: Interaction73
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxActionsActionIdApprove79(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: Action77
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxActionsActionIdReject80(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: Action77
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply81(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: Action77
    interaction: Interaction73
    # 2 of 2 fields were present in every observed state


class Receipt84(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    remote_reply_id: str
    text: str
    is_mock: bool
    mock: bool
    # 4 of 4 fields were present in every observed state


class Result83(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    sent: bool
    reason: str
    state: str
    action_id: str
    remote_reply_id: str
    is_mock: bool
    receipt: Receipt84
    verification_status: str
    # 8 of 8 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxActionsActionIdSend82(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: Action77
    result: Result83
    # 2 of 2 fields were present in every observed state


class ActionsRow86(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    interaction_id: str
    conversation_id: str | None = None
    account_id: str
    platform: str
    action_type: str
    mode: str
    state: str
    origin: str
    badge: str
    draft_text: str
    final_text: str
    brand_check: BrandCheck78
    approval_user_id: str | None = None
    rejected_user_id: str | None = None
    is_mock: bool
    remote_reply_id: str
    error: str
    sent_at: str
    created_at: str
    updated_at: str
    # 22 of 22 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxInteractionsInteractionId85(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    interaction: Interaction73
    linked_publication: Any | None = None
    thread: list
    actions: list[ActionsRow86]
    # 4 of 4 fields were present in every observed state


class LastInteraction89(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    platform: str
    author_name: str
    status: str
    text: str
    created_at: str
    # 7 of 7 fields were present in every observed state


class Conversation88(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    platform: str
    account_id: str
    thread_key: str
    title: str
    participant_remote_id: str
    participant_name: str
    published_post_id: Any | None = None
    last_interaction_at: str
    unread_count: int
    status: str
    priority: str
    last_interaction: LastInteraction89
    created_at: str
    updated_at: str
    # 16 of 16 fields were present in every observed state


class InteractionsRow90(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    platform: str
    account_id: str
    remote_id: str
    kind: str
    text: str
    author_remote_id: str
    author_name: str
    parent_interaction_id: Any | None = None
    thread_id: str
    conversation_id: str
    post_remote_id: str
    published_post_id: Any | None = None
    content_item_id: Any | None = None
    campaign_id: Any | None = None
    status: str
    unread: bool
    moderation_state: str
    sentiment: str
    intent: str
    priority: str
    is_question: bool
    classifications: list[ClassificationsRow75]
    moderation: list
    remote_created_at: str
    created_at: str
    updated_at: str
    # 28 of 28 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxConversationsConversationId87(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    conversation: Conversation88
    interactions: list[InteractionsRow90]
    # 2 of 2 fields were present in every observed state


class Caption94(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    max_lines: int
    max_chars_per_line: int
    style: str
    # 3 of 3 fields were present in every observed state


class Thumbnail95(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    behavior: str
    cover_text_max: int
    # 2 of 2 fields were present in every observed state


class Media93(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspects: list
    preferred_duration: list
    max_duration: float
    safe_zones: SafeZones59
    caption: Caption94
    thumbnail: Thumbnail95
    cta: list
    posting_windows: list
    publication_mode: str = None
    # 8 of 9 fields were present in every observed state


class MetadataLimits96(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    title_max: int
    description_max: int
    hashtag_max: int
    hashtag_limit: int
    # 4 of 4 fields were present in every observed state


class PlatformsRow92(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    platform: str
    campaign_platforms: list
    capabilities: list
    media: Media93
    metadata_limits: MetadataLimits96
    aspect_ratios: list
    duration_s: list
    supports_inbox: bool
    supports_analytics: bool
    # 9 of 9 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxPlatforms91(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    platforms: list[PlatformsRow92]
    capabilities: Capabilities18
    observed: list
    # 4 of 4 fields were present in every observed state


class Caps99(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_replies: int
    hourly_replies: int
    # 2 of 2 fields were present in every observed state


class Autonomy98(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str
    classes: list
    caps: Caps99
    # 3 of 3 fields were present in every observed state


class Defaults100(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str
    classes: list
    caps: Caps99
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxAutonomy97(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    autonomy: Autonomy98
    modes: list
    classes: list
    defaults: Defaults100
    # 4 of 4 fields were present in every observed state


class Metrics102(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    window_days: int
    since: str
    interactions: int
    comments: int
    questions: int
    spam: int
    reply_rate: float
    replies_sent: int
    content_requests: int
    lead_signals: int
    # 11 of 11 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxAnalytics101(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    metrics: Metrics102
    kpis: list
    totals: Totals5
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxConversations103(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxInteractions104(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    next_cursor: Any | None = None
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxActions105(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxInsights106(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    suggestions: list
    suggestions_available: bool
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceInboxSync107(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    type: str
    payload: dict | None = None
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspacePublishingAccounts108(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspacePublishingJobs109(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceDistributionPlatforms110(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class EntriesRow112(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    platform: str
    run_at: str
    status: str
    content_item_id: str
    campaign_id: str | None = None
    # 6 of 6 fields were present in every observed state


class Capacity113(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    declared: bool
    locale: str
    longform_per_week: float
    shorts_per_day: float
    ugc_per_day: float
    localization_per_day: float
    render_hours_per_day: float
    review_slots_per_day: float
    notes: str
    # 9 of 9 fields were present in every observed state


class Committed114(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    locale: str
    committed: Committed114
    committed_cost: float
    item_count: int
    # 4 of 4 fields were present in every observed state


class Remaining115(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    shorts: Any | None = None
    ugc: Any | None = None
    localization: Any | None = None
    render: Any | None = None
    review: Any | None = None
    longform: Any | None = None
    # 0 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerCalendar111(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    days: int
    entries: list[EntriesRow112]
    capacity: Capacity113
    committed: Committed114
    remaining: Remaining115
    plan_item_count: int
    note: str
    # 8 of 8 fields were present in every observed state


class ByState118(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    CONFIGURED: int
    NOT_CONFIGURED: int
    NOT_REQUIRED: int
    # 3 of 3 fields were present in every observed state


class CredentialSummary117(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    by_state: ByState118
    providers_without_credentials: list
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceProviderMaturity116(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    items: list[ItemsRow2]
    count: int
    resolved_credential_states: list
    note: str
    credential_summary: CredentialSummary117
    # 6 of 6 fields were present in every observed state


class Tts120(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    provider: str
    healthy: bool
    is_mock: bool
    # 3 of 3 fields were present in every observed state


class Youtube122(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str
    ready: bool
    detail: str
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class Tiktok123(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str
    ready: bool
    detail: str
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class Facebook124(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str
    ready: bool
    detail: str
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class Instagram125(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str
    ready: bool
    detail: str
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class Publishers121(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    youtube: Youtube122
    tiktok: Tiktok123
    facebook: Facebook124
    instagram: Instagram125
    # 4 of 4 fields were present in every observed state


class Queue126(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    backend: str
    gpu_worker: bool
    gpu_cuda: bool
    redis: Any | None = None
    # 4 of 4 fields were present in every observed state


class Mocks127(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    llm: bool
    trends: bool
    publishing: bool
    analytics: bool
    video_engine: bool
    # 5 of 5 fields were present in every observed state


class ApiV1SystemHealth119(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    database: bool
    video_engine: bool
    video_engine_name: str
    video_engine_version: str
    llm_provider: bool
    tts: Tts120
    publishers: Publishers121
    queue: Queue126
    mocks: Mocks127
    time: str
    # 11 of 11 fields were present in every observed state


class ApiV1WorkspacesWorkspacePublishingOauthFacebookStart128(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    authorize_url: str
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspacePublishingAccountsAccountId129(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: bool
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceExperimentsExperimentIdAnalyze130(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    kind: str
    hypothesis: str
    control: dict | None = None
    variants: list
    platform: str
    primary_metric: str
    secondary_metrics: list
    minimum_sample: int
    status: str
    result: Result83
    confidence: str
    started_at: Any | None = None
    ended_at: Any | None = None
    created_at: str
    updated_at: str
    # 17 of 17 fields were present in every observed state


class Control132(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    variant_ref: str
    # 1 of 1 fields were present in every observed state


class VariantsRow133(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    variant_ref: str
    descriptor: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceExperiments131(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    kind: str
    hypothesis: str
    control: Control132
    variants: list[VariantsRow133]
    platform: str
    primary_metric: str
    secondary_metrics: list
    minimum_sample: int
    status: str
    result: Result83
    confidence: str
    started_at: Any | None = None
    ended_at: Any | None = None
    created_at: str
    updated_at: str
    # 17 of 17 fields were present in every observed state


class ApiV1WorkspacesWorkspaceExperimentsExperimentId134(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    kind: str
    hypothesis: str
    control: Control132
    variants: list
    platform: str
    primary_metric: str
    secondary_metrics: list
    minimum_sample: int
    status: str
    result: Result83
    confidence: str
    started_at: Any | None = None
    ended_at: Any | None = None
    created_at: str
    updated_at: str
    # 17 of 17 fields were present in every observed state


class ApiV1WorkspacesWorkspaceIntelligenceDecisionsLog135(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class Rank138(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    agreed: int
    agreement_rate: float
    # 3 of 3 fields were present in every observed state


class ByKind137(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    rank: Rank138 = None
    # 0 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceIntelligenceDecisionsShadowReport136(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    kind: Any | None = None
    total: int
    agreed: int
    disagreed: int
    agreement_rate: float | None = None
    avg_latency_ms: float | None = None
    total_cost_usd: float
    by_kind: ByKind137
    # 9 of 9 fields were present in every observed state


class Providers140(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    remote: bool
    local: bool
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceIntelligenceRoutingHealth139(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    providers: Providers140
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceIntelligenceRoutingLog141(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    entries: list
    # 1 of 1 fields were present in every observed state


class Chain143(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    count: int
    broken_at: Any | None = None
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceIntelligenceVerificationLedger142(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    chain: Chain143
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLocalizationGlossary144(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLocalizationGlossaryTermId145(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: str
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAssetsDubStatus146(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ffmpeg: bool
    llm: bool
    tts: bool
    tts_provider: str
    languages: list
    ready: bool
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceDubbingPlans147(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class ChecksRow149(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    step: str
    status: str
    detail: str
    # 3 of 3 fields were present in every observed state


class WarnsRow150(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    step: str
    status: str
    detail: str
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAssetsDubDryRun148(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    checks: list[ChecksRow149]
    warns: list[WarnsRow150]
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLipsyncHealth151(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    provider: str
    status: str
    available: bool
    detail: str
    remediation: str
    checks: Checks35
    queue: Queue126
    # 7 of 7 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLipsyncJobs152(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLipsyncJobsJobIdCancel153(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    provider: str
    status: str
    execution_outcome: str
    cost_outcome: str
    progress: float
    error: str
    result_asset_ref: str
    cost: dict | None = None
    video_ref: str
    audio_ref: str
    opts: dict | None = None
    adapter_job_id: str
    created_at: str
    updated_at: str
    started_at: Any | None = None
    completed_at: str
    # 18 of 18 fields were present in every observed state


class ApiV1WorkspacesWorkspaceMediaIntelSpeakers154(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    count: int
    anonymous: bool
    note: str
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspaceMediaIntelSpeakersAliases155(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    count: int
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLocalization156(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLocalizationLocalizedIdQc157(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    localized_content_id: str
    language: str
    status: str
    created_at: str
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceLocalizationRun158(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    queued: bool
    job_id: str
    items: list[ItemsRow2]
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceKnowledgeMemories159(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceKnowledgeGraph160(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    nodes: list
    edges: list
    # 2 of 2 fields were present in every observed state


class MemoriesRow162(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    topic: str
    status: str
    lifecycle: str
    confidence: float
    content: str
    # 7 of 7 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerMemory161(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    used_ids: list
    memory_count: int
    settled_topics: list
    needs_revalidation: list
    memories: list[MemoriesRow162]
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceKnowledgeMemoriesMemoryIdDisable163(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    brand_id: Any | None = None
    type: str
    content: str
    topic: str
    topic_key: str
    scope: str
    platform: str
    source_ids: list
    evidence_ids: list
    confidence: float
    freshness: str
    status: str
    effective_status: str
    origin: str
    content_hash: str
    conflict_group: str
    last_verified_at: Any | None = None
    superseded_by: Any | None = None
    use_count: int
    last_used_at: str
    related_json: dict | None = None
    created_at: str
    updated_at: str
    # 25 of 25 fields were present in every observed state


class ApiV1WorkspacesWorkspaceKnowledgeSources164(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    # 1 of 1 fields were present in every observed state


class ByStatus167(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    QUEUED: int
    # 1 of 1 fields were present in every observed state


class Jobs166(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    by_status: ByStatus167
    total: int
    failed_recent: list
    # 4 of 4 fields were present in every observed state


class Reviews168(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    open: int
    stale_approvals: list
    stale_approval_count: int
    # 4 of 4 fields were present in every observed state


class Exports169(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    by_state: ByState118
    failed: list
    failed_count: int
    # 4 of 4 fields were present in every observed state


class Storage170(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    bytes: int
    file_count: int
    source: str
    # 4 of 4 fields were present in every observed state


class ProviderHealth171(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    status: str
    checked_at: str
    blocking_failures: list
    checks: list
    # 5 of 5 fields were present in every observed state


class Costs172(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    last_24h_by_category: dict | None = None
    spent_last_24h_usd: int
    daily_budget_usd: float
    per_video_budget_usd: float
    within_budget: bool
    remaining_usd: float
    since: str
    # 8 of 8 fields were present in every observed state


class Audit173(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    events_last_7d: int
    since: str
    retention_enforced: bool
    # 4 of 4 fields were present in every observed state


class Retention174(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    audit_retention_days: Any | None = None
    render_retention_days: Any | None = None
    temp_asset_retention_days: Any | None = None
    export_retention_days: Any | None = None
    id: Any | None = None
    workspace_id: Any | None = None
    updated_by: Any | None = None
    updated_at: Any | None = None
    audit_enforced: bool
    # 10 of 10 fields were present in every observed state


class ApiV1WorkspacesWorkspaceOpsOverview165(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    jobs: Jobs166
    reviews: Reviews168
    exports: Exports169
    storage: Storage170
    provider_health: ProviderHealth171
    costs: Costs172
    audit: Audit173
    retention: Retention174
    workspace_id: str
    generated_at: str
    # 10 of 10 fields were present in every observed state


class PublicationsByPlatform176(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    tiktok: int = None
    # 0 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCostsIntelligence175(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total_cost_usd: float
    per_cycle_usd: Any | None = None
    per_video_usd: float | None = None
    per_publication_usd: float | None = None
    cost_per_1000_views_usd: Any | None = None
    by_category: dict | None = None
    by_agent: dict | None = None
    publications_by_platform: PublicationsByPlatform176
    totals: Totals5
    estimated_return_usd: Any | None = None
    estimated_return_note: str
    # 11 of 11 fields were present in every observed state


class ApiV1WorkspacesWorkspaceRetention177(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audit_retention_days: Any | None = None
    render_retention_days: Any | None = None
    temp_asset_retention_days: Any | None = None
    export_retention_days: Any | None = None
    id: Any | None = None
    workspace_id: Any | None = None
    updated_by: Any | None = None
    updated_at: Any | None = None
    audit_enforced: bool
    # 9 of 9 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCalendarEntryId178(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    run_at: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCalendarBestTimes179(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    measured: bool
    # 2 of 2 fields were present in every observed state


class AudienceActivity181(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    hours: list
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceCalendarResponseWindows180(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    measured: bool
    activity: bool
    audience_activity: AudienceActivity181
    caps: Caps99
    avoid_hours: list
    activity_policy: str
    notes: list
    # 8 of 8 fields were present in every observed state


class PlansRow183(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    horizon_days: int
    goals: list
    platforms: list
    budget_usd: float
    spent_usd: float
    budget_remaining: float
    autonomy: str
    constraints: dict | None = None
    status: str
    item_count: int
    items: list[ItemsRow2]
    # 12 of 12 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerPlans182(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    count: int
    plans: list[PlansRow183]
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerItemsItemIdApprove184(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerItemsItemIdReject185(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    reason: str
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerItemsItemIdResearchMore186(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerItemsItemIdCampaign187(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    plan_item_id: str
    opportunity_id: str
    campaign_id: str
    content_item_id: str
    schedule_entry_id: str
    stages_done: list
    skipped: list
    blocked: str
    complete: bool
    # 9 of 9 fields were present in every observed state


class ScheduleEntry189(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    plan_item_id: str
    entry_id: str
    status: str
    created: bool
    run_at: str
    reason: str
    evidence_backed: bool
    # 7 of 7 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerItemsItemIdSchedule188(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    schedule_entry: ScheduleEntry189
    evidence_backed_slot: bool
    why_scheduled: str
    schedule_entry_id: str
    publishes: bool
    # 7 of 7 fields were present in every observed state


class Memory191(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    used_ids: list
    memory_count: int
    settled_topics: list
    needs_revalidation: list
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerPlan190(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    plan_id: str
    items: list
    blocked: list
    suggestions: list
    memory: Memory191
    notes: list
    autonomy: str
    preview: bool
    publishes: bool
    # 9 of 9 fields were present in every observed state


class Declared193(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    shorts: Any | None = None
    ugc: Any | None = None
    localization: Any | None = None
    render: Any | None = None
    review: Any | None = None
    longform: Any | None = None
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerCapacity192(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    locale: str
    is_unbounded: bool
    declared: Declared193
    declared_note: str
    committed: Committed114
    # 6 of 6 fields were present in every observed state


class Disabled196(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    rank: int
    suggests: bool
    creates_plan_items: bool
    creates_campaign_drafts: bool
    starts_research: bool
    schedules: bool
    advances_production: bool
    publishes: bool
    note: str
    # 9 of 9 fields were present in every observed state


class Recommend197(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    rank: int
    suggests: bool
    creates_plan_items: bool
    creates_campaign_drafts: bool
    starts_research: bool
    schedules: bool
    advances_production: bool
    publishes: bool
    note: str
    # 9 of 9 fields were present in every observed state


class Approval198(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    rank: int
    suggests: bool
    creates_plan_items: bool
    creates_campaign_drafts: bool
    starts_research: bool
    schedules: bool
    advances_production: bool
    publishes: bool
    note: str
    # 9 of 9 fields were present in every observed state


class Autonomous199(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    rank: int
    suggests: bool
    creates_plan_items: bool
    creates_campaign_drafts: bool
    starts_research: bool
    schedules: bool
    advances_production: bool
    publishes: bool
    note: str
    # 9 of 9 fields were present in every observed state


class Table195(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    DISABLED: Disabled196
    RECOMMEND: Recommend197
    APPROVAL: Approval198
    AUTONOMOUS: Autonomous199
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerPolicy194(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    modes: list
    actions: list
    table: Table195
    publishes: bool
    note: str
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceOpportunities200(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class SignalsRow202(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    source: str
    topic: str
    topic_key: str
    external_ref: str
    observed_at: str
    freshness: str
    scope: str
    confidence: float
    status: str
    evidence_ids: list
    recurrence: int
    velocity: Any | None = None
    usable_as_demand: bool
    # 14 of 14 fields were present in every observed state


class ApiV1WorkspacesWorkspacePlannerSignals201(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    count: int
    sources: list
    signals: list[SignalsRow202]
    note: str
    # 5 of 5 fields were present in every observed state


class Strategy204(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    angle: str
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContentContentId203(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    topic: str
    status: str
    campaign_id: Any | None = None
    cycle_id: Any | None = None
    strategy: Strategy204
    error: str
    video: Any | None = None
    variants_count: int
    created_at: str
    research: dict | None = None
    tags: list
    variants: list[VariantsRow133]
    # 13 of 13 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContentContentIdTimeline205(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class Self207(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    topic: str
    status: str
    derivation_type: Any | None = None
    lineage_version: int
    campaign_id: Any | None = None
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContentContentIdLineage206(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    self: Self207
    root_id: str
    ancestors: list
    children: list
    # 4 of 4 fields were present in every observed state


class Content209(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    topic: str
    status: str
    error: str
    created_at: str
    # 5 of 5 fields were present in every observed state


class VideosRow210(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    engine: str
    status: str
    aspect_ratio: str
    resolution: str
    duration_seconds: Any | None = None
    params: dict | None = None
    error: str
    # 8 of 8 fields were present in every observed state


class PublishedPostsRow211(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    platform: str
    remote_post_id: str
    remote_url: str
    title: str
    is_mock: bool
    metrics: Any | None = None
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceContentContentIdAudit208(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    content: Content209
    decision_why: Any | None = None
    strategy: Strategy204
    research: dict | None = None
    variants: list[VariantsRow133]
    videos: list[VideosRow210]
    quality_checks: list
    publishing_jobs: list
    published_posts: list[PublishedPostsRow211]
    event_trail: list
    # 10 of 10 fields were present in every observed state


class ApiV1WorkspacesWorkspaceConnections212(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceApiKeysKeyIdRevoke213(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    revoked: bool
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceMembers214(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class Safety216(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_budget_usd: float
    monthly_budget_usd: float
    per_video_budget_usd: float
    max_videos_per_day: int
    max_uploads_per_hour: int
    min_qc_score: int
    max_render_attempts: int
    max_consecutive_failures: int
    max_concurrent_renders: int
    similarity_threshold: float
    require_human_review_risk_above: float
    require_approval_before_publish: bool
    produce_score_threshold: float
    # 13 of 13 fields were present in every observed state


class ApiV1WorkspacesWorkspaceSafety215(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    safety: Safety216
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceNotifications217(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list
    count: int
    unread: int
    limit: int
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspaceWebhooks218(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    events: list
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceApiKeys219(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAgentsConfig220(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[ItemsRow2]
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAgentsConfigAgentKey221(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceWebhooksSubId222(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: bool
    # 1 of 1 fields were present in every observed state


class ApiV1WorkspacesWorkspaceTimelines223(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class TracksRow225(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    name: str
    clips: list
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspaceTimelinesTimelineId224(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    content_item_id: Any | None = None
    video_id: Any | None = None
    name: str
    fps: float
    duration_seconds: float
    aspect_ratio: str
    tracks: list[TracksRow225]
    version: int
    parent_timeline_id: Any | None = None
    created_at: str
    updated_at: str
    # 13 of 13 fields were present in every observed state


class ApiV1WorkspacesWorkspaceTimelinesTimelineIdScenes226(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    scenes: list
    # 2 of 2 fields were present in every observed state


class Lanes229(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    server: bool
    sadtalker: bool
    wavlip: bool
    mock: bool
    # 4 of 4 fields were present in every observed state


class LicenseNotes230(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    wavlip: str
    # 1 of 1 fields were present in every observed state


class Health228(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ready: bool
    detail: str
    backend: str
    provider: str
    ffmpeg: bool
    lanes: Lanes229
    license_notes: LicenseNotes230
    # 7 of 7 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAvatarsHealth227(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    provider: str
    ready: bool
    detail: str
    consent_required: bool
    consent_states: list
    health: Health228
    capabilities: Capabilities18
    workspace_id: str
    # 8 of 8 fields were present in every observed state


class Flags233(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    lip_sync_failure: bool
    # 1 of 1 fields were present in every observed state


class Qc232(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    checks: Checks35
    flags: Flags233
    report_type: str
    # 4 of 4 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAvatarsRender231(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    output_id: str
    asset_id: str
    storage_key: str
    duration: float
    backend: str
    provider: str
    is_mock: bool
    consent_state: str
    timeline_id: str
    content_item_id: str
    qc: Qc232
    # 11 of 11 fields were present in every observed state


class Profile235(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    name: str
    source_asset_ref: str
    voice_ref: str
    expression_preset: str
    motion_preset: str
    framing: str
    background: str
    language: str
    brand_association: str
    provider: str
    status: str
    # 11 of 11 fields were present in every observed state


class AuthorizationEvidence237(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    kind: str
    reference: str
    # 2 of 2 fields were present in every observed state


class Consent236(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    state: str
    source: str
    granted_by: str
    granted_at: str
    authorization_evidence: AuthorizationEvidence237
    statement: str
    # 6 of 6 fields were present in every observed state


class ApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize234(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    name: str
    profile: Profile235
    source_asset_ref: str
    consent_state: str
    consent: Consent236
    provider: str
    status: str
    created_at: str
    # 10 of 10 fields were present in every observed state


class ApiV1WorkspacesWorkspaceUgcPresets238(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    workspace_id: str
    # 3 of 3 fields were present in every observed state


class ApiV1WorkspacesWorkspaceUgcProjects239(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    total: int
    items: list[ItemsRow2]
    # 2 of 2 fields were present in every observed state


class Brief241(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    topic: str
    cta: str
    # 2 of 2 fields were present in every observed state


class ProductAssets242(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    resolved: list
    unresolved: list
    # 2 of 2 fields were present in every observed state


class ApiV1WorkspacesWorkspaceUgcProjectsProjectId240(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    preset: str
    status: str
    brief: Brief241
    timeline_id: str
    render_asset_ref: str
    qc: Qc232
    lineage: dict | None = None
    created_at: str
    updated_at: str
    product_assets: ProductAssets242
    open_in_editor: bool
    # 13 of 13 fields were present in every observed state


class BrandQc246(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    effective_config_id: str
    # 3 of 3 fields were present in every observed state


class Lineage245(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_qc: BrandQc246
    # 1 of 1 fields were present in every observed state


class Project244(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    workspace_id: str
    preset: str
    status: str
    brief: Brief241
    timeline_id: str
    render_asset_ref: str
    qc: Qc232
    lineage: Lineage245
    created_at: str
    updated_at: str
    # 11 of 11 fields were present in every observed state


class Render247(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_id: str
    storage_key: str
    duration_seconds: float
    timeline_id: str
    qc: Qc232
    # 5 of 5 fields were present in every observed state


class ApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender243(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    project: Project244
    render: Render247
    # 2 of 2 fields were present in every observed state

