"""Content storage boundary.

Generated videos move: engine temp output -> YMONEY storage -> permanent asset.
Local filesystem provider today; S3-compatible provider can be added behind the
same interface without touching agents or engines.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

STORAGE_ROOT = Path("data/videos")
MOCK_ROOT = Path("data/mock_videos")


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def managed_path(workspace_id: str, stored_path: str) -> Path | None:
    """Resolve a stored media path only when it belongs to this workspace.

    Database values are treated as untrusted because older rows or an operator
    import could contain arbitrary paths. Returning None makes API callers fail
    closed instead of serving files outside the managed workspace directory.
    """
    if not workspace_id or not stored_path or stored_path.startswith("mock:"):
        return None
    storage_root = STORAGE_ROOT.resolve()
    workspace_root = (STORAGE_ROOT / workspace_id).resolve()
    if not _inside(storage_root, workspace_root):
        return None
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    return candidate if _inside(workspace_root, candidate) else None


def mock_render_spec_path(reference: str) -> Path | None:
    """Resolve a mock artifact reference without permitting path traversal."""
    if not reference.startswith("mock:"):
        return None
    relative = Path(reference.split(":", 1)[1])
    root = MOCK_ROOT.resolve()
    if relative.is_absolute() or ".." in relative.parts or relative.name != "final-1.mp4":
        return None
    candidate = (root / relative.parent / "render_spec.json").resolve()
    return candidate if _inside(root, candidate) else None


@dataclass
class StoredVideo:
    path: str
    size_bytes: int | None
    duration_seconds: float | None
    width: int | None
    height: int | None


def probe_metadata(path: Path) -> dict:
    """ffprobe-based metadata; returns {} when ffprobe is unavailable/fails."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format", {})
        width = height = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(stream.get("width") or 0) or None
                height = int(stream.get("height") or 0) or None
                break
        return {
            "duration_seconds": float(fmt.get("duration")) if fmt.get("duration") else None,
            "width": width,
            "height": height,
            "format": fmt.get("format_name"),
        }
    except Exception as exc:  # noqa: BLE001 — metadata is best-effort
        logger.warning(f"ffprobe metadata unavailable for {path.name}: {exc}")
        return {}


class LocalStorage:
    """Local development storage under data/videos/{workspace_id}/."""

    def save_video(self, workspace_id: str, source_path: str | None, data: bytes | None = None,
                   filename: str | None = None) -> StoredVideo:
        dest_dir = STORAGE_ROOT / workspace_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = filename or (Path(source_path).name if source_path else "video.mp4")
        dest = dest_dir / name
        if data is not None:
            dest.write_bytes(data)
        elif source_path and Path(source_path).exists():
            shutil.copyfile(source_path, dest)
        else:
            raise FileNotFoundError(f"nothing to store (source={source_path})")
        meta = probe_metadata(dest)
        logger.info(f"stored video asset: {dest} ({dest.stat().st_size} bytes)")
        return StoredVideo(
            path=str(dest),
            size_bytes=dest.stat().st_size,
            duration_seconds=meta.get("duration_seconds"),
            width=meta.get("width"),
            height=meta.get("height"),
        )

    def open_bytes(self, stored_path: str) -> bytes:
        return Path(stored_path).read_bytes()

    def save_media(self, workspace_id: str, data: bytes, filename: str) -> str:
        """Store a generic media blob (e.g. generated images) in the workspace
        directory. Returns the stored path; boundary-checked like videos."""
        dest_dir = STORAGE_ROOT / workspace_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(filename).name
        dest.write_bytes(data)
        logger.info(f"stored media asset: {dest} ({dest.stat().st_size} bytes)")
        return str(dest)

    def extract_thumbnail(self, stored_path: str, at_seconds: float = 1.0) -> str | None:
        """Extract a poster frame next to the video via ffmpeg.

        Returns the thumbnail path, or None when ffmpeg is unavailable —
        callers must treat thumbnails as optional polish, never required.
        """
        src = Path(stored_path)
        if not src.exists() or not shutil.which("ffmpeg"):
            return None
        out = src.with_suffix(".jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "quiet", "-ss", str(at_seconds), "-i", str(src),
                 "-frames:v", "1", "-q:v", "3", str(out)],
                check=True,
                timeout=60,
            )
            return str(out) if out.exists() else None
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning(f"thumbnail extraction failed for {src.name}: {exc}")
            return None


def get_storage() -> LocalStorage:
    """Storage factory. S3-compatible providers plug in here later."""
    return LocalStorage()
