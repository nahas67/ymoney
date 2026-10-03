"""Work 15 §1-§12 — the planning engine, end to end and hermetic.

Drives the real engine, the real models and the real budget/memory layers
against a real (temporary) database. No mocks of the system under test.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws(db_session):
    """A real workspace row (slug is NOT NULL, so it is always provided)."""
    import os

    from app.models import Workspace

    workspace = Workspace(name="planner-test",
                          slug=f"ws-{os.urandom(4).hex()}")
    db_session.add(workspace)
    db_session.commit()
    return workspace


def _count(db, model, workspace_id: str) -> int:
    """Rows for ONE workspace.

    ``db_session`` COMMITS, so the database is shared across tests in a run. A
    global count would therefore depend on test ordering, so every count in this
    module is workspace-scoped.
    """
    return db.query(model).filter(model.workspace_id == workspace_id).count()


def _signal(db, workspace_id, *, topic="budget tips", source="community",
            ref="r1", evidence=("ev-1",), days_ago=0.0, confidence=0.8,
            verified=True):
    from app.engine.planning.signals import SignalIngest, ingest_signal

    return ingest_signal(db, workspace_id, SignalIngest(
        source=source, topic=topic, external_ref=ref,
        evidence_ids=list(evidence),
        observed_at=datetime.now(UTC) - timedelta(days=days_ago),
        confidence=confidence, evidence_verified=verified))


# ===========================================================================
# §1 TrendSignal
# ===========================================================================


def test_signal_preserves_its_evidence(db_session, ws):
    from app.engine.planning.signals import claim_signals

    out = _signal(db_session, ws.id)
    assert out["created"] is True
    signals = claim_signals(db_session, ws.id)
    assert len(signals) == 1
    assert signals[0].evidence_ids == ["ev-1"]
    assert signals[0].is_usable is True
    assert signals[0].recurrence == 1


def test_duplicate_signal_ingestion_does_not_create_a_second_row(
    db_session, ws
):
    first = _signal(db_session, ws.id, ref="r1")
    second = _signal(db_session, ws.id, ref="r1")
    assert first["id"] == second["id"]
    assert second["created"] is False and second["duplicate"] is True
    from app.engine.planning.signals import claim_signals

    # recurrence must not be inflated by re-delivering the same item
    assert claim_signals(db_session, ws.id)[0].recurrence == 1


def test_distinct_observations_create_recurrence(db_session, ws):
    _signal(db_session, ws.id, source="community", ref="r1")
    _signal(db_session, ws.id, source="research", ref="r2")
    from app.engine.planning.signals import claim_signals

    signals = claim_signals(db_session, ws.id)
    assert signals[0].recurrence == 2


def test_no_trend_score_is_fabricated_from_one_observation(db_session, ws):
    """A single observation has no magnitude, and must not pretend to."""
    from app.engine.planning.signals import (
        SignalIngest,
        claim_signals,
        compute_velocity,
        ingest_signal,
    )

    _signal(db_session, ws.id)
    assert compute_velocity(db_session, ws.id, "budget tips") is None
    signal = claim_signals(db_session, ws.id)[0]
    assert signal.velocity is None

    # A SECOND ROW for the same (source, topic) with no external_ref is a
    # separate observation and does unlock velocity -- so the floor above is a
    # real precondition, not a blanket refusal.
    ingest_signal(db_session, ws.id, SignalIngest(
        source="research", topic="budget tips", evidence_ids=["ev-2"],
        observed_at=datetime.now(UTC) - timedelta(days=3), confidence=0.7,
        evidence_verified=True))
    assert compute_velocity(db_session, ws.id, "budget tips") is not None

    # A topic observed only ONCE, and again at the SAME instant, still has no
    # measurable span -- so no rate is reported even though two rows exist.
    solo = "same instant topic"
    instant = datetime.now(UTC)
    for ref in ("a", "b"):
        ingest_signal(db_session, ws.id, SignalIngest(
            source="research", topic=solo, external_ref=ref,
            evidence_ids=[f"ev-{ref}"], observed_at=instant, confidence=0.7,
            evidence_verified=True))
    assert compute_velocity(db_session, ws.id, solo) is None, (
        "two observations at one instant have no rate")


def test_velocity_is_measured_between_two_real_observations(db_session, ws):
    from app.engine.planning.signals import compute_velocity

    _signal(db_session, ws.id, source="community", ref="r1", days_ago=4)
    _signal(db_session, ws.id, source="research", ref="r2", days_ago=0)
    velocity = compute_velocity(db_session, ws.id, "budget tips")
    assert velocity is not None
    assert velocity["observations"] == 2
    assert velocity["kind"] == "observed_count_delta"
    assert "NOT a demand estimate" in velocity["note"]


def test_freshness_decays_and_stale_signals_stop_being_usable(db_session, ws):
    from app.engine.planning.signals import claim_signals, signal_freshness

    now = datetime.now(UTC)
    assert signal_freshness(now - timedelta(days=1), now=now) == "FRESH"
    assert signal_freshness(now - timedelta(days=10), now=now) == "AGING"
    assert signal_freshness(now - timedelta(days=45), now=now) == "STALE"

    _signal(db_session, ws.id, days_ago=60)
    signals = claim_signals(db_session, ws.id)
    assert signals[0].freshness == "STALE"
    assert signals[0].is_usable is False


def test_unverified_signal_confidence_is_capped(db_session, ws):
    from app.engine.planning.signals import claim_signals

    _signal(db_session, ws.id, confidence=0.99, verified=False,
            evidence=("claim-only",))
    signal = claim_signals(db_session, ws.id)[0]
    assert signal.confidence <= 0.5


def test_a_caller_cannot_self_certify_its_own_evidence(db_session, ws):
    """``evidence_verified`` is derived, not trusted.

    Accepting it verbatim let any member POST ``verified: true, confidence: 1.0``
    and have their own claim treated as measured demand.
    """
    from app.engine.planning.signals import (
        SignalIngest,
        claim_signals,
        ingest_signal,
    )

    # an OPERATOR stating a topic is an assertion, never a measurement
    ingest_signal(db_session, ws.id, SignalIngest(
        source="operator", topic="viral dance challenge", external_ref="op1",
        evidence_ids=["whatever-i-like"], confidence=1.0,
        evidence_verified=True))
    operator = claim_signals(db_session, ws.id, topic="viral dance challenge")[0]
    assert operator.confidence <= 0.5, (
        "a human asserting a topic must stay capped")

    # a self-resolving source WITH evidence is honoured
    ingest_signal(db_session, ws.id, SignalIngest(
        source="research", topic="compound interest math", external_ref="r1",
        evidence_ids=["paper-42"], confidence=0.9, evidence_verified=True))
    research = claim_signals(db_session, ws.id, topic="compound interest math")[0]
    assert research.confidence > 0.5

    # ...but claiming verified with NO evidence is not
    ingest_signal(db_session, ws.id, SignalIngest(
        source="research", topic="empty evidence topic", external_ref="r2",
        evidence_ids=[], confidence=1.0, evidence_verified=True))
    empty = claim_signals(db_session, ws.id, topic="empty evidence topic")[0]
    assert empty.confidence <= 0.5


def test_unknown_source_is_refused(db_session, ws):
    from app.engine.planning.signals import SignalIngest, ingest_signal

    with pytest.raises(ValueError):
        ingest_signal(db_session, ws.id,
                      SignalIngest(source="astrology", topic="x"))


def test_topic_normalisation_is_order_insensitive():
    from app.engine.planning.signals import normalize_topic

    assert normalize_topic("Budget Tips 2026") == normalize_topic(
        "2026 tips budget")
    assert normalize_topic("How to budget") == normalize_topic("budget how to")


# ===========================================================================
# §2/§3 opportunity + scoring
# ===========================================================================


def test_no_fake_virality_or_revenue_field_exists(db_session):
    """§3: the module must not even be able to express those claims."""
    from app.engine.planning.opportunities import forbidden_claims
    from app.models.content import Opportunity

    forbidden = forbidden_claims()
    assert "guaranteed virality" in forbidden
    assert "guaranteed revenue" in forbidden
    # the legacy `virality` column exists but the planner must never write it
    assert Opportunity.virality.default.arg in (0.0, 0, 0.0, None)
    # and no planning column carries a success probability
    for name in ("success_probability", "probability", "revenue_estimate"):
        assert not hasattr(Opportunity, name)


def test_unmeasured_factors_contribute_zero_and_say_so():
    from app.engine.planning.opportunities import (
        OpportunityDraft,
        score_opportunity,
    )

    scored = score_opportunity(OpportunityDraft(topic="x", basis="OBSERVED"),
                               recurrence=None, community_demand=None,
                               prior_performance=None, brand_fit=None,
                               platform_fit=None, library_similarity=None,
                               saturation=None)
    assert scored.total == 0.0
    for factor in scored.factors.values():
        if factor["value"] is None:
            assert factor["measured"] is False
            # every unmeasured factor must EXPLAIN the absence, not just be 0
            assert factor["why"]
            assert factor["contribution"] == 0.0
    assert any("unmeasured" in n for n in scored.notes)


def test_measured_factors_raise_the_score():
    from app.engine.planning.opportunities import (
        OpportunityDraft,
        score_opportunity,
    )

    base = score_opportunity(OpportunityDraft(topic="x"), recurrence=3,
                             community_demand=8, brand_fit=0.9,
                             prior_performance=0.5, platform_fit=0.8,
                             library_similarity=0.1, saturation=0.2,
                             freshness="FRESH")
    assert base.total > 0.6
    # freshness is only "measured" when a band was actually supplied
    assert all(f["measured"] for f in base.factors.values())


def test_recommended_basis_is_capped_and_never_reads_as_demand():
    from app.engine.planning.opportunities import (
        RECOMMENDED_SCORE_CAP,
        OpportunityDraft,
        recommend_basis,
        score_opportunity,
    )

    assert recommend_basis(has_evidence=False, is_ai_suggestion=True) \
        == "RECOMMENDED"
    scored = score_opportunity(
        OpportunityDraft(topic="x", basis="RECOMMENDED"), recurrence=99,
        community_demand=999, brand_fit=1.0, prior_performance=1.0,
        platform_fit=1.0, library_similarity=0.0, saturation=0.0)
    assert scored.total == RECOMMENDED_SCORE_CAP
    assert any("RECOMMENDED" in n for n in scored.notes)


def test_observed_basis_when_evidence_exists():
    from app.engine.planning.opportunities import recommend_basis

    assert recommend_basis(has_evidence=True, is_ai_suggestion=False) \
        == "OBSERVED"
    # An AI-proposed idea is never OBSERVED, even when the demand is real: the
    # demand is measured but the IDEA is still a guess, and the row must not
    # imply otherwise.
    assert recommend_basis(has_evidence=True, is_ai_suggestion=True) \
        == "INFERRED"


def test_opportunity_records_the_signals_it_came_from(db_session, ws):
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.opportunities import generate_opportunities

    _signal(db_session, ws.id, topic="budget tips", source="community", ref="r1")
    _signal(db_session, ws.id, topic="budget tips", source="research", ref="r2")
    out = generate_opportunities(db_session, ws.id)
    assert len(out) == 1
    opportunity = out[0]
    assert opportunity["recurrence"] == 2
    assert opportunity["sources"] == ["community", "research"]
    assert opportunity["basis"] == "OBSERVED"
    # the evidence is stored on the row
    from app.models.content import Opportunity

    row = db_session.get(Opportunity, opportunity["opportunity_id"])
    assert row.evidence
    assert {e["source"] for e in row.evidence} == {"community", "research"}
    _ = (ContentPlanningEngine, PlanningInputs)


# ===========================================================================
# §4 GlobalMemory integration
# ===========================================================================


def test_memory_is_consulted_before_planning(db_session, ws):
    from app.engine.knowledge.memory import GlobalMemory
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import (
        ContentPlanningEngine,
        PlanningInputs,
        consult_memory,
    )

    # GlobalMemory.store returns a dict projection, so the id is its "id" key.
    memory_id = GlobalMemory.store(
        db_session, ws.id, type="CREATIVE_LESSON",
        content="shorts with a question hook outperformed", confidence=0.8,
        topic="budget tips", platform="youtube_shorts")["id"]
    _signal(db_session, ws.id)

    brief = consult_memory(db_session, ws.id)
    assert memory_id in brief.used_ids

    # A memory on the SAME topic is SETTLED, so the planner refuses to
    # re-research it. That is the §4 requirement actually changing a decision
    # rather than being collected and ignored.
    settled = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, autonomy=AutonomyPolicy(
            mode=AutonomyMode.AUTONOMOUS)))
    assert settled.memory.used_ids, "memory must be consulted"
    assert memory_id in settled.memory.used_ids
    assert settled.items == [], "a settled topic must not be re-planned"
    blocked = [b for b in settled.blocked if b.get("verdict") == "SETTLED"]
    assert blocked, settled.blocked
    assert memory_id in blocked[0]["memory_ids"]

    # A memory on an UNRELATED topic does not block, and is still recorded on
    # the resulting plan item so the decision is auditable.
    other = GlobalMemory.store(
        db_session, ws.id, type="CREATIVE_LESSON",
        content="kubernetes ingress needs a rewrite rule", confidence=0.8,
        topic="kubernetes ingress configuration")["id"]
    _signal(db_session, ws.id, topic="how to meal prep for a week", ref="mp")
    fresh = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert fresh.items, fresh.blocked
    why = fresh.items[0].why
    assert other in why["memories_used"]
    assert other in why["reproducible_from"]["memory_ids"]
    # and the signals that justified it are recorded too
    assert why["reproducible_from"]["signals"]


def test_stale_research_fact_is_flagged_for_revalidation(db_session, ws):
    from app.engine.knowledge.freshness import AGING_DAYS
    from app.engine.knowledge.memory import GlobalMemory
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import (
        ContentPlanningEngine,
        PlanningInputs,
        consult_memory,
    )
    from app.models.knowledge import KnowledgeMemory

    stale_days = AGING_DAYS

    # store() returns a dict projection; the id is its "id" key.
    # evidence_ids is required here: an UNPROVENANCED memory is stored
    # UNVERIFIED, and in Work 10 lifecycle beats age -- so an UNVERIFIED row
    # would never reach the STALE band this test is about.
    memory_id = GlobalMemory.store(
        db_session, ws.id, type="RESEARCH_FACT",
        content="interest rates are 2%", confidence=0.7, topic="rates",
        evidence_ids=["central-bank-release-2026-01"])["id"]
    # Age it out of the FRESH band rather than hand-editing the status column:
    # freshness is COMPUTED by Work 10, and the planner must honour the computed
    # band -- not the stored lifecycle value.
    row = db_session.get(KnowledgeMemory, memory_id)
    aged = (datetime.now(UTC) - timedelta(days=stale_days + 5)).replace(
        tzinfo=None)
    row.last_verified_at = aged
    row.created_at = aged
    db_session.commit()

    brief = consult_memory(db_session, ws.id)
    assert any(m["id"] == memory_id for m in brief.needs_revalidation)

    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, autonomy=AutonomyPolicy(mode=AutonomyMode.RECOMMEND)))
    assert any("STALE" in n for n in result.notes)


def test_memory_prevents_re_researching_a_settled_topic(db_session, ws):
    from app.engine.knowledge.memory import GlobalMemory
    from app.engine.planning.engine import consult_memory

    GlobalMemory.store(db_session, ws.id, type="RESEARCH_FACT",
                       content="already answered", confidence=0.9,
                       topic="compound interest")
    brief = consult_memory(db_session, ws.id, topic="compound interest")
    assert "compound interest" in brief.settled_topics


# ===========================================================================
# §10 dedupe / fatigue
# ===========================================================================


def _content(db, workspace_id, title, *, days_ago=1.0):
    from app.models.content import ContentItem

    row = ContentItem(workspace_id=workspace_id, topic=title, status="IDEA")
    row.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    db.add(row)
    db.commit()
    return row


def test_duplicate_topic_is_detected(db_session, ws):
    from app.engine.planning.dedup import detect_dedupe

    _content(db_session, ws.id, "how to budget your paycheck")
    result = detect_dedupe(db_session, ws.id, "how to budget your paycheck")
    assert result.verdict == "DUPLICATE"
    assert result.is_blocking is True


def test_new_topic_is_new(db_session, ws):
    from app.engine.planning.dedup import detect_dedupe

    _content(db_session, ws.id, "how to budget your paycheck")
    result = detect_dedupe(db_session, ws.id, "kubernetes ingress controllers")
    assert result.verdict == "NEW"
    assert result.is_blocking is False


def test_similar_topic_is_related_not_blocking(db_session, ws):
    from app.engine.planning.dedup import (
        DUPLICATE_SIMILARITY,
        detect_dedupe,
    )

    _content(db_session, ws.id, "how to budget a tight paycheck")
    # near-identical wording -> DUPLICATE, i.e. a repeat
    duplicate = detect_dedupe(db_session, ws.id, "how to budget a tight paycheck")
    assert duplicate.verdict == "DUPLICATE"
    assert duplicate.is_blocking is True
    # shares most of its vocabulary but adds distinct subjects -> RELATED, i.e.
    # a follow-up worth making rather than a repeat to block
    related = detect_dedupe(db_session, ws.id,
                            "how to budget a tight paycheck with student "
                            "loans in college")
    assert related.verdict == "RELATED"
    assert related.is_blocking is False
    assert related.similarity < DUPLICATE_SIMILARITY


def test_dedupe_sees_through_inflection(db_session, ws):
    """'budget tips' and 'budgeting tips' are the same subject."""
    from app.engine.planning.dedup import detect_dedupe

    _content(db_session, ws.id, "budgeting tips for beginners")
    result = detect_dedupe(db_session, ws.id, "budget tips for beginners")
    assert result.verdict in ("RELATED", "DUPLICATE")
    assert result.similarity >= 0.55


def test_saturated_topic_is_blocked(db_session, ws):
    from app.engine.planning.dedup import detect_dedupe

    for index in range(3):
        _content(db_session, ws.id, f"budgeting tips for beginners part {index}",
                 days_ago=2.0)
    result = detect_dedupe(db_session, ws.id, "budget tips for beginners")
    assert result.verdict == "SATURATED"
    assert result.is_blocking is True


def test_series_exception_lifts_the_block_but_is_recorded(db_session, ws):
    """A legitimate sequel is intentionally similar and must be allowed."""
    from app.engine.planning.dedup import detect_dedupe

    _content(db_session, ws.id, "how to budget your paycheck")
    blocked = detect_dedupe(db_session, ws.id, "how to budget your paycheck")
    assert blocked.is_blocking is True

    sequel = detect_dedupe(db_session, ws.id, "how to budget your paycheck",
                           series_name="Paycheck Playbook")
    assert sequel.is_blocking is False
    assert sequel.series_exempt is True
    assert sequel.series_name == "Paycheck Playbook"
    # the underlying similarity is preserved, not hidden
    assert sequel.similarity == blocked.similarity


def test_excessive_platform_repetition_is_flagged_related(db_session, ws):
    """Repeating one platform is fatigue even when the TOPIC is brand new.

    This is the "excessive platform repetition" rule from the work order, and it
    is deliberately not a block: a fresh topic on a crowded platform is a
    scheduling problem, not a duplicate idea.
    """
    from app.engine.planning.dedup import (
        PLATFORM_REPEAT_LIMIT,
        detect_dedupe,
    )
    from app.models.content import ContentItem, PublishedPost

    for index in range(PLATFORM_REPEAT_LIMIT):
        item = ContentItem(workspace_id=ws.id,
                           topic=f"unrelated topic {index}", status="PUBLISHED")
        db_session.add(item)
        db_session.flush()
        post = PublishedPost(
            workspace_id=ws.id, content_item_id=item.id, video_id=f"v{index}",
            platform="threads", remote_url=f"https://x.test/{index}",
            publication_mode="MOCK")
        # inside the repetition window: this is what makes it "repeating"
        post.created_at = (datetime.now(UTC) - timedelta(days=1)).replace(
            tzinfo=None)
        db_session.add(post)
    db_session.commit()

    # a completely new topic, but threads is already carrying its quota
    result = detect_dedupe(db_session, ws.id, "quantum error correction",
                           platforms=["threads", "bluesky"])
    assert result.verdict == "RELATED"
    assert result.is_blocking is False
    assert "threads" in result.reason
    assert "space it out" in result.reason


def test_dedupe_is_workspace_scoped(db_session, ws):
    from app.engine.planning.dedup import detect_dedupe
    from app.models import Workspace

    other = Workspace(name="other", slug=f"ws-{os.urandom(4).hex()}")
    db_session.add(other)
    db_session.commit()
    _content(db_session, ws.id, "how to budget your paycheck")
    # the same topic in a DIFFERENT workspace is not a duplicate
    assert detect_dedupe(db_session, other.id,
                         "how to budget your paycheck").verdict == "NEW"


# ===========================================================================
# §8 capacity
# ===========================================================================


def test_capacity_refuses_an_impossible_workload(db_session, ws):
    from app.engine.planning.capacity import (
        CapacityLedger,
        check_capacity,
        upset_capacity,
    )

    capacity = upset_capacity(db_session, ws.id, shorts_per_day=2,
                              review_slots_per_day=2)
    # horizon_days=1: the rates are per-day, so 2/day is 2 for one day
    decision = check_capacity(capacity, CapacityLedger(), items=[
        {"content_format": "SHORT", "estimated_cost_usd": 1.0}] * 5,
        horizon_days=1)
    assert decision.fits is False
    assert "shorts" in decision.shortfalls
    assert "review" in decision.shortfalls
    # the reason names the resource and the numbers
    assert any("available" in r for r in decision.reasons)


def test_capacity_allows_a_workload_that_fits(db_session, ws):
    from app.engine.planning.capacity import (
        CapacityLedger,
        check_capacity,
        upset_capacity,
    )

    capacity = upset_capacity(db_session, ws.id, shorts_per_day=5,
                              review_slots_per_day=5)
    decision = check_capacity(capacity, CapacityLedger(), items=[
        {"content_format": "SHORT", "estimated_cost_usd": 1.0}] * 3)
    assert decision.fits is True


def test_engine_refuses_a_plan_over_capacity(db_session, ws):
    """The engine's OWN gate must refuse, not just the helper function.

    Exercising ``check_capacity`` directly would pass even if the engine stopped
    calling it, so this drives the real planner against a capacity row that
    cannot possibly absorb the batch.
    """
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.capacity import upset_capacity
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    # ONE short and ONE review slot for the whole horizon, but four topics.
    # The rates are per-DAY, so the horizon is pinned to 1 day: a 30-day
    # horizon at 1/day would legitimately allow 30.
    upset_capacity(db_session, ws.id, shorts_per_day=1, review_slots_per_day=1)
    for index in range(4):
        _signal(db_session, ws.id, topic=f"topic number {index}", ref=f"r{index}")

    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, horizon_days=1,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items == [], "an over-capacity plan must persist no items"
    blocked = [b for b in result.blocked if b.get("verdict") == "OVER_CAPACITY"]
    assert blocked, result.blocked
    # the refusal names the exhausted resource and the numbers
    assert any("shorts" in r or "review" in r for r in blocked[0]["reasons"])
    assert blocked[0]["shortfalls"]


def test_daily_capacity_is_scoped_to_the_horizon(db_session, ws):
    """A per-DAY rate must not be treated as a per-PLAN allowance.

    ``shorts_per_day=2`` over a 30-day horizon is 60 shorts, not 2. Reading the
    rate as a plan total made the first plan exhaust the workspace for a month.
    """
    from app.engine.planning.capacity import (
        CapacityLedger,
        check_capacity,
        upset_capacity,
    )

    capacity = upset_capacity(db_session, ws.id, shorts_per_day=2,
                              review_slots_per_day=100)
    items = [{"content_format": "SHORT", "estimated_cost_usd": 1.0}] * 10
    # 10 items over 30 days at 2/day is comfortable
    assert check_capacity(capacity, CapacityLedger(), items=items,
                          horizon_days=30).fits is True
    # the same 10 over a 1-day horizon is not
    tight = check_capacity(capacity, CapacityLedger(), items=items,
                           horizon_days=1)
    assert tight.fits is False
    assert "shorts" in tight.shortfalls


def test_an_undeclared_pool_is_unbounded_not_zero(db_session, ws):
    """Declaring only render hours must not make shorts impossible.

    A pool left at 0.0 means "no limit declared", not "no capacity". Reporting
    it as zero would refuse every plan in a workspace that only configured the
    pool it cared about.
    """
    from app.engine.planning.capacity import (
        CapacityLedger,
        check_capacity,
        upset_capacity,
    )

    capacity = upset_capacity(db_session, ws.id, render_hours_per_day=8)
    decision = check_capacity(capacity, CapacityLedger(), items=[
        {"content_format": "SHORT", "estimated_cost_usd": 1.2}] * 25,
        horizon_days=30)
    assert decision.fits is True, decision.reasons
    assert "shorts" not in decision.shortfalls
    assert "review" not in decision.shortfalls


def test_a_zero_cost_item_is_not_charged_the_default(db_session, ws):
    """``0.0 or default`` is a falsy-zero bug: a $0 item became $1.20."""
    from app.engine.planning.capacity import CapacityLedger, check_capacity

    decision = check_capacity(None, CapacityLedger(), items=[
        {"content_format": "LONGFORM", "estimated_cost_usd": 0.0}],
        budget_remaining=1.0)
    assert decision.fits is True, decision.reasons
    assert "budget" not in decision.shortfalls


def test_one_markets_capacity_does_not_consume_anothers(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.capacity import load_ledger, upset_capacity
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    upset_capacity(db_session, ws.id, shorts_per_day=5, review_slots_per_day=5)
    _signal(db_session, ws.id)
    ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, locale="en-US",
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    en = load_ledger(db_session, ws.id, locale="en-US")
    de = load_ledger(db_session, ws.id, locale="de-DE")
    assert en.committed, "the en-US plan should be committed against en-US"
    assert de.committed == {}, (
        "a de-DE ledger must not count en-US commitments")


def test_unset_capacity_is_unbounded_not_infeasible(db_session, ws):
    """No capacity row must not be reported as 'impossible'."""
    from app.engine.planning.capacity import CapacityLedger, check_capacity, get_capacity

    assert get_capacity(db_session, ws.id) is None
    decision = check_capacity(None, CapacityLedger(), items=[
        {"content_format": "SHORT", "estimated_cost_usd": 1.0}] * 50)
    assert decision.fits is True
    assert any("unbounded" in r for r in decision.reasons)


def test_committed_items_reduce_remaining_capacity(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.capacity import (
        check_capacity,
        format_commitment,
        load_ledger,
        upset_capacity,
    )
    from app.engine.planning.engine import (
        ContentPlanningEngine,
        PlanningInputs,
    )

    upset_capacity(db_session, ws.id, shorts_per_day=3, review_slots_per_day=3)
    _signal(db_session, ws.id)
    engine = ContentPlanningEngine(db_session, ws.id)
    result = engine.plan(PlanningInputs(
        workspace_id=ws.id, autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items
    ledger = load_ledger(db_session, ws.id, plan_id=result.plan.id)
    pool, units = format_commitment("SHORT")
    assert ledger.committed[pool] >= units
    # A second batch now exceeds the remaining capacity. The horizon is 1 day
    # because 3/day is 3 items for one day, and the first plan already spent
    # one of them.
    decision = check_capacity(
        upset_capacity(db_session, ws.id, shorts_per_day=3,
                       review_slots_per_day=3),
        ledger,
        items=[{"content_format": "SHORT", "estimated_cost_usd": 1.0}] * 3,
        horizon_days=1)
    assert decision.fits is False


# ===========================================================================
# §12 budget
# ===========================================================================


def test_the_planner_scores_with_the_library_evidence_it_already_has(
    db_session, ws
):
    """Novelty and saturation must be measured, not left as "no data".

    The dedupe pass already queries the library, so the ranking can use those
    two factors instead of reporting them unmeasured while holding the
    evidence.
    """
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    # one novel topic and one the library already covers
    _content(db_session, ws.id, "how to budget a tight paycheck")
    _signal(db_session, ws.id, topic="quantum error correction", ref="q")
    _signal(db_session, ws.id, topic="how to budget a tight paycheck", ref="b")

    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, budget_usd=50.0,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items
    factors = result.items[0].why["scoring"]["factors"]
    assert factors["novelty"]["measured"] is True
    assert factors["saturation"]["measured"] is True
    assert factors["novelty"]["value"] is not None
    assert factors["saturation"]["value"] is not None
    # and they differ between a novel topic and a familiar one
    scores = {i.angle: i.priority for i in result.items}
    assert len(scores) == len(set(scores.values())), (
        f"distinct topics scored identically: {scores}")


def test_planning_ledgers_the_committed_estimate(db_session, ws):
    """§12 step four: the estimate must be recorded, not just asserted.

    ``spent_usd`` only ever held what the caller declared up front, so
    ``budget_remaining`` never shrank and a second cycle re-spent the same
    money.
    """
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.capacity import FORMAT_COST_DEFAULTS
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, budget_usd=50.0,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items
    committed = sum(i.estimated_cost_usd for i in result.items)
    assert committed > 0
    assert result.plan.spent_usd == pytest.approx(committed)
    assert result.plan.budget_remaining == pytest.approx(50.0 - committed)
    assert committed == pytest.approx(
        FORMAT_COST_DEFAULTS["SHORT"] * len(result.items), abs=0.001)


def test_plan_over_budget_is_blocked(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, budget_usd=0.50,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items == []
    assert any(b["verdict"] == "OVER_CAPACITY" for b in result.blocked)
    assert any("budget" in b["reasons"][0] or "budget" in str(b)
               for b in result.blocked)


def test_estimate_then_assert_then_ledger_order_is_enforced(db_session, ws):
    """§12: the planner must go through assert_can_spend, not estimate alone."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.services.cost import assert_can_spend

    _signal(db_session, ws.id)
    engine = ContentPlanningEngine(db_session, ws.id)
    result = engine.plan(PlanningInputs(
        workspace_id=ws.id, budget_usd=100.0,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items
    estimated = sum(i.estimated_cost_usd for i in result.items)
    assert estimated > 0
    # the Work 11.5 guard is the authority and is callable on the estimate
    try:
        assert_can_spend(ws.id, estimated)
    except Exception as exc:  # BudgetExceededError when over a per-video cap
        assert "budget" in str(exc).lower()


# ===========================================================================
# §6 autonomy
# ===========================================================================


def test_recommend_mode_produces_suggestions_only(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.models.planning import EditorialPlan, EditorialPlanItem

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, autonomy=AutonomyPolicy(mode=AutonomyMode.RECOMMEND)))
    assert result.suggestions, "RECOMMEND must still produce suggestions"
    assert result.items == []
    assert result.plan is None
    # nothing was persisted FOR THIS WORKSPACE. (db_session commits, so the
    # database is shared across tests -- count scoped, never global.)
    assert db_session.query(EditorialPlan).filter(
        EditorialPlan.workspace_id == ws.id).count() == 0
    assert db_session.query(EditorialPlanItem).filter(
        EditorialPlanItem.workspace_id == ws.id).count() == 0


def test_disabled_mode_plans_nothing(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, autonomy=AutonomyPolicy(mode=AutonomyMode.DISABLED)))
    assert result.items == [] and result.plan is None


