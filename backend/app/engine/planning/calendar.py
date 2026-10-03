"""Work 15 §7 — editorial calendar, built ON the existing scheduler.

There is exactly one scheduler in this codebase: ``ScheduleEntry`` +
``app.engine.agents.scheduler``. This module does not create jobs, does not own
run times after they exist, and does not dispatch anything. It decides *when a
planned item should target* and then writes a ``ScheduleEntry`` through the
existing store, so the Scheduler agent picks it up exactly as it does for every
other entry.

Placement considers, in order:

    1. dependencies     -- a request may not be placed before what it waits on
    2. blackout dates   -- explicit dates and recurring weekdays
    3. platform caps    -- a DOCUMENTED per-day cap, or none
    4. content spacing  -- a minimum gap between same-platform posts
    5. deadline         -- a miss is BLOCKED with the cause, never a silent slip
    6. locale/timezone  -- the target is interpreted in the plan's locale

Two of those are honest no-ops today, and say so rather than pretending:

* **platform caps** -- no verified platform profile documents a creative per-day
  cadence (Threads' 250/day is an API rate limit), so the lookup returns
  ``None``. Spacing and capacity still bound the placement.
* **measured audience timing** -- :func:`measured_windows` returns ``[]``
  because the metric snapshots carry engagement totals with no time-of-day
  dimension. The optimizer therefore uses the platform's SEED window and labels
  it as a seed, because "statistically optimal posting time" from zero
  observations is a claim with no evidence behind it.

Both become real the moment the underlying data exists; neither is guessed at
until then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.models.content import ScheduleEntry

__all__ = [
    "CalendarPlacement",
    "CalendarRequest",
    "build_schedule_entries",
    "platform_daily_cap",
    "resolve_timezone",
    "score_slot",
]

#: Minimum gap between two posts on the same platform.
SPACING_HOURS = 20.0
#: Default gap between any two items on the same day, any platform.
DEFAULT_DAILY_GAP_HOURS = 4.0


def resolve_timezone(name: str) -> ZoneInfo:
    """Resolve a timezone, falling back to UTC on an unknown zone.

    An unknown zone must not silently become the *server's* local time, which
    would make scheduling non-reproducible across deployments.
    """
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


#: The Work 14 verified profile field that would carry a documented per-day
#: posting cap, if any platform documented one. No verified profile defines it
#: today, so every cap lookup returns ``None`` -- meaning "no documented cap",
#: which is NOT the same as a cap of zero and is never treated as one.
#:
#: Threads documents a rolling 24h quota (250 posts/day), which is a rate limit
#: on the API, not a creative cadence the planner should schedule around. So no
#: cap is claimed for it either.
_CAP_FIELD = "daily_post_cap"


def platform_daily_cap(platform: str) -> int | None:
    """Documented per-day post cap, or ``None`` when the platform documents none.

    Read from :mod:`app.engine.distribution.profiles` (Work 14), which stores
    every limit together with the URL that documents it. An absent field means
    the platform documents no cap, so the calendar keeps enforcing spacing and
    capacity rather than inventing a cadence.
    """
    from app.engine.distribution.profiles import get_profile

    try:
        profile = get_profile(platform)
    except Exception:
        return None
    cap = getattr(profile, _CAP_FIELD, None)
    if not isinstance(cap, (int, float)) or cap <= 0:
        return None
    return int(cap)


@dataclass
class CalendarRequest:
    """One thing to place."""

    plan_item_id: str
    content_id: str
    platform: str
    content_format: str = "SHORT"
    campaign_id: str = ""
    target_date: date | None = None
    deadline: date | None = None
    locale: str = "UTC"
    #: other plan items that must be placed first
    depends_on: list[str] = field(default_factory=list)
    priority: float = 0.0
    cost_usd: float = 0.0


@dataclass
class CalendarPlacement:
    request: CalendarRequest
    run_at: datetime
    reason: str
    #: True when the slot came from a measurement rather than a seed window.
    evidence_backed: bool = False
    blocked_reason: str = ""

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_reason)

    def to_dict(self) -> dict:
        return {"plan_item_id": self.request.plan_item_id,
                "platform": self.request.platform,
                "run_at": self.run_at.isoformat(),
                "reason": self.reason,
                "evidence_backed": self.evidence_backed,
                "blocked": self.is_blocked,
                "blocked_reason": self.blocked_reason}


def _seed_windows(platform: str) -> list[int]:
    """The campaign profile's seed hours for a platform.

    These are the pre-existing per-platform ``posting_windows`` seeds, NOT a
    measured optimum. Callers label them as seeds.
    """
    try:
        from app.engine.campaign.platforms import ACCOUNT_PLATFORM, get_profile

        profile = get_profile(ACCOUNT_PLATFORM.get(platform, platform))
        return [int(h) for h in (profile.get("posting_windows") or [])]
    except Exception:
        return [12, 18, 20]


def measured_windows(db, workspace_id: str, platform: str) -> list[int]:
    """Hours with measured engagement for this workspace, or ``[]``.

    Honesty note: the existing ``PostMetric`` snapshots carry engagement
    TOTALS, not a time-of-day breakdown, so there is no honest way to derive an
    "optimal hour" from them. Returning ``[]`` keeps the caller on the platform's
    seed window and labelled as a seed, which is the truthful position. This
    becomes measurable when metric snapshots gain a per-hour dimension — until
    then the answer is unknown, not inferred.

    The early return is deliberate, not an unfinished join: there is nothing to
    select that would change the answer, so the query is not run.
    """
    _ = (db, workspace_id, platform)
    return []


def _blackout(constraints: dict, day: date) -> str:
    """Return a blackout reason for ``day``, or an empty string."""
    for raw in (constraints.get("blackout_dates") or []):
        try:
            if date.fromisoformat(str(raw)) == day:
                return f"{day.isoformat()} is a configured blackout date"
        except ValueError:
            continue
    # recurring weekly blackouts, e.g. ["sat", "sun"]
    weekday = day.strftime("%a").lower()
    for raw in (constraints.get("blackout_weekdays") or []):
        if str(raw).strip().lower()[:3] == weekday:
            return f"{day.strftime('%A')} is a configured blackout weekday"
    return ""


def _same_day_load(db, workspace_id: str, day: date, tz: ZoneInfo) -> list[ScheduleEntry]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    rows = db.scalars(
        select(ScheduleEntry).where(
            ScheduleEntry.workspace_id == workspace_id,
            ScheduleEntry.run_at >= start.replace(tzinfo=None),
            ScheduleEntry.run_at < end.replace(tzinfo=None),
            ScheduleEntry.status != "CANCELLED")).all()
    return list(rows)


def score_slot(existing: list[ScheduleEntry], candidate: datetime,
               *, platform: str, spacing_hours: float = SPACING_HOURS) -> float:
    """Lower is better. Penalises same-platform crowding and same-day stacking.

    Deterministic: the same inputs always produce the same score, which is what
    makes a plan reproducible from stored inputs alone.

    The distance term matters as much as the penalty, and its SIGN matters. With
    a 20h minimum gap, ANY two same-platform posts on the same day violate it, so
    a flat binary penalty leaves every hour on that day tied -- and a first-wins
    tie-break would then put the new post exactly on top of the existing one.
    The distance is therefore SUBTRACTED, bounded by the penalty, so a slot
    further from existing posts is strictly better but can never outrank a
    genuine conflict with a different day.
    """
    score = 0.0
    candidate = candidate.replace(tzinfo=None)
    for entry in existing:
        other = entry.run_at
        if other.tzinfo is not None:
            other = other.replace(tzinfo=None)
        gap_hours = abs((candidate - other).total_seconds()) / 3600.0
        if entry.platform == platform:
            penalty = 100.0 if gap_hours < spacing_hours else 5.0
        else:
            penalty = 3.0 if gap_hours < DEFAULT_DAILY_GAP_HOURS else 0.5
        # never negative, and never a reward: the term only breaks ties within
        # the same penalty class, favouring the emptiest slot
        score += max(0.0, penalty - min(4.0, gap_hours / 6.0))
    return score


def _dependency_after(db, workspace_id: str,
                     request: CalendarRequest) -> str:
    """A reason string when a dependency has not been placed yet, else ``""``.

    A dependency is satisfied only when its own plan item already has a
    ``ScheduleEntry`` in this workspace. An unsatisfied dependency is a hard
    block: the dependent work cannot be placed, and saying so is more useful
    than silently placing it early.
    """
    from app.models.planning import EditorialPlanItem

    wanted = {str(d) for d in request.depends_on if str(d).strip()}
    if not wanted:
        return ""
    placed: set[str] = set()
    for item in db.scalars(select(EditorialPlanItem).where(
            EditorialPlanItem.workspace_id == workspace_id)).all():
        if item.schedule_entry_id:
            placed.add(item.id)
    missing = sorted(wanted - placed)
    if not missing:
        return ""
    return (f"{len(missing)} unmet dependency(ies): {', '.join(missing[:5])}"
            + ("..." if len(missing) > 5 else ""))


def place_request(db, workspace_id: str, request: CalendarRequest, *,
                  constraints: dict | None = None,
                  horizon_days: int = 30) -> CalendarPlacement:
    """Place one request on the calendar, respecting every hard constraint.

    Walk-forward from the target date inside the horizon. A deadline that cannot
    be met is a ``BLOCKED`` placement with the reason, never a silent slip past
    it.
    """
    constraints = constraints or {}
    tz = resolve_timezone(request.locale)
    # Today in the WORKSPACE's zone, not the server's. `date.today()` is the
    # host's local date, so a workspace in Auckland could be planned a day
    # behind (or ahead) of its own calendar.
    start_day = request.target_date or datetime.now(tz).date()

    # Dependencies are a HARD constraint, so they are resolved before the
    # walk-forward: a request may not land before the things it depends on.
    # Previously ``depends_on`` was accepted and ignored, so a part 2 could be
    # scheduled ahead of its part 1.
    #
    # The check is TRUTHINESS, not `is not None`: _dependency_after returns ""
    # when every dependency is satisfied, and `"" is not None` is True -- which
    # sent a satisfied request straight past the optimizer into a hardcoded
    # noon slot, ignoring blackouts, caps, spacing and the deadline.
    blocker = _dependency_after(db, workspace_id, request) \
        if request.depends_on else ""
    if blocker:
        return CalendarPlacement(
            request=request,
            run_at=datetime.combine(start_day, time(hour=12), tzinfo=tz),
            reason="blocked by an unmet dependency",
            evidence_backed=False,
            blocked_reason=blocker)

    seed_hours = _seed_windows(request.platform)
    measured = measured_windows(db, workspace_id, request.platform)
    hours = measured or seed_hours
    evidence_backed = bool(measured)

    cap = platform_daily_cap(request.platform)
    days_tried = 0
    for offset in range(0, max(1, int(horizon_days))):
        day = start_day + timedelta(days=offset)
        days_tried += 1
        blacked = _blackout(constraints, day)
        if blacked:
            continue
        existing = _same_day_load(db, workspace_id, day, tz)
        if cap is not None and len(existing) >= cap:
            continue
        best: tuple[float, datetime] | None = None
        for hour in sorted(hours):
            candidate = datetime.combine(day, time(hour=hour % 24), tzinfo=tz)
            score = score_slot(existing, candidate, platform=request.platform)
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is None:
            continue
        run_at = best[1]
        if request.deadline is not None and run_at.date() > request.deadline:
            # Name the CAUSE, not just the dates: the operator needs to know
            # whether a blackout, a cap or spacing pushed the work late, because
            # each has a different fix.
            blockers = [b for b in (_blackout(constraints,
                                              start_day + timedelta(days=offset))
                                    for offset in range(
                                        0, (run_at.date() - start_day).days + 1))
                        if b]
            cause = ("; ".join(dict.fromkeys(blockers))
                     if blockers else "platform cap or spacing on every earlier day")
            return CalendarPlacement(
                request=request, run_at=run_at, reason="no slot before deadline",
                evidence_backed=evidence_backed,
                blocked_reason=(
                    f"earliest free slot {run_at.date().isoformat()} is after the "
                    f"deadline {request.deadline.isoformat()}: {cause}"))
        return CalendarPlacement(
            request=request, run_at=run_at,
            reason=("measured engagement window" if evidence_backed
                    else "platform seed window (no measured timing data)"),
            evidence_backed=evidence_backed)

    return CalendarPlacement(
        request=request,
        run_at=datetime.combine(start_day, time(hour=12), tzinfo=tz),
        reason="no feasible slot in the horizon",
        evidence_backed=evidence_backed,
        blocked_reason=(f"no slot in {days_tried} day(s) after "
                        f"{start_day.isoformat()}: blackout, cap or spacing"))


def build_schedule_entries(db, workspace_id: str, plan_id: str,
                           placements: list[CalendarPlacement], *,
                           create: bool = True) -> list[dict]:
    """Persist placements through the EXISTING ScheduleEntry store.

    Idempotent per ``(plan_item, platform)``: a re-planned item reuses its entry
    and moves the time rather than creating a second schedule row. Nothing here
    dispatches a job — the Scheduler agent owns that, unchanged.

    ``run_at`` is stored in UTC because the canonical Scheduler reads that naive
    column as UTC. The placement carries a zone-aware local time, so writing it
    verbatim made a 12:00 JST slot fire at 21:00 JST.
    """
    _ = plan_id
    out: list[dict] = []
    for placement in placements:
        if placement.is_blocked:
            out.append({"plan_item_id": placement.request.plan_item_id,
                        "status": "BLOCKED",
                        "reason": placement.blocked_reason, "entry_id": ""})
            continue
        request = placement.request
        # Store UTC, not the workspace's local wall clock. ``run_at`` is a naive
        # DateTime that agents/scheduler.py reads as UTC, so writing 12:00
        # America/New_York made the job fire at 08:00 local -- nine hours off.
        run_at = placement.run_at.astimezone(UTC).replace(tzinfo=None)
        # Identity: the plan item, when there is one. campaign_id and
        # content_item_id are BOTH null for an item that has produced nothing
        # yet, so keying on them alone collapsed every such item on a platform
        # onto one row -- and re-planning one moved the other's time.
        wanted_campaign = request.campaign_id or None
        wanted_content = request.content_id or None
        identity = (
            ScheduleEntry.plan_item_id == request.plan_item_id
            if request.plan_item_id
            else (ScheduleEntry.campaign_id.is_(wanted_campaign)
                  if wanted_campaign is None
                  else ScheduleEntry.campaign_id == wanted_campaign))
        existing = db.scalar(select(ScheduleEntry).where(
            ScheduleEntry.workspace_id == workspace_id,
            identity,
            ScheduleEntry.platform == request.platform,
            ScheduleEntry.content_item_id.is_(wanted_content)
            if wanted_content is None
            else ScheduleEntry.content_item_id == wanted_content))
        if existing is not None and existing.status == "DONE":
            # A published entry is history. The guard comes BEFORE any write:
            # rewinding run_at first and then `continue` silently moved a
            # DONE entry's time. It is still REPORTED, so the caller can see
            # why nothing was scheduled rather than getting a short list.
            out.append({"plan_item_id": request.plan_item_id,
                        "entry_id": existing.id, "created": False,
                        "status": existing.status,
                        "reason": "already published; the entry is history",
                        "skipped": True, "run_at": run_at.isoformat(),
                        "evidence_backed": placement.evidence_backed})
            continue
        if not create:
            # Dry run: report the decision, touch nothing. Previously the entry
            # was already db.add()ed by the time this check ran, so a query
            # autoflush persisted it anyway.
            out.append({"plan_item_id": request.plan_item_id, "entry_id": "",
                        "status": "WOULD_CREATE" if existing is None
                        else existing.status,
                        "created": existing is None,
                        "run_at": run_at.isoformat(), "reason": placement.reason,
                        "evidence_backed": placement.evidence_backed,
                        "dry_run": True})
            continue
        if existing is not None:
            existing.run_at = run_at
            entry = existing
            created = False
        else:
            entry = ScheduleEntry(
                workspace_id=workspace_id,
                content_item_id=request.content_id or None,
                campaign_id=request.campaign_id or None,
                plan_item_id=request.plan_item_id or None,
                platform=request.platform, run_at=run_at, status="PENDING")
            db.add(entry)
            created = True
        db.flush()
        out.append({"plan_item_id": request.plan_item_id, "entry_id": entry.id,
                    "status": entry.status, "created": created,
                    "run_at": run_at.isoformat(), "reason": placement.reason,
                    "evidence_backed": placement.evidence_backed})
    if create:
        db.flush()
    return out
