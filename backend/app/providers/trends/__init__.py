"""Trend source abstraction.

Pluggable providers that respect platform terms: only official/public feeds
and APIs are used; no scraping designed to evade restrictions.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx
from loguru import logger
from datetime import UTC


@dataclass
class TrendCandidate:
    topic: str
    source: str
    external_ref: str = ""
    raw: dict = field(default_factory=dict)
    # Optional provider hints used by scoring (0..1 scales where known)
    velocity_hint: float | None = None
    volume_hint: float | None = None


class TrendSourceError(Exception):
    pass


class BaseTrendSource(ABC):
    kind: str = "base"
    name: str = "base"

    @abstractmethod
    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        ...


# ---------------------------------------------------------------------------
# Google Trends — public RSS feed (designed for consumption, no key needed)
# ---------------------------------------------------------------------------


class GoogleTrendsSource(BaseTrendSource):
    kind = "google_trends"
    name = "Google Trends"

    RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"

    def __init__(self, geo: str = "US"):
        self.geo = geo

    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        try:
            resp = httpx.get(
                self.RSS_URL.format(geo=self.geo),
                timeout=15,
                headers={"User-Agent": "YMONEY/1.0 (+trends reader)"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            items: list[TrendCandidate] = []
            for item in root.iter("item"):
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                news_items = []
                for ne in item.iter("item"):
                    nt = ne.find("title")
                    pic = ne.find("picture")
                    url = ne.find("url") or ne.find("link")
                    if nt is not None and nt.text:
                        news_items.append(
                            {
                                "title": nt.text,
                                "image": pic.text if pic is not None and pic.text else "",
                                "url": (url.text if url is not None and url.text else ""),
                            }
                        )
                approx_traffic = item.find("ht:approx_traffic")
                items.append(
                    TrendCandidate(
                        topic=title_el.text.strip()[:300],
                        source=self.kind,
                        external_ref=f"google_trends:{self.geo}",
                        raw={"news": news_items[:3], "geo": self.geo},
                        volume_hint=_parse_traffic(approx_traffic.text if approx_traffic is not None else None),
                    )
                )
                if len(items) >= limit * 2:
                    break
            return items[:limit]
        except Exception as exc:
            logger.warning(f"google trends fetch failed: {exc}")
            raise TrendSourceError(f"Google Trends unavailable: {exc}") from exc


def _parse_traffic(text_value: str | None) -> float | None:
    """'200K+' -> 200000 normalized 0..1 against ~1M cap."""
    if not text_value:
        return None
    m = re.match(r"([\d.,]+)\s*([KM+]?)", text_value.strip())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"K": 1e3, "M": 1e6}.get(m.group(2), 1.0)
    return min(num * mult / 1e6, 1.0)


# ---------------------------------------------------------------------------
# Reddit — public JSON API with proper identification + rate limiting
# ---------------------------------------------------------------------------


class RedditTrendSource(BaseTrendSource):
    kind = "reddit"
    name = "Reddit"

    HOT_URL = "https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}&raw_json=1"

    def __init__(self, subreddit: str = "all", min_interval_seconds: float = 2.0):
        self.subreddit = subreddit
        import time as _t

        self._last_call = 0.0
        self._min_interval = min_interval_seconds
        self._time = _t.time

    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        wait = self._min_interval - (self._time() - self._last_call)
        if wait > 0:
            import time as _t2

            _t2.sleep(wait)
        try:
            resp = httpx.get(
                self.HOT_URL.format(subreddit=self.subreddit, limit=min(limit, 50)),
                timeout=15,
                headers={"User-Agent": "ymoney-trend-reader/1.0"},
            )
            self._last_call = self._time()
            resp.raise_for_status()
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            out: list[TrendCandidate] = []
            for ch in children:
                d = ch.get("data", {})
                title = (d.get("title") or "").strip()
                if not title:
                    continue
                ups = d.get("ups", 0)
                num_comments = d.get("num_comments", 0)
                created = d.get("created_utc", 0)
                age_h = max((self._time() - created) / 3600.0, 0.5)
                velocity = min(((ups + num_comments * 2) / age_h) / 500.0, 1.0)
                out.append(
                    TrendCandidate(
                        topic=title[:300],
                        source=self.kind,
                        external_ref=d.get("permalink", ""),
                        raw={"subreddit": d.get("subreddit"), "ups": ups},
                        velocity_hint=velocity,
                        volume_hint=min(ups / 10000.0, 1.0),
                    )
                )
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:
            logger.warning(f"reddit fetch failed: {exc}")
            raise TrendSourceError(f"Reddit unavailable: {exc}") from exc



# ---------------------------------------------------------------------------
# Hacker News — Algolia public search API (keyless, official, rate-limit friendly)
# ---------------------------------------------------------------------------


class HackerNewsTrendSource(BaseTrendSource):
    """Keyless social-listening source via the official HN Algolia search API.

    Scores stories by engagement velocity (points + comments per hour of age)
    so fast-moving discussions in the workspace niche surface first. No key,
    no scraping — a public, documented, read-only API.
    """

    kind = "hacker_news"
    name = "Hacker News"

    SEARCH_URL = "https://hn.algolia.com/api/v1/search"

    def __init__(self, tags: str = "story", min_points: int = 20,
                 days: int = 14, use_niche_query: bool = True):
        self.tags = tags
        self.min_points = max(1, min_points)
        self.days = max(1, days)
        self.use_niche_query = use_niche_query

    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        import time as _t

        queries: list[str] = []
        if self.use_niche_query and niche:
            queries = [niche.strip()]
        queries.append("")  # front-page fallback (search-by-date)

        candidates: dict[str, TrendCandidate] = {}
        now = _t.time()
        for q in queries:
            if len(candidates) >= limit:
                break
            params: dict = {
                "tags": self.tags,
                "hitsPerPage": min(max(limit * 2, 20), 100),
                "numericFilters": f"created_at_i>{int(now - self.days * 86400)},points>{self.min_points}",
            }
            if q:
                params["query"] = q
            else:
                params["numericFilters"] = (
                    f"created_at_i>{int(now - self.days * 86400)}"
                )
            try:
                resp = httpx.get(self.SEARCH_URL, params=params, timeout=15,
                                 headers={"User-Agent": "ymoney-trend-reader/1.0"})
                resp.raise_for_status()
                hits = resp.json().get("hits", [])
            except Exception as exc:
                logger.warning(f"hacker_news fetch failed (q={q!r}): {exc}")
                continue
            for h in hits:
                title = (h.get("title") or h.get("story_title") or "").strip()
                if not title:
                    continue
                object_id = h.get("objectID", "")
                if not object_id or object_id in candidates:
                    continue
                points = int(h.get("points") or 0)
                comments = int(h.get("num_comments") or 0)
                created = float(h.get("created_at_i") or now)
                age_h = max((now - created) / 3600.0, 0.5)
                velocity = min(((points + comments * 2) / age_h) / 400.0, 1.0)
                url = h.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
                candidates[object_id] = TrendCandidate(
                    topic=title[:300],
                    source=self.kind,
                    external_ref=url,
                    raw={
                        "points": points,
                        "comments": comments,
                        "age_hours": round(age_h, 1),
                        "author": h.get("author", ""),
                    },
                    velocity_hint=velocity,
                    volume_hint=min(points / 1000.0, 1.0),
                )
                if len(candidates) >= limit:
                    break
        ordered = sorted(
            candidates.values(),
            key=lambda c: (c.velocity_hint or 0),
            reverse=True,
        )
        if not ordered:
            raise TrendSourceError("Hacker News returned no usable stories")
        return ordered[:limit]# ---------------------------------------------------------------------------
# NewsData.io — structured breaking-news API (apiKey, free tier 200 credits/day)
# ---------------------------------------------------------------------------


class NewsDataSource(BaseTrendSource):
    """Structured live-news source via the NewsData.io /latest endpoint.

    Free tier: 200 API credits/day, up to 10 articles per request. One credit
    per request, so a single fetch is trivially cheap. Articles arrive with
    categories, keywords, sentiment, publisher priority and pubDate — richer
    metadata than the keyless sources, feeding explainable scoring with real
    signals (recency from pubDate, category/niche fit, publisher authority).

    Config keys: q (keyword query, defaults to the workspace niche),
    language (comma list, default en), categories (comma list), timeframe
    (hours, default 24), country (comma list), domain (comma list).
    """

    kind = "newsdata"
    name = "NewsData.io"

    LATEST_URL = "https://newsdata.io/api/1/latest"

    def __init__(self, api_key: str = "", q: str = "", language: str = "en",
                 categories: str = "", timeframe: int = 24, country: str = "",
                 domain: str = "", removeduplicates: str = ""):
        self.api_key = (api_key or "").strip()
        self.q = (q or "").strip()
        self.language = (language or "").strip()
        self.categories = (categories or "").strip()
        self.timeframe = max(1, min(int(timeframe or 24), 48))
        self.country = (country or "").strip()
        self.domain = (domain or "").strip()
        self.removeduplicates = (removeduplicates or "").strip()

    def _params(self, niche: str, limit: int) -> dict:
        params: dict = {
            "apikey": self.api_key,
            "size": str(max(1, min(limit, 10))),  # free plan caps at 10/request
        }
        q = self.q or niche
        if q:
            params["q"] = q
        if self.language:
            params["language"] = self.language
        if self.categories:
            params["category"] = self.categories
        if self.timeframe:
            params["timeframe"] = str(self.timeframe)
        if self.country:
            params["country"] = self.country
        if self.domain:
            params["domain"] = self.domain
        if self.removeduplicates:
            params["removeduplicates"] = self.removeduplicates
        return params

    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        if not self.api_key:
            raise TrendSourceError(
                "newsdata: no API key — add newsdata.api_key under Settings → Connections"
            )
        try:
            resp = httpx.get(
                self.LATEST_URL,
                params=self._params(niche, limit),
                timeout=20,
                headers={"User-Agent": "ymoney-trend-reader/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f"newsdata fetch failed: {exc}")
            raise TrendSourceError(f"NewsData.io unavailable: {exc}") from exc
        if data.get("status") != "success":
            raise TrendSourceError(
                f"NewsData.io error: {str(data.get('results') or data)[:150]}"
            )

        import time as _t

        now = _t.time()
        out: list[TrendCandidate] = []
        for art in (data.get("results") or [])[:limit]:
            title = (art.get("title") or "").strip()
            if not title:
                continue
            # velocity: fresh articles score high, decaying over the window
            pub_ts = _parse_pubdate(art.get("pubDate"))
            age_h = max((now - pub_ts) / 3600.0, 0.5) if pub_ts else float(self.timeframe)
            velocity = max(0.0, min(1.0 - (age_h / max(self.timeframe, 1.0)), 1.0))
            # volume: publisher priority (1 = top-tier domain)
            sp = art.get("source_priority")
            volume = 1.0 - min(max((float(sp) - 1) / 999.0, 0.0), 1.0) if sp else None
            out.append(
                TrendCandidate(
                    topic=title[:300],
                    source=self.kind,
                    external_ref=art.get("link") or art.get("article_id", ""),
                    raw={
                        "article_id": art.get("article_id", ""),
                        "description": (art.get("description") or "")[:300],
                        "categories": art.get("category") or [],
                        "keywords": (art.get("keywords") or [])[:8],
                        "sentiment": art.get("sentiment") or "",
                        "publisher": art.get("source_id") or "",
                        "source_priority": sp,
                        "pubDate": art.get("pubDate") or "",
                        "age_hours": round(age_h, 1),
                        "language": art.get("language") or "",
                    },
                    velocity_hint=velocity,
                    volume_hint=volume,
                )
            )
        if not out:
            raise TrendSourceError("NewsData.io returned no usable articles")
        return out


def _parse_pubdate(value: str | None) -> float | None:
    """NewsData pubDate ('2026-09-05 12:34:56' UTC) → epoch seconds."""
    if not value:
        return None
    from datetime import datetime, timezone

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# CoinGecko — keyless crypto market trends (public-apis catalog, no key needed)
# ---------------------------------------------------------------------------


class CoinGeckoTrendSource(BaseTrendSource):
    """Trending crypto coins via the keyless CoinGecko /search/trending API.

    Money-market content is a core short-form vertical; CoinGecko's trending
    list is real market attention (search volume), not editorial. Velocity is
    derived from the 24h price change, volume from market-cap rank. Optional
    demo key raises rate limits; the keyless endpoint works as-is.
    """

    kind = "coingecko"
    name = "CoinGecko"

    TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or "").strip()

    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        headers = {"User-Agent": "ymoney-trend-reader/1.0"}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key
        try:
            resp = httpx.get(self.TRENDING_URL, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(f"coingecko fetch failed: {exc}")
            raise TrendSourceError(f"CoinGecko unavailable: {exc}") from exc

        out: list[TrendCandidate] = []
        for entry in (data.get("coins") or [])[:limit]:
            item = entry.get("item") or {}
            name = (item.get("name") or "").strip()
            symbol = (item.get("symbol") or "").strip()
            if not name:
                continue
            item_data = item.get("data") or {}
            pct = (item_data.get("price_change_percentage_24h") or {}).get("usd")
            try:
                pct_f = abs(float(pct)) if pct is not None else None
            except (TypeError, ValueError):
                pct_f = None
            rank = item.get("market_cap_rank")
            try:
                rank_i = int(rank) if rank else None
            except (TypeError, ValueError):
                rank_i = None
            out.append(
                TrendCandidate(
                    topic=f"{name} ({symbol}) crypto" if symbol else f"{name} crypto",
                    source=self.kind,
                    external_ref=f"coingecko:{item.get('id', '')}",
                    raw={
                        "coin_id": item.get("id", ""),
                        "symbol": symbol,
                        "market_cap_rank": rank_i,
                        "price_change_24h_usd": pct,
                        "price_usd": item_data.get("price"),
                    },
                    velocity_hint=min((pct_f or 0.0) / 50.0, 1.0),
                    volume_hint=(1.0 - min(max(rank_i - 1, 0) / 100.0, 1.0)) if rank_i else None,
                )
            )
        if not out:
            raise TrendSourceError("CoinGecko returned no trending coins")
        return out


# ---------------------------------------------------------------------------
# Dev.to — keyless tech community articles (public-apis catalog, no key needed)
# ---------------------------------------------------------------------------


class DevToTrendSource(BaseTrendSource):
    """Trending dev-community articles via the keyless Dev.to API.

    Complements Hacker News with practitioner-written posts: tags, positive
    reactions and comment counts give direct audience-interest signals for
    tech/AI topics. `tag` config narrows to a niche tag when one exists;
    otherwise the general top-of-week feed is used and scoring filters fit.
    """

    kind = "devto"
    name = "DEV Community"

    ARTICLES_URL = "https://dev.to/api/articles"

    def __init__(self, tag: str = "", days: int = 7, per_page: int = 30):
        self.tag = (tag or "").strip()
        self.days = max(1, min(int(days or 7), 30))
        self.per_page = max(1, min(int(per_page or 30), 60))

    def fetch(self, niche: str, limit: int) -> list[TrendCandidate]:
        params: dict = {"top": str(self.days), "per_page": str(self.per_page)}
        if self.tag:
            params["tag"] = self.tag
        try:
            resp = httpx.get(
                self.ARTICLES_URL,
                params=params,
                timeout=15,
                headers={"User-Agent": "ymoney-trend-reader/1.0"},
            )
            resp.raise_for_status()
            articles = resp.json()
        except Exception as exc:
            logger.warning(f"devto fetch failed: {exc}")
            raise TrendSourceError(f"Dev.to unavailable: {exc}") from exc

        import time as _t

        now = _t.time()
        out: list[TrendCandidate] = []
        for art in articles or []:
            title = (art.get("title") or "").strip()
            if not title:
                continue
            reactions = int(art.get("positive_reactions_count") or 0)
            comments = int(art.get("comments_count") or 0)
            pub = art.get("published_at") or ""
            try:
                from datetime import datetime

                pub_ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pub_ts = now
            age_h = max((now - pub_ts) / 3600.0, 0.5)
            velocity = min(((reactions + comments * 2) / age_h) / 300.0, 1.0)
            out.append(
                TrendCandidate(
                    topic=title[:300],
                    source=self.kind,
                    external_ref=art.get("url") or "",
                    raw={
                        "tag_list": (art.get("tag_list") or [])[:6],
                        "reactions": reactions,
                        "comments": comments,
                        "age_hours": round(age_h, 1),
                        "author": (art.get("user") or {}).get("username", ""),
                    },
                    velocity_hint=velocity,
                    volume_hint=min(reactions / 1000.0, 1.0),
                )
            )
            if len(out) >= limit:
                break
        if not out:
            raise TrendSourceError("Dev.to returned no usable articles")
        out.sort(key=lambda c: (c.velocity_hint or 0), reverse=True)
        return out


REGISTRY: dict[str, type[BaseTrendSource]] = {
    GoogleTrendsSource.kind: GoogleTrendsSource,
    RedditTrendSource.kind: RedditTrendSource,
    HackerNewsTrendSource.kind: HackerNewsTrendSource,
    NewsDataSource.kind: NewsDataSource,
    CoinGeckoTrendSource.kind: CoinGeckoTrendSource,
    DevToTrendSource.kind: DevToTrendSource,
}


def create_source(kind: str, config: dict | None = None) -> BaseTrendSource:
    cls = REGISTRY.get(kind)
    if not cls:
        raise TrendSourceError(f"unknown trend source kind: {kind}")
    cfg = dict(config or {})
    if cls is GoogleTrendsSource:
        return GoogleTrendsSource(geo=cfg.get("geo", "US"))
    if cls is RedditTrendSource:
        return RedditTrendSource(subreddit=cfg.get("subreddit", "all"))
    if cls is HackerNewsTrendSource:
        return HackerNewsTrendSource(
            min_points=int(cfg.get("min_points", 20)),
            days=int(cfg.get("days", 14)),
            use_niche_query=bool(cfg.get("use_niche_query", True)),
        )
    if cls is NewsDataSource:
        from app.services.provider_settings import get_credential

        key, _src = get_credential("newsdata.api_key")
        if not key:
            from app.core.config import settings as _cfg

            key = _cfg.newsdata_api_key
        return NewsDataSource(
            api_key=key or "",
            q=str(cfg.get("q", "")),
            language=str(cfg.get("language", "en")),
            categories=str(cfg.get("categories", "")),
            timeframe=int(cfg.get("timeframe", 24)),
            country=str(cfg.get("country", "")),
            domain=str(cfg.get("domain", "")),
            removeduplicates=str(cfg.get("removeduplicates", "")),
        )
    if cls is CoinGeckoTrendSource:
        from app.services.provider_settings import get_credential

        cg_key, _cg = get_credential("coingecko.api_key")
        return CoinGeckoTrendSource(api_key=cg_key or "")
    if cls is DevToTrendSource:
        return DevToTrendSource(
            tag=str(cfg.get("tag", "")),
            days=int(cfg.get("days", 7)),
            per_page=int(cfg.get("per_page", 30)),
        )
    raise TrendSourceError(f"source kind '{kind}' has no factory")