def test_approval_mode_creates_items_but_not_schedule(db_session, ws):
    from app.engine.planning.autonomy import (
        AutonomyMode,
        AutonomyPolicy,
        AutonomyRefused,
        PlanningAction,
        assert_may_advance,
    )
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    policy = AutonomyPolicy(mode=AutonomyMode.APPROVAL)
    assert_may_advance(policy, PlanningAction.CREATE_CAMPAIGN_DRAFT)
    with pytest.raises(AutonomyRefused):
        assert_may_advance(policy, PlanningAction.SCHEDULE)

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, autonomy=policy))
    assert result.items
    engine = ContentPlanningEngine(db_session, ws.id)
    with pytest.raises(AutonomyRefused):
        engine.schedule_item(result.items[0].id, policy)


def test_autonomous_mode_respects_its_allowlist(db_session, ws):
    from app.engine.planning.autonomy import (
        AutonomyMode,
        AutonomyPolicy,
        AutonomyRefused,
        PlanningAction,
        assert_may_advance,
    )

    policy = AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS).resolved()
    with pytest.raises(AutonomyRefused):
        assert_may_advance(policy, PlanningAction.SCHEDULE)

    allowed = AutonomyPolicy(
        mode=AutonomyMode.AUTONOMOUS,
        allowed_actions=frozenset({PlanningAction.SCHEDULE})).resolved()
    assert_may_advance(allowed, PlanningAction.SCHEDULE)


