"""Reference-video-to-plan: keeps/changes/cost/sample before production spend."""
from __future__ import annotations


def _ctx():
    from app.services.jobs import JobContext

    return JobContext(job_id="j1", type="test", workspace_id="ws-r",
                      cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)


def _canned_mine(moments):
    return {"source_title": "Ref Video", "source_duration": 120.0, "moments": moments}


def test_plan_shapes_keeps_changes_cost_sample(tmp_path, monkeypatch):
    from app.engine.agents import creation as creation_mod
    from app.engine.agents import repurpose as rep_mod
    from app.providers import llm as llm_mod

    monkeypatch.chdir(tmp_path)
    moments = [
        {"start": 0.0, "end": 20.0, "score": 90.0, "hook": "best hook", "reason": "r", "text": "t"},
        {"start": 20.0, "end": 40.0, "score": 70.0, "hook": "ok hook", "reason": "r", "text": "t"},
    ]
    monkeypatch.setattr(rep_mod.LinkMinerAgent, "mine", lambda self, ctx, **kw: _canned_mine(moments))
    seen: dict = {}

    def _fake_complete(**kw):
        seen["system"] = kw.get("system", "")
        seen["user"] = kw.get("user", "")
        return {"keeps": ["fast hook", "quick cuts"], "changes": ["new topic"]}

    monkeypatch.setattr(llm_mod, "complete_json", _fake_complete)

    out = creation_mod.StrategistAgent().plan_from_reference(
        _ctx(), url="http://example.com/v.mp4", topic="quantum")
    assert out["keeps"] == ["fast hook", "quick cuts"]
    assert out["changes"] == ["new topic"]
    assert out["sample_moment"]["hook"] == "best hook"
    assert out["topic"] == "quantum"
    assert out["moments_analyzed"] == 2
    assert out["estimated_cost_usd"] >= 0
    assert "moments" in seen["user"] and "quantum" in seen["user"]


def test_plan_empty_mine_never_crashes(tmp_path, monkeypatch):
    from app.engine.agents import creation as creation_mod
    from app.engine.agents import repurpose as rep_mod
    from app.providers import llm as llm_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rep_mod.LinkMinerAgent, "mine", lambda self, ctx, **kw: _canned_mine([]))
    monkeypatch.setattr(llm_mod, "complete_json", lambda **kw: {})

    out = creation_mod.StrategistAgent().plan_from_reference(_ctx(), url="http://example.com/v.mp4")
    assert out["keeps"] == [] and out["changes"] == []
    assert out["sample_moment"] == {} and out["moments_analyzed"] == 0
