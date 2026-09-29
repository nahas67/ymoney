"""Freshness bands + effective status for GlobalMemory (Work 10).

A memory's age is measured from ``last_verified_at`` when set (verification
resets the clock) else ``created_at``::

    FRESH   age < 7 days
    AGING   age < 30 days
    STALE   age >= 30 days

The lifecycle ``status`` column (ACTIVE | CONFLICTED | SUPERSEDED |
UNVERIFIED | DISABLED) overrides the band whenever it is not ACTIVE, so the
Work 10 states resolve as: FRESH / AGING / STALE (the band of an ACTIVE
row) plus CONFLICTED / SUPERSEDED / UNVERIFIED / DISABLED (lifecycle).

Pure functions over row-like objects — no DB access, no writes. Services
recompute on read (and persist the band when it changed); nothing here
touches the session.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models.base import utcnow

FRESH_DAYS = 7.0
AGING_DAYS = 30.0

FRESH = "FRESH"
AGING = "AGING"
STALE = "STALE"

# lifecycle values (the status column)
ACTIVE = "ACTIVE"
CONFLICTED = "CONFLICTED"
SUPERSEDED = "SUPERSEDED"
UNVERIFIED = "UNVERIFIED"
DISABLED = "DISABLED"

LIFECYCLE_STATUSES: tuple[str, ...] = (
    ACTIVE, CONFLICTED, SUPERSEDED, UNVERIFIED, DISABLED,
)
BANDS: tuple[str, ...] = (FRESH, AGING, STALE)
# every value effective_status() may return
ALL_STATUSES: tuple[str, ...] = LIFECYCLE_STATUSES + BANDS

# status precedence at store time (memory.py): CONFLICTED beats UNVERIFIED
# beats ACTIVE — a conflicting fact is actionable as a conflict first.
STATUS_PRECEDENCE: tuple[str, ...] = (CONFLICTED, UNVERIFIED, ACTIVE)


def _anchor(row: Any) -> datetime | None:
    """Clock anchor: last_verified_at (verification resets age), else created_at."""
    verified = getattr(row, "last_verified_at", None)
    if verified is not None:
        return verified
    return getattr(row, "created_at", None)


def age_days(row: Any, now: datetime | None = None) -> float:
    """Whole-day age of the memory (never negative; 0.0 when no clock anchor)."""
    anchor = _anchor(row)
    if anchor is None:
        return 0.0
    current = now or utcnow()
    return max(0.0, (current - anchor).total_seconds() / 86400.0)


def freshness_band(row: Any, now: datetime | None = None) -> str:
    """FRESH | AGING | STALE from the row's age."""
    age = age_days(row, now)
    if age < FRESH_DAYS:
        return FRESH
    if age < AGING_DAYS:
        return AGING
    return STALE


def effective_status(row: Any, now: datetime | None = None) -> str:
    """Lifecycle status if set, else the freshness band (six states + DISABLED).

    * status != ACTIVE → that status (CONFLICTED / SUPERSEDED / UNVERIFIED /
      DISABLED — lifecycle wins over age).
    * status == ACTIVE → the computed band (FRESH / AGING / STALE).
    """
    status = str(getattr(row, "status", "") or ACTIVE)
    if status != ACTIVE:
        return status
    return freshness_band(row, now)


__all__ = [
    "ACTIVE",
    "AGING",
    "ALL_STATUSES",
    "BANDS",
    "CONFLICTED",
    "DISABLED",
    "FRESH",
    "FRESH_DAYS",
    "AGING_DAYS",
    "LIFECYCLE_STATUSES",
    "STATUS_PRECEDENCE",
    "STALE",
    "SUPERSEDED",
    "UNVERIFIED",
    "age_days",
    "effective_status",
    "freshness_band",
]
