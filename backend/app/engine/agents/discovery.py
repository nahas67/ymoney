"""Trend Hunter + Trend Analyst agents."""

from __future__ import annotations

from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.engine.scoring import score_opportunity
from app.models import Opportunity, TrendSource
from app.providers import llm
from app.providers.trends import create_source


class TrendHunterAgent(BaseAgent):
    meta = AgentMeta(
        key="trend_hunter",
        title="Trend Hunter",
        description="Discovers emerging topics from configured trend sources.",
        skills=("trend_research",),
        tools=("search_trends",),
        permissions=("trend:read",),
    )

    def fetch_candidates(self, workspace_id: str) -> list[dict]:
        with session_scope() as s:
            sources = s.scalars(
                select(TrendSource).where(
                    TrendSource.workspace_id == workspace_id, TrendSource.enabled.is_(True)
                )
            ).all()
            source_rows = [
                {"kind": t.kind, "config": dict(t.config_json or {}), "priority": t.priority}
                for t in sources
            ]
            niche = ""
            from app.models import Workspace

            ws = s.get(Workspace, workspace_id)
            if ws:
                niche = ws.niche
        candidates: list[dict] = []
        if not source_rows:
            # Default discovery set: structured news + Google Trends + keyless social.
            source_rows = []
            if settings.google_trends_enabled and not settings.mock_trends:
                source_rows.append({"kind": "google_trends", "config": {}, "priority": 50})
            # Hacker News Algolia API is keyless and read-only — safe default.
            if not settings.mock_trends:
                source_rows.append({"kind": "hacker_news", "config": {"min_points": 20, "days": 14}, "priority": 55})
            # NewsData.io joins the default set automatically when a key exists.
            if not settings.mock_trends:
                try:
                    from app.providers.trends import create_source as _cs

                    _probe = _cs("newsdata", {})
                    if getattr(_probe, "api_key", ""):
                        source_rows.append({"kind": "newsdata", "config": {}, "priority": 45})
                except Exception:
                    pass
                # Keyless catalog sources ride along by default (rate-limit friendly).
                source_rows.append({"kind": "coingecko", "config": {}, "priority": 60})
                source_rows.append({"kind": "devto", "config": {"days": 7}, "priority": 65})
            if not source_rows:
                source_rows = [{"kind": "mock", "config": {}, "priority": 50}]
        for row in sorted(source_rows, key=lambda r: r["priority"]):
            try:
                src = create_source(row["kind"], row["config"])
                items = src.fetch(niche=niche, limit=20)
                candidates.extend(
                    {
                        "topic": c.topic,
                        "source": c.source,
                        "external_ref": c.external_ref,
                        "source_url": c.external_ref if c.external_ref.startswith("http") else "",
                        "raw": c.raw,
                        "velocity_hint": c.velocity_hint,
                        "volume_hint": c.volume_hint,
                    }
                    for c in items
                )
            except Exception as exc:  # one failing source must not kill the cycle
                self.announce(workspace_id, f"source '{row['kind']}' unavailable: {exc}", level="warning")
        return candidates


