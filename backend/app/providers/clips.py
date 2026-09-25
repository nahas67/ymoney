"""Clip repurposing — turn long-form video into short-form segments (E1: link-to-shorts).

Pipeline: acquire (yt-dlp/local) → transcribe (faster-whisper, optional) →
scene-detect (PySceneDetect, optional) → rank viral moments (LLM w/ deterministic
fallback) → cut 9:16 (face-tracked when MediaPipe present, center-crop otherwise)
→ burn captioned presets.

Every optional dependency degrades gracefully: missing pieces are reported via
status() and the pipeline falls back to deterministic behavior. Nothing here
requires a GPU or a paid service.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.services.storage import STORAGE_ROOT, probe_metadata


class ClipError(Exception):
    pass


def yt_dlp_available() -> bool:
    return bool(shutil.which("yt-dlp"))


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def whisper_available() -> bool:
    return _have_module("faster_whisper")


def scenedetect_available() -> bool:
    return _have_module("scenedetect")


def face_track_available() -> bool:
    return _have_module("mediapipe") and _have_module("cv2")


# ---------------------------------------------------------------------------
# Caption presets (SRT + ASS force_style for the ffmpeg subtitles filter)
# ---------------------------------------------------------------------------

_FALLBACK_PRESETS: dict[str, str] = {
    "minimal": (
        "FontName=Arial,FontSize=14,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H80000000,BorderStyle=1,Outline=1,Shadow=0,"
        "Alignment=2,MarginV=48"
    ),
    "pop": (
        "FontName=Arial,FontSize=19,Bold=1,PrimaryColour=&H0000FFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
        "Alignment=2,MarginV=64"
    ),
    "karaoke": (
        "FontName=Arial,FontSize=16,Bold=1,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,"
        "Alignment=2,MarginV=56"
    ),
}


def _registry_presets() -> dict[str, str]:
    try:
        from app.services.templates import list_templates

        out = {}
        for t in list_templates("captions"):
            style = (t.get("payload") or {}).get("ass_style")
            if style:
                out[t["id"]] = style
        return out or dict(_FALLBACK_PRESETS)
    except Exception:
        return dict(_FALLBACK_PRESETS)


CAPTION_PRESETS: dict[str, str] = _registry_presets()


def caption_style(preset: str) -> str:
    return CAPTION_PRESETS.get((preset or "").lower(), CAPTION_PRESETS["minimal"])


def _preset_style(preset: str, workspace_id: str = "") -> str:
    """Workspace-aware preset: override in settings wins, else built-in."""
    try:
        from app.services.templates import resolve_template

        style = (resolve_template("captions", (preset or "minimal").lower(),
                                  workspace_id or None).get("payload") or {}).get("ass_style")
        if style:
            return style
    except Exception:
        pass
    return caption_style(preset)


@dataclass
class ClipResult:
    path: str            # stored path relative to CWD (storage boundary format)
    start: float
    end: float
    duration: float
    width: int | None = None
    height: int | None = None
    source_url: str = ""
    title: str = ""
    score: float = 0.0
    hook: str = ""
    reason: str = ""
    preset: str = "minimal"


@dataclass
class SourceInfo:
    title: str
    duration: float | None
    width: int | None
    height: int | None
    local_path: Path


@dataclass
class ViralMoment:
    start: float
    end: float
    score: float
    hook: str = ""
    reason: str = ""
    text: str = ""


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


class ClipRepurposer:
    """Download (optional) + transcribe + rank + cut a source into shorts."""

    def __init__(self, work_root: Path | None = None):
        self.work_root = work_root or (STORAGE_ROOT / "_clipwork")

    # -- availability -------------------------------------------------------

    def status(self) -> dict:
        return {
            "yt_dlp": yt_dlp_available(),
            "ffmpeg": ffmpeg_available(),
            "download_supported": yt_dlp_available(),
            "cut_supported": ffmpeg_available(),
            "whisper": whisper_available(),
            "scene_detect": scenedetect_available(),
            "face_track": face_track_available(),
            "caption_presets": sorted(CAPTION_PRESETS),
        }

    # -- acquisition ---------------------------------------------------------

    def acquire(self, url: str, workspace_id: str) -> SourceInfo:
        """Download the source into workspace temp storage and probe it.

        Raises ClipError with a clear reason when yt-dlp is unavailable or the
        URL is not downloadable. Local file paths are also accepted directly.
        """
        if not url:
            raise ClipError("source URL is empty")
        dest_dir = self.work_root / workspace_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_tpl = str(dest_dir / "source.%(ext)s")

        if url.startswith(("http://", "https://")):
            if not yt_dlp_available():
                raise ClipError(
                    "yt-dlp is not installed — install it (pip install yt-dlp) to "
                    "enable source download, or provide a local file path"
                )
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f", "mp4/best[ext=mp4]/best",
                "--no-progress",
                "-o", out_tpl,
                "--", url,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=1800)
            except subprocess.TimeoutExpired as exc:
                raise ClipError("download timed out after 30 minutes") from exc
            if proc.returncode != 0:
                stderr = (proc.stderr or b"").decode(errors="replace")[:300]
                raise ClipError(f"download failed: {stderr or 'yt-dlp error'}")
            files = sorted(dest_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
            if not files:
                raise ClipError("download produced no file")
            local = files[0]
        else:
            local = Path(url)
            if not local.exists():
                raise ClipError(f"local source not found: {url}")

        meta = self._probe(local)
        return SourceInfo(
            title=meta.get("title") or local.stem,
            duration=meta.get("duration"),
            width=meta.get("width"),
            height=meta.get("height"),
            local_path=local,
        )

    # -- transcription ---------------------------------------------------------

    def transcribe_segments(self, source: SourceInfo) -> list[TranscriptSegment]:
        """Word-grouped transcript segments; [] when whisper is unavailable."""
        if not _have_module("faster_whisper"):
            return []
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
            raw, _ = model.transcribe(str(source.local_path), beam_size=1, vad_filter=True)
            out: list[TranscriptSegment] = []
            buf: list[str] = []
            start = 0.0
            for seg in raw:
                words = (seg.text or "").strip()
                if not words:
                    continue
                if not buf:
                    start = float(seg.start)
                buf.append(words)
                if len(" ".join(buf).split()) >= 12 or words.endswith((".", "!", "?")):
                    out.append(TranscriptSegment(start=start, end=float(seg.end), text=" ".join(buf)))
                    buf = []
            if buf:
                out.append(TranscriptSegment(start=start, end=float(out[-1].end if out else start + 1), text=" ".join(buf)))
            return out
        except Exception as exc:
            logger.info(f"[clips] transcription unavailable ({type(exc).__name__}); continuing without it")
            return []

    # -- scene detection ---------------------------------------------------------

    def detect_scenes(self, source: SourceInfo) -> list[tuple[float, float]]:
        """Shot boundaries via PySceneDetect; [] when unavailable."""
        if not scenedetect_available():
            return []
        try:
            from scenedetect import ContentDetector, SceneManager, open_video

            video = open_video(str(source.local_path))
            manager = SceneManager()
            manager.add_detector(ContentDetector())
            manager.detect_scenes(video, show_progress=False)
            return [
                (s[0].get_seconds(), s[1].get_seconds())
                for s in manager.get_scene_list()
            ]
        except Exception as exc:
            logger.info(f"[clips] scene detection unavailable ({type(exc).__name__}); using fixed windows")
            return []

    # -- viral-moment ranking ---------------------------------------------------------

    def rank_moments(self, segments: list[TranscriptSegment] | list[dict],
                     max_moments: int = 5, workspace_id: str = "") -> list[ViralMoment]:
        """Score candidate windows 0..100 with hook + reason each.

        Uses the LLM when configured, otherwise a deterministic heuristic
        (question/number/emotion density) so offline runs stay reproducible.
        """
        norm = [
            {"start": float(s.start if isinstance(s, TranscriptSegment) else s.get("start", 0)),
             "end": float(s.end if isinstance(s, TranscriptSegment) else s.get("end", 0)),
             "text": str(s.text if isinstance(s, TranscriptSegment) else s.get("text", ""))}
            for s in (segments or [])
        ]
        norm = [s for s in norm if s["text"].strip() and s["end"] > s["start"]]
        if not norm:
            return []
        windows = _candidate_windows(norm)
        if not windows:
            return []
        scored = self._llm_rank(windows, workspace_id) or _heuristic_rank(windows)
        return trim_to_best(scored, moment_count_target(max_moments))

    def _llm_rank(self, windows: list[dict], workspace_id: str) -> list[ViralMoment] | None:
        try:
            from app.providers import llm as llm_mod

            if not llm_mod.llm_available():
                return None
            import json as _json

            res = llm_mod.complete_json(
                system=(
                    "You pick viral short-form moments from a transcript. Score each window "
                    "0-100 for standalone short potential (hook strength, payoff, emotion, "
                    "numbers/surprise). Return JSON: {\"moments\": [{\"index\": int, "
                    "\"score\": number, \"hook\": string, \"reason\": string}]}. "
                    "Reply with JSON only."
                ),
                user=_json.dumps([{"index": i, "text": w["text"][:400]} for i, w in enumerate(windows)]),
                workspace_id=workspace_id,
                tier="cheap",
                temperature=0.4,
                max_tokens=1200,
            )
            out: list[ViralMoment] = []
            for m in (res.get("moments") or []):
                try:
                    w = windows[int(m["index"])]
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                out.append(ViralMoment(
                    start=w["start"], end=w["end"],
                    score=max(0.0, min(100.0, float(m.get("score", 50)))),
                    hook=str(m.get("hook", ""))[:180],
                    reason=str(m.get("reason", ""))[:200],
                    text=w["text"],
                ))
            return out or None
        except Exception as exc:
            logger.info(f"[clips] LLM ranking unavailable ({type(exc).__name__}); using heuristic")
            return None

    # -- face-tracked reframe ---------------------------------------------------------

    def face_track_centers(self, source: SourceInfo,
                           segments: list[tuple[float, float]]) -> dict[int, float]:
        """Median face x-center (0..1) per segment index; {} when unavailable."""
        if not face_track_available() or not segments:
            return {}
        try:
            import cv2
            import mediapipe as mp
        except ImportError:
            return {}
        out: dict[int, float] = {}
        try:
            fd = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5)
            tmp = self.work_root / "_faces"
            tmp.mkdir(parents=True, exist_ok=True)
            for idx, (start, end) in enumerate(segments):
                span = max(1.0, end - start)
                n_frames = max(1, min(5, int(span)))
                xs: list[float] = []
                for f in range(n_frames):
                    t = start + span * (f + 0.5) / n_frames
                    frame_p = tmp / f"seg{idx}_{f}.jpg"
                    proc = subprocess.run(
                        ["ffmpeg", "-y", "-v", "quiet", "-ss", f"{t:.2f}",
                         "-i", str(source.local_path), "-frames:v", "1", str(frame_p)],
                        capture_output=True, timeout=60,
                    )
                    if proc.returncode != 0 or not frame_p.exists():
                        continue
                    img = cv2.imread(str(frame_p))
                    if img is None:
                        continue
                    res = fd.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                    if res.detections:
                        box = max(res.detections, key=lambda d: d.score[0]).location_data.relative_bounding_box
                        xs.append(max(0.0, min(1.0, box.xmin + box.width / 2)))
                    frame_p.unlink(missing_ok=True)
                if xs:
                    xs.sort()
                    out[idx] = xs[len(xs) // 2]
            fd.close()
        except Exception as exc:
            logger.info(f"[clips] face tracking failed ({type(exc).__name__}); center crop fallback")
            return {}
        return out

    # -- cutting ---------------------------------------------------------------

    def cut_segments(self, source: SourceInfo, workspace_id: str, *,
                     segments: list[tuple[float, float]] | None = None,
                     moments: list[ViralMoment] | None = None,
                     clip_seconds: float = 45.0,
                     max_clips: int = 5,
                     vertical: bool = True,
                     caption_preset: str = "minimal",
                     captions: dict[int, str] | None = None,
                     face_track: bool = False) -> list[ClipResult]:
        """Cut highlight segments into vertical shorts with burned captions.

        moments (ranked) win over raw segments; captions maps clip index (1-based)
        to SRT text. face_track enables face-centered crop when MediaPipe exists.
        """
        if not ffmpeg_available():
            raise ClipError("ffmpeg/ffprobe not available — install ffmpeg to enable cutting")
        duration = source.duration or 0.0
        if duration <= 0:
            raise ClipError("source duration unknown; cannot plan segments")

        meta_by_idx: dict[int, ViralMoment] = {}
        if moments:
            segments = [(m.start, m.end) for m in moments]
            meta_by_idx = {i + 1: m for i, m in enumerate(moments)}
        if not segments:
            segments = self._even_segments(duration, clip_seconds, max_clips)

        centers = self.face_track_centers(source, segments) if face_track else {}
        style = _preset_style(caption_preset, workspace_id)
        out_dir = STORAGE_ROOT / workspace_id
        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[ClipResult] = []
        for i, (start, end) in enumerate(segments, start=1):
            end = min(end, duration)
            if end - start < 3:
                continue
            name = f"clip-{source.local_path.stem[:40]}-{i:02d}.mp4"
            dest = out_dir / name
            vf = self._vf_chain(vertical, centers.get(i - 1))
            srt_path: Path | None = None
            if captions and captions.get(i):
                srt_path = out_dir / f".clip-{i:02d}.srt"
                srt_path.write_text(_srt_single(captions[i], end - start), encoding="utf-8")
                vf = f"{vf},subtitles='{_escape_filter_path(srt_path)}':force_style='{style}'" if vf else \
                    f"subtitles='{_escape_filter_path(srt_path)}':force_style='{style}'"
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}",
                   "-i", str(source.local_path), "-t", f"{end - start:.2f}"]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", str(dest)]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=900)
            except subprocess.TimeoutExpired:
                logger.warning(f"clip {i} cut timed out")
                continue
            finally:
                if srt_path and srt_path.exists():
                    srt_path.unlink(missing_ok=True)
            if proc.returncode != 0 or not dest.exists():
                stderr = (proc.stderr or b"").decode(errors="replace")[:200]
                logger.warning(f"clip {i} failed: {stderr}")
                continue
            meta = self._probe(dest)
            mom = meta_by_idx.get(i)
            results.append(ClipResult(
                path=str(dest),
                start=round(start, 2),
                end=round(end, 2),
                duration=round(end - start, 2),
                width=meta.get("width"),
                height=meta.get("height"),
                title=source.title,
                score=round(mom.score, 1) if mom else 0.0,
                hook=mom.hook if mom else "",
                reason=mom.reason if mom else "",
                preset=caption_preset,
            ))
        return results

    @staticmethod
    def _vf_chain(vertical: bool, face_x: float | None) -> str:
        if not vertical:
            return ""
        if face_x is None:
            return "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920"
        # face-centered crop: x follows the tracked face, clamped to frame
        fx = max(0.0, min(1.0, face_x))
        return (
            f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)':"
            f"'x=min(max(({fx:.3f}*iw-w/2)\\,0)\\,iw-w)':"
            f"'y=(ih-h)/2',scale=1080:1920"
        )

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _even_segments(duration: float, clip_seconds: float, max_clips: int) -> list[tuple[float, float]]:
        """Deterministic non-overlapping windows across the timeline."""
        n = max(1, min(max_clips, int(duration // max(clip_seconds, 5))))
        window = duration / n
        out = []
        for i in range(n):
            start = i * window
            end = min(start + min(clip_seconds, window), duration)
            if end - start >= 3:
                out.append((start, end))
        return out

    @staticmethod
    def _probe(path: Path) -> dict:
        if not shutil.which("ffprobe"):
            return {}
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, timeout=30,
            )
            data = json.loads(out.stdout or "{}")
        except (subprocess.SubprocessError, ValueError) as exc:
            logger.warning(f"ffprobe failed for {path.name}: {exc}")
            return {}
        fmt = data.get("format", {})
        width = height = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(stream.get("width") or 0) or None
                height = int(stream.get("height") or 0) or None
                break
        return {
            "title": fmt.get("title", ""),
            "duration": float(fmt["duration"]) if fmt.get("duration") else None,
            "width": width,
            "height": height,
        }


def probe_source_quality(url: str, timeout: int = 25) -> dict:
    """Fast pre-flight probe of a repurpose source (openshorts quality_probe pattern).

    Local paths: existence + ffprobe dimensions. Remote URLs: yt-dlp metadata
    only (no download). Never raises — failures degrade to probe="unknown"
    and callers proceed anyway (fail-open): a probe problem must never cost
    the job, but a known-bad source earns a warning before burning a cycle.
    """
    result: dict = {"max_height": 0, "mode": None, "duration": 0,
                    "probe": "unknown", "warning": ""}
    if not (url or "").strip():
        result["warning"] = "source URL is empty"
        return result
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        p = Path(url)
        if not p.exists():
            result["warning"] = "local source not found"
            return result
        try:
            meta = probe_metadata(p)
        except Exception:
            meta = {}
        result.update(mode="local", probe="ok",
                      width=meta.get("width"), height=meta.get("height"),
                      duration=meta.get("duration") or 0)
        if not meta.get("height"):
            result["warning"] = "ffprobe unavailable — dimensions unknown"
        return result
    if not yt_dlp_available():
        result["warning"] = "yt-dlp not installed — source quality unknown"
        return result
    try:
        proc = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--no-download", "--no-playlist",
             "--socket-timeout", "20", "--retries", "1", "--", url],
            capture_output=True, timeout=timeout,
        )
        info = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except (subprocess.SubprocessError, ValueError) as exc:
        logger.info(f"[clips] quality probe failed ({type(exc).__name__}); continuing blind")
        result["warning"] = "probe failed — continuing blind"
        return result
    heights = [f.get("height") or 0 for f in (info.get("formats") or [])
               if f.get("vcodec", "none") != "none"]
    result.update(mode="remote", probe="ok",
                  max_height=max(heights, default=0),
                  duration=int(info.get("duration") or 0))
    if result["max_height"] and result["max_height"] < 720:
        result["warning"] = (
            f"low source resolution ({result['max_height']}p) — vertical crop will be soft"
        )
    return result


def _candidate_windows(segments: list[dict], min_s: float = 15.0, max_s: float = 60.0) -> list[dict]:
    """Merge consecutive transcript segments into 15–60s standalone windows."""
    out: list[dict] = []
    cur: dict | None = None
    for s in segments:
        if cur is None:
            cur = {"start": s["start"], "end": s["end"], "text": s["text"]}
            continue
        if cur["end"] - cur["start"] + (s["end"] - cur["end"]) <= max_s and not cur["text"].rstrip().endswith((".", "!", "?")):
            cur["end"] = s["end"]
            cur["text"] = f"{cur['text']} {s['text']}"
        else:
            if cur["end"] - cur["start"] >= 5:
                out.append(cur)
            cur = {"start": s["start"], "end": s["end"], "text": s["text"]}
    if cur and cur["end"] - cur["start"] >= 5:
        out.append(cur)
    # split overlong windows at sentence boundaries
    final: list[dict] = []
    for w in out:
        if w["end"] - w["start"] <= max_s or "." not in w["text"]:
            final.append(w)
            continue
        parts = [p.strip() for p in w["text"].split(". ") if p.strip()]
        span = (w["end"] - w["start"]) / max(1, len(parts))
        for j, p in enumerate(parts):
            final.append({"start": w["start"] + j * span, "end": min(w["start"] + (j + 1) * span, w["end"]), "text": p})
    return [w for w in final if w["end"] - w["start"] >= min(min_s, 5)]


_HOOK_WORDS = ("?", "secret", "truth", "nobody", "stop", "never", "mistake", "free",
               "how", "why", "what if", "breaking", "urgent", "shocking", "exactly")


def _hook_hits(text: str) -> int:
    """Whole-word matching (so 'show' never counts as 'how')."""
    import re as _re

    low = text.lower()
    hits = 0
    if "?" in low:
        hits += 1
    if "what if" in low:
        hits += 1
    words = set(_re.findall(r"[a-z0-9]+", low))
    for k in _HOOK_WORDS:
        if k in ("?", "what if"):
            continue
        if k in words:
            hits += 1
    return hits


def _heuristic_rank(windows: list[dict]) -> list[ViralMoment]:
    """Deterministic offline ranking: hook density + numbers + emotion + payoff."""
    out: list[ViralMoment] = []
    for w in windows:
        t = w["text"]
        low = t.lower()
        words = t.split()
        hook_hits = _hook_hits(t)
        numbers = sum(1 for tok in words if any(c.isdigit() for c in tok))
        hook = " ".join(words[:14])
        if len(hook) > 120:
            hook = hook[:117] + "..."
        score = 42.0 + min(28.0, hook_hits * 7.0) + min(16.0, numbers * 4.0)
        score += min(8.0, len(words) / 12.0)
        if low.rstrip().endswith(("?", "!")):
            score += 6.0
        out.append(ViralMoment(
            start=w["start"], end=w["end"], score=round(min(100.0, score), 1),
            hook=hook, reason=f"{hook_hits} hook marker(s), {numbers} number(s), {len(words)} words",
            text=t,
        ))
    out.sort(key=lambda m: (-m.score, m.start))
    return out


def moment_count_target(requested: int, default_max: int = 5) -> int:
    """Effective moment-selection count: sane clamps, env-overridable for A/B.

    Adapted from openshorts' CLIP_TARGET_MIN/MAX discipline: experiments run
    without a deploy (LINKMINER_MIN/MAX_MOMENTS), and bad input degrades
    instead of breaking the job (clamped 1..12).
    """
    import os as _os

    if requested is None:
        target = default_max
    else:
        try:
            target = int(requested)
        except (TypeError, ValueError):
            target = default_max
    raw_max = _os.environ.get("LINKMINER_MAX_MOMENTS")
    if raw_max:
        try:
            target = int(raw_max)
        except ValueError:
            pass
    raw_min = _os.environ.get("LINKMINER_MIN_MOMENTS")
    if raw_min:
        try:
            target = max(target, int(raw_min))
        except ValueError:
            pass
    return max(1, min(12, target))


def trim_to_best(moments: list[ViralMoment], max_moments: int) -> list[ViralMoment]:
    """Top-N by score with a deterministic start-time tie-break.

    Score-descending return (best first) is this pipeline's contract —
    callers number clips from this order. Ties resolve to the earliest
    window regardless of input order (sort stability alone only guarantees
    that for pre-sorted input).
    """
    ranked = sorted(moments, key=lambda m: (-m.score, m.start))
    return ranked[:max(1, int(max_moments or 1))]


def _srt_single(text: str, duration: float) -> str:
    """One caption cue spanning the clip (chunked into short lines)."""
    words = (text or "").split()
    if not words:
        return ""
    chunks = [" ".join(words[i:i + 7]) for i in range(0, len(words), 7)]
    weights = [max(len(c), 1) for c in chunks]
    total = sum(weights)
    lines: list[str] = []
    t = 0.0
    for i, (c, wgt) in enumerate(zip(chunks, weights), 1):
        d = duration * (wgt / total)
        lines.append(f"{i}\n{_srt_time(t)} --> {_srt_time(min(t + d, duration))}\n{c.strip()}\n")
        t += d
    return "\n".join(lines)


def _srt_time(seconds: float) -> str:
    ms = max(0, int(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _escape_filter_path(p: Path) -> str:
    s = str(p.resolve()).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


def get_repurposer() -> ClipRepurposer:
    return ClipRepurposer()


__all__ = [
    "CAPTION_PRESETS",
    "ClipError",
    "ClipRepurposer",
    "ClipResult",
    "SourceInfo",
    "TranscriptSegment",
    "ViralMoment",
    "caption_style",
    "face_track_available",
    "ffmpeg_available",
    "get_repurposer",
    "scenedetect_available",
    "whisper_available",
    "yt_dlp_available",
]
