"""Export job executor: build the artifact, verify it, persist the proof.

Contracts §11. One entry point per operation:

* :func:`enqueue_export` -- create the ``export_jobs`` row and queue an
  ``EXPORT_BUILD`` job with an idempotency key. The SAME row is re-queued by
  :func:`retry_export` (attempt+1, fresh job id) and stopped by
  :func:`cancel_export` (``jobs.cancel_job`` + state CANCELLED).
* :func:`run_export` -- the ``EXPORT_BUILD`` handler. Loads the target, asks
  the format registry for its artifact, verifies the artifact independently,
  then persists a ``MediaAsset`` (origin ``export``) and the verdict.
* :func:`register_export_jobs` -- guarded, idempotent handler registration
  (mirrors ``engine/sources/sync.py::register_source_jobs``).

Artifacts always land as real files under ``STORAGE_ROOT/<ws>/exports/`` with
a ``MediaAsset`` row -- never an inline blob, never a path outside the
workspace (``services.storage.validate_storage_key`` is the gate).

State machine (all transitions are explicit; nothing is inferred):

    QUEUED -> RUNNING -> COMPLETE
                      -> FAILED
            -> CANCELLED

A COMPLETE row is only ever written when ``verify_export`` returned
``complete=True``; a partial proof is FAILED with the failed check names in
``error``. That is the whole point of the module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import ContentTimeline, ExportJob, ExportProfile, MediaAsset
from app.models.base import utcnow
from app.services import jobs as jobs_service

from .formats import (
    FORMAT_ASSET_TYPE,
    FORMAT_SUFFIX,
    ExportContext,
    ExportValidationError,
    get_format,
    require_available,
)
from .profiles import check_profile_format, load_profile, watermark_text
from .verify import sha256_file, verify_export

logger = logging.getLogger("ymoney.collab")

JOB_TYPE = "EXPORT_BUILD"
MAX_ATTEMPTS = 5
#: States a retry may act on (contracts §11: FAILED/CANCELLED only).
RETRYABLE_STATES: tuple[str, ...] = ("FAILED", "CANCELLED")
#: States a cancel may act on (queued/running only).
CANCELLABLE_STATES: tuple[str, ...] = ("QUEUED", "RUNNING")

# jobs._Cancelled is private; resolve defensively like sources/sync.py does.
_CANCELLED_CLS = getattr(jobs_service, "_Cancelled", None)


class ExportJobError(ValueError):
    """Domain error with a safe, short message. Routes map it to 4xx."""


def _is_cancellation(exc: BaseException) -> bool:
    return _CANCELLED_CLS is not None and isinstance(exc, _CANCELLED_CLS)


def _short(exc: BaseException, limit: int = 400) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


# ---------------------------------------------------------------------------
# guarded handler registration
# ---------------------------------------------------------------------------


def register_export_jobs() -> None:
    """Register EXPORT_BUILD — idempotent, safe to call on repeat/reload."""
    if JOB_TYPE in jobs_service._handlers:
        return
    jobs_service.register_handler(JOB_TYPE, handle_export_build)


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------


@dataclass
class ExportTarget:
    """The resolved export subject: a timeline doc plus its source media."""

    doc: dict
    workspace_id: str
    target_type: str
    target_id: str
    source_path: Path | None = None
    duration_seconds: float = 0.0
    name: str = "export"


def _doc_of_timeline(row: ContentTimeline) -> dict:
    """The same tracks_json shape every timeline GET serves (api/v1/timelines)."""
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    doc.setdefault("name", row.name)
    return doc


def _timeline_source(db: Session, workspace_id: str, doc: dict) -> tuple[Path | None, float]:
    """First resolvable media asset in a timeline, plus the source duration.

    Uses the render path's own workspace-scoped resolver
    (``providers.video_engine.timeline_render.resolve_clip_source``) so the
    export can never read a file the renderer would refuse to read.
    """
    from app.providers.video_engine.timeline_render import resolve_clip_source

    longest = 0.0
    for track in doc.get("tracks", []):
        for clip in track.get("clips", []):
            longest = max(longest, float(clip.get("duration", 0.0) or 0.0))
            if track.get("kind") not in ("video", "broll", "avatar", "voice",
                                         "music", "sfx", "text", "caption"):
                continue
            path = resolve_clip_source(workspace_id, db, clip.get("source") or {})
            if path is not None:
                return Path(path), float(doc.get("duration_seconds") or longest or 0.0)
    return None, float(doc.get("duration_seconds") or longest or 0.0)


def resolve_target(db: Session, workspace_id: str, target_type: str,
                   target_id: str) -> ExportTarget:
    """Load one workspace-scoped export target.

    A foreign or missing id raises :class:`ExportJobError` -- callers map it
    to 404, so another workspace's id is indistinguishable from a wrong one.
    """
    kind = str(target_type or "").strip()
    tid = str(target_id or "").strip()
    if kind == "timeline":
        row = db.get(ContentTimeline, tid) if tid else None
        if row is None or row.workspace_id != workspace_id:
            raise ExportJobError(f"target not found: timeline {tid or '<missing>'}")
        doc = _doc_of_timeline(row)
        source, duration = _timeline_source(db, workspace_id, doc)
        return ExportTarget(doc, workspace_id, kind, tid, source, duration,
                            name=str(row.name or "timeline"))
    if kind == "video":
        from app.models import Video

        row = db.get(Video, tid) if tid else None
        if row is None or row.workspace_id != workspace_id:
            raise ExportJobError(f"target not found: video {tid or '<missing>'}")
        from app.services.storage import managed_path

        path = managed_path(workspace_id, row.file_path or "")
        label = f"video_{tid[:8]}"
        return ExportTarget(
            {"name": label, "tracks": [],
             "duration_seconds": float(row.duration_seconds or 0.0),
             "fps": 30.0, "aspect_ratio": row.aspect_ratio or "9:16"},
            workspace_id, kind, tid,
            Path(path) if path and Path(path).exists() else None,
            float(row.duration_seconds or 0.0), name=label)
    if kind == "asset":
        row = db.get(MediaAsset, tid) if tid else None
        if row is None or row.workspace_id != workspace_id:
            raise ExportJobError(f"target not found: asset {tid or '<missing>'}")
        from app.services.storage import managed_path

        path = managed_path(workspace_id, row.storage_key or "")
        return ExportTarget(
            {"name": row.storage_key or "asset", "tracks": [],
             "duration_seconds": float(row.duration_seconds or 0.0),
             "fps": float(row.frame_rate or 30.0), "aspect_ratio": "16:9"},
            workspace_id, kind, tid,
            Path(path) if path and Path(path).exists() else None,
            float(row.duration_seconds or 0.0), name=str(row.storage_key or "asset"))
    raise ExportJobError(
        f"unsupported export target_type '{target_type}'; expected timeline|video|asset")


# ---------------------------------------------------------------------------
# enqueue / retry / cancel
# ---------------------------------------------------------------------------


def _artifact_name(target: ExportTarget, fmt: str) -> str:
    """Safe, collision-resistant artifact filename."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", target.name or "export").strip("_")
    return f"{(stem or 'export')[:60]}_{target.target_id[:8]}{FORMAT_SUFFIX.get(fmt, '')}"


