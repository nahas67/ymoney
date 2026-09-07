"""Image provider abstraction — scene image generation.

Providers (selected via IMAGE_PROVIDER):
  pexels        — real stock photography via the Pexels search API. Best when
                  authentic visuals matter more than generated art.
  xkiro         — xKiro async image jobs (free-tier sensenova model, CDN URL
                  results). AI-generated when a PEXELS key is absent.
  pollinations  — keyless free generation via the public pollinations.ai HTTP
                  API. No account, no API key.
  openai_compat — any OpenAI-compatible /images/generations endpoint (self-hosted
                  ComfyUI bridges, LocalAI, or a paid vendor). Fully local when
                  pointed at localhost.
  mock          — simulation-only deterministic placeholder (labeled, no network).

All providers return raw image bytes + format; callers decide how to persist.
This module never writes into workspace storage itself.
"""

from __future__ import annotations

import abc
import hashlib
import re
import time


class ImageProviderError(Exception):
    pass


class BaseImageProvider(abc.ABC):
    name: str = "base"
    is_mock: bool = False

    @abc.abstractmethod
    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        """Generate n images for prompt; returns raw image bytes."""

    @abc.abstractmethod
    def healthy(self) -> bool:
        """Cheap reachability/health probe."""


# ---------------------------------------------------------------------------
# pollinations — keyless public endpoint, free
# ---------------------------------------------------------------------------


