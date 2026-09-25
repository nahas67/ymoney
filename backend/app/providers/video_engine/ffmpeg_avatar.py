"""FFmpeg Avatar Engine — real local video production with zero paid services.

Technology adopted from MoneyPrinterTurbo (running it as a separate service is
now OPTIONAL; its best techniques live here natively):

  1. TTS narration (YMONEY TTS provider layer: edge / kokoro / mock)
  2. Scene visuals — REAL stock video clips first (Pexels Videos API, same key
     as the photo provider), falling back to photos with Ken-Burns motion,
     then to a labeled placeholder slide. Never fails the render on scene art.
  3. Word-timed captions — Whisper transcription (faster-whisper when
     installed) or proportional text timing, chunked into short phrase
     groups like MPT's subtitle pipeline; burned in via ffmpeg subtitles
     filter with styling.
  4. BGM bed — loops a track from data/bgm/ under the narration at low
     volume with a fade-out (MPT bgm.py technique, ffmpeg sidechain-free).
  5. Hardware encoder autodetect (h264_qsv / h264_nvenc / h264_amf / libx264)
     with runtime fallback to libx264 on first failure (MPT video.py pattern).
  6. 9:16 / 16:9 / 1:1 output MP4 (H.264 + AAC).

This is a REAL render — fully local except the stock assets themselves.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import threading
import time
import uuid
from pathlib import Path

from loguru import logger

from app.providers.video_engine.base import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_NOT_FOUND,
    STATE_PROCESSING,
    STATE_QUEUED,
    BaseVideoEngine,
    RenderHandle,
    RenderRequest,
    RenderStatus,
    VideoEngineError,
)

WORK_DIR = Path("data/ffmpeg_engine")
CLIP_CACHE_DIR = WORK_DIR / "cache"
_MANIFEST_NAME = "job.json"
BGM_DIR = Path("data/bgm")
_WPS = 2.6  # words per second used to pace scenes from narration text

_ENCODER_CANDIDATES = ("h264_qsv", "h264_nvenc", "h264_amf", "libx264")
_encoder_lock = threading.Lock()
_runtime_disabled_encoders: set[str] = set()


def _available_encoders() -> set[str]:
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        return set(r.stdout.split()) if r.returncode == 0 else set()
    except Exception:
        return {"libx264"}


def pick_encoder(preferred: str = "") -> str:
    """Best available encoder; hardware first, libx264 always works."""
    with _encoder_lock:
        have = _available_encoders()
        order = [preferred] if preferred and preferred in _ENCODER_CANDIDATES else []
        order += [c for c in _ENCODER_CANDIDATES if c not in order]
        for enc in order:
            if enc in have and enc not in _runtime_disabled_encoders:
                return enc
        return "libx264"


def disable_encoder(enc: str) -> None:
    if enc == "libx264":
        return
    with _encoder_lock:
        _runtime_disabled_encoders.add(enc)
    logger.warning(f"[FFMPEG AVATAR] encoder {enc} disabled after failure; falling back to libx264")


def _font_file() -> str:
    """Locate a usable TTF for captions (raw path, unescaped)."""
    import os

    for cand in (
        os.environ.get("YMONEY_FONT_FILE") or "",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if cand and Path(cand).exists():
            return cand.replace("\\", "/")
    return ""


def _escape_subtitles_path(p: Path) -> str:
    """Escape a path for the ffmpeg subtitles= filter (Windows drive letters,
    backslashes, colons and quotes all need care)."""
    s = str(p.resolve()).replace("\\", "/")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(json.loads(out.stdout or "{}").get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


def _sanitize(text: str) -> str:
    """Escape text for FFmpeg drawtext."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
    )


