"""Work 15 §1 — ``TrendSignal``: observations with their evidence attached.

A signal records *what was seen, when, and what backs it*. It deliberately has
no "trend score" field, because a score implies a measurement we usually do
not have. The three things that are commonly faked, and what this module does
instead:

**Trend magnitude.**  A single observation has no magnitude. Velocity is only
computed when the SAME topic is observed at least twice, and it is a plain
count-delta between the two observations — not a growth-rate extrapolation.
Before that, ``velocity`` is ``None`` and callers must render "unknown".

**Freshness.**  Reuses the Work 10 knowledge bands (FRESH/AGING/STALE, 7/30
days) so a signal and a memory age by the same rule. A STALE signal is not
usable as fresh demand; the planner has to revalidate it first.

**Confidence.**  Measures how sure we are the thing was *seen*, not how much
demand it represents. An unverified signal is capped at 0.5, because
"someone said this exists" is weaker evidence than "the API returned it".

Dedupe: re-ingesting the same ``(source, topic, external_ref)`` updates the
existing row rather than creating a second one, so a source that re-syncs does
not manufacture recurrence out of nothing. Recurrence must come from
*distinct* observations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.planning import (
    AGING,
    FRESH,
    SIGNAL_ACTIVE,
    SIGNAL_STALE,
    SIGNAL_SUPERSEDED,
    STALE,
    TrendSignal,
)

__all__ = [
    "SIGNAL_SOURCES",
    "SignalIngest",
    "SignalRead",
    "claim_signals",
    "compute_velocity",
    "ingest_signal",
    "list_signals",
    "normalize_topic",
    "signal_freshness",
    "topic_recurrence",
]

#: Every ingestion path the planner accepts. A source outside this set is a
#: programming error, not a new "trend".
SIGNAL_SOURCES: tuple[str, ...] = (
    "research",        # research / browser intelligence (Work 05/12)
    "source",          # RSS + source connectors (Work 06)
    "community",       # community questions (Work 09)
    "request",         # explicit content requests
    "performance",     # measured performance change
    "platform",        # an official platform API signal
    "operator",        # a human typed it
)

#: Mirrors app.engine.knowledge.freshness so a signal and a memory age alike.
FRESH_DAYS = 7.0
AGING_DAYS = 30.0

#: Confidence ceiling for a signal with no verified evidence behind it.
UNVERIFIED_CONFIDENCE_CAP = 0.5

#: Recurrence only counts observations inside this window. Recurrence is a claim
#: about CURRENT demand, so two-year-old evidence cannot contribute to it.
RECURRENCE_WINDOW_DAYS = 30.0

_WORD_RE = re.compile(r"[a-z0-9]+")
#: Noise words that make two different topics look identical.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "how", "what", "why", "is", "are", "do", "does", "my", "your", "best",
})


def normalize_topic(topic: str) -> str:
    """Canonical topic key: lowercased, stop-worded, order-insensitive.

    Sorting the tokens makes "budget tips" and "tips budget" the same key, which
    is what lets recurrence and dedupe work across differently-worded
    observations.
    """
    tokens = [t for t in _WORD_RE.findall(str(topic or "").lower())
              if t not in _STOPWORDS]
    if not tokens:
        # a topic made only of stop words still needs a stable key
        return " ".join(sorted(_WORD_RE.findall(str(topic or "").lower())))
    return " ".join(sorted(tokens))


def signal_freshness(observed_at: datetime, *, now: datetime | None = None
                     ) -> str:
    """FRESH / AGING / STALE using the Work 10 thresholds.

    Both datetimes are normalised to UTC first: a value read back from SQLite
    arrives NAIVE, so comparing it directly against an aware ``now()`` raises
    instead of ageing. A naive timestamp is interpreted as UTC, which is what
    :func:`ingest_signal` stores.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if observed_at is None:
        return STALE
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    age = (now - observed_at).total_seconds() / 86400.0
    if age <= FRESH_DAYS:
        return FRESH
    if age <= AGING_DAYS:
        return AGING
    return STALE


