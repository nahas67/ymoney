"""Scored provider selection: deterministic ranking, reasons, explanations."""
from __future__ import annotations

from app.engine.provider_scoring import score_images, score_tts

NOKEYS = {"elevenlabs_key": False, "kokoro_url": False, "chatterbox_ready": False,
          "qwen_url": False, "production": False}
FULL = {"elevenlabs_key": True, "kokoro_url": True, "chatterbox_ready": True,
        "qwen_url": True, "production": False}
IMG_NONE = {"pexels_key": False, "xkiro_key": False, "openai_url": False, "production": False}


def test_default_task_prefers_keyless_edge():
    ranked = score_tts({}, NOKEYS)
    assert [s.provider for s in ranked][:1] == ["edge"]
    eleven = next(s for s in ranked if s.provider == "elevenlabs")
    assert eleven.dims["reliability"]["score"] == 0.0
    assert "key" in eleven.dims["reliability"]["reason"].lower()


def test_cloning_task_prefers_cloners():
    ranked = score_tts({"needs_cloning": True}, FULL)
    assert ranked[0].provider in ("chatterbox", "elevenlabs", "qwen3")
    assert "cloning" in ranked[0].dims["task_fit"]["reason"].lower()


def test_budget_task_penalizes_paid():
    ranked = score_tts({"needs_cloning": True, "budget_sensitive": True}, FULL)
    assert ranked[0].provider == "chatterbox"
    eleven = next(s for s in ranked if s.provider == "elevenlabs")
    assert eleven.dims["cost_efficiency"]["score"] < 0.3


def test_offline_task_penalizes_network():
    cfg = dict(NOKEYS, kokoro_url=True)
    ranked = score_tts({"offline_only": True}, cfg)
    assert ranked[0].provider == "kokoro"
    edge = next(s for s in ranked if s.provider == "edge")
    assert edge.dims["task_fit"]["score"] <= 0.20


def test_mock_blocked_in_production():
    ranked = score_tts({}, dict(NOKEYS, production=True))
    mock = next(s for s in ranked if s.provider == "mock")
    assert mock.dims["reliability"]["score"] == 0.0
    assert "production" in mock.dims["reliability"]["reason"].lower()


def test_continuity_rewards_incumbent():
    ranked = score_tts({}, NOKEYS, current="kokoro")
    kokoro = next(s for s in ranked if s.provider == "kokoro")
    assert kokoro.dims["continuity"] == {"score": 1.0, "weight": 0.05, "reason": "incumbent"}
    edge = next(s for s in ranked if s.provider == "edge")
    assert edge.dims["continuity"]["score"] == 0.5


def test_explanation_names_top_driver():
    top = score_tts({}, NOKEYS)[0]
    assert top.provider in top.explanation
    assert "task_fit" in top.explanation or "reliability" in top.explanation
    d = top.to_dict()
    assert {"provider", "capability", "dims", "weighted", "explanation"} <= set(d)


def test_images_keyless_default_and_stock_steered():
    ranked = score_images({}, IMG_NONE)
    assert ranked[0].provider == "pollinations"
    stocked = score_images({"needs_stock_photo": True}, dict(IMG_NONE, pexels_key=True))
    assert stocked[0].provider == "pexels"
    assert "stock" in stocked[0].dims["task_fit"]["reason"].lower()


def test_status_endpoints_carry_ranked():
    import uuid

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"rnk{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ws_id = r.json()["workspace"]["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/connections/tts", headers=headers)
    assert r.status_code == 200, r.text
    ranked = r.json()["ranked"]
    assert len(ranked) == 6
    assert all({"provider", "weighted", "explanation", "dims"} <= set(s) for s in ranked)

    r = client.get(f"/api/v1/workspaces/{ws_id}/connections/images", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["ranked"]) == 5
