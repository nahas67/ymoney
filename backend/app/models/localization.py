"""Localization domain ORM models (Work 07 Lane A).

A localization run NEVER mutates its source. `LocalizedContent` records the
source → child lineage (a new ContentItem child, derivation kind "localized"),
the target language/locale and the translation version; the actual artifacts
(new ContentTimeline, scenes, voice/caption tracks, MediaAsset rows) hang off
the child. Glossary terms are workspace-owned and shared across runs; QC
reports hang off one localization row.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# brand/product terms must survive translation; pronunciation terms are TTS-only
GLOSSARY_KINDS = ("brand", "product", "terminology", "pronunciation")
# PENDING: queued · RUNNING: pipeline active · READY: artifacts produced
# (QC verdict lives on the report) · FAILED: pipeline error · CANCELLED: job cancelled
LOCALIZATION_STATUSES = ("PENDING", "RUNNING", "READY", "FAILED", "CANCELLED")
QC_STATUSES = ("PASS", "PASS_WITH_WARNINGS", "REVIEW_REQUIRED", "FAIL")


class LocalizedContent(Base, PKMixin, TimestampMixin):
    """One localization run for one target language, derived from a source item."""

    __tablename__ = "localized_contents"
    __table_args__ = (
        Index("ix_localized_ws_lang", "workspace_id", "language"),
        Index("ix_localized_source", "source_content_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    source_content_id: Mapped[str] = mapped_column(String(36), index=True)
    # derived ContentItem child (kind "localized") holding the output artifacts
    child_content_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(10), default="")
    locale: Mapped[str] = mapped_column(String(20), default="")
    translation_version: Mapped[int] = mapped_column(Integer, default=1)
    lineage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    error: Mapped[str] = mapped_column(String(2000), default="")


class GlossaryTerm(Base, PKMixin, TimestampMixin):
    """Workspace glossary: terms that must never be mistranslated."""

    __tablename__ = "glossary_terms"
    __table_args__ = (
        Index("ix_glossary_ws_term", "workspace_id", "term"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    # empty replacement = keep the source term verbatim (brands, proper nouns)
    replacement: Mapped[str] = mapped_column(String(200), default="")
    # ISO-639-1 codes; empty list = applies to every target language
    target_languages: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(20), default="terminology")  # GLOSSARY_KINDS
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)


class LocalizationQCReport(Base, PKMixin, TimestampMixin):
    """Deterministic (+ advisory SHADOW semantic) QC verdict for one run."""

    __tablename__ = "localization_qc_reports"
    __table_args__ = (
        Index("ix_locqc_ws_content", "workspace_id", "localized_content_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    localized_content_id: Mapped[str] = mapped_column(
        ForeignKey("localized_contents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="REVIEW_REQUIRED")  # QC_STATUSES
    checks_json: Mapped[dict] = mapped_column(JSON, default=dict)
