"""Dubbing pipeline tests: SRT utils, timing fit, assembly, agent, API validation."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.providers import dubbing as dub_mod
from app.providers.dubbing import (
    DubError,
    fit_ratio,
    format_srt,
    parse_srt,
    pick_voice,
    to_bilingual,
)


def _has_ffmpeg() -> bool:
    return dub_mod.ffmpeg_present()


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:04,500
Hello and welcome back

2
00:00:05,000 --> 00:00:08,000
Today we save money
"""


def test_srt_round_trip():
    cues = parse_srt(SAMPLE_SRT)
    assert len(cues) == 2
    assert cues[0].start == 1.0 and cues[0].end == 4.5
    assert cues[1].text == "Today we save money"
    assert parse_srt(format_srt(cues))[1].text == "Today we save money"


def test_srt_skips_malformed_blocks():
    cues = parse_srt("garbage\n\n" + SAMPLE_SRT + "\n99\nno-timestamps-here\n")
    assert len(cues) == 2


def test_bilingual_merge_keeps_original_first():
    cues = parse_srt(SAMPLE_SRT)
    out = to_bilingual(cues, ["Hola y bienvenidos", ""])
    assert out[0].text == "Hello and welcome back\nHola y bienvenidos"
    assert out[1].text == "Today we save money"


def test_fit_ratio_clamps():
    assert fit_ratio(2.0, 5.0) == 1.0  # fits: untouched
    assert fit_ratio(5.0, 5.0) == 1.0
    assert fit_ratio(6.0, 5.0) == 1.2
    assert fit_ratio(10.0, 5.0) == 1.35  # never past chipmunk range
    assert fit_ratio(3.0, 0.0) == 1.0


def test_translate_without_llm_fails_closed():
    from app.providers import llm as llm_mod

    if llm_mod.llm_available():
        pytest.skip("LLM configured; offline failure path not applicable")
    with pytest.raises(DubError, match="LLM provider not configured"):
        dub_mod.translate_segments(["hello"], "es", "ws-x")


def test_pick_voice_explicit_wins():
    assert pick_voice("es", "en-US-AndrewNeural") == "en-US-AndrewNeural"


def test_dub_status_contract():
    status = dub_mod.dub_status()
    assert set(status) >= {"ffmpeg", "llm", "tts", "languages", "ready"}
    assert "es" in status["languages"] and "hi" in status["languages"]
    assert isinstance(status["ready"], bool)


def _make_video(dest: Path, seconds: int = 20) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=640x960:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dest)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]


def _make_tone(dest: Path, seconds: float = 2.0) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds}",
         "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]


@pytest.mark.slow
@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_assemble_dubbed_portrait_bilingual(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "source.mp4"
    _make_video(src)
    work = tmp_path / "work"
    work.mkdir()
    dub_files = []
    for i in range(2):
        p = work / f"dub_{i}.wav"
        _make_tone(p, seconds=3.0)
        dub_files.append(p)
    cues = parse_srt(SAMPLE_SRT)
    srt_path = work / "bilingual.srt"
    srt_path.write_text(format_srt(dub_mod.to_bilingual(cues, ["Hola", "Ahorra"])), encoding="utf-8")
    built = dub_mod.assemble_dubbed(src, cues, dub_files, "ws-dub", srt_path, portrait=True)
    out = Path(built["video_path"])
    assert out.exists() and out.stat().st_size > 10_000
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(out)], capture_output=True, timeout=30,
    )
    import json as _json

    streams = _json.loads(probe.stdout or "{}").get("streams", [])
    kinds = {s.get("codec_type") for s in streams}
    assert {"video", "audio"} <= kinds
    video = next(s for s in streams if s.get("codec_type") == "video")
    assert (video.get("width"), video.get("height")) == (1080, 1920)


def test_dubbing_agent_with_mocked_pipeline(tmp_path, monkeypatch):
    from app.engine.agents import dubbing as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)

    class _FakeInfo:
        title = "fake talk"
        duration = 20.0
        local_path = Path("source.mp4")

    class _FakeRep:
        def acquire(self, source, ws):
            return _FakeInfo()

        def transcribe_segments(self, info):
            from app.providers.clips import TranscriptSegment

            return [TranscriptSegment(start=0.0, end=5.0, text="hello world")]

    monkeypatch.setattr(agent_mod, "get_repurposer", lambda: _FakeRep())
    monkeypatch.setattr(agent_mod, "translate_segments", lambda texts, lang, ws, gloss=None: ["hola mundo"])
    monkeypatch.setattr(agent_mod, "pick_voice", lambda lang, voice="": "es-Voice")
    monkeypatch.setattr(agent_mod, "synthesize_segments",
                        lambda texts, voice, d: [d / "dub_000.mp3"])
    monkeypatch.setattr(agent_mod, "assemble_dubbed",
                        lambda *a, **k: {"video_path": "data/videos/ws-x/dub.mp4", "srt_path": "x.srt"})

    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-x",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = agent_mod.DubbingLocalizerAgent().dub(ctx, source="local.mp4", target_lang="es")
    assert out["target_lang"] == "es"
    assert out["voice"] == "es-Voice"
    assert out["cues"] == 1
    assert out["video_path"].endswith("dub.mp4")


def test_dub_api_validation():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"dub{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/assets/dub/status", headers=headers)
    assert r.status_code == 200, r.text
    assert "languages" in r.json()

    # missing source file → honest 400, not a silent job
    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/dub", headers=headers,
                    json={"source": "/nonexistent/video.mp4", "target_lang": "es"})
    assert r.status_code == 400


def test_dry_run_validates_without_side_effects(tmp_path):
    from app.providers import dubbing as dub_mod

    bad = dub_mod.dry_run_dub("/nonexistent/video.mp4", "xx", srt="not subtitles")
    assert bad["ok"] is False
    steps = {c["step"]: c["status"] for c in bad["checks"]}
    assert steps["language"] == "failed" and steps["source"] == "failed"
    assert steps["subtitles"] == "failed"

    f = tmp_path / "s.mp4"
    f.write_bytes(b"\x00" * 512)
    srt = "1\n00:00:00,000 --> 00:00:02,000\nhola mundo\n"
    good = dub_mod.dry_run_dub(str(f), "es", voice="Rachel", srt=srt)
    assert [c["step"] for c in good["checks"]] == [
        "language", "source", "subtitles", "translation", "voice", "tts", "assemble"]
    assert good["checks"][2]["detail"].startswith("1 supplied cue")
    assert "Rachel" in good["checks"][4]["detail"]
    # nothing written anywhere near the source
    assert [p for p in tmp_path.iterdir() if p.name != "s.mp4"] == []


def test_dub_dry_run_endpoint_always_200():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"dub{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ws_id = r.json()["workspace"]["id"]

    r = client.post(f"/api/v1/workspaces/{ws_id}/assets/dub/dry-run", headers=headers,
                    json={"source": "/nonexistent/video.mp4", "target_lang": "xx"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert {c["step"] for c in body["checks"]} >= {"language", "source", "translation", "tts", "assemble"}