def _exports_dir(workspace_id: str) -> Path:
    from app.services.storage import STORAGE_ROOT

    return STORAGE_ROOT / workspace_id / "exports"


def enqueue_export(
    db: Session,
    workspace_id: str,
    *,
    profile_id: str,
    fmt: str,
    target_type: str,
    target_id: str,
    user_id: str,
) -> dict:
    """Validate + create the export row, then queue EXPORT_BUILD.

    Validation order matters: profile first (unknown id -> 404), then the
    format probe (NOT_AVAILABLE -> 422 carrying the honest reason), then the
    profile/format compatibility (``MP4`` + ``CAPTIONS_ONLY`` -> 422). The row
    is only written once every gate passed, so a QUEUED export is always
    runnable.
    """
    ws = str(workspace_id or "")
    if not ws or not str(user_id or ""):
        raise ExportJobError("workspace and user are required")
    spec = require_available(fmt)  # 422 with the probe's reason when unavailable
    try:
        profile = load_profile(db, ws, str(profile_id or ""))
    except KeyError:
        raise ExportJobError(f"profile not found: {profile_id or '<missing>'}") from None
    config = dict(profile.config_json or {})
    try:
        check_profile_format(str(profile.preset or "CUSTOM"), config, spec.name)
    except ValueError as exc:
        raise ExportJobError(_short(exc, 200)) from None
    resolve_target(db, ws, target_type, target_id)  # 404 on foreign/missing

    row = ExportJob(
        workspace_id=ws,
        profile_id=profile.id,
        format=spec.name,
        target_type=str(target_type),
        target_id=str(target_id),
        state="QUEUED",
        progress=0,
        attempt=0,
        created_by=str(user_id),
    )
    db.add(row)
    db.flush()
    # COMMIT BEFORE enqueue: jobs.enqueue opens its OWN session, and SQLite
    # holds this session's write lock until commit -- enqueuing while the row
    # is still dirty deadlocks the caller (same ordering rule as
    # api/v1/projects.py: commit, then record_event).
    db.commit()
    job_id = _enqueue_for(row, ws)
    row.job_id = job_id
    db.commit()
    return {"export_id": row.id, "job_id": job_id, "queued": True,
            "state": row.state, "format": row.format}


