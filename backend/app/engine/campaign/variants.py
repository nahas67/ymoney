"""Per-platform variants for campaign shorts (Work 04, Lane B).

A variant is a 9:16 reframe intent over a short: by default it is
metadata-only (``timeline_id`` NULL, sharing the short's base timeline) and
a timeline copy is made ONLY when a real visual diff is needed (safe-zone
caption shift or CTA overlay). Regenerating one variant never touches its
siblings.

Persistence: Lane A owns the ``platform_variants`` table. This module talks
to it through a small store protocol — reflection against the real table
when the migration has landed, otherwise an explicit in-memory store
(used by tests). No migrations are created here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.engine.campaign.metadata import CTA_KINDS
from app.engine.campaign.platforms import get_profile

#: Variant lifecycle states (mirror the Lane A contract).
VARIANT_STATUSES: tuple[str, ...] = ("DRAFT", "READY", "SCHEDULED", "PUBLISHED", "FAILED")


@dataclass
class VariantRecord:
    id: str = ""
    workspace_id: str = ""
    campaign_id: str = ""
    short_content_id: str = ""
    platform: str = ""
    aspect_ratio: str = "9:16"
    timeline_id: str | None = None
    metadata_json: dict = field(default_factory=dict)
    cover_asset_id: str | None = None
    safe_zone_json: dict = field(default_factory=dict)
    status: str = "DRAFT"
    publishing_job_id: str | None = None
    published_post_id: str | None = None


class VariantStore(Protocol):
    def upsert(self, record: VariantRecord) -> VariantRecord: ...
    def get(self, short_content_id: str, platform: str) -> VariantRecord | None: ...
    def list_for_short(self, short_content_id: str) -> list[VariantRecord]: ...


class MemoryVariantStore:
    """In-process store for tests and pre-migration operation."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], VariantRecord] = {}
        self._seq = 0

    def upsert(self, record: VariantRecord) -> VariantRecord:
        if not record.id:
            self._seq += 1
            record.id = f"pv-mem-{self._seq}"
        self._rows[(record.short_content_id, record.platform)] = record
        return record

    def get(self, short_content_id: str, platform: str) -> VariantRecord | None:
        return self._rows.get((short_content_id, platform))

    def list_for_short(self, short_content_id: str) -> list[VariantRecord]:
        return [r for (sid, _), r in self._rows.items() if sid == short_content_id]


class ReflectVariantStore:
    """Store backed by the Lane A ``platform_variants`` table via ORM.

    Uses the real :class:`PlatformVariant` model when importable; raises
    RuntimeError when the table does not exist yet (migration pending) so
    callers fail closed with a clear message.
    """

    def __init__(self, session) -> None:
        from sqlalchemy import inspect

        from app.models.campaign import PlatformVariant

        bind = session.get_bind()
        if "platform_variants" not in inspect(bind).get_table_names():
            raise RuntimeError("platform_variants table unavailable — Lane A migration pending")
        self._session = session
        self._model = PlatformVariant

    @staticmethod
    def _to_record(row) -> VariantRecord:
        return VariantRecord(
            id=str(row.id),
            workspace_id=str(row.workspace_id or ""),
            campaign_id=str(row.campaign_id or ""),
            short_content_id=str(row.short_content_id or ""),
            platform=str(row.platform or ""),
            aspect_ratio=str(row.aspect_ratio or "9:16"),
            timeline_id=row.timeline_id,
            metadata_json=dict(row.metadata_json or {}),
            cover_asset_id=row.cover_asset_id,
            safe_zone_json=dict(row.safe_zone_json or {}),
            status=str(row.status or "DRAFT"),
            publishing_job_id=row.publishing_job_id,
            published_post_id=row.published_post_id,
        )

    def upsert(self, record: VariantRecord) -> VariantRecord:
        from sqlalchemy import select

        s = self._session
        existing = s.scalar(
            select(self._model).where(
                self._model.short_content_id == record.short_content_id,
                self._model.platform == record.platform,
            )
        )
        if existing:
            existing.workspace_id = record.workspace_id
            existing.campaign_id = record.campaign_id
            existing.aspect_ratio = record.aspect_ratio
            existing.timeline_id = record.timeline_id
            existing.metadata_json = record.metadata_json
            existing.cover_asset_id = record.cover_asset_id
            existing.safe_zone_json = record.safe_zone_json
            existing.status = record.status
            existing.publishing_job_id = record.publishing_job_id
            existing.published_post_id = record.published_post_id
            s.flush()
            return self._to_record(existing)
        row = self._model(
            workspace_id=record.workspace_id,
            campaign_id=record.campaign_id,
            short_content_id=record.short_content_id,
            platform=record.platform,
            aspect_ratio=record.aspect_ratio,
            timeline_id=record.timeline_id,
            metadata_json=record.metadata_json,
            cover_asset_id=record.cover_asset_id,
            safe_zone_json=record.safe_zone_json,
            status=record.status,
            publishing_job_id=record.publishing_job_id,
            published_post_id=record.published_post_id,
        )
        s.add(row)
        s.flush()
        return self._to_record(row)

    def get(self, short_content_id: str, platform: str) -> VariantRecord | None:
        from sqlalchemy import select

        row = self._session.scalar(
            select(self._model).where(
                self._model.short_content_id == short_content_id,
                self._model.platform == platform,
            )
        )
        return self._to_record(row) if row else None

    def list_for_short(self, short_content_id: str) -> list[VariantRecord]:
        from sqlalchemy import select

        rows = self._session.scalars(
            select(self._model).where(self._model.short_content_id == short_content_id)
        ).all()
        return [self._to_record(r) for r in rows]


