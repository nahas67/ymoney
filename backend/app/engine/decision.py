"""Decision Engine: NEXT BEST ACTION for a workspace.

Replaces naive "pick highest score" selection. Considers:

    score (with repetition/diversity penalties applied upstream)
    budget state            daily + monthly
    safety caps             max videos/day, max uploads/hour
    queue capacity          concurrent renders in flight
    recent content          topic similarity / format diversity
    learned patterns        confident patterns boost matching topics
    opportunity lifecycle   EMERGING/RISING topics preferred over PEAK/DECLINING

Decisions returned:
    PRODUCE          proceed with full build
    WAIT             defer (capacity/budget); cycle retries later
    SKIP             do not produce; reason recorded
    RESEARCH_MORE    interesting but insufficient research confidence
    HUMAN_REVIEW     risky or ambiguous — requires explicit approval

Every decision carries a complete WHY payload for the UI:
factors with values, contributions and evidence strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_, select

from app.core.config import settings
from app.db import session_scope
from app.models import (
    AutopilotRun,
    ContentItem,
    CostEntry,
    Job,
    LearningPattern,
    Opportunity,
    PublishedPost,
)
from app.models.base import JobStatus, utcnow

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "it", "its",
    "at", "by", "from", "as", "vs", "your", "you", "we", "my",
}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in STOPWORDS}


def topic_similarity(a: str, b: str) -> float:
    """Jaccard similarity between token sets — cheap, deterministic, explainable."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# pattern_key -> topic keywords that make the pattern applicable
_PATTERN_RELEVANCE = {
    "hook_style_question": (),      # format patterns apply broadly
    "duration_long_form": (),
    "title_with_numbers": (),
    "commercial_intent": ("money", "income", "save", "invest", "budget", "earn", "price"),
}


def _pattern_relevant(pattern, topic_lower: str) -> bool:
    """A pattern informs a decision only when its domain applies to the topic.
    Format patterns (hooks/duration/title style) apply broadly; domain patterns
    require topical keyword overlap."""
    keywords = _PATTERN_RELEVANCE.get(
        getattr(pattern, "pattern_key", ""),
        _PATTERN_RELEVANCE["commercial_intent"],  # unknown keys: conservative
    )
    if not keywords:  # broad format pattern
        return True
    return any(k in topic_lower for k in keywords)


def _memory_context(workspace_id: str, topic: str) -> dict:
    """Targeted memory retrieval for decision context (spec #25). Returns
    topic-relevant semantic memories plus always-applicable strategic and
    preference memories. Read-only; never raises into the decision path."""
    try:
        from app.services import memory as memory_service

        topic_hits = [
            m for m in memory_service.retrieve_for_topic(workspace_id, topic, limit=5)
            if m["confidence"] >= 0.4
        ]
        strategic = memory_service.retrieve(workspace_id, type="strategic", limit=3)
        preference = memory_service.retrieve(workspace_id, type="preference", limit=3)
        return {"semantic": topic_hits, "strategic": strategic, "preference": preference}
    except Exception:
        return {"semantic": [], "strategic": [], "preference": []}


@dataclass
class Factor:
    name: str
    value: str          # human-readable value
    contribution: float # signed points added/removed from decision confidence
    detail: str = ""


@dataclass
class Decision:
    action: str                     # PRODUCE|WAIT|SKIP|RESEARCH_MORE|HUMAN_REVIEW
    opportunity_id: str | None = None
    topic: str | None = None
    score: float = 0.0
    confidence: float = 0.0         # 0..1
    reasons: list[str] = field(default_factory=list)
    factors: list[Factor] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def why(self) -> dict:
        return {
            "action": self.action,
            "opportunity_id": self.opportunity_id,
            "topic": self.topic,
            "score": self.score,
            "confidence": round(self.confidence, 2),
            "reasons": self.reasons,
            "factors": [
                {"name": f.name, "value": f.value, "contribution": round(f.contribution, 1), "detail": f.detail}
                for f in self.factors
            ],
            "evidence": self.evidence,
        }


