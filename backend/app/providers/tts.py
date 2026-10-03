"""TTS provider abstraction — narration voice generation.

Providers (selected via TTS_PROVIDER or per-request):
  edge        — Microsoft Edge neural voices over the public edge-tts protocol.
                Free, no key, no local model. Default.
  kokoro      — self-hosted OpenAI-compatible /audio/speech server running the
                Kokoro-82M model (Apache-2 weights). Fully local/offline.
  elevenlabs  — cloud neural voices + voice library (paid API key).
  mock        — simulation-only; produces silence and is labeled everywhere.

The provider returns raw audio bytes + a duration probe; callers decide how to
persist the artifact (storage boundary owns paths).

Work 15.7: every remote ``/audio/speech`` submit goes through
:class:`~app.services.paid_executor.PaidProviderExecutor`. ElevenLabs bills per
character, and the operator servers (Kokoro / Chatterbox / Qwen3) are billable
whenever their base URL points at a remote host -- the credential that holds
that URL is a paid-capability credential. The budget gate therefore runs
BEFORE the POST, and a lost response is recorded as an UNKNOWN exposure rather
than a flat ``TTSError`` the caller cannot distinguish from a refusal.
"""

from __future__ import annotations

import abc
import math
import struct
from dataclasses import dataclass

from app.services.paid_executor import (
    IdempotencySupport,
    PaidJobError,
    PaidProviderExecutor,
    PaidSubmissionUnconfirmed,
    RemoteSubmission,
)
from app.services.paid_provider import (
    PaidOperation,
    SpendAuthority,
    absorb_paid_failure,
    paid_event,
    paid_operation,
)

#: Ledger category for a synthesis. "tts", so a dub costs against the same
#: daily cap as everything else the workspace buys.
COST_CATEGORY = "tts"


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
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
        """Narrate text. exaggeration/clone_from are honored only by providers
        that support them (Chatterbox); others ignore them."""
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
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
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
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")
        speed = max(0.25, min(4.0, float(rate or 1.0)))
        key = self._key()
        payload = {
            "model": "kokoro",
            "input": text,
            "voice": voice or self.DEFAULT_VOICE,
            "response_format": "mp3",
            "speed": speed,
        }
        # Billable-if-remote: the Kokoro base URL is a credential, so a hosted
        # server bills us and a lost response may already have cost money.
        audio = _speech_post(self.base_url, key, payload, 180,
                             provider=self.name, remote_label="kokoro_speech")
        return TTSResult(audio_bytes=audio, format="mp3", provider=self.name)

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
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
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
# shared OpenAI-compatible /audio/speech transport
# ---------------------------------------------------------------------------


def _speech_post(base_url: str, api_key: str, payload: dict, timeout: int = 180,
                 *, provider: str = "", remote_label: str = "") -> bytes:
    """POST ``/audio/speech`` for the operator-server TTS providers.

    One submit, ever. The base URL arrives as a credential, so a non-local
    server is somebody's invoice: the request is routed through the paid
    executor, the pre-spend gate runs before it, and a lost response becomes an
    UNKNOWN exposure instead of a ``TTSError`` a caller could reasonably
    retry into a second purchase.
    """
    import httpx

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    name = provider or "tts_server"
    billable = _remote_base_url(base_url)
    paid, executor = _paid(
        f"speech.{remote_label or 'openai_compat'}", provider=name,
        billable=billable)

    def submit(_idempotency_key: str) -> RemoteSubmission:
        resp = httpx.post(f"{base_url.rstrip('/')}/audio/speech", json=payload,
                          headers=headers, timeout=timeout)
        if resp.status_code != 200:
            # Same reasoning as providers/broll.py: the status has to reach the
            # paid classifier rather than being flattened into one generic
            # "server unreachable" string -- a 4xx was never billed and a 5xx
            # may follow a rendered, billed segment.
            raise httpx.HTTPStatusError(
                f"tts server returned HTTP {resp.status_code}: "
                f"{str(getattr(resp, 'text', ''))[:200]}",
                request=(getattr(resp, "request", None)
                         or httpx.Request("POST",
                                          f"{base_url.rstrip('/')}/audio/speech")),
                response=resp)
        if not resp.content:
            # 2xx with no audio: billed, unfulfilled, and not reconcilable
            # through a remote id. Never reported as a success.
            raise PaidSubmissionUnconfirmed(
                provider=name,
                detail="tts server answered 2xx with an empty body")
        return RemoteSubmission(
            remote_id=_header(resp, "x-request-id"),
            artifact_path="inline", raw={"audio": resp.content})

    try:
        handle = executor.execute(submit)
    except TTSError:
        raise
    except PaidJobError as exc:
        # Whether money is gone is decided ONCE, in the shared helper: a 4xx
        # releases the reservation, a lost response keeps it and marks the
        # exposure unknown, a connection that never opened proves nothing was
        # delivered. The caller cannot tell those apart by exception type, so
        # the ledger has already been told.
        absorb_paid_failure(paid, exc)
        raise TTSError(str(exc)) from exc
    paid.mark_succeeded(
        amount_unknown=True,
        detail=("remote operator TTS server; the response carries no price"
                if billable else "local operator TTS server; nothing billed"))
    return bytes(handle.raw.get("audio") or b"")


