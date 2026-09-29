"""All ORM models re-exported for migration auto-creation."""

from app.db import Base
from app.models.assets import MediaAsset, Scene
from app.models.avatar import AvatarOutputRow, AvatarProfileRow
from app.models.base import ContentStatus, can_transition
from app.models.brand import (
    Brand,
    BrandAsset,
    BrandDNARow,
    BrandEffectiveConfig,
    BrandOverride,
    BrandPreset,
)
from app.models.campaign import CampaignPlan, PlatformVariant, PublishingPlan
from app.models.capabilities import CapabilityPermission, ToolCallAudit
from app.models.community import (
    CommunityAction,
    CommunityInsight,
    CommunityOpportunity,
    CommunitySyncState,
    Conversation,
    SocialInteraction,
)
from app.models.content import (
    Campaign,
    ContentItem,
    Opportunity,
    PostMetric,
    PublishedPost,
    PublishingJob,
    QualityCheck,
    ScheduleEntry,
    TrendSource,
    Video,
    VideoVariant,
)
from app.models.creative import CreativeCommandRow
from app.models.dubbing import DubbingPlanRow
from app.models.identity import (
    ApiCredential,
    AuditLog,
    RefreshToken,
    SocialAccount,
    User,
    WebhookSubscription,
    Workspace,
    WorkspaceApiKey,
    WorkspaceMember,
)
from app.models.integrations import TelegramLink
from app.models.intelligence import BrowserRun, DecisionRecordRow, EvidenceRecord
from app.models.knowledge import (
    KnowledgeEdge,
    KnowledgeEvidence,
    KnowledgeMemory,
    KnowledgeNode,
    SourceConnector,
    SourceDocument,
)
from app.models.lipsync import LipSyncJob
from app.models.localization import GlossaryTerm, LocalizationQCReport, LocalizedContent
from app.models.longform import LongFormChapter, LongFormProject
from app.models.ops import (
    AgentConfig,
    AgentRun,
    AutopilotRun,
    CostEntry,
    Cycle,
    EventLog,
    Job,
    LearningPattern,
    MemoryRecord,
    SystemLog,
)
from app.models.performance import (
    CreativeFeature,
    PerformanceObservation,
    RetentionPoint,
)
from app.models.timeline import ContentTimeline
from app.models.ugc import UgcProjectRow

__all__ = [
    "AgentConfig",
    "AgentRun",
    "ApiCredential",
    "AuditLog",
    "AutopilotRun",
    "AvatarOutputRow",
    "AvatarProfileRow",
    "Base",
    "Brand",
    "BrandAsset",
    "BrandDNARow",
    "BrandEffectiveConfig",
    "BrandOverride",
    "BrandPreset",
    "BrowserRun",
    "Campaign",
    "CampaignPlan",
    "CapabilityPermission",
    "CommunityAction",
    "CommunityInsight",
    "CommunityOpportunity",
    "CommunitySyncState",
    "ContentItem",
    "ContentStatus",
    "ContentTimeline",
    "Conversation",
    "CostEntry",
    "CreativeCommandRow",
    "CreativeFeature",
    "Cycle",
    "DecisionRecordRow",
    "DubbingPlanRow",
    "EventLog",
    "EvidenceRecord",
    "GlossaryTerm",
    "Job",
    "KnowledgeEdge",
    "KnowledgeEvidence",
    "KnowledgeMemory",
    "KnowledgeNode",
    "LearningPattern",
    "LipSyncJob",
    "LocalizationQCReport",
    "LocalizedContent",
    "LongFormChapter",
    "LongFormProject",
    "MediaAsset",
    "MemoryRecord",
    "Opportunity",
    "PerformanceObservation",
    "PlatformVariant",
    "PostMetric",
    "PublishedPost",
    "PublishingJob",
    "PublishingPlan",
    "QualityCheck",
    "RefreshToken",
    "RetentionPoint",
    "ScheduleEntry",
    "Scene",
    "SocialAccount",
    "SocialInteraction",
    "SourceConnector",
    "SourceDocument",
    "SystemLog",
    "TelegramLink",
    "ToolCallAudit",
    "TrendSource",
    "UgcProjectRow",
    "User",
    "Video",
    "VideoVariant",
    "WebhookSubscription",
    "Workspace",
    "WorkspaceApiKey",
    "WorkspaceMember",
    "can_transition",
]
