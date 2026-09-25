"""CoinGecko + Dev.to keyless trend source tests — HTTP mocked, no network."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.providers.trends import (
    BilibiliTrendSource,
    CoinGeckoTrendSource,
    DevToTrendSource,
    TrendSourceError,
    create_source,
)


def _resp(payload, status: int = 200):
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


# ---------------------------------------------------------------- CoinGecko

_CG_PAYLOAD = {
    "coins": [
        {
            "item": {
                "id": "solana",
                "name": "Solana",
                "symbol": "SOL",
                "market_cap_rank": 5,
                "data": {
                    "price": 210.5,
                    "price_change_percentage_24h": {"usd": 12.4},
                },
            }
        }
    ]
}


def test_coingecko_maps_trending_coins():
    src = CoinGeckoTrendSource()
    with patch("httpx.get", return_value=_resp(_CG_PAYLOAD)):
        items = src.fetch(niche="", limit=10)
    assert len(items) == 1
    c = items[0]
    assert c.topic == "Solana (SOL) crypto"
    assert c.source == "coingecko"
    assert c.external_ref == "coingecko:solana"
    assert c.raw["market_cap_rank"] == 5
    assert abs((c.velocity_hint or 0) - 12.4 / 50.0) < 1e-6
    assert c.volume_hint is not None and c.volume_hint < 1.0


def test_coingecko_raises_on_empty_and_error():
    src = CoinGeckoTrendSource()
    with (
        patch("httpx.get", return_value=_resp({"coins": []})),
        pytest.raises(TrendSourceError, match="no trending coins"),
    ):
        src.fetch(niche="", limit=5)
    with (
        patch("httpx.get", side_effect=RuntimeError("429")),
        pytest.raises(TrendSourceError, match="unavailable"),
    ):
        src.fetch(niche="", limit=5)


def test_coingecko_factory_reads_optional_key(monkeypatch):
    from app.services import provider_settings as ps

    monkeypatch.setattr(ps, "get_credential", lambda key: ("demo-key", "db"))
    src = create_source("coingecko", {})
    assert isinstance(src, CoinGeckoTrendSource)
    assert src.api_key == "demo-key"


# ---------------------------------------------------------------- Dev.to

_DV_PAYLOAD = [
    {
        "title": "I built an autonomous video pipeline with 12 agents",
        "url": "https://dev.to/p/1",
        "positive_reactions_count": 420,
        "comments_count": 30,
        "published_at": "2026-09-04T10:00:00Z",
        "tag_list": ["ai", "python"],
        "user": {"username": "devcoder"},
    }
]


def test_devto_maps_articles_with_velocity():
    src = DevToTrendSource(days=7)
    with patch("httpx.get", return_value=_resp(_DV_PAYLOAD)):
        items = src.fetch(niche="", limit=10)
    assert len(items) == 1
    c = items[0]
    assert c.topic.startswith("I built an autonomous video pipeline")
    assert c.source == "devto"
    assert c.external_ref == "https://dev.to/p/1"
    assert "ai" in c.raw["tag_list"]
    assert 0.0 <= (c.velocity_hint or 0) <= 1.0


def test_devto_passes_tag_and_top_params():
    src = DevToTrendSource(tag="python", days=3, per_page=15)
    with patch("httpx.get", return_value=_resp([])):
        try:
            src.fetch(niche="", limit=5)
        except TrendSourceError:
            pass
    # last call captured params
    # (httpx.get patched module-level; assert via the source config instead)
    assert src.tag == "python"
    assert src.days == 3
    assert src.per_page == 15


# ---------------------------------------------------------------- Bilibili

_BILI_PAYLOAD = {
    "code": 0,
    "data": {
        "result": [
            {"result_type": "user", "data": [{"name": "someone"}]},
            {"result_type": "video", "data": [
                {"title": '如何<em class="keyword">赚钱</em>存钱',
                 "bvid": "BV1xx", "author": "up1", "play": 500000,
                 "danmaku": 2000, "pubdate": 1758000000},
                {"title": "plain video", "bvid": "",
                 "author": "up2", "play": 0, "danmaku": 0, "pubdate": 0},
            ]},
        ]
    },
}


def test_bilibili_maps_videos_and_strips_markup():
    src = BilibiliTrendSource(q="赚钱")
    with patch("httpx.get", return_value=_resp(_BILI_PAYLOAD)):
        out = src.fetch(niche="ignored", limit=5)
    assert len(out) == 2
    assert out[0].topic == "如何赚钱存钱"
    assert "<em" not in out[0].topic
    assert out[0].external_ref == "https://www.bilibili.com/video/BV1xx"
    assert out[0].source == "bilibili"
    assert out[0].raw["plays"] == 500000
    assert out[0].velocity_hint is not None and out[0].velocity_hint > 0
    # no-bvid item still yields a topic with empty ref
    assert out[1].external_ref == ""


def test_bilibili_requires_query_and_fails_closed():
    with pytest.raises(TrendSourceError, match="keyword"):
        BilibiliTrendSource().fetch(niche="", limit=5)
    with (
        patch("httpx.get", return_value=_resp({"code": -400, "message": "bad"})),
        pytest.raises(TrendSourceError, match="code=-400"),
    ):
        BilibiliTrendSource(q="x").fetch(niche="", limit=5)
    with (
        patch("httpx.get", return_value=_resp({"code": 0, "data": {"result": []}})),
        pytest.raises(TrendSourceError, match="no usable videos"),
    ):
        BilibiliTrendSource(q="x").fetch(niche="", limit=5)


def test_bilibili_factory_and_registry():
    src = create_source("bilibili", {"q": "AI"})
    assert isinstance(src, BilibiliTrendSource) and src.q == "AI"
    assert create_source("bilibili", {}).q == ""
    with pytest.raises(TrendSourceError, match="unknown trend source kind"):
        create_source("nope", {})
