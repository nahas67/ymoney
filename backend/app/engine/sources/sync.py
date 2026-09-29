"""SOURCE_SYNC durable job (Work 10 Lane C).

Registration mirrors ``engine.community.sync``: ``register_source_jobs()``
is guard-on-present idempotent and is called at module import, so
importing this module anywhere registers the handler exactly once (the
guarded bootstrap in ``api/v1/inbox.py`` may re-call it safely).

Enqueue policy (contract delta): we deliberately do NOT pass
``idempotency_key`` to ``jobs.enqueue`` — that key dedupes against ANY
historical job for the key and would block every re-sync forever.
``enqueue_source_sync`` dedupes only against INFLIGHT (queued/running)
SOURCE_SYNC jobs for the same connector + workspace.

Commit discipline: the handler is sync (``jobs._execute`` runs it via
``asyncio.to_thread`` inside its own ``session_scope``, like the
community handlers), with two durability twists required by the spec:
  * each page commits its upserts AND the resume cursor, so an
    interrupted run restarts at that page instead of from the top;
  * the connector failure record (last_error/status) is committed BEFORE
    re-raising — ``session_scope`` would roll a plain flush back on raise.

Failure isolation: any adapter/SourceError is recorded on THAT connector
row only (last_error truncated to 500; status ERROR, or UNAVAILABLE for
configuration problems) and re-raised so the jobs layer applies
retry/backoff. Cancellation (``jobs._Cancelled``) propagates without
being recorded as a connector failure.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.engine.sources.base import SourceConfigError, SourceError, canonical_remote_id
from app.engine.sources.registry import create_connector
from app.models import Job, SourceConnector, SourceDocument
from app.models.base import utcnow
from app.services import jobs as jobs_service

JOB_TYPE = "SOURCE_SYNC"
_ERROR_LIMIT = 500
_REASON_LIMIT = 200
# active = will still run; dedupe ONLY against these (never history)
_ACTIVE_JOB_STATUSES = ("QUEUED", "RUNNING")

# jobs._Cancelled is private; resolve defensively so a jobs refactor can
# never turn cancellation into a fake connector ERROR
_CANCELLED_CLS = getattr(jobs_service, "_Cancelled", None)


def _is_cancellation(exc: BaseException) -> bool:
    return _CANCELLED_CLS is not None and isinstance(exc, _CANCELLED_CLS)


def register_source_jobs() -> None:
    """Register SOURCE_SYNC — idempotent, safe to call on repeat/reload."""
    if JOB_TYPE in jobs_service._handlers:
        return
    jobs_service.register_handler(JOB_TYPE, handle_source_sync)


def enqueue_source_sync(db: Session, workspace_id: str, connector_id: str) -> str | None:
    """Queue (or join) a SOURCE_SYNC job for one connector; returns job id.

    The connector must exist in ``workspace_id`` and be enabled, else a
    SourceError with a safe message. Dedupe scope: in-flight
    queued/running jobs for THIS connector only — completed history never
    blocks a re-sync (jobs idempotency_key is intentionally unused).
    """
    ws = str(workspace_id or "")
    cid = str(connector_id or "")
    row = db.get(SourceConnector, cid) if cid else None
    if not ws or row is None or row.workspace_id != ws:
        raise SourceError(f"source connector not found: {cid or '<missing>'}")
    if not row.enabled:
        raise SourceError("source connector is disabled; enable it before syncing")
    inflight = _inflight_sync_job(db, ws, cid)
    if inflight:
        return inflight
    return jobs_service.enqueue(JOB_TYPE, {"connector_id": cid}, workspace_id=ws)


def _inflight_sync_job(db: Session, workspace_id: str, connector_id: str) -> str | None:
    jobs = db.scalars(
        select(Job)
        .where(
            Job.type == JOB_TYPE,
            Job.workspace_id == workspace_id,
            Job.status.in_(_ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.created_at.desc())
    ).all()
    for job in jobs:
        if str((job.payload or {}).get("connector_id") or "") == connector_id:
            return str(job.id)
    return None


def handle_source_sync(ctx: jobs_service.JobContext) -> dict:
    """``SOURCE_SYNC`` job: sync ONE connector (payload ``connector_id``).

    Workspace comes from ``ctx.workspace_id`` and hard-filters every
    query: a foreign or missing connector is skipped honestly and never
    touched. Returns created/updated/unchanged/deleted counts on
    success, ``{"skipped": reason}`` otherwise; adapter failures record
    themselves on the connector row and re-raise for jobs retry.
    """
    workspace_id = str(ctx.workspace_id or "")
    payload = ctx.payload or {}
    connector_id = str(payload.get("connector_id") or "")
    if not workspace_id:
        return {"skipped": "SOURCE_SYNC job requires workspace_id"}
    if not connector_id:
        return {"skipped": "SOURCE_SYNC job requires connector_id"}
    with session_scope() as db:
        row = db.scalar(
            select(SourceConnector).where(
                SourceConnector.id == connector_id,
                SourceConnector.workspace_id == workspace_id,
            )
        )
        if row is None:
            return {"skipped": "connector not found in this workspace"}
        if not row.enabled:
            return {"skipped": "connector is disabled"}
        try:
            return _sync_connector(db, row, workspace_id, ctx)
        except Exception as exc:
            if not _is_cancellation(exc):
                _record_failure(db, connector_id, workspace_id, exc)
            raise


def _sync_connector(
    db: Session, row: SourceConnector, workspace_id: str, ctx: jobs_service.JobContext
) -> dict:
    config = dict(row.config_json or {})
    # scope comes from the hard-filtered row, never from persisted config
    config["workspace_id"] = workspace_id
    connector = create_connector(row.kind, config)
    started_at_beginning = not str(row.last_cursor or "")
    counts = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    seen: set[str] = set()
    cursor = str(row.last_cursor or "") or None

    while True:
        jobs_service.check_cancelled(ctx)
        docs, next_cursor = connector.list(cursor=cursor)
        next_cursor = str(next_cursor or "")
        for doc in docs:
            jobs_service.check_cancelled(ctx)
            remote_id = canonical_remote_id(doc.remote_id)
            if not remote_id:
                continue  # no stable identity — never invent one
            seen.add(remote_id)
            _upsert_document(db, workspace_id, row.id, doc, remote_id, counts)
        if not next_cursor:
            break
        # durable resume point: commit BEFORE the next page can fail, so an
        # interrupted run restarts here instead of replaying earlier pages
        row.last_cursor = next_cursor
        db.commit()
        cursor = next_cursor

    # deletion only after a COMPLETE pass of a snapshot connector: the pass
    # must have started at the beginning (a resumed run never saw page 1, so
    # it is not entitled to declare unseen rows deleted)
    if connector.snapshot and started_at_beginning:
        counts["deleted"] = _mark_unseen_deleted(db, workspace_id, row.id, seen)

    row.last_cursor = ""
    row.last_sync_at = utcnow()
    row.last_error = ""
    row.unavailable_reason = ""
    if row.status != "DISABLED":
        row.status = "AVAILABLE"
    db.flush()
    row.doc_count = _count_live(db, workspace_id, row.id)
    return {**counts, "next_cursor": ""}


def _record_failure(
    db: Session, connector_id: str, workspace_id: str, exc: Exception
) -> None:
    """Commit the honest failure onto the connector row; never mask ``exc``."""
    try:
        db.rollback()  # drop partial page state; page commits already durable
        fresh = db.scalar(
            select(SourceConnector).where(
                SourceConnector.id == connector_id,
                SourceConnector.workspace_id == workspace_id,
            )
        )
        if fresh is None:
            return
        message = str(exc).strip() or type(exc).__name__
        fresh.last_error = message[:_ERROR_LIMIT]
        if isinstance(exc, SourceConfigError):
            fresh.status = "UNAVAILABLE"
            fresh.unavailable_reason = message[:_REASON_LIMIT]
        elif fresh.status != "DISABLED":
            fresh.status = "ERROR"
        db.commit()
    except Exception:  # pragma: no cover - bookkeeping must not mask the cause
        with contextlib.suppress(Exception):
            db.rollback()


def _upsert_document(
    db: Session,
    workspace_id: str,
    connector_id: str,
    doc,
    remote_id: str,
    counts: dict,
) -> None:
    existing = db.scalar(
        select(SourceDocument).where(
            SourceDocument.workspace_id == workspace_id,
            SourceDocument.connector_id == connector_id,
            SourceDocument.remote_id == remote_id,
        )
    )
    now = utcnow()
    incoming_checksum = (doc.checksum or "")[:64]
    if existing is None:
        db.add(
            SourceDocument(
                workspace_id=workspace_id,
                connector_id=connector_id,
                remote_id=remote_id,
                title=doc.title or "",
                mime_type=(doc.mime_type or "")[:100],
                remote_created_at=_naive_utc(doc.created_at),
                remote_updated_at=_naive_utc(doc.updated_at),
                author=(doc.author or "")[:200],
                content=doc.content or "",
                asset_reference=(doc.asset_reference or "")[:500],
                checksum=incoming_checksum,
                cursor=(doc.cursor or "")[:200],
                state="active",
                first_seen_at=now,
                last_seen_at=now,
                meta_json=dict(doc.meta or {}),
            )
        )
        counts["created"] += 1
        return
    changed = (
        (existing.checksum or "") != incoming_checksum
        or (existing.content or "") != (doc.content or "")
        or (existing.title or "") != (doc.title or "")
    )
    if not changed:
        existing.last_seen_at = now
        if existing.state == "deleted":
            # identical content seen upstream again: resurrect honestly
            existing.state = "active"
        counts["unchanged"] += 1
        return
    existing.title = doc.title or ""
    existing.mime_type = (doc.mime_type or "")[:100]
    existing.remote_created_at = _naive_utc(doc.created_at)
    existing.remote_updated_at = _naive_utc(doc.updated_at)
    existing.author = (doc.author or "")[:200]
    existing.content = doc.content or ""
    existing.asset_reference = (doc.asset_reference or "")[:500]
    existing.checksum = incoming_checksum
    existing.cursor = (doc.cursor or "")[:200]
    existing.meta_json = dict(doc.meta or {})
    existing.state = "updated"
    existing.last_seen_at = now
    counts["updated"] += 1


def _mark_unseen_deleted(
    db: Session, workspace_id: str, connector_id: str, seen: set[str]
) -> int:
    stmt = select(SourceDocument).where(
        SourceDocument.workspace_id == workspace_id,
        SourceDocument.connector_id == connector_id,
        SourceDocument.state.in_(("active", "updated")),
    )
    if seen:
        stmt = stmt.where(SourceDocument.remote_id.notin_(seen))
    stale = db.scalars(stmt).all()
    for row in stale:
        row.state = "deleted"  # history preserved — never physically deleted
    return len(stale)


def _count_live(db: Session, workspace_id: str, connector_id: str) -> int:
    total = db.scalar(
        select(func.count())
        .select_from(SourceDocument)
        .where(
            SourceDocument.workspace_id == workspace_id,
            SourceDocument.connector_id == connector_id,
            SourceDocument.state.in_(("active", "updated")),
        )
    )
    return int(total or 0)


def _naive_utc(value: datetime | None) -> datetime | None:
    """Adapters should hand us naive UTC; tolerate tz-aware inputs."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


register_source_jobs()


__all__ = [
    "JOB_TYPE",
    "enqueue_source_sync",
    "handle_source_sync",
    "register_source_jobs",
]
