"""Lane B ModelRouter tests: tiers, budget/privacy/health routing, fallbacks, log."""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app.engine.intelligence.router import (
    LLMRoutingError,
    ModelCapabilityRegistry,
    ModelRouter,
    PrivacyRefusal,
    RouteRequest,
)


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[dict, str]:
    email = f"lane-b-router-{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def _req(**kw) -> RouteRequest:
    base = {"task_type": "general", "workspace_id": "ws-test"}
    base.update(kw)
    return RouteRequest(**base)


# ---------------------------------------------------------------------------
# tier / strategy routing
# ---------------------------------------------------------------------------

class TestTierRouting:
    def test_low_budget_routes_to_fast(self):
        router = ModelRouter()
        d = router.route(_req(budget_usd=0.10))
        assert d.tier == "FAST"
        assert d.remote is True
        assert d.model  # resolved from provider settings, never empty for remote
        assert "FAST" in d.reason or "selected_tier=FAST" in d.reason

    def test_top_quality_routes_to_premium(self):
        router = ModelRouter()
        d = router.route(_req(quality_required="top"))
        assert d.tier == "PREMIUM"

    def test_high_complexity_routes_to_quality(self):
        router = ModelRouter()
        d = router.route(_req(complexity=0.9))
        assert d.tier == "PREMIUM"

    def test_latency_sensitive_routes_to_fast(self):
        router = ModelRouter()
        d = router.route(_req(latency_sensitive=True, complexity=0.2))
        assert d.tier == "FAST"
        assert d.latency_class == "low"

    def test_explicit_tier_pin_wins(self):
        router = ModelRouter()
        d = router.route(_req(tier="PREMIUM", complexity=0.0))
        assert d.tier == "PREMIUM"
        assert "requested_tier=PREMIUM" in d.reason

    def test_unknown_tier_raises(self):
        router = ModelRouter()
        with pytest.raises(LLMRoutingError):
            router.route(_req(tier="NOPE"))

    def test_vision_modality_needs_vision_tier(self):
        router = ModelRouter()
        d = router.route(_req(modality="vision"))
        assert d.tier == "HIGH_QUALITY"

    def test_workspace_preference_is_honored(self):
        router = ModelRouter()
        ws = {"intelligence": {"provider_preference": "FAST"}}
        d = router.route(_req(workspace_settings=ws, complexity=0.9))
        assert d.tier == "FAST"

    def test_routing_strategies(self):
        router = ModelRouter()
        cost = router.route(_req(workspace_settings={"intelligence": {"routing_strategy": "cost"}}))
        assert cost.tier == "FAST"
        quality = router.route(_req(workspace_settings={"intelligence": {"routing_strategy": "quality"}}))
        assert quality.tier == "PREMIUM"
        latency = router.route(_req(workspace_settings={"intelligence": {"routing_strategy": "latency"}}))
        assert latency.tier == "FAST"

    def test_disabled_tiers_fall_back_to_local(self):
        router = ModelRouter()
        ws = {"intelligence": {"disabled_tiers": ["FAST", "BALANCED", "HIGH_QUALITY", "PREMIUM"]}}
        d = router.route(_req(workspace_settings=ws))
        assert d.tier in ("LOCAL_ONLY", "PRIVATE")
        assert d.remote is False

    def test_workspace_model_override_resolves(self):
        router = ModelRouter()
        ws = {"intelligence": {"models": {"fast": "custom-fast-model"}}}
        d = router.route(_req(budget_usd=0.05, workspace_settings=ws))
        assert d.tier == "FAST"
        assert d.model == "custom-fast-model"


# ---------------------------------------------------------------------------
# privacy: LOCAL_ONLY / PRIVATE never go remote
# ---------------------------------------------------------------------------

