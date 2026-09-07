"""Opportunity scoring tests: explainability, calibration, recommendations."""

from app.engine.scoring import DEFAULT_WEIGHTS, score_opportunity


def test_strong_candidate_reaches_produce():
    scored = score_opportunity(
        {
            "topic": "AI side hustles earning real money",
            "source": "mock",
            "raw": {},
            "velocity_hint": 0.9,
            "volume_hint": 0.9,
        },
        niche="AI money",
    )
    assert scored.overall >= 68
    assert scored.recommendation in ("PRODUCE", "CREATE_NOW")
    # explainability: every component has a reason
    assert all(c.reason for c in scored.components)
    keys = {c.key for c in scored.components}
    assert {"trend_velocity", "competition", "risk", "monetization"} <= keys


def test_generic_topic_scores_low_without_signals():
    scored = score_opportunity(
        {"topic": "some random thing happened", "source": "google_trends", "raw": {}},
        niche="",
    )
    assert scored.overall < 60
    assert scored.recommendation in ("WAIT", "SKIP")


def test_risky_topic_is_penalized():
    safe = score_opportunity({"topic": "budgeting tips", "source": "x", "raw": {}}, niche="finance")
    risky = score_opportunity(
        {"topic": "war crime tragedy scandal", "source": "x", "raw": {}},
        niche="finance",
    )
    assert risky.overall < safe.overall
    risk_comp = next(c for c in risky.components if c.key == "risk")
    assert risk_comp.value > 60


def test_custom_weights_change_outcome():
    base = {"topic": "credit score mistakes to avoid", "source": "mock", "raw": {},
            "velocity_hint": 0.8, "volume_hint": 0.8}
    neutral = score_opportunity(base, niche="")
    boosted = score_opportunity(base, niche="", weights={**DEFAULT_WEIGHTS, "trend_velocity": 5.0})
    assert boosted.overall >= neutral.overall


def test_breakdown_serializable():
    import json

    scored = score_opportunity({"topic": "test topic", "source": "mock", "raw": {}}, niche="")
    json.dumps(scored.breakdown())  # must not raise
