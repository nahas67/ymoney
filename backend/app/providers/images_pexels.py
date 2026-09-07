"""Pexels stock-photo provider — real photography for scene visuals.

Unlike generative providers, Pexels returns real stock photos matching a
keyword query. The generative-style prompt is reduced to keywords (the
Pexels search engine matches subject words, not sentences). Every download
is a real photograph; attribution data is embedded in the provider name and
returned via `last_attribution` for callers that want to credit the
photographer (Pexels license requires no attribution but appreciates it).

API: GET https://api.pexels.com/v1/search with `Authorization: <key>`.
"""

from __future__ import annotations

import re

from app.providers.images import BaseImageProvider, ImageProviderError


class PexelsImageProvider(BaseImageProvider):
    name = "pexels"
    is_mock = False

    _STOPWORDS = {
        "a", "an", "the", "of", "in", "on", "at", "for", "with", "and", "or",
        "scene", "shot", "style", "photo", "image", "picture", "rendering",
        "keywords", "close-up", "wide", "landscape", "portrait",
    }

    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key or ""
        self.timeout = timeout
        self.last_attribution: list[dict] = []

    @staticmethod
    def _prompt_to_query(prompt: str, max_words: int = 6) -> str:
        """Reduce a generative prompt to search keywords."""
        words = re.findall(r"[a-zA-Z0-9']+", prompt.lower())
        kept = [w for w in words if w not in PexelsImageProvider._STOPWORDS]
        return " ".join(kept[:max_words]) or prompt[:60]

    @staticmethod
    def _pick_src(photo: dict) -> str:
        """Choose the best size URL: landscape renders want wide images."""
        src = photo.get("src") or {}
        return (
            src.get("landscape")
            or src.get("large2x")
            or src.get("large")
            or src.get("original")
            or ""
        )

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import httpx

        if not self.api_key:
            raise ImageProviderError("pexels: no API key configured (PEXELS_API_KEY)")
        query = self._prompt_to_query(prompt)
        orientation = "landscape"  # 16:9-ish scenes; portrait handled by ffmpeg crop
        per_page = max(1, min(n * 3, 15))

        try:
            resp = httpx.get(
                "https://api.pexels.com/v1/search",
                params={"query": query, "per_page": per_page, "orientation": orientation},
                headers={"Authorization": self.api_key},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise ImageProviderError(f"pexels search failed: {type(exc).__name__}: {exc}") from exc

        photos = data.get("photos") or []
        if not photos:
            raise ImageProviderError(f"pexels: no photos for query {query!r}")

        out: list[bytes] = []
        self.last_attribution = []
        for photo in photos:
            if len(out) >= max(1, n):
                break
            url = self._pick_src(photo)
            if not url:
                continue
            try:
                dl = httpx.get(url, timeout=60.0, follow_redirects=True)
                dl.raise_for_status()
            except Exception as exc:
                raise ImageProviderError(f"pexels download failed: {exc}") from exc
            if len(dl.content) < 2048:
                raise ImageProviderError("pexels image suspiciously small")
            out.append(dl.content)
            self.last_attribution.append({
                "photographer": photo.get("photographer", ""),
                "photo_url": photo.get("url", ""),
                "pexels_id": photo.get("id"),
            })
        if not out:
            raise ImageProviderError("pexels returned no usable images")
        return out

    def healthy(self) -> bool:
        import httpx

        if not self.api_key:
            return False
        try:
            resp = httpx.get(
                "https://api.pexels.com/v1/curated",
                params={"per_page": 1},
                headers={"Authorization": self.api_key},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception:
            return False
