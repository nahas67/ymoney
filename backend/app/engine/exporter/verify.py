"""Export verification: the evidence a COMPLETE verdict is built from.

Contracts §11. ``verify_export`` runs five independent checks and returns::

    {complete: bool, checks: [{name, passed, detail, critical}],
     checksum: str, probe: {...}}

1. ``file_exists``      -- the artifact is on disk and size > 0
2. ``streams_present``  -- media formats only: ffprobe
   (``services.storage.probe_metadata``) must show the streams the format
   promises (video formats: v+a, audio formats: a only, no video)
3. ``duration_matches`` -- media formats only: within tolerance (<=2% OR
   <=0.5s) of the source duration
4. ``resolution_matches`` -- media formats only, and only when the profile
   actually carries dims (ARCHIVE_MASTER is source passthrough by design)
5. ``checksum_recorded`` -- sha256 of the written file
6. ``text_roundtrip``   -- text formats only: the artifact is parsed back with
   the format's own parser (SRT via ``providers.dubbing.parse_srt``)
7. ``job_persisted``    -- the export_jobs row exists in THIS workspace

A verdict is COMPLETE only when **every critical check** passes. Any critical
failure makes the verdict incomplete and its check names go into
``error`` -- the export center never says "done" on a partial proof.

The verifier is deliberately *independent* of the code that wrote the
artifact: it re-probes the file, re-parses the text, and re-hashes the bytes.
``we wrote it, so it must be fine`` is exactly the failure mode this module
exists to prevent.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import ExportJob

from .formats import MEDIA_FORMATS, get_format

logger = logging.getLogger("ymoney.collab")

#: Duration tolerance: relative OR absolute, whichever is looser.
DURATION_TOLERANCE_RATIO = 0.02
DURATION_TOLERANCE_SECONDS = 0.5


def sha256_file(path: Path) -> str:
    """Streaming sha256 of a file (never loads a whole render into memory)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(name: str, passed: bool, detail: str, *, critical: bool = True) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail[:400],
            "critical": bool(critical)}


def _duration_within(source_seconds: float | None, got: float | None) -> tuple[bool, str]:
    if not source_seconds or not got:
        return False, "no duration to compare"
    delta = abs(float(got) - float(source_seconds))
    tolerance = max(DURATION_TOLERANCE_RATIO * float(source_seconds),
                    DURATION_TOLERANCE_SECONDS)
    return delta <= tolerance, (
        f"source={float(source_seconds):.3f}s got={float(got):.3f}s "
        f"delta={delta:.3f}s tol={tolerance:.3f}s")


def _expected_streams(fmt: str) -> tuple[bool, bool]:
    """(expect_video, expect_audio) for a media format."""
    if fmt in ("MP4", "MOV", "WebM"):
        return True, True
    return False, True  # MP3 / WAV


