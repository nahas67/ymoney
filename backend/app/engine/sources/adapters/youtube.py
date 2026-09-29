"""YouTube channel connector (Work 10 v1: metadata only).

Feeds the public Atom endpoint ``https://www.youtube.com/feeds/videos.xml?
channel_id=<id>`` and lists one document per video (id, title, description,
dates, duration in meta).

Honest v1 scope: **no transcripts and no downloads** — captions/transcripts
keep coming from the existing clips pipeline; this connector only surfaces
feed metadata and the description text.

``channel_url`` mapping:
  * ``/channel/UC...``  -> channel id extracted reliably;
  * ``/watch``, ``/playlist``, ``/@handle``, ``/user/``, ``/c/`` ->
    refused with "pass channel_id for this URL form" (guessing an id from
    those forms needs network scraping — an honest error beats a guess).
"""

from __future__ import annotations

import re

from app.engine.sources.adapters.rss import (
    FEED_MIME,
    child_text,
    clean_text,
    first_child,
    local_name,
    parse_feed_date,
)
from app.engine.sources.adapters.url import fetch_url
from app.engine.sources.base import (
    SourceConfigError,
    SourceConnector,
    SourceDocumentDoc,
    SourceError,
    canonical_remote_id,
    extract_html,
    sha256_text,
)

FEED_BASE = "https://www.youtube.com/feeds/videos.xml"

# YouTube channel ids: "UC" + 22 url-safe base64 characters
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def extract_channel_id(config: dict) -> str:
    """Validated channel id from ``channel_id`` or a ``/channel/UC...`` URL."""
    channel_id = str((config or {}).get("channel_id") or "").strip()
    if channel_id:
        if not _CHANNEL_ID_RE.match(channel_id):
            raise SourceConfigError(
                f"invalid YouTube channel_id {channel_id[:40]!r} (expected UC + 22 characters)"
            )
        return channel_id
    channel_url = str((config or {}).get("channel_url") or "").strip()
    if not channel_url:
        raise SourceConfigError("youtube connector requires channel_id or channel_url")
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(channel_url)
    except ValueError as exc:
        raise SourceConfigError(f"invalid channel_url: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceConfigError("channel_url must be http(s):// with a host")
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) >= 2 and segments[0] == "channel" and _CHANNEL_ID_RE.match(segments[1]):
        return segments[1]
    raise SourceConfigError(
        f"cannot extract a channel id from channel_url {parts.path[:80]!r}; "
        "pass channel_id for this URL form"
    )


def _video_entry_to_doc(entry, channel_id: str) -> SourceDocumentDoc | None:  # ElementTree
    video_id = child_text(entry, "videoId")
    id_text = child_text(entry, "id")
    if not video_id and id_text.startswith("yt:video:"):
        video_id = id_text[len("yt:video:"):].strip()
    title = child_text(entry, "title")
    published = parse_feed_date(child_text(entry, "published"))
    updated = parse_feed_date(child_text(entry, "updated")) or published

    description = ""
    for el in entry.iter():
        if local_name(el.tag) == "description" and el.text:
            description = el.text
            break
    content, _title = extract_html(description)

    link = ""
    for el in entry:
        if local_name(el.tag) == "link" and el.get("rel") in (None, "alternate"):
            link = (el.get("href") or "").strip()
            if link:
                break

    duration = None
    for el in entry.iter():
        if local_name(el.tag) == "duration":
            seconds = (el.get("seconds") or "").strip()
            if seconds.isdigit():
                duration = int(seconds)
            break

    author = ""
    author_el = first_child(entry, "author")
    if author_el is not None:
        name_el = first_child(author_el, "name")
        if name_el is not None and name_el.text:
            author = clean_text(name_el.text)
        else:
            author = clean_text(author_el.text)

    owner_channel = child_text(entry, "channelId") or channel_id
    if not video_id and not title:
        return None  # malformed entry: skip rather than invent an identity
    remote_id = video_id or sha256_text(f"{title}|{updated.isoformat() if updated else ''}")
    return SourceDocumentDoc(
        remote_id=canonical_remote_id(remote_id),
        title=title,
        mime_type="text/plain" if content else "",
        created_at=published,
        updated_at=updated,
        author=author,
        content=content,
        checksum=sha256_text(content),
        meta={
            "video_id": video_id,
            "channel_id": owner_channel,
            "duration_seconds": duration,
            "url": link or (f"https://www.youtube.com/watch?v={video_id}" if video_id else ""),
        },
    )


def parse_video_feed(text: str, *, channel_id: str = "") -> list[SourceDocumentDoc]:
    """Parse a YouTube ``videos.xml`` Atom feed (stdlib ElementTree)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text or "")
    except ET.ParseError as exc:
        raise SourceError(f"response is not valid XML: {exc}") from exc
    docs: list[SourceDocumentDoc] = []
    for entry in root.iter():
        if local_name(entry.tag) != "entry":
            continue
        doc = _video_entry_to_doc(entry, channel_id)
        if doc is not None:
            docs.append(doc)
    return docs


class YoutubeConnector(SourceConnector):
    """YouTube channel feed connector (metadata + description only)."""

    kind = "youtube"
    snapshot = True

    def connect(self) -> None:
        channel_id = extract_channel_id(self.config)
        if self.config.get("channel_id") != channel_id:
            self.config["channel_id"] = channel_id  # hold the normalized id

    def _feed_url(self) -> str:
        return f"{FEED_BASE}?channel_id={extract_channel_id(self.config)}"

    def _fetch_docs(self) -> list[SourceDocumentDoc]:
        self.connect()
        fetched = fetch_url(
            self._feed_url(),
            allow_private=bool(self.config.get("allow_private")),
            allowed_mime=FEED_MIME,
        )
        return parse_video_feed(fetched.text, channel_id=str(self.config.get("channel_id") or ""))

    def list(self, *, cursor: str | None = None) -> tuple[list[SourceDocumentDoc], str | None]:
        docs = self._fetch_docs()
        if cursor:
            seen = {part for part in str(cursor).split("\n") if part}
            docs = [doc for doc in docs if doc.remote_id not in seen]
        return docs, ""

    def fetch(self, remote_id: str) -> SourceDocumentDoc:
        wanted = canonical_remote_id(remote_id)
        for doc in self._fetch_docs():
            if doc.remote_id == wanted:
                return doc
        raise SourceError(f"video not found in channel feed: {wanted[:120]}")


__all__ = [
    "FEED_BASE",
    "YoutubeConnector",
    "extract_channel_id",
    "parse_video_feed",
]
