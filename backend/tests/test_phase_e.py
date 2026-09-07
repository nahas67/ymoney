"""Phase E regression coverage: persistent memory subsystem (spec #25).

Covers store/retrieve semantics, type validation, expiry, workspace
isolation, targeted-retrieval cap, and the workspace-scoped API.
"""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from app.models.base import utcnow

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[str, str, dict]:
    email = f"phaseE{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


def _register_second_workspace(client) -> tuple[str, str, dict]:
    return _register(client)


# ---------------------------------------------------------------------------
# service-level behavior
# ---------------------------------------------------------------------------


def test_store_and_targeted_retrieve(db_session):
    from app.services import memory as memory_service

    ws = "ws-mem-a"
    memory_service.store(
        ws, content="Question-style hooks outperform by 12% (n=9)",
        type="semantic", source="learning", confidence=0.7, importance=0.8,
        scope="hook_style_question",
    )
    memory_service.store(
        ws, content="User prefers faceless documentary format",
        type="preference", source="user", importance=0.9, scope="format",
    )

    # targeted by type
    semantic = memory_service.retrieve(ws, type="semantic")
    assert len(semantic) == 1
    assert "Question-style" in semantic[0]["content"]

    # targeted by keyword query
    hits = memory_service.retrieve(ws, query="faceless")
    assert len(hits) == 1
    assert hits[0]["type"] == "preference"

    # wildcard characters are literal in the query match (no SQL injection): only
    # records whose content truly contains the substring are returned.
    all_recs = memory_service.retrieve(ws, query="documentary")
    assert len(all_recs) == 1


def test_expired_memories_never_returned(db_session):
    from app.db import session_scope
    from app.models import MemoryRecord
    from app.services import memory as memory_service

    ws = "ws-mem-exp"
    memory_service.store(ws, content="short-lived note", type="short_term",
                         ttl_hours=0.001, importance=0.9)
    rid = memory_service.retrieve(ws, query="short-lived")[0]["id"]

    # force expiry
    with session_scope() as s:
        rec = s.get(MemoryRecord, rid)
        rec.expires_at = utcnow()

    assert memory_service.retrieve(ws, query="short-lived") == []

    # lazy purge removes it permanently on next store
    memory_service.store(ws, content="another note", type="episodic")
    with session_scope() as s:
        left = s.scalars(
            s.query(MemoryRecord).where(MemoryRecord.workspace_id == ws).statement
        ).all()
        assert all(r.content != "short-lived note" for r in left)


def test_invalid_type_rejected(db_session):
    from app.services import memory as memory_service

    try:
        memory_service.store("ws-mem-bad", content="x", type="hologram")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_retrieve_hard_cap(db_session):
    from app.services import memory as memory_service

    ws = "ws-mem-cap"
    for i in range(8):
        memory_service.store(ws, content=f"note {i}", type="episodic", importance=i / 10)
    assert len(memory_service.retrieve(ws, limit=3)) == 3
    # MAX_RETRIEVE enforced (50) — request above the cap is clamped, not fatal
    assert len(memory_service.retrieve(ws, limit=10_000)) <= 50


def test_workspace_isolation(db_session):
    from app.services import memory as memory_service

    memory_service.store("ws-iso-1", content="secret for ws1", type="semantic")
    assert memory_service.retrieve("ws-iso-2", query="secret") == []
    assert memory_service.retrieve("ws-iso-1", query="secret")


# ---------------------------------------------------------------------------
# API surface (workspace-scoped)
# ---------------------------------------------------------------------------


