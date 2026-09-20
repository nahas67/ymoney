"""B-roll lane tests: planner, stock/AI lanes (mocked HTTP), agent, API."""
from __future__ import annotations

import pytest

from app.providers import broll as broll_mod
from app.providers.broll import BrollError, broll_status


def _no_llm(monkeypatch):
    monkeypatch.setattr("app.providers.llm.llm_available", lambda: False)


def test_status_contract():
    status = broll_status()
    assert set(status) >= {"stock", "ai_backend", "ai_ready", "ai_detail", "ffmpeg", "ready"}
    assert isinstance(status["ready"], bool)


def test_plan_deterministic_fallback(monkeypatch):
    _no_llm(monkeypatch)
    plan = broll_mod.plan_scenes("save money fast", ["budget", "coins"], 4)
    assert len(plan) == 4
    assert [s.source for s in plan] == ["stock", "ai", "stock", "ai"]
    assert all("save money fast" in s.query for s in plan)
    assert all(s.prompt for s in plan)


def test_plan_clamps_scenes(monkeypatch):
    _no_llm(monkeypatch)
    assert len(broll_mod.plan_scenes("t", [], 99)) == 8
    assert len(broll_mod.plan_scenes("t", [], 0)) == 1


def test_search_needs_key(monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "pexels_api_key", "")
    with pytest.raises(BrollError, match="Pexels key"):
        broll_mod.search_stock("money")


def test_search_and_fetch_with_mocked_pexels(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "pexels_api_key", "test-key")

    videos = {"videos": [{
        "id": 123, "duration": 12, "url": "https://pexels.com/v/123",
        "image": "https://img.pexels.com/123.jpg",
        "user": {"name": "Jane"},
        "video_files": [
            {"file_type": "video/mp4", "height": 1920, "link": "https://dl.pexels.com/123.mp4"},
            {"file_type": "video/mp4", "height": 640, "link": "https://dl.pexels.com/123-sm.mp4"},
        ],
    }]}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return videos

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    found = broll_mod.search_stock("money", per_page=2)
    assert len(found) == 1 and found[0].video_id == "123"
    assert found[0].author == "Jane"

    detail = {"id": 123, "video_files": videos["videos"][0]["video_files"]}

    class _Detail(_Resp):
        def json(self):
            return detail

    class _Dl(_Resp):
        content = b"0" * 60_000

    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        if "videos/videos" in url:
            return _Detail()
        return _Dl()

    monkeypatch.setattr("httpx.get", fake_get)
    path = broll_mod.fetch_stock_clip("123", "ws-b")
    from pathlib import Path

    assert Path(path).exists()
    # cache hit: second fetch skips the network
    path2 = broll_mod.fetch_stock_clip("123", "ws-b")
    assert path2 == path and calls["n"] == 2


def test_generate_server_with_mocked_http(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "broll_ai_backend", "server")
    monkeypatch.setattr(config_mod.settings, "broll_ai_base_url", "http://broll.local")

    class _Resp:
        status_code = 200
        headers = {"content-type": "video/mp4"}
        content = b"mp4bytes"
        text = ""

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    monkeypatch.setattr(broll_mod, "_probe_duration", lambda p: 2.0)
    path = broll_mod.generate_clip("neon chart rising", "ws-b", seconds=2.0)
    from pathlib import Path

    assert Path(path).exists()


def test_generate_native_without_diffusers_fails_closed(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "broll_ai_backend", "wan")
    if broll_mod._have_module("diffusers"):
        pytest.skip("diffusers installed; native path not testable offline")
    with pytest.raises(BrollError, match="diffusers"):
        broll_mod.generate_clip("neon chart", "ws-b")


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed")
def test_generate_synth_placeholder(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "broll_ai_backend", "synth")
    path = broll_mod.generate_clip("test clip", "ws-b", seconds=2.0)
    from pathlib import Path

    assert Path(path).exists() and "synth-" in Path(path).name


def test_broll_researcher_agent(tmp_path, monkeypatch):
    from app.engine.agents import broll as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    _no_llm(monkeypatch)
    monkeypatch.setattr(agent_mod, "search_stock",
                        lambda q, per_page=4: [type("C", (), {"video_id": "9"})()])
    monkeypatch.setattr(agent_mod, "fetch_stock_clip",
                        lambda vid, ws, aspect="9:16": f"data/videos/{ws}/broll/pexels-9.mp4")
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-b",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    planned = agent_mod.BrollResearcherAgent().research(ctx, topic="save money", keywords=["budget"], n_scenes=2)
    assert len(planned["scenes"]) == 2
    fetched = agent_mod.BrollResearcherAgent().fetch(ctx, query="money")
    assert fetched["source"] == "stock" and fetched["path"].endswith("pexels-9.mp4")
    with pytest.raises(BrollError):
        agent_mod.BrollResearcherAgent().fetch(ctx)


def test_broll_skill_agent_registered():
    from app.engine.agents.registry import AGENTS
    from app.engine.capabilities import get_skill, get_tool

    assert get_skill("broll_research").required_tools == ("fetch_broll",)
    assert "media:render" in get_tool("fetch_broll").permissions
    assert AGENTS["broll_researcher"].meta.title == "B-roll Researcher"
    assert len(AGENTS) == 19


def test_broll_api_status_and_validation(monkeypatch):
    import os

    from fastapi.testclient import TestClient

    _no_llm(monkeypatch)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"br{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/broll/status", headers=headers)
    assert r.status_code == 200, r.text
    assert "ai_backend" in r.json()

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/broll/plan", headers=headers,
                    json={"topic": "save money fast", "keywords": ["budget"], "n_scenes": 2})
    assert r.status_code == 200, r.text
    assert len(r.json()["scenes"]) == 2

    monkeypatch.setattr(broll_mod, "search_stock",
                        lambda *a, **k: (_ for _ in ()).throw(BrollError("no key")))
    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/broll/search", headers=headers,
                    json={"query": "money"})
    assert r.status_code == 400
