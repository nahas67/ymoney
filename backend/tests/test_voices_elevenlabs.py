"""ElevenLabs cloud TTS: factory gating, wire payload, voices, health, cost estimate."""
from __future__ import annotations

import pytest

from app.providers import tts as tts_mod
from app.providers.tts import (
    ElevenLabsTTSProvider,
    TTSError,
    get_tts_provider,
)


class _Resp:
    def __init__(self, status=200, content=b"x" * 2048, text="", json_data=None):
        self.status_code = status
        self.content = content
        self.text = text
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=self)  # type: ignore[arg-type]


def _no_key(monkeypatch):
    monkeypatch.setattr(tts_mod, "_cred", lambda key, env="": "")


def test_factory_needs_api_key(monkeypatch):
    _no_key(monkeypatch)
    with pytest.raises(TTSError, match="ELEVENLABS_API_KEY|elevenlabs_api_key"):
        get_tts_provider("elevenlabs")
    with pytest.raises(TTSError, match="ELEVENLABS_API_KEY|elevenlabs_api_key"):
        ElevenLabsTTSProvider()


def test_factory_explicit_override(monkeypatch):
    monkeypatch.setattr(tts_mod, "_cred", lambda key, env="": "k-test")
    prov = get_tts_provider("elevenlabs")
    assert prov.name == "elevenlabs"
    assert get_tts_provider("xi").name == "elevenlabs"


def test_synthesize_wire_payload(monkeypatch):
    seen: dict = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["headers"] = kw["headers"]
        seen["json"] = kw["json"]
        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    prov = ElevenLabsTTSProvider(api_key="k-test")
    res = prov.synthesize("hello there", voice="v1", rate=1.1)
    assert res.provider == "elevenlabs" and res.format == "mp3"
    assert seen["url"] == "https://api.elevenlabs.io/v1/text-to-speech/v1"
    assert seen["headers"]["xi-api-key"] == "k-test"
    assert seen["json"]["model_id"] == "eleven_multilingual_v2"
    assert seen["json"]["speed"] == 1.1


def test_synthesize_default_voice_and_speed_rules(monkeypatch):
    seen: dict = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["json"] = kw["json"]
        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    prov = ElevenLabsTTSProvider(api_key="k-test")
    prov.synthesize("hi")  # default voice, rate 1.0 -> no speed key
    assert seen["url"].endswith(f"/text-to-speech/{ElevenLabsTTSProvider.DEFAULT_VOICE}")
    assert "speed" not in seen["json"]
    prov.synthesize("hi", rate=5.0)  # clamped to 1.2
    assert seen["json"]["speed"] == 1.2


def test_synthesize_errors_surface(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(status=401, text="invalid key", content=b""))
    with pytest.raises(TTSError, match="HTTP 401"):
        ElevenLabsTTSProvider(api_key="bad").synthesize("hi")
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(content=b"tiny"))
    with pytest.raises(TTSError, match="suspiciously small"):
        ElevenLabsTTSProvider(api_key="k").synthesize("hi")
    with pytest.raises(TTSError, match="text is empty"):
        ElevenLabsTTSProvider(api_key="k").synthesize("  ")


def test_voices_mapping_and_health(monkeypatch):
    voices_payload = {"voices": [
        {"voice_id": "v1", "name": "A", "labels": {"gender": "female", "accent": "american"}},
        {"voice_id": "v2", "name": "B", "labels": {"gender": "male", "accent": "british"}},
    ]}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(json_data=voices_payload))
    prov = ElevenLabsTTSProvider(api_key="k-test")
    voices = prov.voices()
    assert voices[0] == {"id": "v1", "gender": "female", "locale": "american"}
    assert [v["id"] for v in prov.voices(language="british")] == ["v2"]

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(json_data={}))
    assert prov.health() is True
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(status=401, text="nope"))
    assert prov.health() is False

    def _boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr("httpx.get", _boom)
    assert prov.health() is False


def test_voice_designer_records_estimate(tmp_path, monkeypatch):
    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: ElevenLabsTTSProvider(api_key="k-test"))
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    text = "hello brave new world"
    out = agent_mod.VoiceDesignerAgent().design(ctx, text=text, provider="elevenlabs")
    assert out["provider"] == "elevenlabs"
    assert ctx.artifacts["cost_usd"] == pytest.approx(len(text) * ElevenLabsTTSProvider.EST_USD_PER_CHAR)
