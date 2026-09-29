"""Upgrade 0026: community / unified social inbox (Work 09).

Tables (mirrors app/models/community.py):
  social_interactions, conversations, community_actions,
  community_opportunities, community_insights, community_sync_state.

Append-only and idempotent: CREATE TABLE IF NOT EXISTS + guarded indexes, so
it replays as a no-op even when the ORM already created the tables
(Base.metadata.create_all runs first in the runner). No existing table is
altered -- every Work 01-08 schema stays untouched.
"""

from __future__ import annotations


def upgrade(session) -> None:
    from sqlalchemy import text

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS social_interactions (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            platform VARCHAR(30) NOT NULL,
            account_id VARCHAR(36) NOT NULL DEFAULT '',
            remote_id VARCHAR(200) NOT NULL,
            kind VARCHAR(20) NOT NULL DEFAULT 'COMMENT',
            text TEXT NOT NULL DEFAULT '',
            author_remote_id VARCHAR(200) NOT NULL DEFAULT '',
            author_name VARCHAR(200) NOT NULL DEFAULT '',
            parent_interaction_id VARCHAR(36) REFERENCES social_interactions(id) ON DELETE SET NULL,
            thread_id VARCHAR(200) NOT NULL DEFAULT '',
            conversation_id VARCHAR(36),
            post_remote_id VARCHAR(200) NOT NULL DEFAULT '',
            published_post_id VARCHAR(36),
            content_item_id VARCHAR(36),
            campaign_id VARCHAR(36),
            status VARCHAR(20) NOT NULL DEFAULT 'unread',
            moderation_state VARCHAR(20) NOT NULL DEFAULT 'pending',
            sentiment VARCHAR(16) NOT NULL DEFAULT '',
            intent VARCHAR(30) NOT NULL DEFAULT '',
            priority VARCHAR(10) NOT NULL DEFAULT 'normal',
            is_question BOOLEAN NOT NULL DEFAULT 0,
            classifications_json JSON NOT NULL DEFAULT '[]',
            moderation_json JSON NOT NULL DEFAULT '[]',
            remote_created_at TIMESTAMP,
            CONSTRAINT uq_interaction_remote UNIQUE
                (workspace_id, platform, account_id, remote_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_workspace_id ON social_interactions (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_account_id ON social_interactions (account_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_parent_interaction_id ON social_interactions (parent_interaction_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_thread_id ON social_interactions (thread_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_conversation_id ON social_interactions (conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_post_remote_id ON social_interactions (post_remote_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_published_post_id ON social_interactions (published_post_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_campaign_id ON social_interactions (campaign_id)",
        "CREATE INDEX IF NOT EXISTS ix_social_interactions_status ON social_interactions (status)",
        "CREATE INDEX IF NOT EXISTS ix_interaction_ws_created ON social_interactions (workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_interaction_ws_status ON social_interactions (workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_interaction_post ON social_interactions (published_post_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS conversations (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            platform VARCHAR(30) NOT NULL DEFAULT '',
            account_id VARCHAR(36) NOT NULL DEFAULT '',
            thread_key VARCHAR(200) NOT NULL DEFAULT '',
            title VARCHAR(300) NOT NULL DEFAULT '',
            participant_remote_id VARCHAR(200) NOT NULL DEFAULT '',
            participant_name VARCHAR(200) NOT NULL DEFAULT '',
            published_post_id VARCHAR(36),
            last_interaction_at TIMESTAMP,
            unread_count INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'open',
            priority VARCHAR(10) NOT NULL DEFAULT 'normal',
            CONSTRAINT uq_conversation_thread UNIQUE
                (workspace_id, account_id, thread_key)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_conversations_workspace_id ON conversations (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_account_id ON conversations (account_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_published_post_id ON conversations (published_post_id)",
        "CREATE INDEX IF NOT EXISTS ix_conv_ws_last ON conversations (workspace_id, last_interaction_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS community_actions (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            interaction_id VARCHAR(36) NOT NULL REFERENCES social_interactions(id) ON DELETE CASCADE,
            conversation_id VARCHAR(36),
            account_id VARCHAR(36) NOT NULL DEFAULT '',
            platform VARCHAR(30) NOT NULL DEFAULT '',
            action_type VARCHAR(24) NOT NULL DEFAULT 'REPLY',
            mode VARCHAR(24) NOT NULL DEFAULT 'DRAFT_ONLY',
            state VARCHAR(20) NOT NULL DEFAULT 'draft',
            origin VARCHAR(20) NOT NULL DEFAULT 'ai',
            draft_text TEXT NOT NULL DEFAULT '',
            final_text TEXT NOT NULL DEFAULT '',
            brand_check_json JSON NOT NULL DEFAULT '{}',
            approval_user_id VARCHAR(36),
            rejected_user_id VARCHAR(36),
            provider_receipt_json JSON NOT NULL DEFAULT '{}',
            remote_reply_id VARCHAR(200) NOT NULL DEFAULT '',
            is_mock BOOLEAN NOT NULL DEFAULT 0,
            verification_json JSON NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            sent_at TIMESTAMP,
            send_claimed_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_community_actions_workspace_id ON community_actions (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_community_actions_interaction_id ON community_actions (interaction_id)",
        "CREATE INDEX IF NOT EXISTS ix_community_actions_account_id ON community_actions (account_id)",
        "CREATE INDEX IF NOT EXISTS ix_caction_ws_state ON community_actions (workspace_id, state)",
        "CREATE INDEX IF NOT EXISTS ix_caction_ws_sent ON community_actions (workspace_id, sent_at)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS community_opportunities (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            opportunity_type VARCHAR(40) NOT NULL DEFAULT 'content_request',
            title VARCHAR(300) NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            evidence_count INTEGER NOT NULL DEFAULT 1,
            source_interaction_ids JSON NOT NULL DEFAULT '[]',
            confidence VARCHAR(10) NOT NULL DEFAULT 'low',
            state VARCHAR(20) NOT NULL DEFAULT 'open',
            converted_idea_json JSON NOT NULL DEFAULT '{}',
            converted_opportunity_id VARCHAR(36)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_community_opportunities_workspace_id ON community_opportunities (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_community_opportunities_state ON community_opportunities (state)",
        "CREATE INDEX IF NOT EXISTS ix_copp_ws_state ON community_opportunities (workspace_id, state)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS community_insights (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            topic_key VARCHAR(64) NOT NULL DEFAULT '',
            topic VARCHAR(300) NOT NULL DEFAULT '',
            representative_text TEXT NOT NULL DEFAULT '',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            source_interaction_ids JSON NOT NULL DEFAULT '[]',
            platforms JSON NOT NULL DEFAULT '[]',
            related_content_ids JSON NOT NULL DEFAULT '[]',
            confidence VARCHAR(10) NOT NULL DEFAULT 'low',
            state VARCHAR(20) NOT NULL DEFAULT 'new',
            opportunity_id VARCHAR(36),
            CONSTRAINT uq_insight_topic UNIQUE (workspace_id, topic_key)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_community_insights_workspace_id ON community_insights (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_community_insights_state ON community_insights (state)",
        "CREATE INDEX IF NOT EXISTS ix_cins_ws_state ON community_insights (workspace_id, state)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS community_sync_state (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            account_id VARCHAR(36) NOT NULL,
            platform VARCHAR(30) NOT NULL DEFAULT '',
            cursors_json JSON NOT NULL DEFAULT '{}',
            last_success_at TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP,
            CONSTRAINT uq_sync_account UNIQUE (workspace_id, account_id)
        )
    """))
    session.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_community_sync_state_workspace_id "
        "ON community_sync_state (workspace_id)"
    ))
