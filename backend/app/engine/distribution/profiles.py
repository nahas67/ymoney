"""``PlatformOptimizationProfile`` — verified platform constraints (Work 14 §6).

The single source of truth for "what does this platform actually accept", and
the only place a platform limit may be written down.

**The rule that makes this module worth having: a value is present only when an
official platform document states it, and the URL that states it is stored
next to it.** Everything else is ``None`` -- meaning *unknown*, which the
optimizer must then decline to act on rather than guess. That is the opposite
of how a "helpful" hardcoded profile behaves, and guessing a limit produces
either silent truncation or a publish-time rejection discovered by the user.

Each field therefore carries ``None`` for UNKNOWN, never a plausible default.
``ProfileLimit`` keeps the verified number AND its provenance so an operator can
audit the number's origin (and so a later docs change is traceable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ProfileLimit",
    "PlatformOptimizationProfile",
    "PROFILES",
    "UNKNOWN",
    "get_profile",
    "profile_platforms",
]


class _Unknown(str):
    """Sentinel for "official docs do not state this" (distinct from None).

    Subclasses ``str`` deliberately: the profile is serialised straight to the
    API/UI, and a plain object would make the response encoder fail. The value
    is the literal string ``"UNKNOWN"`` so a consumer can render it without
    special-casing, while ``is UNKNOWN`` still works for identity checks.
    """

    def __new__(cls) -> _Unknown:
        return super().__new__(cls, "UNKNOWN")

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


#: Explicit "not documented" marker. Prefer this over ``None`` so an accidental
#: ``None`` from a missing dict key cannot be mistaken for a verified zero.
UNKNOWN = _Unknown()


@dataclass(frozen=True)
class ProfileLimit:
    """One verified constraint plus the document that states it."""

    value: Any
    source: str
    note: str = ""

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.value} ({self.source})"


def _limit(value: Any, source: str, note: str = "") -> ProfileLimit:
    return ProfileLimit(value=value, source=source, note=note)


@dataclass(frozen=True)
class PlatformOptimizationProfile:
    """Verified constraints/preferences for one account platform.

    Every optional field is either a :class:`ProfileLimit` (verified, with its
    source) or :data:`UNKNOWN`. Read them through :meth:`value` so callers never
    have to know which they got.
    """

    platform: str

    # -- media -------------------------------------------------------------
    #: Media containers this platform's official API accepts.
    media_types: frozenset[str] = frozenset()
    #: Aspect ratios ("W:H") the official docs accept, canonical first.
    aspect_ratios: tuple[str, ...] = ()
    #: (preferred_min_s, preferred_max_s, hard_max_s) -- only verified values.
    duration: ProfileLimit | None = None
    #: Whether a distinct cover/thumbnail image may be supplied.
    cover_supported: ProfileLimit | None = None
    cover_required: ProfileLimit | None = None

    # -- text --------------------------------------------------------------
    #: (text_max_graphemes, text_max_bytes) where officially documented.
    text_limits: ProfileLimit | None = None
    title_max: ProfileLimit | None = None
    description_max: ProfileLimit | None = None
    hashtag_limit: ProfileLimit | None = None

    # -- links -------------------------------------------------------------
    #: How a link in the body behaves (e.g. "card_unfurl_client_side").
    link_behavior: ProfileLimit | None = None
    link_limit: ProfileLimit | None = None

    # -- accessibility -----------------------------------------------------
    alt_text: ProfileLimit | None = None

    # -- behaviour ---------------------------------------------------------
    #: Ordered CTA kinds this platform actually supports.
    cta_kinds: tuple[str, ...] = ()
    #: Caption safe-zone fractions (top, bottom, left, right).
    safe_zone: ProfileLimit | None = None

    # -- operational -------------------------------------------------------
    #: Documented quota, when the platform publishes one.
    rate_limit: ProfileLimit | None = None
    #: Scopes/permissions the official docs require, for the OAuth scope check.
    required_permissions: tuple[str, ...] = ()
    #: Documented endpoint base, for auditing the provider.
    api_base: str = ""
    #: Free-form record of anything verified that has no typed field above.
    verified_notes: tuple[str, ...] = ()

    def value(self, field_name: str) -> Any:
        """Return the verified value, or :data:`UNKNOWN`."""
        raw = getattr(self, field_name, UNKNOWN)
        if isinstance(raw, ProfileLimit):
            return raw.value
        return UNKNOWN

    def source_of(self, field_name: str) -> str:
        raw = getattr(self, field_name, None)
        return raw.source if isinstance(raw, ProfileLimit) else ""

    def known(self, field_name: str) -> bool:
        """True only when official docs state this constraint."""
        return not isinstance(self.value(field_name), _Unknown)

    def to_dict(self) -> dict:
        """Serialise for the API/UI, preserving unknown-vs-verified."""
        out: dict[str, Any] = {"platform": self.platform}
        for name in self.__dataclass_fields__:
            if name == "platform":
                continue
            raw = getattr(self, name)
            if isinstance(raw, ProfileLimit):
                out[name] = {"value": raw.value, "source": raw.source,
                             "note": raw.note}
            elif isinstance(raw, (frozenset, tuple, list)):
                out[name] = sorted(raw) if isinstance(raw, frozenset) else list(raw)
            else:
                out[name] = raw
        return out


# ---------------------------------------------------------------------------
# The verified profiles (Work 14 §2/§3/§4)
# ---------------------------------------------------------------------------

_THREADS_POSTS = "https://developers.facebook.com/documentation/threads/posts"
_THREADS_PUBREF = "https://developers.facebook.com/documentation/threads/reference/publishing"
_THREADS_OVERVIEW = "https://developers.facebook.com/documentation/threads/overview"
_THREADS_REPLIES = "https://developers.facebook.com/documentation/threads/reference/reply-management"
_THREADS_INSIGHTS = "https://developers.facebook.com/documentation/threads/reference/insights"

#: Bluesky -- every value traced to the lexicon JSON or bsky.network/docs.
_BLUESKY_POST = ("https://github.com/bluesky-social/atproto/blob/main/"
                 "lexicons/app/bsky/feed/post.json")
_BSU = "https://bsky.network/docs/bluesky-api"
_BIMAGES = ("https://github.com/bluesky-social/atproto/blob/main/"
            "lexicons/app/bsky/embed/images.json")
_BEXTERNAL = ("https://github.com/bluesky-social/atproto/blob/main/"
              "lexicons/app/bsky/embed/external.json")
_BVIDEO = ("https://github.com/bluesky-social/atproto/blob/main/"
           "lexicons/app/bsky/embed/video.json")
_BLIMITS = "https://bsky.network/docs/rate-limits"

#: Threads -- every limit below is quoted from the official pages named.
THREADS = PlatformOptimizationProfile(
    platform="threads",
    media_types=frozenset({"TEXT", "IMAGE", "VIDEO", "CAROUSEL"}),
    aspect_ratios=("1:1", "4:5", "16:9"),
    # MOV/MP4, <=300s, <=1GB, <=1920 cols, 23-60 fps, H.264/HEVC, AAC 48kHz
    duration=_limit(300.0, _THREADS_OVERVIEW,
                    "video max 300s; images have no documented duration limit"),
    # No documented cover-image field on the Threads create endpoint.
    cover_supported=_limit(False, _THREADS_PUBREF,
                           "no cover/thumbnail parameter exists on the "
                           "container create call"),
    cover_required=_limit(False, _THREADS_PUBREF),
    # 500 chars on `text`; emoji counted as UTF-8 bytes.
    text_limits=_limit({"chars": 500, "bytes_utf8": 500},
                       _THREADS_OVERVIEW,
                       "500-char text limit; emoji count as UTF-8 bytes"),
    title_max=UNKNOWN,
    description_max=_limit(500, _THREADS_OVERVIEW,
                           "same single `text` field serves as the caption"),
    hashtag_limit=UNKNOWN,
    # `link_attachment` is text-only and there is no unfurl endpoint.
    link_behavior=_limit("text_only_no_unfurl", _THREADS_POSTS,
                         "link_attachment is text-only; no server unfurl"),
    link_limit=_limit(5, _THREADS_POSTS,
                      ">5 links fails at container creation "
                      "(THREADS_API__LINK_LIMIT_EXCEEDED)"),
    alt_text=_limit(1000, "https://developers.facebook.com/documentation/threads/posts/accessibility",
                    "alt_text on image/video/carousel posts only; NOT text-only"),
    cta_kinds=("FOLLOW", "COMMENT", "REPLY", "LEARN_MORE", "VISIT_PROFILE"),
    safe_zone=_limit({"top": 0.08, "bottom": 0.20, "left": 0.05, "right": 0.05},
                     "ymoney-editor-default",
                     "operator default: Meta publishes no caption safe-zone box"),
    rate_limit=_limit({"posts_per_24h": 250, "replies_per_24h": 1000,
                       "deletes_per_24h": 100},
                      _THREADS_OVERVIEW,
                      "rolling 24h per profile; read via "
                      "GET /{threads-user-id}/threads_publishing_limit"),
    required_permissions=("threads_basic", "threads_content_publish",
                          "threads_manage_replies", "threads_read_replies",
                          "threads_manage_insights", "threads_delete"),
    api_base="https://graph.threads.net/v1.0",
    verified_notes=(
        "AUDIO is readable on posts but NOT creatable: no media_type=AUDIO.",
        "Container status literals: EXPIRED, ERROR, FINISHED, IN_PROGRESS, "
        "PUBLISHED. Poll once/minute for at most 5 minutes.",
        "Container expiry is 24 HOURS, not minutes.",
        "Carousel children: 2..20, each IMAGE or VIDEO via is_carousel_item, "
        "published as ONE post against the 250/24h quota.",
        "Text posts may use auto_publish_text=true (text only, skips publish).",
        "impressions is NOT a queryable insights metric; `clicks` is "
        "user-level only, never media-level.",
        "Third-party replies additionally need threads_keyword_search or "
        "threads_manage_mentions, plus Advanced Access.",
        "Threads OAuth is a separate integration (threads.com/oauth/authorize, "
        "th_exchange_token/th_refresh_token) -- never reuse Instagram tokens.",
    ),
)

# ---------------------------------------------------------------------------
# The verified profiles (Work 14 §2/§3/§4)
# ---------------------------------------------------------------------------
_BLUESKY_POST = ("https://github.com/bluesky-social/atproto/blob/main/"
                 "lexicons/app/bsky/feed/post.json")
_BSU = "https://bsky.network/docs/bluesky-api"
_BIMAGES = ("https://github.com/bluesky-social/atproto/blob/main/"
            "lexicons/app/bsky/embed/images.json")
_BEXTERNAL = ("https://github.com/bluesky-social/atproto/blob/main/"
              "lexicons/app/bsky/embed/external.json")
_BVIDEO = ("https://github.com/bluesky-social/atproto/blob/main/"
           "lexicons/app/bsky/embed/video.json")
_BLIMITS = "https://bsky.network/docs/rate-limits"

BLUESKY = PlatformOptimizationProfile(
    platform="bluesky",
    media_types=frozenset({"TEXT", "IMAGE", "VIDEO", "GALLERY", "QUOTE",
                           "EXTERNAL_CARD"}),
    # Any image/* up to the documented byte caps; no official aspect whitelist.
    aspect_ratios=(),
    duration=UNKNOWN,
    cover_supported=UNKNOWN,
    cover_required=UNKNOWN,
    # text: maxLength 3000 bytes AND maxGraphemes 300
    text_limits=_limit({"graphemes": 300, "bytes": 3000}, _BLUESKY_POST,
                       "lexicon: maxLength 3000 + maxGraphemes 300"),
    title_max=UNKNOWN,
    description_max=_limit(3000, _BLUESKY_POST,
                           "no separate title; the text field is the body"),
    hashtag_limit=_limit(8, _BLUESKY_POST,
                         "tags[] maxLength 8, 640 bytes / 64 graphemes each"),
    # No server-side unfurl: the CLIENT must fetch and embed the card.
    link_behavior=_limit("client_side_unfurl_required", _BSU,
                         "uri+title+description are all mandatory; the client "
                         "must fetch the card and upload the thumb blob"),
    link_limit=UNKNOWN,
    # alt is REQUIRED on app.bsky.embed.images#image; NO documented char cap.
    alt_text=_limit({"required_on_images": True, "max_chars": UNKNOWN},
                    _BIMAGES,
                    "alt is required (may be empty); the lexicon sets no "
                    "maxLength -- the 1000/2000 figures are a client UI "
                    "constant, not an API contract"),
    cta_kinds=("COMMENT", "FOLLOW", "REPLY", "LEARN_MORE", "VISIT_PROFILE"),
    safe_zone=UNKNOWN,
    rate_limit=_limit({"write_points_per_hour": 5000,
                       "write_points_per_day": 35000,
                       "create_points": 3,
                       "max_posts_per_hour": 1666,
                       "api_requests_per_5min_per_ip": 3000,
                       "blob_max_bytes": 52428800,
                       "create_session_per_5min": 30,
                       "create_session_per_day": 300},
                      _BLIMITS,
                      "create costs 3 points; HTTP 429 + Retry-After on breach"),
    required_permissions=("repo:app.bsky.feed.post?action=create",
                          "blob:accept=image/*,video/mp4",
                          "transition:generic"),
    api_base="{pds_host}/xrpc",
    verified_notes=(
        "VIDEO IS SUPPORTED via app.bsky.embed.video + "
        "app.bsky.video.uploadVideo on video.bsky.app (NOT unavailable).",
        "Video embed: video/mp4, maxSize 300,000,000 bytes, <=20 VTT caption "
        "tracks, optional alt + aspectRatio.",
        "Email must be verified before video upload: check "
        "com.atproto.server.getSession -> emailConfirmed.",
        "getJobStatus may return an `already_exists` ERROR that still carries "
        "a valid blob ref -- always read .blob off the response.",
        "Images: app.bsky.embed.images maxLength 4, image/*, maxSize 2,000,000 "
        "bytes each.",
        "app.bsky.embed.external REQUIRES uri + title + description; thumb "
        "blob is optional and capped at 1,000,000 bytes.",
        "Replies need BOTH root and parent strongRefs (uri + cid are both "
        "mandatory); never fabricate a CID.",
        "app.bsky.feed.getReplies DOES NOT EXIST -- use getPostThread(depth="
        "0..1000), which has NO cursor; walk replies[] client-side.",
        "Video DURATION limit is UNVERIFIED in official docs -- do not "
        "hardcode a cap.",
        "deleteRecord (repo, collection, rkey) is idempotent by design.",
        "Client-credentials OAuth is NOT supported; the spec only allows the "
        "authorization_code grant. Use an app password + createSession.",
    ),
)

#: Snapchat -- organic publishing is allowlist-gated, so the honest model today
#: is USER_HANDOFF. The research found that an official server-side publishing
#: API DOES exist (Public Profile API) but is not reachable by a general tool, so
#: it is recorded here in full (endpoints + scopes + constraints) without being
#: declared as an enabled capability. See providers/publishers/snapchat.py.
_PPAPI = "https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement"
_PPINTRO = "https://developers.snap.com/marketing-api/Public-Profile-API/Introduction"
_PPGET = "https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted"

SNAPCHAT = PlatformOptimizationProfile(
    platform="snapchat",
    # Organic publishing today is a HANDOFF: the target is a human in the app.
    media_types=frozenset(),
    aspect_ratios=(),
    duration=UNKNOWN,
    cover_supported=UNKNOWN,
    cover_required=UNKNOWN,
    text_limits=UNKNOWN,
    title_max=UNKNOWN,
    description_max=UNKNOWN,
    hashtag_limit=UNKNOWN,
    link_behavior=UNKNOWN,
    link_limit=UNKNOWN,
    alt_text=UNKNOWN,
    cta_kinds=(),
    safe_zone=UNKNOWN,
    rate_limit=UNKNOWN,
    required_permissions=(),
    api_base="",
    verified_notes=(
        "MODELLED AS USER_HANDOFF, NOT DIRECT_PUBLISH. Snap Kit / Creative Kit "
        "/ Share Kit all terminate inside the mobile app where a human taps "
        "send, so YMONEY prepares the media and the user publishes.",
        "AN OFFICIAL SERVER PUBLISHING API DOES EXIST but is NOT usable by a "
        "general tool today: the Public Profile API (businessapi.snapchat.com) "
        "exposes POST /v1/public_profiles/{profile_id}/stories and "
        ".../spotlights, and it is ALLOWLIST-ONLY (a Snap Business "
        "Organization + an OAuth app created in Ads Manager + Snap-side "
        "allowlisting of the client id; no self-serve path).",
        "That path is deliberately NOT enabled: the required scope is "
        "UNVERIFIED (snapchat-profile-api is documented as read-only yet the "
        "write endpoints live under that surface), and Snap's own docs "
        "contradict themselves ('Snap public profile APIs are read only' vs. "
        "two documented POST publish endpoints).",
        "Only a PUBLIC PROFILE (creator/business tier) can be a target; there "
        "is no documented publish path to a personal account, to friends, or "
        "to DMs.",
        "Saved Story posting is UNVERIFIED: only custom1zed_ttl=ONE_WEEK on "
        "Story is documented; no POST endpoint for saved stories.",
        "If allowlisting is ever granted, the exact recorded contract is: "
        "create media container (POST /v1/public_profiles/{id}/media, 24h "
        "expiry) -> multipart chunk upload (action=ADD, part_number 1..35, "
        "<=1GB) -> action=FINALIZE -> POST .../stories or .../spotlights "
        "(Spotlight description <=160 chars). Story .mp4 5-60s >=540x960; "
        "Spotlight .mp4 6-60s >=540x960. OAuth authorization_code only, token "
        "lifetime 3600s. Contracts for this path live in "
        "tests/test_work14_snapchat.py and stay DISABLED until a real "
        "allowlisted credential is configured.",
        "Analytics via the Public Profile metrics endpoints is likewise "
        "allowlist-gated, so FETCH_METRICS is NOT declared for snapchat.",
    ),
)

#: Pinterest -- every value traced to the official v5 OpenAPI description.
_PIN_CREATE = ("https://github.com/pinterest/pinterest-api-description/blob/main/"
               "v5/openapi.json")
_PINDOCS = "https://developers.pinterest.com/docs/api/v5"
_PINLIMITS = "https://developers.pinterest.com/docs/reference/rate-limits"

PINTEREST = PlatformOptimizationProfile(
    platform="pinterest",
    # media_source union: image_url | image_base64 | video_id |
    # multiple_image_base64 | multiple_image_urls
    media_types=frozenset({"IMAGE", "VIDEO", "CAROUSEL"}),
    # No official aspect whitelist is documented for Pins.
    aspect_ratios=(),
    duration=UNKNOWN,
    # cover_image_url lives INSIDE media_source for video Pins.
    cover_supported=_limit(True, _PIN_CREATE,
                           "media_source.cover_image_url; the OpenAPI schema "
                           "marks it optional but the official guide warns "
                           "omitting it returns 400, so it is always sent"),
    cover_required=_limit(True, _PINDOCS,
                          "guide: 'A valid image url must be provided to "
                          "avoid a 400 Bad Request'"),
    text_limits=UNKNOWN,
    # PinCreate schema maxima.
    title_max=_limit(100, _PIN_CREATE, "PinCreate.title maxLength 100"),
    description_max=_limit(800, _PIN_CREATE,
                           "PinCreate.description maxLength 800"),
    hashtag_limit=UNKNOWN,
    link_behavior=_limit("plain_link_field", _PIN_CREATE,
                         "PinCreate.link is a plain 2048-char string; "
                         "Pinterest does not unfurl"),
    link_limit=_limit(2048, _PIN_CREATE, "PinCreate.link maxLength 2048"),
    alt_text=_limit(500, _PIN_CREATE, "PinCreate.alt_text maxLength 500"),
    cta_kinds=("SAVE", "LEARN_MORE", "FOLLOW", "VISIT_PROFILE"),
    safe_zone=UNKNOWN,
    rate_limit=_limit({"org_write_per_min": 100, "org_write_trial_per_day": 300,
                       "org_read_per_min": 1000, "org_analytics_per_min": 60,
                       "access_token_seconds": 2592000},
                      _PINLIMITS,
                      "Standard plan limits per user per app; the legacy "
                      "user_pin_create categories are pre-5.16 and gone"),
    required_permissions=("boards:read", "boards:write", "pins:read",
                          "pins:write", "user_accounts:read"),
    api_base="https://api.pinterest.com/v5",
    verified_notes=(
        "NO IDEMPOTENCY: pins/create has no Idempotency-Key header, no client "
        "key and no documented 409, so a retried create makes a DUPLICATE PIN. "
        "Idempotency is enforced publisher-side; DELETE /v5/pins/{id} (204) is "
        "the only documented compensating action.",
        "POST /v5/pins accepts application/json ONLY -- there is no multipart "
        "on the Pin create path. Local images must be base64-inlined.",
        "Image content_type is restricted to image/jpeg and image/png. No "
        "GIF/WEBP.",
        "Video is a REAL staged flow: POST /v5/media (media_type=video) -> "
        "multipart POST to the presigned S3 upload_url -> poll "
        "GET /v5/media/{id} until status == 'succeeded' (registered | "
        "processing | succeeded | failed) -> then create the Pin with "
        "source_type video_id. MediaUploadType is video-ONLY, so there is no "
        "staged upload for images.",
        "video_url is a RESPONSE field, not a request field: do not send it.",
        "Carousel items are 2..5, tighter than every other Work 14 platform.",
        "Board name length is NOT documented (BoardSection.name has maxLength "
        "180, but BoardBase.name declares none) -- treated as unconstrained "
        "but unverified.",
        "NO Pin status field and no 'Pin is live' poll. creative_type is "
        "documented as temporarily WRONG during video processing, so treat it "
        "as a best-effort signal only.",
        "Pin analytics requires start_date, end_date and metric_types; the "
        "window may not exceed 90 days back or 90 days past start_date. "
        "Metrics: IMPRESSION, PIN_CLICK, OUTBOUND_CLICK, SAVE, SAVE_RATE, "
        "TOTAL_COMMENTS, TOTAL_REACTIONS, USER_FOLLOW, PROFILE_VISIT. "
        "Bulk pin analytics is beta-gated.",
        "The scope is user_accounts:read (PLURAL); the singular form is not "
        "valid.",
    ),
)

PROFILES: dict[str, PlatformOptimizationProfile] = {
    "threads": THREADS,
    "pinterest": PINTEREST,
    "bluesky": BLUESKY,
    "snapchat": SNAPCHAT,
}


def get_profile(platform: str) -> PlatformOptimizationProfile:
    """Return the optimization profile; KeyError when the platform is unknown."""
    try:
        return PROFILES[str(platform).strip().lower()]
    except KeyError as exc:
        raise KeyError(
            f"no PlatformOptimizationProfile for {platform!r}; "
            f"pick from {sorted(PROFILES)}"
        ) from exc


def profile_platforms() -> tuple[str, ...]:
    return tuple(sorted(PROFILES))