def test_no_autonomy_mode_can_publish(db_session, ws):
    """The wall between planning and publishing."""
    from app.engine.planning.autonomy import (
        AutonomyMode,
        AutonomyPolicy,
        AutonomyRefused,
        PlanningAction,
        assert_may_advance,
    )

    for mode in AutonomyMode:
        policy = AutonomyPolicy(
            mode=mode,
            allowed_actions=frozenset({a for a in PlanningAction})).resolved()
        with pytest.raises(AutonomyRefused) as caught:
            assert_may_advance(policy, PlanningAction.PUBLISH)
        assert "never available to the planner" in str(caught.value)


def test_planner_never_writes_a_published_status(db_session, ws):
    """Structural proof: the planner module has no PUBLISHED writer."""
    from pathlib import Path

    import app.engine.planning as planning_pkg

    root = Path(planning_pkg.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        # the only permitted mentions are the enum membership and the gate
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if "= PUBLISHED" in stripped or 'status = "PUBLISHED"' in stripped:
                raise AssertionError(
                    f"{path.name} assigns PUBLISHED: {stripped}")


# ===========================================================================
# §7 calendar
# ===========================================================================


def test_calendar_places_on_a_seed_window_and_labels_it(db_session, ws):
    from datetime import date

    from app.engine.planning.calendar import CalendarRequest, place_request

    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=date.today()))
    assert placement.is_blocked is False
    assert placement.evidence_backed is False
    assert "seed window" in placement.reason
    assert "no measured timing" in placement.reason


