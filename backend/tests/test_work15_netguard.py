"""Work 15 §0 — outbound-URL hardening (SSRF, DNS rebinding, redirect hops).

Covers the carry-forward from Work 14, where the publisher's media-URL check
was host-based and therefore could not see a hostname that resolves inward.
The guard is now shared (``app.core.netguard``) and applied to every
YMONEY-SIDE fetch (``app.core.fetch``).

The scope boundary is asserted explicitly at the bottom: a URL handed to a
third party (the Threads container media URL, which Meta fetches) is NOT
covered by this module, and no test pretends otherwise.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from app.core.netguard import (
    MAX_REDIRECTS,
    NetGuardError,
    PinnedTarget,
    assert_public_host,
    assert_public_ip,
    pin,
    reject_unsafe_literal_host,
    resolve_public_addresses,
    safe_url,
    split_url,
)

# ---------------------------------------------------------------------------
# the carry-forward gap: ranges the old allow-list of "bad" classes missed
# ---------------------------------------------------------------------------

#: 100.100.100.200 is Alibaba Cloud's metadata service; 192.0.2.0/24 is
#: TEST-NET; 198.18.0.0/15 is benchmarking. None is flagged ``is_private`` by
#: the stdlib, so an is_private-based guard lets all three through.
MISSED_BY_CLASS_ALLOWLIST = [
    "100.100.100.200",
    "192.0.2.1",
    "198.18.0.1",
    "100.64.0.1",       # CGNAT shared address space
    "203.0.113.9",      # another TEST-NET block
]


@pytest.mark.parametrize("address", MISSED_BY_CLASS_ALLOWLIST)
def test_metadata_and_reserved_ranges_are_refused(address):
    import ipaddress

    with pytest.raises(NetGuardError):
        assert_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "0.0.0.0", "::1", "fe80::1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
])
def test_every_non_public_class_is_refused(address):
    import ipaddress

    with pytest.raises(NetGuardError):
        assert_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_public_addresses_are_allowed(address):
    import ipaddress

    assert_public_ip(ipaddress.ip_address(address))


def test_ipv4_mapped_private_address_is_unwrapped():
    """::ffff:10.0.0.1 must be treated as 10.0.0.1, not as exotic IPv6."""
    import ipaddress

    with pytest.raises(NetGuardError):
        assert_public_ip(ipaddress.ip_address("::ffff:10.0.0.1"))


# ---------------------------------------------------------------------------
# URL structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com",
    "javascript:alert(1)",
    "",
    "   ",
    "https://user:pw@example.com/x",
    "https://localhost/x",
    "https://box.local/x",
    "https://svc.internal/x",
])
def test_dangerous_urls_are_refused(url):
    with pytest.raises(NetGuardError):
        split_url(url)


def test_canonical_url_drops_fragment_and_lowercases_host():
    _scheme, host, canonical = split_url("HTTPS://Example.COM/a/b?x=1#frag")
    assert host == "example.com"
    assert canonical == "https://example.com/a/b?x=1"
    assert "#" not in canonical


# ---------------------------------------------------------------------------
# resolution: fails closed, and a hostname resolving inward is refused
# ---------------------------------------------------------------------------


def test_hostname_resolving_to_a_private_address_is_refused(monkeypatch):
    """This is the case a host-string check cannot see."""
    def _fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    with pytest.raises(NetGuardError) as caught:
        resolve_public_addresses("rebind.example")
    assert "10.1.2.3" in str(caught.value) or "not a public address" in str(
        caught.value)


def test_every_resolved_address_must_be_public(monkeypatch):
    """A host resolving to one public + one private address is refused."""
    def _mixed(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed)
    with pytest.raises(NetGuardError):
        resolve_public_addresses("mixed.example")


def test_resolution_failure_fails_closed(monkeypatch):
    def _boom(host, port, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(NetGuardError):
        resolve_public_addresses("nope.example")


def test_public_hostname_resolves_and_returns_addresses(monkeypatch):
    def _public(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _public)
    assert resolve_public_addresses("example.com") == ["93.184.216.34"]
    assert assert_public_host("example.com") == ["93.184.216.34"]


def test_safe_url_validates_and_returns_canonical_form(monkeypatch):
    def _public(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _public)
    assert safe_url("https://Example.com/a#x") == "https://example.com/a"


# ---------------------------------------------------------------------------
# DNS rebinding: pin, then connect to the PINNED address
# ---------------------------------------------------------------------------


def test_pin_captures_the_validated_addresses(monkeypatch):
    def _public(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _public)
    target = pin("https://example.com/a")
    assert isinstance(target, PinnedTarget)
    assert target.addresses == ("93.184.216.34",)
    assert target.host == "example.com"


def test_pinned_transport_rewrites_to_the_literal_address():
    """The request must go to the pinned IP, with Host + SNI preserved."""
    from app.core.fetch import PinnedHTTPTransport

    target = PinnedTarget(url="https://example.com/a", host="example.com",
                         addresses=("93.184.216.34",))
    transport = PinnedHTTPTransport(target)
    original = httpx.Request("GET", "https://example.com/a?q=1")
    rewritten = transport._rewrite(original)
    assert str(rewritten.url) == "https://93.184.216.34/a?q=1"
    # the Host header and TLS SNI keep the original hostname so certificate
    # validation and virtual hosting still work
    assert rewritten.headers["Host"] == "example.com"
    assert rewritten.extensions["sni_hostname"] == "example.com"


def test_pinned_transport_preserves_an_explicit_port():
    from app.core.fetch import PinnedHTTPTransport

    target = PinnedTarget(url="https://example.com:8443/a", host="example.com",
                         addresses=("93.184.216.34",))
    rewritten = PinnedHTTPTransport(target)._rewrite(
        httpx.Request("GET", "https://example.com:8443/a"))
    assert str(rewritten.url) == "https://93.184.216.34:8443/a"
    assert rewritten.headers["Host"] == "example.com:8443"


def test_a_rebinding_resolver_cannot_swap_the_address_between_check_and_use(
    monkeypatch,
):
    """The whole point of pinning: the 2nd DNS answer is never consulted.

    The resolver answers public on the first query (validation) and private on
    the second (connect). Because the transport connects to the address that
    validation already approved, the second answer is irrelevant.
    """
    from app.core.fetch import PinnedHTTPTransport

    answers = iter([
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    ])
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: next(answers))

    target = pin("https://rebind.example/a")          # 1st answer: public
    assert target.addresses == ("93.184.216.34",)
    # a connect step that re-resolves would get 127.0.0.1; the pinned transport
    # never re-resolves, so the rewrite still targets the validated address
    rewritten = PinnedHTTPTransport(target)._rewrite(
        httpx.Request("GET", "https://rebind.example/a"))
    assert "93.184.216.34" in str(rewritten.url)
    assert "127.0.0.1" not in str(rewritten.url)


# ---------------------------------------------------------------------------
# redirects: re-validated at every hop
# ---------------------------------------------------------------------------


class _StubTransport(httpx.MockTransport):
    """Returns a scripted response per URL, with no real connection."""

    def __init__(self, script: dict) -> None:
        self._script = script
        self.seen: list[str] = []
        super().__init__(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        for url, response in self._script.items():
            if url in str(request.url):
                return response
        return httpx.Response(404, text="no stub")


def _stub_guard(monkeypatch, mapping: dict[str, str]) -> None:
    """Force ``pin`` to approve/reject per host without DNS."""
    real = pin

    def _fake_pin(url):
        scheme, host, canonical = split_url(url)
        verdict = mapping.get(host, "allow")
        if verdict == "deny":
            raise NetGuardError(f"host {host!r} is not a public address")
        if verdict == "resolve_private":
            raise NetGuardError(
                f"host {host!r} resolved to 10.0.0.1 (not a public address)")
        return PinnedTarget(url=canonical, host=host, addresses=("93.184.216.34",))

    monkeypatch.setattr("app.core.fetch.pin", _fake_pin)
    _ = real


def test_redirect_to_a_private_host_aborts_the_fetch(monkeypatch):
    """A public URL must not be able to bounce the fetch inward."""
    from app.core import fetch as fetch_module

    _stub_guard(monkeypatch, {"public.example": "allow",
                              "internal.example": "deny"})
    script = {"/a": httpx.Response(302, headers={"Location": "http://internal.example/b"})}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    with pytest.raises(NetGuardError) as caught:
        fetch_module.fetch_public_url("https://public.example/a")
    assert "internal.example" in str(caught.value)


def test_redirect_to_a_hostname_resolving_private_aborts(monkeypatch):
    from app.core import fetch as fetch_module

    _stub_guard(monkeypatch, {"public.example": "allow",
                              "rebind.example": "resolve_private"})
    script = {"/a": httpx.Response(302,
                                  headers={"Location": "https://rebind.example/b"})}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    with pytest.raises(NetGuardError):
        fetch_module.fetch_public_url("https://public.example/a")


def test_relative_redirect_is_resolved_against_the_current_url(monkeypatch):
    from app.core import fetch as fetch_module

    _stub_guard(monkeypatch, {"public.example": "allow"})
    script = {"/a": httpx.Response(302, headers={"Location": "/b"}),
              "/b": httpx.Response(200, text="final body")}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    result = fetch_module.fetch_public_url("https://public.example/a")
    assert result.status_code == 200
    assert result.text == "final body"
    # the hop was re-pinned, so BOTH urls are validated
    assert len(result.hops) == 2


def test_redirect_loop_is_bounded(monkeypatch):
    from app.core import fetch as fetch_module

    _stub_guard(monkeypatch, {"public.example": "allow"})
    script = {"/a": httpx.Response(302, headers={"Location": "/a"})}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    with pytest.raises(NetGuardError) as caught:
        fetch_module.fetch_public_url("https://public.example/a")
    assert "too many redirects" in str(caught.value)
    assert MAX_REDIRECTS >= 3


def test_redirect_without_location_is_an_error(monkeypatch):
    from app.core import fetch as fetch_module

    _stub_guard(monkeypatch, {"public.example": "allow"})
    script = {"/a": httpx.Response(302)}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    with pytest.raises(NetGuardError):
        fetch_module.fetch_public_url("https://public.example/a")


def test_fetch_revalidates_on_every_hop(monkeypatch):
    """Assert the invariant directly: pin() is called once per hop."""
    from app.core import fetch as fetch_module

    calls: list[str] = []
    real_split = split_url

    def _counting_pin(url):
        calls.append(url)
        scheme, host, canonical = real_split(url)
        return PinnedTarget(url=canonical, host=host,
                            addresses=("93.184.216.34",))

    monkeypatch.setattr(fetch_module, "pin", _counting_pin)
    script = {"/a": httpx.Response(302, headers={"Location": "/b"}),
              "/b": httpx.Response(302, headers={"Location": "/c"}),
              "/c": httpx.Response(200, text="done")}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    fetch_module.fetch_public_url("https://public.example/a")
    assert len(calls) == 3, calls


# ---------------------------------------------------------------------------
# config-time check never touches the resolver
# ---------------------------------------------------------------------------


def test_literal_host_check_makes_no_dns_call(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("the resolver must not be used at config time")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(NetGuardError):
        reject_unsafe_literal_host("169.254.169.254")
    # a hostname is simply not checked here (no resolution)
    assert reject_unsafe_literal_host("example.com") is None
    assert reject_unsafe_literal_host("8.8.8.8") is None


# ---------------------------------------------------------------------------
# scope boundary: what this does NOT claim to protect
# ---------------------------------------------------------------------------


def test_the_guard_does_not_claim_to_protect_third_party_fetches():
    """A URL Meta fetches is out of scope, and that must stay documented.

    The Threads container takes ``image_url``/``video_url`` which META's
    infrastructure retrieves. This module cannot validate what Meta's network
    does with it, so the Threads publisher keeps its own host-level check and
    neither module claims the other's coverage.
    """
    from app.providers.publishers import threads as threads_module

    source = threads_module.__doc__ or ""
    assert "publicly" in source.lower() or "public" in source.lower()
    # the publisher's own check still exists and still refuses a private host
    with pytest.raises(threads_module.ThreadsError):
        threads_module._public_media_url("http://10.1.2.3/a.jpg")
    # The BEHAVIOURAL distinction: the publisher's check is host-based and makes
    # NO DNS call, so a hostname pointing inward is NOT detected there. That is
    # exactly why the shared guard exists, and why neither module may claim the
    # other's coverage.
    def _boom(*args, **kwargs):
        raise AssertionError(
            "the Threads publisher must not resolve DNS: Meta fetches the URL, "
            "not YMONEY")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        # a hostname that resolves inward passes the publisher's host check
        assert threads_module._public_media_url(
            "https://rebind.example/a.jpg").endswith("a.jpg")
    finally:
        monkeypatch.undo()
    # ...while the shared guard DOES resolve and refuses it
    with pytest.raises(NetGuardError):
        resolve_public_addresses("rebind.example")


def test_netguard_docstring_states_the_scope_boundary():
    from app.core import netguard

    doc = netguard.__doc__ or ""
    assert "YMONEY" in doc
    assert "third party" in doc.lower()


# ---------------------------------------------------------------------------
# every resolution failure must fail closed AS A NetGuardError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [
    socket.gaierror("no such host"),
    TimeoutError("timed out"),
    OSError("resolver exploded"),
    UnicodeError("bad hostname"),
])
def test_any_resolution_failure_fails_closed_as_a_netguard_error(
    monkeypatch, error
):
    """A timeout or OSError must not escape as a raw socket exception.

    Only ``socket.gaierror`` was converted, so a resolver timeout propagated
    out of a function whose contract is to fail closed as ``NetGuardError``.
    """
    def _boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(NetGuardError) as caught:
        resolve_public_addresses("example.com")
    assert "cannot resolve" in str(caught.value)


def test_a_private_address_reports_itself_not_an_invalid_address(monkeypatch):
    """NetGuardError subclasses ValueError, so a broad except can swallow it."""
    def _private(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _private)
    with pytest.raises(NetGuardError) as caught:
        resolve_public_addresses("rebind.example")
    message = str(caught.value)
    assert "not a public address" in message
    assert "invalid address" not in message


def test_a_304_is_not_reported_as_a_broken_redirect(monkeypatch):
    """304 Not Modified is not a redirect and must reach the caller."""
    from app.core import fetch as fetch_module

    def _allow(url):
        scheme, host, canonical = split_url(url)
        return PinnedTarget(url=canonical, host=host,
                            addresses=("93.184.216.34",))

    monkeypatch.setattr(fetch_module, "pin", _allow)
    script = {"/a": httpx.Response(304, headers={"ETag": "x"})}
    monkeypatch.setattr(fetch_module, "_client_for",
                        lambda target, timeout: httpx.Client(
                            transport=_StubTransport(script),
                            follow_redirects=False))
    result = fetch_module.fetch_public_url("https://public.example/a")
    assert result.status_code == 304
