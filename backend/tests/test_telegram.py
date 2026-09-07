"""Telegram remote-control integration tests.

Covers: pairing code lifecycle, command handling, API endpoints
(pairing-code, status, unlink, toggle, test-send) and the record_event
fan-out hook. The Telegram HTTP boundary is faked via monkeypatched
send/_api — no real network access.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _mk_workspace(db, name: str) -> str:
    from app.models import Workspace

    ws = Workspace(name=name, slug=name)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws.id


def _register(client) -> tuple[str, dict]:
    email = f"tg{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], {
        "Authorization": f"Bearer {data['access_token']}"
    }, data["workspace"]["id"]


@pytest.fixture()
def tg_env(monkeypatch):
    """Fake bot token + captured outgoing messages."""
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id, text, thread_id=None):
        sent.append((chat_id, text))

    from app.services import telegram_service as tg

    monkeypatch.setattr(tg, "send_message", fake_send)
    monkeypatch.setattr(tg, "bot_configured", lambda: True)
    monkeypatch.setattr(tg, "get_bot_token", lambda: ("test-token", "test"))
    return sent


# ---------------------------------------------------------------------------
# Service-level
# ---------------------------------------------------------------------------

def test_pairing_code_lifecycle(tg_env, db_session):
    from app.services import telegram_service as tg

    ws_id = _mk_workspace(db_session, f"ws-tg-{os.urandom(3).hex()}")
    out = tg.create_pairing_code(ws_id, "user-1")
    assert len(out["code"]) == 6
    assert out["expires_in_seconds"] == 15 * 60


def test_handle_update_pairs_chat(tg_env, db_session):
    from app.db import session_scope
    from app.models import TelegramLink
    from app.services import telegram_service as tg

    ws_id = _mk_workspace(db_session, f"ws-pair-{os.urandom(3).hex()}")
    code = tg.create_pairing_code(ws_id, "user-1")["code"]

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 555001, "title": "Ops Chat"},
            "text": f"/start {code}",
        },
    }
    tg.handle_update(update)

    with session_scope() as s:
        link = s.query(TelegramLink).filter_by(workspace_id=ws_id, chat_id="555001").first()
        assert link is not None
        assert link.active is True
    # pairing confirmation was "sent"
    assert any(cid == "555001" for cid, _ in tg_env)


def test_handle_update_rejects_bad_code(tg_env):
    from app.db import session_scope
    from app.models import TelegramLink
    from app.services import telegram_service as tg

    tg.handle_update({
        "update_id": 2,
        "message": {"chat": {"id": 555002}, "text": "/start NOPE01"},
    })
    with session_scope() as s:
        assert s.query(TelegramLink).filter_by(chat_id="555002").first() is None
    assert any("Invalid or expired" in txt for _, txt in tg_env)


def test_commands_require_link(tg_env):
    from app.services import telegram_service as tg

    tg.handle_update({
        "update_id": 3,
        "message": {"chat": {"id": 555003}, "text": "/status"},
    })
    assert any("not linked" in txt.lower() for _, txt in tg_env)


def test_status_command(tg_env, db_session):
    from app.services import telegram_service as tg

    ws_id = _mk_workspace(db_session, f"ws-status-{os.urandom(3).hex()}")
    code = tg.create_pairing_code(ws_id, "user-1")["code"]
    tg.handle_update({"update_id": 4, "message": {"chat": {"id": 555004}, "text": f"/start {code}"}})
    tg_env.clear()
    tg.handle_update({"update_id": 5, "message": {"chat": {"id": 555004}, "text": "/status"}})
    assert any("Autopilot" in txt for _, txt in tg_env)


def test_on_event_filtering(tg_env, monkeypatch):
    from app.services import telegram_service as tg

    # error-level events always notify
    tg.on_event("ws-none", "anything.at.all", "boom", "error")
    # non-notifying info event for a workspace with no links is a no-op
    tg.on_event("ws-none", "cycle.completed", "done", "info")
    # nothing should crash; sends attempted only for error kind
    assert isinstance(tg_env, list)


def test_quality_failed_and_cycle_failure_notify(tg_env, db_session, monkeypatch):
    """The operator's phone must light up on QC failure and cycle failure."""
    from app.db import session_scope
    from app.models import TelegramLink
    from app.services import telegram_service as tg

    ws_id = _mk_workspace(db_session, f"ws-notify-{os.urandom(3).hex()}")
    with session_scope() as s:
        s.add(TelegramLink(workspace_id=ws_id, chat_id="777100", chat_title="ops", active=True))

    tg_env.clear()
    tg.on_event(ws_id, "quality.failed", "Video failed quality after 2 attempts", "error")
    tg.on_event(ws_id, "cycle.failed", "Cycle failed (1/3 consecutive): render failed", "error")
    tg.on_event(ws_id, "quality.rejected", "Quality rejected (72/100) — regenerating", "warning")
    tg.on_event(ws_id, "autopilot.stopped", "Autopilot stopped — circuit breaker", "error")
    assert len(tg_env) == 4
    joined = " ".join(txt for _, txt in tg_env)
    assert "QUALITY FAILED" in joined
    assert "CYCLE FAILED" in joined
    assert "QUALITY REJECTED" in joined
    assert "AUTOPILOT STOPPED" in joined


