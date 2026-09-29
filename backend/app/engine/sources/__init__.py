"""Source-connector engine (Work 10 Lane C).

Public surface for Lane D (``api/v1/knowledge.py``) and the guarded job
bootstrap in ``api/v1/inbox.py``:

* :data:`CONNECTOR_CATALOG` / :func:`create_connector` — the 13-kind registry;
* :class:`SourceError` (with the :class:`SourceConfigError` subclass) — honest,
  user-safe failures;
* :func:`enqueue_source_sync` / :func:`register_source_jobs` — the durable
  SOURCE_SYNC job (importing this package registers it, idempotently —
  the guarded bootstrap may re-call ``register_source_jobs()`` safely);
* :func:`describe_connector` — THE redacted connector renderer. API layers
  must pass rows through this instead of touching ``config_json`` themselves:
  sensitive keys (token|key|secret|password|credential) are stripped and only
  a ``has_credentials`` bool survives.

Adapters live in ``adapters/`` (local, url, rss, youtube, s3); the sync job
lives in ``sync.py``.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select

from app.engine.sources.base import (
    ALLOWED_TEXT_MIME,
    AVAILABLE,
    ERROR,
    MAX_ASSET_BYTES,
    MAX_SOURCE_BYTES,
    UNAVAILABLE,
    SourceConfigError,
    SourceConnector,
    SourceDocumentDoc,
    SourceError,
)
from app.engine.sources.registry import CONNECTOR_CATALOG, create_connector
from app.engine.sources.sync import (
    JOB_TYPE,
    enqueue_source_sync,
    handle_source_sync,
    register_source_jobs,
)
from app.models import SourceDocument

#: config keys whose values never leave the server
_SENSITIVE_CONFIG_KEY = re.compile(r"token|key|secret|password|credential", re.IGNORECASE)


def describe_connector(db, workspace_id, row) -> dict:
    """Redacted connector summary — the single config renderer for API/UI.

    Strips every sensitive config key (matching token|key|secret|password|
    credential) and reports ``has_credentials`` instead; ``doc_count`` is a
    live count of active/updated documents. Raises :class:`SourceError` when
    the row does not belong to ``workspace_id``.
    """
    ws = str(workspace_id or "")
    if not ws or row.workspace_id != ws:
        raise SourceError("source connector does not belong to this workspace")
    raw = dict(row.config_json or {})
    redacted = {k: v for k, v in raw.items() if not _SENSITIVE_CONFIG_KEY.search(str(k))}
    has_credentials = any(
        _SENSITIVE_CONFIG_KEY.search(str(k)) and v for k, v in raw.items()
    )
    live_docs = (
        db.scalar(
            select(func.count())
            .select_from(SourceDocument)
            .where(
                SourceDocument.workspace_id == row.workspace_id,
                SourceDocument.connector_id == row.id,
                SourceDocument.state.in_(("active", "updated")),
            )
        )
        or 0
    )
    catalog = CONNECTOR_CATALOG.get(str(row.kind) or "", {})
    last_sync = row.last_sync_at
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "status": row.status,
        "unavailable_reason": row.unavailable_reason,
        "enabled": bool(row.enabled),
        "config": redacted,
        "has_credentials": bool(has_credentials),
        "implemented": bool(catalog.get("implemented", False)),
        "requires_credentials": bool(catalog.get("requires_credentials", False)),
        "title": str(catalog.get("title") or row.kind),
        "blurb": str(catalog.get("blurb") or ""),
        "doc_count": int(live_docs),
        "last_sync_at": last_sync.isoformat() if isinstance(last_sync, datetime) else None,
        "last_error": row.last_error or "",
    }


__all__ = [
    "ALLOWED_TEXT_MIME",
    "AVAILABLE",
    "CONNECTOR_CATALOG",
    "ERROR",
    "JOB_TYPE",
    "MAX_ASSET_BYTES",
    "MAX_SOURCE_BYTES",
    "UNAVAILABLE",
    "SourceConfigError",
    "SourceConnector",
    "SourceDocumentDoc",
    "SourceError",
    "create_connector",
    "describe_connector",
    "enqueue_source_sync",
    "handle_source_sync",
    "register_source_jobs",
]
