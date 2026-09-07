"""NewsData.io trend source tests — HTTP boundary mocked, no network."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.providers.trends import NewsDataSource, TrendSourceError, create_source


def _resp(payload: dict, status: int = 200):
    class _R:
        def __init__(self):
            self.status_code = status
            self.text = str(payload)
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    return _R()


_PAYLOAD = {
    "status": "success",
    "results": [
        {
            "article_id": "abc_123",
            "title": "New AI tool changes video editing workflows",
            "link": "https://example.com/a1",
            "pubDate": "2026-09-05 09:30:00",
            "category": ["technology", "artificial intelligence"],
            "keywords": ["ai", "video"],
            "sentiment": "positive",
            "source_id": "techdaily",
            "source_priority": 1,
            "language": "en",
        },
        {
            "article_id": "def_456",
            "title": "  ",
            "link": "https://example.com/a2",
        },
    ],
}


def test_fetch_maps_articles_to_candidates():
    src = NewsDataSource(api_key="k", timeframe=24)
    with patch("httpx.get", return_value=_resp(_PAYLOAD)):
        items = src.fetch(niche="ai tools", limit=10)
    assert len(items) == 1  # blank-title article skipped
    c = items[0]
    assert c.topic.startswith("New AI tool")
    assert c.source == "newsdata"
    assert c.external_ref == "https://example.com/a1"
    assert "technology" in c.raw["categories"]
    assert c.raw["sentiment"] == "positive"
    assert c.raw["publisher"] == "techdaily"
    assert 0.0 <= (c.velocity_hint or 0) <= 1.0
    assert c.volume_hint == 1.0  # source_priority 1 → top authority


def test_fetch_uses_niche_as_default_query():
    src = NewsDataSource(api_key="k")
    with patch("httpx.get", return_value=_resp(_PAYLOAD)) as mock_get:
        src.fetch(niche="ai tools and technology trends", limit=5)
    params = mock_get.call_args.kwargs["params"]
    assert params["q"] == "ai tools and technology trends"
    assert params["apikey"] == "k"
    assert params["size"] == "5"
    assert params["language"] == "en"
    assert params["timeframe"] == "24"


def test_fetch_raises_without_api_key():
    src = NewsDataSource(api_key="")
    with pytest.raises(TrendSourceError, match="no API key"):
        src.fetch(niche="x", limit=5)


def test_fetch_raises_on_api_error_payload():
    src = NewsDataSource(api_key="k")
    with patch("httpx.get", return_value=_resp({"status": "error", "results": {"message": "bad key"}})):
        with pytest.raises(TrendSourceError, match="NewsData.io error"):
            src.fetch(niche="x", limit=5)


def test_fetch_raises_when_no_usable_articles():
    src = NewsDataSource(api_key="k")
    with patch("httpx.get", return_value=_resp({"status": "success", "results": []})):
        with pytest.raises(TrendSourceError, match="no usable articles"):
            src.fetch(niche="x", limit=5)


def test_factory_reads_credential(monkeypatch):
    from app.services import provider_settings as ps

    monkeypatch.setattr(ps, "get_credential", lambda key: ("factory-key", "db"))
    src = create_source("newsdata", {"timeframe": 12, "categories": "technology"})
    assert isinstance(src, NewsDataSource)
    assert src.api_key == "factory-key"
    assert src.timeframe == 12
    assert src.categories == "technology"


def test_pubdate_parser_handles_known_formats():
    ts = NewsDataSource._parse_pubdate if hasattr(NewsDataSource, "_parse_pubdate") else None
    from app.providers.trends import _parse_pubdate

    assert _parse_pubdate("2026-09-05 09:30:00") is not None
    assert _parse_pubdate("2026-09-05T09:30:00") is not None
    assert _parse_pubdate("garbage") is None
    assert _parse_pubdate(None) is None
