"""Work 15 §0 — the canonical outbound-URL guard (SSRF / DNS-rebinding).

Every YMONEY-side fetch of a user- or source-supplied URL must go through this
module. It exists because the guard originally lived inside the URL source
adapter, where a second caller (the Work 15 planner, ingesting a source or a
community-request link) would otherwise have had to copy it -- and a copied
guard silently rots. There is one implementation and one test surface.

What it enforces, per request and per redirect hop:

* **scheme** http/https only; no userinfo (a credential-leak pattern).
* **resolution**: the hostname is resolved and EVERY returned address must be
  publicly routable. Resolution failure fails CLOSED.
* **address classes** rejected: loopback, private, link-local, multicast,
  reserved, unspecified, and -- critically -- anything that is simply not
  ``is_global``. The last one is what catches ranges ``ipaddress`` does not
  label (CGNAT/Alibaba metadata ``100.100.100.200``, TEST-NET ``192.0.2.0/24``,
  benchmarking ``198.18.0.0/15``). A property allow-list of "bad" classes is
  not sufficient here; the safe primitive is the deny-by-default gate.
* **redirects** are followed manually and re-validated at every hop, so a
  public endpoint cannot bounce the fetch inward.
* **DNS rebinding**: the address validated is the address pinned. See
  :class:`PinnedTransport` -- a pre-flight check alone is defeated by a
  resolver that answers public on the first query and private on the second.

Scope honesty
-------------
This protects fetches **YMONEY performs**. It does NOT protect a URL that is
handed to a third party to fetch -- notably the Threads container
``image_url``/``video_url``, which Meta's own infrastructure retrieves. That
boundary is enforced separately (see
``providers/publishers/threads.py::_public_media_url``), and no claim is made
here about it.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "MAX_REDIRECTS",
    "NetGuardError",
    "PinnedTarget",
    "assert_public_host",
    "assert_public_ip",
    "pin",
    "reject_unsafe_literal_host",
    "resolve_public_addresses",
    "safe_url",
    "split_url",
]


class NetGuardError(ValueError):
    """A URL or host was refused. Always fails closed."""


MAX_REDIRECTS = 5
_ALLOWED_SCHEMES = ("http", "https")


def _as_ipv4_mapped(ip):
    """Unwrap ``::ffff:10.0.0.1`` so a mapped private address is caught."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _describe(ip) -> str:
    return "private/loopback/link-local/multicast/reserved/not-globally-routable"


def assert_public_ip(ip) -> None:
    """Raise unless ``ip`` is a publicly routable address.

    The primary gate is ``is_global``. The explicit class checks are kept as
    well: they are defence in depth (so a future stdlib change cannot quietly
    widen this) and they produce a clearer operator-facing message.
    """
    ip = _as_ipv4_mapped(ip)
    explicit = (
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )
    if explicit or not ip.is_global:
        raise NetGuardError(
            f"host {ip} is not a public address ({_describe(ip)})")


def resolve_public_addresses(host: str) -> list[str]:
    """Resolve ``host`` and return only its publicly routable addresses.

    Fails closed on resolution error. Returns the address list so a caller can
    PIN the connection to a validated address (defeating DNS rebinding).
    """
    if not str(host or "").strip():
        raise NetGuardError("missing host")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        # OSError covers socket.gaierror AND socket.timeout; a resolver that
        # times out must fail CLOSED as a NetGuardError, not escape as a raw
        # socket error the caller may not be expecting.
        raise NetGuardError(f"cannot resolve host {host!r}: {exc}") from exc
    addresses = sorted({info[4][0] for info in infos if info and info[4]})
    if not addresses:
        raise NetGuardError(f"cannot resolve host {host!r}")
    for addr in addresses:
        try:
            parsed = ipaddress.ip_address(addr)
        except ValueError as exc:
            # Only the PARSE is guarded here. NetGuardError subclasses
            # ValueError, so asserting inside this try/except would swallow a
            # refusal and report it as "invalid address" instead.
            raise NetGuardError(
                f"host {host!r} resolved to an invalid address") from exc
        assert_public_ip(parsed)
    return addresses


def reject_unsafe_literal_host(host: str) -> None:
    """Config-time check for a LITERAL IP: never touches the resolver.

    Used where a DNS lookup is unacceptable (validation on a save path). A
    hostname is not checked here -- use :func:`assert_public_host` for that.
    """
    raw = str(host or "").strip()
    if not raw:
        raise NetGuardError("missing host")
    candidate = raw.strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return  # a hostname: nothing to check without resolving
    assert_public_ip(address)


def assert_public_host(host: str) -> list[str]:
    """Validate a host, returning the resolved public addresses."""
    raw = str(host or "").strip().strip("[]")
    if not raw:
        raise NetGuardError("missing host")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return resolve_public_addresses(raw)
    assert_public_ip(address)
    return [raw]


def split_url(url: str):
    """Return ``(scheme, host, canonical_url)`` after structural validation."""
    raw = str(url or "").strip()
    if not raw:
        raise NetGuardError("url is required")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise NetGuardError(f"invalid URL: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise NetGuardError(f"url scheme must be http(s), got {scheme or '(none)'!r}")
    if parts.username or parts.password:
        raise NetGuardError("url must not embed credentials")
    host = (parts.hostname or "").strip().strip("[]")
    if not host:
        raise NetGuardError("url has no host")
    if host.endswith((".local", ".internal", ".localhost")) or host == "localhost":
        raise NetGuardError(f"host {host!r} is not publicly reachable")
    canonical = urlunsplit((scheme, parts.netloc.lower(), parts.path or "/",
                            parts.query, ""))
    return scheme, host, canonical


def safe_url(url: str) -> str:
    """Structurally validate + resolve ``url``; return the canonical form.

    Call this immediately BEFORE a request. It re-checks the host on every
    redirect hop too, which is what stops a public URL bouncing the fetch to
    an internal one.
    """
    _scheme, host, canonical = split_url(url)
    assert_public_host(host)
    return canonical


@dataclass(frozen=True)
class PinnedTarget:
    """A host plus the exact addresses validation approved for it.

    Passing this to the transport means the connection is made to an address
    that was already checked, so a resolver that flips to a private answer
    between validation and connect cannot be used to pivot inward.
    """

    url: str
    host: str
    addresses: tuple[str, ...]


def pin(url: str) -> PinnedTarget:
    """Validate and PIN a URL in one step."""
    _scheme, host, canonical = split_url(url)
    addresses = assert_public_host(host)
    return PinnedTarget(url=canonical, host=host, addresses=tuple(addresses))
