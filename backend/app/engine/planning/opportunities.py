"""Work 15 §2/§3 — ``ContentOpportunity`` and honest scoring.

An opportunity is a *proposal* derived from signals. The single most important
property of this module is that the derivation is recorded:

    ``OBSERVED``    the demand itself was seen in evidence
    ``INFERRED``    derived from evidence by scoring/clustering
    ``RECOMMENDED`` an AI suggestion with no measurement behind it

``RECOMMENDED`` is not a lesser ``INFERRED``. It means *there is no evidence
this demand exists*, and the planner treats it differently: it may be recorded
and shown, but it may not be auto-scheduled as demand, and its score is capped
so an eloquent suggestion cannot outrank a measured signal.

Scoring uses only factors that are actually measurable, and every factor
records whether it was MEASURED or ASSUMED:

    recurrence        counted distinct signals            measured
    freshness         days since observation              measured
    community demand  actual request/comment counts       measured
    prior performance measured engagement on our own posts measured
    brand fit         BrandDNA match                       measured
    platform fit      platform capability match           measured
    novelty           library similarity                  measured
    effort            hours/cost estimate                  ASSUMED
    saturation        our own recent posts on the topic    measured

There is deliberately **no** virality term, no revenue term, and no
"probability of success". :func:`forbidden_claims` is the guard: it returns the
claims the module refuses to make, and a test asserts no such field exists on
the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select

from app.engine.planning.signals import (
    SignalRead,
    claim_signals,
    signal_freshness,
    topic_recurrence,
)
from app.models.content import Opportunity
from app.models.planning import (
    AGING,
    FRESH,
    INFERRED,
    OBSERVED,
    RECOMMENDED,
    STALE,
)

__all__ = [
    "FORBIDDEN_CLAIMS",
    "OpportunityDraft",
    "OpportunityScore",
    "build_opportunity",
    "forbidden_claims",
    "generate_opportunities",
    "score_opportunity",
]

#: The claims this module must never make. Kept as data so a test can assert
#: against the same list the code is written from.
FORBIDDEN_CLAIMS: tuple[str, ...] = (
    "guaranteed virality",
    "guaranteed revenue",
    "probability of success",
    "will go viral",
    "certain to succeed",
    "success probability",
    "expected revenue",
)

#: A RECOMMENDED idea is capped so a suggestion can never outrank measured
#: demand. It may be planned, but only with human eyes on it.
RECOMMENDED_SCORE_CAP = 0.35

#: Weight per factor. Kept explicit so a plan is reproducible from the stored
#: inputs alone (Work 15 §14).
FACTOR_WEIGHTS: dict[str, float] = {
    "recurrence": 0.22,
    "freshness": 0.14,
    "community_demand": 0.14,
    "prior_performance": 0.12,
    "brand_fit": 0.12,
    "platform_fit": 0.08,
    "novelty": 0.08,
    "saturation": 0.10,
}


@dataclass
class OpportunityScore:
    """A score with every factor and its provenance attached."""

    total: float
    factors: dict[str, dict] = field(default_factory=dict)
    basis: str = INFERRED
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"total": round(self.total, 4), "basis": self.basis,
                "factors": self.factors, "notes": list(self.notes)}


@dataclass
class OpportunityDraft:
    """A candidate before it is persisted."""

    topic: str
    basis: str = INFERRED
    angle: str = ""
    audience: str = ""
    platforms: list[str] = field(default_factory=list)
    format: str = ""
    evidence: list[dict] = field(default_factory=list)
    effort_hours: float = 0.0
    cost_usd: float = 0.0
    priority_inputs: dict = field(default_factory=dict)
    competition: dict = field(default_factory=dict)


def forbidden_claims() -> tuple[str, ...]:
    """The claims scoring will never emit. Exposed so a test can assert it."""
    return FORBIDDEN_CLAIMS


def _factor(name: str, value: float | None, *, measured: bool,
            why: str) -> dict:
    """One factor with its measurement status and the reason for its value."""
    if value is None:
        return {"factor": name, "value": None, "weight": FACTOR_WEIGHTS.get(name, 0.0),
                "measured": False, "contribution": 0.0,
                "why": why or "no data; contributes 0 rather than a guess"}
    clamped = max(0.0, min(1.0, float(value)))
    return {"factor": name, "value": round(clamped, 4),
            "weight": FACTOR_WEIGHTS.get(name, 0.0),
            "measured": bool(measured),
            "contribution": round(clamped * FACTOR_WEIGHTS.get(name, 0.0), 4),
            "why": why}


def score_opportunity(draft: OpportunityDraft, *,
                      library_similarity: float | None = None,
                      saturation: float | None = None,
                      community_demand: int | None = None,
                      prior_performance: float | None = None,
                      brand_fit: float | None = None,
                      platform_fit: float | None = None,
                      recurrence: int | None = None,
                      freshness: str | None = None) -> OpportunityScore:
    """Score a draft. Any factor without data contributes 0 and says so.

    ``None`` means *not measured*. It never means zero-demand: a missing factor
    is reported as ``value: None`` with a "no data" reason, so the score is
    interpretable instead of quietly depressed.

    ``freshness`` defaults to ``None``, NOT to ``FRESH``. A band can only be
    computed from an actual observation, and defaulting it would award a
    perfect freshness score to an idea nobody has ever seen -- which is exactly
    the kind of unearned confidence this module exists to avoid.
    """
    factors: dict[str, dict] = {}
    notes: list[str] = []

    recurrence_value = (min(1.0, float(recurrence) / 3.0)
                        if recurrence is not None else None)
    factors["recurrence"] = _factor(
        "recurrence", recurrence_value, measured=recurrence is not None,
        why=(f"{recurrence} distinct observation(s) of this topic"
             if recurrence is not None else ""))

    freshness_value = (None if freshness is None
                       else {FRESH: 1.0, AGING: 0.6, STALE: 0.15}.get(freshness))
    factors["freshness"] = _factor(
        "freshness", freshness_value, measured=freshness is not None,
        why=(f"observation freshness band is {freshness}"
             if freshness is not None
             else "no observation, so freshness is unknown"))

    demand_value = (min(1.0, float(community_demand) / 10.0)
                    if community_demand is not None else None)
    factors["community_demand"] = _factor(
        "community_demand", demand_value, measured=community_demand is not None,
        why=(f"{community_demand} community request(s)/interactions"
             if community_demand is not None else
             "no community demand signal ingested for this topic"))

    factors["prior_performance"] = _factor(
        "prior_performance", prior_performance,
        measured=prior_performance is not None,
        why=("measured engagement on our own similar posts"
             if prior_performance is not None else
             "no prior performance on a similar topic"))

    factors["brand_fit"] = _factor(
        "brand_fit", brand_fit, measured=brand_fit is not None,
        why=("BrandDNA match against the topic"
             if brand_fit is not None else "no BrandDNA match computed"))

    factors["platform_fit"] = _factor(
        "platform_fit", platform_fit, measured=platform_fit is not None,
        why=("platform capability match for the proposed formats"
             if platform_fit is not None else "no platform fit computed"))

    # Novelty is the inverse of library similarity, so a topic already covered
    # scores LOWER novelty.
    novelty_value = (max(0.0, 1.0 - float(library_similarity))
                     if library_similarity is not None else None)
    factors["novelty"] = _factor(
        "novelty", novelty_value, measured=library_similarity is not None,
        why=(f"library similarity {float(library_similarity):.2f} (inverted)"
             if library_similarity is not None else
             "library not searched for this topic"))

    factors["saturation"] = _factor(
        "saturation", saturation, measured=saturation is not None,
        why=("recent posts on this topic/format by us"
             if saturation is not None else "saturation not measured"))

    total = sum(f["contribution"] for f in factors.values())
    if draft.basis == RECOMMENDED and total > RECOMMENDED_SCORE_CAP:
        notes.append(
            f"score capped at {RECOMMENDED_SCORE_CAP}: this is a RECOMMENDED "
            f"idea with no measured demand behind it")
        total = RECOMMENDED_SCORE_CAP
    if all(f["value"] is None for f in factors.values()):
        notes.append(
            "every factor is unmeasured: this score reflects structure only and "
            "carries no demand signal")
    return OpportunityScore(total=round(total, 4), factors=factors,
                            basis=draft.basis, notes=notes)


def build_opportunity(db, workspace_id: str, draft: OpportunityDraft, *,
                      cycle_id: str = "", source: str = "planner",
                      external_ref: str = "",
                      library_similarity: float | None = None,
                      saturation: float | None = None,
                      community_demand: int | None = None,
                      prior_performance: float | None = None,
                      brand_fit: float | None = None,
                      platform_fit: float | None = None,
                      as_of: datetime | None = None) -> tuple[Opportunity, dict]:
    """Persist one opportunity and return it with its scoring record."""
    from app.engine.planning.signals import normalize_topic

    topic_key = normalize_topic(draft.topic)
    # Match on the NORMALIZED topic, not the raw string. Comparing raw text let
    # "Budget Tips" and "budget tips" become two rows for the same subject, so a
    # re-planned topic could duplicate itself.
    existing = db.scalar(select(Opportunity).where(
        Opportunity.workspace_id == workspace_id,
        func.lower(Opportunity.topic) == draft.topic.lower()))
    if existing is None:
        # fall back to the token key for rewordings ("tips budget" == "budget tips")
        for candidate in db.scalars(select(Opportunity).where(
                Opportunity.workspace_id == workspace_id)).all():
            if normalize_topic(candidate.topic) == topic_key:
                existing = candidate
                break
    if existing is not None:
        # A re-planned topic updates rather than duplicating, which keeps the
        # plan reproducible from the same inputs.
        existing.topic = draft.topic
        existing.basis = draft.basis
        existing.angle = draft.angle or existing.angle
        existing.audience = draft.audience or existing.audience
        existing.platforms_json = list(draft.platforms or existing.platforms)
        existing.format = draft.format or existing.format
        existing.evidence_json = list(draft.evidence or existing.evidence)
        existing.brand_fit = float(brand_fit or 0.0)
        existing.competition_json = dict(draft.competition or {})
        existing.estimated_effort_hours = float(draft.effort_hours or 0.0)
        existing.estimated_cost_usd = float(draft.cost_usd or 0.0)
        existing.priority_inputs_json = dict(draft.priority_inputs)
        row = existing
    else:
        row = Opportunity(
            workspace_id=workspace_id, cycle_id=cycle_id or None,
            topic=draft.topic, source=source, external_ref=external_ref,
            raw_payload={}, score=0.0, components_json={},
            recommendation="WAIT", lifecycle="UNKNOWN", confidence=0.0,
            virality=0.0,            # never populated: see FORBIDDEN_CLAIMS
            basis=draft.basis, angle=draft.angle, audience=draft.audience,
            platforms_json=list(draft.platforms), format=draft.format,
            evidence_json=list(draft.evidence), brand_fit=float(brand_fit or 0.0),
            # overwritten below from the real signal; seeded empty so a crash
            # between here and the recompute cannot leave a fabricated FRESH
            freshness="", competition_json=dict(draft.competition),
            estimated_effort_hours=float(draft.effort_hours or 0.0),
            estimated_cost_usd=float(draft.cost_usd or 0.0),
            # the column, not the read-only property of the same name
            priority_inputs_json=dict(draft.priority_inputs))
        db.add(row)

    signals = claim_signals(db, workspace_id, topic=draft.topic, as_of=as_of)
    recurrence = topic_recurrence(db, workspace_id, topic_key, as_of=as_of) \
        if signals else 0
    freshness = (signal_freshness(signals[0].observed_at, now=as_of)
                 if signals else STALE)
    scored = score_opportunity(
        draft, library_similarity=library_similarity, saturation=saturation,
        community_demand=community_demand, prior_performance=prior_performance,
        brand_fit=brand_fit, platform_fit=platform_fit, recurrence=recurrence,
        freshness=freshness)
    row.score = scored.total
    row.components_json = scored.to_dict()
    row.confidence = (max([s.confidence for s in signals], default=0.0)
                      if signals else 0.0)
    row.freshness = freshness
    # The evidence behind the opportunity: the signals it was derived from.
    row.evidence_json = [
        {"signal_id": s.id, "source": s.source, "topic": s.topic,
         "observed_at": s.observed_at.isoformat(), "freshness": s.freshness,
         "confidence": s.confidence, "evidence_ids": s.evidence_ids,
         "recurrence": s.recurrence}
        for s in signals
    ] or list(draft.evidence or [])
    db.flush()
    return row, scored.to_dict()


def generate_opportunities(db, workspace_id: str, *,
                           as_of: datetime | None = None,
                           min_recurrence: int = 1,
                           library_lookup=None,
                           community_lookup=None,
                           brand_fit_fn=None) -> list[dict]:
    """Turn clustered signals into opportunities.

    One opportunity per normalized topic, and only where the topic has at
    least ``min_recurrence`` distinct observations — a single unverified
    mention is not an opportunity, it is an anecdote.
    """
    signals: list[SignalRead] = claim_signals(db, workspace_id, as_of=as_of)
    usable = [s for s in signals if s.evidence_ids]
    grouped: dict[str, list[SignalRead]] = {}
    for signal in usable:
        grouped.setdefault(signal.topic_key, []).append(signal)

    out: list[dict] = []
    for topic_key, group in sorted(grouped.items()):
        if len(group) < min_recurrence:
            continue
        newest = max(group, key=lambda s: s.observed_at)
        # OBSERVED when the demand was seen in real evidence; otherwise the
        # opportunity is an INFERENCE about that evidence.
        basis = OBSERVED if any(s.evidence_ids for s in group) else INFERRED
        draft = OpportunityDraft(
            topic=newest.topic, basis=basis,
            angle=f"cover {newest.topic}",
            audience="", platforms=[], format="SHORT",
            effort_hours=2.0, cost_usd=0.0)
        row, scored = build_opportunity(
            db, workspace_id, draft,
            source=newest.source,
            library_similarity=(library_lookup(workspace_id, newest.topic)
                                if library_lookup else None),
            community_demand=(community_lookup(workspace_id, newest.topic)
                              if community_lookup else None),
            brand_fit=(brand_fit_fn(newest.topic) if brand_fit_fn else None),
            as_of=as_of)
        out.append({"opportunity_id": row.id, "topic": row.topic,
                    "basis": row.basis, "score": row.score,
                    "recurrence": len({(s.source, s.external_ref)
                                       for s in group}),
                    "sources": sorted({s.source for s in group}),
                    "scoring": scored})
    return out


def recommend_basis(*, has_evidence: bool, is_ai_suggestion: bool) -> str:
    """The basis rule, as a function, so the rule itself is testable.

    The two questions are independent, and the label reports the WEAKER one:

    * is the demand itself something we observed?  -> OBSERVED
    * was this particular idea proposed by a model? -> not OBSERVED

    So a real, observed demand that a model happens to have proposed is still
    ``INFERRED``: the demand is measured but the *idea* is a guess, and a reader
    of the row should not be told the idea was measured.
    """
    if is_ai_suggestion:
        return RECOMMENDED if not has_evidence else INFERRED
    return OBSERVED if has_evidence else INFERRED
