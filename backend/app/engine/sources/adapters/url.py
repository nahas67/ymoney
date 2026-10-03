"""Single-URL source adapter + shared URL-safety/fetch helpers.

Every network adapter in this package routes its fetches through
:func:`fetch_url`, which enforces (on EVERY request and redirect hop):

  * scheme http/https only — via ``services.webhooks.validate_url``,
  * private/loopback/link-local/reserved target hosts rejected unless
    ``config.allow_private`` is set (literal IPs are checked directly;
    hostnames are resolved with ``socket.getaddrinfo`` and every resolved
    address must be public),
  * redirects followed manually up to 5 hops with the same checks,
  * the ``MAX_SOURCE_BYTES`` cap enforced both from ``Content-Length``
    and while streaming chunks (a lying server cannot flood us),
  * the content-type must be in the caller's MIME allowlist.

``_assert_public_host`` is factored so tests can exercise the address
policy directly with literal IPs (no DNS, no network).
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.core.netguard import NetGuardError, assert_public_ip
from app.engine.sources.base import (
    ALLOWED_TEXT_MIME,
    MAX_SOURCE_BYTES,
    SourceConfigError,
    SourceConnector,
    SourceDocumentDoc,
    SourceError,
    canonical_remote_id,
    extract_html,
    sha256_text,
)
from app.services.webhooks import validate_url

_MAX_REDIRECTS = 5
_TIMEOUT_SECONDS = 30.0
_CHUNK_BYTES = 64 * 1024
_USER_AGENT = "ymoney-source-connector/1.0"


@dataclass
class FetchedUrl:
    """Raw fetch result (text extracted by the caller, not here)."""

    url: str
    mime: str
    charset: str
    text: str
    last_modified: datetime | None = None


def _try_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = str(host or "").strip().strip("[]")
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _as_ipv4_mapped(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _reject_ip(ip) -> None:
    # Work 15 §0: delegated to the shared guard. The old local check was an
    # ALLOW-list of bad classes (is_private/is_loopback/...), which silently
    # missed ranges ipaddress does not label -- CGNAT/Alibaba metadata
    # 100.100.100.200, TEST-NET 192.0.2.0/24 and benchmarking 198.18.0.0/15
    # were all "not private" and therefore got through. The shared guard gates
    # on is_global instead, which is deny-by-default.
    try:
        assert_public_ip(ip)
    except NetGuardError as exc:
        raise SourceError(
            f"{exc}; set allow_private to permit this for development"
        ) from exc


def _operator_allows_private() -> bool:
    """Operator kill-switch for private-target connectors (W11.5 C-F1).

    ``allow_private`` in a connector config is necessary but NOT sufficient:
    the operator must also enable ``allow_private_connectors`` (default False).
    Without this, any workspace admin could self-serve a pivot to loopback or
    cloud-metadata endpoints. Read defensively so a missing setting fails closed.
    """
    try:
        from app.core.config import settings

        return bool(getattr(settings, "allow_private_connectors", False))
    except Exception:
        return False


def _assert_public_host(host: str, *, allow_private: bool = False) -> None:
    """Reject non-public targets (SSRF guard). Raises SourceError.

    Literal IPs are checked directly; hostnames are resolved with
    ``socket.getaddrinfo`` and every resolved address must be public —
    resolution failure fails closed. Config validation that must never
    touch the resolver uses :func:`_reject_unsafe_literal_host` instead.
    """
    if allow_private and not _operator_allows_private():
        raise SourceError(
            "allow_private is disabled by the operator "
            "(allow_private_connectors=False); private targets are refused"
        )
    if allow_private:
        return
    if not str(host or "").strip():
        raise SourceError("missing host")
    ip = _try_ip(host)
    if ip is not None:
        _reject_ip(ip)
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SourceError(f"cannot resolve host {host!r}: {exc}") from exc
    addresses = sorted({info[4][0] for info in infos if info and info[4]})
    if not addresses:
        raise SourceError(f"cannot resolve host {host!r}")
    for addr in addresses:
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise SourceError(f"host {host!r} resolved to an invalid address") from exc
        _reject_ip(resolved)


def _reject_unsafe_literal_host(host: str, *, allow_private: bool = False) -> None:
    """Config-time check: literal IPs only, never touches the resolver."""
    if allow_private and not _operator_allows_private():
        raise SourceConfigError(
            "allow_private is disabled by the operator "
            "(allow_private_connectors=False); private targets are refused"
        )
    if allow_private:
        return
    ip = _try_ip(host)
    if ip is not None:
        _reject_ip(ip)


def normalize_url(url: str) -> str:
    """Canonical remote_id: http(s), lowercased host, no userinfo/fragment."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise SourceError(f"invalid URL: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise SourceError("URL must be http(s):// with a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceError(f"invalid URL port: {exc}") from exc
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def validate_config_url(url: str, *, allow_private: bool = False) -> str:
    """Config-time URL validation (no DNS): scheme + literal-host policy."""
    try:
        cleaned = validate_url(url)
    except ValueError as exc:
        raise SourceConfigError(str(exc)) from exc
    _reject_unsafe_literal_host(urlsplit(cleaned).hostname or "", allow_private=allow_private)
    return cleaned


def _split_ctype(value: str) -> tuple[str, str]:
    parts = [piece.strip() for piece in (value or "").split(";")]
    mime = (parts[0] or "").lower()
    charset = ""
    for param in parts[1:]:
        if param.lower().startswith("charset="):
            charset = param.split("=", 1)[1].strip().strip('"').lower()
    return mime, charset


def _decode_body(raw: bytes, mime: str, charset: str) -> str:
    if mime == "application/pdf":
        return raw.decode("latin-1")  # byte-preserving + deterministic
    if charset:
        with contextlib.suppress(LookupError, UnicodeDecodeError):
            decoded = raw.decode(charset)
            return decoded
    return raw.decode("utf-8", errors="replace")


def _parse_http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value or "")
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def fetch_url(
    url: str,
    *,
    allow_private: bool = False,
    max_bytes: int = MAX_SOURCE_BYTES,
    allowed_mime: tuple[str, ...] | set[str] | None = ALLOWED_TEXT_MIME,
    timeout: float = _TIMEOUT_SECONDS,
) -> FetchedUrl:
    """GET one URL safely; SourceError for every failure mode.

    Redirects are followed manually (max 5 hops) so the scheme/SSRF checks
    re-run on every hop — a public endpoint cannot redirect us inward.
    """
    allowed = None if allowed_mime is None else set(allowed_mime)
    current = url
    with httpx.Client(
        follow_redirects=False, timeout=timeout, headers={"User-Agent": _USER_AGENT}
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                validated = validate_url(current)
            except ValueError as exc:
                raise SourceError(str(exc)) from exc
            host = urlsplit(validated).hostname or ""
            _assert_public_host(host, allow_private=allow_private)
            with client.stream("GET", validated) as resp:
                status = resp.status_code
                if status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise SourceError(f"redirect from {host} has no location header")
                    current = urljoin(validated, location)
                    continue
                if not 200 <= status < 300:
                    raise SourceError(f"HTTP {status} from {host}")
                mime, charset = _split_ctype(resp.headers.get("content-type", ""))
                if allowed is not None and mime not in allowed:
                    raise SourceError(f"disallowed content-type: {mime or 'unknown'}")
                length = (resp.headers.get("content-length") or "").strip()
                if length.isdigit() and int(length) > max_bytes:
                    raise SourceError(f"content exceeds the {max_bytes} byte limit")
                buf = bytearray()
                for chunk in resp.iter_bytes(_CHUNK_BYTES):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise SourceError(f"content exceeds the {max_bytes} byte limit")
                last_modified = _parse_http_date(resp.headers.get("last-modified", ""))
            text = _decode_body(bytes(buf), mime, charset)
            return FetchedUrl(
                url=validated, mime=mime, charset=charset, text=text,
                last_modified=last_modified,
            )
    raise SourceError(f"too many redirects (> {_MAX_REDIRECTS})")


def _fallback_title(url: str) -> str:
    parts = urlsplit(url)
    segment = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    return segment or url


class UrlConnector(SourceConnector):
    """Fetches exactly ONE configured URL as a document (no crawling)."""

    kind = "url"
    snapshot = False

    def _url(self) -> str:
        return str(self.config.get("url") or "").strip()

    def _allow_private(self) -> bool:
        return bool(self.config.get("allow_private"))

    def connect(self) -> None:
        if not self._url():
            raise SourceConfigError("url connector requires 'url' in config")
        validate_config_url(self._url(), allow_private=self._allow_private())

    def _fetch(self) -> SourceDocumentDoc:
        self.connect()
        fetched = fetch_url(self._url(), allow_private=self._allow_private())
        content, title = fetched.text, ""
        if fetched.mime == "text/html":
            content, title = extract_html(fetched.text)
        remote_id = normalize_url(fetched.url)
        return SourceDocumentDoc(
            remote_id=remote_id,
            title=title or _fallback_title(fetched.url),
            mime_type=fetched.mime,
            updated_at=fetched.last_modified,
            content=content,
            checksum=sha256_text(content),
            meta={"url": remote_id},
        )

    def list(self, *, cursor: str | None = None) -> tuple[list[SourceDocumentDoc], str | None]:
        return [self._fetch()], ""

    def fetch(self, remote_id: str) -> SourceDocumentDoc:
        doc = self._fetch()
        wanted = canonical_remote_id(remote_id)
        candidates = {canonical_remote_id(doc.remote_id), canonical_remote_id(self._url())}
        if wanted not in candidates:
            raise SourceError(
                f"url connector only serves its configured URL (wanted {wanted[:80]!r})"
            )
        return doc


__all__ = [
    "FetchedUrl",
    "UrlConnector",
    "fetch_url",
    "normalize_url",
    "validate_config_url",
]
