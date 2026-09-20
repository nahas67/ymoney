"""Template registry tests: loading, validation, overrides, consumers, API."""
from __future__ import annotations

import pytest

from app.services import templates as tpl


def test_builtins_load_across_modules():
    all_t = tpl.load_all()
    modules = {m for (m, _t, _v) in all_t}
    assert {"captions", "hooks", "motion"} <= modules
    assert len(tpl.list_templates("captions")) == 3
    assert len(tpl.list_templates("hooks")) == 5
    assert len(tpl.list_templates("motion")) == 4


def test_validate_rejects_bad_template():
    assert tpl.validate_template({})  # missing keys
    assert tpl.validate_template({"module": "x", "id": "y", "version": "v1",
                                  "title": "t", "payload": "not-a-dict"})
    assert tpl.validate_template({"module": "captions", "id": "pop", "version": "v1",
                                  "title": "Pop", "payload": {"ass_style": "x"}}) == []


def test_get_latest_and_version_pin():
    latest = tpl.get_template("captions", "pop")
    assert latest["version"] == "v1"
    pinned = tpl.get_template("captions", "pop", "v1")
    assert pinned["payload"] == latest["payload"]
    with pytest.raises(KeyError):
        tpl.get_template("captions", "nope")
    with pytest.raises(KeyError):
        tpl.get_template("captions", "pop", "v99")


def test_apply_override_deep_merges():
    base = {"payload": {"a": 1, "nested": {"x": 1, "y": 2}}, "title": "T"}
    out = tpl.apply_override(base, {"payload": {"nested": {"y": 9}}, "title": "T2"})
    assert out == {"payload": {"a": 1, "nested": {"x": 1, "y": 9}}, "title": "T2"}
    assert base["payload"]["nested"]["y"] == 2  # no mutation


def test_resolve_without_workspace_returns_builtin():
    resolved = tpl.resolve_template("motion", "hook")
    assert resolved["payload"]["kind"] == "hook"
    assert "overridden" not in resolved


def test_resolve_applies_workspace_override(monkeypatch):
    monkeypatch.setattr(tpl, "workspace_overrides",
                        lambda ws: {"motion/hook": {"payload": {"accent": "#ff0000"}}})
    resolved = tpl.resolve_template("motion", "hook", "ws-1")
    assert resolved["payload"]["accent"] == "#ff0000"
    assert resolved["overridden"] is True


def test_consumers_read_registry():
    from app.engine.agents.creation import HOOK_TEMPLATES
    from app.providers import clips as clips_mod
    from app.providers import motion as motion_mod

    assert set(HOOK_TEMPLATES) == {"question", "bold_claim", "story", "statistic", "curiosity_gap"}
    assert all("{topic}" in v for v in HOOK_TEMPLATES.values())
    assert "FontSize=19" in clips_mod.caption_style("pop")
    assert set(motion_mod.CARD_KINDS) == {"hook", "stat", "cta", "lower"}


def test_templates_api_lists_and_details():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"tpl{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/templates", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 12
    assert all("module" in i and "payload" in i for i in items)

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/templates/hooks/question", headers=headers)
    assert r.status_code == 200, r.text
    assert "{topic}" in r.json()["payload"]["template"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/templates/nope/nope", headers=headers)
    assert r.status_code == 404
