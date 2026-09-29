"""Enterprise ops API (Work 11 Lane L) -- contracts §10.

    GET /workspaces/{ws}/ops/overview        admin
    GET /workspaces/{ws}/retention           admin
    PUT /workspaces/{ws}/retention           admin

**Overview is failure-isolated per section.** Every section is computed
in its own ``try/except``; a section that cannot be computed answers
``{"available": false, "reason": ...}`` and the rest of the overview
still renders. An ops dashboard that returns 500 because the storage
walk failed is useless precisely when it matters -- the point of the
page is to see that something is wrong. The reason string is a short,
type-only description (never an exception message), so nothing internal
leaks through it.

Sections:

  jobs            by status + recent failures (jobs table, this workspace)
  reviews         open reviews + approvals invalidated by a version bump
  exports         state histogram + failures
  storage         sum of MediaAsset.file_size, with a STORAGE_ROOT walk
                  as a fallback when the DB has no sizes recorded
  provider_health the 11 existing readiness probes (reused as-is; no
                  new health stack is introduced)
  costs           the existing cost summary fields
  audit           ledger rows in the last 7 days

Retention: GET returns the policy row (every NULL day count = keep
forever). PUT validates through ``services/retention.validate_days``
(non-negative ints, max 3650) -> 422 on anything else, and emits
``RETENTION_POLICY_UPDATED`` on success. The value of
``audit_retention_days`` is stored and reported, never enforced by
deletion -- the sweep reports audit rows as untouched.
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    CostEntry,
    ExportJob,
    Job,
    MediaAsset,
    Review,
    User,
    Workspace,
)
from app.models.base import utcnow
from app.services import activity as activity_service
from app.services import retention as retention_service
from app.services.auth_service import get_current_user, require_workspace_role

ops_router = APIRouter(prefix="/workspaces/{workspace_id}/ops", tags=["ops"])
retention_router = APIRouter(prefix="/workspaces/{workspace_id}/retention", tags=["retention"])
logger = logging.getLogger("ymoney.collab")

#: review states that still need somebody's attention.
_OPEN_REVIEW_STATES = ("DRAFT", "IN_REVIEW", "CHANGES_REQUESTED")


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
            logger.exception("ops route failed: %s", getattr(fn, "__name__", fn))
            raise HTTPException(status_code=500, detail="internal error") from None

    return run


def _unavailable(exc: BaseException) -> dict:
    """A failed section: available=False with a type-only reason."""
    logger.warning("ops section unavailable: %s", type(exc).__name__)
    return {"available": False, "reason": type(exc).__name__}


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def _jobs_section(db: Session, workspace_id: str) -> dict:
    by_status = {
        str(status or "UNKNOWN"): int(count or 0)
        for status, count in db.execute(
            select(Job.status, func.count())
            .where(Job.workspace_id == workspace_id)
            .group_by(Job.status)
        ).all()
    }
    since = utcnow() - timedelta(days=7)
    failed_recent = [
        {
            "id": row.id,
            "type": row.type,
            "status": row.status,
            "error": (row.last_error or "")[:200],
            "retry_count": row.retry_count,
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
        }
        for row in db.scalars(
            select(Job)
            .where(
                Job.workspace_id == workspace_id,
                Job.status.in_(("FAILED", "DEAD", "RETRYING")),
                Job.created_at >= since,
            )
            .order_by(Job.created_at.desc())
            .limit(20)
        ).all()
    ]
    return {
        "available": True,
        "by_status": by_status,
        "total": sum(by_status.values()),
        "failed_recent": failed_recent,
    }


def _reviews_section(db: Session, workspace_id: str) -> dict:
    open_reviews = int(
        db.scalar(
            select(func.count())
            .select_from(Review)
            .where(
                Review.workspace_id == workspace_id,
                Review.state.in_(_OPEN_REVIEW_STATES),
            )
        )
        or 0
    )
    stale_approvals = [
        {
            "id": row.id,
            "title": row.title,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "bound_version": row.bound_version,
            "stale_detected_at": (
                row.stale_detected_at.isoformat() + "Z" if row.stale_detected_at else ""
            ),
        }
        for row in db.scalars(
            select(Review)
            .where(
                Review.workspace_id == workspace_id,
                Review.state == "APPROVED",
                Review.stale.is_(True),
            )
            .order_by(Review.updated_at.desc())
            .limit(20)
        ).all()
    ]
    return {
        "available": True,
        "open": open_reviews,
        "stale_approvals": stale_approvals,
        "stale_approval_count": len(stale_approvals),
    }


def _exports_section(db: Session, workspace_id: str) -> dict:
    by_state = {
        str(state or "UNKNOWN"): int(count or 0)
        for state, count in db.execute(
            select(ExportJob.state, func.count())
            .where(ExportJob.workspace_id == workspace_id)
            .group_by(ExportJob.state)
        ).all()
    }
    failed = [
        {
            "id": row.id,
            "format": row.format,
            "state": row.state,
            "error": (row.error or "")[:200],
            "attempt": row.attempt,
            "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
        }
        for row in db.scalars(
            select(ExportJob)
            .where(ExportJob.workspace_id == workspace_id, ExportJob.state == "FAILED")
            .order_by(ExportJob.created_at.desc())
            .limit(20)
        ).all()
    ]
    return {
        "available": True,
        "by_state": by_state,
        "failed": failed,
        "failed_count": len(failed),
    }


def _storage_section(db: Session, workspace_id: str) -> dict:
    total_bytes, file_count = db.execute(
        select(
            func.coalesce(func.sum(MediaAsset.file_size), 0),
            func.count(MediaAsset.id),
        ).where(MediaAsset.workspace_id == workspace_id)
    ).one()
    payload: dict[str, Any] = {
        "available": True,
        "bytes": int(total_bytes or 0),
        "file_count": int(file_count or 0),
        "source": "database",
    }
    if not int(file_count or 0):
        # No asset rows with sizes: fall back to walking the workspace
        # storage directory so the section is never a misleading zero.
        with suppress(Exception):
            from app.services.storage import STORAGE_ROOT

            walked_bytes = 0
            walked_files = 0
            root = STORAGE_ROOT / workspace_id
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file():
                        walked_files += 1
                        walked_bytes += path.stat().st_size
            payload.update(
                {
                    "bytes": walked_bytes,
                    "file_count": walked_files,
                    "source": "filesystem",
                }
            )
    return payload


def _provider_health_section() -> dict:
    """Reuse the 11 readiness probes verbatim -- no new health stack."""
    from app.services import readiness

    report = readiness.run_readiness()
    checks = [
        {
            "id": check["id"],
            "status": check["status"],
            "blocking": check["blocking"],
            "latency_ms": check["latency_ms"],
        }
        for check in report.get("checks", [])
    ]
    return {
        "available": True,
        "status": report.get("status"),
        "checked_at": report.get("checked_at"),
        "blocking_failures": list(report.get("blocking_failures") or []),
        "checks": checks,
    }


def _costs_section(db: Session, workspace_id: str) -> dict:
    """The existing cost summary fields (same numbers as GET /costs)."""
    from app.core.config import settings

    since = utcnow() - timedelta(hours=24)
    by_category = {
        str(category or "unknown"): round(float(amount or 0.0), 4)
        for category, amount in db.execute(
            select(CostEntry.category, func.sum(CostEntry.amount_usd))
            .where(CostEntry.workspace_id == workspace_id, CostEntry.created_at >= since)
            .group_by(CostEntry.category)
        ).all()
    }
    spent = round(sum(by_category.values()), 4)
    return {
        "available": True,
        "last_24h_by_category": by_category,
        "spent_last_24h_usd": spent,
        "daily_budget_usd": settings.daily_budget_usd,
        "per_video_budget_usd": settings.per_video_budget_usd,
        "within_budget": (settings.daily_budget_usd - spent) > 0,
        "remaining_usd": round(settings.daily_budget_usd - spent, 4),
        "since": since.isoformat() + "Z",
    }


def _audit_section(db: Session, workspace_id: str) -> dict:
    since = utcnow() - timedelta(days=7)
    total = activity_service.count_since(db, workspace_id, since)
    return {
        "available": True,
        "events_last_7d": total,
        "since": since.isoformat() + "Z",
        "retention_enforced": False,
    }


def _retention_section(db: Session, workspace_id: str) -> dict:
    policy = retention_service.get_policy(db, workspace_id)
    return {"available": True, **retention_service.policy_dto(policy)}


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


@ops_router.get("/overview", summary="Enterprise ops overview (admin, failure-isolated)")
@_guard
def ops_overview(
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sections: dict[str, Callable[[], dict]] = {
        "jobs": lambda: _jobs_section(db, ws.id),
        "reviews": lambda: _reviews_section(db, ws.id),
        "exports": lambda: _exports_section(db, ws.id),
        "storage": lambda: _storage_section(db, ws.id),
        "provider_health": _provider_health_section,
        "costs": lambda: _costs_section(db, ws.id),
        "audit": lambda: _audit_section(db, ws.id),
        "retention": lambda: _retention_section(db, ws.id),
    }
    out: dict[str, Any] = {}
    for name, compute in sections.items():
        try:
            out[name] = compute()
        except Exception as exc:  # noqa: BLE001 -- one bad section must not 500
            out[name] = _unavailable(exc)
    out["workspace_id"] = ws.id
    out["generated_at"] = utcnow().isoformat() + "Z"
    return out


# ---------------------------------------------------------------------------
# retention policy
# ---------------------------------------------------------------------------


class RetentionBody(BaseModel):
    audit_retention_days: int | None = None
    render_retention_days: int | None = None
    temp_asset_retention_days: int | None = None
    export_retention_days: int | None = None


@retention_router.get("", summary="Retention policy (NULL days = keep forever)")
@_guard
def get_retention(
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    policy = retention_service.get_policy(db, ws.id)
    return retention_service.policy_dto(policy)


@retention_router.put("", summary="Update the retention policy (admin)")
@_guard
def put_retention(
    body: RetentionBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    values = body.model_dump(exclude_unset=True)
    try:
        policy = retention_service.upsert_policy(db, ws.id, values, by_user=user.id)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=" ".join(str(exc).split())[:180] or "invalid retention policy"
        ) from None
    db.commit()
    activity_service.emit(
        ws.id,
        "RETENTION_POLICY_UPDATED",
        message="Retention policy updated",
        actor=user.id,
        data={
            name: getattr(policy, name)
            for name in retention_service.POLICY_FIELDS
        },
    )
    return retention_service.policy_dto(policy)


# ---------------------------------------------------------------------------
# guarded job bootstrap (contracts §10) -- idempotent
# ---------------------------------------------------------------------------


def _bootstrap_retention_jobs() -> None:
    """Register the RETENTION_SWEEP handler idempotently.

    Guarded so a half-landed sibling can never break the API import;
    ``register_retention_jobs`` itself skips duplicates, so re-running
    this (module reload, direct test call) is safe.
    """
    with suppress(Exception):  # noqa: S110 - sibling lanes land in parallel
        module = importlib.import_module("app.services.retention")
        module.register_retention_jobs()


_bootstrap_retention_jobs()

__all__ = ["RetentionBody", "ops_router", "retention_router"]
