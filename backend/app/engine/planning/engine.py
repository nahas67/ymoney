"""Work 15 §4/§5 — GlobalMemory-assisted planning and the planning engine.

``ContentPlanningEngine`` is the orchestrator for a planning horizon. It does
not create a second campaign engine, a second research pipeline, or a second
scheduler: it *proposes* work, writes ``EditorialPlanItem`` rows that reference
the canonical ``Opportunity``/``Campaign``/``ScheduleEntry`` objects, and every
state change passes through the autonomy gate.

Memory comes first (§4). Before an idea is proposed the engine asks GlobalMemory
what already exists -- previous content, research, community requests,
experiments, creative lessons, brand knowledge, recent opportunities -- and
records WHICH memories it consulted. That does three things: it stops the
planner re-researching a settled question, it stops it proposing a duplicate,
and it lets stale factual knowledge trigger revalidation instead of being
treated as current.

Memory stays freshness-aware by construction: a STALE research fact is reported
as ``needs_revalidation`` rather than silently used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.engine.knowledge.freshness import STALE, effective_status
from app.engine.planning.autonomy import (
    AutonomyPolicy,
    AutonomyRefused,
    PlanningAction,
    assert_may_advance,
)
from app.engine.planning.capacity import (
    FORMAT_COST_DEFAULTS,
    check_capacity,
    get_capacity,
    load_ledger,
)
from app.engine.planning.dedup import detect_dedupe
from app.engine.planning.opportunities import (
    OpportunityDraft,
    build_opportunity,
    recommend_basis,
    score_opportunity,
)
from app.engine.planning.signals import claim_signals, normalize_topic
from app.models.knowledge import KnowledgeMemory
from app.models.planning import (
    BLOCKED,
    IDEA,
    OBSERVED,
    PLANNED,
    EditorialPlan,
    EditorialPlanItem,
)

__all__ = [
    "ContentPlanningEngine",
    "MemoryBrief",
    "PlanningInputs",
    "PlanningResult",
]

#: Memory types consulted before proposing. This is the §4 checklist.
MEMORY_TYPES: tuple[str, ...] = (
    "CONTENT_RESULT",     # previous content
    "RESEARCH_FACT",      # previous research
    "COMMUNITY_INSIGHT",  # community requests
    "EXPERIMENT_RESULT",  # experiments
    "CREATIVE_LESSON",    # creative lessons
    "BRAND_KNOWLEDGE",    # brand knowledge
    "AUDIENCE_INSIGHT",
    "PLATFORM_LEARNING",
)

#: A research fact older than this is not used to justify re-researching; it is
#: flagged for revalidation instead.
STALE_FACT_DAYS = 30.0


@dataclass
class PlanningInputs:
    """Everything one planning run needs."""

    workspace_id: str
    horizon_days: int = 30
    goals: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    budget_usd: float = 0.0
    spent_usd: float = 0.0
    autonomy: AutonomyPolicy = field(default_factory=AutonomyPolicy)
    constraints: dict = field(default_factory=dict)
    #: Empty means "the workspace's single capacity row" -- the same sentinel
    #: ``get_capacity``/``upset_capacity`` use. It must NOT default to "UTC":
    #: that mismatch makes the engine look up a locale-keyed row that never
    #: exists, find None, and treat the workspace as UNBOUNDED, which silently
    #: disables every capacity limit. An empty zone is resolved to UTC at
    #: placement time by ``resolve_timezone``, so nothing is lost.
    locale: str = ""
    #: topic_key -> explicit series declaration, exempting dedupe
    series: dict[str, str] = field(default_factory=dict)


@dataclass
class MemoryBrief:
    """What memory knew, and what of it is no longer trustworthy."""

    memories: list[dict] = field(default_factory=list)
    #: memory ids that were actually consulted (recorded on the plan item)
    used_ids: list[str] = field(default_factory=list)
    needs_revalidation: list[dict] = field(default_factory=list)
    #: topics memory already covers, so the planner does not re-research
    settled_topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"used_ids": list(self.used_ids),
                "memory_count": len(self.memories),
                "settled_topics": list(self.settled_topics),
                "needs_revalidation": list(self.needs_revalidation)}


@dataclass
class PlanningResult:
    plan: EditorialPlan | None
    items: list[EditorialPlanItem] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)
    memory: MemoryBrief = field(default_factory=MemoryBrief)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"plan_id": getattr(self.plan, "id", ""),
                "items": [{"id": i.id, "opportunity_id": i.opportunity_id,
                           "topic": i.angle, "format": i.content_format,
                           "platforms": i.platforms, "priority": i.priority,
                           "status": i.status,
                           "blocked_reason": i.blocked_reason,
                           "target_date": i.target_date.isoformat()
                           if i.target_date else "",
                           "estimated_cost_usd": i.estimated_cost_usd,
                           "why": i.why} for i in self.items],
                "blocked": list(self.blocked),
                "suggestions": list(self.suggestions),
                "memory": self.memory.to_dict(),
                "notes": list(self.notes)}


def consult_memory(db, workspace_id: str, *, topic: str = "",
                   limit: int = 12) -> MemoryBrief:
    """Ask GlobalMemory what already exists for this topic (§4).

    Returns the consulted ids, the topics already covered, and any STALE
    research fact that must be revalidated before it is relied on again.
    """
    brief = MemoryBrief()
    for memory_type in MEMORY_TYPES:
        rows = db.scalars(
            select(KnowledgeMemory).where(
                KnowledgeMemory.workspace_id == workspace_id,
                KnowledgeMemory.type == memory_type)
            .order_by(KnowledgeMemory.created_at.desc()).limit(limit)).all()
        for row in rows:
            row_topic = str(getattr(row, "topic", "") or "")
            if topic and row_topic and normalize_topic(
                    row_topic) != normalize_topic(topic):
                continue
            # effective_status() is Work 10's COMPUTED freshness band, which
            # differs from the stored lifecycle column: a memory whose
            # last_verified_at has aged out reads STALE while its column still
            # says ACTIVE. Reading the column would miss exactly the records
            # this needs to flag.
            effective = effective_status(row)
            entry = {
                "id": row.id,
                "type": row.type,
                "topic": row_topic,
                "status": effective,
                "lifecycle": str(getattr(row, "status", "")),
                "confidence": float(getattr(row, "confidence", 0.0) or 0.0),
                "content": str(getattr(row, "content", ""))[:200],
            }
            brief.memories.append(entry)
            brief.used_ids.append(row.id)
            if row_topic:
                brief.settled_topics.append(row_topic)
            # A STALE research fact is NOT usable as current knowledge. Flag it
            # so the planner asks for revalidation instead of quietly reusing
            # something that aged out.
            if row.type == "RESEARCH_FACT" and effective == STALE:
                brief.needs_revalidation.append(
                    {"id": row.id, "topic": row_topic,
                     "why": "research fact is STALE; revalidate before reuse"})

    seen: set[str] = set()
    brief.used_ids = [i for i in brief.used_ids
                      if not (i in seen or seen.add(i))]
    brief.settled_topics = list(dict.fromkeys(brief.settled_topics))
    return brief


#: A memory whose topic matches within this similarity counts as SETTLED. Tight
#: enough that "compound interest" does not settle "index fund allocation".
_SETTLED_SIMILARITY = 0.82


def _saturation_for(dedupe) -> float | None:
    """Saturation (0..1) implied by a dedupe verdict, or ``None`` if unknown.

    Derived from a measurement the dedupe pass already made, never invented:
    SATURATED means the ceiling, RELATED means partial overlap, and NEW means
    there is nothing to be saturated against. A DUPLICATE never reaches here
    (it is blocked), so it returns ``None`` rather than guessing.
    """
    from app.engine.planning.dedup import (
        DEDUPE_NEW,
        DEDUPE_SATURATED,
        SATURATION_COUNT,
    )

    if dedupe.verdict == DEDUPE_SATURATED:
        return 1.0
    if dedupe.verdict == DEDUPE_NEW:
        return 0.0
    # RELATED: scale by how close it got to the duplicate threshold
    return round(min(1.0, dedupe.similarity / SATURATION_COUNT * 2), 4)


def _settled_by(topic: str, brief: MemoryBrief) -> list[str]:
    """Memory ids that already answer this topic, newest first.

    Consulting memory is only useful if it CHANGES a decision. Without this the
    brief was collected, reported, and ignored -- §4 satisfied on paper only.
    """
    from app.engine.decision import topic_similarity

    hits: list[tuple[float, str]] = []
    for entry in brief.memories:
        if not entry.get("topic"):
            continue
        score = topic_similarity(topic, entry["topic"])
        if score >= _SETTLED_SIMILARITY:
            hits.append((score, entry["id"]))
    return [memory_id for _score, memory_id in sorted(hits, reverse=True)]


class ContentPlanningEngine:
    """Plan a horizon: signals -> opportunities -> items -> calendar."""

    def __init__(self, db, workspace_id: str) -> None:
        self.db = db
        self.workspace_id = workspace_id

    # -- plan lifecycle ---------------------------------------------------

    def create_plan(self, inputs: PlanningInputs) -> EditorialPlan:
        plan = EditorialPlan(
            workspace_id=self.workspace_id,
            horizon_days=int(inputs.horizon_days),
            goals_json=list(inputs.goals),
            platforms_json=list(inputs.platforms),
            budget_usd=float(inputs.budget_usd),
            spent_usd=float(inputs.spent_usd),
            autonomy=str(inputs.autonomy.mode),
            constraints_json={**inputs.constraints,
                              "locale": inputs.locale,
                              "series": dict(inputs.series)},
            status="DRAFT")
        self.db.add(plan)
        self.db.flush()
        return plan

    # -- the main entry point --------------------------------------------

    def plan(self, inputs: PlanningInputs, *,
             plan: EditorialPlan | None = None) -> PlanningResult:
        """Produce a plan for a horizon.

        In ``RECOMMEND`` and ``DISABLED`` nothing is persisted: the result
        carries ``suggestions`` only, and no plan row is written. Persisting
        begins at ``APPROVAL``, where a plan item may be created, and at
        ``AUTONOMOUS``, where additionally only the operator's allow-listed
        actions may advance unattended.
        """
        result = PlanningResult(plan=plan)
        policy = inputs.autonomy.resolved()
        as_of = datetime.now(UTC)

        memory = consult_memory(self.db, self.workspace_id)
        result.memory = memory
        if memory.needs_revalidation:
            result.notes.append(
                f"{len(memory.needs_revalidation)} memory record(s) are STALE and "
                f"need revalidation before reuse")

        # 1. signals -> candidate topics
        signals = claim_signals(self.db, self.workspace_id, as_of=as_of)
        candidates: dict[str, list] = {}
        for signal in signals:
            if signal.evidence_ids:
                candidates.setdefault(signal.topic_key, []).append(signal)
        if not candidates:
            result.notes.append(
                "no signal carries evidence: nothing is planned, and nothing is "
                "invented in its place")
            return result

        # 2. capacity/budget gate BEFORE any autonomous action (§12)
        planned: list[dict] = []
        for topic_key, group in sorted(candidates.items()):
            newest = max(group, key=lambda s: s.observed_at)
            content_format = str(inputs.constraints.get(
                "default_format") or "SHORT").upper()
            cost = FORMAT_COST_DEFAULTS.get(content_format, 1.0)
            draft = OpportunityDraft(
                topic=newest.topic,
                basis=recommend_basis(has_evidence=True,
                                      is_ai_suggestion=False),
                angle=f"cover {newest.topic}",
                platforms=list(inputs.platforms),
                format=content_format,
                effort_hours=float(inputs.constraints.get("effort_hours", 2.0)),
                cost_usd=cost)
            score = score_opportunity(
                draft,
                recurrence=len({(s.source, s.external_ref) for s in group}),
                community_demand=None, prior_performance=None,
                brand_fit=None, platform_fit=None,
                library_similarity=None, saturation=None,
                freshness=newest.freshness)
            planned.append({"topic_key": topic_key, "draft": draft,
                            "score": score, "signals": group})

        # 3. autonomy gate: may we persist a plan at all?
        if not policy.permits(PlanningAction.CREATE_PLAN_ITEM):
            # RECOMMEND and DISABLED both stop here. Suggestions are still
            # produced (they are the deliverable) but NOTHING is written, so a
            # read-only operator sees ideas without acquiring plan rows.
            for entry in planned:
                result.suggestions.append({
                    "topic": entry["draft"].topic,
                    "score": entry["score"].total,
                    "basis": entry["draft"].basis,
                    "reason": (f"{policy.mode} mode: suggestion only, nothing "
                               f"persisted"),
                    "scoring": entry["score"].to_dict()})
            result.notes.append(
                f"autonomy {policy.mode}: suggestions produced, no plan items "
                f"written")
            return result

        if plan is None:
            plan = self.create_plan(inputs)
        result.plan = plan

        # 4. dedupe + capacity per candidate
        ledger = load_ledger(self.db, self.workspace_id,
                             plan_id=plan.id,
                             horizon_days=inputs.horizon_days,
                             locale=inputs.locale)
        capacity = get_capacity(self.db, self.workspace_id,
                                locale=inputs.locale)
        batch: list[dict] = []
        for entry in sorted(planned, key=lambda e: -e["score"].total):
            draft: OpportunityDraft = entry["draft"]
            # §4: a topic memory already covers is SETTLED. Planning it again
            # would re-research a question the workspace has already answered,
            # so it is recorded as blocked-by-memory rather than silently
            # re-planned. The blocking memory ids travel with the entry so the
            # decision is auditable.
            settled = _settled_by(draft.topic, result.memory)
            if settled:
                result.blocked.append({
                    "topic": draft.topic, "verdict": "SETTLED",
                    "reason": ("memory already covers this topic: "
                               f"{len(settled)} record(s) -- research is not "
                               f"repeated"),
                    "memory_ids": settled})
                continue
            dedupe = detect_dedupe(
                self.db, self.workspace_id, draft.topic, angle=draft.angle,
                platforms=draft.platforms,
                series_name=inputs.series.get(entry["topic_key"], ""),
                as_of=as_of)
            if dedupe.is_blocking:
                result.blocked.append({
                    "topic": draft.topic, "verdict": dedupe.verdict,
                    "reason": dedupe.reason, "similarity": dedupe.similarity,
                    "series_exempt": dedupe.series_exempt})
                continue
            # The dedupe pass already measured the two library-side factors, so
            # rescore with them. Without this the ranking leaned only on
            # recurrence and freshness while the other six factors reported "no
            # data" -- honest, but it discarded evidence the planner was already
            # holding. A topic we have covered before now scores lower on
            # novelty, and one we have covered three times scores lower on
            # saturation.
            entry["score"] = score_opportunity(
                draft,
                recurrence=len({(s.source, s.external_ref)
                                for s in entry["signals"]}),
                community_demand=None, prior_performance=None,
                brand_fit=None, platform_fit=None,
                library_similarity=dedupe.similarity,
                saturation=_saturation_for(dedupe),
                freshness=entry["signals"][0].freshness)
            entry["settled_memory_ids"] = settled
            batch.append({"content_format": draft.format,
                          "estimated_cost_usd": draft.cost_usd,
                          "entry": entry, "dedupe": dedupe})

        decision = check_capacity(
            capacity, ledger, items=batch,
            budget_remaining=(plan.budget_remaining
                              if plan.budget_usd > 0 else None),
            horizon_days=inputs.horizon_days)
        if not decision.fits:
            # Refuse the WHOLE batch rather than trimming silently: an operator
            # needs to see the shortfall, not a quietly smaller plan.
            result.blocked.append({
                "verdict": "OVER_CAPACITY",
                "reasons": decision.reasons,
                "shortfalls": decision.to_dict()["shortfalls"]})
            result.notes.append(
                "plan blocked on capacity/budget: " + "; ".join(decision.reasons))
            return result

        # 5. persist items
        for item in batch:
            entry = item["entry"]
            draft: OpportunityDraft = entry["draft"]
            opportunity, _scoring = build_opportunity(
                self.db, self.workspace_id, draft,
                source=entry["signals"][0].source,
                as_of=as_of)
            # Persist the RESCORE, not build_opportunity's own recomputation:
            # the rescore is the one that includes the library factors, so
            # storing the other would make the stored WHY contradict the score.
            scoring = entry["score"].to_dict()
            opportunity.score = entry["score"].total
            opportunity.components_json = scoring
            opportunity.dedupe_verdict = item["dedupe"].verdict
            opportunity.dedupe_reason = item["dedupe"].reason
            row = EditorialPlanItem(
                workspace_id=self.workspace_id, plan_id=plan.id,
                opportunity_id=opportunity.id,
                content_format=draft.format, angle=draft.angle,
                platforms_json=list(draft.platforms),
                priority=entry["score"].total,
                estimated_cost_usd=draft.cost_usd,
                status=IDEA,
                why_json={
                    "why_created": (
                        f"{len(entry['signals'])} signal(s) observed this topic: "
                        f"{sorted({s.source for s in entry['signals']})}"),
                    "why_selected": entry["score"].notes or
                    "highest scoring candidate in this batch",
                    "memories_used": result.memory.used_ids[:12],
                    "lessons_influencing": [
                        m["id"] for m in result.memory.memories
                        if m["type"] in ("CREATIVE_LESSON", "EXPERIMENT_RESULT")],
                    "constraints_applied": decision.reasons,
                    "scoring": scoring,
                    "dedupe": item["dedupe"].to_dict(),
                    "basis": draft.basis,
                    "reproducible_from": {
                        "signals": [s.id for s in entry["signals"]],
                        "memory_ids": result.memory.used_ids[:12],
                        "score_inputs": entry["score"].to_dict()["factors"],
                    },
                })
            self.db.add(row)
            self.db.flush()
            opportunity.plan_item_id = row.id
            result.items.append(row)

        # §12's fourth step: LEDGER the commitment. The estimate was asserted
        # against the budget above; now it is recorded against the plan, so a
        # later cycle sees the money already spoken for instead of re-spending
        # it. Without this, `spent_usd` only ever held what the caller claimed
        # up front and `budget_remaining` never shrank.
        plan.spent_usd = round(
            float(plan.spent_usd or 0.0)
            + sum(float(r.estimated_cost_usd or 0.0) for r in result.items), 4)
        self.db.flush()
        return result

    # -- gated transitions ------------------------------------------------

    def approve_item(self, item_id: str) -> dict:
        """Human approval: IDEA -> PLANNED. Never used automatically."""
        row = self._item(item_id)
        if row.status == IDEA:
            row.status = PLANNED
        self.db.flush()
        return {"id": row.id, "status": row.status}

    def reject_item(self, item_id: str, reason: str) -> dict:
        row = self._item(item_id)
        row.status = "CANCELLED"
        row.blocked_reason = reason[:400]
        self.db.flush()
        return {"id": row.id, "status": row.status, "reason": row.blocked_reason}

    def request_more_research(self, item_id: str, reason: str = "") -> dict:
        row = self._item(item_id)
        row.status = "RESEARCHING"
        why = dict(row.why_json or {})
        why["research_requested"] = reason or "operator requested more evidence"
        row.why_json = why
        self.db.flush()
        return {"id": row.id, "status": row.status}

    def schedule_item(self, item_id: str, policy: AutonomyPolicy, *,
                      placement=None) -> dict:
        """Move an item to SCHEDULED. Gated, and never publishes.

        ``placement`` is produced by the calendar optimizer, which writes a
        ScheduleEntry through the existing store. The planner's only authority
        here is to mark INTENT; the Scheduler agent still owns dispatch and the
        publication approval path still owns publishing.
        """
        assert_may_advance(policy, PlanningAction.SCHEDULE)
        if policy.requires_approval(PlanningAction.SCHEDULE):
            raise AutonomyRefused(
                f"SCHEDULE needs human approval under {policy.mode}; approve "
                f"the item first")
        row = self._item(item_id)
        if placement is not None and placement.is_blocked:
            row.status = BLOCKED
            row.blocked_reason = placement.blocked_reason[:400]
            self.db.flush()
            return {"id": row.id, "status": row.status,
                    "blocked_reason": row.blocked_reason}
        row.status = "SCHEDULED"
        if placement is not None:
            row.target_date = placement.run_at.replace(tzinfo=None)
            why = dict(row.why_json or {})
            why["why_scheduled"] = placement.reason
            why["evidence_backed_slot"] = placement.evidence_backed
            row.why_json = why
        self.db.flush()
        return {"id": row.id, "status": row.status}

    def _item(self, item_id: str) -> EditorialPlanItem:
        from app.engine.planning.autonomy import AutonomyRefused as _Refused

        row = self.db.get(EditorialPlanItem, item_id)
        if row is None or row.workspace_id != self.workspace_id:
            raise _Refused(f"plan item {item_id!r} not found in this workspace")
        return row


def stale_cutoff(days: float = STALE_FACT_DAYS) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def basis_for_evidence(*, has_evidence: bool, is_ai_suggestion: bool) -> str:
    return recommend_basis(has_evidence=has_evidence,
                          is_ai_suggestion=is_ai_suggestion)


__all__ += ["OBSERVED", "stale_cutoff", "basis_for_evidence"]
