"""E2 voices: Chatterbox/Qwen3 providers, Voice Designer agent, preview API."""
from __future__ import annotations

import pytest

from app.providers import tts as tts_mod
from app.providers.tts import (
    ChatterboxTTSProvider,
    QwenTTSProvider,
    TTSError,
    get_tts_provider,
    tts_provider_status,
)


class _Resp:
    def __init__(self, status=200, content=b"audio-bytes", text="", json_data=None):
        self.status_code = status
        self.content = content
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def test_factory_rejects_unknown_provider():
    with pytest.raises(TTSError, match="unknown TTS provider"):
        get_tts_provider("nope-tts")


def test_chatterbox_needs_package_or_server(monkeypatch):
    monkeypatch.setattr(ChatterboxTTSProvider, "_native_available", staticmethod(lambda: False))
    monkeypatch.setattr(tts_mod, "_chatterbox_base_url", lambda: "")
    with pytest.raises(TTSError, match="chatterbox-tts|chatterbox_base_url"):
        get_tts_provider("chatterbox").synthesize("hello")


def test_qwen_needs_server_url(monkeypatch):
    monkeypatch.setattr(tts_mod, "_qwen_base_url", lambda: "")
    with pytest.raises(TTSError, match="qwen_base_url"):
        get_tts_provider("qwen3")


def test_chatterbox_server_path(monkeypatch):
    seen: dict = {}

    def fake_post(url, **kw):
        seen.update(kw.get("json", {}))
        assert url.endswith("/audio/speech")
        assert kw["json"]["exaggeration"] == 0.7
        return _Resp(content=b"mp3bytes")

    monkeypatch.setattr("httpx.post", fake_post)
    prov = ChatterboxTTSProvider("http://tts.local")
    res = prov.synthesize("hello [laugh] world", voice="v1", exaggeration=0.7)
    assert res.format == "mp3" and res.provider == "chatterbox"
    assert seen["model"] == "chatterbox-turbo"
    assert "[laugh]" in seen["input"]  # paralinguistic tags pass through


def test_chatterbox_server_clone_needs_native():
    prov = ChatterboxTTSProvider("http://tts.local")
    with pytest.raises(TTSError, match="native chatterbox package"):
        prov.synthesize("hi", clone_from="data/videos/ws/x.wav")


def test_qwen_server_payload(monkeypatch):
    seen: dict = {}

    def fake_post(url, **kw):
        seen.update(kw.get("json", {}))
        return _Resp(content=b"mp3bytes")

    monkeypatch.setattr("httpx.post", fake_post)
    prov = QwenTTSProvider("http://qwen.local", instruct="speak cheerfully")
    res = prov.synthesize("bonjour", voice="m1", language="fr")
    assert res.provider == "qwen3"
    assert seen["language"] == "fr" and seen["instruct"] == "speak cheerfully"


def test_qwen_server_error_surfaces(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(status=500, text="boom"))
    with pytest.raises(TTSError, match="HTTP 500"):
        QwenTTSProvider("http://qwen.local").synthesize("hi")


def test_voices_fallback_without_server(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(status=500, text="x"))
    assert ChatterboxTTSProvider("http://x").voices() == [
        {"id": "default", "gender": "", "locale": "en"}]
    assert QwenTTSProvider("http://x").voices()[0]["id"] == "default"


def test_db_provider_override_selects_mock():
    from app.services.provider_settings import get_credential, set_credential

    before, _ = get_credential("tts.provider")
    set_credential("tts.provider", "mock")
    try:
        assert get_tts_provider().name == "mock"
        assert tts_provider_status()["provider"] == "mock"
    finally:
        set_credential("tts.provider", before)


def test_voice_design_skill_and_agent_registered():
    from app.engine.agents.registry import AGENTS
    from app.engine.capabilities import get_skill, get_tool

    assert get_skill("voice_design").required_tools == ("synthesize_speech",)
    assert "tts:synthesize" in get_tool("synthesize_speech").permissions
    assert AGENTS["voice_designer"].meta.title == "Voice Designer"
    assert len(AGENTS) == 17


def test_voice_designer_design_with_mock(tmp_path, monkeypatch):
    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = agent_mod.VoiceDesignerAgent().design(ctx, text="hello brave new world")
    assert out["provider"] == "mock" and out["is_mock"] is True
    from pathlib import Path

    assert Path(out["audio_path"]).exists()


def test_voice_designer_clone_outside_boundary_rejected(tmp_path, monkeypatch):
    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    with pytest.raises(TTSError, match="workspace asset"):
        agent_mod.VoiceDesignerAgent().design(ctx, text="hi there friend", clone_from="/etc/passwd")


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed")
def test_voice_designer_batch_concats(tmp_path, monkeypatch):
    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = agent_mod.VoiceDesignerAgent().design_batch(
        ctx, parts=[{"speaker": "a", "text": "first line here now"},
                    {"speaker": "b", "text": "second line here now"}])
    assert out["parts"] == 2
    from pathlib import Path

    assert Path(out["audio_path"]).exists()


def test_voice_preview_api():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"vox{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/voice/preview", headers=headers,
                    json={"text": "hello voice lab", "provider": "mock"})
    assert r.status_code == 200, r.text
    assert r.headers["X-TTS-Provider"] == "mock"
    assert len(r.content) > 100

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/voice/preview", headers=headers,
                    json={"text": "hi", "provider": "nope-tts"})
    assert r.status_code == 503
