"""Competitor Analyst agent (E6 brains): watchlist scanning + trend-jack alerts.

Reads enabled `youtube_channel` trend sources as a competitor watchlist,
persists fresh competitor videos as opportunities, and raises trend-jack
alerts for fast-moving topics the workspace hasn't covered yet.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.engine.agents.discovery import persist_opportunities
from app.models import ContentItem, TrendSource
from app.providers.trends import create_source


class CompetitorAnalystAgent(BaseAgent):
    meta = AgentMeta(
        key="competitor_analyst",
        title="Competitor Analyst",
        description="Tracks competitor channels and flags trend-jack openings.",
        skills=("trend_research",),
        tools=("search_trends",),
        permissions=("trend:read",),
    )

    def scan(self, ctx, *, limit_per_channel: int = 10) -> dict:
        def work():
            from app.engine.decision import topic_similarity

            ws = ctx.workspace_id or ""
            self.step("load_watchlist", "enabled youtube_channel sources")
            with session_scope() as s:
                sources = s.scalars(
                    select(TrendSource).where(
                        TrendSource.workspace_id == ws,
                        TrendSource.enabled.is_(True),
                        TrendSource.kind == "youtube_channel",
                    )
                ).all()
                cfgs = [(t.id, dict(t.config_json or {})) for t in sources]
                niche = ""
                from app.models import Workspace

                row = s.get(Workspace, ws)
                if row:
                    niche = row.niche or ""
                recent = list(s.scalars(
                    select(ContentItem.topic)
                    .where(ContentItem.workspace_id == ws,
                           ContentItem.status.in_(["PUBLISHED", "ANALYZING", "LEARNED"]))
                    .order_by(ContentItem.created_at.desc()).limit(40)
                ).all())
            if not cfgs:
                self.step_done("ok", "no competitor channels tracked")
                return {"summary": "watchlist empty — add youtube_channel sources in Settings",
                        "new": 0, "alerts": []}
            self.step_done("ok", f"{len(cfgs)} channel(s)")
            new_total, alerts = 0, []
            for _sid, cfg in cfgs:
                try:
                    src = create_source("youtube_channel", cfg)
                    candidates = src.fetch(niche=niche, limit=limit_per_channel)
                except Exception as exc:
                    self.announce(ws, f"competitor source failed: {exc}", level="warning")
                    continue
                items = [{
                    "topic": c.topic, "source": "competitor",
                    "external_ref": c.external_ref, "source_url": c.external_ref,
                    "raw": {**(c.raw or {}), "watchlist": True},
                    "velocity_hint": c.velocity_hint, "volume_hint": c.volume_hint,
                } for c in candidates]
                new_total += persist_opportunities(self, ws, items, ctx.cycle_id or "")
                for c in candidates:
                    if (c.velocity_hint or 0) < 0.7:
                        continue
                    if any(topic_similarity(c.topic, rt) >= 0.55 for rt in recent):
                        continue
                    alerts.append({"topic": c.topic, "url": c.external_ref,
                                   "channel": (c.raw or {}).get("channel", "")})
            self.step("alert", f"{len(alerts)} trend-jack opening(s)")
            for a in alerts[:5]:
                self.announce(ws, f"trend-jack opening: '{a['topic'][:70]}' ({a['channel']})",
                              level="warning", cycle_id=ctx.cycle_id)
            self.step_done("ok", f"{new_total} new, {len(alerts)} alert(s)")
            return {"summary": f"scanned {len(cfgs)} channel(s): {new_total} new, {len(alerts)} alerts",
                    "new": new_total, "alerts": alerts}

        return self.execute(ctx, "scan_competitors", input_summary="competitor watchlist", fn=work)