def _enqueue_for(row: ExportJob, workspace_id: str) -> str | None:
    """Queue EXPORT_BUILD for a row with a per-attempt idempotency key.

    The key carries the row id AND the attempt so a retry is a genuinely new
    job while an accidental double-POST of the same request is deduped by
    ``jobs.enqueue`` rather than producing two parallel renders.
    """
    return jobs_service.enqueue(
        JOB_TYPE,
        {"export_id": row.id, "attempt": int(row.attempt or 0)},
        workspace_id=workspace_id,
        idempotency_key=f"export:{row.id}:{int(row.attempt or 0)}",
    )


def retry_export(db: Session, workspace_id: str, export_id: str) -> dict:
    """Re-queue a FAILED / CANCELLED export with attempt+1 and a fresh job id."""
    row = _load_job(db, workspace_id, export_id)
    if str(row.state or "").upper() not in RETRYABLE_STATES:
        raise ExportJobError(
            f"only {list(RETRYABLE_STATES)} exports can be retried; this one is {row.state}")
    if int(row.attempt or 0) >= MAX_ATTEMPTS:
        raise ExportJobError(
            f"export exhausted its {MAX_ATTEMPTS} attempts; start a new export instead")
    row.attempt = int(row.attempt or 0) + 1
    row.state = "QUEUED"
    row.progress = 0
    row.error = None
    row.started_at = None
    row.finished_at = None
    db.commit()  # release the write lock before jobs.enqueue opens its session
    job_id = _enqueue_for(row, workspace_id)
    row.job_id = job_id
    db.commit()
    return {"export_id": row.id, "job_id": job_id, "queued": True,
            "state": row.state, "attempt": int(row.attempt or 0)}


def cancel_export(db: Session, workspace_id: str, export_id: str) -> dict:
    """Stop a QUEUED / RUNNING export (jobs.cancel_job + state CANCELLED)."""
    row = _load_job(db, workspace_id, export_id)
    if str(row.state or "").upper() not in CANCELLABLE_STATES:
        raise ExportJobError(
            f"only {list(CANCELLABLE_STATES)} exports can be cancelled; "
            f"this one is {row.state}")
    if row.job_id:
        jobs_service.cancel_job(str(row.job_id))
    row.state = "CANCELLED"
    row.finished_at = utcnow()
    row.error = "cancelled by request"
    db.commit()
    return {"export_id": row.id, "state": row.state, "cancelled": True}


