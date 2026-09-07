"""Analytics providers — PRODUCTION ONLY.

Metrics come from platform APIs. When a platform has no configured provider,
the collector records an explicit 'not_configured' state; numbers are never
simulated.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class AnalyticsNotConfigured(Exception):
    def __init__(self, platform: str):
        super().__init__(
            f"{platform}: analytics provider not configured — connect the account "
            "or provide API credentials (Settings → Publishing / Connections)"
        )
        self.platform = platform


@dataclass
class PostStats:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_seconds: float = 0.0
    avg_view_duration_seconds: float = 0.0
    completion_rate: float = 0.0
    ctr: float | None = None
    followers_gained: int = 0


class BaseAnalyticsProvider(abc.ABC):
    platform: str = "base"

    @abc.abstractmethod
    def fetch_stats(self, post: dict, account: dict) -> PostStats:
        ...


class YouTubeAnalyticsProvider(BaseAnalyticsProvider):
    """YouTube Data API v3 public statistics."""

    platform = "youtube"

    def fetch_stats(self, post: dict, account: dict) -> PostStats:
        import httpx

        video_id = post.get("remote_post_id") or ""
        if not video_id:
            raise ValueError("missing remote_post_id for youtube post")
        params = {"part": "statistics", "id": video_id}
        token = account.get("access_token") or ""
        api_key = account.get("api_key") or ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if api_key and not token:
            params["key"] = api_key
            headers = {}
        resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params=params, headers=headers, timeout=20,
        )
        if resp.status_code == 401 and not token:
            raise PermissionError("YouTube analytics requires an OAuth token or API key")
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return PostStats()
        st = items[0].get("statistics", {})
        return PostStats(views=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)),
                         comments=int(st.get("commentCount", 0)))


class MockAnalyticsProvider(BaseAnalyticsProvider):
    """Simulation provider — deterministic, clearly-labeled metrics.

    Development/simulation ONLY (MOCK_ANALYTICS=true). Numbers are derived
    deterministically from the remote post id so replays are stable, and are
    never mixed with real platform data.
    """

    platform = "mock"

    def fetch_stats(self, post: dict, account: dict) -> PostStats:
        seed = sum(ord(c) for c in str(post.get("remote_post_id", "mock")))
        views = 400 + (seed * 37) % 4600
        likes = views // 12
        comments = views // 90
        shares = views // 60
        completion = 0.35 + (seed % 45) / 100.0  # 0.35-0.79
        return PostStats(
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            watch_time_seconds=round(views * 8.5, 1),
            avg_view_duration_seconds=8.5,
            completion_rate=round(completion, 3),
            followers_gained=views // 200,
        )


def get_provider(platform: str) -> BaseAnalyticsProvider:
    from app.core.config import settings

    if settings.mock_analytics:
        return MockAnalyticsProvider()
    if platform == "youtube":
        return YouTubeAnalyticsProvider()
    raise AnalyticsNotConfigured(platform)
