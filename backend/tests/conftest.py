"""Test bootstrap: isolated database per test session."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Isolated temp DB BEFORE any app import touches the engine.
_TMPDIR = tempfile.mkdtemp(prefix="ymoney-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(_TMPDIR) / 'test.db').as_posix()}"
os.environ["YMONEY_SECRET_KEY"] = "test-secret-key-not-for-production-123"
os.environ["VIDEO_ENGINE"] = "mock"
os.environ["MOCK_LLM"] = "true"
os.environ["MOCK_TRENDS"] = "true"
os.environ["MOCK_PUBLISHING"] = "true"
os.environ["MOCK_ANALYTICS"] = "true"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tests import fakes as _fakes


@pytest.fixture()
def db_session():
    from app.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture()
def workspace_with_user(db_session):
    from app.models import User, Workspace, WorkspaceMember

    user = User(email=f"t{os.urandom(4).hex()}@test.local", password_hash="x")
    ws = Workspace(name="Test WS", slug=f"ws-{os.urandom(4).hex()}", niche="AI money")
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceMember.ROLE_OWNER)
    )
    db_session.commit()
    return {"user": user.id, "workspace": ws.id}


@pytest.fixture(scope="session", autouse=True)
def _migrate_once():
    import app.models  # noqa: F401 - ensure all ORM models are on Base.metadata
    from app.db import session_scope
    from app.migrations.runner import run_migrations

    with session_scope() as s:
        run_migrations(s)


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    """Deterministic in-test LLM. The PRODUCT has no mock paths; this is a
    test double injected at the HTTP boundary."""
    from app.providers import llm as llm_mod

    transport = _fakes.FakeLLMTransport()
    monkeypatch.setattr(llm_mod, "_effective", lambda: {
        "api_key": "test-key", "base_url": "http://llm.test/v1",
        "model": "test-model", "configured": True, "mock": False,
        "sources": {}, "tiers": {},
    })
    monkeypatch.setattr(llm_mod.httpx, "post", lambda url, **kw: transport.post(url, **kw))
    return transport


@pytest.fixture(autouse=True)
def fake_trends(monkeypatch):
    from app.engine.agents.discovery import TrendHunterAgent

    data = [
        {"topic": f"deterministic trend {i}", "source": "google_trends",
         "external_ref": f"t{i}", "raw": {"news": [{}]},
         "velocity_hint": 0.5 + i * 0.05, "volume_hint": 0.6}
        for i in range(6)
    ]
    monkeypatch.setattr(TrendHunterAgent, "fetch_candidates", lambda self, ws: data)


@pytest.fixture(autouse=True)
def fake_video_engine(monkeypatch):
    """Pipeline tests render through an instant engine double."""
    from app.providers.video_engine import factory

    eng = _fakes.FakeEngine()

    class _Cfg(dict):
        pass

    monkeypatch.setattr(factory, "_effective_engine_config",
                        lambda: {"base_url": "http://engine.test", "timeout": 300, "sources": {}})
    monkeypatch.setattr(factory, "get_video_engine", lambda: eng)
    return eng


@pytest.fixture(autouse=True)
def fake_publishing(monkeypatch):
    """Publishing succeeds through an in-test recorder; no network, no mocks shipped."""
    import app.providers.publishers.factory as pfactory
    from app.core.config import settings as cfg

    pub = _fakes.FakePublisher()
    monkeypatch.setattr(cfg, "mock_publishing", False)
    monkeypatch.setitem(pfactory._registry, "youtube", pub)
    monkeypatch.setitem(pfactory._registry, "tiktok", pub)
    monkeypatch.setitem(pfactory._registry, "facebook", pub)
    monkeypatch.setattr(pfactory, "relay_ready", lambda: True)
    return pub


@pytest.fixture(autouse=True)
def fake_analytics(monkeypatch):
    import app.providers.analytics as analytics_mod

    monkeypatch.setattr(analytics_mod, "get_provider",
                        lambda platform: _fakes.FakeAnalyticsProvider())


@pytest.fixture(autouse=True)
def fake_publish(monkeypatch):
    """Pipeline tests: publishing succeeds through an in-test recorder."""
    from app.engine.agents.distribution import PublisherAgent

    published = []

    def _fake_publish(self, ctx, *, video_path, platforms, metadata_by_platform, workspace_id):
        results = []
        for p in platforms:
            results.append({
                "platform": p,
                "success": True,
                "remote_post_id": f"fake-{len(published)}",
                "remote_url": "",
                "error": "",
                "mock": False,
            })
            published.append(p)
        return results

    monkeypatch.setattr(PublisherAgent, "publish_to_platforms", _fake_publish)
    return published
