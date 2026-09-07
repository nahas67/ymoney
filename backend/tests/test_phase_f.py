"""Phase F regression coverage: scheduled publishing executor.

The Calendar's PENDING entries must actually EXECUTE: a sweep claims due
entries (DISPATCHING) and enqueues an idempotent cycle.upload restricted to
the entry's platform. Queue acceptance alone never marks an entry DONE — the
entry stays QUEUED until the provider reports success, and honest FAILED
status is reached when retries are exhausted. Entries without renderable
content are cancelled rather than silently skipped.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models.base import utcnow


@pytest.fixture()
def db(db_session):
    """Alias for the shared db_session fixture (shorter name)."""
    return db_session


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[str, str, dict]:
    email = f"phaseF{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


def _mk_workspace(db, name: str) -> str:
    from app.models import Workspace

    ws = Workspace(name=name, slug=name)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws.id


def _mk_entry(db, ws_id: str, *, platform: str = "youtube", due: bool = True,
              content_item_id: str | None = None, run_at=None) -> str:
    from app.models import ScheduleEntry

    entry = ScheduleEntry(
        workspace_id=ws_id,
        platform=platform,
        run_at=run_at or (utcnow() - timedelta(minutes=1) if due else utcnow() + timedelta(hours=1)),
        content_item_id=content_item_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry.id


def test_sweep_dispatches_due_entry_with_content(db):

    from app.engine.autopilot import sweep_due_schedules
    from app.models import ContentItem, ScheduleEntry
    from app.services import jobs as jobs_service

    ws = _mk_workspace(db, "sched-a")
    content = ContentItem(workspace_id=ws, topic="scheduled topic", status="APPROVED")
    db.add(content)
    db.commit()
    db.refresh(content)

    # attach a selected variant + rendered video so the sweep finds content
    from app.models import Video, VideoVariant
    variant = VideoVariant(content_item_id=content.id, selected=True, script="s")
    db.add(variant)
    db.commit()
    db.refresh(variant)
    video = Video(workspace_id=ws, variant_id=variant.id,
                  file_path="data/videos/x.mp4", engine="mock", status="READY")
    db.add(video)
    db.commit()
    db.refresh(video)

    entry_id = _mk_entry(db, ws, platform="tiktok", content_item_id=content.id)

    dispatched = sweep_due_schedules()
    assert dispatched == 1

    db.refresh(entry := db.get(ScheduleEntry, entry_id))
    # Queue acceptance is NOT publication: the entry must stay visibly
    # in-flight (QUEUED) until the upload job reports provider success.
    assert entry.status == "QUEUED"

    # idempotent upload job enqueued for the right platform
    job = db.query(jobs_service.Job).filter(
        jobs_service.Job.idempotency_key == f"sched-{entry_id}"
    ).first()
    assert job is not None
    assert job.payload["platforms_override"] == ["tiktok"]
    assert job.payload["video_id"] == video.id
    assert job.type == "cycle.upload"

    # second sweep is a no-op: the fresh QUEUED lease is not stale yet
    assert sweep_due_schedules() == 0

    # Executing the queued upload job (mock provider succeeds) is what flips
    # the entry to DONE — proving DONE means "published", not "queued".
    from app.engine.autopilot import handle_upload
    from app.services.jobs import JobContext

    ctx = JobContext(
        job_id=job.id, type="cycle.upload", workspace_id=ws, cycle_id=None,
        payload={"cycle_id": None, "content_id": content.id, "video_id": video.id,
                 "platforms_override": ["tiktok"], "scheduled_entry_id": entry_id},
        attempt=1, cancelled=lambda: False,
    )
    handle_upload(ctx)

    db.expire_all()
    assert db.get(ScheduleEntry, entry_id).status == "DONE"


def test_sweep_cancels_entry_without_content_honestly(db):
    from app.engine.autopilot import sweep_due_schedules
    from app.models import ScheduleEntry

    ws = _mk_workspace(db, "sched-b")
    entry_id = _mk_entry(db, ws, content_item_id=None)

    assert sweep_due_schedules() == 0
    assert db.get(ScheduleEntry, entry_id).status == "CANCELLED"


def test_sweep_releases_claim_when_queue_insert_fails(db, monkeypatch):
    """A transient queue failure must not consume the schedule entry."""
    from app.engine.autopilot import sweep_due_schedules
    from app.models import ContentItem, ScheduleEntry, Video, VideoVariant
    from app.services import jobs as jobs_service

    ws = _mk_workspace(db, "sched-queue-failure")
    content = ContentItem(workspace_id=ws, topic="queue failure topic")
    db.add(content)
    db.flush()
    variant = VideoVariant(content_item_id=content.id, selected=True, script="s")
    db.add(variant)
    db.flush()
    video = Video(
        workspace_id=ws, variant_id=variant.id, file_path="data/videos/x.mp4",
        engine="mock", status="READY",
    )
    db.add(video)
    db.commit()
    db.refresh(video)
    entry_id = _mk_entry(db, ws, content_item_id=content.id)

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(jobs_service, "enqueue", fail_enqueue)
    assert sweep_due_schedules() == 0
    assert db.get(ScheduleEntry, entry_id).status == "PENDING"

    # Keep the shared test database isolated from later sweep assertions while
    # preserving the regression assertion above.
    db.get(ScheduleEntry, entry_id).status = "CANCELLED"
    db.commit()


def test_dead_scheduled_upload_marks_entry_failed(db):
    """Retry exhaustion (job DEAD) must surface as FAILED, never a QUEUED loop."""
    from app.models import ScheduleEntry
    from app.services import jobs as jobs_service

    ws = _mk_workspace(db, "sched-dead")
    entry_id = _mk_entry(db, ws, content_item_id=None)
    entry = db.get(ScheduleEntry, entry_id)
    entry.status = "QUEUED"
    db.commit()

    ctx = jobs_service.JobContext(
        job_id="dead-job", type="cycle.upload", workspace_id=ws, cycle_id=None,
        payload={"scheduled_entry_id": entry_id},
        attempt=4, cancelled=lambda: False,
    )
    jobs_service._on_dead(ctx, RuntimeError("provider rejected upload"))
    db.expire_all()
    assert db.get(ScheduleEntry, entry_id).status == "FAILED"

    # A crash after provider success must never regress a DONE entry.
    entry = db.get(ScheduleEntry, entry_id)
    entry.status = "DONE"
    db.commit()
    jobs_service._on_dead(ctx, RuntimeError("late duplicate"))
    db.expire_all()
    assert db.get(ScheduleEntry, entry_id).status == "DONE"


def test_transient_publisher_exception_stays_retryable(db):
    """A wholesale provider exception must not burn the retry budget.

    Marking the claimed PublishingJob terminal FAILED before re-raising makes
    the queue's retry a no-op that "completes" while the schedule stays QUEUED
    forever. The claim must remain retryable so the next attempt publishes.
    """
    from unittest.mock import patch

    import pytest

    from app.engine.autopilot import handle_upload, sweep_due_schedules
    from app.models import ContentItem, PublishingJob, ScheduleEntry, Video, VideoVariant
    from app.services.jobs import JobContext

    ws = _mk_workspace(db, "sched-transient")
    content = ContentItem(workspace_id=ws, topic="transient failure topic", status="APPROVED")
    db.add(content)
    db.flush()
    variant = VideoVariant(content_item_id=content.id, selected=True, script="s")
    db.add(variant)
    db.flush()
    video = Video(workspace_id=ws, variant_id=variant.id, file_path="data/videos/x.mp4",
                  engine="mock", status="READY")
    db.add(video)
    db.commit()
    db.refresh(video)
    entry_id = _mk_entry(db, ws, platform="youtube", content_item_id=content.id)
    assert sweep_due_schedules() == 1
    db.expire_all()
    assert db.get(ScheduleEntry, entry_id).status == "QUEUED"

    ctx = JobContext(
        job_id="tx-job", type="cycle.upload", workspace_id=ws, cycle_id=None,
        payload={"cycle_id": None, "content_id": content.id, "video_id": video.id,
                 "platforms_override": ["youtube"], "scheduled_entry_id": entry_id},
        attempt=1, cancelled=lambda: False,
    )

    with patch("app.engine.autopilot.PublisherAgent") as mock_pub:
        mock_pub.return_value.publish_to_platforms.side_effect = RuntimeError("platform timeout")
        with pytest.raises(RuntimeError):
            handle_upload(ctx)

    db.expire_all()
    pj = db.query(PublishingJob).filter(
        PublishingJob.video_id == video.id, PublishingJob.platform == "youtube"
    ).first()
    assert pj is not None
    assert pj.status == "RETRYING"  # not terminal FAILED
    assert db.get(ScheduleEntry, entry_id).status == "QUEUED"

    # The retry actually claims the platform again and completes the publish.
    with patch("app.engine.autopilot.PublisherAgent") as mock_pub:
        mock_pub.return_value.publish_to_platforms.return_value = [{
            "platform": "youtube", "success": True, "remote_post_id": "mock-y",
            "remote_url": "mock://y", "mock": True,
        }]
        handle_upload(ctx)

    db.expire_all()
    assert db.get(ScheduleEntry, entry_id).status == "DONE"


def test_sweep_ignores_future_and_cancelled(db):
    from app.engine.autopilot import sweep_due_schedules
    from app.models import ScheduleEntry

    ws = _mk_workspace(db, "sched-c")
    future_id = _mk_entry(db, ws, due=False, content_item_id=None)
    cancelled = ScheduleEntry(workspace_id=ws, platform="youtube",
                              run_at=utcnow() - timedelta(hours=2), status="CANCELLED")
    db.add(cancelled)
    db.commit()

    # The sweep is workspace-global, so another test's due entry may also be
    # dispatched. This test owns only these two rows and verifies they remain
    # untouched rather than asserting a global dispatch count.
    sweep_due_schedules()
    assert db.get(ScheduleEntry, future_id).status == "PENDING"
    assert db.get(ScheduleEntry, cancelled.id).status == "CANCELLED"


def test_calendar_round_trip_and_due_publish(client, db):
    """API-created entries become sweep-dispatchable; PATCH/DELETE rules hold."""
    _tok, ws_id, headers = _register(client)

    # future entry via API
    from datetime import timedelta as _td

    from app.models.base import utcnow as _u
    r = client.post(
        f"/api/v1/workspaces/{ws_id}/calendar",
        json={"platform": "instagram", "run_at": (_u() + _td(minutes=5)).isoformat()},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]

    # visible in list
    r = client.get(f"/api/v1/workspaces/{ws_id}/calendar", headers=headers)
    assert any(e["id"] == entry_id for e in r.json()["items"])

    # reschedule into the past -> sweep cancels it honestly (no content attached)
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/calendar/{entry_id}",
        json={"run_at": (_u() - _td(minutes=1)).isoformat()},
        headers=headers,
    )
    assert r.status_code == 200

    from app.engine.autopilot import sweep_due_schedules
    sweep_due_schedules()

    from app.models import ScheduleEntry
    assert db.get(ScheduleEntry, entry_id).status == "CANCELLED"

    # cancelling a CANCELLED entry conflicts
    r = client.delete(f"/api/v1/workspaces/{ws_id}/calendar/{entry_id}", headers=headers)
    assert r.status_code == 409