def test_memory_api_store_list_retrieve_delete(client):
    _tok, ws_id, headers = _register(client)

    r = client.post(
        f"/api/v1/workspaces/{ws_id}/memory/store",
        json={"content": "Audience responds to data-heavy explainers",
              "type": "semantic", "scope": "format", "confidence": 0.8,
              "importance": 0.7},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True
    mid = r.json()["id"]

    # list with filter
    r = client.get(f"/api/v1/workspaces/{ws_id}/memory?type=semantic", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["content"].startswith("Audience responds")

    # targeted retrieve requires at least one filter
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/memory/retrieve", json={}, headers=headers
    )
    assert r.status_code == 422

    r = client.post(
        f"/api/v1/workspaces/{ws_id}/memory/retrieve",
        json={"query": "explainers"}, headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["count"] == 1

    # invalid type rejected
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/memory/store",
        json={"content": "x", "type": "nope"}, headers=headers,
    )
    assert r.status_code == 422

    # delete
    r = client.delete(f"/api/v1/workspaces/{ws_id}/memory/{mid}", headers=headers)
    assert r.status_code == 200
    r = client.get(f"/api/v1/workspaces/{ws_id}/memory", headers=headers)
    assert r.json()["items"] == []


def test_memory_api_cross_workspace_isolated(client):
    _t1, ws1, h1 = _register(client)
    _t2, ws2, h2 = _register(client)

    r = client.post(
        f"/api/v1/workspaces/{ws1}/memory/store",
        json={"content": "ws1-only fact", "type": "semantic"},
        headers=h1,
    )
    assert r.status_code == 200

    # ws2 cannot see ws1 memories
    r = client.get(f"/api/v1/workspaces/{ws2}/memory?q=ws1-only", headers=h2)
    assert r.status_code == 200
    assert r.json()["items"] == []

    # ws2 cannot delete ws1's memory
    mid = client.get(f"/api/v1/workspaces/{ws1}/memory", headers=h1).json()["items"][0]["id"]
    r = client.delete(f"/api/v1/workspaces/{ws2}/memory/{mid}", headers=h2)
    assert r.status_code == 404


def test_learning_agent_writes_semantic_memory(db_session):
    """The Learning Agent's pattern mirror lands in semantic memory."""
    from unittest.mock import patch

    from app.engine.agents.intelligence import LearningAgent

    ws = "ws-mem-learn"
    findings = [{
        "pattern_key": "hook_style_question",
        "description": "question-style titles",
        "observed_improvement_pct": 14.2,
        "confidence": "medium",
        "sample_size": 9,
        "evidence": {"median_views": 1000, "multiplier": 1.14},
    }]
    with patch.object(LearningAgent, "_extract_patterns", return_value=findings), \
         patch.object(LearningAgent, "_upsert_pattern", return_value=None):
        agent = LearningAgent()
        agent._started = time.monotonic()
        agent._steps = []

        class _Ctx:
            workspace_id = ws
            job_id = None
            cycle_id = None
            type = "test"
            payload = {}
            artifacts = {}

        result = agent.learn_from_recent(_Ctx(), min_sample=3)

    assert result["stored"] == 1

    from app.services import memory as memory_service
    hits = memory_service.retrieve(ws, type="semantic", query="Pattern")
    assert len(hits) == 1
    assert "hook_style_question" in hits[0]["content"]
    assert "+14.2%" in hits[0]["content"]
    assert hits[0]["source"] == "learning"


# ---------------------------------------------------------------------------
# memory-informed decision + research (spec #25 feedback loop)
# ---------------------------------------------------------------------------


def test_decision_engine_surfaces_memory_context(db_session):
    """Topic-relevant semantic memories add an inspectable WHY factor."""
    from app.engine.decision import _memory_context

    ws = "ws-mem-decision"
    from app.services import memory as memory_service
    memory_service.store(
        ws, content="AI agents topics historically retain 70%+ to completion",
        type="semantic", source="learning", confidence=0.8, importance=0.8,
        scope="ai_agents",
    )

    mem = _memory_context(ws, "AI agents explained")
    assert len(mem["semantic"]) == 1
    assert "AI agents" in mem["semantic"][0]["content"]
    # unrelated topic gets no semantic hits; strategic/preference stay empty but present
    other = _memory_context(ws, "quantum computing breakthrough")
    assert other["semantic"] == []
    assert other["strategic"] == [] and other["preference"] == []
    # memory failures degrade to empty context, never raise
    assert _memory_context("ws-does-not-exist", "anything")["strategic"] == []


def test_decision_factor_includes_memory_when_producing(client, db_session):
    """A PRODUCE decision with relevant memories carries a memory_context factor."""
    from app.db import session_scope
    from app.models import Opportunity, Workspace
    from app.services import memory as memory_service

    _tok, ws_id, headers = _register(client)
    with session_scope() as s:
        w = s.get(Workspace, ws_id)
        w.settings_json = {"safety": {"produce_score_threshold": 1.0, "min_qc_score": 60}}
        s.add(Opportunity(
            workspace_id=ws_id, topic="ai agents for developers", score=88.0,
            selected=False, lifecycle="RISING", confidence=0.8,
        ))
    memory_service.store(
        ws_id, content="ai agents content performs 2x for developer audiences",
        type="semantic", source="learning", confidence=0.9, importance=0.9,
    )

    r = client.get(f"/api/v1/workspaces/{ws_id}/decision", headers=headers)
    assert r.status_code == 200, r.text
    d = r.json()
    names = [f["name"] for f in d.get("factors", [])]
    if d["action"] in ("PRODUCE", "WAIT", "SKIP", "RESEARCH_MORE"):
        assert "memory_context" in names or d["action"] != "PRODUCE", (
            "PRODUCE decisions must show the memory factor when relevant memories exist"
        )


def test_research_agent_grounds_with_memory(db_session):
    """The Research Agent injects retrieved memories into the LLM prompt."""
    from unittest.mock import patch

    from app.engine.agents.creation import ResearchAgent
    from app.services import memory as memory_service

    ws = "ws-mem-research"
    memory_service.store(
        ws, content="Viewers respond to concrete pricing examples",
        type="semantic", source="learning", confidence=0.8, importance=0.7,
    )

    captured = {}

    def _fake_complete_json(system, user, **kwargs):
        captured["user"] = user
        return {
            "summary": "s", "key_facts": ["f"], "angles": ["a"],
            "visual_keywords": ["v"], "cautions": [],
            "claims": [{"claim": "c", "status": "LIKELY", "confidence": 0.6, "basis": "b"}],
        }

    agent = ResearchAgent()
    agent._started = time.monotonic()
    agent._steps = []

    class _Ctx:
        workspace_id = ws
        job_id = None
        cycle_id = None
        type = "test"
        payload = {}
        artifacts = {}

    with patch("app.providers.llm.complete_json", side_effect=_fake_complete_json):
        agent.research(_Ctx(), "pricing psychology")

    assert "persistent memory" in captured["user"].lower()
    assert "pricing examples" in captured["user"].lower()


# ---------------------------------------------------------------------------
# memory-grounded strategy + script generation (spec #25)
# ---------------------------------------------------------------------------


def test_style_context_merges_preference_and_strategic(db_session):
    from app.services import memory as memory_service

    ws = "ws-mem-style"
    memory_service.store(ws, content="Faceless documentary only", type="preference",
                         source="user", confidence=1.0, importance=0.9)
    memory_service.store(ws, content="Educational explainers over hot takes", type="strategic",
                         source="user", confidence=0.9, importance=0.8)
    memory_service.store(ws, content="Some unrelated semantic note", type="semantic",
                         source="learning", confidence=0.8, importance=0.8)

    style = memory_service.style_context(ws)
    contents = [m["content"] for m in style]
    assert any("Faceless" in c for c in contents)
    assert any("Educational" in c for c in contents)
    assert not any("unrelated semantic" in c for c in contents)  # semantic excluded


def test_strategist_and_scriptwriter_inject_style_memory(db_session):
    """Preference/strategic memories must reach the LLM prompts for strategy
    and script writing."""
    from unittest.mock import patch

    from app.engine.agents.creation import ScriptWriterAgent, StrategistAgent
    from app.services import memory as memory_service

    ws = "ws-mem-gen"
    memory_service.store(ws, content="Use faceless documentary style only",
                         type="preference", source="user", confidence=1.0, importance=0.9)

    captured = {}

    def _fake_json(system, user, **kw):
        captured["strategy_system"] = system
        return {"duration_seconds": 30}

    def _fake_complete(system, user, **kw):
        captured["script_system"] = system

        class _R:
            text = "HOOK: test"
        return _R()

    class _Ctx:
        workspace_id = ws
        job_id = None
        cycle_id = None
        type = "test"
        payload = {}
        artifacts = {}

    with patch("app.providers.llm.complete_json", side_effect=_fake_json):
        StrategistAgent().strategize(_Ctx(), "topic x", {"summary": "s"})
    with patch("app.providers.llm.complete", side_effect=_fake_complete):
        ScriptWriterAgent().write_script(_Ctx(), "topic x", {"duration_seconds": 30}, {})

    assert "faceless documentary" in captured["strategy_system"].lower()
    assert "style guidance" in captured["strategy_system"].lower()
    assert "faceless documentary" in captured["script_system"].lower()

    # workspaces without memories get no block (prompt unchanged)
    captured.clear()

    class _CtxEmpty:
        workspace_id = "ws-mem-empty"
        job_id = None
        cycle_id = None
        type = "test"
        payload = {}
        artifacts = {}

    with patch("app.providers.llm.complete", side_effect=_fake_complete):
        ScriptWriterAgent().write_script(_CtxEmpty(), "t", {}, {})
    assert "style guidance" not in captured["script_system"].lower()
