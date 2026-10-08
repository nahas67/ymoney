"""Content storage boundary.

Generated videos move: engine temp output -> YMONEY storage -> permanent asset.
Local filesystem provider today; S3-compatible provider can be added behind the
same interface without touching agents or engines.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from loguru import logger

#: A Windows drive component -- ``C:`` -- anywhere in a stored path.
#:
#: It is refused on every platform. On Windows such a path is absolute, so
#: resolving it escapes the workspace; on POSIX the very same string is an
#: ordinary relative directory, so a guard written against the host's own
#: ``pathlib`` accepts it there. The same stored row would then be readable on
#: one platform and refused on the other, which is a portability bug with a
#: security shape. A drive component is never a legitimate workspace-relative
#: name, so the check is unconditional rather than platform-dependent.
_DRIVE_COMPONENT = re.compile(r"^[A-Za-z]:$")


def has_drive_component(value: str, *, allow_leading: bool = False) -> bool:
    """True when a path component of ``value`` is a Windows drive letter.

    Both separators are honoured: a stored row may carry ``C:\\x`` or ``C:/x``,
    and normalising only one of them would leave the other as an ordinary
    directory name.

    ``allow_leading`` exists for callers that legitimately accept an ABSOLUTE
    path, where ``C:/data/file`` is the ordinary Windows spelling of a real file
    and the containment check is what must judge it. Those callers still refuse
    an *embedded* drive (``masks/C:/x``), which no honest absolute path contains.
    Callers that require a workspace-RELATIVE key leave it False: there, a drive
    anywhere -- including first -- means the value is not relative at all.
    """
    parts = PurePosixPath(str(value or "").replace("\\", "/")).parts
    candidates = parts[1:] if allow_leading and parts else parts
    return any(_DRIVE_COMPONENT.match(part) for part in candidates)


def _resolve_storage_root() -> Path:
    """Root for local media. Overridable with ``STORAGE_ROOT``.

    The default stays **cwd-relative**, exactly as it has always been, because
    that is what test isolation relies on: a test that chdirs into ``tmp_path``
    expects ``data/videos`` to follow it there. Anchoring the default to the
    package directory instead sent every write to ``backend/data/videos`` while
    the test read ``<tmp>/data/videos``, which broke 16 export/cover/reframe
    tests at once.

    The container problem is real but it is a *configuration* problem, and it
    is fixed as configuration: ``docker-compose.prod.yml`` mounts a durable
    volume at ``/data`` and sets ``STORAGE_ROOT: /data/videos``. An operator who
    does not set it in a container gets cwd-relative storage inside the image
    layer, which is the behaviour that has always existed -- now visible and
    documented instead of silent.
    """
    from app.core.config import settings

    configured = (settings.storage_root or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("data/videos")


STORAGE_ROOT = _resolve_storage_root()
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
    if has_drive_component(stored_path, allow_leading=True):
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


def validate_storage_key(workspace_id: str, key: str):
    """Validate a workspace-relative media key for MediaAsset registration.

    Returns the normalized relative key, or None when the key is absolute,
    escapes the workspace directory, or is empty. No filesystem access —
    registration records references; bytes stay managed by storage providers.
    """
    from pathlib import PurePosixPath

    if not workspace_id or not (key or "").strip():
        return None
    if has_drive_component(key):
        return None
    normalized = PurePosixPath((key or "").replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        return None
    text = normalized.as_posix().lstrip("/")
    if not text or text in (".",):
        return None
    workspace_root = (STORAGE_ROOT / workspace_id).resolve()
    storage_root = STORAGE_ROOT.resolve()
    if not _inside(storage_root, workspace_root):
        return None
    candidate = (workspace_root / text).resolve()
    return text if _inside(workspace_root, candidate) else None


def mock_render_spec_path(reference: str) -> Path | None:
    """Resolve a mock artifact reference without permitting path traversal."""
    if not reference.startswith("mock:"):
        return None
    tail = reference.split(":", 1)[1]
    if has_drive_component(tail):
        return None
    relative = Path(tail)
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

    def open_stream(self, stored_path: str, *, chunk_bytes: int | None = None):
        """Bounded-memory read. ``read_bytes`` on a large asset is an OOM."""
        from app.services import streaming_io

        return streaming_io.iter_chunks(stored_path, chunk_bytes=chunk_bytes)

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

    def extract_covers(self, stored_path: str, timestamps: list[float]) -> list[dict]:
        """Extract N candidate cover frames (side-by-side compare set).

        Returns [{index, path, at_seconds}] for frames that rendered; missing
        ffmpeg or unreadable sources yield [] — never an exception.
        """
        src = Path(stored_path)
        if not src.exists() or not shutil.which("ffmpeg"):
            return []
        out: list[dict] = []
        for i, at in enumerate(timestamps):
            dest = src.parent / f"{src.stem}.cover-{i}.jpg"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "quiet", "-ss", str(max(0.0, at)), "-i", str(src),
                     "-frames:v", "1", "-q:v", "3", str(dest)],
                    check=True,
                    timeout=60,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                logger.warning(f"cover {i} extraction failed for {src.name}: {exc}")
                continue
            if dest.exists():
                out.append({"index": i, "path": str(dest), "at_seconds": round(max(0.0, at), 2)})
        return out

    @staticmethod
    def cover_path_for(stored_path: str, index: int) -> Path:
        src = Path(stored_path)
        return src.parent / f"{src.stem}.cover-{int(index)}.jpg"


def get_storage() -> LocalStorage:
    """Storage factory. S3-compatible providers plug in here later."""
    from app.core.config import settings as _settings

    if (_settings.storage_backend or "local").lower() == "s3":
        return S3Storage()
    return LocalStorage()


class S3Storage:
    """S3-compatible object storage (S3/MinIO/R2). Fails closed when unconfigured.

    Local files are still used as a staging area; objects are uploaded with a
    workspace-prefixed key and the public URL (or s3:// URI) is stored.
    """

    def __init__(self):
        from app.core.config import settings as _settings

        self.bucket = _settings.s3_bucket
        self.endpoint = _settings.s3_endpoint_url
        self.public_base = (_settings.s3_public_base_url or "").rstrip("/")
        if not self.bucket:
            raise RuntimeError("S3 storage selected but S3_BUCKET is not configured")

    def _client(self):
        import boto3

        from app.core.config import settings as _settings

        kwargs: dict = {"region_name": _settings.s3_region or "us-east-1"}
        if _settings.s3_endpoint_url:
            kwargs["endpoint_url"] = _settings.s3_endpoint_url
        if _settings.s3_access_key:
            kwargs["aws_access_key_id"] = _settings.s3_access_key
            kwargs["aws_secret_access_key"] = _settings.s3_secret_key
        return boto3.client("s3", **kwargs)

    def save_video(self, workspace_id: str, source_path: str | None, data: bytes | None = None,
                   filename: str | None = None) -> StoredVideo:
        import uuid as _uuid

        name = filename or (Path(source_path).name if source_path else "video.mp4")
        key = f"{workspace_id}/{_uuid.uuid4().hex[:8]}-{Path(name).name}"
        body = data if data is not None else Path(source_path).read_bytes()
        self._client().put_object(Bucket=self.bucket, Key=key, Body=body,
                                  ContentType="video/mp4")
        url = f"{self.public_base}/{key}" if self.public_base else f"s3://{self.bucket}/{key}"
        return StoredVideo(path=url, size_bytes=len(body), duration_seconds=None,
                           width=None, height=None)

    def open_bytes(self, stored_path: str) -> bytes:
        if stored_path.startswith("s3://"):
            _, _, rest = stored_path[5:].partition("/")
            bucket, _, key = rest.partition("/")
            obj = self._client().get_object(Bucket=bucket or self.bucket, Key=key)
            return obj["Body"].read()
        return Path(stored_path).read_bytes()

    def open_stream(self, stored_path: str, *, chunk_bytes: int | None = None):
        """Streaming GET.

        ``get_object`` on a 3 GB asset returns a ``StreamingBody``; calling
        ``.read()`` on it without a size is the whole-file buffer this replaces.
        The multipart threshold is a request, not a policy: S3 streams a single
        ranged GET for anything below it, so nothing is copied through a scratch
        file unless the object is genuinely larger than the threshold.
        """
        if stored_path.startswith("s3://"):
            _, _, rest = stored_path[5:].partition("/")
            bucket, _, key = rest.partition("/")
            obj = self._client().get_object(Bucket=bucket or self.bucket, Key=key)
            body = obj["Body"]
            size = max(64 * 1024, int(chunk_bytes or 8 << 20))
            try:
                while True:
                    block = body.read(size)
                    if not block:
                        return
                    yield block
            finally:
                close = getattr(body, "close", None)
                if close is not None:
                    close()
        from app.services import streaming_io

        yield from streaming_io.iter_chunks(stored_path, chunk_bytes=chunk_bytes)

    def presigned_url(self, key: str, *, expires_seconds: float = 3600.0) -> str:
        """Time-limited URL for one object. None when the backend cannot sign."""
        try:
            return str(self._client().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=int(expires_seconds),
            ))
        except Exception as exc:  # noqa: BLE001 - signing is best-effort
            logger.warning(f"presigned url unavailable for {key}: {exc}")
            return ""

    def save_media(self, workspace_id: str, data: bytes, filename: str) -> str:
        import uuid as _uuid

        key = f"{workspace_id}/{_uuid.uuid4().hex[:8]}-{Path(filename).name}"
        self._client().put_object(Bucket=self.bucket, Key=key, Body=data)
        return f"{self.public_base}/{key}" if self.public_base else f"s3://{self.bucket}/{key}"

    def extract_thumbnail(self, stored_path: str, at_seconds: float = 1.0) -> str | None:
        return None

    def extract_covers(self, stored_path: str, timestamps: list[float]) -> list[dict]:
        return []