class PollinationsImageProvider(BaseImageProvider):
    name = "pollinations"

    _URL = "https://image.pollinations.ai/prompt/{prompt}"
    _MAX_PROMPT = 380  # keep URLs sane
    _RETRIES = 3       # free keyless service; intermittent 500s are normal

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import httpx

        clean = re.sub(r"\s+", " ", (prompt or "").strip())[: self._MAX_PROMPT]
        if not clean:
            raise ImageProviderError("prompt is empty")
        out: list[bytes] = []
        for i in range(max(1, n)):
            # unique seed per image so n>1 does not return identical bytes
            seed = int(hashlib.sha256(f"{clean}:{i}:{time.time()}".encode()).hexdigest()[:8], 16)
            from urllib.parse import quote

            encoded = quote(clean, safe="")  # full path encoding (commas, spaces)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={size.split('x')[0]}&height={size.split('x')[1]}&seed={seed}&nologo=true"
            )
            last_exc: Exception | None = None
            for attempt in range(self._RETRIES):
                try:
                    resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
                    resp.raise_for_status()
                    ctype = resp.headers.get("content-type", "")
                    if not ctype.startswith("image/"):
                        raise ImageProviderError(
                            f"pollinations returned non-image content-type {ctype!r}"
                        )
                    out.append(resp.content)
                    last_exc = None
                    break
                except ImageProviderError:
                    raise  # non-image content is a hard error, not transient
                except Exception as exc:
                    last_exc = exc
                    if attempt < self._RETRIES - 1:
                        time.sleep(1.5 * (attempt + 1))  # backoff: 1.5s, 3s
            if last_exc is not None:
                raise ImageProviderError(
                    f"pollinations generate failed after {self._RETRIES} attempts: "
                    f"{type(last_exc).__name__}: {last_exc}"
                ) from last_exc
        return out

    def healthy(self) -> bool:
        try:
            import httpx

            resp = httpx.get(
                "https://image.pollinations.ai/models",
                timeout=8.0,
                follow_redirects=True,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# openai-compatible — self-hosted or vendor /images/generations
# ---------------------------------------------------------------------------


class OpenAICompatImageProvider(BaseImageProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 120.0,
                 model: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.model = model

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import base64

        import httpx

        if not self.base_url:
            raise ImageProviderError("openai_compat: base_url is not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict = {"prompt": prompt, "n": max(1, n), "size": size,
                      "response_format": "b64_json"}
        if self.model:
            body["model"] = self.model
        try:
            resp = httpx.post(
                f"{self.base_url}/images/generations",
                json=body, headers=headers, timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise ImageProviderError(
                f"openai_compat generate failed: {type(exc).__name__}: {exc}"
            ) from exc
        items = data.get("data") or []
        out: list[bytes] = []
        for item in items:
            if item.get("b64_json"):
                out.append(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                dl = httpx.get(item["url"], timeout=self.timeout,
                               follow_redirects=True)
                dl.raise_for_status()
                out.append(dl.content)
        if not out:
            raise ImageProviderError("openai_compat returned no images")
        return out

    def healthy(self) -> bool:
        if not self.base_url:
            return False
        try:
            import httpx

            resp = httpx.get(self.base_url, timeout=6.0, follow_redirects=True)
            return resp.status_code < 500
        except Exception:
            return False


# ---------------------------------------------------------------------------
# xkiro — async image jobs, free-tier model, CDN results
# ---------------------------------------------------------------------------


class XkiroImageProvider(BaseImageProvider):
    """xKiro /v1/images/generations — asynchronous job + poll + CDN download.

    Uses the free-tier sensenova model by default (billed as free images
    within the plan's 24h allowance). The API is async: submit returns a job
    id that must be polled until succeeded/failed/blocked. Sizes are limited
    to a fixed set; we snap to the nearest supported one.
    """

    name = "xkiro"

    _SUPPORTED = (256, 512, 1024, 1536, 1792)
    _FREE_MODEL = "sensenova/sensenova-u1.5-lite"
    _POLL_INTERVAL = 4.0
    _POLL_DEADLINE = 240.0

    def __init__(self, base_url: str, api_key: str, model: str = "",
                 timeout: float = 30.0):
        self.base_url = (base_url or "https://api.xkiro.com/v1").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or self._FREE_MODEL
        self.timeout = timeout

    @staticmethod
    def _snap_size(size: str) -> str:
        """Snap any WxH to the nearest supported xKiro size."""
        try:
            w, h = (int(v) for v in size.lower().split("x"))
        except ValueError:
            return "1024x1024"
        landscape = w >= h
        if landscape:
            return "1792x1024" if w / h >= 1.5 else "1536x1024" if w / h >= 1.2 else "1024x1024"
        return "1024x1536" if h / w >= 1.2 else "1024x1024"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import httpx

        if not self.api_key:
            raise ImageProviderError("xkiro: no API key configured")
        snapped = self._snap_size(size)
        results: list[bytes] = []
        with httpx.Client(timeout=self.timeout) as client:
            for i in range(max(1, n)):
                try:
                    resp = client.post(
                        f"{self.base_url}/images/generations",
                        headers=self._headers(),
                        json={
                            "model": self.model,
                            "prompt": prompt,
                            "n": 1,  # API currently requires 1 per job
                            "size": snapped,
                        },
                    )
                    resp.raise_for_status()
                    job = resp.json()
                except Exception as exc:
                    raise ImageProviderError(
                        f"xkiro submit failed: {type(exc).__name__}: {exc}"
                    ) from exc
                job_id = job.get("id")
                if not job_id:
                    raise ImageProviderError(f"xkiro submit returned no job id: {str(job)[:120]}")
                results.append(self._poll_and_download(client, job_id, prompt))
        return results

    def _poll_and_download(self, client, job_id: str, prompt: str) -> bytes:
        import time as _time


        deadline = _time.time() + self._POLL_DEADLINE
        wait = self._POLL_INTERVAL
        while _time.time() < deadline:
            _time.sleep(wait)
            wait = min(wait * 1.4, 10.0)
            try:
                resp = client.get(
                    f"{self.base_url}/images/generations/{job_id}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                job = resp.json()
            except Exception as exc:
                raise ImageProviderError(f"xkiro poll failed: {exc}") from exc
            status = job.get("status")
            if status == "succeeded":
                url = ((job.get("data") or [{}])[0].get("url") or "")
                if not url:
                    raise ImageProviderError("xkiro job succeeded but no URL returned")
                try:
                    dl = client.get(url, timeout=90.0, follow_redirects=True)
                    dl.raise_for_status()
                except Exception as exc:
                    raise ImageProviderError(f"xkiro CDN download failed: {exc}") from exc
                if len(dl.content) < 1024:
                    raise ImageProviderError("xkiro CDN image suspiciously small")
                return dl.content
            if status in ("failed", "blocked"):
                msg = (job.get("error") or {}).get("message", status)
                raise ImageProviderError(f"xkiro job {status}: {msg}")
        raise ImageProviderError(f"xkiro job {job_id} timed out after {self._POLL_DEADLINE:.0f}s")

    def healthy(self) -> bool:
        import httpx

        if not self.api_key:
            return False
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                params={"modality": "image"},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# mock — simulation only, deterministic placeholder, labeled
# ---------------------------------------------------------------------------


def _gradient_png(width: int, height: int, seed_text: str) -> bytes:
    """Tiny deterministic PNG (no deps beyond stdlib zlib/struct)."""
    import struct
    import zlib

    h = int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = b"\x00"
        for x in range(width):
            r = (x * 255 // max(1, width)) ^ (h & 0x55)
            g = (y * 255 // max(1, height)) ^ ((h >> 8) & 0x55)
            b = (h >> 16) & 0xFF
            row += bytes((r, g, b))
        rows.append(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b""))


class MockImageProvider(BaseImageProvider):
    name = "mock"
    is_mock = True

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        try:
            w, h = (int(v) for v in size.lower().split("x"))
        except ValueError:
            w, h = 640, 360
        w, h = max(16, min(1280, w)), max(16, min(1280, h))
        return [_gradient_png(w, h, f"{prompt}:{i}") for i in range(max(1, n))]

    def healthy(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# factory / status
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "pexels": None,  # lazily imported (separate module avoids an import cycle)
    "xkiro": XkiroImageProvider,
    "pollinations": PollinationsImageProvider,
    "openai_compat": OpenAICompatImageProvider,
    "mock": MockImageProvider,
}

_instances: dict[str, BaseImageProvider] = {}
_GLOBAL_SCOPE = "__global__"


def _scope_key() -> str:
    try:
        from app.services.provider_settings import current_workspace_id

        return current_workspace_id() or _GLOBAL_SCOPE
    except Exception:
        return _GLOBAL_SCOPE


def reset_image_provider(workspace_id: str | None = None) -> None:
    if workspace_id:
        _instances.pop(workspace_id, None)
    else:
        _instances.clear()


def get_image_provider():
    from app.core.config import settings
    from app.services.provider_settings import get_credential

    scope = _scope_key()
    if scope in _instances:
        return _instances[scope]
    name = (settings.image_provider or "pollinations").lower().strip()
    if name == "pexels":
        from app.providers.images_pexels import PexelsImageProvider

        key, _psrc = get_credential("pexels.api_key")
        if not key:
            key = settings.pexels_api_key
        _instances[scope] = PexelsImageProvider(api_key=key or "")
        return _instances[scope]
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ImageProviderError(
            f"IMAGE_PROVIDER '{name}' is not one of {sorted(_PROVIDERS)}"
        )
    if cls is XkiroImageProvider:
        base_url, _ = get_credential("image.openai_base_url")
        key, _ksrc = get_credential("image.openai_api_key")
        model, _msrc = get_credential("image.openai_model")
        if not key:
            # fall back to the shared LLM key when no dedicated image key set
            key, _ = get_credential("llm.api_key")
        _instances[scope] = XkiroImageProvider(
            base_url=base_url or "https://api.xkiro.com/v1",
            api_key=key or "",
            model=model or "",
        )
    elif cls is PollinationsImageProvider:
        _instances[scope] = PollinationsImageProvider()
    elif cls is OpenAICompatImageProvider:
        base_url, _src = get_credential("image.openai_base_url")
        key, _ksrc = get_credential("image.openai_api_key")
        model, _msrc = get_credential("image.openai_model")
        _instances[scope] = OpenAICompatImageProvider(
            base_url=base_url or "",
            api_key=key or "",
            model=model or "",
        )
    else:
        _instances[scope] = MockImageProvider()
    return _instances[scope]


def image_provider_status() -> dict:
    from app.core.config import settings

    name = (settings.image_provider or "pollinations").lower().strip()
    info: dict = {
        "provider": name,
        "is_mock": name == "mock",
        "healthy": False,
        "error": "",
    }
    try:
        provider = get_image_provider()
        info["healthy"] = provider.healthy()
    except ImageProviderError as exc:
        info["error"] = str(exc)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info
