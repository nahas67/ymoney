"""HyperFrames motion-card tests: templates, probes, fail-closed render, agent.

No test requires a working browser: the success path fakes the CLI, and the
real environment asserts fail-closed behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.providers import motion as motion_mod
from app.providers.motion import (
    CARD_KINDS,
    MotionError,
    build_composition,
    motion_status,
)


def test_composition_kinds_are_valid_documents():
    for kind in CARD_KINDS:
        html = build_composition(kind, "Stop losing money", "Save more every month")
        assert html.startswith("<!DOCTYPE html>")
        assert 'data-composition-id="ymoney-' in html
        assert 'data-width="1080"' in html and 'data-height="1920"' in html
        assert "data-no-timeline" in html
        assert "Stop losing money" in html


def test_composition_escapes_html():
    html = build_composition("hook", '<script>alert(1)</script>', "")
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_composition_unknown_kind_raises():
    with pytest.raises(MotionError):
        build_composition("explosion", "t")


@pytest.mark.slow
def test_status_contract():
    status = motion_status()
    assert set(status) >= {"cli", "version", "browser", "ffmpeg", "ready", "kinds"}
    assert status["kinds"] == ["hook", "stat", "cta", "lower"]
    assert status["ready"] == bool(status["cli"] and status["ffmpeg"] and status["browser"])
    assert isinstance(status["ready"], bool)


def test_render_fails_closed_without_ready_browser(monkeypatch):
    monkeypatch.setattr(motion_mod, "motion_status",
                        lambda: {"cli": True, "version": "x", "browser": False,
                                 "ffmpeg": True, "ready": False, "kinds": list(CARD_KINDS)})
    with pytest.raises(MotionError, match="Chrome"):
        motion_mod.render_card("hook", "t", "ws-x")


def test_render_success_path_with_faked_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(motion_mod, "motion_status",
                        lambda: {"cli": True, "version": "x", "browser": True,
                                 "ffmpeg": True, "ready": True, "kinds": list(CARD_KINDS)})

    def fake_run(cmd, **kw):
        from types import SimpleNamespace

        if "lint" in cmd:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fakemp4")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(motion_mod.subprocess, "run", fake_run)
    card = motion_mod.render_card("stat", "87%", "ws-e2e", subtitle="completion rate")
    assert card.kind == "stat"
    assert Path(card.path).exists()
    assert str(Path("data/videos/ws-e2e")) in card.path


def test_motion_designer_agent_uses_provider(tmp_path, monkeypatch):
    from app.engine.agents import motion as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        agent_mod, "render_card",
        lambda kind, title, ws, **kw: motion_mod.MotionCard(path="data/videos/ws-x/hook-card.mp4", kind=kind, duration=3.0),
    )
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-x",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = agent_mod.MotionDesignerAgent().design(ctx, kind="hook", title="Stop losing money")
    assert out["kind"] == "hook"
    assert out["path"].endswith("hook-card.mp4")


def test_motion_designer_rejects_unknown_kind(tmp_path, monkeypatch):
    from app.engine.agents import motion as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-x",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    with pytest.raises(ValueError, match="unknown card kind"):
        agent_mod.MotionDesignerAgent().design(ctx, kind="explosion", title="t")
