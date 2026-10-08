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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _ObservedBase(BaseModel):
    """Base for every generated contract: preserve what we did not observe."""

    model_config = ConfigDict(extra="allow")


class GetApiV1WorkspacesWorkspaceJobs4f937872ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    claimed_at: None
    claimed_by: str
    completed_at: None
    created_at: str
    cycle_id: None
    heartbeat_at: None
    id: str
    last_error: str
    lease_expires_at: None
    lease_state: str
    max_retries: int
    next_run_at: str
    payload: dict[str, JsonValue]
    priority: int
    result: dict[str, JsonValue]
    retry_count: int
    started_at: None
    status: str
    type: str
    workspace_id: str
    # 20 of 20 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceJobs4f937872(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceJobs4f937872ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceRoutingChains0f45c4fc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    chains: list[JsonValue]
    note: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMediaCaption(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    max_chars_per_line: int
    max_lines: int
    style: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMediaSafeZones(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    bottom: float
    left: float
    right: float
    top: float
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMediaThumbnail(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    behavior: str
    cover_text_max: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMedia(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspects: list[str]
    caption: GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMediaCaption
    cta: list[str]
    max_duration: float
    posting_windows: list[int]
    preferred_duration: list[float]
    publication_mode: str | None = None
    safe_zones: GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMediaSafeZones
    thumbnail: GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMediaThumbnail
    # 8 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMetadataLimits(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    description_max: int
    hashtag_limit: int
    hashtag_max: int
    title_max: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_platforms: list[str]
    capabilities: list[Literal["ALT_TEXT", "CAROUSEL", "COMMENTS", "DELETE_COMMENT", "DIRECT_PUBLISH", "FETCH_METRICS", "IMAGE", "LINK", "METRICS", "PUBLISH_IMAGE", "PUBLISH_SHORT", "PUBLISH_TEXT", "PUBLISH_VIDEO", "READ_COMMENTS", "READ_MENTIONS", "REPLY", "REPLY_COMMENT", "TEXT", "USER_HANDOFF", "VIDEO"]]
    direct_publish: bool
    media: GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMedia
    metadata_limits: GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRowMetadataLimits
    platform: str
    publish_mode: str
    supports_analytics: bool
    supports_inbox: bool
    user_handoff: bool
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceDistributionCapabilitiesab914761ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsOverview02e895a9Totals(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    comments: None
    followers_gained: None
    likes: None
    shares: None
    views: None
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsOverview02e895a9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    best_post: None
    content_items: int
    cost_total_unknown_exposure_rows: int
    cost_total_usd: None
    mock_analytics: bool
    per_platform: dict[str, JsonValue]
    posts_published: int
    totals: GetApiV1WorkspacesWorkspaceAnalyticsOverview02e895a9Totals
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26aByDurationRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    avg_views: None
    engagement_pct: None
    engagement_samples: int
    key: str
    mock_posts: int
    posts: int
    total_views: None
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26aByHookStyleRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    avg_views: None
    engagement_pct: None
    engagement_samples: int
    key: str
    mock_posts: int
    posts: int
    total_views: None
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26aByTopicRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    avg_views: None
    engagement_pct: None
    engagement_samples: int
    key: str
    mock_posts: int
    posts: int
    total_views: None
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    by_duration: list[GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26aByDurationRow]
    by_hook_style: list[GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26aByHookStyleRow]
    by_topic: list[GetApiV1WorkspacesWorkspaceAnalyticsBreakdowns4fc5f26aByTopicRow]
    mock_analytics: bool
    posts_with_metrics: int
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAnalyticsPatterns1c915da0(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingPosts51c42dfaItemsRowMetrics(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    comments: None
    completion_rate: None
    likes: None
    views: None
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingPosts51c42dfaItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    is_mock: bool
    metrics: GetApiV1WorkspacesWorkspacePublishingPosts51c42dfaItemsRowMetrics
    platform: str
    published_at: None
    remote_url: str
    title: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingPosts51c42dfa(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspacePublishingPosts51c42dfaItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaigns4eda2759ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    automation_level: str
    budget_daily_usd: None
    ends_at: None
    goal: str
    id: str
    name: str
    platforms: list[JsonValue]
    starts_at: None
    status: str
    target_videos: int
    videos_per_day: float
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaigns4eda2759(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceCampaigns4eda2759ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePerformanceOverview7b224c4aRollupTotals(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    comments: None
    completion: None
    engagement_rate: None
    likes: None
    saves: None
    shares: None
    views: None
    watch_time: None
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePerformanceOverview7b224c4aRollup(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    master: None
    master_content_id: None
    platforms: list[JsonValue]
    post_count: int
    short_count: int
    shorts: list[JsonValue]
    totals: GetApiV1WorkspacesWorkspacePerformanceOverview7b224c4aRollupTotals
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePerformanceOverview7b224c4a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    platforms: dict[str, JsonValue]
    rollup: GetApiV1WorkspacesWorkspacePerformanceOverview7b224c4aRollup
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePerformanceRetention3765b951CoarseProxy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    label: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePerformanceRetention3765b951(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    coarse_proxy: GetApiV1WorkspacesWorkspacePerformanceRetention3765b951CoarseProxy
    curve: dict[str, JsonValue]
    drops: list[JsonValue]
    mapping: dict[str, JsonValue]
    post_id: str
    reason: str
    rewatches: list[JsonValue]
    short_content_id: None
    status: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePerformanceCompare4fee534c(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    causal: bool
    group_by: str
    groups: list[JsonValue]
    note: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAssetsMediaaf324685ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checksum: str
    created_at: str
    duration_seconds: float | None
    height: None
    id: str
    mime_type: str
    origin: str
    provider: str
    storage_key: str
    type: str
    width: None
    workspace_id: str
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAssetsMediaaf324685(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceAssetsMediaaf324685ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAssetsc96c61d2Capabilities(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    note: str
    upload: bool
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAssetsc96c61d2ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    created_at: str
    engine: str
    id: str
    is_mock: bool
    resolution: str
    size_bytes: None
    status: str
    title: str
    type: str
    variant_label: str
    video_id: str
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAssetsc96c61d2(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    capabilities: GetApiV1WorkspacesWorkspaceAssetsc96c61d2Capabilities
    items: list[GetApiV1WorkspacesWorkspaceAssetsc96c61d2ItemsRow]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsd295e71bItemsRowConsent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    authorization_evidence: dict[str, JsonValue] | None = None
    granted_at: str | None = None
    granted_by: str
    source: str
    state: str | None = None
    statement: str | None = None
    # 2 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsd295e71bItemsRowProfile(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    background: str
    brand_association: str
    expression_preset: str
    framing: str
    language: str
    motion_preset: str
    name: str
    provider: str
    source_asset_ref: str
    status: str
    voice_ref: str
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsd295e71bItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    consent: GetApiV1WorkspacesWorkspaceAvatarsd295e71bItemsRowConsent
    consent_state: str
    created_at: str
    id: str
    name: str
    profile: GetApiV1WorkspacesWorkspaceAvatarsd295e71bItemsRowProfile
    provider: str
    source_asset_ref: str
    status: str
    workspace_id: str
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsd295e71b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceAvatarsd295e71bItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMusicPolicyafecfb9c(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_disabled: bool
    configured: bool
    forbidden_genres: list[JsonValue]
    generate: bool
    note: str
    prefs: dict[str, JsonValue]
    prefs_keys: list[str]
    provider_key: str
    raw_settings: dict[str, JsonValue]
    reason: str
    settings_key: str
    workspace_id: str
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAssetsBrollSearchf51938b9ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    author: str
    duration: float
    page_url: str
    preview: str
    video_id: str
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAssetsBrollSearchf51938b9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[PostApiV1WorkspacesWorkspaceAssetsBrollSearchf51938b9ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsFormats37b0bb27ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    format: str
    kind: str
    media_type: str
    reason: str | None
    suffix: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsFormats37b0bb27(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceExportsFormats37b0bb27ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfigCaptions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    enabled: bool
    formats: list[str]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfigColor(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    matrix: str
    transfer: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfigWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    enabled: bool
    text: str | None = None
    # 1 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfig(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audio_bitrate_kbps: int | None
    audio_channels: int | None
    audio_codec: str | None
    bitrate_kbps: int | None
    captions: GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfigCaptions
    color: GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfigColor | None
    fps: float | None
    height: int | None
    video_codec: str | None
    watermark: GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfigWatermark
    width: int | None
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    config: GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRowConfig
    created_at: str
    id: str
    is_builtin: bool
    name: str
    preset: str
    updated_at: str
    workspace_id: str | None
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceExportsProfiles7944fb34ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExports55d547baItemsRowProfile(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    name: str
    preset: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExports55d547baItemsRowTarget(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExports55d547baItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    artifact: None
    attempt: int
    created_at: str
    error: None
    finished_at: None
    format: str
    id: str
    profile: GetApiV1WorkspacesWorkspaceExports55d547baItemsRowProfile
    progress: int
    state: str
    target: GetApiV1WorkspacesWorkspaceExports55d547baItemsRowTarget
    verification: None
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExports55d547ba(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceExports55d547baItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsExportId70a46cecProfile(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    name: str
    preset: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsExportId70a46cecTarget(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExportsExportId70a46cec(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    artifact: None
    attempt: int
    created_at: str
    error: None
    finished_at: None
    format: str
    id: str
    profile: GetApiV1WorkspacesWorkspaceExportsExportId70a46cecProfile
    progress: int
    state: str
    target: GetApiV1WorkspacesWorkspaceExportsExportId70a46cecTarget
    verification: None
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExports9d80f763(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    export_id: str
    format: str
    job_id: str
    queued: bool
    state: str
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExportsExportIdCancel3ad814c2(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cancelled: bool
    export_id: str
    state: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceVoicePreviewProvidersc35da5c2ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    contract_tested: bool
    label: str
    live_verified: bool
    message: str
    missing_credentials: list[str]
    offerable: bool
    provider: str
    qualification_labels: list[Literal["COMMERCIAL_LIMITATION", "CONFIG_GATED", "CONTRACT_TESTED", "IMPLEMENTED", "SIMULATION", "UNAVAILABLE"]]
    reason: str
    simulation_only: bool
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceVoicePreviewProvidersc35da5c2(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    default_provider: str
    items: list[GetApiV1WorkspacesWorkspaceVoicePreviewProvidersc35da5c2ItemsRow]
    max_chars: int
    max_per_window: int
    offerable: list[str]
    unavailable: list[str]
    window_seconds: float
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceVoicePreviewProvidersProviderIdVoicesd6d922f5ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    gender: str
    id: str
    locale: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceVoicePreviewProvidersProviderIdVoicesd6d922f5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cache: str
    count: int
    empty_reason: str
    items: list[GetApiV1WorkspacesWorkspaceVoicePreviewProvidersProviderIdVoicesd6d922f5ItemsRow]
    language: str
    provider: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAutopilotStart02b505c0(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    state: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAutopilotStop327a06f3(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    stopping: bool
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAutopilotPause2fff26cc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    paused: bool
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAutopilotResume228b44a5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    resumed: bool
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAutopilotRunOneCycleadc52683(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    state: str
    # 2 of 2 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceAgentsConfigAgentKey604c5cc8(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAutopilotStatus4c80730e(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    config: dict[str, JsonValue]
    current_cycle: None
    cycles_completed: int
    cycles_target: int
    last_error: str
    mode: str
    queued_jobs: int
    scheduled_start_at: None
    scheduled_stop_at: None
    state: str
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDecision691f9e06FactorsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    contribution: float
    detail: str
    name: str
    value: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDecision691f9e06(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: str
    confidence: float
    evidence: list[JsonValue]
    factors: list[GetApiV1WorkspacesWorkspaceDecision691f9e06FactorsRow]
    opportunity_id: str | None
    reasons: list[str]
    score: float
    topic: str | None
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAgentsConfig6add3e8aItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cost_limit_usd: None
    description: str
    enabled: bool
    execution_policy: str
    key: str
    model: str
    model_policy: str
    permissions: list[str]
    skills: list[str]
    timeout_seconds: int
    title: str
    tools: list[str]
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAgentsConfig6add3e8a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceAgentsConfig6add3e8aItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCycles7f1447ad(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceBrandsea60bbe0BrandsRowAssetsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_role: str
    brand_id: str
    created_at: str
    id: str
    label: str
    media_asset_id: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceBrandsea60bbe0BrandsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    assets: list[GetApiV1WorkspacesWorkspaceBrandsea60bbe0BrandsRowAssetsRow]
    created_at: str
    dna: dict[str, JsonValue]
    id: str
    is_default: bool
    name: str
    status: str
    updated_at: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceBrandsea60bbe0(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brands: list[GetApiV1WorkspacesWorkspaceBrandsea60bbe0BrandsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceBrandsPresetsa7340d9d(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    presets: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLessons70f2d331(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceBrand3dbb4de0Brand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    accent: str
    app_name: str
    logo_path: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceBrand3dbb4de0(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: GetApiV1WorkspacesWorkspaceBrand3dbb4de0Brand
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMusicPrefs4700a95a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    keys: list[str]
    note: str
    prefs: dict[str, JsonValue]
    stated: bool
    workspace_id: str
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrands5ac0ae54Brand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    assets: list[JsonValue]
    created_at: str
    dna: dict[str, JsonValue]
    id: str
    is_default: bool
    name: str
    status: str
    updated_at: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrands5ac0ae54(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: PostApiV1WorkspacesWorkspaceBrands5ac0ae54Brand
    # 1 of 1 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceBrandsBrandId25b5bfebBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    assets: list[JsonValue]
    created_at: str
    dna: dict[str, JsonValue]
    id: str
    is_default: bool
    name: str
    status: str
    updated_at: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceBrandsBrandId25b5bfeb(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: PutApiV1WorkspacesWorkspaceBrandsBrandId25b5bfebBrand
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdAssets8ed45ee1Asset(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_role: str
    brand_id: str
    created_at: str
    id: str
    label: str
    media_asset_id: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdAssets8ed45ee1(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset: PostApiV1WorkspacesWorkspaceBrandsBrandIdAssets8ed45ee1Asset
    # 1 of 1 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspaceBrandsBrandIdAssets4a248280(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    removed: int
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksApprovedColors(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksAvatarApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksCaptionStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksCtaStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksFonts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksForbiddenPhrases(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksLogoUse(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksRequiredDisclaimers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksTerminology(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksThumbnailConventions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksVoiceApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_colors: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksApprovedColors
    avatar_approval: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksAvatarApproval
    caption_style: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksCaptionStyle
    cta_style: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksCtaStyle
    fonts: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksFonts
    forbidden_phrases: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksForbiddenPhrases
    logo_use: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksLogoUse
    required_disclaimers: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksRequiredDisclaimers
    terminology: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksTerminology
    thumbnail_conventions: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksThumbnailConventions
    voice_approval: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksVoiceApproval
    watermark: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecksWatermark
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportProvenance(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_avatars: str
    approved_logos: str
    approved_voices: str
    brand_colors: str
    caption_style: str
    claims_policy: str
    cta_style: str
    fonts: str
    forbidden_phrases: str
    logo_safe_zone: str
    pronunciation_rules: str
    required_disclaimers: str
    thumbnail_style: str
    tone: str
    vocabulary: str
    watermark: str
    # 16 of 16 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportSemantic(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    authoritative: bool
    detail: str
    mode: str
    status: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReport(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    artifact_kind: str
    authoritative: bool
    checks: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportChecks
    effective_config_id: str
    provenance: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportProvenance
    report_type: str
    semantic: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReportSemantic
    status: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    report: PostApiV1WorkspacesWorkspaceBrandsBrandIdVerify9c53511bReport
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdDeriveff195f1a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdGenerateMore5860c5fe(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created: list[str]
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdScheduledd86eda5ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approval_state: str
    depends_on: list[JsonValue]
    is_master: bool
    planned_at: str
    platform: str
    priority: int
    publication_state: str
    variant_id: str
    # 8 of 8 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdScheduledd86eda5Schedule(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created: list[str]
    reused: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdScheduledd86eda5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[PostApiV1WorkspacesWorkspaceCampaignsCampaignIdScheduledd86eda5ItemsRow]
    schedule: PostApiV1WorkspacesWorkspaceCampaignsCampaignIdScheduledd86eda5Schedule
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdPublish6ef19a9b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    enqueued: list[JsonValue]
    skipped: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsCampaignIdCancel4a5e5dcd(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceContentContentIdRegenerate37b01d62(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    lineage_version: int
    status: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceContentContentIdPlatformVariants41270f23Metadata(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    caption: str
    cover_text: str
    cta_kind: str
    cta_text: str
    description: str
    hashtags: list[str]
    keywords: list[str]
    pinned_comment: str
    platform: str
    title: str
    # 10 of 10 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceContentContentIdPlatformVariants41270f23SafeZones(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    bottom: float
    left: float
    right: float
    top: float
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceContentContentIdPlatformVariants41270f23(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    cover_asset_id: None
    id: str
    metadata: PostApiV1WorkspacesWorkspaceContentContentIdPlatformVariants41270f23Metadata
    platform: str
    published_post_id: None
    publishing_job_id: None
    safe_zones: PostApiV1WorkspacesWorkspaceContentContentIdPlatformVariants41270f23SafeZones
    shares_base_timeline: bool
    short_content_id: str
    status: str
    timeline_id: None
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignId31803b8fProgress(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    content_items: int
    published: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignId31803b8f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    automation_level: str
    budget_daily_usd: None
    ends_at: None
    goal: str
    id: str
    name: str
    platforms: list[JsonValue]
    progress: GetApiV1WorkspacesWorkspaceCampaignsCampaignId31803b8fProgress
    starts_at: None
    status: str
    target_videos: int
    videos_per_day: float
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bCampaign(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    platforms: list[JsonValue]
    status: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bCosts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    entries: int
    priced_entries: int
    total_usd: None
    unknown_exposure_entries: int
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bPlanItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approval_state: str
    depends_on: list[JsonValue]
    is_master: bool
    planned_at: str
    platform: str
    priority: int
    publication_state: str
    variant_id: str
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bPlan(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bPlanItemsRow]
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bProgress(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    completed: int
    total: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bQcItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    overall: None
    passed: None
    short_content_id: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    avg_overall: None
    items: list[GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bQcItemsRow]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bShortsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    derivation_type: str
    id: str
    parent_content_id: None
    root_content_id: None
    status: str
    topic: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign: GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bCampaign
    costs: GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bCosts
    master: None
    plan: GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bPlan
    progress: GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bProgress
    qc: GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bQc
    shorts: list[GetApiV1WorkspacesWorkspaceCampaignsCampaignIdAggregate1e63c18bShortsRow]
    variants: list[JsonValue]
    # 8 of 8 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaigns85cf3243(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContent8084c7d5ItemsRowStrategy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    angle: str | None = None
    # 0 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContent8084c7d5ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str | None
    created_at: str
    cycle_id: None
    error: str
    id: str
    status: str
    strategy: GetApiV1WorkspacesWorkspaceContent8084c7d5ItemsRowStrategy
    topic: str
    variants_count: int
    video: None
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContent8084c7d5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceContent8084c7d5ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsFromMasterbbcc739aPlanProgress(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    completed: int
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsFromMasterbbcc739aPlan(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cta_kind: str
    desired_shorts: int
    goal: str
    master_content_id: str
    progress: PostApiV1WorkspacesWorkspaceCampaignsFromMasterbbcc739aPlanProgress
    status: str
    target_platforms: list[str]
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceCampaignsFromMasterbbcc739a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    plan: PostApiV1WorkspacesWorkspaceCampaignsFromMasterbbcc739aPlan
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendard1d45047ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str | None
    content_item_id: str
    id: str
    platform: str
    run_at: str
    status: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendard1d45047(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceCalendard1d45047ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCostsc5c240e9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_budget_usd: float
    last_24h_by_category: dict[str, JsonValue]
    per_video_budget_usd: float
    remaining_usd: float
    spent_last_24h_unknown_exposure_rows: int
    spent_last_24h_usd: None
    within_budget: bool
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerOpportunities165707deOpportunitiesRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    angle: str
    audience: str
    basis: str
    basis_meaning: str
    brand_fit: float
    competition_evidence: dict[str, JsonValue]
    confidence: float
    dedupe_reason: str
    dedupe_verdict: str
    estimated_cost_usd: float
    estimated_effort_hours: float
    evidence: list[JsonValue]
    format: str
    freshness: str
    id: str
    plan_item_id: None
    platforms: list[JsonValue]
    score: float
    scoring: dict[str, JsonValue]
    topic: str
    why: str
    # 21 of 21 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerOpportunities165707de(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    forbidden_claims: list[str]
    note: str
    opportunities: list[GetApiV1WorkspacesWorkspacePlannerOpportunities165707deOpportunitiesRow]
    workspace_id: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxOpportunities63732f48(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1SystemReadinessbbd03314(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    blocking_failures: list[JsonValue]
    checked_at: str
    checks: list[JsonValue]
    message: str
    stale_after_hours: int
    status: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceProviderMaturityIncidents764a0829(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    items: list[JsonValue]
    note: str
    states: list[str]
    unknown_exposure_count: int
    workspace_id: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceReviews2d8accf5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsc9fedb91ItemsRowControl(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    variant_ref: str | None = None
    # 0 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsc9fedb91ItemsRowVariantsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    descriptor: str
    variant_ref: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsc9fedb91ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: str
    control: GetApiV1WorkspacesWorkspaceExperimentsc9fedb91ItemsRowControl
    created_at: str
    ended_at: None
    hypothesis: str
    id: str
    kind: str
    minimum_sample: int
    platform: str
    primary_metric: str
    result: dict[str, JsonValue]
    secondary_metrics: list[JsonValue]
    started_at: None
    status: str
    updated_at: str
    variants: list[GetApiV1WorkspacesWorkspaceExperimentsc9fedb91ItemsRowVariantsRow]
    workspace_id: str
    # 17 of 17 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsc9fedb91(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceExperimentsc9fedb91ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceKnowledgeMemories28792a8bItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_id: None
    confidence: float
    conflict_group: str
    content: str
    content_hash: str
    created_at: str
    effective_status: str
    evidence_ids: list[JsonValue]
    freshness: str
    id: str
    last_used_at: str
    last_verified_at: None
    origin: str
    platform: str
    related_json: dict[str, JsonValue]
    scope: str
    source_ids: list[JsonValue]
    status: str
    superseded_by: None
    topic: str
    topic_key: str
    type: str
    updated_at: str
    use_count: int
    workspace_id: str
    # 25 of 25 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceKnowledgeMemories28792a8b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceKnowledgeMemories28792a8bItemsRow]
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdRead90174efeInteraction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    author_name: str
    author_remote_id: str
    campaign_id: None
    classifications: list[JsonValue]
    content_item_id: None
    conversation_id: str
    created_at: str
    id: str
    intent: str
    is_question: bool
    kind: str
    moderation: list[JsonValue]
    moderation_state: str
    parent_interaction_id: None
    platform: str
    post_remote_id: str
    priority: str
    published_post_id: None
    remote_created_at: str
    remote_id: str
    sentiment: str
    status: str
    text: str
    thread_id: str
    unread: bool
    updated_at: str
    workspace_id: str
    # 28 of 28 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdRead90174efe(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    interaction: PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdRead90174efeInteraction
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088ClassificationsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence: str
    label: str
    model: str
    provider: str
    source: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088InteractionClassificationsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence: str
    label: str
    model: str
    provider: str
    source: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088Interaction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    author_name: str
    author_remote_id: str
    campaign_id: None
    classifications: list[PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088InteractionClassificationsRow]
    content_item_id: None
    conversation_id: str
    created_at: str
    id: str
    intent: str
    is_question: bool
    kind: str
    moderation: list[JsonValue]
    moderation_state: str
    parent_interaction_id: None
    platform: str
    post_remote_id: str
    priority: str
    published_post_id: None
    remote_created_at: str
    remote_id: str
    sentiment: str
    status: str
    text: str
    thread_id: str
    unread: bool
    updated_at: str
    workspace_id: str
    # 28 of 28 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    classifications: list[PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088ClassificationsRow]
    interaction: PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdClassify74ffd088Interaction
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdActionBrandCheck(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    attempts: int
    effective_config_id: str
    forbidden_hits: list[JsonValue]
    missing_disclaimers: list[JsonValue]
    required_disclaimers: list[JsonValue]
    status: str
    tone: str
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdAction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: None
    badge: str
    brand_check: PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdActionBrandCheck
    conversation_id: str
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: None
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdInteractionClassificationsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence: str
    label: str
    model: str
    provider: str
    source: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdInteraction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    author_name: str
    author_remote_id: str
    campaign_id: None
    classifications: list[PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdInteractionClassificationsRow]
    content_item_id: None
    conversation_id: str
    created_at: str
    id: str
    intent: str
    is_question: bool
    kind: str
    moderation: list[JsonValue]
    moderation_state: str
    parent_interaction_id: None
    platform: str
    post_remote_id: str
    priority: str
    published_post_id: None
    remote_created_at: str
    remote_id: str
    sentiment: str
    status: str
    text: str
    thread_id: str
    unread: bool
    updated_at: str
    workspace_id: str
    # 28 of 28 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cd(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdAction
    interaction: PostApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdDraftb614e4cdInteraction
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdApprovebfcbbce0Action(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: str
    badge: str
    brand_check: dict[str, JsonValue]
    conversation_id: None
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: None
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdApprovebfcbbce0(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: PostApiV1WorkspacesWorkspaceInboxActionsActionIdApprovebfcbbce0Action
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdRejectee4e3194Action(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: None
    badge: str
    brand_check: dict[str, JsonValue]
    conversation_id: None
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: str
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdRejectee4e3194(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: PostApiV1WorkspacesWorkspaceInboxActionsActionIdRejectee4e3194Action
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527Action(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: None
    badge: str
    brand_check: dict[str, JsonValue]
    conversation_id: str
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: None
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527InteractionClassificationsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence: str
    label: str
    model: str
    provider: str
    source: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527Interaction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    author_name: str
    author_remote_id: str
    campaign_id: None
    classifications: list[PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527InteractionClassificationsRow]
    content_item_id: None
    conversation_id: str
    created_at: str
    id: str
    intent: str
    is_question: bool
    kind: str
    moderation: list[JsonValue]
    moderation_state: str
    parent_interaction_id: None
    platform: str
    post_remote_id: str
    priority: str
    published_post_id: None
    remote_created_at: str
    remote_id: str
    sentiment: str
    status: str
    text: str
    thread_id: str
    unread: bool
    updated_at: str
    workspace_id: str
    # 28 of 28 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527Action
    interaction: PostApiV1WorkspacesWorkspaceInboxConversationsConversationIdReply04676527Interaction
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625Action(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: None
    badge: str
    brand_check: dict[str, JsonValue]
    conversation_id: None
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: None
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625ResultReceipt(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    is_mock: bool
    mock: bool
    remote_reply_id: str
    text: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625Result(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action_id: str
    is_mock: bool
    reason: str
    receipt: PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625ResultReceipt
    remote_reply_id: str
    sent: bool
    state: str
    verification_status: str
    # 8 of 8 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    action: PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625Action
    result: PostApiV1WorkspacesWorkspaceInboxActionsActionIdSendb1b34625Result
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dActionsRowBrandCheck(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    attempts: int | None = None
    effective_config_id: str | None = None
    forbidden_hits: list[JsonValue] | None = None
    missing_disclaimers: list[JsonValue] | None = None
    required_disclaimers: list[JsonValue] | None = None
    status: str | None = None
    tone: str | None = None
    # 0 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dActionsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: str | None
    badge: str
    brand_check: GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dActionsRowBrandCheck
    conversation_id: str | None
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: str | None
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dInteractionClassificationsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence: str
    label: str
    model: str
    provider: str
    source: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dInteraction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    author_name: str
    author_remote_id: str
    campaign_id: None
    classifications: list[GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dInteractionClassificationsRow]
    content_item_id: None
    conversation_id: str
    created_at: str
    id: str
    intent: str
    is_question: bool
    kind: str
    moderation: list[JsonValue]
    moderation_state: str
    parent_interaction_id: None
    platform: str
    post_remote_id: str
    priority: str
    published_post_id: None
    remote_created_at: str
    remote_id: str
    sentiment: str
    status: str
    text: str
    thread_id: str
    unread: bool
    updated_at: str
    workspace_id: str
    # 28 of 28 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265d(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    actions: list[GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dActionsRow]
    interaction: GetApiV1WorkspacesWorkspaceInboxInteractionsInteractionIdc017265dInteraction
    linked_publication: None
    thread: list[JsonValue]
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fConversationLastInteraction(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    author_name: str
    created_at: str
    id: str
    kind: str
    platform: str
    status: str
    text: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fConversation(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    created_at: str
    id: str
    last_interaction: GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fConversationLastInteraction
    last_interaction_at: str
    participant_name: str
    participant_remote_id: str
    platform: str
    priority: str
    published_post_id: None
    status: str
    thread_key: str
    title: str
    unread_count: int
    updated_at: str
    workspace_id: str
    # 16 of 16 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fInteractionsRowClassificationsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence: str
    label: str
    model: str
    provider: str
    source: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fInteractionsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    author_name: str
    author_remote_id: str
    campaign_id: None
    classifications: list[GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fInteractionsRowClassificationsRow]
    content_item_id: None
    conversation_id: str
    created_at: str
    id: str
    intent: str
    is_question: bool
    kind: str
    moderation: list[JsonValue]
    moderation_state: str
    parent_interaction_id: None
    platform: str
    post_remote_id: str
    priority: str
    published_post_id: None
    remote_created_at: str
    remote_id: str
    sentiment: str
    status: str
    text: str
    thread_id: str
    unread: bool
    updated_at: str
    workspace_id: str
    # 28 of 28 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    conversation: GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fConversation
    interactions: list[GetApiV1WorkspacesWorkspaceInboxConversationsConversationIdacf5b89fInteractionsRow]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMediaCaption(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    max_chars_per_line: int
    max_lines: int
    style: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMediaSafeZones(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    bottom: float
    left: float
    right: float
    top: float
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMediaThumbnail(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    behavior: str
    cover_text_max: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMedia(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspects: list[str]
    caption: GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMediaCaption
    cta: list[str]
    max_duration: float
    posting_windows: list[int]
    preferred_duration: list[float]
    publication_mode: str | None = None
    safe_zones: GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMediaSafeZones
    thumbnail: GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMediaThumbnail
    # 8 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMetadataLimits(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    description_max: int
    hashtag_limit: int
    hashtag_max: int
    title_max: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratios: list[str]
    campaign_platforms: list[str]
    capabilities: list[Literal["ALT_TEXT", "CAROUSEL", "COMMENTS", "DELETE_COMMENT", "DIRECT_PUBLISH", "FETCH_METRICS", "IMAGE", "LINK", "METRICS", "PUBLISH_IMAGE", "PUBLISH_SHORT", "PUBLISH_TEXT", "PUBLISH_VIDEO", "READ_COMMENTS", "READ_MENTIONS", "REPLY", "REPLY_COMMENT", "TEXT", "USER_HANDOFF", "VIDEO"]]
    duration_s: list[float]
    media: GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMedia
    metadata_limits: GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRowMetadataLimits
    platform: str
    supports_analytics: bool
    supports_inbox: bool
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    capabilities: dict[str, JsonValue]
    observed: list[str]
    platforms: list[GetApiV1WorkspacesWorkspaceInboxPlatforms3ce40ba2PlatformsRow]
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13AutonomyCaps(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_replies: int
    hourly_replies: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13Autonomy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    caps: GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13AutonomyCaps
    classes: list[JsonValue]
    mode: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13DefaultsCaps(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_replies: int
    hourly_replies: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13Defaults(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    caps: GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13DefaultsCaps
    classes: list[JsonValue]
    mode: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    autonomy: GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13Autonomy
    classes: list[str]
    defaults: GetApiV1WorkspacesWorkspaceInboxAutonomyaa86cd13Defaults
    modes: list[str]
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAnalytics0830acedMetrics(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    comments: int
    content_requests: int
    interactions: int
    lead_signals: int
    questions: int
    replies_sent: int
    reply_rate: float
    since: str
    spam: int
    window_days: int
    workspace_id: str
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxAnalytics0830aced(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    kpis: list[JsonValue]
    metrics: GetApiV1WorkspacesWorkspaceInboxAnalytics0830acedMetrics
    totals: dict[str, JsonValue]
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxActions933dc997ItemsRowBrandCheck(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    attempts: int | None = None
    effective_config_id: str | None = None
    forbidden_hits: list[JsonValue] | None = None
    missing_disclaimers: list[JsonValue] | None = None
    required_disclaimers: list[JsonValue] | None = None
    status: str | None = None
    tone: str | None = None
    # 0 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxActions933dc997ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    action_type: str
    approval_user_id: str | None
    badge: str
    brand_check: GetApiV1WorkspacesWorkspaceInboxActions933dc997ItemsRowBrandCheck
    conversation_id: str | None
    created_at: str
    draft_text: str
    error: str
    final_text: str
    id: str
    interaction_id: str
    is_mock: bool
    mode: str
    origin: str
    platform: str
    rejected_user_id: str | None
    remote_reply_id: str
    sent_at: str
    state: str
    updated_at: str
    workspace_id: str
    # 22 of 22 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxActions933dc997(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceInboxActions933dc997ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceInboxInsightsa4a55f4f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    suggestions: list[JsonValue]
    suggestions_available: bool
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceInboxSynca67ccd11(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    payload: dict[str, JsonValue]
    type: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingOauthFacebookStart2e65f9aa(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    authorize_url: str
    # 1 of 1 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspacePublishingAccountsAccountId8cc09d2d(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: bool
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingAccounts3b2b7991ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    display_name: str
    external_id: str
    id: str
    platform: str
    status: str
    token_expires_at: None
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingAccounts3b2b7991(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspacePublishingAccounts3b2b7991ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePublishingJobsb7dc1418(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionPlatforms0bdf0826ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    capabilities: list[Literal["ALT_TEXT", "CAROUSEL", "COMMENTS", "DIRECT_PUBLISH", "FETCH_METRICS", "IMAGE", "LINK", "METRICS", "PUBLISH_IMAGE", "PUBLISH_SHORT", "PUBLISH_TEXT", "PUBLISH_VIDEO", "READ_COMMENTS", "REPLY", "REPLY_COMMENT", "TEXT", "USER_HANDOFF", "VIDEO"]]
    media_types: list[str]
    platform: str
    publish_mode: str
    unverified: list[str]
    verified_limits: list[str]
    verified_notes: list[str]
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDistributionPlatforms0bdf0826(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceDistributionPlatforms0bdf0826ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerCalendare7818739Capacity(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    declared: bool
    locale: str
    localization_per_day: float
    longform_per_week: float
    notes: str
    render_hours_per_day: float
    review_slots_per_day: float
    shorts_per_day: float
    ugc_per_day: float
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerCalendare7818739CommittedCommitted(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    review: float | None = None
    shorts: float | None = None
    # 0 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerCalendare7818739Committed(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    committed: GetApiV1WorkspacesWorkspacePlannerCalendare7818739CommittedCommitted
    committed_cost: float
    item_count: int
    locale: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerCalendare7818739EntriesRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str | None
    content_item_id: str
    id: str
    platform: str
    run_at: str
    status: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerCalendare7818739Remaining(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    localization: None
    longform: None
    render: None
    review: None
    shorts: None
    ugc: None
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerCalendare7818739(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    capacity: GetApiV1WorkspacesWorkspacePlannerCalendare7818739Capacity
    committed: GetApiV1WorkspacesWorkspacePlannerCalendare7818739Committed
    days: int
    entries: list[GetApiV1WorkspacesWorkspacePlannerCalendare7818739EntriesRow]
    note: str
    plan_item_count: int
    remaining: GetApiV1WorkspacesWorkspacePlannerCalendare7818739Remaining
    workspace_id: str
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cdCredentialSummaryByState(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    CONFIGURED: int
    NOT_CONFIGURED: int
    NOT_REQUIRED: int
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cdCredentialSummary(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    by_state: GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cdCredentialSummaryByState
    providers_without_credentials: list[str]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cdItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    blockers: list[str]
    capability: str
    commercial_status: str
    contract_status: str
    credential_keys: list[str]
    credential_status: str
    evidence: list[str]
    gaps: list[str]
    health: str
    implementation_status: str
    last_verified_at: str
    live_status: str
    notes: str
    production_ready: bool
    provider: str
    resolved_credential_status: str
    simulation_only: bool
    # 17 of 17 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cd(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    credential_summary: GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cdCredentialSummary
    items: list[GetApiV1WorkspacesWorkspaceProviderMaturityd992d4cdItemsRow]
    note: str
    resolved_credential_states: list[str]
    workspace_id: str
    # 6 of 6 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abMocks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    analytics: bool
    llm: bool
    publishing: bool
    trends: bool
    video_engine: bool
    # 5 of 5 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abPublishersFacebook(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    mode: str
    ready: bool
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abPublishersInstagram(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    mode: str
    ready: bool
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abPublishersTiktok(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    mode: str
    ready: bool
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abPublishersYoutube(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    mode: str
    ready: bool
    via_relay: bool
    # 4 of 4 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abPublishers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    facebook: GetApiV1SystemHealth7b9a63abPublishersFacebook
    instagram: GetApiV1SystemHealth7b9a63abPublishersInstagram
    tiktok: GetApiV1SystemHealth7b9a63abPublishersTiktok
    youtube: GetApiV1SystemHealth7b9a63abPublishersYoutube
    # 4 of 4 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abQueue(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    backend: str
    gpu_cuda: bool
    gpu_worker: bool
    redis: None
    # 4 of 4 fields were present in every observed state


class GetApiV1SystemHealth7b9a63abTts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    healthy: bool
    is_mock: bool
    provider: str
    # 3 of 3 fields were present in every observed state


class GetApiV1SystemHealth7b9a63ab(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    database: bool
    llm_provider: bool
    mocks: GetApiV1SystemHealth7b9a63abMocks
    publishers: GetApiV1SystemHealth7b9a63abPublishers
    queue: GetApiV1SystemHealth7b9a63abQueue
    status: str
    time: str
    tts: GetApiV1SystemHealth7b9a63abTts
    video_engine: bool
    video_engine_name: str
    video_engine_version: str
    # 11 of 11 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExperimentsExperimentIdAnalyzeea18e30eResultControl(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mean: float
    n: int
    variant_ref: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExperimentsExperimentIdAnalyzeea18e30eResult(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    analyzed_at: str
    arms: list[JsonValue]
    control: PostApiV1WorkspacesWorkspaceExperimentsExperimentIdAnalyzeea18e30eResultControl
    minimum_sample: int
    primary_metric: str
    reason: str
    total_samples: int
    winner: None
    # 8 of 8 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExperimentsExperimentIdAnalyzeea18e30e(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: str
    control: dict[str, JsonValue]
    created_at: str
    ended_at: None
    hypothesis: str
    id: str
    kind: str
    minimum_sample: int
    platform: str
    primary_metric: str
    result: PostApiV1WorkspacesWorkspaceExperimentsExperimentIdAnalyzeea18e30eResult
    secondary_metrics: list[JsonValue]
    started_at: None
    status: str
    updated_at: str
    variants: list[JsonValue]
    workspace_id: str
    # 17 of 17 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExperiments04398940Control(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    variant_ref: str
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExperiments04398940VariantsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    descriptor: str
    variant_ref: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceExperiments04398940(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: str
    control: PostApiV1WorkspacesWorkspaceExperiments04398940Control
    created_at: str
    ended_at: None
    hypothesis: str
    id: str
    kind: str
    minimum_sample: int
    platform: str
    primary_metric: str
    result: dict[str, JsonValue]
    secondary_metrics: list[JsonValue]
    started_at: None
    status: str
    updated_at: str
    variants: list[PostApiV1WorkspacesWorkspaceExperiments04398940VariantsRow]
    workspace_id: str
    # 17 of 17 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsExperimentIde22ed1afResultControl(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mean: float
    n: int
    variant_ref: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsExperimentIde22ed1afResult(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    analyzed_at: str
    arms: list[JsonValue]
    control: GetApiV1WorkspacesWorkspaceExperimentsExperimentIde22ed1afResultControl
    minimum_sample: int
    primary_metric: str
    reason: str
    total_samples: int
    winner: None
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceExperimentsExperimentIde22ed1af(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: str
    control: dict[str, JsonValue]
    created_at: str
    ended_at: None
    hypothesis: str
    id: str
    kind: str
    minimum_sample: int
    platform: str
    primary_metric: str
    result: GetApiV1WorkspacesWorkspaceExperimentsExperimentIde22ed1afResult
    secondary_metrics: list[JsonValue]
    started_at: None
    status: str
    updated_at: str
    variants: list[JsonValue]
    workspace_id: str
    # 17 of 17 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62ItemsRowInput(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    criterion: str
    items: list[str]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62ItemsRowOutput(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    order: list[int]
    reason: str
    scores: list[float]
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    actual_provider: str
    agree: None
    cost_usd: float
    created_at: str
    fallback_reason: str
    id: str
    input: GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62ItemsRowInput
    kind: str
    latency_ms: int
    mode: str
    model: str
    output: GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62ItemsRowOutput
    requested_provider: str
    # 13 of 13 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceIntelligenceDecisionsLogc2de7b62ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsShadowReport7d055e51ByKindRank(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    agreed: int
    agreement_rate: float
    total: int
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsShadowReport7d055e51ByKind(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    rank: GetApiV1WorkspacesWorkspaceIntelligenceDecisionsShadowReport7d055e51ByKindRank | None = None
    # 0 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceDecisionsShadowReport7d055e51(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    agreed: int
    agreement_rate: float | None
    avg_latency_ms: float | None
    by_kind: GetApiV1WorkspacesWorkspaceIntelligenceDecisionsShadowReport7d055e51ByKind
    disagreed: int
    kind: None
    total: int
    total_cost_usd: float
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceRoutingHealth8b0c0630Providers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    local: bool
    remote: bool
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceRoutingHealth8b0c0630(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    providers: GetApiV1WorkspacesWorkspaceIntelligenceRoutingHealth8b0c0630Providers
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceRoutingLogb5a890cb(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    entries: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceVerificationLedger266de275Chain(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    broken_at: None
    count: int
    ok: bool
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceIntelligenceVerificationLedger266de275(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    chain: GetApiV1WorkspacesWorkspaceIntelligenceVerificationLedger266de275Chain
    items: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalizationLocalizedIdQcc5586b08(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created_at: str
    id: str
    language: str
    localized_content_id: str
    status: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAssetsDubStatus42a42568(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ffmpeg: bool
    languages: list[str]
    llm: bool
    ready: bool
    tts: bool
    tts_provider: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceLocalizationRun528985bdItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    language: str
    locale: str
    status: str
    translation_version: int
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceLocalizationRun528985bd(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[PostApiV1WorkspacesWorkspaceLocalizationRun528985bdItemsRow]
    job_id: str
    queued: bool
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalizationLocalizedId00d6c34bQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    counts: dict[str, JsonValue]
    created_at: str
    id: str
    status: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalizationLocalizedId00d6c34b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    child_content_id: None
    costs: dict[str, JsonValue]
    created_at: str
    error: str
    id: str
    language: str
    lineage: dict[str, JsonValue]
    locale: str
    qc: GetApiV1WorkspacesWorkspaceLocalizationLocalizedId00d6c34bQc
    repairs: list[JsonValue]
    source_content_id: str
    stages: list[JsonValue]
    status: str
    timeline_id: None
    translation_version: int
    updated_at: str
    warnings: list[JsonValue]
    workspace_id: str
    # 18 of 18 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalizationGlossarya05e35e6ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    case_sensitive: bool
    created_at: str
    id: str
    kind: str
    replacement: str
    target_languages: list[JsonValue]
    term: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalizationGlossarya05e35e6(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceLocalizationGlossarya05e35e6ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceLocalizationGlossary8ca88723(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    case_sensitive: bool
    created_at: str
    id: str
    kind: str
    replacement: str
    target_languages: list[JsonValue]
    term: str
    # 7 of 7 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspaceLocalizationGlossaryTermIdb256ae22(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: str
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlanSegmentsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audio_seconds: None
    end: float
    index: int
    needs_review: bool
    planned_rate: float
    review_reason: str
    speaker_id: str
    start: float
    target_text: str
    text: str
    voice_review: bool
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlanSpeakersRowTimingConstraints(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    max_rate: float
    min_rate: float
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlanSpeakersRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    language: str
    pronunciation_rules: dict[str, JsonValue]
    source_voice: str
    speaker_id: str
    speaking_rate: float
    target_voice: str
    timing_constraints: GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlanSpeakersRowTimingConstraints
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlan(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    needs_review: bool
    notes: list[str]
    review_count: int
    segments: list[GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlanSegmentsRow]
    source_ref: str
    speakers: list[GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlanSpeakersRow]
    status: str
    target_language: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created_at: str
    id: str
    needs_review: bool
    plan: GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRowPlan
    review_count: int
    source_ref: str
    status: str
    target_language: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceDubbingPlans3fd6b94aItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAssetsDubDryRund039e63fChecksRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    step: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAssetsDubDryRund039e63fWarnsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    step: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAssetsDubDryRund039e63f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: list[PostApiV1WorkspacesWorkspaceAssetsDubDryRund039e63fChecksRow]
    ok: bool
    warns: list[PostApiV1WorkspacesWorkspaceAssetsDubDryRund039e63fWarnsRow]
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLipsyncHealth05dbb75aChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    gpu: bool
    max_concurrency: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLipsyncHealth05dbb75aQueue(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    active: int
    concurrency: int
    max_retries: int
    provider: str
    timeout_seconds: float
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLipsyncHealth05dbb75a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    checks: GetApiV1WorkspacesWorkspaceLipsyncHealth05dbb75aChecks
    detail: str
    provider: str
    queue: GetApiV1WorkspacesWorkspaceLipsyncHealth05dbb75aQueue
    remediation: str
    status: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLipsyncJobs8270d26cItemsRowCost(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    attempts: int | None = None
    cost_priced: bool | None = None
    cost_usd: float | None = None
    gpu_seconds: float | None = None
    provider: str | None = None
    # 0 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLipsyncJobs8270d26cItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    adapter_job_id: str
    audio_ref: str
    completed_at: str | None
    cost: GetApiV1WorkspacesWorkspaceLipsyncJobs8270d26cItemsRowCost
    cost_outcome: str
    created_at: str
    error: str
    execution_outcome: str
    id: str
    opts: dict[str, JsonValue]
    progress: float
    provider: str
    result_asset_ref: str
    started_at: str | None
    status: str
    updated_at: str
    video_ref: str
    workspace_id: str
    # 18 of 18 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLipsyncJobs8270d26c(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceLipsyncJobs8270d26cItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceLipsyncJobs781ca1fc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    adapter_job_id: str
    audio_ref: str
    completed_at: None
    cost: dict[str, JsonValue]
    cost_outcome: str
    created_at: str
    error: str
    execution_outcome: str
    id: str
    opts: dict[str, JsonValue]
    progress: float
    provider: str
    result_asset_ref: str
    started_at: None
    status: str
    updated_at: str
    video_ref: str
    workspace_id: str
    # 18 of 18 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceLipsyncJobsJobIdCancel9425e509(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    adapter_job_id: str
    audio_ref: str
    completed_at: str
    cost: dict[str, JsonValue]
    cost_outcome: str
    created_at: str
    error: str
    execution_outcome: str
    id: str
    opts: dict[str, JsonValue]
    progress: float
    provider: str
    result_asset_ref: str
    started_at: None
    status: str
    updated_at: str
    video_ref: str
    workspace_id: str
    # 18 of 18 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMediaIntelSpeakers47d824ff(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    anonymous: bool
    count: int
    items: list[JsonValue]
    note: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMediaIntelSpeakersAliases3b3ff569(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    items: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalization4855c9e4ItemsRowCosts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mode: str | None = None
    note: str | None = None
    translation_usd: float | None = None
    tts_usd: float | None = None
    # 0 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalization4855c9e4ItemsRowQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    counts: dict[str, JsonValue]
    created_at: str
    id: str
    status: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalization4855c9e4ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    child_content_id: None
    costs: GetApiV1WorkspacesWorkspaceLocalization4855c9e4ItemsRowCosts
    created_at: str
    error: str
    id: str
    language: str
    locale: str
    qc: GetApiV1WorkspacesWorkspaceLocalization4855c9e4ItemsRowQc | None
    repairs: list[JsonValue]
    source_content_id: str
    stages: list[JsonValue]
    status: str
    timeline_id: None
    translation_version: int
    updated_at: str
    warnings: list[JsonValue]
    workspace_id: str
    # 17 of 17 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceLocalization4855c9e4(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceLocalization4855c9e4ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceKnowledgeMemoriesMemoryIdDisable9043dc89(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_id: None
    confidence: float
    conflict_group: str
    content: str
    content_hash: str
    created_at: str
    effective_status: str
    evidence_ids: list[JsonValue]
    freshness: str
    id: str
    last_used_at: str
    last_verified_at: None
    origin: str
    platform: str
    related_json: dict[str, JsonValue]
    scope: str
    source_ids: list[JsonValue]
    status: str
    superseded_by: None
    topic: str
    topic_key: str
    type: str
    updated_at: str
    use_count: int
    workspace_id: str
    # 25 of 25 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceKnowledgeMemories6a05a20d(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_id: None
    confidence: float
    conflict_group: str
    content: str
    content_hash: str
    created_at: str
    effective_status: str
    evidence_ids: list[JsonValue]
    freshness: str
    id: str
    last_used_at: str
    last_verified_at: None
    origin: str
    platform: str
    related_json: dict[str, JsonValue]
    scope: str
    source_ids: list[JsonValue]
    status: str
    superseded_by: None
    topic: str
    topic_key: str
    type: str
    updated_at: str
    use_count: int
    workspace_id: str
    # 25 of 25 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceKnowledgeSources0b9e70c7ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    blurb: str
    config: dict[str, JsonValue]
    doc_count: int
    enabled: bool
    has_credentials: bool
    id: str
    implemented: bool
    kind: str
    last_cursor: str
    last_error: str
    last_sync_at: None
    name: str
    requires_credentials: bool
    status: str
    title: str
    unavailable_reason: str
    # 16 of 16 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceKnowledgeSources0b9e70c7(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceKnowledgeSources0b9e70c7ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceKnowledgeSourcesConnectorIdDocumentsf7175ed8(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceKnowledgeGraph97742c74(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    edges: list[JsonValue]
    nodes: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerMemory787fc296MemoriesRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    content: str
    id: str
    lifecycle: str
    status: str
    topic: str
    type: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerMemory787fc296(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    memories: list[GetApiV1WorkspacesWorkspacePlannerMemory787fc296MemoriesRow]
    memory_count: int
    needs_revalidation: list[JsonValue]
    settled_topics: list[JsonValue]
    used_ids: list[str]
    workspace_id: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Audit(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    events_last_7d: int
    retention_enforced: bool
    since: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Costs(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    daily_budget_usd: float
    last_24h_by_category: dict[str, JsonValue]
    per_video_budget_usd: float
    remaining_usd: float
    since: str
    spent_last_24h_usd: int
    within_budget: bool
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8ExportsByState(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    CANCELLED: int | None = None
    QUEUED: int | None = None
    # 0 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Exports(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    by_state: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8ExportsByState
    failed: list[JsonValue]
    failed_count: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8JobsByStatus(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    QUEUED: int
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Jobs(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    by_status: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8JobsByStatus
    failed_recent: list[JsonValue]
    total: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8ProviderHealth(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    blocking_failures: list[JsonValue]
    checked_at: str
    checks: list[JsonValue]
    status: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Retention(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audit_enforced: bool
    audit_retention_days: None
    available: bool
    export_retention_days: None
    id: str | None
    render_retention_days: None
    temp_asset_retention_days: None
    updated_at: str | None
    updated_by: str | None
    workspace_id: str | None
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Reviews(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    open: int
    stale_approval_count: int
    stale_approvals: list[JsonValue]
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Storage(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    bytes: int
    file_count: int
    source: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audit: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Audit
    costs: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Costs
    exports: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Exports
    generated_at: str
    jobs: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Jobs
    provider_health: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8ProviderHealth
    retention: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Retention
    reviews: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Reviews
    storage: GetApiV1WorkspacesWorkspaceOpsOverviewb5f2eec8Storage
    workspace_id: str
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCostsIntelligencec8747b22PublicationsByPlatform(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    tiktok: int | None = None
    # 0 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCostsIntelligencec8747b22Totals(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cycles: int
    posts_published: int
    videos_built: int
    views: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCostsIntelligencec8747b22(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    by_agent: dict[str, JsonValue]
    by_category: dict[str, JsonValue]
    cost_per_1000_views_usd: None
    estimated_return_note: str
    estimated_return_usd: None
    per_cycle_usd: None
    per_publication_usd: float | None
    per_video_usd: float | None
    publications_by_platform: GetApiV1WorkspacesWorkspaceCostsIntelligencec8747b22PublicationsByPlatform
    total_cost_usd: float
    totals: GetApiV1WorkspacesWorkspaceCostsIntelligencec8747b22Totals
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceRetention893e665b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audit_enforced: bool
    audit_retention_days: None
    export_retention_days: None
    id: str
    render_retention_days: None
    temp_asset_retention_days: None
    updated_at: str
    updated_by: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class PatchApiV1WorkspacesWorkspaceCalendarEntryIdd3790aab(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    run_at: str
    # 2 of 2 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspaceCalendarEntryIdade1119e(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cancelled: bool
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendarBestTimes6ca51070ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    avg_views: int
    hour: int
    posts: int
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendarBestTimes6ca51070(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceCalendarBestTimes6ca51070ItemsRow]
    measured: bool
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bdddaAudienceActivity(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    hours: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bdddaCaps(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cooldown_seconds: int
    daily_cap: int
    max_uploads_per_hour: int
    rate_per_10min: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bdddaItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    hour: int
    reason: str
    sources: list[str]
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bddda(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    activity: bool
    activity_policy: str
    audience_activity: GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bdddaAudienceActivity
    avoid_hours: list[int]
    caps: GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bdddaCaps
    items: list[GetApiV1WorkspacesWorkspaceCalendarResponseWindows594bdddaItemsRow]
    measured: bool
    notes: list[str]
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpportunitiesacce22b9ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    components: dict[str, JsonValue]
    confidence: float
    created_at: str
    id: str
    lifecycle: str
    recommendation: str
    score: float
    selected: bool
    skipped_reason: str
    source: str
    source_url: None
    topic: str
    velocity: None
    virality: float
    volume: None
    # 15 of 15 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceOpportunitiesacce22b9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceOpportunitiesacce22b9ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerSignals924e09b9SignalsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    confidence: float
    evidence_ids: list[JsonValue]
    external_ref: str
    freshness: str
    id: str
    observed_at: str
    recurrence: int
    scope: str
    source: str
    status: str
    topic: str
    topic_key: str
    usable_as_demand: bool
    velocity: None
    # 14 of 14 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerSignals924e09b9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    note: str
    signals: list[GetApiV1WorkspacesWorkspacePlannerSignals924e09b9SignalsRow]
    sources: list[str]
    workspace_id: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPlans6bb4b2cePlansRowItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    angle: str
    blocked_reason: str
    campaign_id: None
    content_format: str
    dependencies: list[JsonValue]
    estimated_cost_usd: float
    id: str
    opportunity_id: str
    platforms: list[str]
    priority: float
    schedule_entry_id: None
    status: str
    target_date: str
    why: dict[str, JsonValue]
    # 14 of 14 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPlans6bb4b2cePlansRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    autonomy: str
    budget_remaining: float
    budget_usd: float
    constraints: dict[str, JsonValue]
    goals: list[JsonValue]
    horizon_days: int
    id: str
    item_count: int
    items: list[GetApiV1WorkspacesWorkspacePlannerPlans6bb4b2cePlansRowItemsRow]
    platforms: list[str]
    spent_usd: float
    status: str
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPlans6bb4b2ce(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    plans: list[GetApiV1WorkspacesWorkspacePlannerPlans6bb4b2cePlansRow]
    workspace_id: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerItemsItemIdApprove6c3f990b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerItemsItemIdReject3ee5a8be(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    reason: str
    status: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerItemsItemIdResearchMore8b8d677c(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerItemsItemIdCampaign1bcd6dd9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    blocked: str
    campaign_id: str
    complete: bool
    content_item_id: str
    opportunity_id: str
    plan_item_id: str
    schedule_entry_id: str
    skipped: list[str]
    stages_done: list[str]
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerItemsItemIdSchedule3d0b6e9aScheduleEntry(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created: bool
    entry_id: str
    evidence_backed: bool
    plan_item_id: str
    reason: str
    run_at: str
    status: str
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerItemsItemIdSchedule3d0b6e9a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    evidence_backed_slot: bool
    id: str
    publishes: bool
    schedule_entry: PostApiV1WorkspacesWorkspacePlannerItemsItemIdSchedule3d0b6e9aScheduleEntry
    schedule_entry_id: str
    status: str
    why_scheduled: str
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerPlan6fcca071Memory(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    memory_count: int
    needs_revalidation: list[JsonValue]
    settled_topics: list[JsonValue]
    used_ids: list[str]
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerPlan6fcca071(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    autonomy: str
    blocked: list[JsonValue]
    items: list[JsonValue]
    memory: PostApiV1WorkspacesWorkspacePlannerPlan6fcca071Memory
    notes: list[str]
    plan_id: str
    preview: bool
    publishes: bool
    suggestions: list[JsonValue]
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99CommittedCommitted(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    review: float | None = None
    shorts: float | None = None
    # 0 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99Committed(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    committed: PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99CommittedCommitted
    committed_cost: float
    item_count: int
    locale: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99Declared(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    localization: None
    longform: None
    render: None
    review: None
    shorts: None
    ugc: None
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    committed: PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99Committed
    declared: PostApiV1WorkspacesWorkspacePlannerCapacity21a67e99Declared
    declared_note: str
    is_unbounded: bool
    locale: str
    workspace_id: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    advances_production: bool
    creates_campaign_drafts: bool
    creates_plan_items: bool
    note: str
    publishes: bool
    rank: int
    schedules: bool
    starts_research: bool
    suggests: bool
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableAutonomous(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    advances_production: bool
    creates_campaign_drafts: bool
    creates_plan_items: bool
    note: str
    publishes: bool
    rank: int
    schedules: bool
    starts_research: bool
    suggests: bool
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableDisabled(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    advances_production: bool
    creates_campaign_drafts: bool
    creates_plan_items: bool
    note: str
    publishes: bool
    rank: int
    schedules: bool
    starts_research: bool
    suggests: bool
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableRecommend(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    advances_production: bool
    creates_campaign_drafts: bool
    creates_plan_items: bool
    note: str
    publishes: bool
    rank: int
    schedules: bool
    starts_research: bool
    suggests: bool
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7Table(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    APPROVAL: GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableApproval
    AUTONOMOUS: GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableAutonomous
    DISABLED: GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableDisabled
    RECOMMEND: GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7TableRecommend
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    actions: list[str]
    modes: list[str]
    note: str
    publishes: bool
    table: GetApiV1WorkspacesWorkspacePlannerPolicycf2d07e7Table
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentId9800c97dStrategy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    angle: str
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentId9800c97dVariantsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    hook: str
    id: str
    label: str
    metadata: dict[str, JsonValue]
    predicted_score: None
    script: str
    selected: bool
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentId9800c97d(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: None
    created_at: str
    cycle_id: None
    error: str
    id: str
    research: dict[str, JsonValue]
    status: str
    strategy: GetApiV1WorkspacesWorkspaceContentContentId9800c97dStrategy
    tags: list[str]
    topic: str
    variants: list[GetApiV1WorkspacesWorkspaceContentContentId9800c97dVariantsRow]
    variants_count: int
    video: None
    # 13 of 13 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdTimelinef228121aItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    at: str
    detail: str
    kind: str
    label: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdTimelinef228121a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceContentContentIdTimelinef228121aItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdLineage62759a56Self(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    campaign_id: None
    derivation_type: None
    id: str
    lineage_version: int
    status: str
    topic: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdLineage62759a56(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ancestors: list[JsonValue]
    children: list[JsonValue]
    root_id: str
    self: GetApiV1WorkspacesWorkspaceContentContentIdLineage62759a56Self
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5Content(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created_at: str
    error: str
    id: str
    status: str
    topic: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5PublishedPostsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    is_mock: bool
    metrics: None
    platform: str
    remote_post_id: str
    remote_url: str
    title: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5Strategy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    angle: str
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5VariantsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    hook: str
    id: str
    label: str
    metadata: dict[str, JsonValue]
    predicted_score: None
    script: str
    selected: bool
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5VideosRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    duration_seconds: None
    engine: str
    error: str
    id: str
    params: dict[str, JsonValue]
    resolution: str
    status: str
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    content: GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5Content
    decision_why: None
    event_trail: list[JsonValue]
    published_posts: list[GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5PublishedPostsRow]
    publishing_jobs: list[JsonValue]
    quality_checks: list[JsonValue]
    research: dict[str, JsonValue]
    strategy: GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5Strategy
    variants: list[GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5VariantsRow]
    videos: list[GetApiV1WorkspacesWorkspaceContentContentIdAudit2a4a6bc5VideosRow]
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsdb0db763ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    configured: bool
    hint: str
    key: str
    label: str
    masked: str | None
    secret: bool
    source: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsdb0db763(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceConnectionsdb0db763ItemsRow]
    # 1 of 1 fields were present in every observed state


class PatchApiV1WorkspacesWorkspace385c1bbcSettings(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    automation: str
    music: dict[str, JsonValue] | None = None
    safety: dict[str, JsonValue] | None = None
    scoring_weights: dict[str, JsonValue]
    # 2 of 4 fields were present in every observed state


class PatchApiV1WorkspacesWorkspace385c1bbc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_voice: str
    created_at: str
    id: str
    language: str
    name: str
    niche: str
    settings: PatchApiV1WorkspacesWorkspace385c1bbcSettings
    slug: str
    timezone: str
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceApiKeys8e72153a(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    api_key: str
    created_at: str
    id: str
    last_used_at: None
    name: str
    prefix: str
    revoked: bool
    role: str
    # 8 of 8 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceApiKeysKeyIdRevoke702743ca(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    revoked: bool
    # 1 of 1 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceSafety075613b7Safety(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_budget_usd: float
    max_concurrent_renders: int
    max_consecutive_failures: int
    max_render_attempts: int
    max_uploads_per_hour: int
    max_videos_per_day: int
    min_qc_score: int
    monthly_budget_usd: float
    per_video_budget_usd: float
    produce_score_threshold: float
    require_approval_before_publish: bool
    require_human_review_risk_above: float
    similarity_threshold: float
    # 13 of 13 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceSafety075613b7(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    safety: PutApiV1WorkspacesWorkspaceSafety075613b7Safety
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspacePublishingAccountsa7b81ca8(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    id: str
    # 1 of 1 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceConnectionsVideoEngine95a4461bDefaults(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    subtitles: bool
    voice: str
    # 3 of 3 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceConnectionsVideoEngine95a4461bSources(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    base_url: str
    timeout: str
    # 2 of 2 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceConnectionsVideoEngine95a4461b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    base_url: str
    capabilities: list[str]
    concurrency_note: str
    defaults: PutApiV1WorkspacesWorkspaceConnectionsVideoEngine95a4461bDefaults
    engine: str
    healthy: bool
    sources: PutApiV1WorkspacesWorkspaceConnectionsVideoEngine95a4461bSources
    timeout_seconds: int
    version: str
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceConnectionsTestLlm4f0e4bc5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    mode: str
    ok: bool
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceConnectionsTestPublishinge1e5edd1(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    connected_accounts: dict[str, JsonValue]
    detail: str
    ok: bool
    relay_configured: bool
    relay_email: None
    relay_plan: None
    relay_valid: None
    # 7 of 7 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceMusicPolicy7b581e3e(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_disabled: bool
    configured: bool
    forbidden_genres: list[JsonValue]
    generate: bool
    note: str
    prefs: dict[str, JsonValue]
    prefs_keys: list[str]
    provider_key: str
    raw_settings: dict[str, JsonValue]
    reason: str
    settings_key: str
    workspace_id: str
    # 12 of 12 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceRetentionbc3249c7(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audit_enforced: bool
    audit_retention_days: None
    export_retention_days: None
    id: str
    render_retention_days: None
    temp_asset_retention_days: None
    updated_at: str
    updated_by: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceNotificationsNotificationIdRead4d323daa(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    unread: int
    updated: int
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceNotificationsReadAll1052575b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    unread: int
    updated: int
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceTelegramPairingCodeb7eb21f7(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    code: str
    expires_in_seconds: int
    # 2 of 2 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspaceTelegramLinksLinkId70348ab9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    unlinked: bool
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceTelegramLinksLinkIdToggle29f8831f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    active: bool
    id: str
    # 2 of 2 fields were present in every observed state


class PutApiV1WorkspacesWorkspaceConnections9796f242(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    configured: bool
    key: str
    masked: None
    source: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceWebhooksa8c8cec4(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    active: bool
    created_at: str
    events: list[str]
    id: str
    secret: str
    url: str
    # 6 of 6 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspaceWebhooksSubIda8b84071(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: bool
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceWebhooksSubIdTeste37bdb98(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    enqueued: bool
    job_id: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceKnowledgeSourcesConnectorIdSyncda50649f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    job_id: str
    queued: bool
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceKnowledgeSourcesConnectorIdDisconnect6ac9aa16(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    blurb: str
    config: dict[str, JsonValue]
    doc_count: int
    enabled: bool
    has_credentials: bool
    id: str
    implemented: bool
    kind: str
    last_cursor: str
    last_error: str
    last_sync_at: None
    name: str
    requires_credentials: bool
    status: str
    title: str
    unavailable_reason: str
    # 16 of 16 fields were present in every observed state


class DeleteApiV1WorkspacesWorkspaceTrendSourcesSourceId6be20a93(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    deleted: bool
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspace6186f2a0Settings(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    automation: str
    music: dict[str, JsonValue]
    safety: dict[str, JsonValue]
    scoring_weights: dict[str, JsonValue]
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspace6186f2a0(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_voice: str
    created_at: str
    id: str
    language: str
    name: str
    niche: str
    settings: GetApiV1WorkspacesWorkspace6186f2a0Settings
    slug: str
    timezone: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMembers3f79fc0fItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    email: str
    role: str
    user_id: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMembers3f79fc0f(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceMembers3f79fc0fItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceSafety48187125Safety(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    daily_budget_usd: float
    max_concurrent_renders: int
    max_consecutive_failures: int
    max_render_attempts: int
    max_uploads_per_hour: int
    max_videos_per_day: int
    min_qc_score: int
    monthly_budget_usd: float
    per_video_budget_usd: float
    produce_score_threshold: float
    require_approval_before_publish: bool
    require_human_review_risk_above: float
    similarity_threshold: float
    # 13 of 13 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceSafety48187125(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    safety: GetApiV1WorkspacesWorkspaceSafety48187125Safety
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsContinuity(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsControl(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsCostEfficiency(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsLatency(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsOutputQuality(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsReliability(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsTaskFit(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    continuity: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsContinuity
    control: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsControl
    cost_efficiency: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsCostEfficiency
    latency: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsLatency
    output_quality: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsOutputQuality
    reliability: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsReliability
    task_fit: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDimsTaskFit
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    capability: str
    dims: GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRowDims
    explanation: str
    provider: str
    weighted: float
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616VoicesRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    gender: str
    id: str
    locale: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsTts51410616(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    error: None
    healthy: bool
    is_mock: bool
    provider: str
    ranked: list[GetApiV1WorkspacesWorkspaceConnectionsTts51410616RankedRow]
    voices: list[GetApiV1WorkspacesWorkspaceConnectionsTts51410616VoicesRow]
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsContinuity(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsControl(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsCostEfficiency(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsLatency(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsOutputQuality(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsReliability(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsTaskFit(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    reason: str
    score: float
    weight: float
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    continuity: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsContinuity
    control: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsControl
    cost_efficiency: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsCostEfficiency
    latency: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsLatency
    output_quality: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsOutputQuality
    reliability: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsReliability
    task_fit: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDimsTaskFit
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    capability: str
    dims: GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRowDims
    explanation: str
    provider: str
    weighted: float
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    error: str
    healthy: bool
    is_mock: bool
    provider: str
    ranked: list[GetApiV1WorkspacesWorkspaceConnectionsImagesffb7a53bRankedRow]
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsVideoEngined76a8f25Defaults(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    subtitles: bool
    voice: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsVideoEngined76a8f25Sources(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    base_url: str
    timeout: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceConnectionsVideoEngined76a8f25(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    base_url: str
    capabilities: list[str]
    concurrency_note: str
    defaults: GetApiV1WorkspacesWorkspaceConnectionsVideoEngined76a8f25Defaults
    engine: str
    healthy: bool
    sources: GetApiV1WorkspacesWorkspaceConnectionsVideoEngined76a8f25Sources
    timeout_seconds: int
    version: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMusicProviders4b01edccItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: bool
    detail: str
    key: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceMusicProviders4b01edcc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    available: list[JsonValue]
    items: list[GetApiV1WorkspacesWorkspaceMusicProviders4b01edccItemsRow]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceNotifications5ff5e9f1ItemsRowPayload(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    observed: bool
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceNotifications5ff5e9f1ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created_at: str
    id: str
    kind: str
    payload: GetApiV1WorkspacesWorkspaceNotifications5ff5e9f1ItemsRowPayload
    read: bool
    read_at: str
    user_id: str
    workspace_id: str
    # 8 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceNotifications5ff5e9f1(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    count: int
    items: list[GetApiV1WorkspacesWorkspaceNotifications5ff5e9f1ItemsRow]
    limit: int
    unread: int
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTelegramStatus2f551ed5LinksRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    active: bool
    chat_id: str
    chat_title: str
    id: str
    linked_at: str
    settings: dict[str, JsonValue]
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTelegramStatus2f551ed5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    bot_configured: bool
    linked: bool
    links: list[GetApiV1WorkspacesWorkspaceTelegramStatus2f551ed5LinksRow]
    token_source: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceWebhooksbb2921dbItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    active: bool
    created_at: str
    events: list[str]
    id: str
    url: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceWebhooksbb2921db(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    events: list[str]
    items: list[GetApiV1WorkspacesWorkspaceWebhooksbb2921dbItemsRow]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTrendSourcesc7c5a5ce(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[JsonValue]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceApiKeys31ac9dd2ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    created_at: str
    id: str
    last_used_at: None
    name: str
    prefix: str
    revoked: bool
    role: str
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceApiKeys31ac9dd2(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceApiKeys31ac9dd2ItemsRow]
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRowClipsRowSource(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_id: str | None = None
    ref: str | None = None
    # 0 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRowClipsRowTextObject(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    content: str | None = None
    size: int | None = None
    # 0 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRowClipsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    duration: float
    effects: list[JsonValue] | None = None
    fade_in: float | None = None
    fade_out: float | None = None
    id: str
    name: str
    source: GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRowClipsRowSource | None = None
    source_start: float | None = None
    speed: float | None = None
    start: float
    text: GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRowClipsRowTextObject | str | None = None
    transform: dict[str, JsonValue] | None = None
    transition_in: str | None = None
    transition_out: str | None = None
    volume: float | None = None
    # 4 of 15 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    clips: list[GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRowClipsRow]
    id: str | None = None
    kind: str
    name: str | None = None
    # 2 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    content_item_id: str | None
    created_at: str
    duration_seconds: float
    fps: float
    id: str
    name: str
    parent_timeline_id: None
    tracks: list[GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRowTracksRow]
    updated_at: str
    version: int
    video_id: None
    workspace_id: str
    # 13 of 13 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelines905be746(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceTimelines905be746ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelinesTimelineId2c7a2ebcTracksRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    clips: list[JsonValue]
    id: str
    kind: str
    name: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelinesTimelineId2c7a2ebc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    content_item_id: None
    created_at: str
    duration_seconds: float
    fps: float
    id: str
    name: str
    parent_timeline_id: None
    tracks: list[GetApiV1WorkspacesWorkspaceTimelinesTimelineId2c7a2ebcTracksRow]
    updated_at: str
    version: int
    video_id: None
    workspace_id: str
    # 13 of 13 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceTimelinesTimelineIdScenes09206c34(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    scenes: list[JsonValue]
    total: int
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectBrief(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    topic: str
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrandBrandProvenance(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_avatars: str
    approved_logos: str
    approved_voices: str
    brand_colors: str
    caption_style: str
    claims_policy: str
    cta_style: str
    fonts: str
    forbidden_phrases: str
    logo_safe_zone: str
    pronunciation_rules: str
    required_disclaimers: str
    thumbnail_style: str
    tone: str
    vocabulary: str
    watermark: str
    # 16 of 16 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    brand_degraded: str
    brand_provenance: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrandBrandProvenance
    brand_template: str
    effective_config_id: str
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrandQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    effective_config_id: str
    status: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrollPlanRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    index: int
    prompt: str
    query: str
    source: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageCta(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    spoken: bool
    text: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageMusicPolicy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_disabled: bool
    forbidden_genres: list[JsonValue]
    generate: bool
    instrumental: None
    note: str
    prefs: dict[str, JsonValue]
    provider_key: str
    reason: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageMusic(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    generated: bool
    policy: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageMusicPolicy
    reason: str
    track: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    resolved: list[JsonValue]
    unresolved: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageStrategyBrandBrandProvenance(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_avatars: str
    approved_logos: str
    approved_voices: str
    brand_colors: str
    caption_style: str
    claims_policy: str
    cta_style: str
    fonts: str
    forbidden_phrases: str
    logo_safe_zone: str
    pronunciation_rules: str
    required_disclaimers: str
    thumbnail_style: str
    tone: str
    vocabulary: str
    watermark: str
    # 16 of 16 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageStrategyBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    brand_degraded: str
    brand_provenance: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageStrategyBrandBrandProvenance
    brand_template: str
    effective_config_id: str
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageStrategy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    brand: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageStrategyBrand
    broll_density: str
    caption_preset: str
    cta: str
    duration_seconds: int
    format: str
    hook_type: str
    music_preference: str
    pacing: str
    # 10 of 10 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageVoiceRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_id: str
    duration: float
    words: int
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineage(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audience: str
    brand: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrand
    brand_qc: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrandQc
    broll_plan: list[PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageBrollPlanRow]
    content_item_id: str
    cta: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageCta
    generated_version: int
    hook: str
    manifest_hash: str
    music: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageMusic
    preset: str
    product_assets: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageProductAssets
    script: str
    script_source: str
    strategy: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageStrategy
    topic: str
    voice: list[PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineageVoiceRow]
    # 17 of 17 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksApprovedColors(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksAvatarApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksCaptionStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksCtaStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksFonts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksForbiddenPhrases(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksLogoUse(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksRequiredDisclaimers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksTerminology(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksThumbnailConventions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksVoiceApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_colors: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksApprovedColors
    avatar_approval: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksAvatarApproval
    caption_style: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksCaptionStyle
    cta_style: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksCtaStyle
    fonts: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksFonts
    forbidden_phrases: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksForbiddenPhrases
    logo_use: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksLogoUse
    required_disclaimers: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksRequiredDisclaimers
    terminology: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksTerminology
    thumbnail_conventions: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksThumbnailConventions
    voice_approval: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksVoiceApproval
    watermark: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecksWatermark
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    checks: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrandChecks
    detail: str
    effective_config_id: str
    status: str
    verifier_status: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksCtaPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksHookPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksMusicDecision(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksTimelineComplete(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksUnsupportedClaims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksBrand
    cta_present: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksCtaPresent
    hook_present: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksHookPresent
    music_decision: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksMusicDecision
    product_assets: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksProductAssets
    timeline_complete: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksTimelineComplete
    unsupported_claims: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecksUnsupportedClaims
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQcChecks
    preset: str
    report_type: str
    status: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeProject(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brief: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectBrief
    created_at: str
    id: str
    lineage: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectLineage
    preset: str
    qc: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProjectQc
    render_asset_ref: str
    status: str
    timeline_id: str
    updated_at: str
    workspace_id: str
    # 11 of 11 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksApprovedColors(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksAvatarApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksCaptionStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksCtaStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksFonts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksForbiddenPhrases(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksLogoUse(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksRequiredDisclaimers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksTerminology(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksThumbnailConventions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksVoiceApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_colors: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksApprovedColors
    avatar_approval: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksAvatarApproval
    caption_style: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksCaptionStyle
    cta_style: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksCtaStyle
    fonts: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksFonts
    forbidden_phrases: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksForbiddenPhrases
    logo_use: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksLogoUse
    required_disclaimers: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksRequiredDisclaimers
    terminology: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksTerminology
    thumbnail_conventions: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksThumbnailConventions
    voice_approval: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksVoiceApproval
    watermark: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecksWatermark
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    checks: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrandChecks
    detail: str
    effective_config_id: str
    status: str
    verifier_status: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksCtaPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksHookPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksMusicDecision(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksTimelineComplete(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksUnsupportedClaims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksBrand
    cta_present: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksCtaPresent
    hook_present: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksHookPresent
    music_decision: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksMusicDecision
    product_assets: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksProductAssets
    timeline_complete: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksTimelineComplete
    unsupported_claims: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecksUnsupportedClaims
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276aeQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQcChecks
    preset: str
    report_type: str
    status: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjects494276ae(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    project: PostApiV1WorkspacesWorkspaceUgcProjects494276aeProject
    qc: PostApiV1WorkspacesWorkspaceUgcProjects494276aeQc
    ran: bool
    script: str
    timeline_id: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjectsProjectIde2dd6a3bProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    resolved: list[JsonValue]
    unresolved: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjectsProjectIde2dd6a3b(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brief: dict[str, JsonValue]
    created_at: str
    id: str
    lineage: dict[str, JsonValue]
    open_in_editor: bool
    preset: str
    product_assets: GetApiV1WorkspacesWorkspaceUgcProjectsProjectIde2dd6a3bProductAssets
    qc: dict[str, JsonValue]
    render_asset_ref: str
    status: str
    timeline_id: str
    updated_at: str
    workspace_id: str
    # 13 of 13 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectBrief(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cta: str
    topic: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectLineageBrandQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    effective_config_id: str
    status: str
    # 3 of 3 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectLineage(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_qc: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectLineageBrandQc
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksApprovedColors(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksAvatarApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksCaptionStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksCtaStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksFonts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksForbiddenPhrases(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksLogoUse(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksRequiredDisclaimers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksTerminology(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksThumbnailConventions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksVoiceApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_colors: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksApprovedColors
    avatar_approval: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksAvatarApproval
    caption_style: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksCaptionStyle
    cta_style: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksCtaStyle
    fonts: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksFonts
    forbidden_phrases: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksForbiddenPhrases
    logo_use: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksLogoUse
    required_disclaimers: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksRequiredDisclaimers
    terminology: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksTerminology
    thumbnail_conventions: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksThumbnailConventions
    voice_approval: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksVoiceApproval
    watermark: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecksWatermark
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    checks: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrandChecks
    detail: str
    effective_config_id: str
    status: str
    verifier_status: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksCtaPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksHookPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksRender(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksTimelineComplete(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksUnsupportedClaims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksBrand
    cta_present: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksCtaPresent
    hook_present: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksHookPresent
    product_assets: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksProductAssets
    render: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksRender
    timeline_complete: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksTimelineComplete
    unsupported_claims: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecksUnsupportedClaims
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQcChecks
    preset: str
    report_type: str
    status: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9Project(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brief: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectBrief
    created_at: str
    id: str
    lineage: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectLineage
    preset: str
    qc: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9ProjectQc
    render_asset_ref: str
    status: str
    timeline_id: str
    updated_at: str
    workspace_id: str
    # 11 of 11 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksApprovedColors(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksAvatarApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksCaptionStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksCtaStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksFonts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksForbiddenPhrases(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksLogoUse(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksRequiredDisclaimers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksTerminology(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksThumbnailConventions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksVoiceApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_colors: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksApprovedColors
    avatar_approval: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksAvatarApproval
    caption_style: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksCaptionStyle
    cta_style: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksCtaStyle
    fonts: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksFonts
    forbidden_phrases: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksForbiddenPhrases
    logo_use: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksLogoUse
    required_disclaimers: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksRequiredDisclaimers
    terminology: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksTerminology
    thumbnail_conventions: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksThumbnailConventions
    voice_approval: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksVoiceApproval
    watermark: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecksWatermark
    # 12 of 12 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    checks: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrandChecks
    detail: str
    effective_config_id: str
    status: str
    verifier_status: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksCtaPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksHookPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksRender(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksTimelineComplete(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksUnsupportedClaims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksBrand
    cta_present: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksCtaPresent
    hook_present: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksHookPresent
    product_assets: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksProductAssets
    render: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksRender
    timeline_complete: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksTimelineComplete
    unsupported_claims: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecksUnsupportedClaims
    # 7 of 7 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQcChecks
    preset: str
    report_type: str
    status: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9Render(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_id: str
    duration_seconds: float
    qc: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9RenderQc
    storage_key: str
    timeline_id: str
    # 5 of 5 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    project: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9Project
    render: PostApiV1WorkspacesWorkspaceUgcProjectsProjectIdRender795c2dd9Render
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5CapabilitiesLicenseNotes(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    wavlip: str
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5Capabilities(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audio_driven: bool
    backend: str
    consent_required: bool
    consent_states: list[str]
    detail: str
    expression_presets: list[str]
    framings: list[str]
    inputs: list[Literal["image", "video"]]
    lanes: list[str]
    license_notes: GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5CapabilitiesLicenseNotes
    motion_presets: list[str]
    outputs: list[str]
    ready: bool
    renders: bool
    # 14 of 14 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5HealthLanes(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    mock: bool
    sadtalker: bool
    server: bool
    wavlip: bool
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5HealthLicenseNotes(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    wavlip: str
    # 1 of 1 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5Health(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    backend: str
    detail: str
    ffmpeg: bool
    lanes: GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5HealthLanes
    license_notes: GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5HealthLicenseNotes
    provider: str
    ready: bool
    # 7 of 7 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    capabilities: GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5Capabilities
    consent_required: bool
    consent_states: list[str]
    detail: str
    health: GetApiV1WorkspacesWorkspaceAvatarsHealth10f237b5Health
    provider: str
    ready: bool
    workspace_id: str
    # 8 of 8 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksAvDuration(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksBlackOutput(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksConsent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksFacePresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksLipSync(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksOutputFile(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    av_duration: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksAvDuration
    black_output: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksBlackOutput
    consent: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksConsent
    face_present: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksFacePresent
    lip_sync: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksLipSync
    output_file: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecksOutputFile
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcFlags(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    lip_sync_failure: bool
    # 1 of 1 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcChecks
    flags: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQcFlags
    report_type: str
    status: str
    # 4 of 4 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520c(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_id: str
    backend: str
    consent_state: str
    content_item_id: str
    duration: float
    is_mock: bool
    output_id: str
    provider: str
    qc: PostApiV1WorkspacesWorkspaceAvatarsRenderb5eb520cQc
    storage_key: str
    timeline_id: str
    # 11 of 11 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatars09f549bfConsent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    authorization_evidence: dict[str, JsonValue]
    granted_at: str
    granted_by: str
    source: str
    state: str
    statement: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatars09f549bfProfile(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    background: str
    brand_association: str
    expression_preset: str
    framing: str
    language: str
    motion_preset: str
    name: str
    provider: str
    source_asset_ref: str
    status: str
    voice_ref: str
    # 11 of 11 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatars09f549bf(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    consent: PostApiV1WorkspacesWorkspaceAvatars09f549bfConsent
    consent_state: str
    created_at: str
    id: str
    name: str
    profile: PostApiV1WorkspacesWorkspaceAvatars09f549bfProfile
    provider: str
    source_asset_ref: str
    status: str
    workspace_id: str
    # 10 of 10 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166ConsentAuthorizationEvidence(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    kind: str
    reference: str
    # 2 of 2 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166Consent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    authorization_evidence: PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166ConsentAuthorizationEvidence
    granted_at: str
    granted_by: str
    source: str
    state: str
    statement: str
    # 6 of 6 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166Profile(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    background: str
    brand_association: str
    expression_preset: str
    framing: str
    language: str
    motion_preset: str
    name: str
    provider: str
    source_asset_ref: str
    status: str
    voice_ref: str
    # 11 of 11 fields were present in every observed state


class PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    consent: PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166Consent
    consent_state: str
    created_at: str
    id: str
    name: str
    profile: PostApiV1WorkspacesWorkspaceAvatarsProfileIdAuthorize9f6dd166Profile
    provider: str
    source_asset_ref: str
    status: str
    workspace_id: str
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcPresetsdd4b0f63ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    duration_seconds: int
    format: str
    hook_type: str
    key: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcPresetsdd4b0f63(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceUgcPresetsdd4b0f63ItemsRow]
    total: int
    workspace_id: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowBrief(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    cta: str | None = None
    topic: str | None = None
    # 0 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrandBrandProvenance(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_avatars: str
    approved_logos: str
    approved_voices: str
    brand_colors: str
    caption_style: str
    claims_policy: str
    cta_style: str
    fonts: str
    forbidden_phrases: str
    logo_safe_zone: str
    pronunciation_rules: str
    required_disclaimers: str
    thumbnail_style: str
    tone: str
    vocabulary: str
    watermark: str
    # 16 of 16 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    brand_degraded: str
    brand_provenance: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrandBrandProvenance
    brand_template: str
    effective_config_id: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrandQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    effective_config_id: str
    status: str
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrollPlanRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    index: int
    prompt: str
    query: str
    source: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageCta(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    spoken: bool
    text: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageMusicPolicy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand_disabled: bool
    forbidden_genres: list[JsonValue]
    generate: bool
    instrumental: None
    note: str
    prefs: dict[str, JsonValue]
    provider_key: str
    reason: str
    workspace_id: str
    # 9 of 9 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageMusic(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    generated: bool
    policy: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageMusicPolicy
    reason: str
    track: str
    # 4 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    resolved: list[JsonValue]
    unresolved: list[JsonValue]
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageStrategyBrandBrandProvenance(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_avatars: str
    approved_logos: str
    approved_voices: str
    brand_colors: str
    caption_style: str
    claims_policy: str
    cta_style: str
    fonts: str
    forbidden_phrases: str
    logo_safe_zone: str
    pronunciation_rules: str
    required_disclaimers: str
    thumbnail_style: str
    tone: str
    vocabulary: str
    watermark: str
    # 16 of 16 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageStrategyBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    brand_degraded: str
    brand_provenance: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageStrategyBrandBrandProvenance
    brand_template: str
    effective_config_id: str
    # 5 of 5 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageStrategy(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    aspect_ratio: str
    brand: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageStrategyBrand
    broll_density: str
    caption_preset: str
    cta: str
    duration_seconds: int
    format: str
    hook_type: str
    music_preference: str
    pacing: str
    # 10 of 10 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageVoiceRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    asset_id: str
    duration: float
    words: int
    # 3 of 3 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineage(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    audience: str | None = None
    brand: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrand | None = None
    brand_qc: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrandQc | None = None
    broll_plan: list[GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageBrollPlanRow] | None = None
    content_item_id: str | None = None
    cta: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageCta | None = None
    generated_version: int | None = None
    hook: str | None = None
    manifest_hash: str | None = None
    music: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageMusic | None = None
    preset: str | None = None
    product_assets: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageProductAssets | None = None
    script: str | None = None
    script_source: str | None = None
    strategy: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageStrategy | None = None
    topic: str | None = None
    voice: list[GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineageVoiceRow] | None = None
    # 0 of 17 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksApprovedColors(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksAvatarApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksCaptionStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksCtaStyle(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksFonts(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksForbiddenPhrases(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksLogoUse(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksRequiredDisclaimers(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksTerminology(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksThumbnailConventions(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksVoiceApproval(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksWatermark(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    approved_colors: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksApprovedColors
    avatar_approval: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksAvatarApproval
    caption_style: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksCaptionStyle
    cta_style: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksCtaStyle
    fonts: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksFonts
    forbidden_phrases: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksForbiddenPhrases
    logo_use: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksLogoUse
    required_disclaimers: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksRequiredDisclaimers
    terminology: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksTerminology
    thumbnail_conventions: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksThumbnailConventions
    voice_approval: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksVoiceApproval
    watermark: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecksWatermark
    # 12 of 12 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrand(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    applied_brand: bool
    checks: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrandChecks
    detail: str
    effective_config_id: str
    status: str
    verifier_status: str
    # 6 of 6 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksCtaPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksHookPresent(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksMusicDecision(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksProductAssets(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksRender(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksTimelineComplete(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksUnsupportedClaims(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    detail: str
    status: str
    # 2 of 2 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecks(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brand: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksBrand
    cta_present: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksCtaPresent
    hook_present: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksHookPresent
    music_decision: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksMusicDecision | None = None
    product_assets: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksProductAssets
    render: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksRender | None = None
    timeline_complete: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksTimelineComplete
    unsupported_claims: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecksUnsupportedClaims
    # 6 of 8 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQc(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    checks: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQcChecks | None = None
    preset: str | None = None
    report_type: str | None = None
    status: str | None = None
    # 0 of 4 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRow(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    brief: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowBrief
    created_at: str
    id: str
    lineage: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowLineage
    preset: str
    qc: GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRowQc
    render_asset_ref: str
    status: str
    timeline_id: str
    updated_at: str
    workspace_id: str
    # 11 of 11 fields were present in every observed state


class GetApiV1WorkspacesWorkspaceUgcProjects8e87e051(_ObservedBase):
    """Observed response contract."""

    model_config = ConfigDict(extra="allow")

    items: list[GetApiV1WorkspacesWorkspaceUgcProjects8e87e051ItemsRow]
    total: int
    # 2 of 2 fields were present in every observed state