def _load_job(db: Session, workspace_id: str, export_id: str) -> ExportJob:
    row = db.get(ExportJob, str(export_id or ""))
    if row is None or row.workspace_id != workspace_id:
        raise ExportJobError("export not found")
    return row


def get_export(db: Session, workspace_id: str, export_id: str) -> ExportJob:
    return _load_job(db, workspace_id, export_id)


def list_exports(db: Session, workspace_id: str, *, state: str | None = None,
                 limit: int = 100) -> list[ExportJob]:
    q = select(ExportJob).where(ExportJob.workspace_id == workspace_id)
    if state:
        q = q.where(ExportJob.state == str(state).upper())
    return list(db.scalars(q.order_by(ExportJob.created_at.desc())
                          .limit(max(1, min(int(limit), 500)))).all())


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------


def handle_export_build(ctx: jobs_service.JobContext) -> dict:
    """``EXPORT_BUILD`` job handler. Runs the export inline in a session."""
    workspace_id = str(ctx.workspace_id or "")
    export_id = str((ctx.payload or {}).get("export_id") or "")
    if not workspace_id or not export_id:
        return {"skipped": "EXPORT_BUILD requires workspace_id and export_id"}
    with session_scope() as db:
        row = db.get(ExportJob, export_id)
        if row is None or row.workspace_id != workspace_id:
            return {"skipped": "export not found in this workspace"}
        if str(row.state or "") in ("COMPLETE", "CANCELLED"):
            return {"skipped": f"export already {row.state}"}
        try:
            return run_export(db, workspace_id, export_id, ctx=ctx)
        except Exception as exc:
            if not _is_cancellation(exc):
                _fail(db, row, _short(exc))
            raise


def run_export(
    db: Session,
    workspace_id: str,
    export_id: str,
    *,
    ctx: jobs_service.JobContext | None = None,
) -> dict:
    """Build, verify and persist one export. Returns the job dict.

    Two failure classes, handled differently on purpose:

    * a **failed verdict** (the artifact was written but a check did not
      pass) is RETURNED as ``{"state": "FAILED", "failed": [...]}`` -- it is
      a terminal, honest outcome and re-running it would only re-prove the
      same bytes, so the queue must not burn retries on it;
    * a **transient error** (a crashed ffmpeg, a vanished file, an
      unavailable encoder) is recorded as FAILED on the row with a short
      message (never a stack) and RE-RAISED so the jobs layer applies its
      own retry/backoff.

    :func:`run_export_now` is the non-raising convenience wrapper.
    """
    row = _load_job(db, workspace_id, export_id)
    # The registry's canonical name, NOT an uppercased copy: "WebM" keeps its
    # mixed case in export_jobs.format, and a .upper()ed string silently
    # misses every mixed-case dict lookup (FORMAT_SUFFIX -> no file extension
    # -> "ffmpeg failed ... Invalid argument"). get_format normalizes input.
    spec = get_format(row.format)
    fmt = spec.name
    row.state = "RUNNING"
    row.started_at = row.started_at or utcnow()
    row.progress = 5
    db.flush()
    try:
        require_available(fmt)  # re-probe at run time: the build may differ
        profile = db.get(ExportProfile, row.profile_id) if row.profile_id else None
        config = dict((profile.config_json if profile is not None else None) or {})
        target = resolve_target(db, workspace_id, row.target_type, row.target_id)
        out_path = _exports_dir(workspace_id) / _artifact_name(target, fmt)

        def _progress(pct: int) -> None:
            row.progress = max(0, min(99, int(pct)))
            db.flush()

        context = ExportContext(
            fmt=fmt,
            out_path=out_path,
            doc=target.doc,
            config=config,
            workspace_id=workspace_id,
            target={"type": target.target_type, "id": target.target_id},
            source_path=target.source_path,
            duration_seconds=target.duration_seconds,
            watermark_text=watermark_text(db, workspace_id, config),
            metadata={
                "generated_at": datetime.now(UTC).isoformat(),
                "profile": {"id": profile.id if profile is not None else None,
                            "name": profile.name if profile is not None else "",
                            "preset": profile.preset if profile is not None else ""},
                "target_name": target.name,
            },
            progress=_progress,
        )
        if ctx is not None:
            jobs_service.check_cancelled(ctx)
        produced = spec.export(context)
        if ctx is not None:
            jobs_service.check_cancelled(ctx)
        row.progress = 90
        db.flush()
        return _persist(db, workspace_id, row, spec, context, produced, config)
    except Exception as exc:
        if not _is_cancellation(exc):
            _fail(db, row, _short(exc))
        raise


