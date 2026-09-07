"""Pexels image provider tests — HTTP boundary fully mocked (no network)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers.images import ImageProviderError, reset_image_provider
from app.providers.images_pexels import PexelsImageProvider


# ---------------------------------------------------------------- query reduction

def test_prompt_to_query_stops_stopwords_and_caps_words():
    q = PexelsImageProvider._prompt_to_query(
        "A cinematic wide shot of a busy city street at night, neon lights"
    )
    # stopwords + scene words removed, capped at 6 keywords
    assert q == "cinematic busy city street night neon"


def test_prompt_to_query_falls_back_to_prompt_head_when_all_stopwords():
    q = PexelsImageProvider._prompt_to_query("the of and")
    assert q == "the of and"[:60]


# ---------------------------------------------------------------- size selection

def test_pick_src_prefers_landscape_then_falls_back():
    assert PexelsImageProvider._pick_src(
        {"src": {"landscape": "http://x/l", "large": "http://x/g"}}
    ) == "http://x/l"
    assert PexelsImageProvider._pick_src(
        {"src": {"large2x": "http://x/l2", "original": "http://x/o"}}
    ) == "http://x/l2"
    assert PexelsImageProvider._pick_src({"src": {}}) == ""


# ---------------------------------------------------------------- generation

class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


_SEARCH_PAYLOAD = {
    "photos": [
        {"id": 1, "photographer": "Ada", "url": "https://pexels.com/p1",
         "src": {"landscape": "https://img.example/1.jpg"}},
        {"id": 2, "photographer": "Bob", "url": "https://pexels.com/p2",
         "src": {"landscape": "https://img.example/2.jpg"}},
    ]
}


def test_generate_downloads_n_images_and_records_attribution():
    provider = PexelsImageProvider(api_key="test-key")

    def fake_get(url, **kwargs):
        if url.startswith("https://api.pexels.com/v1/search"):
            return _FakeResponse(_SEARCH_PAYLOAD)
        return _FakeResponse({})  # unused for image bytes path

    class _FakeBytesResponse:
        status_code = 200
        content = b"\xff\xd8\xff" + b"x" * 4096  # > 2048 bytes

        def raise_for_status(self):
            pass

    with patch("httpx.get", side_effect=lambda url, **kw: (
        _FakeResponse(_SEARCH_PAYLOAD) if "api.pexels.com" in url
        else _FakeBytesResponse()
    )):
        out = provider.generate("city street night", n=2)

    assert len(out) == 2
    assert all(len(b) > 2048 for b in out)
    assert [a["photographer"] for a in provider.last_attribution] == ["Ada", "Bob"]


def test_generate_raises_without_api_key():
    provider = PexelsImageProvider(api_key="")
    try:
        provider.generate("anything")
        raised = False
    except ImageProviderError as exc:
        raised = True
        assert "no API key" in str(exc)
    assert raised


def test_generate_raises_when_no_photos_match():
    provider = PexelsImageProvider(api_key="test-key")
    with patch("httpx.get", return_value=_FakeResponse({"photos": []})):
        try:
            provider.generate("zzz unsearchable")
            raised = False
        except ImageProviderError as exc:
            raised = True
            assert "no photos" in str(exc)
    assert raised


def test_healthy_true_on_200_and_false_without_key():
    provider = PexelsImageProvider(api_key="k")
    with patch("httpx.get", return_value=_FakeResponse({}, status=200)):
        assert provider.healthy() is True
    with patch("httpx.get", side_effect=RuntimeError("boom")):
        assert provider.healthy() is False


# ---------------------------------------------------------------- factory wiring

def test_factory_returns_pexels_provider(monkeypatch):
    from app.core import config
    from app.providers import images as images_mod

    monkeypatch.setattr(config.settings, "image_provider", "pexels", raising=False)
    monkeypatch.setattr(config.settings, "pexels_api_key", "factory-key", raising=False)
    # no DB credential override
    monkeypatch.setattr(
        "app.services.provider_settings.get_credential",
        lambda key: ("", None),
    )
    images_mod.reset_image_provider()
    try:
        provider = images_mod.get_image_provider()
        assert isinstance(provider, PexelsImageProvider)
        assert provider.api_key == "factory-key"
    finally:
        reset_image_provider()