class TestPrivacy:
    def test_local_only_tier_is_local(self):
        router = ModelRouter()
        d = router.route(_req(tier="LOCAL_ONLY"))
        assert d.remote is False

    def test_private_tier_is_local(self):
        router = ModelRouter()
        d = router.route(_req(tier="PRIVATE"))
        assert d.remote is False

    def test_remote_tier_refused_under_private_mode(self):
        router = ModelRouter()
        ws = {"intelligence": {"privacy_mode": "private"}}
        with pytest.raises(PrivacyRefusal):
            router.route(_req(tier="PREMIUM", workspace_settings=ws))

    def test_remote_tier_refused_under_local_only_mode(self):
        router = ModelRouter()
        ws = {"intelligence": {"privacy_mode": "local_only"}}
        with pytest.raises(PrivacyRefusal):
            router.route(_req(tier="FAST", workspace_settings=ws))

    def test_require_remote_refused_under_private_mode(self):
        router = ModelRouter()
        ws = {"intelligence": {"privacy_mode": "PRIVATE"}}
        with pytest.raises(PrivacyRefusal):
            router.route(_req(require_remote=True, workspace_settings=ws))

    def test_require_remote_refused_when_remote_disallowed(self):
        router = ModelRouter()
        ws = {"intelligence": {"remote_allowed": False}}
        with pytest.raises(PrivacyRefusal):
            router.route(_req(require_remote=True, workspace_settings=ws))

    def test_private_mode_routes_local_without_being_asked(self):
        router = ModelRouter()
        ws = {"intelligence": {"privacy_mode": "private"}}
        d = router.route(_req(workspace_settings=ws))
        assert d.remote is False
        assert d.tier in ("LOCAL_ONLY", "PRIVATE")


# ---------------------------------------------------------------------------
# health + fallback chains observable via routing_log
# ---------------------------------------------------------------------------

class TestHealthAndFallbacks:
    def test_decision_records_fallbacks_and_reason(self):
        router = ModelRouter()
        d = router.route(_req(task_type="script"))
        assert d.fallbacks  # ordered chain after the pick
        assert d.tier not in d.fallbacks
        assert d.task_type == "script"
        assert isinstance(d.provider_health, dict)

    def test_routing_log_observes_decisions(self):
        router = ModelRouter()
        router.route(_req(task_type="task-logged-1", workspace_id="ws-a"))
        router.route(_req(task_type="task-logged-2", workspace_id="ws-b"))
        entries = router.log()
        assert any(e["task_type"] == "task-logged-1" for e in entries)
        only_a = router.log("ws-a")
        assert all(e["workspace_id"] == "ws-a" for e in only_a)
        assert any(e["task_type"] == "task-logged-1" for e in only_a)

    def test_health_failure_reroutes_away_from_remote(self):
        router = ModelRouter()
        router.report_failure("remote")
        assert router.health()["remote"] is False
        d = router.route(_req())
        assert d.remote is False  # local tiers are the only healthy slot
        router.report_success("remote")
        d2 = router.route(_req())
        assert d2.remote is True

    def test_complete_falls_back_on_provider_error(self):
        import app.providers.llm as llm_mod

        router = ModelRouter()
        calls: list[str | None] = []

        real_complete = llm_mod.complete

        def flaky(system, user, **kw):
            calls.append(kw.get("model"))
            if len(calls) == 1:
                raise llm_mod.LLMError("boom")
            return real_complete(system, user, **kw)

        llm_mod.complete = flaky
        try:
            result = router.complete("sys", "hi", request=_req(workspace_id="ws-fb"))
        finally:
            llm_mod.complete = real_complete
        assert len(calls) == 2  # selected tier failed, fallback served
        assert result is not None

    def test_complete_raises_when_everything_fails(self, monkeypatch):
        import app.providers.llm as llm_mod

        router = ModelRouter()
        monkeypatch.setattr(
            llm_mod, "complete",
            lambda *a, **k: (_ for _ in ()).throw(llm_mod.LLMError("down")),
        )
        with pytest.raises(LLMRoutingError):
            router.complete("sys", "hi", request=_req())

    def test_registry_enable_disable(self):
        registry = ModelCapabilityRegistry()
        registry.set_enabled("FAST", False)
        assert registry.get("FAST").enabled is False
        router = ModelRouter(registry)
        d = router.route(_req(budget_usd=0.05))
        assert d.tier != "FAST"


