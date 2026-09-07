"""TTS provider abstraction — narration voice generation.

Providers (selected via TTS_PROVIDER or per-request):
  edge        — Microsoft Edge neural voices over the public edge-tts protocol.
                Free, no key, no local model. Default.
  kokoro      — self-hosted OpenAI-compatible /audio/speech server running the
                Kokoro-82M model (Apache-2 weights). Fully local/offline.
  mock        — simulation-only; produces silence and is labeled everywhere.

The provider returns raw audio bytes + a duration probe; callers decide how to
persist the artifact (storage boundary owns paths).
"""

from __future__ import annotations

import abc
import math
import struct
from dataclasses import dataclass


class TTSError(Exception):
    pass


def _run_coro(coro):
    """Run an async coroutine from sync code regardless of threading context.

    FastAPI runs sync endpoints in worker threads that have no event loop, so
    asyncio.run() alone is not enough when an outer loop exists. This helper
    tries asyncio.run first, then falls back to a dedicated loop.
    """
    import asyncio

    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


@dataclass
class TTSResult:
    audio_bytes: bytes
    format: str  # "mp3" | "wav"
    provider: str
    is_mock: bool = False
    sample_rate: int | None = None  # wav only


class BaseTTSProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "") -> TTSResult:
        ...

    @abc.abstractmethod
    def voices(self, language: str = "") -> list[dict]:
        ...

    def health(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# edge — public neural voices, free, no key
# ---------------------------------------------------------------------------


class EdgeTTSProvider(BaseTTSProvider):
    name = "edge"

    DEFAULT_VOICE = "en-US-AndrewNeural"

    async def _run(self, text: str, voice: str, rate: float, volume: float) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice, rate=self._rate_str(rate),
                                           volume=self._volume_str(volume))
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.append(chunk["data"])
        if not chunks:
            raise TTSError("edge tts produced no audio")
        return b"".join(chunks)

    @staticmethod
    def _rate_str(rate: float) -> str:
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 1.0
        if rate <= 0:
            rate = 1.0
        pct = round((rate - 1.0) * 100)
        return f"+{pct}%" if pct >= 0 else f"{pct}%"

    @staticmethod
    def _volume_str(volume: float) -> str:
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 1.0
        pct = round((volume - 1.0) * 100)
        return f"+{pct}%" if pct >= 0 else f"{pct}%"

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "") -> TTSResult:
        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")
        try:
            audio = _run_coro(self._run(text, voice or self.DEFAULT_VOICE, rate, volume))
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(f"edge tts failed: {type(exc).__name__}: {exc}") from exc
        return TTSResult(audio_bytes=audio, format="mp3", provider=self.name)

    def voices(self, language: str = "") -> list[dict]:
        async def _list():
            import edge_tts

            return await edge_tts.list_voices()

        try:
            raw = _run_coro(_list())
        except Exception as exc:
            raise TTSError(f"voice list unavailable: {exc}") from exc
        out = []
        for v in raw:
            if language and language not in str(v.get("Locale", "")):
                continue
            out.append({
                "id": v.get("ShortName"),
                "gender": v.get("Gender"),
                "locale": v.get("Locale"),
            })
        return out


# ---------------------------------------------------------------------------
# kokoro — self-hosted OpenAI-compatible /audio/speech (local model)
# ---------------------------------------------------------------------------


