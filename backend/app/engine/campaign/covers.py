"""Platform-aware covers for campaign variants (Work 04, Lane B).

No new image pipeline: covers reuse the existing thumbnail flow
(Video.thumbnail_path via storage.extract_thumbnail, MediaAsset rows of
type "thumbnail") and only compute safe-zone-aware text placement +
per-platform cover text. Rendering/overlay stays with the video engine.
"""

from __future__ import annotations

from app.engine.campaign.platforms import caption_safe_box, get_profile


def brand_cover_hint(session, workspace_id: str, *, platform: str = "",
                     campaign_id: str = "") -> dict:
    """Brand colors / style / logo-safe-zone hint for a cover (never raises).

    Returns the :func:`brand_cover_style` fragment (``brand_colors``,
    ``style_hint``, ``logo_safe_zone``, ``tone`` + lineage markers) or ````
    when the brand module or policy is unavailable — covers stay polish,
    never a hard dependency.
    """
    try:
        from app.engine.brand_templates import brand_cover_style, brand_gate

        gate = brand_gate(session, workspace_id, campaign_id=campaign_id,
                          platform=platform,
                          artifact={"content_format": "short"})
        return brand_cover_style(gate)
    except Exception:  # noqa: BLE001 — brand must never break covers
        return {}


def build_cover_spec(
    *,
    topic: str,
    platform: str,
    title: str = "",
    thumbnail_path: str = "",
    width: int = 1080,
    height: int = 1920,
    brand: dict | None = None,
) -> dict:
    """Pure cover spec: source image + safe-zone-aware text box.

    Returns a dict the video engine / thumbnail flow can consume; never
    generates image bytes itself. ``brand`` is the optional
    :func:`brand_cover_hint` fragment — when present the spec carries brand
    colors / style / logo safe zone for the renderer.
    """
    profile = get_profile(platform)
    cover_text = (title or topic or "Untitled").strip()
    cover_text = cover_text[: int(profile["thumbnail"]["cover_text_max"])]
    box = caption_safe_box(platform, width=width, height=height)
    # Text sits in the lower third of the safe box (clear of top chrome and
    # bottom action rail); height capped to ~18% of the safe area.
    text_h = min(int(box["h"] * 0.18), 260)
    text_box = {
        "x": box["x"],
        "y": box["y"] + box["h"] - text_h - 40,
        "w": box["w"],
        "h": text_h,
    }
    spec = {
        "platform": platform,
        "behavior": profile["thumbnail"]["behavior"],
        "source_thumbnail": thumbnail_path or "",
        "cover_text": cover_text,
        "safe_box": box,
        "text_box": text_box,
        "canvas": {"width": width, "height": height},
    }
    if brand:
        spec["brand"] = dict(brand)
    return spec


def resolve_thumbnail(session, *, workspace_id: str, content_item_id: str) -> str:
    """Best-effort thumbnail lookup for a short (existing pipeline only).

    Prefers the READY video's poster frame, then a MediaAsset thumbnail row.
    Returns "" when nothing exists — covers are polish, never required.
    """
    try:
        from app.models import MediaAsset, Video, VideoVariant
    except Exception:  # pragma: no cover — models always importable in prod
        return ""
    try:
        variants = (
            session.query(VideoVariant)
            .filter(VideoVariant.content_item_id == content_item_id)
            .all()
        )
        for vv in variants:
            vids = session.query(Video).filter(Video.variant_id == vv.id).all()
            for v in vids:
                if getattr(v, "status", "") == "READY" and getattr(v, "thumbnail_path", ""):
                    return v.thumbnail_path
        asset = (
            session.query(MediaAsset)
            .filter(
                MediaAsset.workspace_id == workspace_id,
                MediaAsset.type == "thumbnail",
            )
            .order_by(MediaAsset.created_at.desc())
            .first()
        )
        key = (getattr(asset, "storage_key", "") or "") if asset else ""
        return key
    except Exception:
        return ""
