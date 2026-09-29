"""Brand system ORM models (Work 08 Lane A).

One workspace can own several brands; each brand carries a typed `BrandDNA`
document (the full creative identity: colors/fonts/captions/voice/tone/
forbidden phrases/...), links to MediaAsset rows for logos, watermarks,
intro/outro and lower thirds (references ONLY -- bytes never duplicated), and
optionally a default brand whose DNA applies workspace-wide.

Override layers (campaign / content / platform) live in their OWN rows --
resolving them never mutates stored BrandDNA. Every resolution snapshot is
persisted to `brand_effective_configs` so any generated artifact can record
the exact policy it was produced under (reproducibility).

Tables (mirrored idempotently in migrations/versions/0024_brand.py):
  * brands                  -- brand identities (one default per workspace)
  * brand_dna               -- typed BrandDNA document (brand_id NULL = ws default)
  * brand_assets            -- MediaAsset refs by role (logo/watermark/intro/...)
  * brand_presets           -- creative template presets (Lane C registers builtins)
  * brand_overrides         -- persisted campaign/content/platform override patches
  * brand_effective_configs -- resolution snapshots (effective config + dna version)
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

BRAND_STATUSES = ("active", "archived")
BRAND_ASSET_ROLES = (
    "logo",
    "watermark",
    "intro",
    "outro",
    "lower_third",
    "thumbnail_frame",
)
# brand_overrides.subject_type: where a patch applies in the precedence chain
OVERRIDE_SUBJECT_TYPES = ("campaign", "content", "platform")
# brand_effective_configs.subject_type: what a snapshot was resolved for
EFFECTIVE_SUBJECT_TYPES = ("workspace", "campaign", "content")


class Brand(Base, PKMixin, TimestampMixin):
    """One brand identity inside a workspace."""

    __tablename__ = "brands"
    __table_args__ = (
        Index("ix_brand_ws_status", "workspace_id", "status"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|archived


class BrandDNARow(Base, PKMixin, TimestampMixin):
    """The full typed BrandDNA document for a brand (or the workspace default).

    `brand_id` NULL means workspace-default DNA: it applies when no brand row
    is selected. The document is JSON (validated by engine.brand.dna.BrandDNA).
    """

    __tablename__ = "brand_dna"
    __table_args__ = (
        Index("ix_brand_dna_ws_brand", "workspace_id", "brand_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    brand_id: Mapped[str | None] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=True, index=True
    )
    dna_json: Mapped[dict] = mapped_column(JSON, default=dict)


class BrandAsset(Base, PKMixin, TimestampMixin):
    """MediaAsset reference by role -- never copies bytes."""

    __tablename__ = "brand_assets"
    __table_args__ = (
        Index("ix_brand_asset_ws_role", "workspace_id", "asset_role"),
        Index("ix_brand_asset_brand_role", "brand_id", "asset_role"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), default="", index=True
    )
    asset_role: Mapped[str] = mapped_column(String(30), default="logo")  # ASSET_ROLES
    media_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(160), default="")


class BrandPreset(Base, PKMixin, TimestampMixin):
    """Creative-template preset (builtins registered by Lane C)."""

    __tablename__ = "brand_presets"
    __table_args__ = (
        Index("ix_brand_preset_ws_builtin", "workspace_id", "builtin"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    preset_json: Mapped[dict] = mapped_column(JSON, default=dict)


class BrandOverride(Base, PKMixin, TimestampMixin):
    """Persisted override patch for one subject (campaign/content/platform).

    Kept OUTSIDE `brand_dna` on purpose: applying an override never mutates the
    stored DNA document, so a campaign can diverge without poisoning the brand.
    """

    __tablename__ = "brand_overrides"
    __table_args__ = (
        Index(
            "uq_brand_override_subject",
            "workspace_id",
            "subject_type",
            "subject_id",
            unique=True,
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    subject_type: Mapped[str] = mapped_column(String(20), default="campaign")
    subject_id: Mapped[str] = mapped_column(String(60), default="")
    override_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str] = mapped_column(String(60), default="")


class BrandEffectiveConfig(Base, PKMixin, TimestampMixin):
    """Snapshot of one policy resolution (reproducibility ledger).

    Every generated artifact can store `effective_config_id` and later re-read
    the exact config (effective JSON + DNA version) it was produced under.
    """

    __tablename__ = "brand_effective_configs"
    __table_args__ = (
        Index("ix_brand_eff_ws_subject", "workspace_id", "subject_type", "subject_id"),
        Index("ix_brand_eff_ws_platform", "workspace_id", "platform"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    subject_type: Mapped[str] = mapped_column(String(20), default="workspace")
    subject_id: Mapped[str] = mapped_column(String(36), default="")
    platform: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    effective_json: Mapped[dict] = mapped_column(JSON, default=dict)
    dna_version: Mapped[str] = mapped_column(String(64), default="")
    # free-form provenance note (which levels contributed, caller, etc.)
    note: Mapped[str] = mapped_column(Text, default="")
