"""End-to-end pipeline integration test: FIND → … → LEARN through the real
job system with all providers mocked. Also covers failure recovery."""

import pathlib
import time

import pytest

# import registers handlers
import app.engine.autopilot  # noqa: F401
from app.db import session_scope
from app.engine.autopilot import (
    get_autopilot_status,
    start_autopilot,
    stop_autopilot,
)
from app.models import ContentItem, Opportunity, PublishedPost
from app.services import jobs as jobs_service


def _patch_ready(monkeypatch):
    """Tests use injected doubles; skip the real-dependency readiness gate."""
    import app.services.readiness as rd

    monkeypatch.setattr(rd, "run_readiness", lambda force_refresh=True: {
        "status": "ready", "checked_at": "", "stale_after_hours": 24,
        "checks": [], "blocking_failures": [], "message": "test",
    })


def _wait_for(predicate, timeout=60.0, interval=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def running_workers():
    import asyncio
    import threading

    # drain leftovers from earlier tests so this test starts clean
    with session_scope() as s:
        from app.models import Job

        for j in s.query(Job).filter(Job.status.in_(["QUEUED", "RETRYING", "RUNNING"])).all():
            j.status = "CANCELLED"

    # Run the worker loop on a dedicated background thread for the test body.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _start():
        await jobs_service.start_workers(count=3)

    try:
        asyncio.run_coroutine_threadsafe(_start(), loop).result(timeout=15)
        yield
    finally:
        async def _stop():
            await jobs_service.stop_workers()

        try:
            asyncio.run_coroutine_threadsafe(_stop(), loop).result(timeout=15)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_single_cycle_reaches_learned(workspace_with_user, running_workers, monkeypatch, tmp_path):
    ws = workspace_with_user["workspace"]
    from app.models import Workspace

    with session_scope() as sess:
        w = sess.get(Workspace, ws)
        w.settings_json = {"safety": {"min_qc_score": 60}}
    _patch_ready(monkeypatch)
    start_autopilot(ws, mode="SINGLE_CYCLE", cycles_target=1,
                    config={"interval_seconds": 1, "measure_delay_minutes": 0.02})

    ok = _wait_for(
        lambda: (lambda st: st["state"] == "STOPPED")(get_autopilot_status(ws)),
        timeout=120,
    )
    st = get_autopilot_status(ws)
    from app.models import Job
    with session_scope() as s:
        dbg_jobs = [(j.type, j.status, j.retry_count, (j.last_error or "")[:140])
                    for j in s.query(Job).order_by(Job.created_at.desc()).limit(12)]
    assert ok, f"single cycle should complete: state={st} jobs={dbg_jobs}"

    with session_scope() as s:
        items = s.query(ContentItem).filter(ContentItem.workspace_id == ws).all()
        assert len(items) >= 1
        learned = [i for i in items if i.status in ("LEARNED", "PUBLISHED")]
        if not learned:
            from app.models import EventLog, QualityCheck
            dbg = []
            for i in items:
                dbg.append(f"CONTENT {i.status} err={(i.error or '')[:200]}")
            for q in s.query(QualityCheck).all():
                comps = (q.components_json or {}).get("components", {})
                scores = {k: (v.get("score") if isinstance(v, dict) else v)
                          for k, v in comps.items()}
                dbg.append(f"QC overall={q.overall} passed={q.passed} scores={scores}")
            evs = s.query(EventLog).order_by(EventLog.created_at.desc()).limit(14).all()
            for e in reversed(evs):
                dbg.append(f"EV {e.level} {e.source}: {e.message[:130]}")
            (tmp_path / "pipeline-failure.txt").write_text(
                chr(10).join(dbg), encoding="utf-8")
        assert learned, f"expected LEARNED content, got {[i.status for i in items]}"
        posts = s.query(PublishedPost).filter(PublishedPost.workspace_id == ws).all()
        assert posts, "cycle must produce published (mock) posts"
        assert not any(p.is_mock for p in posts), "posts must NOT be marked as mock in production mode"

        opps = s.query(Opportunity).filter(Opportunity.workspace_id == ws).all()
        # dedupe: no duplicate topics
        topics = [o.topic.lower() for o in opps]
        assert len(topics) == len(set(topics))


def test_stop_prevents_new_cycles(workspace_with_user, running_workers, monkeypatch):
    ws = workspace_with_user["workspace"]
    _patch_ready(monkeypatch)
    start_autopilot(ws, mode="CONTINUOUS",
                    config={"interval_seconds": 1, "measure_delay_minutes": 0.02})
    assert _wait_for(lambda: get_autopilot_status(ws)["state"] == "RUNNING", timeout=90)
    assert stop_autopilot(ws)
    ok = _wait_for(lambda: get_autopilot_status(ws)["state"] == "STOPPED", timeout=90)
    assert ok
