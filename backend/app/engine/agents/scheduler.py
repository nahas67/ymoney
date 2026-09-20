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
            return {"summary": f"scheduled {len(created)} publish(es) over {days_n} day(s)",
                    "created": created}

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