def _speech_voices(base_url: str, timeout: int = 15) -> list[dict] | None:
    import httpx

    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/audio/voices", timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


# ---------------------------------------------------------------------------
# chatterbox — MIT zero-shot clone voice (native package or compat server)
# ---------------------------------------------------------------------------


class ChatterboxTTSProvider(BaseTTSProvider):
    """Resemble Chatterbox-Turbo: MIT-licensed, cloning from ~5s of audio,
    emotion exaggeration + paralinguistic tags ([laugh], [cough]) in text.

    Two backends, checked in order:
      1. native `chatterbox-tts` package (GPU recommended),
      2. OpenAI-compatible server at the chatterbox base URL (CPU-friendly).
    Clone references must live inside the workspace boundary.
    """

    name = "chatterbox"

    DEFAULT_VOICE = "default"

    def __init__(self, base_url: str = ""):
        self.base_url = (base_url or "").rstrip("/")

    @staticmethod
    def _native_available() -> bool:
        try:
            import importlib.util as _u

            return _u.find_spec("chatterbox") is not None
        except (ImportError, ValueError):
            return False

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")
        ex = max(0.0, min(1.0, float(exaggeration if exaggeration is not None else 0.5)))
        if self.base_url:
            if clone_from:
                raise TTSError("clone references need the native chatterbox package — "
                               "clear clone_from or install chatterbox-tts")
            payload = {
                "model": "chatterbox-turbo",
                "input": text,
                "voice": voice or self.DEFAULT_VOICE,
                "response_format": "mp3",
                "speed": max(0.25, min(4.0, float(rate or 1.0))),
                "exaggeration": ex,
            }
            audio = _speech_post(self.base_url, "", payload,
                                 provider=self.name,
                                 remote_label="chatterbox_speech")
            return TTSResult(audio_bytes=audio, format="mp3", provider=self.name)
        if not self._native_available():
            raise TTSError(
                "chatterbox selected but neither the native package nor a server URL "
                "is available — pip install chatterbox-tts (GPU) or set "
                "tts.chatterbox_base_url under Settings → Connections"
            )
        return self._native_synth(text, voice or self.DEFAULT_VOICE, ex, clone_from)

    def _native_synth(self, text: str, voice: str, exaggeration: float, clone_from: str) -> TTSResult:
        try:
            import io as _io

            import torch
            from chatterbox.tts import ChatterboxTurbo

            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = ChatterboxTurbo.from_pretrained(device=device)
            kwargs: dict = {"exaggeration": exaggeration}
            if clone_from:
                from pathlib import Path as _P

                ref = _P(clone_from)
                if not ref.is_file():
                    raise TTSError(f"clone reference not found: {clone_from}")
                kwargs["audio_prompt_path"] = str(ref)
            elif voice and voice != self.DEFAULT_VOICE:
                kwargs["audio_prompt_path"] = voice
            wav = model.generate(text, **kwargs)
            sr = int(getattr(model, "sr", 24000))
            buf = _io.BytesIO()
            try:
                import soundfile as _sf

                _sf.write(buf, wav.squeeze(0).cpu().numpy(), sr, format="WAV")
            except ImportError:
                import torchaudio as _ta

                _ta.save(buf, wav.cpu(), sr, format="wav")
            return TTSResult(audio_bytes=buf.getvalue(), format="wav", provider=self.name,
                             sample_rate=sr)
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(f"chatterbox native synthesis failed: {type(exc).__name__}: {exc}") from exc

    def voices(self, language: str = "") -> list[dict]:
        if self.base_url:
            raw = _speech_voices(self.base_url)
            if raw is None:
                return [{"id": self.DEFAULT_VOICE, "gender": "", "locale": language or "en"}]
            out = []
            for v in raw or []:
                if isinstance(v, str):
                    out.append({"id": v, "gender": "", "locale": ""})
                elif isinstance(v, dict) and v.get("id"):
                    out.append({"id": v["id"], "gender": v.get("gender", ""),
                                "locale": v.get("locale", "")})
            return out or [{"id": self.DEFAULT_VOICE, "gender": "", "locale": language or "en"}]
        return [{"id": self.DEFAULT_VOICE, "gender": "", "locale": language or "en"}]

    def health(self) -> bool:
        if self.base_url:
            return _speech_voices(self.base_url, timeout=5) is not None
        return self._native_available()