@dataclass
class SignalIngest:
    """One observation to record.

    ``velocity`` is intentionally NOT a parameter: velocity is *computed* from
    repeated observations (:func:`compute_velocity`), never supplied by a
    caller. Accepting it would let a source assert a growth rate it never
    measured.
    """

    source: str
    topic: str
    external_ref: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    observed_at: datetime | None = None
    scope: str = "workspace"
    confidence: float = 0.5
    payload: dict = field(default_factory=dict)
    #: True only when the evidence ids were actually resolved/verified.
    evidence_verified: bool = False


@dataclass
class SignalRead:
    """A signal projected for the planner, with honesty about what is missing."""

    id: str
    source: str
    topic: str
    topic_key: str
    #: the source's own id. Part of a signal's identity, so consumers can tell
    #: a re-delivery from a genuinely new observation.
    external_ref: str
    observed_at: datetime
    freshness: str
    confidence: float
    status: str
    evidence_ids: list[str]
    velocity: dict | None
    recurrence: int
    scope: str

    @property
    def is_usable(self) -> bool:
        """Usable as fresh demand: active, not stale, and has evidence."""
        return (self.status == SIGNAL_ACTIVE
                and self.freshness != STALE
                and bool(self.evidence_ids))


def _clamp_confidence(value: float, *, verified: bool) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    score = max(0.0, min(1.0, score))
    if not verified:
        score = min(score, UNVERIFIED_CONFIDENCE_CAP)
    return round(score, 4)


#: Sources whose evidence YMONEY resolves itself, so "verified" is a fact
#: rather than an assertion. A caller may NOT self-certify these; verification
#: is derived from whether the evidence ids resolve.
_SELF_RESOLVING_SOURCES = frozenset({"research", "source", "platform"})

#: Sources that are inherently a human asserting something. These can never be
#: "verified evidence" no matter what the caller claims, because a person
#: typing a topic is an assertion, not a measurement.
_ASSERTION_SOURCES = frozenset({"operator", "request"})


def ingest_signal(db, workspace_id: str, payload: SignalIngest) -> dict:
    """Record one observation. Idempotent on ``(source, topic, external_ref)``.

    Returns ``{"id", "created": bool, "duplicate": bool, ...}``. A duplicate
    ingest refreshes ``observed_at``/``evidence`` on the EXISTING row and
    reports ``created=False`` -- which is what stops a re-syncing source from
    inflating recurrence.

    ``evidence_verified`` is NOT taken on trust. A caller could otherwise POST
    ``verified: true, confidence: 1.0`` and have their own claim treated as
    measured demand. It is honoured only for self-resolving sources (where
    YMONEY resolved the evidence itself) AND only when evidence ids were
    actually supplied; an operator's word is never verification, so operator and
    request signals are always capped.
    """
    source = str(payload.source or "").strip().lower()
    if source not in SIGNAL_SOURCES:
        raise ValueError(
            f"unknown signal source {payload.source!r}; "
            f"pick from {list(SIGNAL_SOURCES)}")
    topic = str(payload.topic or "").strip()
    if not topic:
        raise ValueError("a signal needs a topic")
    observed_at = payload.observed_at or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    topic_key = normalize_topic(topic)
    external_ref = str(payload.external_ref or "")
    evidence_ids = [str(e) for e in (payload.evidence_ids or []) if str(e).strip()]

    # Verification is DERIVED, never taken on trust. An assertion source
    # (operator, request) is a person stating something, so it is capped no
    # matter what the caller sends; a self-resolving source counts as verified
    # only when it actually supplied evidence ids.
    verified = bool(payload.evidence_verified and evidence_ids
                    and source in _SELF_RESOLVING_SOURCES
                    and source not in _ASSERTION_SOURCES)

    existing = db.scalar(
        select(TrendSignal).where(
            TrendSignal.workspace_id == workspace_id,
            TrendSignal.source == source,
            TrendSignal.topic_key == topic_key,
            TrendSignal.external_ref == external_ref))
    if existing is not None:
        # Same source item re-delivered: refresh, do not create. Recurrence
        # only grows from DISTINCT observations (a different external_ref).
        # A row read back from SQLite is NAIVE; the incoming value may be aware.
        # Normalise both before taking the max.
        previous = (existing.observed_at.replace(tzinfo=UTC)
                    if existing.observed_at.tzinfo is None
                    else existing.observed_at)
        existing.observed_at = max(previous, observed_at).replace(tzinfo=None)
        # A fresh re-delivery makes the signal FRESH again, so the stored band is
        # recomputed. Without this the row kept its old STALE band until a
        # persist=True read happened to heal it.
        existing.freshness = signal_freshness(observed_at)
        if evidence_ids:
            merged = list(dict.fromkeys([*existing.evidence_ids, *evidence_ids]))
            existing.evidence_ids_json = merged
        existing.confidence = max(
            existing.confidence,
            _clamp_confidence(payload.confidence,
                              verified=verified))
        existing.status = SIGNAL_ACTIVE
        db.flush()
        return {"id": existing.id, "created": False, "duplicate": True,
                "topic_key": topic_key,
                "freshness": existing.freshness}

    row = TrendSignal(
        workspace_id=workspace_id,
        source=source,
        topic=topic,
        topic_key=topic_key,
        external_ref=external_ref,
        evidence_ids_json=evidence_ids,
        # stored naive: the column is a naive DateTime, and a value read back
        # must compare cleanly against future rows
        observed_at=observed_at.replace(tzinfo=None),
        freshness=signal_freshness(observed_at),
        scope=str(payload.scope or "workspace"),
        velocity_json=None,          # only computed, never asserted
        confidence=_clamp_confidence(
            payload.confidence, verified=verified),
        status=SIGNAL_ACTIVE,
        payload_json=dict(payload.payload or {}),
    )
    db.add(row)
    db.flush()
    return {"id": row.id, "created": True, "duplicate": False,
            "topic_key": topic_key, "freshness": row.freshness}


