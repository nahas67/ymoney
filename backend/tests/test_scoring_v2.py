"""Scoring v2 + trend lifecycle classification tests."""

from app.engine.scoring import score_opportunity
from app.engine.trends import classify_lifecycle


class TestLifecycleClassification:
    def test_high_velocity_low_volume_is_emerging(self):
        lc, conf = classify_lifecycle(velocity_hint=0.9, volume_hint=0.2)
        assert lc == "EMERGING"
        assert conf > 0.5

    def test_high_velocity_high_volume_is_rising(self):
        lc, _ = classify_lifecycle(velocity_hint=0.85, volume_hint=0.7)
        assert lc == "RISING"

    def test_low_velocity_high_volume_news_is_declining(self):
        lc, _ = classify_lifecycle(velocity_hint=0.1, volume_hint=0.8, news_count=3)
        assert lc == "DECLINING"

    def test_no_signals_unknown(self):
        lc, conf = classify_lifecycle(velocity_hint=None, volume_hint=None)
        assert lc == "UNKNOWN"
        assert conf < 0.4


class TestScoringV2:
    def candidate(self, **overrides):
        base = {
            "topic": "AI side hustles earning real money",
            "source": "mock",
            "raw": {},
            "velocity_hint": 0.9,
            "volume_hint": 0.9,
        }
        base.update(overrides)
        return base

    def test_components_have_source_and_confidence(self):
        scored = score_opportunity(self.candidate(), niche="AI money")
        for comp in scored.components:
            assert comp.source in ("provider", "heuristic", "learned", "policy", "workspace")
            assert 0 <= comp.confidence <= 1
            assert comp.reason

    def test_breakdown_includes_new_fields(self):
        breakdown = score_opportunity(self.candidate(), niche="AI money").breakdown()
        assert "lifecycle" in breakdown and "confidence" in breakdown
        comp = breakdown["components"]["trend_velocity"]
        assert set(comp) == {"score", "weight", "reason", "source", "confidence"}

    def test_repetition_penalty_lowers_score(self):
        fresh = score_opportunity(self.candidate(), niche="").overall
        repeated = score_opportunity(
            self.candidate(), niche="", recent_topics=["ai side hustles earning real money"]
        ).overall
        assert repeated < fresh

    def test_repetition_component_present_when_history(self):
        scored = score_opportunity(self.candidate(), niche="", recent_topics=["unrelated topic"])
        rep = next(c for c in scored.components if c.key == "repetition")
        assert rep.value == 0  # unrelated topic -> no repetition signal

        scored2 = score_opportunity(self.candidate(), niche="",
                                    recent_topics=["ai side hustles earning real money"])
        rep2 = next(c for c in scored2.components if c.key == "repetition")
        assert rep2.value > 50

    def test_emerging_beats_declining_on_same_base(self):
        emerging = score_opportunity(self.candidate(lifecycle="EMERGING"), niche="")
        declining = score_opportunity(self.candidate(lifecycle="DECLINING"), niche="")
        assert emerging.overall > declining.overall

    def test_confidence_lower_without_provider_signals(self):
        with_signals = score_opportunity(self.candidate(), niche="").confidence
        without = score_opportunity(
            self.candidate(velocity_hint=None, volume_hint=None, raw={}), niche=""
        ).confidence
        assert without < with_signals
