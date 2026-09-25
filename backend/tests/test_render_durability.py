"""Render durability & idempotency.

Covers the four crash/retry windows:
1. happy path: submit -> poll -> complete -> stored
2. crash DURING polling: reattach to persisted engine_task_id (no resubmit)
3. crash BETWEEN submit & persisting task id: adopt the orphaned engine task
   via engine-side reconciliation (no resubmit)
4. completed render retried: fully idempotent no-op
"""

import time

import pytest

from app.db import session_scope
from app.engine.agents.production import VideoProducerAgent
from app.models import ContentItem, Opportunity, Video, VideoVariant
from app.services import jobs as jobs_service


class FakeEngine:
    """In-memory engine that records submissions and supports reconciliation."""

    engine_name = "fake"

    def __init__(self):
        self.submissions: list[tuple[str, str]] = []  # (task_id, request_hash)
        self.subjects: dict[str, str] = {}
        self.polls: dict[str, int] = {}
        self.fail_next_status = False

    def health(self):
        return True

    def submit(self, req):
        tid = f"task-{len(self.submissions) + 1}"
        self.submissions.append((tid, req.request_hash()))
        self.subjects[tid] = req.subject
        return type("H", (), {"engine_task_id": tid, "engine": self.engine_name})()

    def status(self, handle):
        if self.fail_next_status:
            self.fail_next_status = False
            raise RuntimeError("engine exploded")
        n = self.polls.get(handle.engine_task_id, 0) + 1
        self.polls[handle.engine_task_id] = n
        done = n >= 2
        return type("S", (), {
            "state": "complete" if done else "processing",
            "progress": 100 if done else 40,
            "videos": [f"{handle.engine_task_id}/final.mp4"] if done else [],
            "error": "", "failed_stage": "", "is_terminal": done,
        })()

    def get_video_url(self, handle):
        return f"{handle.engine_task_id}/final.mp4"

    def fetch_video_bytes(self, ref):
        return b"fake-video-bytes"

    def estimate_cost(self, req):
        return 0.01

    def get_capabilities(self):
        return set()

    def list_recent_tasks(self, limit=30):
        out = []
        for tid, _h in self.submissions:
            n = self.polls.get(tid, 0)
            out.append({
                "task_id": tid,
                "subject": self.subjects[tid],
                "state": "complete" if n >= 2 else "processing",
                "progress": 100 if n >= 2 else 40,
            })
        return out


@pytest.fixture()
def fake_engine(monkeypatch):
    eng = FakeEngine()

    from app.providers.video_engine import factory

    monkeypatch.setattr(factory, "get_video_engine", lambda: eng)

    from app.services import storage as storage_mod

    class TmpStorage(storage_mod.LocalStorage):
        def save_video(self, workspace_id, source_path=None, data=None, filename=None):
            d = storage_mod.STORAGE_ROOT.parent / "test_store"
            d.mkdir(parents=True, exist_ok=True)
            p = d / (filename or "v.mp4")
            p.write_bytes(data or b"")
            return storage_mod.StoredVideo(str(p), p.stat().st_size, 30.0, 1080, 1920)

    monkeypatch.setattr(storage_mod, "get_storage", lambda: TmpStorage())
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return eng


def _ctx(ws):
    return jobs_service.JobContext(
        job_id="j1", type="cycle.build", workspace_id=ws, cycle_id=None,
        payload={}, attempt=1, cancelled=lambda: False,
    )


def _make_content(workspace_with_user, script="script text here"):
    ws = workspace_with_user["workspace"]
    with session_scope() as s:
        opp = Opportunity(workspace_id=ws, topic="t", score=90)
        content = ContentItem(workspace_id=ws, topic="t", status="PRODUCTION")
        variant = VideoVariant(content_item_id=None, label="v1", script=script,
                               selected=True)
        s.add_all([opp, content])
        s.flush()
        variant.content_item_id = content.id
        s.add(variant)
        s.flush()
        return {"ws": ws, "content": content.id, "variant": variant.id}


def _render(ids, **kw):
    agent = VideoProducerAgent()
    return agent.render(_ctx(ids["ws"]), topic=kw.pop("topic", "t"),
                        script=kw.pop("script", "script text here"),
                        keywords=[], aspect_ratio="9:16",
                        variant_id=ids["variant"], **kw)


def _video_row(variant_id):
    with session_scope() as s:
        row = s.query(Video).filter(Video.variant_id == variant_id).order_by(
            Video.created_at.desc()).first()
        if row:
            s.expunge(row)
        return row


def test_first_render_completes_and_stores(workspace_with_user, fake_engine):
    ids = _make_content(workspace_with_user)
    r = _render(ids)
    assert len(fake_engine.submissions) == 1
    row = _video_row(ids["variant"])
    assert row.status == "READY"
    assert row.engine_task_id == fake_engine.submissions[0][0]
    assert row.duration_seconds == pytest.approx(30.0)
    assert row.resolution == "1080x1920"
    assert r["video_id"] == row.id


def test_crash_during_polling_reattaches(workspace_with_user, fake_engine):
    ids = _make_content(workspace_with_user)

    # attempt 1: die during polling (row + task id already persisted)
    real_status = fake_engine.status

    def die_on_first_poll(handle):
        fake_engine.polls[handle.engine_task_id] = (
            fake_engine.polls.get(handle.engine_task_id, 0) + 1
        )
        raise KeyboardInterrupt

    fake_engine.status = die_on_first_poll
    with pytest.raises(KeyboardInterrupt):
        _render(ids)

    row = _video_row(ids["variant"])
    assert row.status == "RENDERING"
    assert row.engine_task_id == "task-1"

    # attempt 2 (fresh process): normal completion via reattachment
    fake_engine.status = real_status
    r2 = _render(ids)
    assert len(fake_engine.submissions) == 1, "must NOT resubmit"
    assert r2["video_id"] == row.id
    assert _video_row(ids["variant"]).status == "READY"


def test_crash_between_submit_and_persist_adopts_orphan(workspace_with_user, fake_engine):
    ids = _make_content(workspace_with_user)

    # attempt 1: engine ACCEPTS the job, then YMONEY dies before persisting id
    real_submit = fake_engine.submit

    def accept_then_die(req):
        real_submit(req)
        raise KeyboardInterrupt

    fake_engine.submit = accept_then_die
    with pytest.raises(KeyboardInterrupt):
        _render(ids)

    # row exists but has NO engine_task_id (the crash window)
    row = _video_row(ids["variant"])
    assert row.status == "RENDERING"
    assert row.engine_task_id == ""

    # attempt 2: reconciliation finds the orphaned engine task by subject match
    fake_engine.submit = real_submit
    r2 = _render(ids)
    assert len(fake_engine.submissions) == 1, "orphan must be adopted, not resubmitted"
    assert r2["engine_task_id"] == "task-1"
    adopted = _video_row(ids["variant"])
    assert adopted.id in (r2["video_id"], row.id)


def test_completed_render_is_fully_idempotent(workspace_with_user, fake_engine):
    ids = _make_content(workspace_with_user)
    r1 = _render(ids)
    r2 = _render(ids)
    assert len(fake_engine.submissions) == 1
    assert r2["video_id"] == r1["video_id"]
