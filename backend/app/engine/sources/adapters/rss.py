"""RSS 2.0 / Atom feed connector — stdlib ``xml.etree`` only.

Honest v1 scope: entries become documents (guid/link identity, HTML-stripped
content, author, parsed dates). No transcripts, no media downloads — those
stay in the existing clips pipeline.

``list()`` returns the feed as a single complete page (``snapshot=True``);
the cursor argument is only a "skip these remote_ids" optimization — the
sync layer's upsert is the real idempotency, so ``next_cursor`` is always
``""`` (one page).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.engine.sources.adapters.url import fetch_url, validate_config_url
from app.engine.sources.base import (
    ALLOWED_TEXT_MIME,
    SourceConfigError,
    SourceConnector,
    SourceDocumentDoc,
    SourceError,
    canonical_remote_id,
    extract_html,
    sha256_text,
)

#: feeds legitimately serve text/xml alongside the base allowlist
FEED_MIME: tuple[str, ...] = ALLOWED_TEXT_MIME + ("text/xml",)


def local_name(tag: str) -> str:
    """Local tag name regardless of namespace (``{ns}item`` -> ``item``)."""
    return str(tag).rsplit("}", 1)[-1]


def first_child(elem, *names):
    """First direct child whose local name matches one of ``names``."""
    wanted = set(names)
    for child in elem:
        if local_name(child.tag) in wanted:
            return child
    return None


def clean_text(value: str | None) -> str:
    text = str(value or "")
    return " ".join(text.split())


def child_text(elem, *names) -> str:
    child = first_child(elem, *names)
    if child is None:
        return ""
    return clean_text(child.text)


def parse_feed_date(value: str) -> datetime | None:
    """RFC822 (RSS ``pubDate``) or ISO-8601 (Atom) -> naive UTC datetime.

    Unparseable input returns ``None`` — a weird date must never crash a sync.
    """
    text = (value or "").strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _entry_content(entry) -> str:  # ElementTree element
    for name in ("encoded", "content", "description", "summary"):
        child = first_child(entry, name)
        if child is None:
            continue
        raw = "".join(child.itertext())
        if raw.strip():
            text, _title = extract_html(raw)
            return text
    return ""


def _entry_author(entry) -> str:
    author = child_text(entry, "creator")  # dc:creator (RSS)
    if author:
        return author[:200]
    author_el = first_child(entry, "author")
    if author_el is None:
        return ""
    name_el = first_child(author_el, "name")
    if name_el is not None and name_el.text:
        return clean_text(name_el.text)[:200]
    return clean_text(author_el.text)[:200]


def _entry_link(entry) -> str:
    link_el = first_child(entry, "link")
    if link_el is None:
        return ""
    href = (link_el.get("href") or "").strip()
    if href:
        return href
    return clean_text(link_el.text)


def entry_to_doc(entry) -> SourceDocumentDoc:
    """One RSS ``<item>`` / Atom ``<entry>`` -> SourceDocumentDoc."""
    title_el = first_child(entry, "title")
    # itertext: titles may carry nested markup (``Hello <b>World</b>``) —
    # plain child.text would silently drop the tail
    title = clean_text("".join(title_el.itertext())) if title_el is not None else ""
    link = _entry_link(entry)
    created = parse_feed_date(
        child_text(entry, "pubDate")
        or child_text(entry, "published")
        or child_text(entry, "issued")
        or child_text(entry, "date")
    )
    updated = parse_feed_date(child_text(entry, "updated")) or created
    content = _entry_content(entry)
    guid = child_text(entry, "guid") or child_text(entry, "id")
    remote_id = guid or link or sha256_text(f"{title}|{created.isoformat() if created else ''}")
    return SourceDocumentDoc(
        remote_id=canonical_remote_id(remote_id),
        title=title,
        mime_type="text/plain" if content else "",
        created_at=created,
        updated_at=updated,
        author=_entry_author(entry),
        content=content,
        checksum=sha256_text(content),
        meta={"link": link} if link else {},
    )


def parse_feed(text: str) -> list[SourceDocumentDoc]:
    """Parse RSS 2.0 or Atom XML into documents (stdlib ElementTree)."""
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError as exc:
        raise SourceError(f"response is not valid XML: {exc}") from exc
    entries = [el for el in root.iter() if local_name(el.tag) in ("item", "entry")]
    return [entry_to_doc(el) for el in entries]


class RssConnector(SourceConnector):
    """RSS 2.0 / Atom feed connector (full-feed snapshot lists)."""

    kind = "rss"
    snapshot = True

    def _url(self) -> str:
        return str(self.config.get("url") or "").strip()

    def _allow_private(self) -> bool:
        return bool(self.config.get("allow_private"))

    def connect(self) -> None:
        if not self._url():
            raise SourceConfigError("rss connector requires 'url' in config")
        validate_config_url(self._url(), allow_private=self._allow_private())

    def _fetch_docs(self) -> list[SourceDocumentDoc]:
        self.connect()
        fetched = fetch_url(
            self._url(),
            allow_private=self._allow_private(),
            allowed_mime=FEED_MIME,
        )
        return parse_feed(fetched.text)

    def list(self, *, cursor: str | None = None) -> tuple[list[SourceDocumentDoc], str | None]:
        docs = self._fetch_docs()
        if cursor:
            # optimization only: skip ids reported before this cursor; the
            # sync upsert is the real idempotency (next_cursor stays "")
            seen = {part for part in str(cursor).split("\n") if part}
            docs = [doc for doc in docs if doc.remote_id not in seen]
        return docs, ""

    def fetch(self, remote_id: str) -> SourceDocumentDoc:
        wanted = canonical_remote_id(remote_id)
        for doc in self._fetch_docs():
            if doc.remote_id == wanted:
                return doc
        raise SourceError(f"feed entry not found: {wanted[:120]}")


__all__ = [
    "FEED_MIME",
    "RssConnector",
    "child_text",
    "clean_text",
    "entry_to_doc",
    "first_child",
    "local_name",
    "parse_feed",
    "parse_feed_date",
]
