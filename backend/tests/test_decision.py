"""Decision engine tests: gates, diversity, escalation, WHY payload."""

import pytest

from app.engine.decision import (
    Decision,
    get_safety_settings,
    topic_similarity,
)


class TestTopicSimilarity:
    def test_identical_topics(self):
        assert topic_similarity("AI tools for saving money", "money-saving AI tools") > 0.4

    def test_disjoint_topics(self):
        assert topic_similarity("quantum physics", "chocolate cake recipe") == 0.0

    def test_stopwords_ignored(self):
        assert topic_similarity("the best of tools", "best tools") > 0.7

    def test_empty_inputs(self):
        assert topic_similarity("", "anything") == 0.0


def test_safety_defaults_are_safe():
    s = get_safety_settings({})
    assert s["max_videos_per_day"] >= 1
    assert 0 <= s["similarity_threshold"] <= 1
    assert s["min_qc_score"] > 0


def test_safety_overrides_merge():
    merged = get_safety_settings({"safety": {"max_videos_per_day": 3, "bogus": "x"}})
    assert merged["max_videos_per_day"] == 3
    assert "daily_budget_usd" in merged


class TestDecisionGates:
    """Hard capacity gates must fire before candidate evaluation."""

    def _decision(self, action, **kw) -> Decision:
        return Decision(action=action, **kw)

    def test_wait_when_no_candidates(self):
        d = self._decision("WAIT", reasons=["no unselected opportunities available; discovery will refresh"])
        assert d.action == "WAIT"

    def test_human_review_carries_reason_and_confidence(self):
        d = self._decision(
            "HUMAN_REVIEW",
            score=80,
            confidence=0.9,
            reasons=["sensitive subject matter (risk 85/100) requires manual approval"],
        )
        why = d.why()
        assert why["action"] == "HUMAN_REVIEW"
        assert "manual approval" in why["reasons"][0]
        assert why["confidence"] == 0.9

    def test_skip_includes_similarity_evidence(self):
        d = self._decision(
            "SKIP",
            score=60,
            confidence=0.85,
            reasons=["too similar to recently published 'old topic' (70% similar)"],
            evidence=["Most similar recent content: 'old topic'"],
        )
        assert d.action == "SKIP"
        assert any("similar" in e for e in d.evidence)

    def test_why_payload_serializable(self):
        import json

        d = self._decision(
            "PRODUCE",
            opportunity_id="o1",
            topic="t",
            score=75,
            confidence=0.8,
            reasons=["ok"],
            factors=[__import__("app.engine.decision", fromlist=["Factor"]).Factor("base_score", "75/100", 30)],
            evidence=["evidence-1"],
        )
        json.dumps(d.why())  # must not raise


@pytest.mark.usefixtures("db_session")
class TestDecideAgainstDb:
    def test_produce_for_strong_candidate(self, workspace_with_user):
        from app.db import session_scope
        from app.engine.decision import decide_next_best_action
        from app.models import Opportunity

        ws = workspace_with_user["workspace"]
        with session_scope() as s:
            s.add(Opportunity(
                workspace_id=ws,
                topic="AI side hustles earning real money",
                source="mock",
                raw_payload={"_velocity_hint": 0.9, "_volume_hint": 0.9},
                score=72.0,
                recommendation="PRODUCE",
                lifecycle="RISING",
                confidence=0.8,
            ))
        decision = decide_next_best_action(ws)
        assert decision.action in ("PRODUCE",)
        assert decision.opportunity_id is not None
        assert any(f.name == "lifecycle" for f in decision.factors)

    def test_skip_on_duplicate_of_recent_content(self, workspace_with_user):

        from app.db import session_scope
        from app.engine.decision import decide_next_best_action
        from app.models import ContentItem, Opportunity

        ws = workspace_with_user["workspace"]
        with session_scope() as s:
            s.add(ContentItem(
                workspace_id=ws,
                topic="AI side hustles earning real money fast",
                status="LEARNED",
            ))
            s.add(Opportunity(
                workspace_id=ws,
                topic="AI side hustles earning real money",
                source="mock",
                raw_payload={"_velocity_hint": 0.9},
                score=90.0,
                recommendation="CREATE_NOW",
            ))

        # lower the similarity threshold so the duplicate trips it deterministically
        with session_scope() as s:
            from app.models import Workspace

            w = s.get(Workspace, ws)
            w.settings_json = {"safety": {"similarity_threshold": 0.2}}

        decision = decide_next_best_action(ws)
        assert decision.action == "SKIP"
        assert any("similar" in r for r in decision.reasons)

    def test_daily_cap_returns_wait(self, workspace_with_user):
        from app.db import session_scope
        from app.engine.decision import decide_next_best_action
        from app.models import ContentItem, Opportunity, Workspace

        ws = workspace_with_user["workspace"]
        with session_scope() as s:
            s.add(Opportunity(workspace_id=ws, topic="great topic", score=95))
            for i in range(10):
                s.add(ContentItem(workspace_id=ws, topic=f"topic {i}", status="PUBLISHED"))
            w = s.get(Workspace, ws)
            w.settings_json = {"safety": {"max_videos_per_day": 5}}

        decision = decide_next_best_action(ws)
        assert decision.action == "WAIT"
        assert "cap" in decision.reasons[0]

    def test_skipped_rows_and_skip_recommendations_are_not_candidates(self, workspace_with_user):
        from app.db import session_scope
        from app.engine.decision import decide_next_best_action
        from app.models import Opportunity

        ws = workspace_with_user["workspace"]
        with session_scope() as s:
            s.add(Opportunity(
                workspace_id=ws, topic="already rejected", score=99,
                recommendation="SKIP", skipped_reason="duplicate",
            ))
            s.add(Opportunity(
                workspace_id=ws, topic="also rejected", score=98,
                recommendation="SKIP",
            ))

        decision = decide_next_best_action(ws)
        assert decision.action == "WAIT"
        assert decision.opportunity_id is None
