"""Source-connector base types (Work 10 Lane C).

Every adapter is a held-config object: ``create_connector(kind, config)``
returns an instance that validates its own configuration in ``connect()``
and exposes the uniform ``list`` / ``fetch`` / ``search`` / ``health``
surface that the durable SOURCE_SYNC job drives.

Design rules:
  * Honest failures only — adapters raise :class:`SourceError` with a
    message that is safe to surface (no stack traces, no credential
    values). Configuration problems raise :class:`SourceConfigError`
    (a subclass) so the sync layer can record UNAVAILABLE instead of
    ERROR on the connector row.
  * ``snapshot`` connectors return the COMPLETE current set from
    ``list()``; the sync layer uses a completed pass to mark vanished
    documents ``state="deleted"`` (history is never physically deleted).
  * Size and MIME caps are enforced by the adapters while reading, so a
    hostile endpoint can never flood the documents table.
"""

from __future__ import annotations

import contextlib
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

# text content cap (10 MiB), enforced while streaming any text fetch
MAX_SOURCE_BYTES = 10 * 1024 * 1024
# binary asset download cap (100 MiB) for adapters that pull asset bytes
MAX_ASSET_BYTES = 100 * 1024 * 1024
ALLOWED_TEXT_MIME = (
    "text/plain",
    "text/html",
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/json",
    "application/pdf",
)

# health() status values (mirrors source_connectors.status)
AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
ERROR = "ERROR"

# source_documents.remote_id is String(200): longer ids are stored as their
# sha256 so the unique (workspace, connector, remote_id) key stays intact on
# every backend (SQLite would not enforce the length; Postgres would).
REMOTE_ID_LIMIT = 200

# how many list() pages search() may scan before stopping
MAX_SEARCH_PAGES = 20


class SourceError(Exception):
    """Honest connector failure; ``str(exc)`` is safe to surface to users."""


class SourceConfigError(SourceError):
    """Configuration problem — surfaces as health UNAVAILABLE, not ERROR."""


@dataclass
class SourceDocumentDoc:
    """Adapter-neutral document handed to the sync layer for upserting."""

    remote_id: str
    title: str = ""
    mime_type: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    author: str = ""
    content: str = ""
    asset_reference: str = ""
    checksum: str = ""
    cursor: str = ""
    meta: dict = field(default_factory=dict)


def sha256_text(text: str) -> str:
    """sha256 hex of the stored text (canonical utf-8 encoding)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def canonical_remote_id(remote_id: str) -> str:
    """Normalize a remote id to the 200-char column budget (stable hash)."""
    text = str(remote_id or "").strip()
    if not text:
        return ""
    if len(text) <= REMOTE_ID_LIMIT:
        return text
    return sha256_text(text)


class _HtmlTextExtractor(HTMLParser):
    """Deterministic readable-text + <title> extractor (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):  # HTMLParser API
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):  # HTMLParser API
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):  # HTMLParser API
        if self._in_title:
            self._title_parts.append(data)
        elif not self._skip:
            piece = " ".join(str(data).split())
            if piece:
                self._parts.append(piece)

    @property
    def text(self) -> str:
        return " ".join(self._parts)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())


def extract_html(html: str) -> tuple[str, str]:
    """Strip an HTML document/fragment to ``(readable_text, title)``.

    Script/style content is dropped and whitespace is collapsed, so the
    same input always yields the same text (stable checksums across runs).
    Malformed HTML never raises — the parser is tolerant by design.
    """
    parser = _HtmlTextExtractor()
    with contextlib.suppress(Exception):
        parser.feed(html or "")
        parser.close()
    return parser.text, parser.title


class SourceConnector(ABC):
    """Uniform connector surface (held-config style).

    ``config`` is validated by ``connect()``; ``health()`` never performs
    network I/O — it reports whether the connector is *configured* and
    available, not whether the remote endpoint is up right now.
    """

    kind: str = ""
    implemented: bool = True
    requires_credentials: bool = False
    # True -> list() returns the COMPLETE current set, so a completed pass
    # lets the sync layer mark rows that vanished as state="deleted"
    snapshot: bool = False

    def __init__(self, config: dict | None = None) -> None:
        self.config = dict(config or {})

    def connect(self) -> None:
        """Validate held config; raise SourceError on missing/invalid values."""

    def health(self) -> dict:
        """``{"status": AVAILABLE|UNAVAILABLE|ERROR, "reason": str}``."""
        try:
            self.connect()
        except SourceError as exc:
            return {"status": UNAVAILABLE, "reason": str(exc)[:200]}
        except Exception as exc:  # unexpected -> honest ERROR, never fake health
            return {"status": ERROR, "reason": f"{type(exc).__name__}: {exc}"[:200]}
        return {"status": AVAILABLE, "reason": ""}

    @abstractmethod
    def list(self, *, cursor: str | None = None) -> tuple[list[SourceDocumentDoc], str | None]:
        """One page of documents + next cursor (``""``/``None`` = complete)."""

    @abstractmethod
    def fetch(self, remote_id: str) -> SourceDocumentDoc:
        """Exactly one document by remote id; SourceError when unavailable."""

    def search(self, query: str, *, limit: int = 20) -> list[SourceDocumentDoc]:
        """Case-insensitive substring match over title+content (paged)."""
        needle = (query or "").strip().lower()
        if not needle:
            return []
        found: list[SourceDocumentDoc] = []
        cursor: str | None = None
        for _ in range(MAX_SEARCH_PAGES):
            docs, next_cursor = self.list(cursor=cursor)
            for doc in docs:
                haystack = f"{doc.title or ''}\n{doc.content or ''}".lower()
                if needle in haystack:
                    found.append(doc)
                    if len(found) >= limit:
                        return found
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return found

    def disconnect(self) -> None:
        """Release resources; the default connector holds none."""


__all__ = [
    "ALLOWED_TEXT_MIME",
    "AVAILABLE",
    "ERROR",
    "MAX_ASSET_BYTES",
    "MAX_SEARCH_PAGES",
    "MAX_SOURCE_BYTES",
    "REMOTE_ID_LIMIT",
    "UNAVAILABLE",
    "SourceConfigError",
    "SourceConnector",
    "SourceDocumentDoc",
    "SourceError",
    "canonical_remote_id",
    "extract_html",
    "sha256_text",
]