# ---------------------------------------------------------------------------
# qwen3-tts — Apache-2.0 multilingual cloner behind an OpenAI-compat server
# ---------------------------------------------------------------------------


class QwenTTSProvider(BaseTTSProvider):
    """Qwen3-TTS via a vLLM-Omni (or compatible) /audio/speech server.

    Server-only: the model needs a serving stack. Supports voice cloning from
    ~3s of audio plus natural-language delivery direction (`instruct`,
    e.g. 'speak cheerfully'). Configure the base URL (and optional default
    instruction) under Settings → Connections.
    """

    name = "qwen3"

    DEFAULT_VOICE = "default"

    def __init__(self, base_url: str, instruct: str = ""):
        if not base_url:
            raise TTSError("qwen3 TTS selected but no server URL configured — set "
                           "tts.qwen_base_url under Settings → Connections")
        self.base_url = base_url.rstrip("/")
        self.instruct = (instruct or "").strip()

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")
        payload: dict = {
            "model": "qwen3-tts",
            "input": text,
            "voice": voice or self.DEFAULT_VOICE,
            "response_format": "mp3",
            "speed": max(0.25, min(4.0, float(rate or 1.0))),
        }
        if language:
            payload["language"] = language
        if self.instruct:
            payload["instruct"] = self.instruct
        if clone_from:
            payload["clone_from"] = clone_from
        audio = _speech_post(self.base_url, self._key(), payload,
                             provider=self.name, remote_label="qwen3_speech")
        return TTSResult(audio_bytes=audio, format="mp3", provider=self.name)

    def _key(self) -> str:
        try:
            from app.services.provider_settings import get_credential

            val, _src = get_credential("tts.qwen_api_key")
            return val or ""
        except Exception:
            return ""

    def voices(self, language: str = "") -> list[dict]:
        raw = _speech_voices(self.base_url)
        if raw is None:
            return [{"id": self.DEFAULT_VOICE, "gender": "", "locale": language or ""}]
        out = []
        for v in raw or []:
            if isinstance(v, str):
                out.append({"id": v, "gender": "", "locale": ""})
            elif isinstance(v, dict) and v.get("id"):
                out.append({"id": v["id"], "gender": v.get("gender", ""),
                            "locale": v.get("locale", "")})
        return out or [{"id": self.DEFAULT_VOICE, "gender": "", "locale": language or ""}]

    def health(self) -> bool:
        return _speech_voices(self.base_url, timeout=5) is not None


# ---------------------------------------------------------------------------
# elevenlabs — cloud neural voices + voice library (paid, API key)
# ---------------------------------------------------------------------------