def topic_recurrence(db, workspace_id: str, topic_key: str, *,
                     as_of: datetime | None = None,
                     within_days: float | None = RECURRENCE_WINDOW_DAYS
                     ) -> int:
    """How many DISTINCT active observations exist for a normalized topic.

    Counted across sources: the same demand seen by research AND a community
    request is recurrence, which is the strongest honest demand signal we have.

    ``within_days`` bounds how recent the evidence must be. Without a bound a
    two-year-old mention counted as today's recurrence, so the planner treated
    settled history as live demand. ``None`` disables the bound explicitly.
    """
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    query = select(TrendSignal).where(
        TrendSignal.workspace_id == workspace_id,
        TrendSignal.topic_key == topic_key,
        TrendSignal.status == SIGNAL_ACTIVE)
    if within_days is not None:
        cutoff = as_of - timedelta(days=float(within_days))
        query = query.where(TrendSignal.observed_at >= cutoff.replace(
            tzinfo=None))
    rows = db.scalars(query).all()
    # (source, external_ref) is a signal's identity; re-delivery of the same
    # item was already collapsed by ingest_signal, so the set size here is the
    # number of genuinely distinct observations.
    return len({(r.source, r.external_ref) for r in rows})


def compute_velocity(db, workspace_id: str, topic_key: str, *,
                     as_of: datetime | None = None) -> dict | None:
    """Velocity between two observations of the same topic, or ``None``.

    Returns ``None`` when fewer than two distinct observations exist, because a
    rate measured over one data point is a fabrication. Otherwise returns the
    observable count delta and the interval between the first and last
    observation -- stated as a count, never extrapolated to a forecast.
    """
    as_of = as_of or datetime.now(UTC)
    rows = db.scalars(
        select(TrendSignal).where(
            TrendSignal.workspace_id == workspace_id,
            TrendSignal.topic_key == topic_key,
            TrendSignal.status == SIGNAL_ACTIVE)
        .order_by(TrendSignal.observed_at.asc())).all()
    distinct: dict[tuple[str, str], datetime] = {}
    for row in rows:
        distinct.setdefault((row.source, row.external_ref), row.observed_at)
    if len(distinct) < 2:
        return None
    times = sorted(t.replace(tzinfo=UTC) if t.tzinfo is None else t
                 for t in distinct.values())
    span_days = (times[-1] - times[0]).total_seconds() / 86400.0
    if span_days <= 0:
        return None
    return {
        "observations": len(distinct),
        "sources": sorted({source for source, _ref in distinct}),
        "first_observed_at": times[0].isoformat(),
        "last_observed_at": times[-1].isoformat(),
        "span_days": round(span_days, 4),
        # an observed COUNT delta per day, explicitly not a forecast
        "observations_per_day": round((len(distinct) - 1) / span_days, 4),
        "kind": "observed_count_delta",
        "note": "count delta between two real observations; NOT a growth "
                "forecast and NOT a demand estimate",
        "as_of": as_of.isoformat(),
    }


