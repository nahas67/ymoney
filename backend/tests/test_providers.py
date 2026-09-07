"""Provider layer tests: mock video engine, mock publisher, trend sources."""

import pytest

from app.providers.trends import create_source


def test_trend_registry_creates_sources():
    from app.providers.trends import RedditTrendSource

    assert create_source("google_trends", {"geo": "DE"}).geo == "DE"
    assert isinstance(create_source("reddit", {}), RedditTrendSource)
    from app.providers.trends import TrendSourceError

    with pytest.raises(TrendSourceError):
        create_source("nope", {})


def test_publishing_blocked_raises(monkeypatch):
    import app.providers.publishers.factory as pfactory
    from app.core.config import settings as cfg
    from app.providers.publishers.factory import PublishingBlocked, get_publisher

    monkeypatch.setattr(cfg, "mock_publishing", False)
    monkeypatch.setattr(pfactory, "relay_ready", lambda: False)
    monkeypatch.delitem(pfactory._registry, "tiktok", raising=False)

    with pytest.raises(PublishingBlocked):
        get_publisher("tiktok", has_account=False)


def test_parse_traffic_normalization():
    from app.providers.trends import _parse_traffic

    assert _parse_traffic(None) is None
    assert _parse_traffic("200K+") == pytest.approx(0.2)
    assert _parse_traffic("1M+") == pytest.approx(1.0)
