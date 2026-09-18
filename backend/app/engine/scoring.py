"""Explainable opportunity scoring (v2).

Overall score is a weighted blend of component scores (0..100 each):
    positive: trend_velocity, engagement, audience_fit, search_demand,
              novelty, monetization, historical_performance
    negative: competition, production_cost, risk, repetition

Every component carries score/weight/reason/source/confidence so the UI can
render a full WHY panel. Penalties scale the positive average
multiplicatively; a repetition penalty applies when the topic resembles
recently published content. Weights come from workspace settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.trends import classify_lifecycle

DEFAULT_WEIGHTS: dict[str, float] = {
    "trend_velocity": 1.4,
    "engagement": 0.8,
    "audience_fit": 1.3,
    "search_demand": 1.0,
    "novelty": 0.7,
    "monetization": 0.9,
    "historical_performance": 0.6,
    "competition": -1.0,
    "production_cost": -0.6,
    "risk": -1.2,
}


@dataclass
class ComponentScore:
    key: str
    value: float        # 0..100
    weight: float
    weighted: float
    reason: str
    source: str = "heuristic"       # provider | heuristic | learned | policy
    confidence: float = 0.6         # 0..1 confidence in this component's value


@dataclass
class ScoredOpportunity:
    overall: float
    recommendation: str  # CREATE_NOW | PRODUCE | WAIT | SKIP
    lifecycle: str = "UNKNOWN"
    confidence: float = 0.5
    virality: float = 0.0  # 0..100 breakout potential (informational; not in overall)
    components: list[ComponentScore] = field(default_factory=list)

    def breakdown(self) -> dict:
        return {
            "overall": round(self.overall, 1),
            "recommendation": self.recommendation,
            "lifecycle": self.lifecycle,
            "confidence": round(self.confidence, 2),
            "virality": round(self.virality, 1),
            "components": {
                c.key: {
                    "score": round(c.value, 1),
                    "weight": c.weight,
                    "reason": c.reason,
                    "source": c.source,
                    "confidence": round(c.confidence, 2),
                }
                for c in self.components
            },
        }


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def score_opportunity(
    candidate: dict,
    niche: str,
    patterns: list[dict] | None = None,
    weights: dict | None = None,
    recent_topics: list[str] | None = None,
) -> ScoredOpportunity:
    """Score one candidate dict with keys:
    topic, source, velocity_hint, volume_hint, raw, lifecycle (optional)
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    topic = (candidate.get("topic") or "").lower()
    source = candidate.get("source", "unknown")
    velocity_hint = candidate.get("velocity_hint")
    volume_hint = candidate.get("volume_hint")
    raw = candidate.get("raw") or {}
    news = raw.get("news") or []

    # --- trend lifecycle ----------------------------------------------------
    lifecycle, life_conf = classify_lifecycle(
        velocity_hint=velocity_hint,
        volume_hint=volume_hint,
        news_count=len(news),
        source=source,
    )
    if candidate.get("lifecycle") and candidate["lifecycle"] in (
        "EMERGING", "RISING", "PEAK", "DECLINING", "EVERGREEN"
    ):
        lifecycle = candidate["lifecycle"]
        life_conf = max(life_conf, 0.8)

    # --- trend velocity -----------------------------------------------------
    if velocity_hint is not None:
        vel = _clamp(velocity_hint * 100)
        vel_reason = f"provider-reported momentum ({velocity_hint:.2f})"
        vel_source, vel_conf = "provider", 0.85
    else:
        # lifecycle-aware neutral estimate
        vel = {"EMERGING": 78, "RISING": 70, "PEAK": 55, "DECLINING": 35}.get(lifecycle, 60.0)
        vel_reason = f"no momentum signal; {lifecycle.lower()} lifecycle estimate"
        vel_source, vel_conf = "heuristic", 0.45
    comps = [ComponentScore("trend_velocity", vel, w["trend_velocity"], vel * w["trend_velocity"],
                            vel_reason, vel_source, vel_conf)]

    # --- engagement ---------------------------------------------------------
    if news:
        eng = _clamp(55 + len(news) * 10)
        eng_reason = f"covered by {len(news)} recent news item(s)"
        eng_conf = 0.75
    elif raw.get("ups"):
        ups = raw["ups"]
        eng = _clamp(min(100, 45 + (ups / 1000) * 20))
        eng_reason = f"{ups} upvotes on source thread"
        eng_conf = 0.7
    else:
        eng, eng_reason, eng_conf = 58.0, "baseline engagement estimate", 0.4
    comps.append(ComponentScore("engagement", eng, w["engagement"], eng * w["engagement"],
                                eng_reason, "provider" if news or raw.get("ups") else "heuristic", eng_conf))

    # --- audience fit -------------------------------------------------------
    niche_words = set(niche.lower().split()) if niche else set()
    overlap = len(niche_words & set(topic.split())) if niche_words else 0
    fit = _clamp(55 + overlap * 25)
    comps.append(
        ComponentScore(
            "audience_fit", fit, w["audience_fit"], fit * w["audience_fit"],
            f"topic/niche keyword overlap: {overlap}" if niche else "no niche configured; general channel assumed",
            "workspace", 0.8 if niche else 0.5,
        )
    )

    # --- search demand ------------------------------------------------------
    demand = _clamp((volume_hint or 0.5) * 100)
    comps.append(
        ComponentScore(
            "search_demand", demand, w["search_demand"], demand * w["search_demand"],
            "provider volume signal" if volume_hint is not None else "default demand estimate",
            "provider" if volume_hint is not None else "heuristic",
            0.8 if volume_hint is not None else 0.45,
        )
    )

    # --- novelty ------------------------------------------------------------
    novelty = _clamp(70 - len(topic.split()) * 2)
    comps.append(
        ComponentScore("novelty", novelty, w["novelty"], novelty * w["novelty"],
                       "topic specificity proxy", "heuristic", 0.5)
    )

    # --- monetization -------------------------------------------------------
    money_terms = ("money", "income", "budget", "save", "invest", "earn", "cash", "finance")
    mon = _clamp(80 if any(t in topic for t in money_terms) else 55)
    comps.append(
        ComponentScore(
            "monetization", mon, w["monetization"], mon * w["monetization"],
            "strong commercial intent detected" if mon > 60 else "general interest topic",
            "policy", 0.65,
        )
    )

    # --- historical performance ---------------------------------------------
    hist = 50.0
    hist_reason = "insufficient channel history"
    hist_conf = 0.4
    if patterns:
        boosts = [
            p.get("observed_improvement_pct", 0)
            for p in patterns
            if p.get("confidence") in ("medium", "high") and p.get("active", True)
        ]
        if boosts:
            avg_boost = sum(boosts) / len(boosts)
            hist = _clamp(50 + avg_boost / 2)
            hist_reason = f"learned patterns avg +{avg_boost:.0f}% (n={len(boosts)})"
            hist_conf = 0.7
    comps.append(ComponentScore("historical_performance", hist, w["historical_performance"],
                                hist * w["historical_performance"], hist_reason, "learned", hist_conf))

    # --- competition ---------------------------------------------------------
    broad = len(topic.split()) <= 3
    comp_score = _clamp(75 if broad else 45)
    comps.append(ComponentScore(
        "competition", comp_score, w["competition"], comp_score * w["competition"],
        "broad topic => heavy competition" if broad else "focused angle => moderate competition",
        "heuristic", 0.55,
    ))

    # --- production cost ------------------------------------------------------
    cost_score = 30.0
    comps.append(ComponentScore(
        "production_cost", cost_score, w["production_cost"], cost_score * w["production_cost"],
        "standard short-form production (~35s)", "heuristic", 0.9,
    ))

    # --- risk -----------------------------------------------------------------
    risky = ("war", "crime", "tragedy", "death", "scandal", "medical", "drug")
    risk = _clamp(80 if any(t in topic for t in risky) else 12)
    comps.append(ComponentScore(
        "risk", risk, w["risk"], risk * w["risk"],
        "sensitive subject matter" if risk > 50 else "low policy/safety risk",
        "policy", 0.75,
    ))

    # --- repetition penalty ----------------------------------------------------
    rep_value, rep_reason = 0.0, "no recent content to compare against"
    if recent_topics:
        from app.engine.decision import topic_similarity

        worst = max((topic_similarity(topic, rt) for rt in recent_topics), default=0.0)
        rep_value = _clamp(worst * 100)
        rep_reason = f"{worst:.0%} similarity to most recent published topic"
    comps.append(ComponentScore(
        "repetition", rep_value, 0.0, 0.0, rep_reason, "workspace",
        0.9 if recent_topics else 1.0,
    ))

    pos_comps = [c for c in comps if c.weight > 0]
    neg_comps = [c for c in comps if c.weight < 0]
    pos_weight = sum(c.weight for c in pos_comps)
    neg_weight = sum(abs(c.weight) for c in neg_comps)
    pos_avg = sum(c.weighted for c in pos_comps) / pos_weight if pos_weight else 0.0
    penalty_avg = sum(abs(c.weight) * c.value for c in neg_comps) / neg_weight if neg_weight else 0.0

    overall = _clamp(pos_avg * (1 - 0.45 * (penalty_avg / 100)) - rep_value * 0.15)

    # explicit lifecycle classification shifts the final score: early trends
    # are worth more than fading ones even at identical provider signals
    lifecycle_adjustment = {"EMERGING": 4.0, "RISING": 2.0, "PEAK": 0.0,
                            "DECLINING": -6.0, "EVERGREEN": 1.0}.get(lifecycle, 0.0)
    overall = _clamp(overall + lifecycle_adjustment)

    # --- recommendation ----------------------------------------------------------
    if overall >= 82:
        rec = "CREATE_NOW"
    elif overall >= 68:
        rec = "PRODUCE"
    elif overall >= 52:
        rec = "WAIT"
    else:
        rec = "SKIP"

    confidences = [c.confidence for c in comps]
    overall_confidence = sum(confidences) / len(confidences)

    # Virality (breakout potential, informational only): momentum × novelty ×
    # whitespace × social proof. Deliberately excluded from `overall` so it
    # cannot change recommendations until the decision engine opts in.
    by_key = {c.key: c.value for c in comps}
    virality = _clamp(
        by_key.get("trend_velocity", 50) * 0.45
        + by_key.get("novelty", 50) * 0.25
        + (100.0 - by_key.get("competition", 50)) * 0.15
        + by_key.get("engagement", 50) * 0.15
    )

    return ScoredOpportunity(
        overall=round(overall, 1),
        recommendation=rec,
        lifecycle=lifecycle,
        confidence=round(overall_confidence, 2),
        virality=round(virality, 1),
        components=comps,
    )