def claim_signals(db, workspace_id: str, *, topic: str = "",
                  as_of: datetime | None = None,
                  persist: bool = True) -> list[SignalRead]:
    """Read the signals the planner may rely on, refreshing freshness.

    Recomputes ``velocity`` where two observations exist, and marks a signal
    STALE once its evidence has aged out so the planner is told to revalidate.

    ``persist=False`` makes the read non-mutating. A viewer-facing GET must use
    it: without it, merely LISTING signals rewrites ``freshness``/``status``/
    ``velocity`` and the route commits, so a read endpoint wrote to the
    database and made "RECOMMEND persists nothing" false at the signal layer.
    """
    as_of = as_of or datetime.now(UTC)
    query = select(TrendSignal).where(
        TrendSignal.workspace_id == workspace_id,
        TrendSignal.status != SIGNAL_SUPERSEDED)
    if topic:
        query = query.where(TrendSignal.topic_key == normalize_topic(topic))
    rows = db.scalars(query).all()
    out: list[SignalRead] = []
    for row in rows:
        band = signal_freshness(row.observed_at, now=as_of)
        # the STALE band is reported in the projection either way; persisting
        # it back onto the row is what a read must not do
        status = row.status
        if persist and band != row.freshness:
            row.freshness = band
        # A signal that aged out is no longer active demand. Marking it STALE
        # (rather than deleting) keeps the audit trail.
        if band == STALE and status == SIGNAL_ACTIVE:
            status = SIGNAL_STALE
            if persist:
                row.status = SIGNAL_STALE
        velocity = row.velocity_json
        if len(db.scalars(select(TrendSignal.id).where(
                TrendSignal.workspace_id == workspace_id,
                TrendSignal.topic_key == row.topic_key)).all()) >= 2:
            velocity = compute_velocity(db, workspace_id, row.topic_key,
                                        as_of=as_of)
            if persist:
                row.velocity_json = velocity
        out.append(SignalRead(
            id=row.id, source=row.source, topic=row.topic,
            topic_key=row.topic_key, external_ref=row.external_ref,
            observed_at=row.observed_at,
            freshness=band, confidence=float(row.confidence),
            status=status, evidence_ids=list(row.evidence_ids_json or []),
            velocity=velocity,
            recurrence=topic_recurrence(db, workspace_id, row.topic_key,
                                        as_of=as_of),
            scope=row.scope))
    if persist:
        db.flush()
    return out


def list_signals(db, workspace_id: str, *, status: str = "",
                 source: str = "", limit: int = 200) -> list[TrendSignal]:
    query = select(TrendSignal).where(TrendSignal.workspace_id == workspace_id)
    if status:
        query = query.where(TrendSignal.status == status)
    if source:
        query = query.where(TrendSignal.source == source)
    return list(db.scalars(
        query.order_by(TrendSignal.observed_at.desc()).limit(int(limit))).all())


def aging_window(*, days: float) -> tuple[datetime, datetime]:
    """(now, cutoff) for a freshness window — used by the planner's horizon."""
    now = datetime.now(UTC)
    return now, now - timedelta(days=days)