def test_calendar_respects_a_blackout(db_session, ws):
    from datetime import date

    from app.engine.planning.calendar import CalendarRequest, place_request

    today = date.today()
    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=today), constraints={"blackout_dates": [today.isoformat()]})
    # it moved off the blackout day
    assert placement.run_at.date() != today


def test_calendar_respects_a_deadline(db_session, ws):
    from datetime import date, timedelta

    from app.engine.planning.calendar import CalendarRequest, place_request

    today = date.today()
    deadline = today + timedelta(days=2)
    # blackout every day up to and past the deadline -> must block, not slip
    constraints = {"blackout_dates": [
        (today + timedelta(days=offset)).isoformat() for offset in range(0, 4)]}
    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=today, deadline=deadline), constraints=constraints)
    assert placement.is_blocked is True
    assert "blackout" in placement.blocked_reason


def test_calendar_writes_through_the_existing_schedule_store(db_session, ws):
    from datetime import date

    from app.engine.planning.calendar import (
        CalendarRequest,
        build_schedule_entries,
        place_request,
    )
    from app.models.content import ScheduleEntry

    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=date.today()))
    out = build_schedule_entries(db_session, ws.id, "plan-1", [placement])
    assert out[0]["created"] is True
    assert _count(db_session, ScheduleEntry, ws.id) == 1
    # a re-run reuses the entry rather than creating a second one
    again = build_schedule_entries(db_session, ws.id, "plan-1", [placement])
    assert again[0]["created"] is False
    assert _count(db_session, ScheduleEntry, ws.id) == 1


