"""HookOptimizer rubric: command type, number bonus, pattern lift, ordering."""
from __future__ import annotations


def _ctx():
    from app.services.jobs import JobContext

    return JobContext(job_id="j1", type="test", workspace_id="ws-h",
                      cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)


def _rank(scripts, patterns=None):
    from app.engine.agents.creation import HookOptimizerAgent

    variants = [{"script": s} for s in scripts]
    return HookOptimizerAgent().rank_hooks(_ctx(), variants, patterns)


def test_command_hook_type_ranks_above_plain():
    out = _rank(["talking about money and markets today here",
                 "Stop losing money right now with this trick today"])
    assert out[0]["script"].startswith("Stop")
    assert out[0]["predicted_score"] == 76.0


def test_number_bonus_rewards_concreteness():
    out = _rank(["talking about money and markets today here",
                 "5 money mistakes draining your wallet this year"])
    assert out[0]["script"].startswith("5 money")
    assert out[0]["predicted_score"] == 64.0


def test_question_still_top_and_bonus_stacks():
    out = _rank(["What if 3 banks fail this week?"])
    # question 82 + number bonus 4
    assert out[0]["predicted_score"] == 86.0


def test_learned_pattern_bonus_still_applies():
    out = _rank(["talking about money and markets today here"],
                [{"pattern_key": "hook_style", "active": True,
                  "confidence": "high", "observed_improvement_pct": 20}])
    assert out[0]["predicted_score"] == 62.0
