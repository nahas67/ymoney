"""Identity, workspace and security-related ORM models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class User(Base, PKMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base, PKMixin, TimestampMixin):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="")

    user: Mapped[User] = relationship(back_populates="refresh_tokens")


class Workspace(Base, PKMixin, TimestampMixin):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    niche: Mapped[str] = mapped_column(String(200), default="")
    brand_voice: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(20), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMember(Base, PKMixin, TimestampMixin):
    __tablename__ = "workspace_members"
    __table_args__ = (Index("ix_member_ws_user", "workspace_id", "user_id"),)

    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_VIEWER = "viewer"
    ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER, ROLE_VIEWER)

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default=ROLE_MEMBER, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="members")


class SocialAccount(Base, PKMixin, TimestampMixin):
    """A connected publishing/analytics account for one platform.

    OAuth tokens are encrypted at rest by the secrets service before storage.
    """

    __tablename__ = "social_accounts"

    PLATFORMS = ("youtube", "tiktok", "facebook", "instagram")

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # Encrypted payloads (never returned to clients in plaintext)
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="connected")  # connected|expired|error
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ApiCredential(Base, PKMixin, TimestampMixin):
    """Generic provider credential storage (LLM keys, trend API keys, etc.)."""

    __tablename__ = "api_credentials"

    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True
    )
    provider: Mapped[str] = mapped_column(String(60), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    value_enc: Mapped[str] = mapped_column(Text, default="")


class WorkspaceApiKey(Base, PKMixin, TimestampMixin):
    """Scoped third-party API key: workspace-bound, role-bearing, revocable.

    Only the sha256 hash is stored (see core.security.hash_token); the
    plaintext `ym_...` value is shown once at mint time and never again.
    """

    __tablename__ = "workspace_api_keys"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), default="")
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")  # viewer|member|admin
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebhookSubscription(Base, PKMixin, TimestampMixin):
    """Outbound webhook: POST signed event payloads to a workspace URL.

    The signing secret is AES-encrypted at rest (see core.security);
    plaintext is shown once at subscribe time and never again.
    """

    __tablename__ = "webhook_subscriptions"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2000), default="")
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    events_json: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base, PKMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    workspace_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(60), default="")
    resource_id: Mapped[str] = mapped_column(String(80), default="")
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
