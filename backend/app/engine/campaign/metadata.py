"""Per-platform metadata generation for campaign shorts (Work 04, Lane B).

Deterministic templates are the default: every platform gets a distinct
title/description/caption/hashtag/CTA bundle derived from the same topic so
feeds never see copy-paste duplicates. When the SEO agent is available its
output can be adapted via :meth:`PlatformMetadataGenerator.from_seo_output`.
"""

from __future__ import annotations

from app.engine.campaign.platforms import CAMPAIGN_PLATFORMS, get_profile

#: Allowed CTA kinds for campaign variants.
CTA_KINDS: tuple[str, ...] = (
    "FOLLOW",
    "SUBSCRIBE",
    "COMMENT",
    "WATCH_FULL_VIDEO",
    "VISIT_PROFILE",
    "LEARN_MORE",
    "NONE",
)

_PLATFORM_VOICE: dict[str, dict] = {
    # Each platform gets a distinct hook prefix, closer style, tag set and
    # CTA phrasing so bundles are NEVER identical across platforms.
    "youtube_shorts": {
        "hook": "The money move nobody talks about",
        "closer": "Full breakdown on the channel.",
        "tags": ["#shorts", "#money", "#finance"],
        "cta_text": {
            "SUBSCRIBE": "Subscribe for one money idea daily.",
            "WATCH_FULL_VIDEO": "Watch the full breakdown on the channel.",
            "COMMENT": "Comment YOUR number below.",
            "FOLLOW": "Follow for daily money shorts.",
            "VISIT_PROFILE": "More breakdowns on the channel page.",
            "LEARN_MORE": "Full guide linked on the channel.",
            "NONE": "",
        },
    },
    "tiktok": {
        "hook": "POV: your money finally starts working",
        "closer": "Follow for part 2.",
        "tags": ["#fyp", "#moneytok", "#personalfinance"],
        "cta_text": {
            "FOLLOW": "Follow for daily money tips.",
            "COMMENT": "Comment 'MORE' and I'll explain next.",
            "WATCH_FULL_VIDEO": "Full video on my YouTube — link in bio.",
            "SUBSCRIBE": "Subscribe on YouTube for the deep dive.",
            "VISIT_PROFILE": "More tips pinned on my profile.",
            "LEARN_MORE": "Link in bio for the full guide.",
            "NONE": "",
        },
    },
    "instagram_reels": {
        "hook": "Save this before your next paycheck",
        "closer": "Save + share with someone who needs this.",
        "tags": ["#reels", "#moneytips", "#wealthbuilding"],
        "cta_text": {
            "FOLLOW": "Follow for weekly money reels.",
            "COMMENT": "Drop a comment with your biggest takeaway.",
            "LEARN_MORE": "Tap learn more for the full breakdown.",
            "VISIT_PROFILE": "More reels like this on my profile.",
            "WATCH_FULL_VIDEO": "Full video linked in bio.",
            "SUBSCRIBE": "Subscribe on YouTube for the long version.",
            "NONE": "",
        },
    },
    "facebook_reels": {
        "hook": "Most people get this money habit backwards",
        "closer": "Share this with family.",
        "tags": ["#moneytips", "#reelsfb", "#financialfreedom"],
        "cta_text": {
            "FOLLOW": "Follow this page for weekly money tips.",
            "COMMENT": "Tell us in the comments: will you try this?",
            "LEARN_MORE": "Tap learn more for the full guide.",
            "WATCH_FULL_VIDEO": "Watch the full video on our channel.",
            "VISIT_PROFILE": "See more on our page.",
            "SUBSCRIBE": "Subscribe for the full series.",
            "NONE": "",
        },
    },
    "linkedin": {
        "hook": "The numbers behind this move, explained",
        "closer": "Full analysis in the document above.",
        "tags": ["#finance", "#markets", "#wealthbuilding"],
        "cta_text": {
            "FOLLOW": "Follow for weekly market breakdowns.",
            "COMMENT": "Share your take in the comments.",
            "LEARN_MORE": "Read the full analysis in the post.",
            "WATCH_FULL_VIDEO": "Watch the full breakdown on our page.",
            "VISIT_PROFILE": "See more analysis on our page.",
            "SUBSCRIBE": "Subscribe for the full series.",
            "NONE": "",
        },
    },
    "x": {
        "hook": "Here's the part everyone misses",
        "closer": "Full breakdown on the timeline.",
        "tags": ["#fintwit", "#money", "#investing"],
        "cta_text": {
            "FOLLOW": "Follow for one money idea a day.",
            "COMMENT": "Reply with your number.",
            "LEARN_MORE": "Full guide linked in the post.",
            "WATCH_FULL_VIDEO": "Full video on our channel.",
            "VISIT_PROFILE": "More breakdowns on our profile.",
            "SUBSCRIBE": "Subscribe for the deep dive.",
            "NONE": "",
        },
    },
    # -- Work 14: expanded distribution -----------------------------------
    # Each new platform gets its OWN hook, closer, tag set and CTA phrasing.
    # Reusing an existing voice here is exactly the "identical metadata renamed
    # per platform" failure the DoD calls out, and the pairwise-distinct test
    # in test_campaign_platforms.py enforces it.
    "threads": {
        "hook": "One money habit, one thread",
        "closer": "Reply 'GUIDE' and I'll post the long version.",
        "tags": ["#money", "#personalfinance", "#threads"],
        "cta_text": {
            "FOLLOW": "Follow for daily money threads.",
            "REPLY": "Reply and I'll send the details.",
            "COMMENT": "Reply with your number below.",
            "LEARN_MORE": "Full guide linked in this thread.",
            "VISIT_PROFILE": "More threads on my profile.",
            "NONE": "",
        },
    },
    "pinterest": {
        "hook": "Pin this: the 20% rule",
        "closer": "Save it for your next payday.",
        "tags": ["#personalfinance", "#budgeting", "#moneytips"],
        "cta_text": {
            "SAVE": "Save this Pin for later.",
            "LEARN_MORE": "Tap the link for the full guide.",
            "FOLLOW": "Follow for weekly money Pins.",
            "VISIT_PROFILE": "More Pins on our profile.",
            "NONE": "",
        },
    },
    "bluesky": {
        "hook": "Worth reading if you budget",
        "closer": "More on the topic in the replies.",
        "tags": ["#money", "#bluesky", "#personalfinance"],
        "cta_text": {
            "FOLLOW": "Follow for short finance posts.",
            "REPLY": "Reply and I'll send the long version.",
            "COMMENT": "Reply with your number.",
            "LEARN_MORE": "Link in the post for the full guide.",
            "VISIT_PROFILE": "More posts on our profile.",
            "NONE": "",
        },
    },
    "snapchat": {
        "hook": "20% first, every single payday",
        "closer": "Snap it and try it this week.",
        "tags": ["#money", "#snaptips", "#personalfinance"],
        # Snapchat has no SERVER-SIDE publish API: this copy is prepared for a
        # human to publish in the app, so the CTA is Snap-native by design.
        "cta_text": {
            "FOLLOW": "Add us on Snap for daily tips.",
            "VISIT_PROFILE": "More Snaps on our public profile.",
            "LEARN_MORE": "Link in the Snap for the full guide.",
            "NONE": "",
        },
    },
}


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = str(t).strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = f"#{t}"
        if t not in out:
            out.append(t)
    return out