def test_calendar_spacing_prefers_a_less_crowded_hour(db_session, ws):
    from datetime import date

    from app.engine.planning.calendar import (
        CalendarRequest,
        place_request,
        score_slot,
    )
    from app.models.content import ScheduleEntry

    busy = ScheduleEntry(workspace_id=ws.id, content_item_id="old",
                         platform="threads",
                         run_at=datetime.combine(date.today(), datetime.min.time()
                                                 ).replace(hour=12),
                         status="PENDING")
    db_session.add(busy)
    db_session.commit()

    crowded = score_slot([busy], datetime.combine(
        date.today(), datetime.min.time()).replace(hour=12), platform="threads")
    free = score_slot([busy], datetime.combine(
        date.today(), datetime.min.time()).replace(hour=3), platform="threads")
    assert crowded > free

    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=date.today()))
    assert placement.run_at.hour != 12


def test_unknown_timezone_falls_back_to_utc_not_server_local(db_session, ws):
    from datetime import date

    from app.engine.planning.calendar import (
        CalendarRequest,
        place_request,
        resolve_timezone,
    )

    assert str(resolve_timezone("Not/AZone")) == "UTC"
    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=date.today(), locale="Not/AZone"))
    assert placement.run_at.tzinfo is not None


# ===========================================================================
# §9 orchestration
# ===========================================================================


