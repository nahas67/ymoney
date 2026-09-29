"""Per-platform profiles for master-to-shorts campaigns (Work 04, Lane B).

Pure data + validation. The profiles are plain dicts (documented below) so
operators can tune them without code changes; a future workspace-settings
overlay can deep-merge overrides onto :data:`PLATFORM_PROFILES`.

Profile schema per platform key:
    aspects: list of accepted "W:H" strings (first = canonical output).
    preferred_duration: [min_s, max_s] sweet spot for the feed.
    max_duration: hard cap; longer shorts are flagged by validation.
    safe_zones: fractions of frame to keep clear of platform chrome
        {top, bottom, left, right} — used for caption/CTA placement.
    caption: {max_lines, max_chars_per_line, style} caption preferences.
    metadata: {title_max, description_max, hashtag_max, hashtag_limit}.
    thumbnail: {behavior, cover_text_max} — how covers are picked.
    cta: preferred CTA kinds in rank order (see metadata.CTA_KINDS).
    posting_windows: best local hours (weekday-agnostic seed; the
        Scheduler agent refines these from measured performance).
"""

from __future__ import annotations

PLATFORM_PROFILES: dict[str, dict] = {
    "youtube_shorts": {
        "aspects": ["9:16"],
        "preferred_duration": [20.0, 60.0],
        "max_duration": 180.0,
        "safe_zones": {"top": 0.10, "bottom": 0.22, "left": 0.06, "right": 0.06},
        "caption": {"max_lines": 2, "max_chars_per_line": 32, "style": "word-highlight"},
        "metadata": {"title_max": 100, "description_max": 5000, "hashtag_max": 30, "hashtag_limit": 8},
        "thumbnail": {"behavior": "poster-frame", "cover_text_max": 40},
        "cta": ["SUBSCRIBE", "WATCH_FULL_VIDEO", "COMMENT", "FOLLOW"],
        "posting_windows": [12, 18, 20],
    },
    "tiktok": {
        "aspects": ["9:16"],
        "preferred_duration": [20.0, 60.0],
        "max_duration": 600.0,
        "safe_zones": {"top": 0.12, "bottom": 0.25, "left": 0.06, "right": 0.12},
        "caption": {"max_lines": 2, "max_chars_per_line": 28, "style": "karaoke"},
        "metadata": {"title_max": 150, "description_max": 2200, "hashtag_max": 30, "hashtag_limit": 6},
        "thumbnail": {"behavior": "poster-frame", "cover_text_max": 34},
        "cta": ["FOLLOW", "COMMENT", "WATCH_FULL_VIDEO", "VISIT_PROFILE"],
        "posting_windows": [12, 18, 21],
    },
    "instagram_reels": {
        "aspects": ["9:16"],
        "preferred_duration": [20.0, 60.0],
        "max_duration": 180.0,
        "safe_zones": {"top": 0.14, "bottom": 0.28, "left": 0.06, "right": 0.06},
        "caption": {"max_lines": 3, "max_chars_per_line": 30, "style": "clean-lower"},
        "metadata": {"title_max": 125, "description_max": 2200, "hashtag_max": 30, "hashtag_limit": 8},
        "thumbnail": {"behavior": "cover-selectable", "cover_text_max": 30},
        "cta": ["FOLLOW", "COMMENT", "LEARN_MORE", "VISIT_PROFILE"],
        "posting_windows": [11, 17, 20],
    },
    "facebook_reels": {
        "aspects": ["9:16"],
        "preferred_duration": [20.0, 60.0],
        "max_duration": 180.0,
        "safe_zones": {"top": 0.10, "bottom": 0.25, "left": 0.06, "right": 0.06},
        "caption": {"max_lines": 3, "max_chars_per_line": 32, "style": "clean-lower"},
        "metadata": {"title_max": 120, "description_max": 2000, "hashtag_max": 30, "hashtag_limit": 5},
        "thumbnail": {"behavior": "poster-frame", "cover_text_max": 30},
        "cta": ["FOLLOW", "COMMENT", "LEARN_MORE", "WATCH_FULL_VIDEO"],
        "posting_windows": [12, 18, 19],
    },
    # Work 09: LinkedIn feed posts (native video ≤15 min / 5 GB, text ≤3000
    # chars, 9:16 uploads play as vertical feed video on mobile).
    "linkedin": {
        "aspects": ["9:16", "1:1", "16:9"],
        "preferred_duration": [20.0, 60.0],
        "max_duration": 900.0,
        "safe_zones": {"top": 0.08, "bottom": 0.20, "left": 0.05, "right": 0.05},
        "caption": {"max_lines": 3, "max_chars_per_line": 34, "style": "clean-lower"},
        "metadata": {"title_max": 150, "description_max": 3000, "hashtag_max": 30, "hashtag_limit": 5},
        "thumbnail": {"behavior": "poster-frame", "cover_text_max": 40},
        "cta": ["LEARN_MORE", "COMMENT", "VISIT_PROFILE", "FOLLOW"],
        "posting_windows": [9, 12, 17],
    },
    # Work 09: X in-feed video ≤140s (2m20s) on standard access; posts ≤280
    # chars. The short cap matches what the standard API plan accepts.
    "x": {
        "aspects": ["9:16", "16:9", "1:1"],
        "preferred_duration": [20.0, 60.0],
        "max_duration": 140.0,
        "safe_zones": {"top": 0.08, "bottom": 0.24, "left": 0.05, "right": 0.05},
        "caption": {"max_lines": 2, "max_chars_per_line": 30, "style": "clean-lower"},
        "metadata": {"title_max": 100, "description_max": 280, "hashtag_max": 30, "hashtag_limit": 4},
        "thumbnail": {"behavior": "poster-frame", "cover_text_max": 40},
        "cta": ["COMMENT", "FOLLOW", "VISIT_PROFILE", "LEARN_MORE"],
        "posting_windows": [9, 13, 19],
    },
    # Minimal long-form profile so master validation shares one code path.
    "youtube_longform": {
        "aspects": ["16:9"],
        "preferred_duration": [480.0, 1800.0],
        "max_duration": 43200.0,
        "safe_zones": {"top": 0.05, "bottom": 0.10, "left": 0.03, "right": 0.03},
        "caption": {"max_lines": 2, "max_chars_per_line": 42, "style": "standard"},
        "metadata": {"title_max": 100, "description_max": 5000, "hashtag_max": 60, "hashtag_limit": 8},
        "thumbnail": {"behavior": "custom-16x9", "cover_text_max": 50},
        "cta": ["SUBSCRIBE", "COMMENT", "WATCH_FULL_VIDEO"],
        "posting_windows": [12, 17],
    },
}

