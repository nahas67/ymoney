"""Work 11 collaboration foundation: projects, reviews, exports, ops (Lane F).

Tables (mirrored idempotently in migrations/versions/0028_collaboration.py):

  * projects             -- workspace-scoped collaboration container
  * project_members      -- per-project role (VIEWER < REVIEWER < EDITOR <
                            ADMIN < OWNER), one row per (project, user)
  * project_targets      -- polymorphic links into a project
                            (content|campaign|timeline|localization|ugc_asset)
  * reviews              -- version-bound approval threads (bound_version +
                            bound_manifest_hash, stale flag when tip moves)
  * review_assignments   -- who was asked to review (append-only)
  * review_decisions     -- APPROVE|REJECT|REQUEST_CHANGES history, each row
                            binds the version it decided on (append-only)
  * revision_requests    -- explicit OPEN -> ADDRESSED | DISMISSED lifecycle
  * comments             -- anchored threads (comments never mutate content)
  * export_profiles      -- saved export presets (workspace rows; the 8
                            builtins carry workspace_id = NULL)
  * export_jobs          -- one export run (state machine + verification)
  * project_archives     -- MANIFEST_ONLY | PORTABLE_ARCHIVE snapshots
  * notifications        -- per-user internal inbox rows
  * retention_policies   -- one row per workspace (NULL days = keep forever)

Design rules (Work 11 foundation):
  * Isolation: every workspace-scoped table carries workspace_id and every
    query filters by it. Polymorphic refs (target_type/target_id) are plain
    strings with no FK, matching the content.py lineage convention.
  * Project roles only NARROW what the workspace role already allows
    (services/project_auth.py never widens a workspace floor).
  * History is append-only: review_decisions, review_assignments and
    notifications are written once and never updated in place.
  * Actor columns (created_by / author_id / assigned_by) are FKs to users;
    nullable actors use ON DELETE SET NULL so deleting one user can never
    destroy a workspace policy row.
  * Every class uses PKMixin + TimestampMixin (models/knowledge.py style),
    so all 13 tables carry created_at + updated_at -- a superset of the
    contracts §2 sketch, documented in the Wave status section.
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
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# target_type vocabulary for project_targets (contracts §2) -- the Projects
# API validates against this tuple and resolves each type to a real,
# workspace-scoped row before linking.
PROJECT_TARGET_TYPES: tuple[str, ...] = (
    "content",
    "campaign",
    "timeline",
    "localization",
    "ugc_asset",
)


class Project(Base, PKMixin, TimestampMixin):
    """Workspace-scoped collaboration container (members + targets + reviews)."""

    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_ws_status", "workspace_id", "status"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, default="", server_default=text("''")
    )
    # ACTIVE | ARCHIVED (archive lifecycle is owned by lane L)
    status: Mapped[str] = mapped_column(
        String(32), default="ACTIVE", server_default=text("'ACTIVE'")
    )
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProjectMember(Base, PKMixin, TimestampMixin):
    """One project role for one workspace user (last-owner rules live in
    services/project_auth.py)."""

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # OWNER | ADMIN | EDITOR | REVIEWER | VIEWER
    role: Mapped[str] = mapped_column(
        String(32), default="VIEWER", server_default=text("'VIEWER'")
    )


class ProjectTarget(Base, PKMixin, TimestampMixin):
    """Polymorphic target linked into a project (strings, no FK)."""

    __tablename__ = "project_targets"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "target_type", "target_id", name="uq_project_target"
        ),
        # reverse lookup used by project_auth.assert_capability
        Index("ix_ptarget_lookup", "target_type", "target_id"),
    )

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)


class Review(Base, PKMixin, TimestampMixin):
    """Version-bound approval thread for one target (stale when tip moves)."""

    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_reviews_ws_created", "workspace_id", "created_at"),
        Index("ix_reviews_ws_state", "workspace_id", "state"),
        Index("ix_reviews_target", "target_type", "target_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # content | timeline_version | campaign | localization | ugc_asset | project
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    # DRAFT | IN_REVIEW | CHANGES_REQUESTED | APPROVED | REJECTED | CANCELLED
    state: Mapped[str] = mapped_column(
        String(32), default="DRAFT", server_default=text("'DRAFT'")
    )
    # exact version under review (timeline version string) + the render
    # manifest hash of that version; approval is stale when the tip moves
    bound_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bound_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    stale_detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReviewAssignment(Base, PKMixin, TimestampMixin):
    """Who was asked to review a given review (append-only)."""

    __tablename__ = "review_assignments"

    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    assigned_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class ReviewDecision(Base, PKMixin, TimestampMixin):
    """One APPROVE | REJECT | REQUEST_CHANGES act (append-only history)."""

    __tablename__ = "review_decisions"

    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    # binding captured AT DECISION TIME (never rewritten)
    bound_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bound_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))


class RevisionRequest(Base, PKMixin, TimestampMixin):
    """Explicit change request: OPEN -> ADDRESSED | DISMISSED (no free states)."""

    __tablename__ = "revision_requests"
    __table_args__ = (
        Index("ix_revision_requests_target", "target_type", "target_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    review_id: Mapped[str | None] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=True, index=True
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # OPEN | ADDRESSED | DISMISSED
    state: Mapped[str] = mapped_column(
        String(32), default="OPEN", server_default=text("'OPEN'")
    )
    # [{kind, description, anchor}, ...]
    items_json: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Comment(Base, PKMixin, TimestampMixin):
    """Anchored comment thread entry (comments NEVER mutate content)."""

    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_ws_created", "workspace_id", "created_at"),
        Index("ix_comments_target", "target_type", "target_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # timeline | timestamp | time_range | scene | timeline_item | caption | asset
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # {t_start?, t_end?, scene_id?, item_id?, caption_id?, asset_id?}
    anchor_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mentions_json: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # timeline version at creation (context only -- never a mutation handle)
    version_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ExportProfile(Base, PKMixin, TimestampMixin):
    """Saved export preset (workspace row, or one of the 8 builtin presets)."""

    __tablename__ = "export_profiles"

    # NULL for builtin profiles shared across workspaces
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # YOUTUBE_4K | YOUTUBE_1080P | SHORTS_1080x1920 | INSTAGRAM_REEL | TIKTOK |
    # ARCHIVE_MASTER | AUDIO_ONLY | CAPTIONS_ONLY
    preset: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ExportJob(Base, PKMixin, TimestampMixin):
    """One export run (state machine + verification verdict + artifact)."""

    __tablename__ = "export_jobs"
    __table_args__ = (
        Index("ix_export_jobs_ws_state", "workspace_id", "state"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("export_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # MP4 | MOV | WebM | MP3 | WAV | SRT | VTT | ASS | TXT | JSON | OTIO | FCPXML
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # QUEUED | RUNNING | COMPLETE | FAILED | CANCELLED
    state: Mapped[str] = mapped_column(
        String(32), default="QUEUED", server_default=text("'QUEUED'")
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    artifact_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    checksum: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    verification_json: Mapped[dict] = mapped_column(
        JSON, default=dict, server_default=text("'{}'")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # jobs.enqueue id (NULL when run inline)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProjectArchive(Base, PKMixin, TimestampMixin):
    """Project export snapshot: MANIFEST_ONLY or PORTABLE_ARCHIVE."""

    __tablename__ = "project_archives"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # MANIFEST_ONLY | PORTABLE_ARCHIVE
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # QUEUED | RUNNING | COMPLETE | FAILED | CANCELLED
    state: Mapped[str] = mapped_column(
        String(32), default="QUEUED", server_default=text("'QUEUED'")
    )
    manifest_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Notification(Base, PKMixin, TimestampMixin):
    """One internal inbox row for one workspace user (append-only)."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_ws_user_read", "workspace_id", "user_id", "read_at"),
        Index("ix_notifications_ws_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # event-ish kind, e.g. REVIEW_ASSIGNED / MENTIONED / EXPORT_COMPLETED
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RetentionPolicy(Base, PKMixin, TimestampMixin):
    """Exactly one row per workspace; NULL day counts mean keep forever."""

    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_retention_workspace"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    audit_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    render_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_asset_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    export_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


__all__ = [
    "Comment",
    "ExportJob",
    "ExportProfile",
    "Notification",
    "PROJECT_TARGET_TYPES",
    "Project",
    "ProjectArchive",
    "ProjectMember",
    "ProjectTarget",
    "RetentionPolicy",
    "Review",
    "ReviewAssignment",
    "ReviewDecision",
    "RevisionRequest",
]