# ---------------------------------------------------------------------------
# guardrail: agents must not hardcode vendor model names
# ---------------------------------------------------------------------------

_VENDOR_MODEL_RE = re.compile(
    r"\b(gpt-3|gpt-4|gpt-4o|o1-|o3-|claude-3|claude-sonnet|claude-opus|"
    r"claude-haiku|gemini-|grok-|llama-|mistral-|mixtral-|deepseek-|qwen-)",
    re.IGNORECASE,
)


def test_no_hardcoded_vendor_models_in_agents_or_router():
    from pathlib import Path

    roots = [
        Path("backend/app/engine/agents"),
        Path("app/engine/agents"),
    ]
    agent_dir = next((p for p in roots if p.is_dir()), None)
    assert agent_dir is not None
    hits = []
    for path in sorted(agent_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in _VENDOR_MODEL_RE.finditer(text):
            hits.append(f"{path.name}: {m.group(0)}")
    assert hits == [], f"hardcoded vendor model names in agents: {hits}"

    router_roots = [Path("backend/app/engine/intelligence/router.py"),
                    Path("app/engine/intelligence/router.py")]
    router_path = next((p for p in router_roots if p.is_file()), None)
    assert router_path is not None
    assert _VENDOR_MODEL_RE.search(router_path.read_text(encoding="utf-8")) is None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

class TestRoutingRoutes:
    def test_route_returns_model_and_reason(self, client):
        headers, ws_id = _register(client)
        r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/routing/route",
                        headers=headers, json={"task_type": "script", "complexity": 0.9})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tier"] in ("PREMIUM", "HIGH_QUALITY")
        assert body["model"]
        assert body["reason"]
        assert isinstance(body["fallbacks"], list) and body["fallbacks"]

    def test_routing_log_and_health(self, client):
        headers, ws_id = _register(client)
        task = f"log-task-{os.urandom(3).hex()}"
        client.post(f"/api/v1/workspaces/{ws_id}/intelligence/routing/route",
                    headers=headers, json={"task_type": task})
        log = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/routing/log",
                         headers=headers)
        assert log.status_code == 200
        assert any(e["task_type"] == task for e in log.json()["entries"])
        health = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/routing/health",
                            headers=headers)
        assert health.status_code == 200
        assert "remote" in health.json()["providers"]

    def test_routing_log_is_workspace_isolated(self, client):
        headers, ws_id = _register(client)
        task = f"isolated-{os.urandom(3).hex()}"
        client.post(f"/api/v1/workspaces/{ws_id}/intelligence/routing/route",
                    headers=headers, json={"task_type": task})
        headers2, ws2 = _register(client)
        log2 = client.get(f"/api/v1/workspaces/{ws2}/intelligence/routing/log",
                          headers=headers2)
        assert log2.status_code == 200
        assert all(e["task_type"] != task for e in log2.json()["entries"])
        # cross-workspace access reads as 404
        x = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/routing/log",
                       headers=headers2)
        assert x.status_code == 404

    def test_private_mode_refuses_remote_via_api(self, client, db_session):
        from app.models import Workspace

        headers, ws_id = _register(client)
        ws = db_session.get(Workspace, ws_id)
        ws.settings_json = {"intelligence": {"privacy_mode": "private"}}
        db_session.commit()
        r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/routing/route",
                        headers=headers, json={"tier": "PREMIUM"})
        assert r.status_code == 403
        r2 = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/routing/route",
                         headers=headers, json={"require_remote": True})
        assert r2.status_code == 403
        ok = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/routing/route",
                         headers=headers, json={"tier": "LOCAL_ONLY"})
        assert ok.status_code == 200
        assert ok.json()["remote"] is False
