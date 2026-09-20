"""E7 scale tests: Redis dispatch (faked), GPU gating, Postgres claim shape."""
from __future__ import annotations

import pytest


class FakeRedis:
    def __init__(self, fail: bool = False):
        self.items: list[str] = []
        self.fail = fail

    def _boom(self):
        raise ConnectionError("redis down")

    def lpush(self, _key, value):
        if self.fail:
            self._boom()
        self.items.insert(0, value if isinstance(value, str) else value.decode())
        return len(self.items)

    def brpop(self, _key, timeout=1):
        if self.fail:
            self._boom()
        if not self.items:
            return None
        return (_key, self.items.pop().encode())

    def ping(self):
        if self.fail:
            self._boom()
        return True


@pytest.fixture(autouse=True)
def _isolate_jobs():
    """DB-touching tests share the session test DB — purge our rows around each."""
    from app.db import session_scope
    from app.models import Job

    def purge():
        with session_scope() as s:
            for row in s.query(Job).filter(Job.workspace_id == "ws-x").all():
                s.delete(row)

    purge()
    yield
    purge()


@pytest.fixture()
def redis_env(monkeypatch):
    from app.core import config as config_mod
    from app.services import queue_redis as qr

    fake = FakeRedis()
    monkeypatch.setattr(config_mod.settings, "job_queue", "redis")
    monkeypatch.setattr(config_mod.settings, "redis_url", "redis://fake:6379/0")
    monkeypatch.setattr(qr, "_get_client", lambda: fake)
    qr._unavailable_until = 0.0
    return fake


def test_redis_push_pop_roundtrip(redis_env):
    from app.services import queue_redis as qr

    assert qr.configured() is True
    assert qr.push("job-1") is True
    assert qr.pop(1) == "job-1"
    assert qr.pop(1) is None
    assert qr.ping() is True


def test_redis_failure_degrades_to_local(monkeypatch):
    from app.services import jobs as jobs_mod
    from app.services import queue_redis as qr

    monkeypatch.setattr(qr, "_get_client", lambda: FakeRedis(fail=True))
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "job_queue", "redis")
    qr._unavailable_until = 0.0
    assert qr.push("x") is False
    assert qr.pop(1) is None
    assert qr.ping() is False
    # enqueue itself never breaks
    jid = jobs_mod.enqueue("test.noop", {}, workspace_id="ws-x")
    assert jid


def test_enqueue_pushes_to_redis(redis_env):
    from app.services import jobs as jobs_mod

    jid = jobs_mod.enqueue("test.noop", {}, workspace_id="ws-x")
    assert redis_env.items == [jid]


async def test_claim_by_id_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from app.services import jobs as jobs_mod

    jid = jobs_mod.enqueue("test.noop", {"a": 1}, workspace_id="ws-x")
    ctx = await jobs_mod._claim_by_id(jid)
    assert ctx is not None and ctx.payload == {"a": 1}
    assert await jobs_mod._claim_by_id(jid) is None
    assert await jobs_mod._claim_by_id("missing") is None


async def test_gpu_jobs_deferred_without_worker(tmp_path, monkeypatch):
    from app.core import config as config_mod
    from app.db import session_scope
    from app.models import Job
    from app.services import jobs as jobs_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "gpu_worker", False)
    jid = jobs_mod.enqueue("cycle.build", {"requires_gpu": True}, workspace_id="ws-x")
    assert await jobs_mod._claim_next() is None
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.status == "QUEUED"
    monkeypatch.setattr(config_mod.settings, "gpu_worker", True)
    try:
        # simulate the 60s deferral elapsing
        from app.models.base import utcnow as _utcnow

        with session_scope() as s:
            s.get(Job, jid).next_run_at = _utcnow()
        ctx = await jobs_mod._claim_next()
        assert ctx is not None and ctx.job_id == jid
    finally:
        monkeypatch.setattr(config_mod.settings, "gpu_worker", False)


async def test_plain_jobs_unaffected_by_gpu_gate(tmp_path, monkeypatch):
    from app.core import config as config_mod
    from app.services import jobs as jobs_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "gpu_worker", False)
    jid = jobs_mod.enqueue("cycle.find", {}, workspace_id="ws-x")
    ctx = await jobs_mod._claim_next()
    assert ctx is not None and ctx.job_id == jid


def test_for_update_only_on_postgres():
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql, sqlite

    from app.models import Job
    from app.services.jobs import _for_update

    class _Bind:
        def __init__(self, name):
            self.dialect = type("D", (), {"name": name})()

    class _Session:
        def __init__(self, name):
            self._bind = _Bind(name)

        def get_bind(self):
            return self._bind

    pg = str(_for_update(select(Job.id), _Session("postgresql")).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    lite = str(_for_update(select(Job.id), _Session("sqlite")).compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in pg and "SKIP LOCKED" in pg
    assert "FOR UPDATE" not in lite


def test_engine_gpu_detection(monkeypatch):
    from app.core import config as config_mod
    from app.engine import autopilot as autopilot_mod
    from app.providers.video_engine.base import VideoEngineRequestInvalid

    # NOTE: _engine_needs_gpu imports the factory lazily, so patch it there
    # (this also dodges the factory's process-global engine cache).
    import app.providers.video_engine.factory as factory_mod

    def boom(*a, **k):
        raise VideoEngineRequestInvalid("no engine")

    monkeypatch.setattr(factory_mod, "get_video_engine", boom)
    monkeypatch.setattr(config_mod.settings, "video_engine", "wan")
    assert autopilot_mod._engine_needs_gpu() is True
    monkeypatch.setattr(config_mod.settings, "video_engine", "ffmpeg_avatar")
    assert autopilot_mod._engine_needs_gpu() is False


def test_health_reports_queue():
    from fastapi.testclient import TestClient

    from app.main import create_app

    r = TestClient(create_app(), raise_server_exceptions=False).get("/api/v1/system/health")
    assert r.status_code == 200, r.text
    queue = r.json()["queue"]
    assert set(queue) >= {"backend", "gpu_worker", "gpu_cuda", "redis"}