class ElevenLabsTTSProvider(BaseTTSProvider):
    """ElevenLabs cloud TTS (https://api.elevenlabs.io/v1).

    Voice variety (including dashboard-cloned voices) via your ElevenLabs
    voice_id library: pass any voice_id as the per-content `voice` override
    (Settings → Connections → TTS lists them). Cloning itself happens in the
    ElevenLabs dashboard — use the resulting voice_id here (`clone_from`
    asset references are ignored by this provider per the synthesize
    contract). Key via the `tts.elevenlabs_api_key` credential or
    ELEVENLABS_API_KEY env. Paid per character (~$0.20/1k chars estimate —
    actual billing per ElevenLabs plan); the Voice Designer records the
    estimate so budgets stay honest.
    """

    name = "elevenlabs"

    API = "https://api.elevenlabs.io/v1"
    DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel (multilingual)
    MODEL = "eleven_multilingual_v2"
    EST_USD_PER_CHAR = 0.0002

    def __init__(self, api_key: str = ""):
        key = api_key or _cred("tts.elevenlabs_api_key", "elevenlabs_api_key")
        if not key:
            raise TTSError(
                "elevenlabs TTS selected but no API key configured — set ELEVENLABS_API_KEY "
                "or add tts.elevenlabs_api_key under Settings → Connections"
            )
        self.api_key = key

    def _headers(self) -> dict:
        return {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

    def synthesize(self, text: str, *, voice: str = "", rate: float = 1.0,
                   volume: float = 1.0, language: str = "",
                   exaggeration: float = 0.5, clone_from: str = "") -> TTSResult:
        import httpx

        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")
        voice_id = (voice or self.DEFAULT_VOICE).strip()
        payload: dict = {
            "text": text,
            "model_id": self.MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        try:
            speed = max(0.7, min(1.2, float(rate or 1.0)))
        except (TypeError, ValueError):
            speed = 1.0
        if speed != 1.0:
            payload["speed"] = speed
        # ElevenLabs bills per character, so this provider has a REAL cost
        # estimate -- and therefore a real budget decision to make before the
        # POST rather than after it.
        estimate = round(len(text) * self.EST_USD_PER_CHAR, 6)
        paid, executor = _paid(
            "tts.text_to_speech", provider=self.name, estimated_cost=estimate)

        def submit(_idempotency_key: str) -> RemoteSubmission:
            resp = httpx.post(
                f"{self.API}/text-to-speech/{voice_id}",
                headers=self._headers(), json=payload, timeout=120.0,
            )
            resp.raise_for_status()
            if len(resp.content) < 512:
                # Charged per character and the answer is unusable: this is an
                # unknown exposure, not a rejection we may buy again.
                raise PaidSubmissionUnconfirmed(
                    provider=self.name,
                    detail=f"elevenlabs returned suspiciously small audio "
                           f"({len(resp.content)} bytes)")
            return RemoteSubmission(
                remote_id=_header(resp, "request-id")
                or _header(resp, "x-request-id"),
                artifact_path="inline", raw={"audio": resp.content})

        try:
            handle = executor.execute(submit, estimated_cost=estimate)
        except TTSError:
            raise
        except PaidJobError as exc:
            # Ambiguous or refused: both are recorded, and the caller cannot
            # tell "billed" from "not billed" by the exception type alone --
            # the submission record can.
            absorb_paid_failure(paid, exc)
            raise TTSError(str(exc)) from exc
        # A REAL per-character estimate, reserved before the POST and settled in
        # place -- never a second track_cost, which would bill one narration
        # twice and double-charge the daily cap.
        paid.mark_succeeded(
            estimate_usd=estimate,
            detail=f"{len(text)} char(s) at ${self.EST_USD_PER_CHAR}/char")
        return TTSResult(audio_bytes=bytes(handle.raw.get("audio") or b""),
                         format="mp3", provider=self.name)

    def voices(self, language: str = "") -> list[dict]:
        import httpx

        try:
            resp = httpx.get(
                f"{self.API}/voices", headers={"xi-api-key": self.api_key}, timeout=20.0
            )
            resp.raise_for_status()
            items = resp.json().get("voices", [])
        except Exception as exc:
            raise TTSError(f"voice list unavailable: {exc}") from exc
        out = []
        for v in items or []:
            labels = v.get("labels") or {}
            if language and language not in str(labels.get("accent", "")):
                continue
            out.append({
                "id": v.get("voice_id"),
                "gender": labels.get("gender", ""),
                "locale": labels.get("accent", ""),
            })
        return out or [{"id": self.DEFAULT_VOICE, "gender": "", "locale": ""}]

    def health(self) -> bool:
        import httpx

        try:
            resp = httpx.get(
                f"{self.API}/user", headers={"xi-api-key": self.api_key}, timeout=10.0
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def _cred(key: str, env_attr: str = "") -> str:
    from app.core.config import settings

    try:
        from app.services.provider_settings import get_credential

        val, _src = get_credential(key)
        if val:
            return val
    except Exception:
        pass
    return getattr(settings, env_attr, "") or "" if env_attr else ""


def _kokoro_base_url() -> str:
    return _cred("tts.kokoro_base_url", "kokoro_base_url")


def _chatterbox_base_url() -> str:
    return _cred("tts.chatterbox_base_url", "chatterbox_base_url")


def _qwen_base_url() -> str:
    return _cred("tts.qwen_base_url", "qwen_base_url")


def _qwen_instruct() -> str:
    return _cred("tts.qwen_instruct", "qwen_tts_instruct")


def _effective_provider_name(name: str = "") -> str:
    from app.core.config import settings

    if name:
        return name.lower()
    try:
        from app.services.provider_settings import get_credential

        val, _src = get_credential("tts.provider")
        if val:
            return val.lower()
    except Exception:
        pass
    return (getattr(settings, "tts_provider", "") or "edge").lower()


def get_tts_provider(name: str = "") -> BaseTTSProvider:
    chosen = _effective_provider_name(name)
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
    if chosen in ("chatterbox", "chatterbox-turbo"):
        base = _chatterbox_base_url()
        if not base and not ChatterboxTTSProvider._native_available():
            raise TTSError(
                "chatterbox TTS selected but neither the native package nor a server "
                "URL is configured — pip install chatterbox-tts (GPU) or add "
                "tts.chatterbox_base_url under Settings → Connections"
            )
        return ChatterboxTTSProvider(base)
    if chosen in ("qwen3", "qwen", "qwen-tts"):
        return QwenTTSProvider(_qwen_base_url(), instruct=_qwen_instruct())
    if chosen in ("elevenlabs", "eleven", "xi", "11labs"):
        return ElevenLabsTTSProvider()
    if chosen in ("edge", ""):
        return EdgeTTSProvider()
    raise TTSError(f"unknown TTS provider '{chosen}' (edge|kokoro|chatterbox|qwen3|elevenlabs|mock)")


def tts_provider_status() -> dict:
    """Diagnostics for the health endpoint."""
    chosen = _effective_provider_name()
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
    if chosen in ("chatterbox", "chatterbox-turbo"):
        out["base_url"] = _chatterbox_base_url()
        out["native"] = ChatterboxTTSProvider._native_available()
    if chosen in ("qwen3", "qwen", "qwen-tts"):
        out["base_url"] = _qwen_base_url()
    return out


# ---------------------------------------------------------------------------
# paid-submission wiring (Work 15.7)
# ---------------------------------------------------------------------------


def _header(resp, name: str, default: str = "") -> str:
    """One response header, tolerating a thin response object.

    ``httpx`` answers with a mapping; a test double may answer with a plain
    dict or with nothing at all. A vendor request id is a reconciliation
    handle, not a reason to fail a render.
    """
    getter = getattr(getattr(resp, "headers", None), "get", None)
    return str(getter(name, default)) if callable(getter) else default


def _remote_base_url(base_url: str) -> bool:
    """True when a TTS base URL names a host other than this machine.

    A self-hosted Kokoro on ``localhost`` is operator CPU. The same server on
    ``https://voices.example.com`` is somebody's invoice, and the credential
    that holds that URL is a paid-capability credential. The audit records both
    readings; the guard has to pick one, so it picks the money-safe one.
    """
    from urllib.parse import urlsplit

    text = str(base_url or "").strip()
    if not text:
        return False
    host = (urlsplit(text if "//" in text else f"//{text}").hostname or "").lower()
    return host not in ("", "localhost", "127.0.0.1", "::1", "0.0.0.0",
                        "host.docker.internal")


def _owner(workspace_id: str = "") -> str:
    """The tenant a synthesis is billed to, or "" when there is none.

    An explicit argument wins; otherwise the ambient scope is read, which is
    what a request thread has. An empty answer is now a REFUSAL rather than a
    silent run: since §1 the shared helper raises before the POST, where this
    used to send the request anyway and book it against ``workspace_id=""``.
    """
    if str(workspace_id or "").strip():
        return str(workspace_id).strip()
    try:
        from app.services.provider_settings import current_workspace_id

        return str(current_workspace_id() or "").strip()
    except Exception:  # noqa: BLE001 - no scope means no ledger owner
        return ""


def _paid(operation: str, *, provider: str, workspace_id: str = "",
          estimated_cost: float = 0.0, billable: bool = True,
          idempotency: IdempotencySupport = IdempotencySupport.UNSUPPORTED,
          ) -> tuple[PaidOperation, PaidProviderExecutor]:
    """One speech synthesis, on the shared money mechanics.

    Returns ``(paid, executor)``; see ``providers/images.py`` for the shape.
    ``billable=False`` keeps the classification (a lost response is still not a
    refusal) while booking nothing for a local operator server -- and now says
    so through a DECLARATION rather than by quietly omitting the gate, which is
    how a genuinely local server and an accidentally-unbudgeted remote one used
    to look identical.
    """
    paid = paid_operation(
        provider=provider,
        operation=operation,
        workspace_id=_owner(workspace_id),
        category=COST_CATEGORY,
        estimated_cost=estimated_cost,
        idempotency=idempotency,
        reservation_extra={"lane": "tts"},
    )
    if billable:
        paid.bind(on_event=lambda phase, level, message:
                  paid_event(paid, phase, level, message))
    else:
        paid.declared(authority=SpendAuthority.EXPLICIT_NONBILLABLE)
    return paid, paid.make_executor()


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
    "ChatterboxTTSProvider",
    "EdgeTTSProvider",
    "KokoroTTSProvider",
    "MockTTSProvider",
    "QwenTTSProvider",
    "TTSError",
    "TTSResult",
    "get_tts_provider",
    "tts_provider_status",
    "wav_duration_seconds",
]
