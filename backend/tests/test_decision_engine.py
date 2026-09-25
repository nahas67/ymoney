"""Work 05 Lane A: DecisionEngine primitives, fallback, shadow, integrations.

Provider-independent intelligence runtime. All decisions work offline via the
deterministic + local providers; remote providers degrade to UNAVAILABLE and
the engine falls back without raising. AI judgment never overrides
auth/RBAC, budgets, publish authorization, compliance, idempotency or DB
invariants — the engine only advises; callers keep their deterministic
guards (asserted by the DISABLED-mode identity tests below).
"""

from __future__ import annotations

import uuid

import pytest

from app.engine.intelligence.decision import (
    DecisionEngine,
    get_intelligence_settings,
)

ALL_KINDS = (
    "boolean",
    "choose",
    "rank",
    "rerank",
    "score",
    "classify",
    "compare",
    "route",
    "verify",
)

_SAMPLE_PAYLOADS = {
    "boolean": {"question": "is this hook promising?", "context": "great viral topic"},
    "choose": {"options": ["alpha launch", "beta launch"], "criterion": "alpha launch plan"},
    "rank": {"items": ["alpha launch", "unrelated weather"], "criterion": "alpha launch"},
    "rerank": {"items": ["alpha launch", "unrelated weather"], "criterion": "alpha launch"},
    "score": {"item": "great viral hook with 3 tips?"},
    "classify": {"item": "alpha launch", "labels": ["alpha", "weather"]},
    "compare": {"a": "alpha launch plan", "b": "weather report", "criterion": "alpha launch"},
    "route": {"task": "alpha launch plan", "routes": ["alpha", "weather"]},
    "verify": {"claim": "alpha launch", "evidence": "alpha launch plan notes"},
}


def _engine(**kw):
    kw.setdefault("persist", False)
    return DecisionEngine("", **kw)


class TestPrimitives:
    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_every_primitive_returns_output_and_record(self, kind):
        out, rec = _engine().decide(kind, dict(_SAMPLE_PAYLOADS[kind]))
        assert out is not None
        assert rec.kind == kind
        assert rec.actual_provider == "deterministic"
        assert rec.mode == "SHADOW"
        assert rec.latency_ms >= 0

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_typed_method_matches_decide(self, kind):
        fn = getattr(_engine(), kind)
        out, rec = fn(dict(_SAMPLE_PAYLOADS[kind]))
        assert rec.kind == kind
        assert out is not None

    def test_boolean_shape(self):
        out, _ = _engine().boolean({"question": "great viral idea", "context": "strong"})
        assert out["value"] is True

    def test_choose_picks_overlap(self):
        out, _ = _engine().choose(
            {"options": ["weather report", "alpha launch plan"], "criterion": "alpha launch"})
        assert out["choice"] == "alpha launch plan"
        assert out["index"] == 1

    def test_rank_orders_by_overlap(self):
        out, _ = _engine().rank(
            {"items": ["weather report", "alpha launch plan"], "criterion": "alpha launch"})
        assert out["order"][0] == 1

    def test_verify_bands(self):
        out, _ = _engine().verify({"claim": "alpha launch", "evidence": "alpha launch plan"})
        assert out["status"] == "SUPPORTED"
        out, _ = _engine().verify({"claim": "quantum baking", "evidence": "alpha launch plan"})
        assert out["status"] == "UNVERIFIED"


class TestModesAndSettings:
    def test_defaults_are_shadow_and_deterministic(self):
        assert get_intelligence_settings(None)["decision_mode"] == "SHADOW"
        assert get_intelligence_settings({})["provider_preference"] == "deterministic"

    def test_unknown_mode_and_provider_fall_back(self):
        s = get_intelligence_settings({"intelligence": {
            "decision_mode": "BOGUS", "provider_preference": "nope"}})
        assert s["decision_mode"] == "SHADOW"
        assert s["provider_preference"] == "deterministic"

    def test_all_workspace_keys_present(self):
        s = get_intelligence_settings(None)
        assert set(s) == {
            "decision_mode", "provider_preference", "remote_allowed",
            "context_filtering", "browser_enabled", "allowed_domains",
            "routing_strategy", "privacy_mode",
        }

    def test_unknown_mode_on_engine_falls_back_to_shadow(self):
        eng = _engine(mode="BOGUS")
        assert eng.mode == "SHADOW"