class KokoroTTSProvider(BaseTTSProvider):
    """Talks to any OpenAI-compatible /audio/speech server hosting Kokoro-82M.

    Community servers expose GET /audio/voices and POST /audio/speech, so the
    provider stays keyless and fully local. Configure the base URL via the
    `tts.kokoro_base_url` credential or KOKORO_BASE_URL env.
    """

    name = "kokoro"

    DEFAULT_VOICE = "af_heart"

    def __init__(self, base_url: str):
        if not base_url:
            raise TTSError("kokoro base URL not configured (KOKORO_BASE_URL)")
        self.base_url = base_url.rstrip("/")

    def _cfg(self):
        from app.core.config import settings

        return settings

    def _key(self) -> str:
        try:
            from app.services.provider_settings import get_credential

            val, _src = get_credential("tts.kokoro_api_key")
            return val or ""
        except Exception:
            return ""

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "") -> TTSResult:
        import httpx

        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")
        speed = max(0.25, min(4.0, float(rate or 1.0)))
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": "kokoro",
            "input": text,
            "voice": voice or self.DEFAULT_VOICE,
            "response_format": "mp3",
            "speed": speed,
        }
        try:
            resp = httpx.post(f"{self.base_url}/audio/speech", json=payload,
                              headers=headers, timeout=180)
        except httpx.HTTPError as exc:
            raise TTSError(f"kokoro server unreachable at {self.base_url}: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise TTSError(f"kokoro returned HTTP {resp.status_code}: {resp.text[:200]}")
        return TTSResult(audio_bytes=resp.content, format="mp3", provider=self.name)

    def voices(self, language: str = "") -> list[dict]:
        import httpx

        try:
            resp = httpx.get(f"{self.base_url}/audio/voices", timeout=15)
            if resp.status_code != 200:
                return [self._fallback_voice()]
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return [self._fallback_voice()]
        raw = data.get("voices") if isinstance(data, dict) else data
        out = []
        for v in raw or []:
            if isinstance(v, str):
                out.append({"id": v, "gender": "", "locale": ""})
            elif isinstance(v, dict) and v.get("id"):
                out.append({"id": v["id"], "gender": v.get("gender", ""),
                            "locale": v.get("locale", "")})
        return out or [self._fallback_voice()]

    @staticmethod
    def _fallback_voice() -> dict:
        return {"id": KokoroTTSProvider.DEFAULT_VOICE, "gender": "", "locale": "en-us"}

    def health(self) -> bool:
        import httpx

        try:
            resp = httpx.get(f"{self.base_url}/audio/voices", timeout=5)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False


# ---------------------------------------------------------------------------
# mock — simulation only, clearly labeled
# ---------------------------------------------------------------------------


class MockTTSProvider(BaseTTSProvider):
    """Produces deterministic silence sized to the text. NEVER for production."""

    name = "mock"

    WORDS_PER_SECOND = 2.6

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "") -> TTSResult:
        words = len((text or "").strip().split())
        if not words:
            raise TTSError("text is empty")
        seconds = max(1.0, words / (self.WORDS_PER_SECOND * max(rate or 1.0, 0.1)))
        sample_rate = 16_000
        n = int(seconds * sample_rate)
        # tiny silence wav (16-bit mono)
        data = b"\x00\x00" * n
        header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                        sample_rate * 2, 2, 16)
        header += b"data" + struct.pack("<I", len(data))
        return TTSResult(audio_bytes=header + data, format="wav", provider=self.name,
                         is_mock=True, sample_rate=sample_rate)

    def voices(self, language: str = "") -> list[dict]:
        return [{"id": "mock-narrator", "gender": "", "locale": language or "en"}]


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def _kokoro_base_url() -> str:
    from app.core.config import settings

    try:
        from app.services.provider_settings import get_credential

        val, _src = get_credential("tts.kokoro_base_url")
        if val:
            return val
    except Exception:
        pass
    return getattr(settings, "kokoro_base_url", "") or ""


def get_tts_provider(name: str = "") -> BaseTTSProvider:
    from app.core.config import settings

    chosen = (name or getattr(settings, "tts_provider", "") or "edge").lower()
    if chosen == "mock":
        return MockTTSProvider()
    if chosen in ("kokoro", "local"):
        base = _kokoro_base_url()
        if not base:
            raise TTSError(
                "kokoro TTS selected but no server URL configured — set KOKORO_BASE_URL "
                "or add tts.kokoro_base_url under Settings → Connections"
            )
        return KokoroTTSProvider(base)
    if chosen in ("edge", ""):
        return EdgeTTSProvider()
    raise TTSError(f"unknown TTS provider '{chosen}' (edge|kokoro|mock)")


def tts_provider_status() -> dict:
    """Diagnostics for the health endpoint."""
    from app.core.config import settings

    chosen = (getattr(settings, "tts_provider", "") or "edge").lower()
    out: dict = {"provider": chosen}
    try:
        provider = get_tts_provider(chosen)
        out["healthy"] = provider.health()
        out["is_mock"] = getattr(provider, "name", "") == "mock"
    except TTSError as exc:
        out["healthy"] = False
        out["error"] = str(exc)
    if chosen in ("kokoro", "local"):
        out["base_url"] = _kokoro_base_url()
    return out


def wav_duration_seconds(audio_bytes: bytes, sample_rate: int = 16_000) -> float:
    """Cheap duration estimate for wav/mock audio; mp3 handled by ffprobe."""
    header = audio_bytes[:44]
    if len(header) >= 44 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        import struct as _s

        try:
            byte_rate = _s.unpack("<I", header[28:32])[0]
            data_size = _s.unpack("<I", header[40:44])[0]
            if byte_rate:
                return data_size / byte_rate
        except struct.error:
            pass
    # fallback: assume words at the narration pace
    return max(1.0, math.ceil(len(audio_bytes) / sample_rate / 2))


__all__ = [
    "BaseTTSProvider",
    "EdgeTTSProvider",
    "KokoroTTSProvider",
    "MockTTSProvider",
    "TTSError",
    "TTSResult",
    "get_tts_provider",
    "tts_provider_status",
    "wav_duration_seconds",
]