#: Short-form campaign platforms (excludes the long-form master profile).
CAMPAIGN_PLATFORMS: tuple[str, ...] = (
    "youtube_shorts",
    "tiktok",
    "instagram_reels",
    "facebook_reels",
    # Work 09: LinkedIn/X campaign keys (account namespaces are linkedin|x).
    "linkedin",
    "x",
)

#: Campaign platform -> SocialAccount/publisher namespace.
ACCOUNT_PLATFORM: dict[str, str] = {
    "youtube_shorts": "youtube",
    "tiktok": "tiktok",
    "instagram_reels": "instagram",
    "facebook_reels": "facebook",
    "youtube_longform": "youtube",
    "linkedin": "linkedin",
    "x": "x",
}


def get_profile(platform: str) -> dict:
    """Return the profile for a platform key; raises KeyError when unknown."""
    try:
        return PLATFORM_PROFILES[platform]
    except KeyError as exc:
        raise KeyError(
            f"unknown campaign platform {platform!r}; pick from {sorted(PLATFORM_PROFILES)}"
        ) from exc


def validate_against_profile(
    platform: str,
    duration: float | None,
    aspect: str | None,
    metadata: dict | None,
) -> list[str]:
    """Check a variant against its platform profile. Returns issue strings."""
    profile = get_profile(platform)
    issues: list[str] = []
    if aspect and aspect not in profile["aspects"]:
        issues.append(f"aspect {aspect} not accepted on {platform} (want {profile['aspects']})")
    if duration is not None:
        lo, hi = profile["preferred_duration"]
        if duration > float(profile["max_duration"]):
            issues.append(f"duration {duration:.1f}s exceeds {platform} max {profile['max_duration']:.0f}s")
        elif duration < lo:
            issues.append(f"duration {duration:.1f}s below {platform} preferred minimum {lo:.0f}s")
        elif duration > hi:
            issues.append(f"duration {duration:.1f}s above {platform} preferred maximum {hi:.0f}s")
    meta = metadata or {}
    limits = profile["metadata"]
    title = str(meta.get("title", ""))
    if not title.strip():
        issues.append(f"missing title for {platform}")
    elif len(title) > int(limits["title_max"]):
        issues.append(f"title {len(title)} chars exceeds {platform} limit {limits['title_max']}")
    desc = str(meta.get("description", "") or meta.get("caption", ""))
    if not desc.strip():
        issues.append(f"missing description/caption for {platform}")
    elif len(desc) > int(limits["description_max"]):
        issues.append(f"description {len(desc)} chars exceeds {platform} limit {limits['description_max']}")
    tags = meta.get("hashtags") or []
    if len(tags) > int(limits["hashtag_limit"]):
        issues.append(f"{len(tags)} hashtags exceeds {platform} limit {limits['hashtag_limit']}")
    return issues


def caption_safe_box(platform: str, width: int = 1080, height: int = 1920) -> dict:
    """Pixel box (x, y, w, h) where captions/CTA stay clear of platform chrome."""
    profile = get_profile(platform)
    zones = profile["safe_zones"]
    x = int(width * zones["left"])
    y = int(height * zones["top"])
    w = int(width * (1.0 - zones["left"] - zones["right"]))
    h = int(height * (1.0 - zones["top"] - zones["bottom"]))
    return {"x": x, "y": y, "w": w, "h": h}
