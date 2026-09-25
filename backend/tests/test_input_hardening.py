"""Untrusted-input hardening: null settings, non-numeric LLM/operator values."""
from __future__ import annotations


def test_safety_settings_tolerates_nulls_and_garbage():
    from app.engine.decision import get_safety_settings

    out = get_safety_settings({"safety": {
        "daily_budget_usd": None,
        "max_videos_per_day": None,
        "similarity_threshold": "not-a-number",
        "require_approval_before_publish": True,
    }})
    assert out["daily_budget_usd"] > 0
    assert out["max_videos_per_day"] == 10
    assert out["similarity_threshold"] == 0.55
    assert out["require_approval_before_publish"] is True
    out2 = get_safety_settings("not-a-dict")  # type: ignore[arg-type]
    assert out2["max_videos_per_day"] == 10


def test_strategize_falls_back_on_non_numeric_duration(monkeypatch):
    from app.engine.agents import creation as creation_mod
    from app.providers import llm as llm_mod
    from app.services.jobs import JobContext

    monkeypatch.setattr(llm_mod, "complete_json",
                        lambda **kw: {"duration_seconds": "thirty", "angle": "x"})
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-h",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = creation_mod.StrategistAgent().strategize(ctx, "t", {})
    assert out["duration_seconds"] == 32


def test_design_batch_rejects_non_numeric_rate(tmp_path, monkeypatch):
    import pytest

    from app.engine.agents import voice as agent_mod
    from app.providers import tts as tts_mod
    from app.providers.tts import TTSError
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    with pytest.raises(TTSError, match="non-numeric"):
        agent_mod.VoiceDesignerAgent().design_batch(
            ctx, parts=[{"speaker": "a", "text": "hello world here", "rate": "fast"}])


def test_assemble_rejects_malformed_moments(tmp_path, monkeypatch):
    import pytest

    from app.engine.agents import repurpose as rep_mod
    from app.providers.clips import SourceInfo
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)

    class _Rep:
        def acquire(self, source, ws):
            return SourceInfo(title="t", duration=10.0, width=1, height=1,
                              local_path=tmp_path / "s.mp4")

        def cut_segments(self, *a, **k):
            return []

    monkeypatch.setattr(rep_mod, "get_repurposer", lambda: _Rep())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-r",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    with pytest.raises(ValueError, match="invalid moment"):
        rep_mod.RepurposeEditorAgent().assemble(
            ctx, source="x", moments=[{"start": "soon", "end": "later"}])
    # numeric strings parse (lexicographic pre-filter would have dropped "100">"20")
    out = rep_mod.RepurposeEditorAgent().assemble(
        ctx, source="x", moments=[{"start": "10", "end": "100", "score": "5"}])
    assert out["clips"] == []
