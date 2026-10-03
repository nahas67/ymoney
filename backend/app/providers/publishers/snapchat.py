"""Snapchat publisher (Work 14 §5) -- USER_HANDOFF, never autonomous publish.

Why this is a handoff and not a publisher
-----------------------------------------
The task assumed organic Snapchat publishing has no server API. The official
documentation was checked and the picture is more specific than that:

* Snap Kit / Creative Kit / Share Kit all terminate INSIDE the Snapchat app
  where a human taps send. They cannot publish a post from a server.
* An official server publishing API DOES exist -- the **Public Profile API**
  (``businessapi.snapchat.com``) exposes
  ``POST /v1/public_profiles/{profile_id}/stories`` and ``.../spotlights``
  with a full encrypted multipart media pipeline. It is not invented.
* But it is **allowlist-only**: it needs a Snap Business Organization, an OAuth
  app created in Ads Manager (explicitly NOT the Developer Portal), and
  Snap-side allowlisting of the client id. There is no self-serve path.
* Snap's own docs contradict themselves ("Snap public profile APIs are read
  only" on the Profiles page vs. two documented POST publish endpoints), and
  the scope required for those POSTs is not documented.

So the honest model is USER_HANDOFF. The partner path is RECORDED IN FULL
(:data:`PARTNER_PUBLISH_CONTRACT`) and exercised by contract tests against
fixtures, but it is **not enabled**: :func:`partner_publish_enabled` returns
False until a real allowlisted credential is configured, because enabling a
documented-but-unverifiable path would be exactly the "pretend it works" failure
this module exists to prevent.

The critical guarantee: a handoff **can never** produce a LIVE publication or
set a variant to PUBLISHED. :func:`build_handoff_payload` is the only way out
of this module and it returns mode HANDOFF with no remote id.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.engine.distribution.modes import PublicationMode
from app.providers.publishers.base import (
    BasePublisher,
    PublishMetadata,
    PublishResult,
)

#: The documented, DISABLED Snapchat partner publish contract. Recorded here so
#: the exact API/permissions/constraints are auditable, and so enabling it later
#: is a reviewed change rather than a rewrite. Each field is a verbatim
#: documented fact; nothing is inferred.
PARTNER_PUBLISH_CONTRACT: dict[str, Any] = {
    "enabled": False,
    "why_not_enabled": [
        "The Public Profile API is ALLOWLIST-ONLY: a Snap Business "
        "Organization, an OAuth app created in Ads Manager (not the Developer "
        "Portal), and Snap-side allowlisting of the client id. No self-serve "
        "path exists, so it cannot be verified from this repo.",
        "Snap's docs contradict themselves: the Profiles page says 'Snap public "
        "profile APIs are read only' while ProfileAssetManagement documents two "
        "POST publish endpoints.",
        "The scope required for those POSTs is UNVERIFIED: "
        "snapchat-profile-api is documented as read-only yet the write "
        "endpoints live on that surface.",
        "Only a PUBLIC PROFILE (creator/business tier) can be targeted; there "
        "is no documented publish path to a personal account, to friends, or "
        "to DMs.",
    ],
    "media_container": {
        "method": "POST",
        "url": "https://businessapi.snapchat.com/v1/public_profiles/"
               "{profile_id}/media",
        "body": ["type (VIDEO|IMAGE)", "name", "key", "iv"],
        "returns": ["media_id", "add_path", "finalize_path"],
        "expires": "24h",
    },
    "chunk_upload": {
        "method": "POST",
        "url": "https://businessapi.snapchat.com/{add_path}",
        "form": ["action=ADD", "part_number (1..35)"],
        "max_total_bytes": 1_073_741_824,  # 1 GB
    },
    "finalize": {
        "method": "POST",
        "url": "https://businessapi.snapchat.com/{finalize_path}",
        "form": ["action=FINALIZE"],
    },
    "publish_story": {
        "method": "POST",
        "url": "https://businessapi.snapchat.com/v1/public_profiles/"
               "{profile_id}/stories",
        "body": ["media_id (required)",
                 "customized_ttl (ONE_DAY|TWO_DAYS|THREE_DAYS|ONE_WEEK)"],
        "constraints": {"format": "mp4", "min_seconds": 5, "max_seconds": 60,
                        "min_width": 540, "min_height": 960},
        "errors": ["MEDIA_EXPIRED", "MEDIA_POSTING_ALREADY_IN_PROGRESS"],
    },
    "publish_spotlight": {
        "method": "POST",
        "url": "https://businessapi.snapchat.com/v1/public_profiles/"
               "{profile_id}/spotlights",
        "body": ["media_id (required)", "skip_save_to_profile", "description",
                 "locale"],
        "constraints": {"format": "mp4", "min_seconds": 6, "max_seconds": 60,
                        "min_width": 540, "min_height": 960,
                        "description_max_chars": 160},
    },
    "auth": {
        "protocol": "OAuth 2.0 authorization_code (NOT client credentials)",
        "authorize": "https://accounts.snapchat.com/login/oauth2/authorize",
        "token": "https://accounts.snapchat.com/login/oauth2/access_token",
        "token_lifetime_seconds": 3600,
        "scopes": ["snapchat-marketing-api", "snapchat-profile-api",
                   "snapchat-offline-conversions-api"],
        "scope_for_publish": "UNVERIFIED",
    },
    "sources": [
        "https://developers.snap.com/marketing-api/Public-Profile-API/Introduction",
        "https://developers.snap.com/marketing-api/Public-Profile-API/ProfileAssetManagement",
        "https://developers.snap.com/marketing-api/Public-Profile-API/GetStarted",
        "https://developers.snap.com/snap-kit/creative-kit/overview",
        "https://developers.snap.com/api/snapchat-for-web/social-plugins/"
        "share-link-to-snapchat",
    ],
}

#: The user-facing share link. Documented: users click it, choose friends, and
#: press Send -- i.e. the human performs the publish.
SHARE_LINK = "https://www.snapchat.com/share?link={url}"


def partner_publish_enabled(account: dict | None = None) -> bool:
    """False until a real allowlisted Snapchat credential is configured.

    Two independent gates, both of which must open:
    ``SNAPCHAT_PARTNER_PUBLISH`` opt-in, AND an account that actually carries
    Public-Profile-API credentials. There is no code path that flips this on
    from a request, a payload, or a database flag.
    """
    if str(os.getenv("SNAPCHAT_PARTNER_PUBLISH", "")).strip().lower() not in (
            "1", "true", "yes"):
        return False
    account = account or {}
    return bool(account.get("snap_public_profile_id")
                and account.get("access_token")
                and account.get("allowlisted") is True)


class SnapchatHandoffPublisher(BasePublisher):
    """Prepares media for a HUMAN to publish. Never publishes autonomously.

    ``publish()`` is intentionally NOT implemented as a live publish. The base
    class requires the method, so it exists -- and it can only ever return
    HANDOFF. A caller that treats ``success=True`` here as "posted" is reading
    the wrong field: :attr:`PublishResult.handoff_required` and the mode
    returned by :func:`publication_mode_for` are the authoritative signals.
    """

    platform = "snapchat"
    #: Read by the factory to build HANDOFF_PLATFORMS, so campaign code never
    #: needs to know this platform by name.
    handoff_only = True

    def publish(self, video_path: str, meta: PublishMetadata,
                account: dict) -> PublishResult:
        return prepare_handoff(video_path=video_path, meta=meta, account=account)


def prepare_handoff(*, video_path: str, meta: PublishMetadata,
                    account: dict | None = None) -> PublishResult:
    """Verify the media exists and build the handoff instructions.

    Deliberately does NOT set a remote id: a remote id is what downstream
    verification reads as proof of publication, and there is no remote id for
    something a human has not published yet.
    """
    payload = build_handoff_payload(video_path=video_path, meta=meta,
                                    account=account)
    return PublishResult(
        success=True,           # the HANDOFF was prepared successfully
        remote_post_id="",      # never: nothing was published
        remote_url=payload.get("share_url", ""),
        error=payload.get("instruction", ""),
    )


def publication_mode_for(account: dict | None = None) -> PublicationMode:
    """The mode this platform can ever reach today."""
    return (PublicationMode.LIVE if partner_publish_enabled(account)
            else PublicationMode.HANDOFF)


def build_handoff_payload(*, video_path: str, meta: PublishMetadata,
                          account: dict | None = None) -> dict:
    """The record of PREPARED work: what to publish, where it is, how.

    This is what a PublishedPost row stores for a Snapchat publication. It is
    auditable, and its presence is exactly what distinguishes "we prepared
    this" from "this went live".
    """
    from urllib.parse import quote

    account = account or {}
    warnings: list[str] = []
    exists = False
    size = 0
    if video_path:
        candidate = Path(video_path)
        try:
            exists = candidate.exists()
            size = candidate.stat().st_size if exists else 0
        except OSError:
            exists = False
        if not exists:
            warnings.append(
                f"prepared media {video_path!r} was not found on disk; the "
                f"handoff records the intent but the file must be re-uploaded "
                f"before the user can publish")
        elif size == 0:
            warnings.append("prepared media is 0 bytes")

    # No official Snapchat publishing endpoint documents a metadata limit, so
    # YMONEY uses its own conservative budget and says so.
    title = str(meta.title or "")[:160]
    description = str(meta.description or "")[:500]
    share_url = SHARE_LINK.format(url=quote(
        f"snapchat-handoff:{video_path}" if video_path else "ymoney"))

    return {
        "mode": PublicationMode.HANDOFF.value,
        "requires_human": True,
        "instruction": (
            "User handoff required: Snapchat organic publishing has no "
            "self-serve server API, so this media is prepared and waiting for "
            "a human to publish it in the Snapchat app. It is NOT published."),
        "share_url": share_url,
        "creative_kit": {
            "available": True,
            "note": ("Snap Kit / Creative Kit hand the media to the Snapchat "
                     "app camera; the user then sends it. Creative Kit limits "
                     "(PNG/JPEG, MP4/MOV, <=300MB, <=5min, captions <=250 "
                     "chars, one sticker) are app-side, not API-side."),
        },
        "media": {"path": video_path, "exists": exists, "size_bytes": size},
        "prepared_metadata": {"title": title, "description": description,
                              "hashtags": list(meta.hashtags or [])[:4]},
        "partner_path": {
            "available": partner_publish_enabled(account),
            "reason": ("allowlist-only Public Profile API; not enabled"
                       if not partner_publish_enabled(account) else "enabled"),
        },
        "warnings": warnings,
        "remote_id": "",   # explicit: there is no remote id for a handoff
    }


def handoff_pending(publication) -> bool:
    """True when a publication is prepared but not yet confirmed by the user."""
    return (
        str(getattr(publication, "publication_mode", "") or "")
        == PublicationMode.HANDOFF.value
        and getattr(publication, "handoff_completed_at", None) is None
    )