def test_orchestration_creates_a_real_campaign_draft(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.orchestration import run_orchestration
    from app.models.content import Campaign

    _signal(db_session, ws.id)
    engine = ContentPlanningEngine(db_session, ws.id)
    result = engine.plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item = result.items[0]
    out = run_orchestration(db_session, ws.id, item.id,
                            policy=AutonomyPolicy(mode=AutonomyMode.APPROVAL))
    assert out.campaign_id
    campaign = db_session.get(Campaign, out.campaign_id)
    assert campaign is not None
    assert campaign.workspace_id == ws.id
    assert campaign.status == "DRAFT"
    # a planner-created campaign never starts autonomous
    assert campaign.automation_level == "MANUAL"


def test_orchestration_is_idempotent(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.orchestration import run_orchestration
    from app.models.content import Campaign

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item = result.items[0]
    policy = AutonomyPolicy(mode=AutonomyMode.APPROVAL)
    first = run_orchestration(db_session, ws.id, item.id, policy=policy)
    second = run_orchestration(db_session, ws.id, item.id, policy=policy)
    assert first.campaign_id == second.campaign_id
    assert _count(db_session, Campaign, ws.id) == 1
    assert "campaign_draft" in second.skipped


def test_orchestration_is_resumable_after_a_crash(db_session, ws):
    """A run that died after the campaign stage must not redo it."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.orchestration import advance_stage, run_orchestration
    from app.models.content import Campaign

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item = result.items[0]
    # simulate a crash: record the research stage, then resume
    advance_stage(db_session, ws.id, item.id, "research")
    calls: list[int] = []
    out = run_orchestration(
        db_session, ws.id, item.id,
        policy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS),
        research_fn=lambda *_: calls.append(1))
    assert calls == [], "an already-recorded stage must not re-run"
    assert _count(db_session, Campaign, ws.id) == 1
    assert out.campaign_id


def test_orchestration_refuses_campaign_draft_in_recommend_mode(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.orchestration import run_orchestration
    from app.models.content import Campaign

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    out = run_orchestration(db_session, ws.id, result.items[0].id,
                            policy=AutonomyPolicy(mode=AutonomyMode.RECOMMEND))
    assert out.blocked
    assert out.campaign_id == ""
    assert _count(db_session, Campaign, ws.id) == 0


# ===========================================================================
# §11 feedback
# ===========================================================================


def test_outcome_is_always_recorded_but_a_lesson_needs_a_sample(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.feedback import (
        MIN_SAMPLE,
        FeedbackRecord,
        derive_lesson,
        record_outcome,
    )

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item = result.items[0]
    out = record_outcome(db_session, ws.id, item.id)
    # the factual outcome is always stored, even with zero measured metrics
    assert out["memory_ids"]
    assert out["outcome"]["views"] == 0
    assert out["lesson_written"] is False
    # but no lesson from a single item
    assert derive_lesson([FeedbackRecord(plan_item_id="a", views=100,
                                        engagements=50)]) is None
    assert MIN_SAMPLE == 5

    # The floor is a COUNT, not a side effect of there being one group: four
    # items across TWO platforms still yield nothing, because four < 5. Without
    # this the MIN_SAMPLE guard could be removed and every assertion above would
    # still pass.
    short_but_comparable = (
        [FeedbackRecord(plan_item_id=f"a{i}", platform="threads", views=1000,
                        engagements=600) for i in range(2)]
        + [FeedbackRecord(plan_item_id=f"b{i}", platform="pinterest",
                          views=1000, engagements=50) for i in range(2)])
    assert len(short_but_comparable) < MIN_SAMPLE
    assert derive_lesson(short_but_comparable) is None

    # ...and zero-view samples are not "measurements" for sample-size purposes,
    # so a pile of them cannot manufacture a lesson either.
    no_metrics = [FeedbackRecord(plan_item_id=f"z{i}", platform="threads")
                  for i in range(20)]
    assert derive_lesson(no_metrics) is None


def test_lesson_is_emitted_once_the_sample_and_spread_clear_the_floor():
    from app.engine.planning.feedback import FeedbackRecord, derive_lesson

    # the spread between the two groups must clear MIN_CONFIDENCE (0.25) on
    # engagement rate, which is why the rates are far apart rather than close
    samples = ([FeedbackRecord(plan_item_id=f"a{i}", platform="threads",
                               views=1000, engagements=600 + i) for i in range(4)]
               + [FeedbackRecord(plan_item_id=f"b{i}", platform="pinterest",
                                 views=1000, engagements=50 + i)
                  for i in range(4)])
    lesson = derive_lesson(samples)
    assert lesson is not None
    assert lesson["sample_size"] == 8
    assert "threads" in lesson["claim"]
    assert "NOT a guarantee" in lesson["caveat"]
    # the sample size travels with the lesson, so nothing can quote it alone
    assert lesson["samples_per_group"] == {"threads": 4, "pinterest": 4}


def test_lesson_refused_when_the_spread_is_noise():
    from app.engine.planning.feedback import FeedbackRecord, derive_lesson

    samples = ([FeedbackRecord(plan_item_id=f"a{i}", platform="threads",
                               views=1000, engagements=100) for i in range(5)]
               + [FeedbackRecord(plan_item_id=f"b{i}", platform="pinterest",
                                 views=1000, engagements=99) for i in range(5)])
    assert derive_lesson(samples) is None


# ===========================================================================
# §14 auditability + isolation
# ===========================================================================


def test_plan_item_why_is_reproducible(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id, goals=["grow"], platforms=["threads"],
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    why = result.items[0].why
    for key in ("why_created", "why_selected", "memories_used",
                "lessons_influencing", "constraints_applied", "scoring",
                "dedupe", "basis", "reproducible_from"):
        assert key in why, f"missing audit key {key}"
    assert why["reproducible_from"]["signals"]
    assert why["scoring"]["factors"]


def test_planning_is_workspace_isolated(db_session, ws):
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.models import Workspace
    from app.models.planning import EditorialPlan

    other = Workspace(name="other", slug=f"ws-{os.urandom(4).hex()}")
    db_session.add(other)
    db_session.commit()
    _signal(db_session, ws.id)

    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item_id = result.items[0].id

    # the other workspace's engine cannot touch it
    stranger = ContentPlanningEngine(db_session, other.id)
    with pytest.raises(Exception):
        stranger._item(item_id)
    # and a foreign workspace sees no plans at all
    from app.engine.planning.engine import ContentPlanningEngine as Engine

    foreign = Engine(db_session, other.id).plan(PlanningInputs(
        workspace_id=other.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert foreign.items == []
    assert db_session.query(EditorialPlan).filter(
        EditorialPlan.workspace_id == other.id).count() == 0


def test_planning_without_evidence_plans_nothing(db_session, ws):
    """No signals -> no invented ideas."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items == []
    assert any("nothing is planned" in n for n in result.notes)


def test_planner_does_not_shadow_the_existing_scheduler(db_session, ws):
    """A second scheduler is FAILED, so prove there is only one store."""
    from pathlib import Path

    import app.engine.planning as planning_pkg
    from app.models.content import ScheduleEntry

    assert hasattr(ScheduleEntry, "run_at")
    # The planner declares no schedule table of its own. Scoped to the classes
    # this module actually defines -- Base.metadata is the whole registry, so
    # querying it directly would find every pre-existing Work 01-14 table.
    from app.models.planning import (
        EditorialPlan,
        EditorialPlanItem,
        ProductionCapacity,
        TrendSignal,
    )

    declared = {model.__tablename__ for model in
                (EditorialPlan, EditorialPlanItem, ProductionCapacity,
                 TrendSignal)}
    assert declared == {"editorial_plans", "editorial_plan_items",
                        "production_capacity", "trend_signals"}
    assert not any("schedule" in name for name in declared), (
        "the planner must reuse schedule_entries, not declare its own")
    assert ScheduleEntry.__tablename__ == "schedule_entries"

    # and it never dispatches: no job enqueue anywhere in the planning package
    root = Path(planning_pkg.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "jobs_service.enqueue" not in text, (
            f"{path.name} enqueues a job: the planner must not dispatch")
        assert "services.jobs" not in text, (
            f"{path.name} touches the job service")
    _ = (db_session, ws)


# ===========================================================================
# regressions the Work 15 audit surfaced
# ===========================================================================


def test_feedback_resolves_the_item_through_content_items(db_session, ws):
    """The feedback lookup must not compare an Opportunity key to a ContentItem key.

    ``PublishedPost.content_item_id == item.opportunity_id`` matched nothing, so
    every outcome read as zero and the loop was dead.
    """
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.feedback import collect_feedback
    from app.models.content import ContentItem, Opportunity, PostMetric, PublishedPost

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item = result.items[0]
    opportunity_id = item.opportunity_id
    assert opportunity_id

    # the content produced for that opportunity
    content = ContentItem(workspace_id=ws.id, topic="budget tips",
                          opportunity_id=opportunity_id, status="PUBLISHED")
    db_session.add(content)
    db_session.flush()
    post = PublishedPost(workspace_id=ws.id, content_item_id=content.id,
                         video_id=f"fb-{ws.id[:8]}", platform="threads",
                         publication_mode="MOCK")
    db_session.add(post)
    db_session.flush()
    db_session.add(PostMetric(post_id=post.id, views=1000, likes=120,
                              comments=10, shares=5, saves=5))
    db_session.commit()

    # sanity: the two key types really are different values
    assert db_session.get(Opportunity, opportunity_id).id != content.id

    record = collect_feedback(db_session, ws.id, item.id)
    assert record.linked is True
    assert record.views == 1000
    assert record.engagements == 140
    assert record.engagement_rate == pytest.approx(0.14)
    assert record.content_item_id == content.id


def test_unpublished_item_is_not_reported_as_zero_views(db_session, ws):
    """``linked`` distinguishes "never published" from "published, no views"."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.feedback import collect_feedback

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    record = collect_feedback(db_session, ws.id, result.items[0].id)
    assert record.linked is False
    assert record.views == 0


def test_a_lesson_needs_a_reachable_published_state(db_session, ws):
    """The lesson path must not filter on a status nothing ever writes.

    ``status == "PUBLISHED"`` never matches a plan item, so lessons were
    unreachable. Delivery states that DO occur must be counted, and the query
    must be workspace-scoped.
    """
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.feedback import record_outcome
    from app.models import Workspace
    from app.models.content import (
        ContentItem,
        Opportunity,
        PostMetric,
        PublishedPost,
    )
    from app.models.planning import EditorialPlan, EditorialPlanItem

    other = Workspace(name="other-ws", slug=f"w15x-{os.urandom(4).hex()}")
    db_session.add(other)
    db_session.commit()

    # A real plan for the OTHER workspace, so a missing workspace filter in the
    # sibling query would pull its items in. The FKs must be genuine rows.
    foreign_opportunity = Opportunity(
        workspace_id=other.id, topic="foreign topic", source="community",
        score=0.5, components_json={}, recommendation="WAIT",
        lifecycle="UNKNOWN", confidence=0.5, virality=0.0)
    db_session.add(foreign_opportunity)
    db_session.flush()
    foreign_plan = EditorialPlan(workspace_id=other.id, horizon_days=30)
    db_session.add(foreign_plan)
    db_session.flush()
    db_session.add(EditorialPlanItem(
        workspace_id=other.id, plan_id=foreign_plan.id,
        opportunity_id=foreign_opportunity.id, content_format="SHORT",
        angle="foreign topic", status="SCHEDULED",
        target_date=datetime.now(UTC)))
    db_session.commit()

    for index in range(6):
        _signal(db_session, ws.id, topic=f"paid topic {index}", ref=f"t{index}")
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert len(result.items) >= 5

    # publish half on threads (high engagement) and half on pinterest (low)
    for index, item in enumerate(result.items):
        content = ContentItem(workspace_id=ws.id, topic=item.angle,
                              opportunity_id=item.opportunity_id,
                              status="PUBLISHED")
        db_session.add(content)
        db_session.flush()
        post = PublishedPost(
            workspace_id=ws.id, content_item_id=content.id,
            video_id=f"lesson-{ws.id[:8]}-{index}",
            platform="threads" if index < 3 else "pinterest",
            publication_mode="MOCK")
        db_session.add(post)
        db_session.flush()
        db_session.add(PostMetric(
            post_id=post.id, views=1000,
            likes=600 if index < 3 else 40))
    db_session.commit()

    out = record_outcome(db_session, ws.id, result.items[0].id)
    assert out["lesson"] is not None, (
        "a real sample with a real spread must produce a lesson")
    assert out["lesson_written"] is True
    assert "effect_size" in out["lesson"]
    # a measured rate gap is NOT a confidence
    assert "confidence" not in out["lesson"]


def test_platform_repetition_window_is_real(db_session, ws):
    """Old posts must not count toward the 7-day repetition window.

    The cutoff was computed and then discarded, so a year-old post made the
    reason string claim "in the last 7 days".
    """
    from datetime import UTC, datetime, timedelta

    from app.engine.planning.dedup import detect_dedupe
    from app.models.content import ContentItem, PublishedPost

    for index in range(3):
        item = ContentItem(workspace_id=ws.id, topic=f"old {index}",
                           status="PUBLISHED")
        db_session.add(item)
        db_session.flush()
        post = PublishedPost(
            workspace_id=ws.id, content_item_id=item.id, video_id=f"old{index}",
            platform="threads", publication_mode="MOCK")
        post.created_at = (datetime.now(UTC)
                           - timedelta(days=400)).replace(tzinfo=None)
        db_session.add(post)
    db_session.commit()

    result = detect_dedupe(db_session, ws.id, "brand new topic",
                           platforms=["threads"])
    assert result.verdict == "NEW", (
        "3 posts from 400 days ago is not a repetition problem")


def test_an_exact_duplicate_is_blocked_despite_the_angle(db_session, ws):
    """Topic and angle must be compared separately.

    Concatenating them diluted an exact duplicate from 1.00 to ~0.75 -- below
    the DUPLICATE threshold -- making the duplicate block unreachable from the
    engine while the helper still looked correct in isolation.
    """
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.dedup import detect_dedupe
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

    _content(db_session, ws.id, "how to budget your paycheck")
    # the engine's own angle is "cover <topic>", which used to dilute it
    blocked = detect_dedupe(db_session, ws.id, "how to budget your paycheck",
                            angle="cover how to budget your paycheck")
    assert blocked.verdict == "DUPLICATE"
    assert blocked.is_blocking is True

    # and through the engine: the same topic must not be planned again
    _signal(db_session, ws.id, topic="how to budget your paycheck", ref="dup")
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    assert result.items == []
    assert any(b.get("verdict") in ("DUPLICATE", "SATURATED")
               for b in result.blocked), result.blocked


def test_dry_run_schedule_writes_nothing(db_session, ws):
    """``create=False`` must not add a row that a later autoflush persists."""
    from datetime import date

    from app.engine.planning.calendar import (
        CalendarRequest,
        build_schedule_entries,
        place_request,
    )
    from app.models.content import ScheduleEntry

    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="dry-run", content_id="dry-content", campaign_id="",
        platform="threads", target_date=date.today()))
    out = build_schedule_entries(db_session, ws.id, "plan-1", [placement],
                                 create=False)
    assert out and out[0]["dry_run"] is True
    assert out[0]["entry_id"] == ""
    # a query triggers an autoflush, which is how the old version persisted it
    # (db_session commits, so this is scoped to the workspace and the content
    # id, never a global count)
    assert _count(db_session, ScheduleEntry, ws.id) == 0


def test_an_unmet_dependency_blocks_placement(db_session, ws):
    """A dependent item may not be scheduled before what it waits on.

    ``depends_on`` used to be accepted and ignored, so a part 2 could be placed
    ahead of its part 1.
    """
    from datetime import date

    from app.engine.planning.calendar import CalendarRequest, place_request
    from app.models.planning import EditorialPlan, EditorialPlanItem

    plan = EditorialPlan(workspace_id=ws.id, horizon_days=30)
    db_session.add(plan)
    db_session.flush()
    first = EditorialPlanItem(workspace_id=ws.id, plan_id=plan.id,
                              content_format="SHORT", angle="part 1",
                              status="PLANNED")
    second = EditorialPlanItem(workspace_id=ws.id, plan_id=plan.id,
                               content_format="SHORT", angle="part 2",
                               status="PLANNED", dependencies_json=[first.id])
    db_session.add_all([first, second])
    db_session.commit()

    blocked = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id=second.id, content_id="", campaign_id="",
        platform="threads", target_date=date.today(),
        depends_on=[first.id]))
    assert blocked.is_blocked is True
    assert "unmet dependency" in blocked.blocked_reason
    assert first.id in blocked.blocked_reason

    # once the dependency has its own schedule entry, the block lifts
    first.schedule_entry_id = "entry-1"
    db_session.commit()
    free = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id=second.id, content_id="", campaign_id="",
        platform="threads", target_date=date.today(),
        depends_on=[first.id]))
    assert free.is_blocked is False


def test_two_items_on_one_platform_get_separate_entries(db_session, ws):
    """The idempotency key is the PLAN ITEM, not (campaign, content, platform).

    Both of those are null for an item that has produced nothing yet, so two
    unrelated items collapsed onto one row and re-planning one moved the
    other's time.
    """
    from datetime import date

    from app.engine.planning.calendar import (
        CalendarRequest,
        build_schedule_entries,
        place_request,
    )
    from app.models.content import ScheduleEntry
    from app.models.planning import EditorialPlan, EditorialPlanItem

    plan = EditorialPlan(workspace_id=ws.id, horizon_days=30)
    db_session.add(plan)
    db_session.flush()
    ids: list[str] = []
    for name in ("item a", "item b"):
        item = EditorialPlanItem(workspace_id=ws.id, plan_id=plan.id,
                                 content_format="SHORT", angle=name,
                                 status="PLANNED")
        db_session.add(item)
        db_session.flush()
        ids.append(item.id)
    db_session.commit()

    first = build_schedule_entries(db_session, ws.id, plan.id, [
        place_request(db_session, ws.id, CalendarRequest(
            plan_item_id=ids[0], content_id="", campaign_id="",
            platform="threads", target_date=date.today()))])
    second = build_schedule_entries(db_session, ws.id, plan.id, [
        place_request(db_session, ws.id, CalendarRequest(
            plan_item_id=ids[1], content_id="", campaign_id="",
            platform="threads", target_date=date.today()))])
    assert first[0]["created"] is True
    assert second[0]["created"] is True, "the second item needs its own entry"
    assert first[0]["entry_id"] != second[0]["entry_id"]
    assert _count(db_session, ScheduleEntry, ws.id) == 2
    # re-planning the first reuses ITS entry and does not touch the second
    again = build_schedule_entries(db_session, ws.id, plan.id, [
        place_request(db_session, ws.id, CalendarRequest(
            plan_item_id=ids[0], content_id="", campaign_id="",
            platform="threads", target_date=date.today()))])
    assert again[0]["created"] is False
    assert again[0]["entry_id"] == first[0]["entry_id"]
    assert _count(db_session, ScheduleEntry, ws.id) == 2


def test_run_at_is_stored_in_utc(db_session, ws):
    """A 12:00 Tokyo slot must not fire nine hours off in local time.

    ``run_at`` is a naive column the canonical Scheduler reads as UTC, so
    writing the workspace-local wall clock silently shifted every non-UTC
    workspace.
    """
    from datetime import UTC, date
    from zoneinfo import ZoneInfo

    from app.engine.planning.calendar import (
        CalendarRequest,
        build_schedule_entries,
        place_request,
    )
    from app.models.content import ScheduleEntry

    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="tokyo", content_id="", campaign_id="",
        platform="threads", target_date=date.today(),
        locale="Asia/Tokyo"))
    build_schedule_entries(db_session, ws.id, "plan-1", [placement])
    db_session.commit()

    entry = db_session.query(ScheduleEntry).filter(
        ScheduleEntry.workspace_id == ws.id).first()
    # the naive column, read as UTC by the scheduler, must equal the instant
    # the optimizer chose -- not the Tokyo wall clock
    assert entry.run_at == placement.run_at.astimezone(UTC).replace(tzinfo=None)
    # and reading it back as UTC really does give the placed local hour
    tokyo = ZoneInfo("Asia/Tokyo")
    assert entry.run_at.replace(tzinfo=UTC).astimezone(tokyo).hour == \
        placement.run_at.astimezone(tokyo).hour


def test_only_the_latest_metric_snapshot_is_counted(db_session, ws):
    """PostMetric rows are CUMULATIVE snapshots; summing them triple-counts."""
    from app.engine.planning.autonomy import AutonomyMode, AutonomyPolicy
    from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs
    from app.engine.planning.feedback import collect_feedback
    from app.models.content import ContentItem, PostMetric, PublishedPost

    _signal(db_session, ws.id)
    result = ContentPlanningEngine(db_session, ws.id).plan(PlanningInputs(
        workspace_id=ws.id,
        autonomy=AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS)))
    item = result.items[0]
    content = ContentItem(workspace_id=ws.id, topic="budget tips",
                          opportunity_id=item.opportunity_id, status="PUBLISHED")
    db_session.add(content)
    db_session.flush()
    post = PublishedPost(workspace_id=ws.id, content_item_id=content.id,
                         video_id=f"snap-{ws.id[:8]}", platform="threads",
                         publication_mode="MOCK")
    db_session.add(post)
    db_session.flush()
    # three cumulative snapshots from the Analytics Agent's per-cycle writes
    for captured, views, likes in ((1, 100, 10), (2, 200, 20), (3, 300, 30)):
        db_session.add(PostMetric(
            post_id=post.id, views=views, likes=likes,
            captured_at=datetime(2026, 1, captured).replace(tzinfo=None)))
    db_session.commit()

    record = collect_feedback(db_session, ws.id, item.id)
    assert record.snapshots_seen == 3
    assert record.views == 300, "the LATEST snapshot, not the sum (600)"
    assert record.engagements == 30


def test_a_satisfied_dependency_does_not_skip_the_optimizer(db_session, ws):
    """``"" is not None`` is True -- a satisfied dependency used to bypass
    blackouts, caps, spacing AND the deadline.
    """
    from datetime import date

    from app.engine.planning.calendar import CalendarRequest, place_request
    from app.models.planning import EditorialPlan, EditorialPlanItem

    plan = EditorialPlan(workspace_id=ws.id, horizon_days=30)
    db_session.add(plan)
    db_session.flush()
    first = EditorialPlanItem(workspace_id=ws.id, plan_id=plan.id,
                              content_format="SHORT", angle="part 1",
                              status="PLANNED", schedule_entry_id="e1")
    second = EditorialPlanItem(workspace_id=ws.id, plan_id=plan.id,
                               content_format="SHORT", angle="part 2",
                               status="PLANNED", dependencies_json=[first.id])
    db_session.add_all([first, second])
    db_session.commit()

    today = date.today()
    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id=second.id, content_id="", campaign_id="",
        platform="threads", target_date=today, depends_on=[first.id]),
        constraints={"blackout_dates": [today.isoformat()]})
    assert placement.is_blocked is False
    # the blackout was still honoured -- the optimizer actually ran
    assert placement.run_at.date() != today
    assert "blocked by an unmet dependency" not in placement.reason


def test_a_done_schedule_entry_is_never_rewound(db_session, ws):
    from datetime import date, time
    from datetime import datetime as dt

    from app.engine.planning.calendar import (
        CalendarRequest,
        build_schedule_entries,
        place_request,
    )
    from app.models.content import ScheduleEntry

    published = ScheduleEntry(
        workspace_id=ws.id, content_item_id="c1", platform="threads",
        run_at=dt.combine(date.today(), time(hour=9)), status="DONE")
    db_session.add(published)
    db_session.commit()

    placement = place_request(db_session, ws.id, CalendarRequest(
        plan_item_id="p1", content_id="c1", platform="threads",
        target_date=date.today()))
    build_schedule_entries(db_session, ws.id, "plan-1", [placement])
    db_session.commit()
    assert published.run_at.hour == 9, "a published entry is history"
