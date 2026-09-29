"""Activity ledger API (Work 11 Lane L) -- contracts §9.

    GET /workspaces/{workspace_id}/activity

**Read-only, by construction.** This router declares exactly ONE route
and it is a GET. The ledger is append-only (models/ops.py::EventLog) and
no mutator exists anywhere in ``api/`` for events -- ``tests/
test_activity_ledger.py`` asserts that every mutating verb on this path
returns 404/405, so the property is enforced by the test suite rather
than by a comment.

The pre-existing ``misc.activity_router`` (``/activity/recent``,
``/activity/stream``) is untouched; this router adds the filterable
ledger view alongside it.

Filters: ``kind``, ``project_id``, ``target_type``, ``target_id``,
``since`` (ISO-8601), ``limit``. Every filter is ANDed with the
workspace scope, so a foreign project/target id can only narrow the
result to nothing -- it can never surface another workspace's rows.

Error policy: bad ``since`` -> 422 (enum-ish validation, knowledge.py
policy); otherwise a generic 500 that logs on ``ymoney.collab`` and
echoes nothing back.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, Workspace
from app.services import activity as activity_service
from app.services.auth_service import get_current_user, require_workspace_role

activity_ledger_router = APIRouter(
    prefix="/workspaces/{workspace_id}/activity", tags=["activity"]
)
logger = logging.getLogger("ymoney.collab")


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Generic 500 on unexpected faults; HTTPException passes through."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
            logger.exception("activity route failed: %s", getattr(fn, "__name__", fn))
            raise HTTPException(status_code=500, detail="internal error") from None

    return run


@activity_ledger_router.get(
    "", summary="Activity ledger (read-only, newest first, workspace-scoped)"
)
@_guard
def list_activity(
    kind: str | None = Query(default=None, max_length=60),
    project_id: str | None = Query(default=None, max_length=36),
    target_type: str | None = Query(default=None, max_length=32),
    target_id: str | None = Query(default=None, max_length=36),
    since: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=activity_service.DEFAULT_LIMIT, ge=1,
                       le=activity_service.MAX_LIMIT),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        since_dt = activity_service.parse_since(since)
    except ValueError:
        raise HTTPException(status_code=422, detail="since must be an ISO-8601 timestamp") from None
    items = activity_service.query(
        db,
        ws.id,
        kind=kind,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        since=since_dt,
        limit=limit,
    )
    return {"items": items, "count": len(items), "limit": limit}


__all__ = ["activity_ledger_router"]
