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
    """YouTube Data API v3 public statistics + Analytics API when authed."""

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
        stats = PostStats(views=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)),
                         comments=int(st.get("commentCount", 0)))
        # YouTube Analytics API: watch time + avg view duration (needs OAuth).
        if token:
            try:
                ar = httpx.get(
                    "https://youtubeanalytics.googleapis.com/v2/reports",
                    params={
                        "ids": "channel==MINE",
                        "startDate": "2020-01-01",
                        "endDate": "2030-01-01",
                        "metrics": "estimatedMinutesWatched,averageViewDuration,annotationClickThroughRate",
                        "filters": f"video=={video_id}",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                )
                if ar.status_code == 200:
                    rows = ar.json().get("rows") or []
                    if rows and rows[0]:
                        stats.watch_time_seconds = float(rows[0][0] or 0) * 60.0
                        stats.avg_view_duration_seconds = float(rows[0][1] or 0) / 1000.0
                        if len(rows[0]) > 2 and rows[0][2] is not None:
                            stats.ctr = float(rows[0][2])
                        if stats.views and stats.avg_view_duration_seconds:
                            stats.completion_rate = min(
                                stats.avg_view_duration_seconds / 30.0, 1.0
                            )
            except Exception:
                pass
        return stats


class TikTokAnalyticsProvider(BaseAnalyticsProvider):
    """TikTok Display API video query (views/likes/comments/shares)."""

    platform = "tiktok"
    API = "https://open.tiktokapis.com/v2"

    def fetch_stats(self, post: dict, account: dict) -> PostStats:
        import httpx

        token = account.get("access_token") or ""
        if not token:
            raise PermissionError("TikTok analytics requires a connected account")
        video_id = post.get("remote_post_id") or ""
        if not video_id:
            raise ValueError("missing remote_post_id for tiktok post")
        resp = httpx.post(
            f"{self.API}/video/query/",
            headers={"Authorization": f"Bearer {token}"},
            json={"filters": {"video_ids": [video_id]},
                  "fields": ["id", "like_count", "comment_count", "share_count", "view_count"]},
            timeout=20,
        )
        resp.raise_for_status()
        videos = (resp.json().get("data") or {}).get("videos") or []
        if not videos:
            return PostStats()
        v = videos[0]
        views = int(v.get("view_count", 0))
        return PostStats(
            views=views,
            likes=int(v.get("like_count", 0)),
            comments=int(v.get("comment_count", 0)),
            shares=int(v.get("share_count", 0)),
        )


class MetaAnalyticsProvider(BaseAnalyticsProvider):
    """Facebook Reels + Instagram media insights via Graph API."""

    platform = "meta"
    GRAPH = "https://graph.facebook.com/v21.0"

    def fetch_stats(self, post: dict, account: dict) -> PostStats:
        import httpx

        token = account.get("access_token") or ""
        if not token:
            raise PermissionError("Meta analytics requires a connected account")
        media_id = post.get("remote_post_id") or ""
        if not media_id:
            raise ValueError("missing remote_post_id for meta post")
        platform = post.get("platform", "facebook")
        if platform == "instagram":
            resp = httpx.get(
                f"{self.GRAPH}/{media_id}/insights",
                params={"metric": "plays,likes,comments,saves,shares,reach",
                        "access_token": token},
                timeout=20,
            )
        else:
            resp = httpx.get(
                f"{self.GRAPH}/{media_id}/video_insights",
                params={"metric": "post_video_views,post_video_likes,post_video_comments",
                        "access_token": token},
                timeout=20,
            )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        vals = {}
        for row in data:
            name = row.get("name", "")
            values = row.get("values") or []
            if values:
                vals[name] = values[-1].get("value", 0)
        if platform == "instagram":
            return PostStats(
                views=int(vals.get("plays", vals.get("reach", 0))),
                likes=int(vals.get("likes", 0)),
                comments=int(vals.get("comments", 0)),
                shares=int(vals.get("shares", 0)),
                saves=int(vals.get("saves", 0)),
            )
        return PostStats(
            views=int(vals.get("post_video_views", 0)),
            likes=int(vals.get("post_video_likes", 0)),
            comments=int(vals.get("post_video_comments", 0)),
        )


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
    if platform == "tiktok":
        return TikTokAnalyticsProvider()
    if platform in ("facebook", "instagram"):
        return MetaAnalyticsProvider()
    raise AnalyticsNotConfigured(platform)