def _srt_time(seconds: float) -> str:
    ms = max(0, int(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_captions(script: str, duration: float, out_path: Path,
                   whisper_audio: Path | None = None) -> bool:
    """Word-timed captions in MPT style: chunk the narration into short phrase
    groups (<= 7 words) and distribute time across words.

    Timing strategy: faster-whisper word timestamps when the package and model
    are available (accurate), else proportional-to-word-length distribution
    (deterministic, no dependency). Returns True when an SRT was written.
    """
    try:
        segments: list[tuple[float, float, str]] = []
        if whisper_audio is not None:
            segments = _whisper_segments(whisper_audio, script)
        if not segments:
            segments = _proportional_segments(script, duration)
        if not segments:
            return False

        lines: list[str] = []
        for i, (start, end, text) in enumerate(segments, 1):
            lines.append(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{text.strip()}\n")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as exc:
        logger.warning(f"[FFMPEG AVATAR] caption build failed: {exc}")
        return False


def _proportional_segments(script: str, duration: float) -> list[tuple[float, float, str]]:
    """Split script into <=7-word phrase groups; time weight = char length."""
    words = script.split()
    if not words:
        return []
    chunks: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        cur.append(w)
        if len(cur) >= 7 or w.endswith((".", "!", "?")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    texts = [" ".join(c) for c in chunks]
    weights = [max(len(t), 1) for t in texts]
    total = sum(weights)
    out: list[tuple[float, float, str]] = []
    t = 0.0
    for text, w in zip(texts, weights):
        d = duration * (w / total)
        out.append((t, min(t + d, duration), text))
        t += d
    return out


def _whisper_segments(audio: Path, script: str) -> list[tuple[float, float, str]]:
    """Real word timestamps via faster-whisper (optional dependency)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return []
    model_size = "tiny.en"
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(
            str(audio), beam_size=1, word_timestamps=True, vad_filter=True,
        )
        words: list[tuple[float, float, str]] = []
        for seg in segments:
            for w in (seg.words or []):
                token = (w.word or "").strip()
                if token:
                    words.append((w.start, w.end, token))
        if not words:
            return []
        # group words into <=7-word phrase groups, aligned to script punctuation
        out: list[tuple[float, float, str]] = []
        group: list[tuple[float, float, str]] = []
        for w in words:
            group.append(w)
            if len(group) >= 7 or w[2].endswith((".", "!", "?")):
                out.append((group[0][0], group[-1][1], " ".join(x[2] for x in group)))
                group = []
        if group:
            out.append((group[0][0], group[-1][1], " ".join(x[2] for x in group)))
        return out
    except Exception as exc:
        logger.info(f"[FFMPEG AVATAR] whisper unavailable ({type(exc).__name__}), using proportional captions")
        return []


def _bgm_allowlist() -> set[str] | None:
    """Licensed-track allowlist basenames, or None when no allowlist file exists."""
    import json as _json

    for cand in (BGM_DIR / ".allowlist.json", BGM_DIR / "licenses.json"):
        try:
            if cand.exists():
                data = _json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    tracks = data.get("tracks") or data.get("allowed") or []
                elif isinstance(data, list):
                    tracks = data
                else:
                    return None
                return {str(t).strip().lower() for t in tracks if str(t).strip()}
        except Exception:
            return None
    return None


def _pick_bgm() -> Path | None:
    if not BGM_DIR.exists():
        return None
    tracks = [p for p in BGM_DIR.iterdir()
              if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg") and p.is_file()]
    if not tracks:
        return None
    allowed = _bgm_allowlist()
    if allowed is not None:
        licensed = [p for p in tracks if p.name.lower() in allowed]
        if not licensed:
            logger.warning("[FFMPEG AVATAR] no allowlisted BGM tracks available — rendering without music")
            return None
        tracks = licensed
    return random.choice(tracks)


def _caption_style(height_label: str, position: str) -> str:
    """force_style payload honoring RenderRequest.subtitle_position.

    libass alignment: 2 = bottom-center (default), 5 = middle-center,
    8 = top-center. MarginV is the distance from the caption box to the
    nearest screen edge for that alignment.
    """
    fontsize = 15 if height_label == "1920" else 12
    alignment = {"top": 8, "center": 5}.get((position or "bottom").lower(), 2)
    margin = 40 if alignment == 8 else 0 if alignment == 5 else 60
    return (
        f"FontName=Arial,FontSize={fontsize},PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=1,"
        f"Alignment={alignment},MarginV={margin}"
    )


def _clamp_volume(volume, default: float = 0.2) -> float:
    """Sanitize a volume request: NaN/parse failure -> default, <=0 -> mute."""
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return default
    if v != v or v <= 0:  # noqa: PLR0124 — intentional NaN check (NaN != itself)
        return 0.0
    return min(v, 1.0)


def _bgm_for(bgm_type: str, bgm_file: str) -> Path | None:
    """Resolve the background bed honoring RenderRequest.bgm_type/bgm_file.

    none/off/mute disables the bed; an explicit file that exists wins over the
    random pick (but must pass the license allowlist when one exists);
    anything else falls back to the bundled bed selection.
    """
    t = (bgm_type or "").strip().lower()
    if t in ("none", "off", "mute", "no"):
        return None
    if bgm_file:
        p = Path(bgm_file)
        if p.is_file():
            allowed = _bgm_allowlist()
            if allowed is not None and p.name.lower() not in allowed:
                logger.warning(f"[FFMPEG AVATAR] BGM {p.name} not in license allowlist — muted")
                return None
            return p
    return _pick_bgm()


def _is_finance_topic(subject: str, script: str) -> bool:
    t = f"{subject} {script[:800]}".lower()
    return any(k in t for k in ("money", "income", "budget", "save", "invest", "earn", "cash", "finance", "stock", "crypto", "debt", "loan"))


class FFmpegAvatarEngine(BaseVideoEngine):
    engine_name = "ffmpeg_avatar"

    def __init__(self):
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- health ---------------------------------------------------------------

    def health(self) -> bool:
        try:
            r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    def version(self) -> str | None:
        try:
            r = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                               text=True, timeout=15)
            return (r.stdout or "").split("\n")[0][:80] or None
        except Exception:
            return None

    # -- submit ---------------------------------------------------------------

    def submit(self, req: RenderRequest) -> RenderHandle:
        task_id = f"ffa-{uuid.uuid4().hex[:12]}"
        job = {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "subject": req.subject,
            "workspace_id": req.workspace_id,
            "pid": os.getpid(),
            "updated_at": time.time(),
        }
        with self._lock:
            self._jobs[task_id] = job
        self._persist_job(task_id, job)
        t = threading.Thread(target=self._render, args=(task_id, req), daemon=True)
        t.start()
        logger.info(f"[FFMPEG AVATAR ENGINE] render queued {task_id}")
        return RenderHandle(engine_task_id=task_id, engine=self.engine_name)

    # -- status ----------------------------------------------------------------

    def status(self, handle: RenderHandle) -> RenderStatus:
        with self._lock:
            job = self._jobs.get(handle.engine_task_id)
        if job is None:
            job = self._load_job(handle.engine_task_id)
            if job is not None:
                with self._lock:
                    self._jobs[handle.engine_task_id] = job
        if job is None:
            return RenderStatus(state=STATE_NOT_FOUND, progress=0, error="render task not found")
        if job.get("status") in ("queued", "processing") and job.get("pid") != os.getpid():
            job["status"] = "failed"
            job["error"] = "render interrupted by application restart"
            self._persist_job(handle.engine_task_id, job)
        mapping = {
            "queued": STATE_QUEUED,
            "processing": STATE_PROCESSING,
            "complete": STATE_COMPLETE,
            "failed": STATE_FAILED,
        }
        return RenderStatus(
            state=mapping.get(job["status"], STATE_NOT_FOUND),
            progress=job.get("progress", 0),
            videos=job.get("videos") or ([f"{handle.engine_task_id}/final-1.mp4"]
            if job["status"] == "complete" else []),
            error=job.get("error", ""),
        )

    def cancel_job(self, handle: RenderHandle) -> bool:
        with self._lock:
            job = self._jobs.get(handle.engine_task_id)
            if not job or job["status"] in ("complete", "failed"):
                return False
            job["cancel"] = True
            job["status"] = "failed"
            job["error"] = "cancelled"
            self._persist_job(handle.engine_task_id, job)
        return True

    def delete_job(self, handle: RenderHandle) -> bool:
        import shutil

        d = WORK_DIR / handle.engine_task_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        with self._lock:
            return self._jobs.pop(handle.engine_task_id, None) is not None or d.exists()

    def get_video_url(self, handle: RenderHandle) -> str | None:
        st = self.status(handle)
        return st.videos[0] if st.videos else None

    def fetch_video_bytes(self, url_or_path: str) -> bytes:
        p = WORK_DIR / url_or_path
        if not p.exists():
            p = WORK_DIR / Path(url_or_path).name.replace(".mp4", "") / "final-1.mp4"
        if not p.exists():
            raise VideoEngineError(f"render artifact not found: {url_or_path}")
        return p.read_bytes()

    def estimate_cost(self, req: RenderRequest) -> float:
        return 0.0  # fully local

    def get_capabilities(self) -> set[str]:
        return {"VIDEO_GENERATION", "SUBTITLES", "TTS", "PORTRAIT", "LANDSCAPE", "SQUARE"}

    # -- render pipeline --------------------------------------------------------

    def _render(self, task_id: str, req: RenderRequest) -> None:
        # Threads do not inherit ContextVar values. Carry the tenant explicitly
        # in RenderRequest so TTS/image/stock providers cannot fall back to a
        # different workspace's credentials.
        from app.services.provider_settings import workspace_scope

        with workspace_scope(req.workspace_id):
            self._render_impl(task_id, req)

    def _render_impl(self, task_id: str, req: RenderRequest) -> None:
        with self._lock:
            job = self._jobs[task_id]
        out_dir = WORK_DIR / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                if job.get("cancel"):
                    job["status"] = "failed"
                    job["error"] = "cancelled before start"
                    self._persist_job(task_id, job)
                    return
                job["status"] = "processing"
                job["progress"] = 5
                self._persist_job(task_id, job)

            # 1) narration ------------------------------------------------------
            from app.providers.tts import TTSError, get_tts_provider

            if not (req.script or "").strip():
                raise VideoEngineError("render refused: script is empty")
            tts = get_tts_provider()
            audio_path = out_dir / "narration.mp3"
            try:
                res = tts.synthesize(
                    req.script,
                    voice=req.voice_name,
                    rate=req.voice_rate,
                    volume=req.voice_volume,
                )
                audio_path.write_bytes(res.audio_bytes)
            except TTSError as exc:
                raise VideoEngineError(f"tts failed: {exc}") from exc
            self._progress(job, 20)

            duration = max(3.0, _probe_duration(audio_path)
                           or max(3.0, len(req.script.split()) / _WPS))
            n_scenes = max(1, min(8, round(duration / req.clip_duration)
                                  if req.clip_duration else 1))

            # 2) scene visuals — stock VIDEO clips first (MPT material technique),
            #    photos with Ken Burns second, labeled slide last ---------------
            scene_inputs, scene_labels = self._gather_scenes(
                out_dir, req, n_scenes, duration, job
            )
            self._progress(job, 55)

            # 3) word-timed captions (adopted from MPT subtitle.py) -------------
            srt_path = out_dir / "captions.srt"
            has_captions = False
            if req.subtitle_enabled:
                has_captions = build_captions(req.script, duration, srt_path,
                                              whisper_audio=audio_path)
            self._progress(job, 60)

            # 4) assemble: per-scene trim/scale + xfade + captions + BGM --------
            if req.aspect_ratio == "9:16":
                w, h = "1080", "1920"
            elif req.aspect_ratio == "1:1":
                w, h = "1080", "1080"
            else:
                w, h = "1920", "1080"

            n = len(scene_inputs)
            scene_dur = duration / n
            inputs: list[str] = []
            for entry in scene_inputs:  # entry = (path, is_video)
                if entry[1]:
                    inputs += ["-stream_loop", "-1", "-t", f"{scene_dur:.2f}",
                               "-i", str(entry[0])]
                else:
                    inputs += ["-loop", "1", "-t", f"{scene_dur:.2f}",
                               "-i", str(entry[0])]
            inputs += ["-i", str(audio_path)]
            audio_idx = n

            filters: list[str] = []
            frames = max(1, int(scene_dur * 30))
            for idx in range(n):
                if scene_labels[idx] == "video":
                    # trim, normalize fps, cover-crop (no zoompan on real footage)
                    filters.append(
                        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                        f"crop={w}:{h},fps=30,setsar=1[v{idx}]"
                    )
                else:
                    filters.append(
                        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                        f"crop={w}:{h},"
                        f"zoompan=z='min(zoom+0.0008,1.12)':d={frames}"
                        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30,"
                        f"setsar=1[v{idx}]"
                    )
            if n > 1:
                acc = "[v0]"
                for idx in range(1, n):
                    outl = f"[vx{idx}]"
                    filters.append(
                        f"{acc}[v{idx}]xfade=transition=fade:duration=0.5:"
                        f"offset={max(0.1, scene_dur * idx - 0.5):.2f}{outl}"
                    )
                    acc = outl
                last = acc
            else:
                last = "[v0]"

            # captions burned via subtitles filter (styled, word-timed); the
            # position/style honor RenderRequest.subtitle_position
            if has_captions and _font_file():
                fpath = _escape_subtitles_path(srt_path)
                filters.append(
                    f"{last}subtitles='{fpath}'"
                    f":force_style='{_caption_style(h, req.subtitle_position)}'"
                    f"[vcap]"
                )
                last = "[vcap]"

            # finance disclaimer footer burn-in (fail-closed monetization gate)
            if _is_finance_topic(req.subject, req.script) and _font_file():
                font = _font_file()
                filters.append(
                    f"{last}drawtext=fontfile='{font}':text='Not financial advice':"
                    f"fontsize=28:fontcolor=white@0.9:borderw=2:bordercolor=black@0.8:"
                    f"x=(w-text_w)/2:y=h-140[vcap2]"
                )
                last = "[vcap2]"

            # 5) BGM bed under narration (adopted from MPT bgm.py); honors
            #    RenderRequest.bgm_type / bgm_file / bgm_volume. Licensed tracks
            #    only (allowlist); final mix gently leveled (silence-safe: no
            #    loudnorm single-pass, which explodes on digital silence).
            bgm = _bgm_for(req.bgm_type, req.bgm_file)
            bgm_vol = _clamp_volume(req.bgm_volume)
            map_audio = f"{audio_idx}:a"
            _master = "aresample=48000,acompressor=threshold=-20dB:ratio=3:attack=5:release=50,alimiter=limit=0.95"
            if bgm and bgm_vol > 0:
                inputs += ["-stream_loop", "-1", "-i", str(bgm)]
                bgm_idx = audio_idx + 1
                filters.append(
                    f"[{bgm_idx}:a]volume={bgm_vol:.2f},afade=t=out:st={max(0.0, duration - 2.5):.2f}"
                    f":d=2.5[bgm]"
                )
                filters.append(
                    f"[{audio_idx}:a][bgm]amix=inputs=2:duration=first:dropout_transition=3,{_master}[aout]"
                )
                map_audio = "[aout]"
                logger.info(f"[FFMPEG AVATAR] BGM licensed track: {bgm.name}")
            else:
                filters.append(f"[{audio_idx}:a]{_master}[aout]")
                map_audio = "[aout]"

            encoder = pick_encoder()
            if encoder == "libx264":
                vquality = ["-crf", "23"]
                vpreset = ["-preset", "veryfast"]
            else:
                vquality = ["-b:v", "6M"]
                vpreset = ["-preset", "fast"]
            cmd = (
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
                + inputs
                + [
                    "-filter_complex", ";".join(filters),
                    "-map", last,
                    "-map", map_audio,
                    "-c:v", encoder, *vpreset, *vquality,
                    "-pix_fmt", "yuv420p", "-r", "30",
                    "-c:a", "aac", "-b:a", "160k",
                    "-shortest",
                    str(out_dir / "final-1.mp4"),
                ]
            )

            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
            if r.returncode != 0 and encoder != "libx264":
                # hardware encoder failed once → disable and retry on libx264
                disable_encoder(encoder)
                encoder = "libx264"
                i = cmd.index("-c:v") + 1
                cmd[i] = encoder
                # swap bitrate for CRF
                j = cmd.index("-b:v")
                cmd[j:j + 2] = ["-crf", "23"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
            if r.returncode != 0:
                raise VideoEngineError(f"ffmpeg failed: {(r.stderr or '')[-400:]}")
            self._progress(job, 100)
            with self._lock:
                job["status"] = "complete"
                job["progress"] = 100
                job["videos"] = [f"{task_id}/final-1.mp4"]
                self._persist_job(task_id, job)
        except Exception as exc:
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:400]
                self._persist_job(task_id, job)
            logger.error(f"[FFMPEG AVATAR] {task_id} failed: {exc}")

    # -- scene gathering ---------------------------------------------------------

    def _gather_scenes(self, out_dir: Path, req: RenderRequest, n_scenes: int,
                       duration: float, job: dict) -> tuple[list[tuple[Path, bool]], list[str]]:
        """Return ([(path, is_video)], [kind_label]). Order: stock video clips →
        photos w/ Ken Burns → labeled placeholder. Never raises."""
        scene_inputs: list[tuple[Path, bool]] = []
        labels: list[str] = []
        try:
            clips = self._pexels_video_clips(out_dir, req, n_scenes)
            for p in clips:
                scene_inputs.append((p, True))
                labels.append("video")
        except Exception as exc:
            logger.info(f"[FFMPEG AVATAR] stock video unavailable: {type(exc).__name__}: {str(exc)[:100]}")

        remaining = n_scenes - len(scene_inputs)
        if remaining > 0:
            from app.providers.images import ImageProviderError, get_image_provider

            img_prov = get_image_provider()
            for i in range(remaining):
                with self._lock:
                    if job.get("cancel"):
                        raise VideoEngineError("cancelled")
                prompt = (f"{req.subject} - scene {i + 1} of {remaining}. "
                          f"Keywords: {', '.join(req.keywords[:6])}")
                try:
                    blobs = img_prov.generate(prompt, size="1024x576", n=1)
                except ImageProviderError as exc:
                    logger.warning(f"[FFMPEG AVATAR] image failed scene {i}: {exc}")
                    continue
                p = out_dir / f"scene_{i}.jpg"
                p.write_bytes(blobs[0])
                scene_inputs.append((p, False))
                labels.append("image")

        if not scene_inputs:
            # LAST RESORT, honestly labeled: primary providers are down.
            logger.warning("[FFMPEG AVATAR] all scene sources failed; using labeled placeholder slide")
            from app.providers.images import MockImageProvider

            fallback = MockImageProvider()
            p = out_dir / "scene_0.jpg"
            p.write_bytes(fallback.generate(
                f"SIMULATED — {req.subject}", size="1024x576", n=1
            )[0])
            scene_inputs.append((p, False))
            labels.append("image")
        return scene_inputs, labels

    def _pexels_video_clips(self, out_dir: Path, req: RenderRequest,
                            n_clips: int) -> list[Path]:
        """Short stock video clips matching the subject (Pexels Videos API —
        same free key as photos). Downloads are cached on disk keyed by the
        query (MPT material_cache technique) so repeated renders reuse them.
        Raises when unavailable; caller falls back to photos."""
        import hashlib

        import httpx

        from app.services.provider_settings import get_credential

        key, _src = get_credential("pexels.api_key")
        if not key:
            from app.core.config import settings as _cfg

            key = _cfg.pexels_api_key
        if not key:
            raise VideoEngineError("no pexels key")

        query = " ".join((req.keywords[:4] or [req.subject])[:4]) or req.subject
        orientation = "portrait" if req.aspect_ratio == "9:16" else "landscape"
        wanted = (1080, 1920) if req.aspect_ratio == "9:16" else (1920, 1080)

        # search results (cheap) are fetched fresh; clip bytes (expensive) cached
        resp = httpx.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": min(max(n_clips * 2, 4), 12),
                    "orientation": orientation},
            headers={"Authorization": key},
            timeout=20,
        )
        resp.raise_for_status()
        videos = resp.json().get("videos") or []

        CLIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        qhash = hashlib.sha256(f"{query}|{orientation}".encode()).hexdigest()[:10]

        paths: list[Path] = []
        for v in videos:
            if len(paths) >= n_clips:
                break
            files = v.get("video_files") or []
            # nearest file not larger than target height
            best = None
            for f in files:
                hgt = int(f.get("height") or 0)
                if f.get("file_type") != "video/mp4":
                    continue
                if best is None or abs(hgt - wanted[1]) < abs(int(best.get("height") or 0) - wanted[1]):
                    best = f
            if not best or not best.get("link"):
                continue
            vid = v.get("id") or hashlib.sha1(str(best.get("link")).encode()).hexdigest()[:8]
            cached = CLIP_CACHE_DIR / f"{qhash}_{vid}.mp4"
            if cached.exists() and cached.stat().st_size > 50_000:
                paths.append(cached)
                continue
            try:
                dl = httpx.get(best["link"], timeout=90, follow_redirects=True)
                dl.raise_for_status()
                if len(dl.content) < 50_000:
                    continue
                tmp = cached.with_suffix(".part")
                tmp.write_bytes(dl.content)
                tmp.replace(cached)  # atomic: no torn cache entries
                paths.append(cached)
            except Exception as exc:
                logger.warning(f"[FFMPEG AVATAR] clip download failed: {exc}")
                continue
        return paths

    @staticmethod
    def _manifest_path(task_id: str) -> Path:
        return WORK_DIR / task_id / _MANIFEST_NAME

    @classmethod
    def _persist_job(cls, task_id: str, job: dict) -> None:
        """Persist task state atomically so a new process can reconcile it."""
        path = cls._manifest_path(task_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v for k, v in job.items() if k != "cancel"}
            data["updated_at"] = time.time()
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning(f"[FFMPEG AVATAR] unable to persist task {task_id}: {exc}")

    @classmethod
    def _load_job(cls, task_id: str) -> dict | None:
        path = cls._manifest_path(task_id)
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError) as exc:
            logger.warning(f"[FFMPEG AVATAR] unable to load task {task_id}: {exc}")
            return None

    def _progress(self, job: dict, pct: int) -> None:
        with self._lock:
            job["progress"] = min(99, pct)
            self._persist_job(job["task_id"], job)