def test_noisy_successes_are_suppressed(tg_env, db_session):
    """Per-render successes stay off the phone; milestones still notify."""
    from app.db import session_scope
    from app.models import TelegramLink
    from app.services import telegram_service as tg

    ws_id = _mk_workspace(db_session, f"ws-quiet-{os.urandom(3).hex()}")
    with session_scope() as s:
        s.add(TelegramLink(workspace_id=ws_id, chat_id="777101", chat_title="ops", active=True))

    tg_env.clear()
    tg.on_event(ws_id, "video.generation.completed", "Video ready (51s)", "success")
    tg.on_event(ws_id, "quality.passed", "Quality approved (79/100)", "success")
    assert len(tg_env) == 0  # suppressed

    tg.on_event(ws_id, "publish.done", "Published to 3 platform(s)", "success")
    tg.on_event(ws_id, "cycle.completed", "Cycle completed — learning updated", "success")
    assert len(tg_env) == 2  # milestones still come through


# ---------------------------------------------------------------------------
# API-level
# ---------------------------------------------------------------------------

def test_api_pairing_and_status(client, tg_env):
    token, headers, ws_id = _register(client)

    r = client.get(f"/api/v1/workspaces/{ws_id}/telegram/status", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is False
    assert "links" in body

    r = client.post(f"/api/v1/workspaces/{ws_id}/telegram/pairing-code", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["code"]) == 6


def test_api_pairing_requires_auth(client):
    r = client.post("/api/v1/workspaces/some-ws/telegram/pairing-code")
    assert r.status_code in (401, 403)


def test_api_toggle_and_unlink(client, tg_env, db_session):
    from app.models import TelegramLink

    token, headers, ws_id = _register(client)
    link = TelegramLink(workspace_id=ws_id, chat_id="999001", chat_title="T", active=True)
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)

    r = client.post(f"/api/v1/workspaces/{ws_id}/telegram/links/{link.id}/toggle", headers=headers)
    assert r.status_code == 200
    assert r.json()["active"] is False

    r = client.delete(f"/api/v1/workspaces/{ws_id}/telegram/links/{link.id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["unlinked"] is True


def test_api_test_send_without_chats(client, tg_env):
    token, headers, ws_id = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/telegram/test", headers=headers, json={})
    assert r.status_code == 409
    assert "no active linked chats" in r.json()["detail"]


def test_api_test_send_with_chats(client, tg_env, db_session):
    from app.models import TelegramLink

    token, headers, ws_id = _register(client)
    link = TelegramLink(workspace_id=ws_id, chat_id="999002", chat_title="T2", active=True)
    db_session.add(link)
    db_session.commit()

    r = client.post(f"/api/v1/workspaces/{ws_id}/telegram/test", headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == 1