def default_store(session, *, memory_fallback: MemoryVariantStore | None = None) -> VariantStore:
    """Real table when migrated, otherwise the given (or a new) memory store."""
    try:
        return ReflectVariantStore(session)
    except RuntimeError:
        return memory_fallback or MemoryVariantStore()


def needs_timeline_copy(
    *,
    platform: str,
    base_aspect: str = "9:16",
    caption_shift: bool = False,
    cta_overlay: bool = False,
) -> bool:
    """True only when the variant needs its own timeline copy.

    A plain 9:16 reframe with default captions shares the base timeline
    (timeline_id NULL). Safe-zone caption shifts and CTA overlays are
    visual diffs that require a copy.
    """
    profile = get_profile(platform)
    if base_aspect not in profile["aspects"]:
        return True
    return bool(caption_shift or cta_overlay)


def reframe_intent(*, platform: str, base_aspect: str = "9:16") -> dict:
    """Recorded 9:16 reframe intent for a variant (no render itself)."""
    profile = get_profile(platform)
    return {
        "platform": platform,
        "from_aspect": base_aspect,
        "to_aspect": profile["aspects"][0],
        "safe_zones": dict(profile["safe_zones"]),
        "caption_style": profile["caption"]["style"],
    }


def build_variant(
    session,
    ws_id: str,
    short_content_id: str,
    platform: str,
    timeline_doc_or_none: dict | None,
    cta_kind: str,
    *,
    campaign_id: str = "",
    metadata: dict | None = None,
    base_aspect: str = "9:16",
    caption_shift: bool = False,
    cta_overlay: bool = False,
    cover_asset_id: str | None = None,
    store: VariantStore | None = None,
) -> VariantRecord:
    """Create or replace one platform variant for a short.

    ``timeline_doc_or_none`` is persisted as a timeline copy ONLY when
    :func:`needs_timeline_copy` says a visual diff exists; otherwise the
    variant shares the short's base timeline (timeline_id NULL).
    """
    if cta_kind not in CTA_KINDS:
        raise ValueError(f"unknown CTA kind {cta_kind!r}; pick from {CTA_KINDS}")
    profile = get_profile(platform)
    active = store or default_store(session)
    copy_needed = timeline_doc_or_none is not None and needs_timeline_copy(
        platform=platform,
        base_aspect=base_aspect,
        caption_shift=caption_shift,
        cta_overlay=cta_overlay,
    )
    timeline_id: str | None = None
    if copy_needed:
        timeline_id = _persist_timeline_copy(
            session,
            ws_id=ws_id,
            short_content_id=short_content_id,
            platform=platform,
            doc=timeline_doc_or_none or {},
        )
    record = VariantRecord(
        workspace_id=ws_id,
        campaign_id=campaign_id,
        short_content_id=short_content_id,
        platform=platform,
        aspect_ratio=profile["aspects"][0],
        timeline_id=timeline_id,
        metadata_json=dict(metadata or {}),
        cover_asset_id=cover_asset_id,
        safe_zone_json=dict(profile["safe_zones"]),
        status="READY" if (metadata or {}) else "DRAFT",
    )
    return active.upsert(record)


def regenerate_variant(
    session,
    ws_id: str,
    short_content_id: str,
    platform: str,
    *,
    metadata: dict | None = None,
    reset_status: str = "DRAFT",
    store: VariantStore | None = None,
) -> VariantRecord:
    """Regenerate ONE variant; siblings are never read or written."""
    active = store or default_store(session)
    current = active.get(short_content_id, platform)
    if current is None or current.workspace_id not in ("", ws_id):
        raise LookupError(f"no {platform} variant for short {short_content_id}")
    if current.workspace_id and current.workspace_id != ws_id:
        raise LookupError(f"no {platform} variant for short {short_content_id}")
    current.metadata_json = dict(metadata) if metadata is not None else current.metadata_json
    current.status = reset_status
    current.publishing_job_id = None
    current.published_post_id = None
    return active.upsert(current)


def _persist_timeline_copy(session, *, ws_id: str, short_content_id: str, platform: str, doc: dict) -> str:
    """Persist a per-variant timeline copy via the existing timeline model."""
    from app.models import ContentTimeline

    row = ContentTimeline(
        workspace_id=ws_id,
        content_item_id=short_content_id,
        name=f"variant-{platform}",
        duration_seconds=float((doc or {}).get("duration_seconds", 0.0) or 0.0),
        tracks_json=dict((doc or {}).get("tracks", {}) or doc or {}),
    )
    session.add(row)
    session.flush()
    return row.id


def variant_payload(record: VariantRecord) -> dict:
    """JSON-safe DTO for API responses."""
    return {
        "id": record.id,
        "short_content_id": record.short_content_id,
        "platform": record.platform,
        "aspect_ratio": record.aspect_ratio,
        "timeline_id": record.timeline_id,
        "shares_base_timeline": record.timeline_id is None,
        "metadata": record.metadata_json,
        "cover_asset_id": record.cover_asset_id,
        "safe_zones": record.safe_zone_json,
        "status": record.status,
        "publishing_job_id": record.publishing_job_id,
        "published_post_id": record.published_post_id,
    }


__all__ = [
    "CTA_KINDS",
    "VARIANT_STATUSES",
    "MemoryVariantStore",
    "ReflectVariantStore",
    "VariantRecord",
    "VariantStore",
    "build_variant",
    "default_store",
    "needs_timeline_copy",
    "reframe_intent",
    "regenerate_variant",
    "variant_payload",
]
