"""Avatar profiles + rendered outputs (Work 07 Lane C).

Rows are the durable half of `app.engine.avatar.profile`:
  * `AvatarProfileRow` — who the presenter is, where the portrait came from,
    and whether the workspace is ALLOWED to render it (consent_state).
  * `AvatarOutputRow`  — one render: output MediaAsset ref + full lineage
    (source portrait, provider/backend, consent snapshot, driving audio, QC).

Consent is never implicit: `consent_state` starts `pending` and only an
explicit authorization record (evidence + source) flips it to `authorized`.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# authorized | pending | revoked  (see app.engine.avatar.profile.CONSENT_STATES)
CONSENT_STATES = ("authorized", "pending", "revoked")


class AvatarProfileRow(Base, PKMixin, TimestampMixin):
    __tablename__ = "avatar_profiles"
    __table_args__ = (
        Index("ix_avatar_profile_ws_status", "workspace_id", "status"),
        Index("ix_avatar_profile_consent", "workspace_id", "consent_state"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), default="")
    # typed AvatarProfile payload: voice/expression/motion/framing/background/
    # language/brand association + any provider hints
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # MediaAsset id of the source portrait image / driving video (workspace-bound)
    source_asset_ref: Mapped[str] = mapped_column(Text, default="")
    consent_state: Mapped[str] = mapped_column(String(20), default="pending")
    # {authorization_evidence, source, granted_by, granted_at, statement}
    consent_json: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")


class AvatarOutputRow(Base, PKMixin, TimestampMixin):
    __tablename__ = "avatar_outputs"
    __table_args__ = (
        Index("ix_avatar_output_ws_status", "workspace_id", "status"),
        Index("ix_avatar_output_profile", "profile_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[str] = mapped_column(String(36), index=True)
    output_asset_ref: Mapped[str] = mapped_column(Text, default="")
    # {source_asset_ref, provider, backend, consent, audio_asset_ref, timeline_id,
    #  content_item_id, qc, license_notes, ...}
    lineage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="ready")