class PlatformMetadataGenerator:
    """Deterministic per-platform metadata. Pure — no network, no secrets."""

    def generate(
        self,
        *,
        topic: str,
        script_excerpt: str = "",
        platform: str,
        cta_kind: str = "FOLLOW",
        master_content_id: str = "",
    ) -> dict:
        """Build one platform's metadata bundle.

        WATCH_FULL_VIDEO stores ``master_content_id`` (resolved to a real URL
        only after the master publishes) — never a hardcoded URL.
        """
        profile = get_profile(platform)
        if cta_kind not in CTA_KINDS:
            raise ValueError(f"unknown CTA kind {cta_kind!r}; pick from {CTA_KINDS}")
        voice = _PLATFORM_VOICE.get(platform, _PLATFORM_VOICE["youtube_shorts"])
        limits = profile["metadata"]
        clean_topic = " ".join(str(topic or "Untitled").split())
        cta_text = voice["cta_text"].get(cta_kind, "")

        if platform == "youtube_shorts":
            title = f"{clean_topic} {voice['hook']}"[: int(limits["title_max"])]
        elif platform == "tiktok":
            title = f"{voice['hook']}: {clean_topic}"[: int(limits["title_max"])]
        elif platform == "instagram_reels":
            title = f"{clean_topic} — {voice['hook']}"[: int(limits["title_max"])]
        else:
            title = f"{voice['hook']} — {clean_topic}"[: int(limits["title_max"])]

        excerpt = " ".join(str(script_excerpt or "").split())[:220]
        parts = [voice["closer"], excerpt, cta_text]
        description = " ".join(p for p in parts if p).strip()
        if any(
            k in f"{clean_topic} {excerpt} {description}".lower()
            for k in ("money", "invest", "save", "earn", "budget", "debt", "stock", "crypto")
        ):
            description = f"{description} Not financial advice. For education only.".strip()
        description = description[: int(limits["description_max"])]

        hashtags = _clean_tags(list(voice["tags"]))[: int(limits["hashtag_limit"])]
        caption = f"{title}\n{' '.join(hashtags)}"
        cover_text = clean_topic[: int(profile["thumbnail"]["cover_text_max"])]
        pinned_comment = {
            "COMMENT": "What's your take — agree or disagree?",
            "FOLLOW": "Which money topic should I cover next?",
            "SUBSCRIBE": "What should the next deep dive be?",
            "WATCH_FULL_VIDEO": "Full video is live — what timestamp helped most?",
        }.get(cta_kind, "What should I break down next?")

        bundle = {
            "platform": platform,
            "title": title,
            "description": description,
            "caption": caption,
            "hashtags": hashtags,
            "keywords": [clean_topic.lower(), platform.replace("_", " "), "shorts"],
            "cta_kind": cta_kind,
            "cta_text": cta_text,
            "pinned_comment": pinned_comment,
            "cover_text": cover_text,
        }
        if cta_kind == "WATCH_FULL_VIDEO":
            # Reference only — the public URL is resolved post-publication.
            bundle["master_content_id"] = master_content_id
            bundle["master_url"] = ""
        return bundle

    def generate_all(
        self,
        *,
        topic: str,
        script_excerpt: str = "",
        platforms: list[str] | None = None,
        cta_kind: str = "FOLLOW",
        master_content_id: str = "",
    ) -> dict[str, dict]:
        plats = list(platforms or list(CAMPAIGN_PLATFORMS))
        return {
            p: self.generate(
                topic=topic,
                script_excerpt=script_excerpt,
                platform=p,
                cta_kind=cta_kind,
                master_content_id=master_content_id,
            )
            for p in plats
        }

    @staticmethod
    def from_seo_output(platform: str, seo_meta: dict, *, cta_kind: str = "FOLLOW") -> dict:
        """Adapt an SEOAgent metadata dict into a campaign bundle.

        Used when the SEO agent is present; falls back to templates when its
        output is empty. Keeps the CTA contract (master ref, no hard URLs).
        """
        profile = get_profile(platform)
        limits = profile["metadata"]
        title = str((seo_meta or {}).get("title", ""))[: int(limits["title_max"])]
        if not title.strip():
            raise ValueError("empty SEO title — use generate() templates instead")
        tags = _clean_tags([(seo_meta or {}).get("hashtags") or []])[: int(limits["hashtag_limit"])]
        return {
            "platform": platform,
            "title": title,
            "description": str((seo_meta or {}).get("description", ""))[: int(limits["description_max"])],
            "caption": str((seo_meta or {}).get("first_comment", ""))[:500],
            "hashtags": tags,
            "keywords": [str(k) for k in ((seo_meta or {}).get("keywords") or [])][:10],
            "cta_kind": cta_kind,
            "cta_text": str((seo_meta or {}).get("pinned_comment", ""))[:300],
            "pinned_comment": str((seo_meta or {}).get("pinned_comment", ""))[:500],
            "cover_text": title[: int(profile["thumbnail"]["cover_text_max"])],
        }
