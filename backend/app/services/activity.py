"""Activity ledger QUERY surface (Work 11 Lane L) -- contracts §9.

The ledger is the existing append-only ``events`` table
(``models/ops.py::EventLog``). Every lane emits through the single
primitive ``services/events.py::record_event``; this module only READS
what was written and normalizes the structured ``data_json`` contract::

    {"actor": <user_id>,
     "target": {"type": <str>, "id": <str>},
     "version": <str|None>,
     "project_id": <str|None>}

Read-only by construction: nothing in this module writes, updates or
deletes an ``EventLog`` row, and no route in ``api/`` exposes a mutator
for events (the API probe test in ``tests/test_activity_ledger.py``
asserts 404/405 on every mutating verb).

``emit()`` is the thin, convention-following wrapper lanes may use so the
``data_json`` shape is written in ONE place; it delegates to
``record_event`` and therefore also fans out to SSE + outbound webhooks.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import EventLog
from app.services.events import record_event
from app.services.json_portability import json_value_equals

logger = logging.getLogger("ymoney.collab")

#: Work 11 required + internal event kinds (contracts §9). Bare names, no
#: prefix -- the webhook whitelist is extended with the same strings.
ACTIVITY_KINDS: tuple[str, ...] = (
    # user-visible collaboration
    "PROJECT_CREATED",
    "PROJECT_UPDATED",
    "PROJECT_MEMBER_ADDED",
    "PROJECT_MEMBER_REMOVED",
    "PROJECT_TRANSFERRED",
    "TIMELINE_EDITED",
    "VERSION_CREATED",
    "COMMENT_ADDED",
    "REVIEW_REQUESTED",
    "CHANGES_REQUESTED",
    "APPROVED",
    "EXPORT_CREATED",
    "PUBLISHED",
    # internal / lane-owned
    "ARCHIVE_CREATED",
    "RETENTION_SWEEP",
    "REVIEW_ASSIGNED",
    "REVISION_REQUESTED",
    "EXPORT_COMPLETED",
    "EXPORT_FAILED",
    "RETENTION_POLICY_UPDATED",
)

#: Ledger page cap (a feed is never unbounded).
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


# ---------------------------------------------------------------------------
# emit (the ONE place that shapes data_json for Work 11)
# ---------------------------------------------------------------------------


def emit(
    workspace_id: str,
    kind: str,
    *,
    message: str = "",
    actor: str | None = None,
    target: dict | None = None,
    version: str | None = None,
    project_id: str | None = None,
    data: dict | None = None,
    level: str = "info",
    source: str = "collab",
) -> dict:
    """Append one ledger row via ``record_event`` with the §9 data shape.

    ``data`` is merged last so a lane can add its own keys without
    breaking the ledger contract. Returns the ``record_event`` payload.
    """
    payload: dict[str, Any] = {
        "actor": actor,
        "target": dict(target) if target else None,
        "version": version,
        "project_id": project_id,
    }
    if data:
        payload.update(data)
    return record_event(workspace_id, kind, message, level=level, source=source, data=payload)


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------


def parse_since(raw: str | None) -> datetime | None:
    """ISO-8601 (with or without ``Z``) -> naive UTC datetime.

    Returns None for an empty value; raises ``ValueError`` for anything
    unparseable so routes can answer 422 instead of silently ignoring it.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz=None).replace(tzinfo=None)
    return parsed


def event_dto(row: EventLog) -> dict:
    """Wire shape of one ledger row (actor/target/version hoisted out of data)."""
    data = dict(row.data_json or {})
    target = data.get("target") or {}
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "level": row.level,
        "source": row.source,
        "message": row.message,
        "actor": data.get("actor"),
        "target": {"type": target.get("type"), "id": target.get("id")} if target else None,
        "version": data.get("version"),
        "project_id": data.get("project_id"),
        "request_id": row.request_id,
        "data": data,
        "created_at": (row.created_at.isoformat() + "Z") if row.created_at else "",
    }


def newest_first_tiebreak(db: Session):
    """Secondary ORDER BY key: the append order when ``created_at`` ties.

    ``record_event`` stamps rows with ``datetime.now()``, whose Windows
    granularity lets two appends in the same tick share a timestamp. The
    primary key is a random uuid, so ``id DESC`` would shuffle a genuinely
    newer row below an older one -- the ledger would contradict its own
    "newest first" contract. SQLite's ``rowid`` is assigned monotonically
    at INSERT time, which IS this table's append order; on other dialects
    (microsecond clocks) the uuid fallback is kept rather than emitting
    dialect-specific SQL.
    """
    if db.get_bind().dialect.name == "sqlite":
        return text("rowid DESC")
    return EventLog.id.desc()


def query(
    db: Session,
    workspace_id: str,
    *,
    kind: str | None = None,
    project_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Newest-first, workspace-filtered ledger page.

    Every filter is ANDed and workspace-scoped: a foreign project/target
    id can never widen the result, it can only narrow it to nothing.
    Structured filters read ``data_json`` through the JSON path index
    (portable on SQLite + PostgreSQL), never through string matching.

    Why ``json_value_equals`` and not ``.as_string()``
    --------------------------------------------------
    ``EventLog.data_json["project_id"].as_string() == str(project_id)`` reads
    like a JSON-string comparison and is not one on both backends. PostgreSQL
    compiles it to ``CAST((col ->> 'project_id') AS VARCHAR)``, which stringifies
    a JSON number, so it matches a row written ``{"project_id": 42}``. SQLite
    compiles it to ``JSON_EXTRACT(col, '$."project_id"')`` with NO cast, and
    ``42 = '42'`` is FALSE in SQLite, so the same stored row is invisible.
    Measured on both over identical rows:

    ====================  ==========  ============
    construct             SQLite      PostgreSQL
    ====================  ==========  ============
    ``.as_string()``      ``[1]``     ``[1, 2]``
    ``json_value_equals`` ``[1, 2]`` ``[1, 2]``
    ====================  ==========  ============

    Idempotency-key style filters in other modules are correct today only
    because every current writer happens to store a STRING; the helper removes
    that dependency on a coincidence of writers. See
    ``services/json_portability.py`` for the full backend audit.
    """
    stmt = select(EventLog).where(EventLog.workspace_id == workspace_id)
    if kind:
        stmt = stmt.where(EventLog.kind == kind)
    if project_id:
        stmt = stmt.where(json_value_equals(EventLog.data_json, "project_id",
                                            project_id))
    if target_type:
        stmt = stmt.where(json_value_equals(EventLog.data_json,
                                            ("target", "type"), target_type))
    if target_id:
        stmt = stmt.where(json_value_equals(EventLog.data_json,
                                            ("target", "id"), target_id))
    if since is not None:
        stmt = stmt.where(EventLog.created_at >= since)
    capped = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    stmt = stmt.order_by(
        EventLog.created_at.desc(), newest_first_tiebreak(db)
    ).limit(capped)
    return [event_dto(row) for row in db.scalars(stmt).all()]


def count_since(db: Session, workspace_id: str, since: datetime) -> int:
    """How many ledger rows this workspace wrote since ``since``."""
    total = db.scalar(
        select(func.count())
        .select_from(EventLog)
        .where(EventLog.workspace_id == workspace_id, EventLog.created_at >= since)
    )
    return int(total or 0)


__all__ = [
    "ACTIVITY_KINDS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "count_since",
    "emit",
    "event_dto",
    "newest_first_tiebreak",
    "parse_since",
    "query",
]
