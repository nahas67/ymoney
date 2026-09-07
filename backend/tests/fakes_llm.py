"""Test doubles for production-only integrations.

These live ONLY in tests. The product has no mock paths; when an external
dependency is unconfigured, agents raise actionable errors.
"""

from __future__ import annotations

import pytest


class FakeLLMTransport:
    """Deterministic OpenAI-compatible responder keyed by prompt intent."""

    def __init__(self):
        self.calls: list[dict] = []

    def post(self, url, *, headers=None, json=None, timeout=None, **kw):  # noqa: ARG002
        self.calls.append(json or {})
        body = json or {}
        msgs = " ".join(m.get("content", "") for m in body.get("messages", []))

        if "researcher" in msgs:
            payload = {
                "summary": "Deterministic research summary.",
                "key_facts": ["fact one", "fact two"],
                "angles": ["angle a"],
                "visual_keywords": ["chart", "finance"],
                "cautions": [],
                "claims": [
                    {"claim": "claim one", "status": "LIKELY", "confidence": 0.7, "basis": "test"},
                    {"claim": "claim two", "status": "VERIFIED", "confidence": 0.8, "basis": "test"},
                ],
            }
        elif "strategist" in msgs:
            payload = {
                "angle": "deterministic angle",
                "target_audience": "testers",
                "format": "talking head",
                "duration_seconds": 30,
                "hook_type": "question",
                "tone": "neutral",
                "cta": "follow",
                "platforms": ["youtube"],
                "aspect_ratio": "9:16",
                "rationale": "test",
            }
        elif "metadata" in msgs or "platform-optimized" in msgs:
            payload = {
                "youtube": {"title": "Test Title", "description": "desc",
                            "hashtags": ["#test"], "keywords": ["test"]},
            }
        else:
            payload = {}

        text = json.dumps(payload) if payload else (
            "Deterministic script body with a hook? "
            "It explains something concrete and ends with follow for more."
        )
        return _HTTPResponse(200, {"choices": [{"message": {"content": text}}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 10}})


class _HTTPResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture()
def fake_llm(monkeypatch):
    """Force LLM 'configured' and intercept HTTP with deterministic responses."""
    from app.providers import llm as llm_mod
    from tests.fakes_llm import FakeLLMTransport  # noqa: F401

    transport = FakeLLMTransport()

    monkeypatch.setattr(llm_mod, "_effective", lambda: {
        "api_key": "test-key", "base_url": "http://llm.test/v1",
        "model": "test-model", "configured": True, "mock": False,
        "sources": {}, "tiers": {},
    })
    monkeypatch.setattr(llm_mod.httpx, "post", lambda url, **kw: transport.post(url, **kw))
    return transport


@pytest.fixture()
def fake_trends(monkeypatch):
    """Deterministic discovery without network access."""
    from app.engine.agents.discovery import TrendHunterAgent

    data = [
        {"topic": f"deterministic trend {i}", "source": "google_trends",
         "external_ref": f"t{i}", "raw": {"news": [{}]},
         "velocity_hint": 0.5 + i * 0.05, "volume_hint": 0.6}
        for i in range(6)
    ]
    monkeypatch.setattr(TrendHunterAgent, "fetch_candidates", lambda self, ws: data)
    return data
