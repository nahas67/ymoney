"""ElevenLabs video-to-music: one billable soundtrack from a video PROXY.

The shape is deliberate and every step exists because the alternative is a
wrong artifact or a double charge:

1. **Validate the request before spending.** Missing key, missing file,
   non-finite/non-positive duration, over-long video, oversized prompt -> a
   typed error, no request sent.
2. **Build a proxy, never upload the master.** Video-to-Music only looks at
   picture; a 1280-long-edge, audio-stripped, byte-capped H.264 proxy costs the
   same and transfers a fraction of the bytes. The proxy lives beside the input
   and is deleted in ``finally``, so it cannot survive an exception.
3. **Submit ONCE, through the paid-job contract.** The submit is wrapped in
   ``classify_submit_exception``: a 4xx is ``PaidJobRejected`` (safe to retry
   because nothing was billed), a read timeout or 5xx is
   ``PaidSubmissionUnconfirmed`` (may have been billed, never auto-retried).
   The state is recorded with ``record_submission`` so a crash cannot lose the
   remote id.
4. **Stream with a byte cap.** The body is written in chunks; exceeding the cap
   aborts instead of filling the disk.
5. **fsync, then FULLY decode, then publish.** The temp file is flushed and
   fsync-ed, decoded end to end by ffmpeg (a truncated mp3 decodes "mostly"),
   and only then moved into place with ``os.replace``. Nothing is ever
   published that ffmpeg cannot read.
6. **``music_or_none`` degrades the render.** A failure here is a warning on
   the video, never a failed video.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry) -- the
donor's ``app/services/elevenlabs_music.py`` supplied the proxy/stream/validate
sequence and the ``finally``-deleted proxy. The paid-job submission contract,
the canonical ``MediaAsset`` result and the ``music`` track placement are
YMONEY's; no donor code is vendored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.providers.music.base import (
    MusicIntelligenceProvider,
    MusicNotConfigured,
    MusicProviderError,
    MusicRequest,
    MusicResult,
    music_prompt,
)
from app.services.paid_jobs import (
    SubmissionRecord,
    SubmissionState,
    classify_submit_exception,
    record_submission,
)

logger = logging.getLogger("ymoney.music")

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
VIDEO_TO_MUSIC_PATH = "/v1/music/video-to-music"
DEFAULT_MODEL_ID = "music_v2"
SUPPORTED_MODEL_IDS: frozenset[str] = frozenset({"music_v1", "music_v2"})

#: Video-to-Music's own ceiling; a longer video cannot be sent at all.
MAX_VIDEO_DURATION_SECONDS = 600.0
MAX_PROMPT_LENGTH = 1000
#: Proxy ceiling, mirroring the provider's upload limit.
MAX_PROXY_BYTES = 200 * 1024 * 1024
#: Generated audio ceiling; an anomaly must not fill the disk.
MAX_AUDIO_BYTES = 50 * 1024 * 1024
#: Proxy long edge. Analysis does not need more, and the master stays local.
PROXY_LONG_EDGE = 1280
PROXY_TIMEOUT_SECONDS = 600
DECODE_TIMEOUT_SECONDS = 120
#: A generated bed must cover at least this fraction of the video. A lower bar
#: than 1.0 is deliberate: a provider may legitimately return a slightly
#: shorter bed, but anything under half the video is a partial download.
MIN_COVERAGE_RATIO = 0.5
#: Submission read timeout: generation is genuinely slow, but still bounded.
SUBMIT_READ_TIMEOUT_SECONDS = 600.0
SUBMIT_CONNECT_TIMEOUT_SECONDS = 15.0
#: Error bodies can carry signed URLs, so only a small bounded prefix is read.
MAX_ERROR_BODY_BYTES = 500


def _credential(api_key: str = "", workspace_id: str = "") -> str:
    """Workspace credential first, then the process-level fallback.

    The key is used only as a request header and never enters a cache key, a
    filename, a log line or the result provenance.
    """
    if api_key.strip():
        return api_key.strip()
    try:
        from app.services.provider_settings import get_credential

        value, _source = get_credential("tts.elevenlabs_api_key", workspace_id or None)
        if value:
            return value.strip()
    except Exception as exc:  # noqa: BLE001 - credential lookup must not break music
        logger.warning("music: credential lookup failed (%s)", type(exc).__name__)
    from app.core.config import settings

    return str(settings.elevenlabs_api_key or "").strip()


def _safe_error(response: httpx.Response) -> str:
    """A bounded, single-line slice of an error body. Never the whole body."""
    try:
        raw = response.content[:MAX_ERROR_BODY_BYTES]
    except Exception:  # noqa: BLE001 - a broken body must not mask the status
        return response.reason_phrase or "request failed"
    return (raw.decode(response.encoding or "utf-8", errors="replace")
            .strip().replace("\n", " ")[:MAX_ERROR_BODY_BYTES]
            or response.reason_phrase or "request failed")


class ElevenLabsMusicProvider(MusicIntelligenceProvider):
    """ElevenLabs ``/v1/music/video-to-music`` (paid, per-generation billing)."""

    key = "elevenlabs_music"
    is_mock = False

    def __init__(self, api_key: str = "", *, base_url: str = "",
                 model_id: str = "", workspace_id: str = "",
                 client: httpx.Client | None = None):
        self.api_key = _credential(api_key, workspace_id)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        raw_model = str(model_id or DEFAULT_MODEL_ID).strip()
        # An unsupported model id is a config error; fall back safely rather
        # than sending a request the provider will reject.
        self.model_id = raw_model if raw_model in SUPPORTED_MODEL_IDS else DEFAULT_MODEL_ID
        self.workspace_id = workspace_id
        self._client = client

    # -- health -------------------------------------------------------------

    def available(self) -> bool:
        return bool(self.api_key) and shutil.which("ffmpeg") is not None

    def health(self) -> dict:
        return {
            "provider": self.key,
            "available": self.available(),
            "is_mock": False,
            "model_id": self.model_id,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "credential_configured": bool(self.api_key),
        }

    def estimate_cost(self, request: MusicRequest) -> float:
        # Flat, honest per-generation estimate; flagged as an estimate upstream.
        return 0.05

    # -- request validation -------------------------------------------------

    def _validate(self, request: MusicRequest) -> float:
        if not self.api_key:
            raise MusicNotConfigured(
                "elevenlabs music selected but no API key is configured - set "
                "ELEVENLABS_API_KEY or add tts.elevenlabs_api_key under Settings"
                " -> Connections")
        if shutil.which("ffmpeg") is None:
            raise MusicNotConfigured("elevenlabs music needs ffmpeg on PATH")
        try:
            duration = float(request.duration_seconds)
        except (TypeError, ValueError) as exc:
            raise MusicProviderError("video duration is not a number") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise MusicProviderError("video duration must be finite and positive")
        if duration > MAX_VIDEO_DURATION_SECONDS:
            raise MusicProviderError(
                f"video is {duration:.0f}s; video-to-music supports at most "
                f"{MAX_VIDEO_DURATION_SECONDS:.0f}s")
        if len(music_prompt(request)) > MAX_PROMPT_LENGTH:
            raise MusicProviderError(
                f"music prompt exceeds {MAX_PROMPT_LENGTH} characters")
        return duration

    # -- proxy --------------------------------------------------------------

    def build_proxy(self, video_path: Path) -> Path:
        """Audio-stripped, 1280-long-edge H.264 proxy for the analysis upload.

        The caller MUST delete it (this module does so in ``generate``'s
        ``finally``). ``-fs`` caps the output so a pathological input cannot
        produce an unbounded proxy.
        """
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".elevenlabs-music-proxy-", suffix=".mp4",
            dir=str(video_path.parent),
        )
        os.close(descriptor)
        proxy = Path(raw_path)
        cmd = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin", "-v", "error", "-y", "-i", str(video_path),
            "-vf", (f"scale=w={PROXY_LONG_EDGE}:h={PROXY_LONG_EDGE}:"
                    f"force_original_aspect_ratio=decrease:force_divisible_by=2"),
            "-an",                      # the music model only reads picture
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-fs", str(MAX_PROXY_BYTES),
            str(proxy),
        ]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=PROXY_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            _unlink(proxy)
            raise MusicProviderError("proxy generation timed out") from exc
        except OSError as exc:
            _unlink(proxy)
            raise MusicProviderError("could not run ffmpeg for the proxy") from exc
        if done.returncode != 0:
            detail = (done.stderr or "").strip().replace("\n", " ")[-MAX_ERROR_BODY_BYTES:]
            _unlink(proxy)
            raise MusicProviderError(f"proxy generation failed: {detail}")
        size = proxy.stat().st_size if proxy.is_file() else 0
        if size <= 0 or size > MAX_PROXY_BYTES:
            _unlink(proxy)
            raise MusicProviderError("proxy is empty or exceeds the 200 MB limit")
        logger.info("music: proxy ready (%d bytes, long edge %d)", size, PROXY_LONG_EDGE)
        return proxy

    # -- stream + validate + publish ---------------------------------------

    def _stream_to_file(self, response: httpx.Response, temp_path: Path) -> int:
        """Write the body in chunks under a byte cap, then fsync."""
        total = 0
        with open(temp_path, "wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise MusicProviderError("generated audio exceeds the 50 MB limit")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if total <= 0:
            raise MusicProviderError("provider returned no audio data")
        return total

    @staticmethod
    def verify_decodable(path: Path, min_seconds: float = 0.0) -> float:
        """FULL ffmpeg decode, then a duration coverage floor.

        A full decode (``-xerror -f null -``) is necessary but NOT sufficient:
        every MP3 frame is independently decodable, so a stream cut off
        mid-download decodes cleanly and exits 0. A header probe is weaker
        still. So after decoding we also require the file to actually COVER the
        video: fewer than ``MIN_COVERAGE_RATIO`` of the requested seconds means
        the artifact is partial, and a partial artifact is not published.

        Returns the measured duration. Raises on any failure.
        """
        ffmpeg = shutil.which("ffmpeg")
        cmd = [ffmpeg or "ffmpeg", "-nostdin", "-v", "error",
               "-xerror", "-i", str(path), "-f", "null", "-"]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=DECODE_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise MusicProviderError("decoding the generated audio timed out") from exc
        except OSError as exc:
            raise MusicProviderError("could not run ffmpeg to validate the audio") from exc
        if done.returncode != 0:
            detail = (done.stderr or "").strip().replace("\n", " ")[-MAX_ERROR_BODY_BYTES:]
            raise MusicProviderError(f"generated audio is not decodable: {detail}")

        measured = _probe_audio_seconds(path)
        if measured is None or measured <= 0:
            raise MusicProviderError("generated audio has no decodable audio stream")
        if min_seconds > 0 and measured < float(min_seconds) * MIN_COVERAGE_RATIO:
            raise MusicProviderError(
                f"generated audio is partial: {measured:.1f}s decoded for a "
                f"{float(min_seconds):.1f}s video")
        return measured

    def _publish(self, temp_path: Path, final_path: Path, min_seconds: float) -> float:
        """Validate then atomically move. The final path only ever holds a
        fully-written, fully-decodable artifact."""
        final_path.parent.mkdir(parents=True, exist_ok=True)
        measured = self.verify_decodable(temp_path, min_seconds)
        os.replace(temp_path, final_path)
        return measured

    # -- generation ---------------------------------------------------------

    def generate(self, request: MusicRequest) -> MusicResult:
        """One soundtrack. Raises rather than returning a half-built result."""
        duration = self._validate(request)
        prompt = music_prompt(request)
        output_dir = Path(request.workspace_id or ".") if request.workspace_id else Path(".")
        output_dir.mkdir(parents=True, exist_ok=True)

        record = SubmissionRecord(
            workspace_id=request.workspace_id, provider=self.key,
            state=SubmissionState.PREPARED,
        )
        record.idempotency_key = hashlib.sha256(
            f"{request.video_asset_id}|{duration:.3f}|{prompt}".encode()
        ).hexdigest()[:32]

        video_path = Path(str(request.video_path or ""))
        if not str(video_path) or not video_path.is_file():
            raise MusicProviderError("video file does not exist")

        final_path = output_dir / f"music_{record.idempotency_key}.mp3"
        descriptor, raw_temp = tempfile.mkstemp(prefix=".elevenlabs-music-",
                                                 suffix=".mp3", dir=str(output_dir))
        os.close(descriptor)
        temp_path = Path(raw_temp)
        proxy: Path | None = None
        try:
            proxy = self.build_proxy(video_path)

            headers = {"xi-api-key": self.api_key}
            try:
                with open(proxy, "rb") as handle:
                    files = {"videos": (proxy.name, handle, "video/mp4")}
                    data = {"model_id": self.model_id, "description": prompt}
                    with self._http().stream(
                        "POST", f"{self.base_url}{VIDEO_TO_MUSIC_PATH}",
                        headers=headers, params={"output_format": "mp3_44100_128"},
                        files=files, data=data,
                        timeout=(SUBMIT_CONNECT_TIMEOUT_SECONDS, SUBMIT_READ_TIMEOUT_SECONDS),
                    ) as response:
                        if response.status_code >= 400:
                            # Route a non-2xx through the SAME classifier as a
                            # transport failure: a 4xx is a definitive refusal
                            # (nothing billed), a 5xx may follow a task that was
                            # created and billed. Guessing here is what causes
                            # both a double charge and a silently lost job.
                            response.read()
                            detail = _safe_error(response)
                            failure = httpx.HTTPStatusError(
                                f"{response.status_code}: {detail}",
                                request=response.request, response=response,
                            )
                            classified = classify_submit_exception(
                                failure, provider=self.key,
                                remote_id=record.remote_id, attempt=record.attempts + 1)
                            record_submission(record, classified)
                            raise classified from failure
                        size = self._stream_to_file(response, temp_path)
            except httpx.HTTPError as exc:
                # THE money guard: a lost submit response may have been billed.
                # classify_submit_exception decides retry-vs-never-retry; this
                # provider re-raises so music_or_none() never resubmits.
                classified = classify_submit_exception(
                    exc, provider=self.key, remote_id=record.remote_id,
                    attempt=record.attempts + 1)
                record_submission(record, classified)
                raise classified from exc

            record_submission(record, None)          # remote id confirmed
            measured = self._publish(temp_path, final_path, duration)
            record.state = SubmissionState.SUCCEEDED
        except Exception:
            _unlink(temp_path)
            raise
        finally:
            # The proxy never outlives the request, success or not.
            if proxy is not None:
                _unlink(proxy)

        logger.info("music: generated soundtrack (%d bytes, state=%s)",
                    size, record.state.value)
        return MusicResult(
            provider=self.key,
            path=str(final_path),
            # The MEASURED decoded duration, not the requested one: the track
            # that exists is the one the timeline must be built from.
            duration_seconds=measured,
            prompt=prompt,
            state=record.state,
            remote_id=record.remote_id,
            file_size=final_path.stat().st_size,
            audio_codec="mp3",
            checksum=_sha256_file(final_path),
            provenance={
                "model_id": self.model_id,
                "base_url": self.base_url,
                "video_asset_id": request.video_asset_id,
                "campaign_id": request.campaign_id,
                "brand_music_preference": request.brand_music_preference,
                "mood": request.style()["mood"],
                "genre": request.style()["genre"],
                "idempotency_key": record.idempotency_key,
            },
        )

    def _http(self) -> httpx.Client:
        # An injected client (tests use MockTransport) wins over a fresh one.
        return self._client or httpx.Client(follow_redirects=False)


def _probe_audio_seconds(path: Path) -> float | None:
    """Duration of the decodable audio stream, or ``None`` when there is none.

    Uses ffprobe via the same stdlib-only subprocess rule as the rest of YMONEY
    (``providers/vision_ffprobe.py`` is the precedent). Reported honestly as
    ``None`` rather than guessed.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format",
           "-show_streams", str(path)]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return None
    streams = [s for s in (data.get("streams") or [])
               if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if not streams:
        return None
    candidates = [streams[0].get("duration"), (data.get("format") or {}).get("duration")]
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def _unlink(path: Path | None) -> None:
    """Best-effort delete that never masks the caller's original exception."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("music: could not remove %s (%s)", Path(path).name, exc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ID",
    "MAX_AUDIO_BYTES",
    "MAX_PROXY_BYTES",
    "MAX_VIDEO_DURATION_SECONDS",
    "PROXY_LONG_EDGE",
    "SUPPORTED_MODEL_IDS",
    "VIDEO_TO_MUSIC_PATH",
    "ElevenLabsMusicProvider",
]