class TrendAnalystAgent(BaseAgent):
    meta = AgentMeta(
        key="trend_analyst",
        title="Trend Analyst",
        description="Scores opportunities explainably and recommends actions.",
        skills=("trend_research",),
        tools=("search_trends",),
        permissions=("trend:read",),
    )

    def score_pending(self, ctx) -> int:
        """Score all unscored opportunities for this workspace; returns count."""
        ws = ctx.workspace_id

        def work():
            weights = {}
            patterns = []
            self.step("load_context", "weights, learned patterns, recent topics")
            with session_scope() as s:
                from app.models import ContentItem, LearningPattern, Workspace

                wrow = s.get(Workspace, ws)
                if wrow:
                    weights = (wrow.settings_json or {}).get("scoring_weights", {})
                pats = s.scalars(
                    select(LearningPattern).where(LearningPattern.workspace_id == ws)
                ).all()
                patterns = [dict(p.__dict__) for p in pats]
                # recent published topics feed the repetition penalty
                recent_rows = s.scalars(
                    select(ContentItem.topic)
                    .where(
                        ContentItem.workspace_id == ws,
                        ContentItem.status.in_(["PUBLISHED", "ANALYZING", "LEARNED"]),
                    )
                    .order_by(ContentItem.created_at.desc())
                    .limit(15)
                ).all()
                recent_topics = list(recent_rows)
                pending = s.scalars(
                    select(Opportunity).where(
                        Opportunity.workspace_id == ws,
                        Opportunity.score == 0.0,
                    )
                ).all()
                ids = []
                self.step_done("ok", f"{len(pending)} unscored opportunity(ies)")
                self.step("score_opportunities", "explainable scoring v2 per opportunity")
                for opp in pending:
                    raw = dict(opp.raw_payload or {})
                    scored = score_opportunity(
                        candidate={
                            "topic": opp.topic,
                            "source": opp.source,
                            "raw": {k: v for k, v in raw.items() if not k.startswith("_")},
                            "velocity_hint": raw.get("_velocity_hint"),
                            "volume_hint": raw.get("_volume_hint"),
                        },
                        niche=(wrow.niche if wrow else "") or "",
                        patterns=patterns,
                        weights=weights,
                        recent_topics=recent_topics,
                    )
                    breakdown = scored.breakdown()
                    opp.score = breakdown["overall"]
                    opp.components_json = breakdown["components"]
                    opp.recommendation = breakdown["recommendation"]
                    opp.lifecycle = breakdown.get("lifecycle", "UNKNOWN")
                    opp.confidence = float(breakdown.get("confidence", 0.5))
                    opp.virality = float(breakdown.get("virality", 0.0))
                    ids.append(opp.id)
                self.step_done("ok", f"scored {len(ids)} opportunity(ies)")
                s.flush()
            return len(ids)

        return self.execute(ctx, "score_opportunities", input_summary="pending opportunities", fn=work)


_persist_lock = __import__("threading").Lock()


def persist_opportunities(agent: TrendHunterAgent, workspace_id: str, candidates: list[dict], cycle_id: str) -> int:
    """Dedupe by topic+source and store new candidates as opportunities."""
    new_count = 0
    with _persist_lock, session_scope() as s:
        existing_topics = {
            t.strip().lower()
            for t in s.scalars(
                select(Opportunity.topic)
                .where(Opportunity.workspace_id == workspace_id)
                .limit(2000)
            ).all()
        }
        for c in candidates:
            topic_key = c["topic"].strip().lower()
            if topic_key in existing_topics:
                continue
            existing_topics.add(topic_key)
            raw = dict(c.get("raw") or {})
            # preserve provider scoring signals for later analysis
            if c.get("velocity_hint") is not None:
                raw["_velocity_hint"] = c["velocity_hint"]
            if c.get("volume_hint") is not None:
                raw["_volume_hint"] = c["volume_hint"]
            if c.get("source_url"):
                raw["_source_url"] = c["source_url"]
            s.add(
                Opportunity(
                    workspace_id=workspace_id,
                    cycle_id=cycle_id,
                    topic=c["topic"],
                    source=c.get("source", "unknown"),
                    external_ref=c.get("external_ref", ""),
                    raw_payload=raw,
                )
            )
            new_count += 1
        s.flush()
    return new_count


def llm_assisted_fit_check(workspace_id: str, topic: str, niche: str) -> float | None:
    """Optional LLM refinement of audience fit; returns None when unavailable."""
    if not llm.llm_available():
        return None
    try:
        res = llm.complete_json(
            system="You rate how well a trending topic fits a channel niche for short-form video. Reply JSON.",
            user=f'Niche: "{niche}"\nTopic: "{topic}"\nReturn: {{"fit": <0-100>}}',
            workspace_id=workspace_id,
            temperature=0.2,
            max_tokens=60,
        )
        fit = res.get("fit")
        if isinstance(fit, (int, float)):
            return max(0.0, min(100.0, float(fit)))
    except Exception:
        pass
    return None