class TestFallback:
    def test_claude_unavailable_falls_back_with_reason(self):
        from app.engine.intelligence.providers import ClaudeDecisionProvider

        assert ClaudeDecisionProvider().health().status == "UNAVAILABLE"
        out, rec = _engine().decide("boolean", dict(_SAMPLE_PAYLOADS["boolean"]),
                                    provider="claude")
        assert out is not None
        assert rec.requested_provider == "claude"
        assert rec.actual_provider in ("local", "deterministic")
        assert rec.fallback_reason != ""

    def test_unknown_provider_falls_back(self):
        out, rec = _engine().decide("boolean", dict(_SAMPLE_PAYLOADS["boolean"]),
                                    provider="nope")
        assert out is not None
        assert rec.actual_provider in ("local", "deterministic")

    def test_deterministic_and_local_always_available(self):
        from app.engine.intelligence.providers import (
            DeterministicProvider,
            LocalHeuristicProvider,
        )

        assert DeterministicProvider().health().status == "AVAILABLE"
        assert LocalHeuristicProvider().health().status == "AVAILABLE"

    def test_provider_capabilities_cover_all_kinds(self):
        from app.engine.intelligence.providers import (
            ClaudeDecisionProvider,
            DeterministicProvider,
            LLMDecisionProvider,
            LocalHeuristicProvider,
        )

        for cls in (DeterministicProvider, LocalHeuristicProvider,
                    LLMDecisionProvider, ClaudeDecisionProvider):
            assert set(ALL_KINDS) <= set(cls.capabilities), cls

    def test_batch_variants(self):
        outs, recs = _engine().boolean_batch([dict(_SAMPLE_PAYLOADS["boolean"])] * 3)
        assert len(outs) == 3 and len(recs) == 3
        outs, recs = _engine().rank_batch([dict(_SAMPLE_PAYLOADS["rank"])] * 2)
        assert len(outs) == 2 and len(recs) == 2
        outs, recs = _engine().score_batch([dict(_SAMPLE_PAYLOADS["score"])])
        assert len(outs) == 1 and len(recs) == 1
        outs, _ = _engine().verify_batch([])
        assert outs == []


class TestShadow:
    def test_run_shadow_agreement(self):
        from app.engine.intelligence.shadow import run_shadow

        entry = run_shadow("", "boolean", {"question": "great excellent viral"},
                           candidate_provider="local", persist=False)
        assert entry["agree"] is True
        assert entry["baseline"] is not None
        assert entry["candidate"] is not None

    def test_run_shadow_disagreement(self):
        from app.engine.intelligence.shadow import run_shadow

        # Negative sentiment (deterministic False) but high stat score (local True).
        entry = run_shadow("", "boolean", {"question": "bad risky fail"},
                           candidate_provider="local", persist=False)
        assert entry["agree"] is False

    def test_shadow_never_raises(self):
        from app.engine.intelligence.shadow import run_shadow

        entry = run_shadow("", "boolean", {}, candidate_provider="bogus-provider",
                           persist=False)
        assert "agree" in entry

    def test_shadow_report_aggregates(self, workspace_with_user):
        from app.engine.intelligence.shadow import run_shadow, shadow_report

        ws = workspace_with_user["workspace"]
        run_shadow(ws, "boolean", {"question": "great excellent viral"},
                   candidate_provider="local", persist=True)
        run_shadow(ws, "boolean", {"question": "bad risky fail"},
                   candidate_provider="local", persist=True)
        report = shadow_report(ws)
        assert report["total"] >= 2
        assert report["agreed"] >= 1
        assert 0.0 <= (report["agreement_rate"] or 0.0) <= 1.0
        assert "boolean" in report["by_kind"]


