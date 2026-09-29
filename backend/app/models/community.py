"""Community / social inbox ORM models (Work 09 — cross-lane contract).

The unified inbox ingests platform interactions (comments, mentions,
messages, reviews) against connected SocialAccounts, links them to the
published posts (and through them to variants/content/campaigns), and
records every agent/human action with full audit evidence.

Tables (mirrored idempotently in migrations/versions/0026_community.py):
  * social_interactions      -- one ingested interaction (idempotent per
                                workspace+platform+account+remote_id)
  * conversations            -- provider thread aggregation per account
  * community_actions        -- every outgoing/queued action (draft, reply,
                                escalation, moderation) with audit evidence
  * community_opportunities  -- lead/partnership/content-request signals
                                with evidence counts
  * community_insights       -- aggregated audience questions/topics feeding
                                the content-opportunity loop
  * community_sync_state     -- per-account incremental sync cursors +
                                backoff bookkeeping

Design rules (Work 09 spec):
  * Idempotency: re-ingesting the same remote item is a no-op.
  * Isolation: every row carries workspace_id; queries filter by it.
  * Provenance: AI classifications store provider + confidence + evidence;
    never sensitive personal traits.
  * Proof: a reply is SENT only with provider receipt + remote reply id;
    fixture/mock receipts are flagged is_mock and never verify as live.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# social_interactions.kind — what the remote item IS
INTERACTION_KINDS = (
    "COMMENT",
    "MENTION",
    "REPLY",
    "MESSAGE",
    "QUESTION",
    "REVIEW",
)
# processing lifecycle (linear-ish; agents advance it)
INTERACTION_STATUSES = (
    "unread",
    "read",
    "classified",
    "drafted",
    "replied",
    "escalated",
    "ignored",
    "spam",
)
# moderation pipeline outcomes (deterministic + semantic, evidence stored)
MODERATION_STATES = ("pending", "allowed", "review", "hidden", "blocked")
# typed classification labels (multi-label allowed)
CLASSIFICATION_LABELS = (
    "QUESTION",
    "POSITIVE",
    "NEGATIVE",
    "FEEDBACK",
    "SUPPORT",
    "LEAD",
    "SPAM",
    "ABUSE",
    "COLLABORATION",
    "CONTENT_REQUEST",
    "OTHER",
)
PRIORITIES = ("low", "normal", "high", "urgent")
# community_actions.action_type — what the action DOES
ACTION_TYPES = (
    "REPLY",          # reply to an interaction (drafted or sent)
    "DRAFT",          # draft only (no send intent yet)
    "APPROVAL_REQUEST",  # queued for human approval
    "HIDE",           # hide comment where platform supports it
    "BLOCK",          # block/flag author where platform supports it
    "ESCALATE",       # surface to human operator
    "INSIGHT",        # community insight recorded
    "OPPORTUNITY",    # opportunity recorded
)
# autonomy mode under which the action was produced
AUTONOMY_MODES = ("DRAFT_ONLY", "LOW_RISK_AUTO", "APPROVAL_REQUIRED", "DISABLED")
ACTION_STATES = (
    "draft",
    "pending_approval",
    "approved",
    "rejected",
    "sent",
    "failed",
    "stale",
    "blocked",
)
# UI provenance badge: AI DRAFT | HUMAN EDITED | AUTO SENT
ACTION_ORIGINS = ("ai", "human_edited", "auto")
# moderation action verdicts (outgoing moderation decisions)
MODERATION_VERDICTS = ("ALLOW", "REVIEW", "HIDE_IF_SUPPORTED", "BLOCK_ACTION")
OPPORTUNITY_TYPES = (
    "product_question",
    "partnership",
    "sponsorship",
    "purchase_intent",
    "content_request",
    "repeated_request",
)
OPPORTUNITY_STATES = ("open", "converted", "dismissed")
CONFIDENCE_LEVELS = ("low", "medium", "high")
INSIGHT_STATES = ("new", "converted", "dismissed")


class SocialInteraction(Base, PKMixin, TimestampMixin):
    """One ingested platform interaction (comment/mention/message/...)."""

    __tablename__ = "social_interactions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "platform",
            "account_id",
            "remote_id",
            name="uq_interaction_remote",
        ),
        Index("ix_interaction_ws_created", "workspace_id", "created_at"),
        Index("ix_interaction_ws_status", "workspace_id", "status"),
        Index("ix_interaction_post", "published_post_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    # social_accounts.id (account isolation; not an FK so account deletion
    # never orphans-delete audit history)
    account_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    remote_id: Mapped[str] = mapped_column(String(200), nullable=False)

    kind: Mapped[str] = mapped_column(String(20), default="COMMENT")
    text: Mapped[str] = mapped_column(Text, default="")
    author_remote_id: Mapped[str] = mapped_column(String(200), default="")
    author_name: Mapped[str] = mapped_column(String(200), default="")
    parent_interaction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("social_interactions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    thread_id: Mapped[str] = mapped_column(String(200), default="", index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )

    # -- linkage: interaction -> publication -> variant -> content -> campaign
    # remote id of the POST the interaction belongs to (platform resource id)
    post_remote_id: Mapped[str] = mapped_column(String(200), default="", index=True)
    published_post_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    # denormalized at link time for fast analytics (nullable: unlinked items)
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # -- processing state
    status: Mapped[str] = mapped_column(String(20), default="unread", index=True)
    moderation_state: Mapped[str] = mapped_column(String(20), default="pending")
    sentiment: Mapped[str] = mapped_column(String(16), default="")
    intent: Mapped[str] = mapped_column(String(30), default="")
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    is_question: Mapped[bool] = mapped_column(Boolean, default=False)
    # AI classification provenance: [{label, provider, confidence, evidence}]
    classifications_json: Mapped[list] = mapped_column(JSON, default=list)
    # moderation provenance: [{verdict, rule, reason, evidence, provider}]
    moderation_json: Mapped[list] = mapped_column(JSON, default=list)

    remote_created_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )


class Conversation(Base, PKMixin, TimestampMixin):
    """Provider thread aggregated per account (one row per thread key)."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "account_id",
            "thread_key",
            name="uq_conversation_thread",
        ),
        Index("ix_conv_ws_last", "workspace_id", "last_interaction_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(30), default="")
    account_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    thread_key: Mapped[str] = mapped_column(String(200), default="")

    title: Mapped[str] = mapped_column(String(300), default="")
    participant_remote_id: Mapped[str] = mapped_column(String(200), default="")
    participant_name: Mapped[str] = mapped_column(String(200), default="")

    published_post_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    last_interaction_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|resolved|spam|blocked
    priority: Mapped[str] = mapped_column(String(10), default="normal")


class CommunityAction(Base, PKMixin, TimestampMixin):
    """Audit record for EVERY outgoing or queued community action.

    A reply counts as SENT only when state=sent AND provider_receipt_json is
    non-empty AND remote_reply_id is set AND account/platform match the
    interaction. Fixture receipts set is_mock=True and never verify as live.
    """

    __tablename__ = "community_actions"
    __table_args__ = (
        Index("ix_caction_ws_state", "workspace_id", "state"),
        Index("ix_caction_interaction", "interaction_id"),
        Index("ix_caction_ws_sent", "workspace_id", "sent_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[str] = mapped_column(
        ForeignKey("social_interactions.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    account_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    platform: Mapped[str] = mapped_column(String(30), default="")

    action_type: Mapped[str] = mapped_column(String(24), default="REPLY")
    mode: Mapped[str] = mapped_column(String(24), default="DRAFT_ONLY")
    state: Mapped[str] = mapped_column(String(20), default="draft")
    origin: Mapped[str] = mapped_column(String(20), default="ai")  # ai|human_edited|auto

    draft_text: Mapped[str] = mapped_column(Text, default="")
    final_text: Mapped[str] = mapped_column(Text, default="")  # exact sent text
    # Brand check outcome for the drafted text (Work 08 policy):
    # {status, forbidden_hits, required_disclaimers, tone, effective_config_id}
    brand_check_json: Mapped[dict] = mapped_column(JSON, default=dict)

    approval_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rejected_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # -- send proof (CompletionVerifier inputs)
    provider_receipt_json: Mapped[dict] = mapped_column(JSON, default=dict)
    remote_reply_id: Mapped[str] = mapped_column(String(200), default="")
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # double-send claim (policy.send_action): committed BEFORE the provider
    # call; stale claims expire after policy.SEND_CLAIM_TTL
    send_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CommunityOpportunity(Base, PKMixin, TimestampMixin):
    """Lead / partnership / content-request signal with evidence."""

    __tablename__ = "community_opportunities"
    __table_args__ = (
        Index("ix_copp_ws_state", "workspace_id", "state"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    opportunity_type: Mapped[str] = mapped_column(String(40), default="content_request")
    title: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[str] = mapped_column(Text, default="")

    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    source_interaction_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(10), default="low")
    state: Mapped[str] = mapped_column(String(20), default="open", index=True)

    # conversion into a future content idea/task (never invent contact details)
    converted_idea_json: Mapped[dict] = mapped_column(JSON, default=dict)
    converted_opportunity_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )


class CommunityInsight(Base, PKMixin, TimestampMixin):
    """Aggregated audience question/topic feeding the content loop.

    Repeat questions aggregate here (evidence_count grows). Single-source
    insights stay confidence=low and are labeled as such — never a trend.
    """

    __tablename__ = "community_insights"
    __table_args__ = (
        UniqueConstraint("workspace_id", "topic_key", name="uq_insight_topic"),
        Index("ix_cins_ws_state", "workspace_id", "state"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # normalized dedupe key (lowercased, hashed) — unique per workspace
    topic_key: Mapped[str] = mapped_column(String(64), default="")
    topic: Mapped[str] = mapped_column(String(300), default="")
    representative_text: Mapped[str] = mapped_column(Text, default="")

    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    source_interaction_ids: Mapped[list] = mapped_column(JSON, default=list)
    platforms: Mapped[list] = mapped_column(JSON, default=list)
    related_content_ids: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(10), default="low")
    state: Mapped[str] = mapped_column(String(20), default="new", index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CommunitySyncState(Base, PKMixin, TimestampMixin):
    """Per-account incremental sync cursor + backoff bookkeeping.

    cursors_json is keyed by resource ("comments" | "mentions" | "messages"),
    each value the provider's opaque pagination cursor ("" = fresh start).
    A single provider/account failure updates only its own row.
    """

    __tablename__ = "community_sync_state"
    __table_args__ = (
        UniqueConstraint("workspace_id", "account_id", name="uq_sync_account"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    platform: Mapped[str] = mapped_column(String(30), default="")

    cursors_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = [
    "ACTION_ORIGINS",
    "ACTION_STATES",
    "ACTION_TYPES",
    "AUTONOMY_MODES",
    "CLASSIFICATION_LABELS",
    "CONFIDENCE_LEVELS",
    "CommunityAction",
    "CommunityInsight",
    "CommunityOpportunity",
    "CommunitySyncState",
    "Conversation",
    "INSIGHT_STATES",
    "INTERACTION_KINDS",
    "INTERACTION_STATUSES",
    "MODERATION_STATES",
    "MODERATION_VERDICTS",
    "OPPORTUNITY_STATES",
    "OPPORTUNITY_TYPES",
    "PRIORITIES",
    "SocialInteraction",
]