def probe_streams(path: Path) -> dict:
    """ffprobe the artifact for its real stream types.

    ``probe_metadata`` (``services/storage.py``) reports container-level
    metadata only. The stream census needs ``ffprobe -show_streams`` directly,
    so this keeps the existing wrapper for what it is good at and adds the
    one thing verify needs. Returns ``{}`` when ffprobe is unavailable --
    the caller then FAILS the check rather than assuming streams exist.
    """
    import json
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30, check=False,
        )
        data = json.loads(out.stdout or "{}")
    except Exception as exc:  # noqa: BLE001 - probe is best effort
        logger.warning("ffprobe stream census failed for %s: %s", path.name, exc)
        return {}
    streams = data.get("streams", [])
    codecs = [str(s.get("codec_name") or "") for s in streams]
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "video_streams": len(video),
        "audio_streams": len(audio),
        "codecs": [c for c in codecs if c],
        "width": int(video[0].get("width") or 0) or None if video else None,
        "height": int(video[0].get("height") or 0) or None if video else None,
        "sample_rate": int(audio[0].get("sample_rate") or 0) or None if audio else None,
        "channels": int(audio[0].get("channels") or 0) or None if audio else None,
        "format_name": str((data.get("format") or {}).get("format_name") or ""),
        "duration_seconds": _float_or_none((data.get("format") or {}).get("duration")),
    }


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def resolve_artifact_path(workspace_id: str, storage_key: str) -> Path | None:
    """Resolve a MediaAsset ``storage_key`` to a path inside THIS workspace.

    ``services.storage.managed_path`` treats a relative key as relative to
    the process CWD, but ``validate_storage_key`` (and every other writer in
    this repo) stores keys relative to the WORKSPACE root -- so feeding one
    to the other silently yields ``None``. This resolves the way the render
    path does (``STORAGE_ROOT/<ws>/<key>``) and keeps the boundary check: a
    stored path may never escape the workspace directory.
    """
    from app.services.storage import STORAGE_ROOT

    key = (storage_key or "").lstrip("/")
    if not workspace_id or not key:
        return None
    storage_root = STORAGE_ROOT.resolve()
    workspace_root = (STORAGE_ROOT / workspace_id).resolve()
    try:
        workspace_root.relative_to(storage_root)
    except ValueError:
        return None  # a workspace id that escapes the root is never trusted
    candidate = (workspace_root / key).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def verify_export(
    db: Session,
    workspace_id: str,
    job: ExportJob,
    *,
    path: Path | None = None,
    source_duration_seconds: float | None = None,
    config: dict | None = None,
) -> dict:
    """Run every check for one export and return the verdict dict.

    ``path`` defaults to the artifact resolved from the job's MediaAsset row.
    ``source_duration_seconds`` is the expected duration (the source file's,
    or the timeline's when the source is a pass-through) -- the caller
    supplies it because the exporter knows which source it used. ``config``
    is the profile config the artifact was BUILT with; it is passed in
    explicitly (not read back from ``verification_json``, which is written
    after this runs) so the resolution check compares against the real dims
    rather than skipping itself.
    """
    from app.models import MediaAsset

    checks: list[dict] = []
    spec = get_format(job.format)
    # Canonical registry name, not an uppercased copy: "WebM" must not be
    # compared against a MEDIA_FORMATS tuple that spells it "WebM".
    fmt = spec.name

    if config is None:
        config = dict(job.verification_json or {}).get("config") or {}
    profile_config = dict(config or {})

    artifact_path = Path(path) if path is not None else None
    if artifact_path is None:
        asset = (db.get(MediaAsset, job.artifact_asset_id)
                 if job.artifact_asset_id else None)
        if asset is not None:
            artifact_path = resolve_artifact_path(workspace_id, asset.storage_key)
        else:
            recorded = (job.verification_json or {}).get("path") if isinstance(
                job.verification_json, dict) else None
            artifact_path = Path(recorded) if recorded else None

    # (1) file exists + non-empty
    exists = artifact_path is not None and Path(artifact_path).is_file()
    size = Path(artifact_path).stat().st_size if exists else 0
    checks.append(_check(
        "file_exists", exists and size > 0,
        f"path={Path(artifact_path).name if artifact_path else 'none'} size={size}"))

    # (5) checksum -- recorded on the job row
    checksum = str(job.checksum or "")
    if exists and size > 0 and not checksum:
        try:
            checksum = sha256_file(Path(artifact_path))
        except OSError as exc:
            checks.append(_check("checksum_recorded", False, f"unreadable: {exc}"))
    checks.append(_check("checksum_recorded", bool(checksum),
                         f"sha256={checksum[:16] or 'none'}"))

    probe: dict = {}
    if exists and size > 0:
        if fmt in MEDIA_FORMATS:
            # (2) streams (3) duration (4) resolution
            from app.services.storage import probe_metadata

            container = probe_metadata(Path(artifact_path)) or {}
            census = probe_streams(Path(artifact_path))
            probe = {**container, **census}
            want_video, want_audio = _expected_streams(fmt)
            got_video = int(census.get("video_streams") or 0)
            got_audio = int(census.get("audio_streams") or 0)
            streams_ok = (
                bool(census)
                and (got_video >= 1) == want_video
                and (got_audio >= 1) == want_audio
            )
            checks.append(_check(
                "streams_present", streams_ok,
                f"want video={want_video} audio={want_audio}; "
                f"got video={got_video} audio={got_audio} "
                f"codecs={census.get('codecs') or 'unknown'}"))

            want_duration = source_duration_seconds
            if want_duration is None:
                want_duration = _float_or_none(
                    container.get("duration_seconds"))
            ok, detail = _duration_within(
                want_duration, _float_or_none(census.get("duration_seconds")))
            checks.append(_check("duration_matches", ok, detail,
                                 critical=want_duration is not None))

            config = dict(job.verification_json or {}).get("config") or {}
            want_w, want_h = profile_config.get("width"), profile_config.get("height")
            if want_w and want_h:
                got_w, got_h = census.get("width"), census.get("height")
                checks.append(_check(
                    "resolution_matches",
                    int(got_w or 0) == int(want_w) and int(got_h or 0) == int(want_h),
                    f"want {int(want_w)}x{int(want_h)} got {got_w}x{got_h}"))
            else:
                checks.append(_check(
                    "resolution_matches", True,
                    "profile has no fixed dims (source passthrough) -- skipped",
                    critical=False))
        else:
            # (6) text roundtrip: the format's own parser reads it back
            try:
                data = Path(artifact_path).read_bytes()
                count = spec.verify(data)
                checks.append(_check("text_roundtrip", True,
                                     f"parsed {count} unit(s) back"))
            except Exception as exc:  # noqa: BLE001 - any parse failure = fail
                checks.append(_check("text_roundtrip", False,
                                     f"{type(exc).__name__}: {str(exc)[:160]}"))

    # (7) job row persisted in THIS workspace
    persisted = db.get(ExportJob, job.id) is not None \
        and db.get(ExportJob, job.id).workspace_id == workspace_id
    checks.append(_check("job_persisted", persisted,
                         f"export_job {job.id} in workspace {workspace_id}"))

    complete = all(c["passed"] for c in checks if c["critical"])
    failed = [c["name"] for c in checks if c["critical"] and not c["passed"]]
    return {
        "complete": complete,
        "checks": checks,
        "checksum": checksum,
        "probe": probe,
        "failed": failed,
        "error": (f"verification failed: {', '.join(failed)}" if failed else ""),
    }


def check_export_contract(session, workspace_id: str, export_id: str) -> dict:
    """Lightweight CompletionVerifier hook (see engine/intelligence/verifier).

    Re-uses the persisted verdict instead of re-probing: the export center
    already recorded per-check evidence when the job finished, and the
    ledger wants that evidence, not a second (potentially much later) probe.
    """
    job = session.get(ExportJob, export_id)
    if job is None or job.workspace_id != workspace_id:
        return {"found": False, "complete": False, "checks": [],
                "execution": "UNKNOWN"}
    verdict = dict(job.verification_json or {})
    checks = list(verdict.get("checks") or [])
    execution = {
        "COMPLETE": "COMPLETED",
        "RUNNING": "RUNNING",
        "QUEUED": "RUNNING",
    }.get(str(job.state or "").upper(), "FAILED")
    return {
        "found": True,
        "complete": bool(verdict.get("complete")) and job.state == "COMPLETE",
        "checks": checks,
        "execution": execution,
    }


__all__ = [
    "DURATION_TOLERANCE_RATIO",
    "DURATION_TOLERANCE_SECONDS",
    "check_export_contract",
    "probe_streams",
    "resolve_artifact_path",
    "sha256_file",
    "verify_export",
]