def _emit(workspace_id: str, kind: str, message: str, row: ExportJob) -> None:
    """Activity-ledger event (contracts §9). Best effort, never fatal.

    ``record_event`` opens its own session, so this runs AFTER the caller
    committed the row -- the same ordering as ``api/v1/timelines.py``.
    """
    try:
        from app.services.events import record_event

        record_event(
            workspace_id, kind, message, level="info", source="exports",
            data={"actor": row.created_by, "export_id": row.id,
                  "format": row.format, "attempt": int(row.attempt or 0),
                  "target": {"type": row.target_type, "id": row.target_id}},
        )
    except Exception:  # pragma: no cover - a ledger write never fails an export
        logger.warning("could not record %s for export %s", kind, row.id)


def _notify(workspace_id: str, kind: str, row: ExportJob, payload: dict) -> None:
    """Write the internal-inbox row for the export's creator (lane L service).

    Lane L owns ``services/notifications.py`` and may land in the same wave
    or a later one, so the import is lazy and guarded -- an export must never
    fail because the inbox service is not there yet (contracts §6 decision).
    """
    try:
        from app.services.notifications import on_event

        with session_scope() as inbox_db:
            on_event(inbox_db, workspace_id, kind, {
                "actor": row.created_by, "export_id": row.id,
                "format": row.format, **payload,
            })
    except Exception:
        logger.debug("notifications service unavailable for export %s", row.id)


def _fail(db: Session, row: ExportJob, message: str) -> None:
    """Persist a terminal failure with a short message (never a stack trace)."""
    row.state = "FAILED"
    row.error = message or "export failed"
    row.finished_at = utcnow()
    row.progress = 0
    try:
        db.commit()
    except Exception:  # pragma: no cover - commit failure must not mask the cause
        logger.warning("could not persist FAILED state for export %s", row.id)
    _emit(row.workspace_id, "EXPORT_FAILED",
          f"Export {row.id[:8]} ({row.format}) failed: {row.error[:120]}", row)
    _notify(row.workspace_id, "export.failed", row, {"error": row.error})


