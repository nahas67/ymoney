"""Outbound webhooks: subscribe validation, fan-out, signed delivery + retries."""
from __future__ import annotations

import hashlib
import hmac
import uuid


def _register(client, email=None):
    email = email or f"wh{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _make_source(path, seconds=30):
    import subprocess

    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=640x480:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]
    return path


def _sub(client, headers, ws_id, url="http://127.0.0.1:9/hook", events=("publish.failed",)):
    r = client.post(f"/api/v1/workspaces/{ws_id}/webhooks", headers=headers,
                    json={"url": url, "events": list(events)})
    assert r.status_code == 200, r.text
    return r.json()


def test_webhook_crud_validation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    # auth required
    r = client.post(f"/api/v1/workspaces/{ws_id}/webhooks", json={"url": "https://x.test/h", "events": ["publish.failed"]})
    assert r.status_code in (401, 403)

    # bad URLs rejected
    for bad in ("ftp://x.test/h", "not-a-url", "http://example.com/hook"):
        r = client.post(f"/api/v1/workspaces/{ws_id}/webhooks", headers=headers,
                        json={"url": bad, "events": ["publish.failed"]})
        assert r.status_code == 422, (bad, r.text)

    # unknown events rejected
    r = client.post(f"/api/v1/workspaces/{ws_id}/webhooks", headers=headers,
                    json={"url": "https://x.test/h", "events": ["nope.kind"]})
    assert r.status_code == 422

    # ok — secret shown once, never listed
    body = _sub(client, headers, ws_id, url="https://x.test/hook")
    assert body["secret"].startswith("whsec_")
    sub_id = body["id"]
    r = client.get(f"/api/v1/workspaces/{ws_id}/webhooks", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert "secret" not in items[0] and "secret_enc" not in items[0]
    assert items[0]["events"] == ["publish.failed"]

    # test ping enqueues (no network — delivery runs on the job queue)
    r = client.post(f"/api/v1/workspaces/{ws_id}/webhooks/{sub_id}/test", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["enqueued"] is True

    # delete + 404 on repeat + cross-workspace isolation
    r = client.delete(f"/api/v1/workspaces/{ws_id}/webhooks/{sub_id}", headers=headers)
    assert r.status_code == 200
    r = client.delete(f"/api/v1/workspaces/{ws_id}/webhooks/{sub_id}", headers=headers)
    assert r.status_code == 404
    _, _, headers2 = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/webhooks", headers=headers2)
    assert r.status_code in (403, 404)


def test_webhook_fanout_and_delivery(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from sqlalchemy import select

    from app.db import session_scope
    from app.main import create_app
    from app.models import Job
    from app.services import webhooks as wh
    from app.services.events import record_event
    from app.services.jobs import JobContext

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)
    sub = _sub(client, headers, ws_id)
    secret, sub_id = sub["secret"], sub["id"]

    def _dispatch_jobs():
        # NOTE: the test DB is session-shared; scope to this workspace.
        with session_scope() as s:
            return s.scalars(
                select(Job).where(Job.type == "webhook.dispatch", Job.workspace_id == ws_id)
            ).all()

    # subscribed kind fans out with a stable idempotency key
    payload = record_event(ws_id, "publish.failed", "boom", level="error", source="publisher", data={"video_id": "v"})
    rows = _dispatch_jobs()
    assert len(rows) == 1
    assert rows[0].idempotency_key == f"wh-{payload['id']}-{sub_id}"
    assert rows[0].workspace_id == ws_id

    # unsubscribed kind fans out nothing
    record_event(ws_id, "variant.selected", "x", source="studio")
    assert len(_dispatch_jobs()) == 1

    def _ctx():
        return JobContext(
            job_id="t1", type="webhook.dispatch", workspace_id=ws_id, cycle_id=None,
            payload={"sub_id": sub_id, "event_id": payload["id"], "kind": "publish.failed",
                      "message": "boom", "level": "error", "source": "publisher",
                      "data": {"video_id": "v"}, "created_at": "t"},
            attempt=1, cancelled=lambda: False,
        )

    # success: HMAC verifies against the once-only secret
    seen: dict = {}

    class Resp200:
        status_code = 200

    def fake_post(url, **kw):
        seen["url"] = url
        seen["headers"] = kw["headers"]
        seen["body"] = kw["content"]
        return Resp200()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = wh.dispatch_webhook(_ctx())
    assert out["delivered"] is True
    assert seen["url"] == "http://127.0.0.1:9/hook"
    expect = "sha256=" + hmac.new(secret.encode(), seen["body"], hashlib.sha256).hexdigest()
    assert seen["headers"]["X-YM-Signature"] == expect
    assert seen["headers"]["X-YM-Event"] == "publish.failed"
    assert "delivery_id" in seen["body"].decode()

    # retryable → raises for queue backoff
    class Resp500:
        status_code = 500

    monkeypatch.setattr(httpx, "post", lambda url, **kw: Resp500())
    try:
        wh.dispatch_webhook(_ctx())
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    # terminal 4xx → returns without raising
    class Resp404:
        status_code = 404

    monkeypatch.setattr(httpx, "post", lambda url, **kw: Resp404())
    out = wh.dispatch_webhook(_ctx())
    assert out["delivered"] is False and out["terminal"] is True


def test_repurpose_webhook_url_validated_first(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/repurpose", headers=headers,
                    json={"url": "http://example.com/v.mp4", "webhook_url": "ftp://x.test/hook"})
    assert r.status_code == 422, r.text
    assert "http" in r.text.lower()  # webhook validation fires before acquire


def test_repurpose_inline_webhook_delivered(tmp_path, monkeypatch):
    import hashlib
    import hmac as _hmac

    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.main import create_app
    from app.models import Job, WebhookSubscription
    from app.services import webhooks as wh
    from app.services.jobs import JobContext

    client = TestClient(create_app(), raise_server_exceptions=False)
    _, ws_id, headers = _register(client)
    src = _make_source(tmp_path / "source.mp4")

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/repurpose", headers=headers,
                    json={"url": str(src), "max_clips": 1,
                          "webhook_url": "http://127.0.0.1:9/hook", "webhook_secret": "s3cr3t"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"], body
    assert body["webhook"]["enqueued"] is True

    # no subscription row — this delivery is per-job
    with session_scope() as s:
        assert s.scalars(select(WebhookSubscription).where(
            WebhookSubscription.workspace_id == ws_id)).all() == []
        rows = s.scalars(select(Job).where(
            Job.type == "webhook.dispatch", Job.workspace_id == ws_id)).all()
    assert len(rows) == 1

    seen: dict = {}

    class Resp200:
        status_code = 200

    def fake_post(url, **kw):
        seen["headers"] = kw["headers"]
        seen["body"] = kw["content"]
        return Resp200()

    monkeypatch.setattr(httpx, "post", fake_post)
    ctx = JobContext(job_id=rows[0].id, type="webhook.dispatch", workspace_id=ws_id, cycle_id=None,
                     payload=dict(rows[0].payload), attempt=1, cancelled=lambda: False)
    out = wh.dispatch_webhook(ctx)
    assert out["delivered"] is True
    expect = "sha256=" + _hmac.new(b"s3cr3t", seen["body"], hashlib.sha256).hexdigest()
    assert seen["headers"]["X-YM-Signature"] == expect
    assert seen["headers"]["X-YM-Event"] == "repurpose.completed"

    # unsigned per-job call omits the signature header
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/repurpose", headers=headers,
                    json={"url": str(src), "max_clips": 1, "webhook_url": "http://127.0.0.1:9/hook"})
    assert r.status_code == 200, r.text
    assert r.json()["webhook"]["enqueued"] is True
    with session_scope() as s:
        rows2 = s.scalars(select(Job).where(
            Job.type == "webhook.dispatch", Job.workspace_id == ws_id)).all()
    newest = max(rows2, key=lambda j: j.created_at)
    ctx2 = JobContext(job_id=newest.id, type="webhook.dispatch", workspace_id=ws_id, cycle_id=None,
                      payload=dict(newest.payload), attempt=1, cancelled=lambda: False)
    seen.clear()
    monkeypatch.setattr(httpx, "post", fake_post)
    out = wh.dispatch_webhook(ctx2)
    assert out["delivered"] is True
    assert "X-YM-Signature" not in seen["headers"]