class TestDisabledIdentical:
    """DISABLED mode: advisory hooks return inputs untouched."""

    def _disabled_ws(self, workspace_with_user):
        from app.db import session_scope
        from app.models import Workspace

        ws = workspace_with_user["workspace"]
        with session_scope() as s:
            row = s.get(Workspace, ws)
            row.settings_json = {"intelligence": {"decision_mode": "DISABLED"}}
        return ws

    def test_trend_advice_identity(self, workspace_with_user):
        from app.engine.intelligence.integrations import advise_trend_scores

        ws = self._disabled_ws(workspace_with_user)
        items = [{"topic": "alpha", "score": 80.0}, {"topic": "beta", "score": 10.0}]
        assert advise_trend_scores([dict(i) for i in items], workspace_id=ws) == items

    def test_repurpose_advice_identity(self, workspace_with_user):
        from app.engine.intelligence.integrations import advise_repurpose_moments

        ws = self._disabled_ws(workspace_with_user)
        moments = [{"hook": "h1", "score": 9.0}, {"hook": "h2", "score": 1.0}]
        assert advise_repurpose_moments([dict(m) for m in moments], workspace_id=ws) == moments

    def test_diversity_advice_empty(self, workspace_with_user):
        from app.engine.intelligence.integrations import advise_diversity

        ws = self._disabled_ws(workspace_with_user)
        assert advise_diversity([{"hook": "h"}], [], workspace_id=ws) == {
            "mode": "DISABLED", "advisory": []}

    def test_broll_advice_identity(self, workspace_with_user):
        from app.engine.intelligence.integrations import advise_broll_rank

        ws = self._disabled_ws(workspace_with_user)
        cands = [{"query": "q1"}, {"query": "q2"}]
        assert advise_broll_rank([dict(c) for c in cands], workspace_id=ws) == cands

    def test_engine_disabled_ignores_provider_request(self):
        out_requested, _ = _engine(mode="DISABLED").decide(
            "boolean", dict(_SAMPLE_PAYLOADS["boolean"]), provider="local")
        out_plain, rec = _engine(mode="DISABLED").decide(
            "boolean", dict(_SAMPLE_PAYLOADS["boolean"]))
        assert out_requested == out_plain
        assert rec.actual_provider == "deterministic"


class TestSecrets:
    def test_sanitize_redacts_keys_and_values(self):
        from app.engine.intelligence.decision import _sanitize

        clean = _sanitize({"api_key": "sk-abcdefgh12345678", "topic": "alpha"})
        assert clean["api_key"] == "[REDACTED]"
        assert clean["topic"] == "alpha"

    def test_persisted_inputs_are_redacted(self, workspace_with_user):
        from sqlalchemy import select

        from app.db import session_scope
        from app.models.intelligence import DecisionRecordRow

        ws = workspace_with_user["workspace"]
        eng = DecisionEngine(ws, mode="SHADOW", persist=True)
        eng.decide("boolean", {"question": "q", "api_key": "sk-abcdefgh12345678"})
        with session_scope() as s:
            row = s.scalars(select(DecisionRecordRow).where(
                DecisionRecordRow.workspace_id == ws).order_by(
                DecisionRecordRow.created_at.desc())).first()
            assert row is not None
            assert row.input_json.get("api_key") == "[REDACTED]"


# -- HTTP surface ------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"de{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return (data["workspace"]["id"],
            {"Authorization": f"Bearer {data['access_token']}"})


@pytest.mark.parametrize("kind", ("boolean", "choose", "rank", "classify", "verify"))
def test_route_decision_kinds(tmp_path, monkeypatch, kind):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    payload: dict = {"input": dict(_SAMPLE_PAYLOADS[kind])}
    r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/decisions/{kind}",
                    headers=headers, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["output"] is not None


def test_route_unknown_kind_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/decisions/nope",
                    headers=headers, json={"input": {}})
    assert r.status_code == 404


def test_route_log_and_shadow_report(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    client.post(f"/api/v1/workspaces/{ws_id}/intelligence/decisions/boolean",
                headers=headers, json={"input": {"question": "great viral hook"}})
    r = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/decisions/log",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) >= 1
    r = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/decisions/shadow-report",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert "total" in r.json()


def test_workspace_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    _ws_b, headers_b = _register(client)
    client.post(f"/api/v1/workspaces/{ws_a}/intelligence/decisions/boolean",
                headers=headers_a, json={"input": {"question": "q"}})
    # Cross-workspace reads mask as 404.
    r = client.get(f"/api/v1/workspaces/{ws_a}/intelligence/decisions/log",
                   headers=headers_b)
    assert r.status_code == 404
    r = client.post(f"/api/v1/workspaces/{ws_a}/intelligence/decisions/boolean",
                    headers=headers_b, json={"input": {"question": "q"}})
    assert r.status_code == 404
