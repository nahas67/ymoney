"""Retention policy + sweep (Work 11 Lane L) -- contracts §10.

Two clearly separated responsibilities:

1. **The policy row** (``retention_policies``, one per workspace). Every
   day count is nullable and NULL means *keep forever*. Values are stored
   and reported; :func:`validate_days` is the single validation entry
   point (non-negative ints, max 3650 days).

2. **The sweep** (:func:`sweep`) -- deletes ONLY storage, never history::

       render assets   older than render_retention_days
       export artifacts older than export_retention_days
       temp assets     older than temp_asset_retention_days

   **Audit rows are never hard-deleted.** ``audit_retention_days`` is
   stored and reported, and the sweep reports how many ledger rows it
   deliberately left untouched (``events_untouched``). Deleting audit
   history would need an explicit, separate policy action; a retention
   sweep silently erasing the evidence trail is exactly the behaviour
   this module refuses to have.

   **Guards (strict superset of the contract's three).** An asset is
   never deleted while ANY of these hold:

     * ``live_review``      -- a non-terminal Review targets it
     * ``open_comment``     -- an unresolved Comment targets it
     * ``live_export``      -- a QUEUED/RUNNING ExportJob owns it
     * ``lineage_parent``   -- a parent ContentItem's scenes use it
     * ``project_target``   -- a ProjectTarget row links it
     * ``referenced_by_content`` -- any Scene or timeline clip uses it

   The reference scan is fail-closed: if it cannot be completed the
   sweep deletes NOTHING in that pass and says so in ``skipped``.

The job handler ``RETENTION_SWEEP`` is registered through
:func:`register_retention_jobs`, which is idempotent and guarded (see
``api/v1/knowledge.py::_bootstrap_source_jobs`` for the same pattern).
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Comment,
    ContentItem,
    ContentTimeline,
    EventLog,
    ExportJob,
    MediaAsset,
    ProjectTarget,
    RetentionPolicy,
    Review,
    Scene,
)
from app.models.base import utcnow
from app.services import jobs as jobs_service

logger = logging.getLogger("ymoney.collab")

JOB_TYPE = "RETENTION_SWEEP"

#: hard ceiling (10 years) -- a longer "retention" is a misconfiguration.
MAX_RETENTION_DAYS = 3650

#: the four stored day counts, in the contract's order.
POLICY_FIELDS: tuple[str, ...] = (
    "audit_retention_days",
    "render_retention_days",
    "temp_asset_retention_days",
    "export_retention_days",
)

#: review states that are still "live" (a target under an open review is
#: referenced work, not garbage).
_LIVE_REVIEW_STATES = ("DRAFT", "IN_REVIEW", "CHANGES_REQUESTED")
_LIVE_EXPORT_STATES = ("QUEUED", "RUNNING")

#: MediaAsset origins treated as temporary intermediates.
_TEMP_ORIGINS = ("proxy",)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def get_policy(db: Session, workspace_id: str) -> RetentionPolicy | None:
    """The workspace policy row, or None when never configured."""
    return db.scalar(
        select(RetentionPolicy).where(RetentionPolicy.workspace_id == workspace_id)
    )


def policy_dto(row: RetentionPolicy | None) -> dict:
    """Wire shape. ``None`` row -> every field NULL (keep forever)."""
    if row is None:
        values: dict[str, Any] = {name: None for name in POLICY_FIELDS}
        values.update(
            {
                "id": None,
                "workspace_id": None,
                "updated_by": None,
                "updated_at": None,
                "audit_enforced": False,  # audit rows are reported, never deleted
            }
        )
        return values
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        **{name: getattr(row, name) for name in POLICY_FIELDS},
        "updated_by": row.updated_by,
        "updated_at": (row.updated_at.isoformat() + "Z") if row.updated_at else None,
        "audit_enforced": False,  # audit rows are reported, never deleted
    }


def validate_days(values: dict) -> dict:
    """Validate one policy update body -> clean ``{field: int|None}``.

    Accepts NULL (reset to keep-forever) and non-negative ints up to
    ``MAX_RETENTION_DAYS``. Raises ``ValueError`` with a short message
    for anything else; routes answer 422.
    """
    unknown = sorted(set(values) - set(POLICY_FIELDS))
    if unknown:
        raise ValueError(f"unknown retention field(s): {', '.join(unknown)}")
    out: dict[str, int | None] = {}
    for name in POLICY_FIELDS:
        if name not in values:
            continue
        raw = values[name]
        if raw is None:
            out[name] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"{name} must be an integer number of days or null")
        try:
            days = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer number of days or null") from None
        if str(raw).strip() not in (str(days), f"{days}.0", str(days) + ".0"):
            raise ValueError(f"{name} must be an integer number of days or null")
        if days < 0:
            raise ValueError(f"{name} must be >= 0")
        if days > MAX_RETENTION_DAYS:
            raise ValueError(f"{name} must be <= {MAX_RETENTION_DAYS} days")
        out[name] = days
    return out


def upsert_policy(
    db: Session,
    workspace_id: str,
    values: dict,
    *,
    by_user: str | None = None,
) -> RetentionPolicy:
    """Create/update the single policy row. Values are validated first."""
    clean = validate_days(values)
    row = get_policy(db, workspace_id)
    if row is None:
        row = RetentionPolicy(workspace_id=workspace_id)
        db.add(row)
    for name, days in clean.items():
        setattr(row, name, days)
    if by_user:
        row.updated_by = by_user
    db.flush()
    return row


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def _asset_ids_from_scenes(db: Session, workspace_id: str) -> set[str]:
    out: set[str] = set()
    rows = db.scalars(select(Scene).where(Scene.workspace_id == workspace_id)).all()
    for row in rows:
        for entry in row.assets_json or []:
            if isinstance(entry, dict):
                value = str(entry.get("asset_id") or "")
            else:
                value = str(entry or "")
            if value:
                out.add(value)
    return out


def _asset_ids_from_timelines(db: Session, workspace_id: str) -> set[str]:
    out: set[str] = set()
    rows = db.scalars(
        select(ContentTimeline).where(ContentTimeline.workspace_id == workspace_id)
    ).all()
    for row in rows:
        for track in (row.tracks_json or {}).get("tracks", []) or []:
            for clip in track.get("clips", []) or []:
                source = clip.get("source") or {}
                value = str(source.get("asset_id") or "")
                if value:
                    out.add(value)
    return out


def protected_asset_ids(db: Session, workspace_id: str) -> set[str]:
    """Every asset id a guard would refuse to delete.

    Raises on a broken scan so the caller can fail closed rather than
    delete something still in use.
    """
    protected: set[str] = set()

    for row in db.scalars(
        select(Review.target_id).where(
            Review.workspace_id == workspace_id,
            Review.state.in_(_LIVE_REVIEW_STATES),
        )
    ).all():
        if row:
            protected.add(str(row))

    for row in db.scalars(
        select(Comment.target_id).where(
            Comment.workspace_id == workspace_id,
            Comment.resolved_at.is_(None),
        )
    ).all():
        if row:
            protected.add(str(row))

    for row in db.scalars(
        select(ExportJob.artifact_asset_id).where(
            ExportJob.workspace_id == workspace_id,
            ExportJob.state.in_(_LIVE_EXPORT_STATES),
        )
    ).all():
        if row:
            protected.add(str(row))

    # ProjectTarget carries no workspace column: resolve through Project
    # first, so a target id linked in ANOTHER workspace cannot protect
    # (nor is protected by) this workspace's assets.
    from app.models import Project

    project_ids = [
        str(value)
        for value in db.scalars(
            select(Project.id).where(Project.workspace_id == workspace_id)
        ).all()
    ]
    if project_ids:
        for row in db.scalars(
            select(ProjectTarget.target_id).where(
                ProjectTarget.project_id.in_(project_ids)
            )
        ).all():
            if row:
                protected.add(str(row))

    parents = set(
        str(value)
        for value in db.scalars(
            select(ContentItem.parent_content_id).where(
                ContentItem.workspace_id == workspace_id,
                ContentItem.parent_content_id.is_not(None),
            )
        ).all()
        if value
    )
    if parents:
        for row in db.scalars(
            select(Scene).where(
                Scene.workspace_id == workspace_id, Scene.content_item_id.in_(parents)
            )
        ).all():
            for entry in row.assets_json or []:
                value = (
                    str(entry.get("asset_id") or "")
                    if isinstance(entry, dict)
                    else str(entry or "")
                )
                if value:
                    protected.add(value)

    protected |= _asset_ids_from_scenes(db, workspace_id)
    protected |= _asset_ids_from_timelines(db, workspace_id)
    return {value for value in protected if value}


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def _candidates(
    db: Session, workspace_id: str, *, render_days: int | None, export_days: int | None,
    temp_days: int | None,
) -> tuple[list[MediaAsset], list[MediaAsset], list[MediaAsset]]:
    """(render, export_artifact, temp) asset rows eligible for deletion."""
    now = utcnow()
    render: list[MediaAsset] = []
    exports: list[MediaAsset] = []
    temp: list[MediaAsset] = []
    if render_days is not None:
        cutoff = now - timedelta(days=render_days)
        render = list(
            db.scalars(
                select(MediaAsset).where(
                    MediaAsset.workspace_id == workspace_id,
                    MediaAsset.origin == "render",
                    MediaAsset.created_at < cutoff,
                )
            ).all()
        )
    if export_days is not None:
        cutoff = now - timedelta(days=export_days)
        artifact_ids = [
            str(value)
            for value in db.scalars(
                select(ExportJob.artifact_asset_id).where(
                    ExportJob.workspace_id == workspace_id,
                    ExportJob.state == "COMPLETE",
                    ExportJob.finished_at.is_not(None),
                    ExportJob.finished_at < cutoff,
                )
            ).all()
            if value
        ]
        if artifact_ids:
            exports = list(
                db.scalars(
                    select(MediaAsset).where(
                        MediaAsset.workspace_id == workspace_id,
                        MediaAsset.id.in_(artifact_ids),
                    )
                ).all()
            )
    if temp_days is not None:
        cutoff = now - timedelta(days=temp_days)
        temp = list(
            db.scalars(
                select(MediaAsset).where(
                    MediaAsset.workspace_id == workspace_id,
                    MediaAsset.origin.in_(_TEMP_ORIGINS),
                    MediaAsset.created_at < cutoff,
                )
            ).all()
        )
    return render, exports, temp


def _unlink_file(row: MediaAsset, workspace_id: str) -> bool:
    """Best-effort byte removal through the storage boundary. True when
    the file is gone (or was never on disk)."""
    from app.services import storage as storage_service

    path = storage_service.managed_path(workspace_id, str(row.storage_key or ""))
    if path is None:
        return True
    with suppress(OSError):
        if path.exists():
            path.unlink()
        return True
    return False


def sweep(db: Session, workspace_id: str) -> dict:
    """Run one retention pass. Returns the contracts §10 result dict.

    ``{"deleted": n, "skipped": {reason: n}, "events_untouched": n}``.
    Audit rows are counted, never deleted. The caller owns the commit;
    the RETENTION_SWEEP event is emitted by the caller after commit (same
    ordering as every other route in this package -- ``record_event``
    opens its own session).
    """
    ws_id = str(workspace_id or "")
    if not ws_id:
        raise ValueError("workspace_id is required")
    result: dict[str, Any] = {"deleted": 0, "skipped": {}, "events_untouched": 0}

    def skip(reason: str) -> None:
        result["skipped"][reason] = int(result["skipped"].get(reason, 0)) + 1

    policy = get_policy(db, ws_id)
    audit_days = getattr(policy, "audit_retention_days", None) if policy else None
    render_days = getattr(policy, "render_retention_days", None) if policy else None
    export_days = getattr(policy, "export_retention_days", None) if policy else None
    temp_days = getattr(policy, "temp_asset_retention_days", None) if policy else None

    # audit is reported, NEVER deleted -- count it before anything else so
    # the number is present even when every storage policy is NULL.
    result["events_untouched"] = int(
        db.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(EventLog.workspace_id == ws_id)
        )
        or 0
    )
    result["policy"] = {
        "audit_retention_days": audit_days,
        "render_retention_days": render_days,
        "export_retention_days": export_days,
        "temp_asset_retention_days": temp_days,
        "audit_enforced": False,
    }

    if render_days is None and export_days is None and temp_days is None:
        result["skipped"]["policy_unset"] = 0
        return result

    try:
        protected = protected_asset_ids(db, ws_id)
    except Exception:  # noqa: BLE001 -- fail closed, never guess
        logger.exception("retention guard scan failed for workspace %s", ws_id)
        result["skipped"]["guard_scan_failed"] = 1
        return result

    seen: set[str] = set()
    render_rows, export_rows, temp_rows = _candidates(
        db, ws_id, render_days=render_days, export_days=export_days, temp_days=temp_days
    )
    for bucket, bucket_name in (
        (render_rows, "render"),
        (export_rows, "export"),
        (temp_rows, "temp"),
    ):
        for row in bucket:
            if row.id in seen:
                continue  # an asset can match two policies; delete it once
            seen.add(row.id)
            if row.id in protected:
                skip("protected")
                continue
            if not _unlink_file(row, ws_id):
                skip("file_locked")
                continue
            db.delete(row)
            result["deleted"] = int(result["deleted"]) + 1
            logger.debug(
                "retention deleted %s asset %s (%s)", bucket_name, row.id, row.origin
            )
    db.flush()
    return result


# ---------------------------------------------------------------------------
# job handler (guarded bootstrap, contracts §10)
# ---------------------------------------------------------------------------


def handle_retention_sweep(ctx: jobs_service.JobContext) -> dict:
    """``RETENTION_SWEEP`` job: one pass for ``payload.workspace_id``."""
    workspace_id = str(ctx.workspace_id or (ctx.payload or {}).get("workspace_id") or "")
    if not workspace_id:
        return {"skipped": "RETENTION_SWEEP job requires workspace_id"}
    from app.db import session_scope
    from app.services import activity as activity_service

    with session_scope() as db:
        jobs_service.check_cancelled(ctx)
        result = sweep(db, workspace_id)
    activity_service.emit(
        workspace_id,
        "RETENTION_SWEEP",
        message=(
            f"Retention sweep: {result['deleted']} asset(s) deleted, "
            f"{result['events_untouched']} audit row(s) untouched"
        ),
        actor=None,
        data=dict(result),
    )
    return result


def register_retention_jobs() -> None:
    """Register RETENTION_SWEEP — idempotent, safe to call on repeat."""
    if JOB_TYPE in jobs_service._handlers:
        return
    jobs_service.register_handler(JOB_TYPE, handle_retention_sweep)


def enqueue_sweep(workspace_id: str, *, delay_seconds: float = 0.0) -> str | None:
    """Queue one sweep pass for a workspace."""
    register_retention_jobs()
    return jobs_service.enqueue(
        JOB_TYPE,
        {"workspace_id": workspace_id},
        workspace_id=workspace_id,
        delay_seconds=delay_seconds,
    )


__all__ = [
    "JOB_TYPE",
    "MAX_RETENTION_DAYS",
    "POLICY_FIELDS",
    "enqueue_sweep",
    "get_policy",
    "handle_retention_sweep",
    "policy_dto",
    "protected_asset_ids",
    "register_retention_jobs",
    "sweep",
    "upsert_policy",
    "validate_days",
]
