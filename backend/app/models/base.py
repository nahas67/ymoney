"""Shared model mixins and enums."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class PKMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)


# ---------------------------------------------------------------------------
# Enums (string-valued so DB rows stay readable and migrations stay simple)
# ---------------------------------------------------------------------------


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"  # waiting on dependency or scheduled time
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RETRYING = "RETRYING"
    DEAD = "DEAD"


class AutopilotState(str, enum.Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class CycleStage(str, enum.Enum):
    FIND = "FIND"
    SCORE = "SCORE"
    SELECT = "SELECT"
    RESEARCH = "RESEARCH"
    BUILD = "BUILD"
    VERIFY = "VERIFY"
    UPLOAD = "UPLOAD"
    MEASURE = "MEASURE"
    LEARN = "LEARN"


class ContentStatus(str, enum.Enum):
    IDEA = "IDEA"
    RESEARCHING = "RESEARCHING"
    STRATEGY = "STRATEGY"
    SCRIPTING = "SCRIPTING"
    SCRIPT_READY = "SCRIPT_READY"
    PRODUCTION = "PRODUCTION"
    QC = "QC"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    ANALYZING = "ANALYZING"
    LEARNED = "LEARNED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


CONTENT_TRANSITIONS: dict[str, set[str]] = {
    ContentStatus.IDEA.value: {
        ContentStatus.RESEARCHING.value,
        ContentStatus.SKIPPED.value,
        ContentStatus.FAILED.value,
    },
    ContentStatus.RESEARCHING.value: {
        ContentStatus.STRATEGY.value,
        ContentStatus.FAILED.value,
    },
    ContentStatus.STRATEGY.value: {ContentStatus.SCRIPTING.value, ContentStatus.FAILED.value},
    ContentStatus.SCRIPTING.value: {
        ContentStatus.SCRIPT_READY.value,
        ContentStatus.FAILED.value,
    },
    ContentStatus.SCRIPT_READY.value: {
        ContentStatus.PRODUCTION.value,
        ContentStatus.SKIPPED.value,
        ContentStatus.FAILED.value,
    },
    ContentStatus.PRODUCTION.value: {
        ContentStatus.QC.value,
        ContentStatus.SCRIPTING.value,  # build retry: re-script from PRODUCTION
        ContentStatus.FAILED.value,
    },
    ContentStatus.QC.value: {
        ContentStatus.APPROVED.value,
        ContentStatus.SCRIPT_READY.value,  # rejected -> regenerate
        ContentStatus.FAILED.value,
    },
    ContentStatus.APPROVED.value: {
        ContentStatus.SCHEDULED.value,
        ContentStatus.PUBLISHED.value,
        ContentStatus.FAILED.value,
    },
    ContentStatus.SCHEDULED.value: {ContentStatus.PUBLISHED.value, ContentStatus.FAILED.value},
    ContentStatus.PUBLISHED.value: {ContentStatus.ANALYZING.value},
    ContentStatus.ANALYZING.value: {ContentStatus.LEARNED.value},
    ContentStatus.LEARNED.value: set(),
    ContentStatus.SKIPPED.value: set(),
    ContentStatus.FAILED.value: {
        ContentStatus.RESEARCHING.value,
        ContentStatus.SCRIPT_READY.value,
        ContentStatus.PRODUCTION.value,
        ContentStatus.APPROVED.value,
    },  # explicit retry transitions
}


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True  # idempotent re-entry (e.g., job retries) is always safe
    return target in CONTENT_TRANSITIONS.get(current, set())
