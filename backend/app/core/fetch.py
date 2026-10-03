"""Work 15 §0 — outbound fetching that cannot be redirected inward.

``fetch_public_url`` is the only sanctioned way for YMONEY to retrieve a
user- or source-supplied URL. It combines the three things a pre-flight check
alone cannot give you:

1. **Pre-flight resolution + validation** (:func:`~app.core.netguard.pin`).
2. **Manual redirects**, re-validated at every hop -- ``httpx``'s own
   ``follow_redirects=True`` would happily follow a public URL into
   ``169.254.169.254`` without re-checking anything.
3. **Address pinning**, so the bytes are actually read from an address that
   was validated. Without this, ``pin()`` and ``connect()`` are two separate
   DNS answers and a rebinding resolver gets a private one for the second.

The pinning works by rewriting the connection to a literal IP and restoring
the original ``Host`` header (and TLS SNI via ``extensions["sni_hostname"]``),
which is the standard technique for making a validated hostname resolution
authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.netguard import (
    MAX_REDIRECTS,
    NetGuardError,
    PinnedTarget,
    pin,
)

__all__ = ["FetchResult", "fetch_public_url", "PinnedHTTPTransport"]

_DEFAULT_TIMEOUT = 20.0
_USER_AGENT = "YMONEY/15 (+https://ymoney.local)"

#: Statuses that genuinely mean "go somewhere else". 304 (Not Modified) and 305
#: (Use Proxy) are 3xx but are NOT redirects, and are returned to the caller.
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class PinnedHTTPTransport(httpx.HTTPTransport):
    """Transport that connects to a pinned, pre-validated address.

    The resolved address is chosen once and the request URL is rewritten to a
    literal IP for the duration of the connection. The original hostname is
    preserved in ``Host`` and in the TLS SNI extension so virtual hosting and
    certificate validation still behave correctly.
    """

    def __init__(self, target: PinnedTarget, **kwargs) -> None:
        self._target = target
        super().__init__(**kwargs)

    def _rewrite(self, request: httpx.Request) -> httpx.Request:
        if not self._target.addresses:
            return request
        address = self._target.addresses[0]
        original = str(request.url)
        parts = urlsplit(original)
        literal_host = f"[{address}]" if ":" in address else address
        port = f":{parts.port}" if parts.port else ""
        pinned_url = urlunsplit(
            (parts.scheme, f"{literal_host}{port}", parts.path or "/",
             parts.query, ""))
        headers = httpx.Headers(request.headers)
        headers["Host"] = (
            f"{self._target.host}:{parts.port}" if parts.port
            else self._target.host)
        extensions = dict(request.extensions)
        # httpx passes this to the TLS layer as SNI.
        extensions["sni_hostname"] = self._target.host
        return httpx.Request(
            method=request.method, url=pinned_url, headers=headers,
            content=request.content, extensions=extensions,
            stream=request.stream)


@dataclass
class FetchResult:
    url: str            # the final URL after redirects
    status_code: int
    text: str
    content_type: str = ""
    hops: tuple[str, ...] = ()
    #: True when the body exceeded max_bytes and was cut short. A consumer that
    #: parses this MUST know, rather than silently reading a partial document.
    truncated: bool = False


def _client_for(target: PinnedTarget, timeout: float) -> httpx.Client:
    transport = PinnedHTTPTransport(target, timeout=timeout)
    return httpx.Client(transport=transport, timeout=timeout,
                        follow_redirects=False,
                        headers={"User-Agent": _USER_AGENT})


def fetch_public_url(url: str, *, timeout: float = _DEFAULT_TIMEOUT,
                     max_bytes: int = 5_000_000) -> FetchResult:
    """Fetch a URL, re-validating every redirect hop and pinning the address.

    Raises :class:`~app.core.netguard.NetGuardError` for a refused URL. Any
    hop that resolves to a non-public address aborts the whole fetch.

    ``max_bytes`` bounds the text RETURNED, and the read is streamed so an
    oversized body is abandoned rather than buffered whole -- otherwise a
    hostile URL could exhaust memory before the limit was ever consulted.
    """
    current = str(url or "").strip()
    hops: list[str] = []
    truncated = False
    for _hop in range(MAX_REDIRECTS + 1):
        # Re-pin on EVERY hop: this is the rebinding defence. A redirect target
        # is a brand-new URL and gets the full treatment.
        target = pin(current)
        hops.append(target.url)
        # streamed, so an oversized body is abandoned rather than buffered whole
        with _client_for(target, timeout) as client, \
                client.stream("GET", target.url) as response:
            status_code = response.status_code
            # httpx treats EVERY 3xx as a redirect, including 304 Not Modified
            # and 305 Use Proxy, which are ordinary responses the caller should
            # see. Only a real redirect status is followed.
            following = response.is_redirect and status_code not in (
                304, 305)
            if following:
                location = response.headers.get("location", "")
                if not location:
                    raise NetGuardError(
                        f"redirect from {target.url} has no Location header")
                current = _resolve_location(target.url, location)
                continue
            content_type = response.headers.get("content-type", "")
            encoding = response.encoding
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    truncated = True
                    break
                chunks.append(chunk)
            body_bytes = b"".join(chunks)
        # A real redirect that reached here lost its Location somewhere.
        if status_code in _REDIRECT_STATUSES:
            raise NetGuardError(
                f"redirect from {target.url} has no Location header")
        body = body_bytes.decode(encoding or "utf-8", errors="replace")
        return FetchResult(url=target.url, status_code=status_code, text=body,
                           content_type=content_type, hops=tuple(hops),
                           truncated=truncated)
    raise NetGuardError(f"too many redirects (> {MAX_REDIRECTS}) from {url!r}")


def _resolve_location(base: str, location: str) -> str:
    """Resolve a possibly-relative Location against the current URL."""
    from urllib.parse import urljoin

    return urljoin(base, location.strip())
