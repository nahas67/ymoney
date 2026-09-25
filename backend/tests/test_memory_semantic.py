"""Semantic memory retrieval: overlap ranking over keyword candidates."""
from __future__ import annotations


def test_semantic_score_units():
    from app.services.memory import semantic_score

    s, matched = semantic_score("money habits", "money habits compound over time")
    assert s == 1.0 and set(matched) == {"money", "habits"}
    s, _ = semantic_score("money habits investing", "money habits compound over time")
    assert s == round(2 / 3, 3)  # 2 of 3 query terms covered
    s, _ = semantic_score("quantum computing", "money habits compound over time")
    assert s == 0.0
    assert semantic_score("", "something")[0] == 0.0
    assert semantic_score("money", "")[0] == 0.0


def test_retrieve_semantic_prefers_coverage_over_importance(workspace_with_user):
    from app.services import memory as mem

    ws = workspace_with_user["workspace"]
    mem.store(ws, content="unrelated channel statistics and schedules", type="semantic",
              importance=0.9)
    mem.store(ws, content="money habits compound over time", type="semantic", importance=0.2)

    hits = mem.retrieve_semantic(ws, "money habits", limit=5)
    assert len(hits) == 2
    assert hits[0]["semantic_score"] == 1.0
    assert set(hits[0]["matched_terms"]) == {"money", "habits"}
    assert hits[0]["content"].startswith("money habits")


def test_retrieve_for_topic_carries_scores(workspace_with_user):
    from app.services import memory as mem

    ws = workspace_with_user["workspace"]
    mem.store(ws, content="money habits compound over time", type="semantic", importance=0.5)

    hits = mem.retrieve_for_topic(ws, "money habits for beginners")
    assert hits and hits[0]["semantic_score"] > 0
    assert "money" in hits[0]["matched_terms"]
    assert mem.retrieve_for_topic(ws, "   ") == []


def test_memory_api_semantic_flag():
    import uuid

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"mem{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ws_id = r.json()["workspace"]["id"]

    for content in ("money habits compound over time", "unrelated channel statistics"):
        r = client.post(f"/api/v1/workspaces/{ws_id}/memory/store", headers=headers,
                        json={"content": content, "type": "semantic"})
        assert r.status_code == 200, r.text

    r = client.get(f"/api/v1/workspaces/{ws_id}/memory", headers=headers,
                   params={"q": "money habits", "semantic": "true"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items and items[0]["content"].startswith("money habits")
    assert items[0]["semantic_score"] == 1.0

    r = client.post(f"/api/v1/workspaces/{ws_id}/memory/retrieve", headers=headers,
                    json={"query": "money habits", "semantic": True})
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["semantic_score"] == 1.0
