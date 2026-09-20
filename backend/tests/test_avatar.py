"""Avatar provider tests: probes, fail-closed backends, agent, API validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.providers import avatar as avatar_mod
from app.providers.avatar import AvatarError, avatar_status


def _has_ffmpeg() -> bool:
    return avatar_mod.ffmpeg_present()


def _ws_files(tmp_path: Path, ws: str = "ws-av") -> tuple[str, str]:
    d = tmp_path / "data" / "videos" / ws
    d.mkdir(parents=True, exist_ok=True)
    img = d / "face.jpg"
    img.write_bytes(b"\xff\xd8\xff fake-jpeg")
    aud = d / "voice.mp3"
    aud.write_bytes(b"fake-mp3")
    return str(img), str(aud)


def test_status_contract():
    status = avatar_status()
    assert set(status) >= {"backend", "ready", "detail", "ffmpeg"}
    assert isinstance(status["ready"], bool)


def test_backends_fail_closed(monkeypatch, tmp_path):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    img, aud = _ws_files(tmp_path)
    monkeypatch.setattr(config_mod.settings, "avatar_backend", "server")
    monkeypatch.setattr(config_mod.settings, "avatar_base_url", "")
    with pytest.raises(AvatarError, match="avatar.base_url"):
        avatar_mod.render_avatar(img, aud, "ws-av")
    monkeypatch.setattr(config_mod.settings, "avatar_backend", "sadtalker")
    monkeypatch.setattr(config_mod.settings, "sadtalker_dir", "")
    with pytest.raises(AvatarError, match="SAD_TALKER_DIR|not ready"):
        avatar_mod.render_avatar(img, aud, "ws-av")


def test_refs_must_be_workspace_assets(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    (tmp_path / "outside.mp3").write_bytes(b"x")
    monkeypatch.setattr(config_mod.settings, "avatar_backend", "mock")
    with pytest.raises(AvatarError, match="workspace asset"):
        avatar_mod.render_avatar(str(tmp_path / "outside.mp3"), str(tmp_path / "outside.mp3"), "ws-av")


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_mock_backend_renders_labeled_clip(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    img, aud = _ws_files(tmp_path)
    monkeypatch.setattr(config_mod.settings, "avatar_backend", "mock")
    clip = avatar_mod.render_avatar(img, aud, "ws-av")
    assert clip.is_mock is True and clip.backend == "mock"
    assert Path(clip.path).exists()


def test_server_backend_success_with_mocked_http(tmp_path, monkeypatch):
    from app.core import config as config_mod

    monkeypatch.chdir(tmp_path)
    img, aud = _ws_files(tmp_path)
    monkeypatch.setattr(config_mod.settings, "avatar_backend", "server")
    monkeypatch.setattr(config_mod.settings, "avatar_base_url", "http://avatar.local")

    class _Resp:
        status_code = 200
        headers = {"content-type": "video/mp4"}
        content = b"mp4bytes"
        text = ""

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    clip = avatar_mod.render_avatar(img, aud, "ws-av")
    assert clip.backend == "server" and Path(clip.path).exists()


def test_avatar_director_agent_text_path(tmp_path, monkeypatch):
    from app.engine.agents import avatar as agent_mod
    from app.providers import tts as tts_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    _ws_files(tmp_path, "ws-x")
    monkeypatch.setattr(agent_mod, "render_avatar",
                        lambda image, audio, ws, **k: avatar_mod.AvatarClip(
                            path="data/videos/ws-x/presenter.mp4", backend="mock",
                            duration=3.0, is_mock=True))
    monkeypatch.setattr("app.providers.tts.get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-x",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = agent_mod.AvatarDirectorAgent().direct(ctx, image="x", text="hello world test script here")
    assert out["backend"] == "mock" and out["is_mock"] is True


def test_avatar_director_rejects_audio_and_text(tmp_path, monkeypatch):
    from app.engine.agents import avatar as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-x",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    with pytest.raises(AvatarError, match="exactly one"):
        agent_mod.AvatarDirectorAgent().direct(ctx, image="x", audio="a.mp3", text="hi")


def test_avatar_skill_agent_registered():
    from app.engine.agents.registry import AGENTS
    from app.engine.capabilities import get_skill, get_tool

    assert get_skill("avatar_direction").required_tools == ("render_avatar",)
    assert "media:render" in get_tool("render_avatar").permissions
    assert AGENTS["avatar_director"].meta.title == "Avatar Director"
    assert len(AGENTS) == 22


def test_avatar_api_validation():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"av{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/avatar/status", headers=headers)
    assert r.status_code == 200, r.text
    assert "backend" in r.json()

    # both audio and text → honest 400
    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/avatar", headers=headers,
                    json={"image": "x.jpg", "audio": "a.mp3", "text": "hi"})
    assert r.status_code == 400

    # missing workspace asset → 400, never silent
    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/avatar", headers=headers,
                    json={"image": "ghost.jpg", "text": "hello world voice test"})
    assert r.status_code in (400, 503)
