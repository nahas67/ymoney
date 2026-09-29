"""Connector catalog + factory (Work 10 Lane C).

13 kinds are declared: 5 implemented today (``local``, ``url``, ``rss``,
``youtube``, ``s3``) and 8 catalog-only kinds that exist so the API/UI can
surface the architecture honestly. Catalog-only kinds are NEVER faked —
``create_connector`` refuses them with "connector not implemented yet" and
their catalog entry carries ``implemented=False`` for honest rendering.
"""

from __future__ import annotations

from app.engine.sources.adapters import (
    LocalConnector,
    RssConnector,
    S3Connector,
    UrlConnector,
    YoutubeConnector,
)
from app.engine.sources.base import SourceConfigError, SourceConnector


def _entry(kind: str, title: str, blurb: str, *, implemented: bool,
           requires_credentials: bool = False) -> dict:
    return {
        "kind": kind,
        "implemented": implemented,
        "requires_credentials": requires_credentials,
        "title": title,
        "blurb": blurb,
    }


CONNECTOR_CATALOG: dict[str, dict] = {
    "local": _entry(
        "local", "Workspace files",
        "Lists this workspace's stored media assets (no network, no credentials).",
        implemented=True,
    ),
    "url": _entry(
        "url", "Single URL",
        "Fetches one configured http(s) URL as a document with SSRF and size guards.",
        implemented=True,
    ),
    "rss": _entry(
        "rss", "RSS / Atom feed",
        "Parses an RSS 2.0 or Atom feed into documents (stdlib XML parsing).",
        implemented=True,
    ),
    "youtube": _entry(
        "youtube", "YouTube channel",
        "Lists a channel's recent videos from the public feed (metadata only).",
        implemented=True,
    ),
    "s3": _entry(
        "s3", "S3 object storage",
        "Lists objects under a prefix in the configured S3-compatible bucket.",
        implemented=True,
        requires_credentials=True,
    ),
    "google_drive": _entry(
        "google_drive", "Google Drive",
        "OAuth-backed Drive files; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "dropbox": _entry(
        "dropbox", "Dropbox",
        "OAuth-backed Dropbox files; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "onedrive": _entry(
        "onedrive", "OneDrive",
        "Microsoft Graph-backed files; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "zoom": _entry(
        "zoom", "Zoom",
        "Zoom cloud recordings; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "riverside": _entry(
        "riverside", "Riverside",
        "Riverside.fm recordings; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "twitch": _entry(
        "twitch", "Twitch",
        "Twitch VODs and clips; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "vimeo": _entry(
        "vimeo", "Vimeo",
        "Vimeo library videos; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
    "loom": _entry(
        "loom", "Loom",
        "Loom library videos; architecture-ready, not implemented yet.",
        implemented=False,
        requires_credentials=True,
    ),
}

_IMPLEMENTATIONS: dict[str, type[SourceConnector]] = {
    "local": LocalConnector,
    "url": UrlConnector,
    "rss": RssConnector,
    "youtube": YoutubeConnector,
    "s3": S3Connector,
}


def create_connector(kind: str, config: dict | None = None) -> SourceConnector:
    """Build a held-config connector for ``kind``.

    Raises SourceError for unknown kinds and SourceConfigError (a subclass)
    for catalog-only kinds that are not implemented yet. The returned
    instance validates its configuration in ``connect()``.
    """
    key = str(kind or "").strip().lower()
    entry = CONNECTOR_CATALOG.get(key)
    if entry is None:
        raise SourceConfigError(f"unknown source connector kind: {kind!r}")
    if not entry["implemented"]:
        raise SourceConfigError("connector not implemented yet")
    return _IMPLEMENTATIONS[key](config)


__all__ = ["CONNECTOR_CATALOG", "create_connector"]