def get_safety_settings(ws_settings: dict) -> dict:
    """Merged safety configuration with safe defaults."""
    s = ws_settings.get("safety", {}) if isinstance(ws_settings, dict) else {}
    return {
        "daily_budget_usd": float(s.get("daily_budget_usd", settings.daily_budget_usd)),
        "monthly_budget_usd": float(s.get("monthly_budget_usd", settings.daily_budget_usd * 30)),
        "per_video_budget_usd": float(s.get("per_video_budget_usd", settings.per_video_budget_usd)),
        "max_videos_per_day": int(s.get("max_videos_per_day", 10)),
        "max_uploads_per_hour": int(s.get("max_uploads_per_hour", 6)),
        "min_qc_score": int(s.get("min_qc_score", settings.quality_threshold)),
        "max_render_attempts": int(s.get("max_render_attempts", 2)),
        "max_consecutive_failures": int(s.get("max_consecutive_failures", 3)),
        "max_concurrent_renders": int(s.get("max_concurrent_renders", 2)),
        "similarity_threshold": float(s.get("similarity_threshold", 0.55)),
        "require_human_review_risk_above": float(s.get("require_human_review_risk_above", 60.0)),
        "require_approval_before_publish": bool(s.get("require_approval_before_publish", False)),
        # adjusted-score bar to commit production spend; below this but above
        # wait_floor the supervisor WAITs for stronger candidates
        "produce_score_threshold": float(s.get("produce_score_threshold", 58.0)),
    }


