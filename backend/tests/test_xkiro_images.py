"""Tests for the XkiroImageProvider — size snapping + async job polling.

The HTTP boundary is mocked with respx-style monkeypatching of httpx.Client;
no real network access in tests.
"""
from __future__ import annotations

import pytest

from app.providers.images import ImageProviderError, XkiroImageProvider




def test_snap_size_landscape_wide():
    assert XkiroImageProvider._snap_size("1024x576") == "1792x1024"


def test_snap_size_landscape_mild():
    assert XkiroImageProvider._snap_size("1280x1024") == "1536x1024"


def test_snap_size_square():
    assert XkiroImageProvider._snap_size("1024x1024") == "1024x1024"


def test_snap_size_portrait():
    assert XkiroImageProvider._snap_size("576x1024") == "1024x1536"


def test_snap_size_garbage():
    assert XkiroImageProvider._snap_size("whatever") == "1024x1024"


def test_generate_requires_key():
    p = XkiroImageProvider(base_url="https://x.test/v1", api_key="")
    with pytest.raises(ImageProviderError, match="no API key"):
        p.generate("a lighthouse")


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = b""

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(f"{self.status_code}", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    """Scripted responses: submit -> polls -> CDN download."""

    def __init__(self):
        self.calls: list[str] = []
        self.poll_count = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append("POST " + url)
        assert json["model"] == "sensenova/sensenova-u1.5-lite"
        assert json["n"] == 1
        return _FakeResponse({"id": "job-1", "status": "processing"})

    def get(self, url, headers=None, timeout=None, follow_redirects=None, params=None):
        self.calls.append("GET " + url)
        if "/models" in url:
            return _FakeResponse({"data": []})
        if "cdn.test" in url:
            return type("R", (), {"content": b"\x89PNG" + b"x" * 2048, "raise_for_status": lambda s: None})()
        self.poll_count += 1
        if self.poll_count < 2:
            return _FakeResponse({"id": "job-1", "status": "processing"})
        return _FakeResponse({
            "id": "job-1", "status": "succeeded",
            "data": [{"url": "https://cdn.test/img.png"}],
        })

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_generate_polls_until_succeeded(monkeypatch):
    p = XkiroImageProvider(base_url="https://x.test/v1", api_key="k", timeout=5)
    p._POLL_INTERVAL = 0.01
    fake = _FakeClient()
    import httpx as _httpx

    monkeypatch.setattr(_httpx, "Client", lambda timeout=None: fake)
    out = p.generate("a lighthouse", size="1024x576", n=1)
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")
    assert any("/images/generations" in c for c in fake.calls)


def test_generate_raises_on_failed_job(monkeypatch):
    p = XkiroImageProvider(base_url="https://x.test/v1", api_key="k")
    p._POLL_INTERVAL = 0.01

    class _FailClient(_FakeClient):
        def get(self, url, headers=None, timeout=None, follow_redirects=None, params=None):
            if "/models" in url or "cdn" in url:
                return super().get(url)
            return _FakeResponse({"id": "job-1", "status": "failed", "error": {"message": "bad prompt"}})

    fake = _FailClient()
    import httpx as _httpx

    monkeypatch.setattr(_httpx, "Client", lambda timeout=None: fake)
    with pytest.raises(ImageProviderError, match="failed"):
        p.generate("x")


def test_healthy_false_without_key():
    p = XkiroImageProvider(base_url="https://x.test/v1", api_key="")
    assert p.healthy() is False

# ---------------------------------------------------------------------------
# Work 15.9 1: billable lanes need an explicit budget owner.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _owner_for_billable_lanes(billable_workspace):
    """This module drives BILLABLE provider lanes.

    A billable call with no budget owner is now REFUSED before the request
    leaves -- correct product behaviour. These tests opt into a synthetic
    workspace scope explicitly rather than the product growing a loophole.
    """
    yield billable_workspace
