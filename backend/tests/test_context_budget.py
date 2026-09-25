"""Lane B context-budget + sanitizer tests: reversible filtering, pins, redaction."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.engine.intelligence.context_budget import (
    PREDEFINED_PINNED_CATEGORIES,
    Category,
    ContextBudgetManager,
    classify,
    estimate_tokens,
)
from app.engine.intelligence.sanitize import (
    REDACTED,
    SecretLeakError,
    assert_no_secrets,
    redact_secrets,
)


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[dict, str]:
    email = f"lane-b-ctx-{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


# ---------------------------------------------------------------------------
# token estimator + classification
# ---------------------------------------------------------------------------

class TestEstimator:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_deterministic(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcde") == 2
        assert estimate_tokens("x" * 400) == 100


class TestClassify:
    def test_safety_text_is_system_critical(self):
        assert classify("Safety constraint: must never publish without approval") == Category.SYSTEM_CRITICAL

    def test_acceptance_criteria_pinned_category(self):
        assert classify("Acceptance criteria: hook under 3 seconds") == Category.SYSTEM_CRITICAL

    def test_objective_is_task_required(self):
        assert classify("Current objective: produce 3 shorts this week") == Category.TASK_REQUIRED

    def test_tool_output_detected(self):
        assert classify("Traceback: AssertionError exit code 1, stderr below") == Category.TOOL_RESULT

    def test_filler_is_low_relevance(self):
        assert classify("lorem ipsum placeholder duplicate, ignore this") == Category.LOW_RELEVANCE

    def test_default_is_recent_working(self):
        assert classify("draft hook line about morning routines") == Category.RECENT_WORKING


# ---------------------------------------------------------------------------
# budget: keep/hide + reversible recall
# ---------------------------------------------------------------------------

class TestBudget:
    def test_reversible_filtering_recalls_exact_original(self):
        mgr = ContextBudgetManager()
        original = "research finding " * 200
        items = [
            mgr.add("User instruction: stay on personal finance", category=Category.TASK_REQUIRED),
            mgr.add(original, category=Category.RESEARCH_SOURCE),
            mgr.add("lorem ipsum filler duplicate", category=Category.LOW_RELEVANCE),
        ]
        result = mgr.budget(items, max_tokens=60)
        assert result.metrics["raw_tokens"] > result.metrics["kept_tokens"]
        assert result.metrics["filtered_tokens"] > 0
        assert result.references
        hidden_texts = {mgr.recall(r["ref_id"]).content for r in result.references}
        assert original in hidden_texts

    def test_pinned_categories_never_filtered(self):
        assert Category.SYSTEM_CRITICAL in PREDEFINED_PINNED_CATEGORIES
        assert Category.TASK_REQUIRED in PREDEFINED_PINNED_CATEGORIES
        mgr = ContextBudgetManager()
        pinned = mgr.add("Safety: must never auto-publish", category=Category.SYSTEM_CRITICAL)
        objective = mgr.add("Objective: ship the video", category=Category.TASK_REQUIRED)
        filler = [mgr.add(f"filler note {i} " * 50, category=Category.LOW_RELEVANCE) for i in range(5)]
        result = mgr.budget([pinned, objective, *filler], max_tokens=5)
        kept_ids = {k["id"] for k in result.kept}
        assert pinned.id in kept_ids
        assert objective.id in kept_ids
        assert result.metrics["over_budget"] is True  # pinned content forces overage

    def test_pinned_false_cannot_unpin_safety(self):
        mgr = ContextBudgetManager()
        item = mgr.add("Safety constraint: forbidden topic list", pinned=False)
        assert item.pinned is True
        result = mgr.budget([item], max_tokens=1)
        assert [k["id"] for k in result.kept] == [item.id]

    def test_chunk_slices_are_exact_and_recallable(self):
        mgr = ContextBudgetManager()
        text = "abcdefghij" * 500
        parts = mgr.chunk(text, max_chars=1000, category=Category.HISTORICAL)
        assert len(parts) == 5
        assert "".join(p.content for p in parts) == text
        for part in parts:
            assert mgr.recall(part.id).content == part.content

    def test_recall_unknown_raises(self):
        mgr = ContextBudgetManager()
        with pytest.raises(KeyError):
            mgr.recall("nope-not-here")

    def test_metrics_shape(self):
        mgr = ContextBudgetManager()
        items = [mgr.add("hello world draft", category=Category.RECENT_WORKING)]
        result = mgr.budget(items, max_tokens=4000)
        metrics = result.metrics
        for key in ("raw_tokens", "kept_tokens", "filtered_tokens", "recalls",
                    "compression_ratio", "latency_ms", "cost_usd"):
            assert key in metrics
        assert metrics["compression_ratio"] == pytest.approx(1.0)
        assert metrics["recalls"] == 0
        mgr.recall(items[0].id)
        assert mgr.recalls == 1

    def test_batch_behavior(self):
        mgr = ContextBudgetManager()
        big = "batch content " * 300
        batches = [
            [mgr.add("User instruction batch one", category=Category.TASK_REQUIRED),
             mgr.add(big, category=Category.HISTORICAL)],
            [mgr.add("second batch objective", category=Category.TASK_REQUIRED),
             mgr.add("small note", category=Category.RECENT_WORKING)],
        ]
        results = mgr.run_batch(batches, max_tokens=40)
        assert len(results) == 2
        assert results[0].references  # big item hidden in batch one
        assert not results[1].references  # batch two fits entirely
        for res in results:
            for ref in res.references:
                assert mgr.recall(ref["ref_id"]).content  # every hidden item recallable


# ---------------------------------------------------------------------------
# sanitizer: key-name + pattern redaction, leak assertion
# ---------------------------------------------------------------------------

FAKE_ANTHROPIC = "sk-ant-" + "a1b2c3d4e5f6g7h8i9j0k1m2"
FAKE_OPENAI = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
FAKE_GITHUB = "ghp_" + "abcdefghij1234567890ABCD"
FAKE_JWT = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_PEM_OPEN = "-" * 5 + "BEGIN " + "PRIVATE KEY" + "-" * 5
_PEM_CLOSE = "-" * 5 + "END " + "PRIVATE KEY" + "-" * 5
FAKE_PEM = _PEM_OPEN + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n" + _PEM_CLOSE


class TestSanitizer:
    def test_key_name_redaction_preserves_siblings(self):
        payload = {"api_key": "hunter2-secret-value", "name": "research brief"}
        out = redact_secrets(payload)
        assert out["api_key"] == REDACTED
        assert out["name"] == "research brief"
        assert payload["api_key"] == "hunter2-secret-value"  # input never mutated

    def test_sensitive_key_variants(self):
        payload = {
            "oauth_token": "tok-12345",
            "clientSecret": "shhh",
            "db_password": "pw",
            "Authorization": "secret thing",
            "author": "kept",  # must NOT match bare-auth heuristics
            "privacy_mode": "private",  # workspace setting must survive
        }
        out = redact_secrets(payload)
        for key in ("oauth_token", "clientSecret", "db_password", "Authorization"):
            assert out[key] == REDACTED, key
        assert out["author"] == "kept"
        assert out["privacy_mode"] == "private"

    def test_pattern_redaction_in_free_text(self):
        text = (f"key {FAKE_ANTHROPIC} then {FAKE_OPENAI} and {FAKE_GITHUB} "
                f"jwt {FAKE_JWT} google ya29.GluffySecretToken123 ok")
        out = redact_secrets(text)
        for secret in (FAKE_ANTHROPIC, FAKE_OPENAI, FAKE_GITHUB, FAKE_JWT,
                       "ya29.GluffySecretToken123"):
            assert secret not in out
        assert REDACTED in out

    def test_env_content_redacted(self):
        env = "OPENAI_API_KEY=" + FAKE_OPENAI + "\nDEBUG=true\nPORT=8000\n"
        out = redact_secrets(env)
        assert FAKE_OPENAI not in out
        assert "DEBUG=true" in out
        assert "PORT=8000" in out

    def test_pem_and_bearer_redacted(self):
        assert REDACTED in redact_secrets(FAKE_PEM)
        assert "MIIEvQ" not in redact_secrets(FAKE_PEM)
        bearer = "Authorization: Bearer abcdefgh12345678XYZ"
        out = redact_secrets(bearer)
        assert "abcdefgh12345678XYZ" not in out
        assert "Bearer" in out

    def test_nested_structures(self):
        payload = {"runs": [{"extracts": [{"text": f"leaked {FAKE_OPENAI} here"}]}]}
        out = redact_secrets(payload)
        assert FAKE_OPENAI not in out["runs"][0]["extracts"][0]["text"]

    def test_assert_no_secrets_passes_on_redacted(self):
        raw = {"api_key": "real-value", "note": f"saw {FAKE_GITHUB} today"}
        assert_no_secrets(redact_secrets(raw))  # must not raise

    def test_assert_no_secrets_raises_on_leak(self):
        with pytest.raises(SecretLeakError):
            assert_no_secrets({"api_key": "real-value"})
        with pytest.raises(SecretLeakError):
            assert_no_secrets(f"token {FAKE_ANTHROPIC}")
        with pytest.raises(SecretLeakError):
            assert_no_secrets("OPENAI_API_KEY=" + FAKE_OPENAI)

    def test_placeholders_are_not_leaks(self):
        assert_no_secrets({"api_key": "[REDACTED]"})
        assert_no_secrets({"token": ""})
        assert_no_secrets({"secret": None})


# ---------------------------------------------------------------------------
# routes: budget/recall + workspace isolation
# ---------------------------------------------------------------------------

class TestContextRoutes:
    def test_budget_and_recall_roundtrip(self, client):
        headers, ws_id = _register(client)
        original = "exact original finding " * 100
        r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/context/budget",
                        headers=headers,
                        json={"items": [
                            {"content": "User instruction: cover ETFs",
                             "category": "TASK_REQUIRED"},
                            {"content": original, "category": "RESEARCH_SOURCE"},
                        ], "max_tokens": 40})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["metrics"]["filtered_tokens"] > 0
        assert body["references"]
        ref_id = body["references"][0]["ref_id"]
        g = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/context/recall/{ref_id}",
                        headers=headers)
        assert g.status_code == 200
        assert g.json()["content"] == original

    def test_recall_unknown_is_404(self, client):
        headers, ws_id = _register(client)
        r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/context/recall/does-not-exist",
                        headers=headers)
        assert r.status_code == 404

    def test_context_workspace_isolation(self, client):
        headers, ws_id = _register(client)
        original = "isolated secret finding " * 100
        r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/context/budget",
                        headers=headers,
                        json={"items": [{"content": original, "category": "HISTORICAL"}],
                              "max_tokens": 5})
        ref_id = r.json()["references"][0]["ref_id"]
        headers2, ws2 = _register(client)
        x = client.post(f"/api/v1/workspaces/{ws2}/intelligence/context/recall/{ref_id}",
                        headers=headers2)
        assert x.status_code == 404  # per-workspace store: foreign refs invisible
        # cross-workspace budget access itself reads as 404
        y = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/context/budget",
                        headers=headers2, json={"items": [], "max_tokens": 10})
        assert y.status_code == 404
        # owner can still recall exactly
        g = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/context/recall/{ref_id}",
                        headers=headers)
        assert g.json()["content"] == original