def decide_next_best_action(workspace_id: str) -> Decision:
    """Evaluate the best unselected opportunity against workspace context."""
    with session_scope() as s:
        from app.models import Workspace

        ws_row = s.get(Workspace, workspace_id)
        ws_settings = (ws_row.settings_json or {}) if ws_row else {}
        niche = (ws_row.niche or "") if ws_row else ""
        safety = get_safety_settings(ws_settings)

        candidates = s.scalars(
            select(Opportunity)
            .where(
                Opportunity.workspace_id == workspace_id,
                Opportunity.selected.is_(False),
                func.coalesce(Opportunity.skipped_reason, "") == "",
                or_(
                    Opportunity.recommendation.is_(None),
                    Opportunity.recommendation.not_in(["SKIP", "HUMAN_REVIEW"]),
                ),
            )
            .order_by(Opportunity.score.desc(), Opportunity.created_at.desc())
            .limit(20)
        ).all()

        # ---- context -------------------------------------------------------
        day_ago = utcnow() - timedelta(hours=24)
        hour_ago = utcnow() - timedelta(hours=1)
        month_ago = utcnow() - timedelta(days=30)

        videos_today = s.scalar(
            select(func.count()).select_from(ContentItem).where(
                ContentItem.workspace_id == workspace_id,
                ContentItem.created_at >= day_ago,
            )
        ) or 0
        uploads_last_hour = s.scalar(
            select(func.count()).select_from(PublishedPost).where(
                PublishedPost.workspace_id == workspace_id,
                PublishedPost.published_at >= hour_ago,
            )
        ) or 0
        renders_in_flight = s.scalar(
            select(func.count()).select_from(Job).where(
                Job.workspace_id == workspace_id,
                Job.type == "cycle.build",
                Job.status.in_([JobStatus.RUNNING.value, JobStatus.RETRYING.value]),
            )
        ) or 0
        cost_24h = s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == workspace_id,
                CostEntry.created_at >= day_ago,
            )
        ) or 0.0
        cost_month = s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == workspace_id,
                CostEntry.created_at >= month_ago,
            )
        ) or 0.0

        recent_published = s.scalars(
            select(ContentItem)
            .where(
                ContentItem.workspace_id == workspace_id,
                ContentItem.created_at >= utcnow() - timedelta(days=14),
                ContentItem.status.in_(["PUBLISHED", "ANALYZING", "LEARNED", "APPROVED", "SCHEDULED"]),
            )
            .order_by(ContentItem.created_at.desc())
            .limit(40)
        ).all()

        patterns = s.scalars(
            select(LearningPattern).where(
                LearningPattern.workspace_id == workspace_id, LearningPattern.active.is_(True)
            )
        ).all()

    factors: list[Factor] = []
    evidence: list[str] = []

    # ---- hard capacity gates (apply regardless of candidate quality) --------
    if videos_today >= safety["max_videos_per_day"]:
        return Decision(
            action="WAIT",
            confidence=1.0,
            reasons=[f"daily video cap reached ({videos_today}/{safety['max_videos_per_day']})"],
            factors=[Factor("daily_cap", f"{videos_today}/{safety['max_videos_per_day']}", 0)],
        )
    if uploads_last_hour >= safety["max_uploads_per_hour"]:
        return Decision(
            action="WAIT",
            confidence=1.0,
            reasons=[f"hourly upload cap reached ({uploads_last_hour}/{safety['max_uploads_per_hour']})"],
            factors=[Factor("upload_rate", f"{uploads_last_hour}/h", 0)],
        )
    if renders_in_flight >= 3:
        return Decision(
            action="WAIT",
            confidence=1.0,
            reasons=[f"render capacity saturated ({renders_in_flight} builds in flight)"],
            factors=[Factor("queue_depth", str(renders_in_flight), 0)],
        )
    if cost_24h >= safety["daily_budget_usd"]:
        return Decision(
            action="WAIT",
            confidence=1.0,
            reasons=[
                f"daily budget consumed (${cost_24h:.2f}/${safety['daily_budget_usd']:.2f}) — "
                "production halts until the window resets"
            ],
            factors=[Factor("daily_budget", f"${cost_24h:.2f}", 0)],
        )
    if cost_month >= safety["monthly_budget_usd"]:
        return Decision(
            action="WAIT",
            confidence=1.0,
            reasons=[f"monthly budget consumed (${cost_month:.2f})"],
            factors=[Factor("monthly_budget", f"${cost_month:.2f}", 0)],
        )
    if not candidates:
        return Decision(
            action="WAIT",
            confidence=0.8,
            reasons=["no unselected opportunities available; discovery will refresh"],
        )

    best = candidates[0]

    # ---- per-candidate scoring adjustments ---------------------------------
    base_score = best.score
    adjusted = base_score
    factors.append(Factor("base_score", f"{base_score:.0f}/100", base_score * 0.4))

    # repetition penalty vs recent content
    worst_sim, worst_topic = 0.0, ""
    sims = []
    for item in recent_published:
        sim = topic_similarity(best.topic, item.topic)
        sims.append(sim)
        if sim > worst_sim:
            worst_sim, worst_topic = sim, item.topic
    repetition_penalty = 0.0
    if sims:
        avg_sim = sum(sims) / len(sims)
        repetition_penalty = min(35.0, worst_sim * 45 + avg_sim * 15)
        adjusted -= repetition_penalty
        factors.append(
            Factor(
                "repetition_penalty",
                f"-{repetition_penalty:.0f}",
                -repetition_penalty * 0.5,
                f"closest published topic: '{worst_topic[:60]}' ({worst_sim:.0%} similar)"
                if worst_topic else "",
            )
        )
        if worst_sim > 0:
            evidence.append(f"Most similar recent content: '{worst_topic[:70]}' ({worst_sim:.0%} overlap)")
    else:
        factors.append(Factor("repetition_penalty", "0 (no history)", 0))

    # lifecycle bonus
    lifecycle_bonus = {"EMERGING": 12.0, "RISING": 8.0, "EVERGREEN": 4.0, "PEAK": 0.0, "DECLINING": -10.0}.get(
        getattr(best, "lifecycle", "UNKNOWN"), 2.0
    )
    adjusted += lifecycle_bonus
    factors.append(
        Factor("lifecycle", f"{best.lifecycle} ({lifecycle_bonus:+.0f})", lifecycle_bonus * 0.5,
               "emerging trends earn outsized attention; declining ones are penalized")
    )
    if best.lifecycle in ("EMERGING", "RISING"):
        evidence.append(f"Trend classified {best.lifecycle} by emerging-trend detection")

    # learned-pattern alignment — each confident pattern contributes a named,
    # inspectable factor so the WHY panel shows WHICH history drives the boost
    if patterns:
        topic_lower = best.topic.lower()
        matched = [
            p for p in patterns
            if p.confidence in ("medium", "high")
            and p.sample_size >= 3
            and _pattern_relevant(p, topic_lower)
        ]
        if matched:
            total_boost = sum(min(p.observed_improvement_pct, 15.0) for p in matched)
            boost = min(total_boost * 0.3, 8.0)
            adjusted += boost
            factors.append(
                Factor("learned_patterns", f"+{boost:.0f}", boost * 0.5,
                       f"{len(matched)} relevant pattern(s): "
                       + "; ".join(
                           f"{p.pattern_key} ({p.observed_improvement_pct:+.0f}%, {p.confidence}, n={p.sample_size})"
                           for p in matched[:3]
                       ))
            )
            evidence.append(
                f"Learned patterns from {sum(p.sample_size for p in matched)} historical posts "
                f"favor topics like this ({', '.join(p.pattern_key for p in matched[:3])})"
            )

    # memory context (spec #25): topic-relevant semantic memories nudge the
    # score modestly and always surface as inspectable WHY evidence.
    mem = _memory_context(workspace_id, best.topic)
    if mem["semantic"]:
        mem_boost = min(1.5 * len(mem["semantic"]), 4.5)
        adjusted += mem_boost
        factors.append(
            Factor("memory_context", f"+{mem_boost:.0f}", mem_boost * 0.5,
                   f"{len(mem['semantic'])} relevant memory record(s): "
                   + "; ".join(m["content"][:60] for m in mem["semantic"][:2]))
        )
        evidence.append(
            "Persistent memory recalls related knowledge: "
            + " | ".join(m["content"][:80] for m in mem["semantic"][:3])
        )
    if mem["strategic"]:
        evidence.append("Strategic memory in effect: " + mem["strategic"][0]["content"][:90])

    # risk → human review escalation
    risk_component = (best.components_json or {}).get("risk", {})
    risk_value = risk_component.get("score", 12.0) if isinstance(risk_component, dict) else 12.0
    if risk_value >= safety["require_human_review_risk_above"]:
        return Decision(
            action="HUMAN_REVIEW",
            opportunity_id=best.id,
            topic=best.topic,
            score=adjusted,
            confidence=0.9,
            reasons=[f"sensitive subject matter (risk {risk_value:.0f}/100) requires manual approval"],
            factors=factors,
            evidence=evidence + ["Policy guardrail: risky topics never auto-publish"],
        )

    # similarity hard-stop
    if worst_sim >= safety["similarity_threshold"]:
        return Decision(
            action="SKIP",
            opportunity_id=best.id,
            topic=best.topic,
            score=adjusted,
            confidence=0.85,
            reasons=[
                f"too similar to recently published '{worst_topic[:60]}' "
                f"({worst_sim:.0%} ≥ threshold {safety['similarity_threshold']:.0%})"
            ],
            factors=factors,
            evidence=evidence,
        )

    adjusted = max(0.0, min(100.0, adjusted))

    # ---- final thresholds ---------------------------------------------------
    if adjusted < 45:
        return Decision(
            action="SKIP",
            opportunity_id=best.id,
            topic=best.topic,
            score=adjusted,
            confidence=0.75,
            reasons=[f"context-adjusted score too low ({adjusted:.0f}/100 after diversity & context)"],
            factors=factors,
            evidence=evidence,
        )
    if adjusted < safety["produce_score_threshold"]:
        return Decision(
            action="WAIT",
            opportunity_id=best.id,
            topic=best.topic,
            score=adjusted,
            confidence=0.7,
            reasons=[
                f"borderline score ({adjusted:.0f}/100) — waiting for stronger signal "
                "or better candidates"
            ],
            factors=factors,
            evidence=evidence,
        )

    # low scoring-confidence opportunities need more research before committing spend
    if float(getattr(best, "confidence", 0.5)) < 0.35 and adjusted < 70:
        return Decision(
            action="RESEARCH_MORE",
            opportunity_id=best.id,
            topic=best.topic,
            score=adjusted,
            confidence=float(best.confidence),
            reasons=[
                f"scoring confidence low ({best.confidence:.0%}); deeper research required "
                "before production spend"
            ],
            factors=factors,
            evidence=evidence,
        )

    remaining_daily = safety["daily_budget_usd"] - cost_24h
    est_cost = settings.per_video_budget_usd
    if remaining_daily < est_cost:
        return Decision(
            action="WAIT",
            opportunity_id=best.id,
            topic=best.topic,
            score=adjusted,
            confidence=0.9,
            reasons=[
                f"remaining daily budget (${remaining_daily:.2f}) below estimated video cost (${est_cost:.2f})"
            ],
            factors=factors,
            evidence=evidence,
        )

    factors.append(Factor("budget_headroom", f"${remaining_daily:.2f} left", 3.0))
    evidence.append(
        f"Budget headroom ${remaining_daily:.2f}; queue depth {renders_in_flight}; "
        f"{videos_today}/{safety['max_videos_per_day']} videos today"
    )

    rec = "CREATE_NOW" if adjusted >= 82 else "PRODUCE"
    return Decision(
        action="PRODUCE",
        opportunity_id=best.id,
        topic=best.topic,
        score=adjusted,
        confidence=min(0.95, 0.5 + adjusted / 200),
        reasons=[f"context-adjusted score {adjusted:.0f}/100 → {rec}"],
        factors=factors,
        evidence=evidence,
    )


def record_auto_pause(workspace_id: str, trigger: str, detail: str) -> bool:
    """Safety-triggered autopilot pause with explanation (Safety Center)."""
    from app.services.events import record_event

    was_running = False
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(
                AutopilotRun.workspace_id == workspace_id,
                AutopilotRun.state.not_in(["STOPPED"]),
            )
            .order_by(AutopilotRun.created_at.desc())
            .limit(1)
        )
        if not run:
            return False
        was_running = run.state in ("RUNNING", "STARTING")
        run.state = "PAUSED"
        run.last_error = f"safety trigger: {trigger}: {detail}"[:2000]
    if was_running:
        record_event(
            workspace_id,
            kind="safety.autopause",
            message=f"AUTOPILOT PAUSED — {trigger}: {detail}",
            level="warning",
            source="safety",
            data={"trigger": trigger},
        )
    return was_running
