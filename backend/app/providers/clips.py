"""Clip repurposing — turn long-form video into short-form segments.

Original YMONEY implementation using yt-dlp (acquisition of a user-provided,
permitted URL) and FFmpeg (cutting). The technique is provider-independent:
any downloader/processor can be swapped in behind ClipRepurposer later.

Safety/rules of the road:
  - only URLs the operator supplies are processed (no discovery of third-
    party content, no terms evasion);
  - yt-dlp is an OPTIONAL dependency — when missing, the tool reports
    `unavailable` instead of pretending;
  - outputs are stored inside the workspace storage boundary only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from app.services.storage import STORAGE_ROOT


class ClipError(Exception):
    pass


def yt_dlp_available() -> bool:
    return bool(shutil.which("yt-dlp"))


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


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


@dataclass
class SourceInfo:
    title: str
    duration: float | None
    width: int | None
    height: int | None
    local_path: Path


class ClipRepurposer:
    """Download (optional) + cut a source video into short segments."""

    def __init__(self, work_root: Path | None = None):
        self.work_root = work_root or (STORAGE_ROOT / "_clipwork")

    # -- availability -------------------------------------------------------

    def status(self) -> dict:
        return {
            "yt_dlp": yt_dlp_available(),
            "ffmpeg": ffmpeg_available(),
            "download_supported": yt_dlp_available(),
            "cut_supported": ffmpeg_available(),
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
            title=meta.get("title", local.stem),
            duration=meta.get("duration"),
            width=meta.get("width"),
            height=meta.get("height"),
            local_path=local,
        )

    # -- cutting ---------------------------------------------------------------

    def cut_segments(self, source: SourceInfo, workspace_id: str, *,
                     segments: list[tuple[float, float]] | None = None,
                     clip_seconds: float = 45.0,
                     max_clips: int = 5,
                     vertical: bool = True) -> list[ClipResult]:
        """Cut highlight segments. If none given, divides evenly across the
        source duration (deterministic; smart selection is a future skill)."""
        if not ffmpeg_available():
            raise ClipError("ffmpeg/ffprobe not available — install ffmpeg to enable cutting")
        duration = source.duration or 0.0
        if duration <= 0:
            raise ClipError("source duration unknown; cannot plan segments")

        if not segments:
            segments = self._even_segments(duration, clip_seconds, max_clips)

        out_dir = STORAGE_ROOT / workspace_id
        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[ClipResult] = []
        for i, (start, end) in enumerate(segments, start=1):
            end = min(end, duration)
            if end - start < 3:
                continue
            name = f"clip-{source.local_path.stem[:40]}-{i:02d}.mp4"
            dest = out_dir / name
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}",
                   "-i", str(source.local_path), "-t", f"{end - start:.2f}"]
            if vertical:
                cmd += ["-vf",
                        "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", str(dest)]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=900)
            except subprocess.TimeoutExpired as exc:
                logger.warning(f"clip {i} cut timed out")
                continue
            if proc.returncode != 0 or not dest.exists():
                stderr = (proc.stderr or b"").decode(errors="replace")[:200]
                logger.warning(f"clip {i} failed: {stderr}")
                continue
            meta = self._probe(dest)
            results.append(ClipResult(
                path=str(dest),
                start=round(start, 2),
                end=round(end, 2),
                duration=round(end - start, 2),
                width=meta.get("width"),
                height=meta.get("height"),
                source_url=source.title and "" or "",
                title=source.title,
            ))
        return results

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


def get_repurposer() -> ClipRepurposer:
    return ClipRepurposer()


__all__ = [
    "ClipError",
    "ClipRepurposer",
    "ClipResult",
    "SourceInfo",
    "ffmpeg_available",
    "get_repurposer",
    "yt_dlp_available",
]