def _persist(
    db: Session,
    workspace_id: str,
    row: ExportJob,
    spec,
    context: ExportContext,
    produced: dict,
    config: dict,
) -> dict:
    """Register the MediaAsset, verify independently, then write the verdict."""
    from app.services.storage import validate_storage_key

    storage_key = validate_storage_key(
        workspace_id, f"exports/{context.out_path.name}")
    if not storage_key:
        raise ExportJobError("refusing to register an artifact outside workspace storage")
    size = context.out_path.stat().st_size
    checksum = sha256_file(context.out_path)
    asset = MediaAsset(
        workspace_id=workspace_id,
        type=FORMAT_ASSET_TYPE.get(context.fmt, "other"),
        origin="export",
        provider="exporter",
        storage_key=storage_key,
        mime_type=str(produced.get("media_type") or spec.media_type),
        duration_seconds=context.duration_seconds or None,
        width=int(config.get("width") or 0) or None,
        height=int(config.get("height") or 0) or None,
        frame_rate=float(config.get("fps") or 0) or None,
        codec=str(config.get("video_codec") or ""),
        audio_codec=str(config.get("audio_codec") or ""),
        channels=int(config.get("audio_channels") or 0) or None,
        file_size=size,
        checksum=checksum,
        meta_json={"format": context.fmt, "export_id": row.id,
                   "target": context.target, "produced": {
                       k: v for k, v in produced.items() if k != "source_probe"}},
    )
    db.add(asset)
    db.flush()
    row.artifact_asset_id = asset.id
    row.checksum = checksum

    source_duration = None
    source_probe = (produced or {}).get("source_probe") or {}
    if source_probe.get("duration_seconds"):
        source_duration = float(source_probe["duration_seconds"])
    elif context.duration_seconds:
        source_duration = float(context.duration_seconds)

    verdict = verify_export(db, workspace_id, row, path=context.out_path,
                            source_duration_seconds=source_duration,
                            config=config)
    row.verification_json = {
        "complete": verdict["complete"],
        "checks": verdict["checks"],
        "checksum": verdict["checksum"],
        "probe": verdict["probe"],
        "path": str(context.out_path),
        "config": {"width": config.get("width"), "height": config.get("height"),
                   "fps": config.get("fps")},
        "size": size,
    }
    row.progress = 100
    if verdict["complete"]:
        row.state = "COMPLETE"
        row.error = None
        row.finished_at = utcnow()
        db.commit()
        _emit(row.workspace_id, "EXPORT_COMPLETED",
              f"Export {row.id[:8]} ({row.format}) verified and complete", row)
        _notify(row.workspace_id, "export.completed", row,
                {"checksum": checksum, "size": size})
        return {"export_id": row.id, "state": "COMPLETE", "complete": True,
                "asset_id": asset.id, "checksum": checksum, "size": size}
    failed = ", ".join(verdict["failed"])
    row.state = "FAILED"
    row.error = f"verification failed: {failed}"
    row.finished_at = utcnow()
    db.commit()
    # A failed VERDICT is a terminal, honest outcome -- it is returned, not
    # raised, so the queue does not burn retries re-proving the same bytes.
    # Only transient errors (a crashed ffmpeg, a vanished file) raise above.
    _emit(row.workspace_id, "EXPORT_FAILED",
          f"Export {row.id[:8]} ({row.format}) failed verification: {failed}", row)
    _notify(row.workspace_id, "export.failed", row, {"error": row.error})
    return {"export_id": row.id, "state": "FAILED", "complete": False,
            "asset_id": asset.id, "checksum": checksum, "size": size,
            "error": row.error, "failed": verdict["failed"]}


def run_export_now(workspace_id: str, export_id: str) -> dict:
    """Synchronous ``run_export`` with its own session (tests + tooling).

    Used by the tests and by any caller that wants the result without going
    through the queue. Failure is returned as ``{"state": "FAILED",
    "error": ...}`` rather than raised, so a caller can assert on the
    persisted failure state directly.
    """
    with session_scope() as db:
        try:
            return run_export(db, workspace_id, export_id)
        except Exception as exc:  # noqa: BLE001 - state is already persisted
            row = db.get(ExportJob, export_id)
            return {"export_id": export_id,
                    "state": str(row.state or "FAILED") if row else "FAILED",
                    "error": _short(exc)}


def artifact_path(workspace_id: str, row: ExportJob) -> Path | None:
    """The managed path of an export artifact (None when absent/foreign)."""
    from app.models import MediaAsset

    from .verify import resolve_artifact_path

    if not row.artifact_asset_id:
        return None
    with session_scope() as db:
        asset = db.get(MediaAsset, row.artifact_asset_id)
        if asset is None or asset.workspace_id != workspace_id:
            return None
        key = asset.storage_key
    return resolve_artifact_path(workspace_id, key)


__all__ = [
    "CANCELLABLE_STATES",
    "ExportContext",
    "ExportJobError",
    "ExportTarget",
    "ExportValidationError",
    "JOB_TYPE",
    "MAX_ATTEMPTS",
    "RETRYABLE_STATES",
    "artifact_path",
    "cancel_export",
    "enqueue_export",
    "get_export",
    "handle_export_build",
    "list_exports",
    "register_export_jobs",
    "resolve_target",
    "retry_export",
    "run_export",
    "run_export_now",
]
