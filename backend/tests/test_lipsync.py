"""Lip-sync layer: provider contract, fail-closed fallback, worker lifecycle,
workspace isolation, cost + progress telemetry.

`FakeAdapter` is a test double only — the product ships no mock provider.
Heavy inference is never exercised here: the queue drives the fake adapter
directly, exactly as it would drive MuseTalk/External in production.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from app.engine.lipsync import rows as job_rows
from app.engine.lipsync import service as lipsync_service
from app.engine.lipsync.base import (
    HEALTH_AVAILABLE,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_SUCCEEDED,
    JOB_TIMEOUT,
    Health,
    LipSyncProvider,
    LipSyncTransient,
    LipSyncUnavailable,
    default_concurrency,
)

# ---------------------------------------------------------------------------
# Fake adapter (tests only)
# ---------------------------------------------------------------------------


class FakeAdapter(LipSyncProvider):
    """In-memory adapter with controllable latency and submit failures."""

    name = "fake"

    def __init__(
        self,
        *,
        run_seconds: float = 0.0,
        submit_failures: int = 0,
        asset_ref: str = "asset://fake/out.mp4",
        gpu_seconds: float = 0.25,
    ):
        self.run_seconds = run_seconds
        self.submit_failures = submit_failures
        self.asset_ref = asset_ref
        self.gpu_seconds = gpu_seconds
        self.submit_calls = 0
        self.result_calls = 0
        self.cancelled_ids: list[str] = []
        self.submit_times: list[float] = []
        self.finish_times: dict[str, float] = {}
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def health(self) -> Health:
        return Health(
            provider=self.name,
            status=HEALTH_AVAILABLE,
            detail="fake adapter ready",
        )

    def submit(self, video_ref, audio_ref, workspace_id, opts=None) -> str:
        with self._lock:
            self.submit_calls += 1
            if self.submit_calls <= self.submit_failures:
                raise LipSyncTransient("fake transient submit failure")
            job_id = f"fake-{self.submit_calls}"
            self._jobs[job_id] = {"started": time.monotonic(), "cancelled": False}
            self.submit_times.append(time.monotonic())
        return job_id

    def status(self, job_id: str) -> dict:
        job = self._jobs[job_id]
        if job["cancelled"]:
            return {"status": JOB_CANCELLED, "progress": 1.0, "error": "cancelled"}
        elapsed = time.monotonic() - job["started"]
        if elapsed >= self.run_seconds:
            self.finish_times.setdefault(job_id, time.monotonic())
            return {"status": JOB_SUCCEEDED, "progress": 1.0, "error": ""}
        progress = elapsed / max(self.run_seconds, 1e-6)
        return {"status": "RUNNING", "progress": round(min(0.95, progress), 3), "error": ""}

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job["cancelled"] = True
        self.cancelled_ids.append(job_id)
        return True

    def result(self, job_id: str) -> dict:
        self.result_calls += 1
        return {
            "asset_ref": self.asset_ref,
            "gpu_seconds": self.gpu_seconds,
            "cost_usd": 0.01,
            "provider": self.name,
        }


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _make_workspace(db_session, tag: str) -> str:
    from app.models import Workspace

    ws = Workspace(
        name=f"LS {tag}",
        slug=f"ls-{tag}-{os.urandom(3).hex()}",
        niche="test",
    )
    db_session.add(ws)
    db_session.commit()
    return ws.id


def _reload(db_session, job_id: str):
    from app.models.lipsync import LipSyncJob

    db_session.expire_all()
    return db_session.get(LipSyncJob, job_id)


@pytest.fixture()
def lipsync_env(monkeypatch):
    """Deterministic provider env + a fresh global queue, reset afterwards."""
    for var in (
        "LIPSYNC_PROVIDER",
        "LIPSYNC_EXTERNAL_BASE_URL",
        "LIPSYNC_EXTERNAL_API_KEY",
        "LIPSYNC_MUSE_COMMAND",
        "LIPSYNC_MUSE_MODEL_DIR",
        "LIPSYNC_GPU",
        "LIPSYNC_MAX_CONCURRENCY",
        "LIPSYNC_MAX_RETRIES",
    ):
        monkeypatch.delenv(var, raising=False)
    lipsync_service.reset_queue()
    yield lipsync_service
    lipsync_service.reset_queue(timeout=2.0)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, tag: str) -> dict:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": f"{tag}{os.urandom(4).hex()}@test.local",
              "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "ws": data["workspace"]["id"],
    }


# ---------------------------------------------------------------------------
# Fail-closed behavior (no GPU / no MuseTalk / nothing configured)
# ---------------------------------------------------------------------------


def test_unavailable_adapter_reports_and_fails_closed():
    from app.engine.lipsync.unavailable import UnavailableAdapter

    adapter = UnavailableAdapter()
    health = adapter.health()
    assert health.available is False
    assert health.status == "unavailable"
    assert health.remediation  # always actionable
    with pytest.raises(LipSyncUnavailable) as exc:
        adapter.submit("v.mp4", "a.mp4", "ws-1")
    assert exc.value.remediation
    assert adapter.cancel("any") is False  # nothing was ever running
    with pytest.raises(LipSyncUnavailable):
        adapter.result("any")


def test_factory_defaults_to_fail_closed_without_any_backend(lipsync_env):
    from app.engine.lipsync.factory import build_provider
    from app.engine.lipsync.unavailable import UnavailableAdapter

    provider = build_provider()  # auto, nothing configured
    assert isinstance(provider, UnavailableAdapter)
    health = provider.health()
    assert health.available is False and health.remediation
    # unknown provider names never raise and never pretend to be ready
    fallback = build_provider("does-not-exist")
    assert isinstance(fallback, UnavailableAdapter)
    with pytest.raises(LipSyncUnavailable):
        fallback.submit("v.mp4", "a.mp4", "ws-1")


def test_musetalk_health_unavailable_without_install(lipsync_env):
    from app.engine.lipsync.musetalk import MuseTalkAdapter

    adapter = MuseTalkAdapter(command="", model_dir="")
    health = adapter.health()
    assert health.available is False
    assert "LIPSYNC_MUSE_COMMAND" in health.remediation
    with pytest.raises(LipSyncUnavailable) as exc:
        adapter.submit("v.mp4", "a.mp4", "ws-1")
    assert "LIPSYNC_MUSE_COMMAND" in exc.value.remediation


def test_musetalk_unavailable_without_gpu(lipsync_env, monkeypatch):
    from app.engine.lipsync import musetalk as muse_mod

    monkeypatch.setattr(muse_mod, "gpu_present", lambda: False)
    adapter = muse_mod.MuseTalkAdapter(
        command="python /opt/musetalk/inference.py {video} {audio} {out}",
        model_dir=os.getcwd(),  # exists -> the GPU check is the failing one
    )
    health = adapter.health()
    assert health.available is False
    assert "GPU" in health.remediation
    with pytest.raises(LipSyncUnavailable):
        adapter.submit("v.mp4", "a.mp4", "ws-1")


def test_app_starts_without_musetalk_or_gpu():
    """MuseTalk must never be mandatory for YMONEY startup."""
    import app  # noqa: F401
    from app.main import create_app

    application = create_app()  # no GPU, no weights, no MuseTalk checkout
    assert application is not None


def test_health_endpoint_never_requires_gpu(lipsync_env, client):
    ctx = _register(client, "lship")
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/lipsync/health",
                   headers=ctx["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False  # nothing configured on this machine
    assert body["remediation"]
    assert "queue" in body


def test_submit_endpoint_fails_closed_with_remediation(lipsync_env, client):
    ctx = _register(client, "lsub")
    r = client.post(
        f"/api/v1/workspaces/{ctx['ws']}/lipsync/jobs",
        headers=ctx["headers"],
        json={"video_ref": "asset://v.mp4", "audio_ref": "asset://a.mp4"},
    )
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["remediation"]
    # fail closed == no job row was created
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/lipsync/jobs",
                   headers=ctx["headers"])
    assert r.status_code == 200 and r.json()["total"] == 0


def test_default_concurrency_is_one_without_gpu(monkeypatch):
    monkeypatch.setenv("LIPSYNC_GPU", "0")
    monkeypatch.delenv("LIPSYNC_MAX_CONCURRENCY", raising=False)
    assert default_concurrency() == 1
    monkeypatch.setenv("LIPSYNC_MAX_CONCURRENCY", "3")
    assert default_concurrency() == 3


# ---------------------------------------------------------------------------
# Adapter contract (FakeAdapter through the real queue)
# ---------------------------------------------------------------------------


def test_adapter_contract_and_success_records_cost(db_session, lipsync_env):
    from app.engine.lipsync.worker import LocalWorkerQueue

    ws_id = _make_workspace(db_session, "contract")
    fake = FakeAdapter(run_seconds=0.05, gpu_seconds=1.5)
    queue = LocalWorkerQueue(
        provider=fake, timeout_seconds=10, poll_interval=0.02, backoff_seconds=0.01
    )

    # contract surface: health/submit/status/cancel/result shapes
    assert fake.health().available is True
    row = job_rows.create_job_row(
        db_session, workspace_id=ws_id, provider=fake.name,
        video_ref="asset://v.mp4", audio_ref="asset://a.mp4",
    )
    db_session.commit()
    queue.submit(row.id, "asset://v.mp4", "asset://a.mp4", ws_id, {})
    assert queue.wait(row.id, timeout=10) is True

    fresh = _reload(db_session, row.id)
    assert fresh.status == JOB_SUCCEEDED
    assert fresh.progress == 1.0
    assert fresh.result_asset_ref == "asset://fake/out.mp4"
    # cost tracking lands on the job row
    assert fresh.cost_json["gpu_seconds"] == 1.5
    assert fresh.cost_json["cost_usd"] == 0.01
    assert fresh.completed_at is not None


def test_success_without_asset_ref_fails_closed(db_session, lipsync_env):
    """A "succeeded" job with no verifiable asset must never ship."""
    from app.engine.lipsync.worker import LocalWorkerQueue

    ws_id = _make_workspace(db_session, "noasset")
    fake = FakeAdapter(asset_ref="")
    queue = LocalWorkerQueue(
        provider=fake, timeout_seconds=5, poll_interval=0.02, backoff_seconds=0.01
    )
    row = job_rows.create_job_row(
        db_session, workspace_id=ws_id, provider=fake.name,
        video_ref="asset://v.mp4", audio_ref="asset://a.mp4",
    )
    db_session.commit()
    queue.submit(row.id, "asset://v.mp4", "asset://a.mp4", ws_id, {})
    assert queue.wait(row.id, timeout=10) is True

    fresh = _reload(db_session, row.id)
    assert fresh.status == JOB_FAILED
    assert fresh.result_asset_ref == ""
    assert "asset" in fresh.error


def test_transient_submit_retries_are_bounded(db_session, lipsync_env):
    from app.engine.lipsync.worker import LocalWorkerQueue

    ws_id = _make_workspace(db_session, "retry")

    # always-transient: exactly max_retries + 1 attempts, then FAILED
    failing = FakeAdapter(submit_failures=99)
    queue = LocalWorkerQueue(
        provider=failing, max_retries=2, backoff_seconds=0.01,
        timeout_seconds=5, poll_interval=0.02,
    )
    row = job_rows.create_job_row(
        db_session, workspace_id=ws_id, provider=failing.name,
        video_ref="asset://v.mp4", audio_ref="asset://a.mp4",
    )
    db_session.commit()
    queue.submit(row.id, "asset://v.mp4", "asset://a.mp4", ws_id, {})
    assert queue.wait(row.id, timeout=10) is True
    assert failing.submit_calls == 3  # bounded, not endless
    assert queue.attempts(row.id) == 3
    fresh = _reload(db_session, row.id)
    assert fresh.status == JOB_FAILED
    assert "after 3 attempt" in fresh.error

    # one transient failure then success: retries recover the job
    flaky = FakeAdapter(submit_failures=1, run_seconds=0.01)
    queue2 = LocalWorkerQueue(
        provider=flaky, max_retries=2, backoff_seconds=0.01,
        timeout_seconds=5, poll_interval=0.02,
    )
    row2 = job_rows.create_job_row(
        db_session, workspace_id=ws_id, provider=flaky.name,
        video_ref="asset://v.mp4", audio_ref="asset://a.mp4",
    )
    db_session.commit()
    queue2.submit(row2.id, "asset://v.mp4", "asset://a.mp4", ws_id, {})
    assert queue2.wait(row2.id, timeout=10) is True
    assert queue2.attempts(row2.id) == 2
    assert _reload(db_session, row2.id).status == JOB_SUCCEEDED


def test_concurrency_semaphore_serializes_jobs(db_session, lipsync_env):
    """VRAM gate: with concurrency=1 the second job cannot start early."""
    from app.engine.lipsync.worker import LocalWorkerQueue

    ws_id = _make_workspace(db_session, "sem")
    fake = FakeAdapter(run_seconds=0.25)
    queue = LocalWorkerQueue(
        provider=fake, concurrency=1, timeout_seconds=20,
        poll_interval=0.02, backoff_seconds=0.01,
    )
    ids = []
    for _ in range(2):
        row = job_rows.create_job_row(
            db_session, workspace_id=ws_id, provider=fake.name,
            video_ref="asset://v.mp4", audio_ref="asset://a.mp4",
        )
        db_session.commit()
        ids.append(row.id)
        queue.submit(row.id, "asset://v.mp4", "asset://a.mp4", ws_id, {})
    assert queue.wait(ids[0], timeout=20) and queue.wait(ids[1], timeout=20)
    assert all(_reload(db_session, i).status == JOB_SUCCEEDED for i in ids)
    # job 2 reached the adapter only after job 1 had finished there
    assert len(fake.submit_times) == 2
    first_done = fake.finish_times["fake-1"]
    assert fake.submit_times[1] >= first_done


# ---------------------------------------------------------------------------
# Cancellation / timeout / progress
# ---------------------------------------------------------------------------


def test_cancel_mid_run_marks_cancelled_without_result(db_session, lipsync_env):
    ws_id = _make_workspace(db_session, "cancel")
    fake = FakeAdapter(run_seconds=60.0)  # hangs until cancelled
    lipsync_service.set_provider(
        fake, timeout_seconds=60, poll_interval=0.02, backoff_seconds=0.01
    )
    dto = lipsync_service.submit_job(
        db_session, ws_id, video_ref="asset://v.mp4", audio_ref="asset://a.mp4"
    )
    job_id = dto["id"]
    assert dto["status"] == "QUEUED"
    assert _wait_until(lambda: job_rows.row_status(job_id) == "RUNNING") is True

    cancelled = lipsync_service.cancel_job(db_session, ws_id, job_id)
    assert cancelled["status"] == JOB_CANCELLED
    assert lipsync_service.get_queue().wait(job_id, timeout=8) is True

    fresh = _reload(db_session, job_id)
    assert fresh.status == JOB_CANCELLED
    assert fresh.result_asset_ref == ""  # no result ever
    assert fake.cancelled_ids, "adapter job must be cancelled too"
    assert fake.result_calls == 0  # and never asked for a result
    # cancelling again is an illegal state, not a silent no-op
    with pytest.raises(lipsync_service.JobStateError):
        lipsync_service.cancel_job(db_session, ws_id, job_id)


def test_hard_timeout_marks_timeout_and_records_cost(db_session, lipsync_env):
    ws_id = _make_workspace(db_session, "timeout")
    fake = FakeAdapter(run_seconds=60.0)
    lipsync_service.set_provider(
        fake, timeout_seconds=0.8, poll_interval=0.05, backoff_seconds=0.01,
        gpu_usd_per_hour=3.6,
    )
    dto = lipsync_service.submit_job(
        db_session, ws_id, video_ref="asset://v.mp4", audio_ref="asset://a.mp4"
    )
    job_id = dto["id"]
    assert lipsync_service.get_queue().wait(job_id, timeout=20) is True

    fresh = _reload(db_session, job_id)
    assert fresh.status == JOB_TIMEOUT
    assert "timeout" in fresh.error.lower()
    assert fresh.result_asset_ref == ""
    assert fresh.cost_json["gpu_seconds"] > 0  # GPU time accounted
    assert fresh.cost_json["cost_usd"] >= 0
    assert fake.cancelled_ids, "timed-out adapter job must be cancelled"


def test_progress_events_are_emitted(db_session, lipsync_env, monkeypatch):
    from app.engine.lipsync import rows as rows_mod

    ws_id = _make_workspace(db_session, "events")
    events: list[tuple[str, str]] = []
    original = rows_mod.emit

    def spy(workspace_id, kind, message, level="info", data=None):
        events.append((kind, level))
        return original(workspace_id, kind, message, level=level, data=data)

    monkeypatch.setattr(rows_mod, "emit", spy)
    fake = FakeAdapter(run_seconds=0.4)
    lipsync_service.set_provider(
        fake, timeout_seconds=10, poll_interval=0.05, backoff_seconds=0.01
    )
    dto = lipsync_service.submit_job(
        db_session, ws_id, video_ref="asset://v.mp4", audio_ref="asset://a.mp4"
    )
    assert lipsync_service.get_queue().wait(dto["id"], timeout=10) is True
    kinds = [k for k, _ in events]
    assert "lipsync.started" in kinds
    assert "lipsync.progress" in kinds
    assert "lipsync.succeeded" in kinds
    assert _reload(db_session, dto["id"]).status == JOB_SUCCEEDED


# ---------------------------------------------------------------------------
# Workspace isolation (service + API)
# ---------------------------------------------------------------------------


def test_workspace_lipsync_isolation(db_session, lipsync_env):
    ws_a = _make_workspace(db_session, "iso-a")
    ws_b = _make_workspace(db_session, "iso-b")
    fake = FakeAdapter(run_seconds=60.0)
    lipsync_service.set_provider(
        fake, timeout_seconds=60, poll_interval=0.02, backoff_seconds=0.01
    )
    dto = lipsync_service.submit_job(
        db_session, ws_a, video_ref="asset://v.mp4", audio_ref="asset://a.mp4"
    )
    job_id = dto["id"]

    # owner workspace sees it
    assert lipsync_service.get_job(db_session, ws_a, job_id).id == job_id
    assert [j["id"] for j in lipsync_service.list_jobs(db_session, ws_a)] == [job_id]

    # foreign workspace sees nothing: 404 semantics, no leaks
    with pytest.raises(lipsync_service.JobNotFound):
        lipsync_service.get_job(db_session, ws_b, job_id)
    with pytest.raises(lipsync_service.JobNotFound):
        lipsync_service.cancel_job(db_session, ws_b, job_id)
    assert lipsync_service.list_jobs(db_session, ws_b) == []

    # cleanup: stop the hanging worker
    lipsync_service.get_queue().cancel(job_id)
    lipsync_service.get_queue().wait(job_id, timeout=8)


def test_api_cross_workspace_job_access_is_404(lipsync_env, client):
    owner = _register(client, "own")
    other = _register(client, "oth")
    fake = FakeAdapter(run_seconds=60.0)
    lipsync_service.set_provider(
        fake, timeout_seconds=60, poll_interval=0.02, backoff_seconds=0.01
    )
    r = client.post(
        f"/api/v1/workspaces/{owner['ws']}/lipsync/jobs",
        headers=owner["headers"],
        json={"video_ref": "asset://v.mp4", "audio_ref": "asset://a.mp4"},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # another user's workspace path for this job: job is scoped to owner's ws
    r = client.get(
        f"/api/v1/workspaces/{other['ws']}/lipsync/jobs/{job_id}",
        headers=other["headers"],
    )
    assert r.status_code == 404, r.text
    # and they cannot even enter the owner's workspace (403)
    r = client.get(
        f"/api/v1/workspaces/{owner['ws']}/lipsync/jobs/{job_id}",
        headers=other["headers"],
    )
    assert r.status_code == 403, r.text
    # the foreign workspace's job list stays empty
    r = client.get(f"/api/v1/workspaces/{other['ws']}/lipsync/jobs",
                   headers=other["headers"])
    assert r.status_code == 200 and r.json()["total"] == 0

    lipsync_service.get_queue().cancel(job_id)
    lipsync_service.get_queue().wait(job_id, timeout=8)
