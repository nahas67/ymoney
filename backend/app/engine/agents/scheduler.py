"""Scheduler agent (E6 brains): best-time calendar auto-fill.

Fills the publishing calendar for the coming days from render-ready APPROVED
content: best measured hours first, platform caps respected, and never double
scheduling the same content+platform. Idempotent — re-running changes nothing.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.models import ContentItem, PostMetric, PublishedPost, ScheduleEntry, Video, VideoVariant
from app.models.base import utcnow


class SchedulerAgent(BaseAgent):
    meta = AgentMeta(
        key="scheduler",
        title="Scheduler",
        description="Auto-fills the calendar at best measured hours within caps.",
        skills=("schedule_planning",),
        tools=("plan_schedule",),
        permissions=("schedule:write",),
    )

    def plan(self, ctx, *, days: int = 7, platforms: list[str] | None = None) -> dict:
        def work():
            from app.engine.decision import get_safety_settings
            from app.models import Workspace

            ws = ctx.workspace_id or ""
            days_n = max(1, min(int(days or 7), 30))
            self.step("load_candidates", "APPROVED + render-ready, unscheduled content")
            with session_scope() as s:
                ws_row = s.get(Workspace, ws) if ws else None
                safety = get_safety_settings((ws_row.settings_json or {}) if ws_row else {})
                cap_day = int(safety.get("max_videos_per_day", 10))
                rows = s.execute(
                    select(ContentItem, Video)
                    .join(VideoVariant, VideoVariant.content_item_id == ContentItem.id)
                    .join(Video, Video.variant_id == VideoVariant.id)
                    .where(ContentItem.workspace_id == ws,
                           ContentItem.status == "APPROVED",
                           VideoVariant.selected.is_(True),
                           Video.status == "READY")
                    .order_by(ContentItem.created_at.asc()).limit(200)
                ).all()
                existing = {(e.content_item_id, e.platform) for e in s.scalars(
                    select(ScheduleEntry).where(
                        ScheduleEntry.workspace_id == ws,
                        ScheduleEntry.status.in_(["PENDING", "DISPATCHING", "QUEUED"]))
                ).all()}
                per_day = {str(k)[:10]: v for k, v in s.execute(
                    select(func.date(ScheduleEntry.run_at), func.count())
                    .where(ScheduleEntry.workspace_id == ws,
                           ScheduleEntry.status.in_(["PENDING", "DISPATCHING", "QUEUED"]))
                    .group_by(func.date(ScheduleEntry.run_at))
                ).all()}
                hours = self._best_hours(s, ws)
            plats = [p for p in (platforms or ["youtube", "tiktok"]) if p]
            if not plats:
                raise ValueError("no platforms to schedule")
            self.step_done("ok", f"{len(rows)} candidate(s), hours {hours[:3]}")
            # Performance lessons (Work 06 Lane C): stable re-rank of the
            # measured hours when `learning_assist` is enabled (default off).
            # Daily caps and idempotency below stay authoritative.
            lesson_keys: list[str] = []
            lesson_recs: list[dict] = []
            try:
                from app.engine.performance import learning as _lessons

                hours, lesson_keys, lesson_recs = _lessons.schedule_hour_prior(ws, hours, plats)
            except Exception:
                pass
            self.step("fill_calendar", f"{days_n} day(s) × {', '.join(plats)}")
            created = []
            with session_scope() as s:
                midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                first_hour = hours[0] if hours else 18
                start_day = midnight if utcnow().hour < first_hour else midnight + timedelta(days=1)
                queue = [(c.id, c.topic) for c, _v in rows]
                qi = 0
                for d in range(days_n):
                    day = start_day + timedelta(days=d)
                    day_key = day.date().isoformat()
                    used = int(per_day.get(day_key, 0) or 0)
                    for platform in plats:
                        if used >= cap_day or qi >= len(queue):
                            break
                        # find next unscheduled content for this platform
                        while qi < len(queue) and (queue[qi][0], platform) in existing:
                            qi += 1
                        if qi >= len(queue):
                            break
                        cid, _topic = queue[qi]
                        qi += 1
                        hour = hours[(d * len(plats) + plats.index(platform)) % len(hours)] if hours else 18
                        entry = ScheduleEntry(
                            workspace_id=ws, content_item_id=cid, platform=platform,
                            run_at=day.replace(hour=hour, minute=0), status="PENDING")
                        s.add(entry)
                        s.flush()
                        existing.add((cid, platform))
                        used += 1
                        created.append({"entry_id": entry.id, "platform": platform,
                                        "run_at": entry.run_at.isoformat() + "Z"})
                s.flush()
            self.step_done("ok", f"{len(created)} entr(ies)")
            self.track_cost(ctx, "schedule", 0.0, provider="planner")
            out = {"summary": f"scheduled {len(created)} publish(es) over {days_n} day(s)",
                   "created": created}
            if lesson_keys or lesson_recs:
                out["applied_lessons"] = lesson_keys
                out["lesson_recommendations"] = lesson_recs
            return out

        return self.execute(ctx, "plan_schedule", input_summary=f"{days} day(s)", fn=work)

    @staticmethod
    def _best_hours(s, workspace_id: str) -> list[int]:
        """Top measured publish hours, generic evenings as fallback."""
        posts = s.scalars(
            select(PublishedPost).where(PublishedPost.workspace_id == workspace_id)).all()
        if not posts:
            return [18, 12, 20]
        metrics = {}
        for m in s.scalars(select(PostMetric).where(
                PostMetric.post_id.in_([p.id for p in posts])).order_by(PostMetric.captured_at.asc())):
            metrics[m.post_id] = m
        buckets: dict[int, list[int]] = {}
        for p in posts:
            m = metrics.get(p.id)
            if m and p.published_at:
                buckets.setdefault(p.published_at.hour, []).append(m.views)
        ranked = sorted(((h, sum(v) / len(v)) for h, v in buckets.items() if v),
                        key=lambda t: -t[1])
        hours = [h for h, _ in ranked[:5]]
        for fallback in (18, 12, 20):
            if fallback not in hours:
                hours.append(fallback)
        return hours[:5]

    # ------------------------------------------------------------------
    # Work 10 Lane E — response-window recommendations (read-only)
    # ------------------------------------------------------------------

    #: metric-backed posts required before a ranking may claim "measured"
    MIN_MEASURED_POSTS = 3
    #: SocialInteraction rows required before audience activity is usable
    MIN_ACTIVITY_INTERACTIONS = 10
    #: how many audience-activity hours / recommended windows are reported
    TOP_ACTIVITY_HOURS = 5
    MAX_WINDOW_ITEMS = 5
    #: ScheduleEntry statuses that count as "still scheduled" (same set plan() uses)
    ACTIVE_ENTRY_STATUSES = ("PENDING", "DISPATCHING", "QUEUED")

    @staticmethod
    def _measured_hour_facts(
        s, workspace_id: str, platform: str | None = None
    ) -> tuple[set[int], int]:
        """Hours of metric-backed published posts + how many such posts exist.

        Counting and provenance labeling ONLY — the ranking itself lives in
        :meth:`_best_hours` and is never re-derived here, so a recommendation
        can never invent best-time data. "Measured" matches ``_best_hours``:
        a post counts when it has at least one ``PostMetric`` row and a
        ``published_at``. Query is hard-filtered by workspace (and platform
        when given).
        """
        stmt = (
            select(PublishedPost.id, PublishedPost.published_at)
            .join(PostMetric, PostMetric.post_id == PublishedPost.id)
            .where(
                PublishedPost.workspace_id == workspace_id,
                PublishedPost.published_at.isnot(None),
            )
        )
        if platform:
            stmt = stmt.where(PublishedPost.platform == platform)
        seen = {}
        for post_id, published_at in s.execute(stmt).all():
            seen[post_id] = published_at
        return {published_at.hour for published_at in seen.values()}, len(seen)

    @staticmethod
    def recommend_response_windows(workspace_id: str, *, platform: str | None = None) -> dict:
        """Rank the hours worth aiming at — RECOMMENDATION ONLY by default.

        Reads live workspace state and nothing else (no network, no new deps):

        * ranking  — reuses :meth:`_best_hours`; the measured computation is
          never duplicated here, so best-time data cannot be invented.
        * measured — ``True`` only with >= ``MIN_MEASURED_POSTS`` metric-backed
          published posts (platform-filtered when ``platform`` is given);
          otherwise every hour is honestly labeled heuristic, never measured.
        * audience — ``SocialInteraction.created_at`` hour buckets (UTC), used
          only from >= ``MIN_ACTIVITY_INTERACTIONS`` rows.
        * caps     — live autonomy config + ``get_safety_settings`` upload cap.
        * avoid    — hours already holding active campaign ``ScheduleEntry``
          rows; items prefer hours not in that set.

        ``activity_policy`` becomes ``action_allowed`` ONLY on the explicit
        ``settings_json["schedule_automation"] is True`` opt-in (default off).
        """
        from app.engine.community.autonomy import load_autonomy
        from app.engine.decision import get_safety_settings
        from app.models import Workspace
        from app.models.community import SocialInteraction

        ws = str(workspace_id or "").strip()
        if not ws:
            raise ValueError("recommend_response_windows requires a workspace")
        token = (platform or "").strip().lower() or None
        notes: list[str] = []

        with session_scope() as s:
            ws_row = s.get(Workspace, ws)
            if ws_row is None:
                # honest zeroed answer; mirrors plan() tolerating a missing row
                cfg = load_autonomy(None)
                return {
                    "items": [],
                    "measured": False,
                    "activity": False,
                    "audience_activity": {"available": False, "hours": []},
                    "caps": {
                        "daily_cap": cfg.daily_cap,
                        "rate_per_10min": cfg.rate_per_10min,
                        "cooldown_seconds": cfg.cooldown_seconds,
                    },
                    "avoid_hours": [],
                    "activity_policy": "recommendation_only",
                    "notes": [f"workspace not found: {ws}"],
                }

            settings_json = dict(ws_row.settings_json or {})
            autonomy = load_autonomy(ws_row)
            safety = get_safety_settings(settings_json)
            caps: dict = {
                "daily_cap": int(autonomy.daily_cap),
                "rate_per_10min": int(autonomy.rate_per_10min),
                "cooldown_seconds": int(autonomy.cooldown_seconds),
            }
            uploads = safety.get("max_uploads_per_hour")
            if uploads is None:
                notes.append("max_uploads_per_hour not configured; cap omitted")
            else:
                caps["max_uploads_per_hour"] = int(uploads)

            # -- measured ranking: CALL _best_hours, never re-derive it ------
            hours_ranked = [int(h) for h in SchedulerAgent._best_hours(s, ws)]
            measured_hours, measured_count = SchedulerAgent._measured_hour_facts(s, ws, token)
            measured = measured_count >= SchedulerAgent.MIN_MEASURED_POSTS
            if not measured:
                notes.append(
                    f"heuristic fallback: {measured_count} measured post(s) < "
                    f"{SchedulerAgent.MIN_MEASURED_POSTS}; seed hours are labeled "
                    "platform_seed, never measured"
                )
            if token:
                notes.append(
                    "hour ranking is workspace-wide (_best_hours has no platform "
                    f"split); measured post count filtered to platform={token}"
                )

            # -- audience activity: hour-of-day buckets of created_at (UTC) --
            buckets: dict[int, int] = {}
            total = 0
            stmt = select(SocialInteraction.created_at, SocialInteraction.platform).where(
                SocialInteraction.workspace_id == ws
            )
            if token:
                stmt = stmt.where(SocialInteraction.platform == token)
            for created_at, _row_platform in s.execute(stmt).all():
                if created_at is None:
                    continue
                total += 1
                hour = int(created_at.hour)
                buckets[hour] = buckets.get(hour, 0) + 1
            activity_available = total >= SchedulerAgent.MIN_ACTIVITY_INTERACTIONS
            activity_hours: list[int] = []
            if activity_available:
                ranked_activity = sorted(buckets.items(), key=lambda t: (-t[1], t[0]))
                activity_hours = [
                    h for h, _ in ranked_activity[: SchedulerAgent.TOP_ACTIVITY_HOURS]
                ]
            else:
                notes.append(
                    f"audience activity unavailable: {total} interaction(s) < "
                    f"{SchedulerAgent.MIN_ACTIVITY_INTERACTIONS}"
                )

            # -- avoid hours: active campaign ScheduleEntries ----------------
            avoid: set[int] = set()
            run_ats = s.scalars(
                select(ScheduleEntry.run_at).where(
                    ScheduleEntry.workspace_id == ws,
                    ScheduleEntry.status.in_(list(SchedulerAgent.ACTIVE_ENTRY_STATUSES)),
                )
            ).all()
            for run_at in run_ats:
                if run_at is not None:
                    avoid.add(int(run_at.hour))
            avoid_hours = sorted(avoid)

            # -- rank items: measured best-hours first, activity fills in ----
            candidates: list[int] = []
            for hour in hours_ranked + activity_hours:
                if 0 <= hour <= 23 and hour not in candidates:
                    candidates.append(hour)
            # prefer hours the calendar is not already busy at (stable order)
            ordered = [h for h in candidates if h not in avoid] + [
                h for h in candidates if h in avoid
            ]

            items: list[dict] = []
            for hour in ordered[: SchedulerAgent.MAX_WINDOW_ITEMS]:
                sources: list[str] = []
                if hour in hours_ranked:
                    sources.append(
                        "measured_performance"
                        if measured and hour in measured_hours
                        else "platform_seed"
                    )
                elif measured and hour in measured_hours:
                    sources.append("measured_performance")
                if hour in activity_hours:
                    sources.append("audience_activity")
                if not sources:  # never emit an unexplained window
                    sources.append("platform_seed")

                facts: list[str] = []
                if hour in hours_ranked and measured and hour in measured_hours:
                    facts.append(
                        f"measured best hour #{hours_ranked.index(hour) + 1} by average views"
                    )
                elif hour in hours_ranked:
                    if measured:
                        facts.append("generic evening seed (no measured post at this hour)")
                    else:
                        facts.append(
                            f"heuristic seed hour (<{SchedulerAgent.MIN_MEASURED_POSTS} "
                            "measured posts — not measured data)"
                        )
                if hour in activity_hours:
                    facts.append(
                        f"audience activity top hour ({buckets[hour]} interactions)"
                    )
                if hour in avoid:
                    facts.append(
                        "already busy with scheduled campaign entries — prefer another hour"
                    )
                items.append(
                    {"hour": hour, "reason": "; ".join(facts), "sources": sources}
                )

            policy = (
                "action_allowed"
                if settings_json.get("schedule_automation") is True
                else "recommendation_only"
            )
            if policy == "recommendation_only":
                notes.append(
                    "recommendation_only: schedule_automation opt-in not enabled"
                )

            return {
                "items": items,
                "measured": measured,
                "activity": activity_available,
                "audience_activity": {
                    "available": activity_available,
                    "hours": activity_hours,
                },
                "caps": caps,
                "avoid_hours": avoid_hours,
                "activity_policy": policy,
                "notes": notes,
            }
