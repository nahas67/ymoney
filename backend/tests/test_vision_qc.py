"""Quality Agent + vision evidence integration via the real HTTP API.

Covers:
- mock video paths skip vision analysis (heuristic-only verdict)
- real video paths run the provider and fold its evidence into QC notes
- broken evidence (no audio, black frames) caps the affected components
- the autopilot verify stage reads the video file path from the Video row
"""

import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.vision import (
    SceneEvidence,
    VisionAnalysis,
    VisionAnalysisProvider,
    get_vision_provider,
    set_vision_provider,
)


class RecordingProvider(VisionAnalysisProvider):
    """Test double: records calls, returns fixed broken-render evidence."""

    name = "recording"
    is_mock = False

    def __init__(self, analysis: VisionAnalysis):
        self.analysis = analysis
        self.calls = []

    def analyze(self, *, video_path, script, scene_count, expected_duration):
        self.calls.append({
            "video_path": video_path,
            "scene_count": scene_count,
            "expected_duration": expected_duration,
        })
        return self.analysis


def _broken_analysis() -> VisionAnalysis:
    return VisionAnalysis(
        provider="recording",
        is_mock=False,
        scenes=[SceneEvidence(index=0, black_frames=True, corrupted=True)],
        audio_present=False,
        silent_sections=[(2.0, 5.0)],
        subtitles_aligned=False,
        visual_relevance=20.0,
        notes="broken render",
    )


def _register(client):
    email = f"qc-{os.urandom(4).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["access_token"], data["workspace"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


@pytest.mark.slow
def test_autopilot_verify_reads_video_path_and_caps_components():
    """Full single cycle: the verify stage reads the Video row's file path,
    runs the vision provider on real renders, and caps the QC components."""
    original = get_vision_provider()
    try:
        recording = RecordingProvider(_broken_analysis())
        set_vision_provider(recording)
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            token, ws_id, headers = _register(client)
            r = client.post(
                f"/api/v1/workspaces/{ws_id}/autopilot/start",
                json={"mode": "SINGLE_CYCLE", "config": {"measure_delay_minutes": 0.02},
                      "override_readiness": True},
                headers=headers,
            )
            assert r.status_code == 200, r.text

            deadline = time.time() + 30
            state = None
            while time.time() < deadline:
                st = client.get(
                    f"/api/v1/workspaces/{ws_id}/autopilot/status",
                    headers=headers,
                ).json()
                state = st["state"]
                if st["cycles_completed"] >= 1 and state in ("STOPPED",):
                    break
                time.sleep(1)
            assert state == "STOPPED"

            # The verify stage ran the vision provider on the real render
            # (the test video engine writes a real file path, not mock:).
            assert recording.calls, "vision provider never ran"
            assert recording.calls[0]["scene_count"] >= 1

            # QC verdicts were recorded with vision evidence in the notes.
            content = client.get(
                f"/api/v1/workspaces/{ws_id}/content", headers=headers
            ).json()
            assert content["total"] >= 1
    finally:
        set_vision_provider(original)


def test_quality_agent_evaluate_caps_components_on_broken_evidence():
    """Direct QualityAgent.evaluate: broken evidence caps the components."""
    from fastapi.testclient import TestClient

    from app.engine.agents.production import QualityAgent

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        email = f"qc-{os.urandom(4).hex()}@test.local"
        r = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "supersecret123"},
        )
        assert r.status_code == 200
        ws_id = r.json()["workspace"]["id"]

        agent = QualityAgent()
        script = "Nobody told you this about AI agents. Here is the truth. " \
                 "Three facts change everything you know about automation."

        # A clean analysis keeps scores high; a broken one caps them.
        class _Clean(VisionAnalysisProvider):
            name = "clean"
            def analyze(self, *, video_path, script, scene_count, expected_duration):
                return VisionAnalysis(
                    provider="clean", is_mock=False,
                    scenes=[SceneEvidence(index=0, visual_matches_script=True)],
                    audio_present=True, silent_sections=[],
                    subtitles_aligned=True, visual_relevance=90.0,
                    notes="clean",
                )

        ctx = SimpleNamespace(workspace_id=ws_id, job_id=None, cycle_id=None, type="test", payload={}, artifacts={})
        original = get_vision_provider()
        try:
            set_vision_provider(_Clean())
            clean = agent.evaluate(
                ctx, script=script, video_path="data/videos/ws/v.mp4",
            )
            set_vision_provider(RecordingProvider(_broken_analysis()))
            broken = agent.evaluate(
                ctx, script=script, video_path="data/videos/ws/v.mp4",
            )
            assert broken["components"]["audio"] < clean["components"]["audio"]
            assert broken["components"]["visual_relevance"] < clean["components"]["visual_relevance"]
            assert broken["components"]["captions"] < clean["components"]["captions"]
            assert "no audio track" in broken.get("notes", "")
        finally:
            set_vision_provider(original)


def test_mock_video_path_skips_vision_provider():
    """Mock renders never invoke the vision provider."""
    from fastapi.testclient import TestClient

    from app.engine.agents.production import QualityAgent

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        email = f"qc-{os.urandom(4).hex()}@test.local"
        r = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "supersecret123"},
        )
        assert r.status_code == 200
        ws_id = r.json()["workspace"]["id"]

        original = get_vision_provider()
        try:
            recording = RecordingProvider(_broken_analysis())
            set_vision_provider(recording)
            agent = QualityAgent()
            script = "Nobody told you this about AI agents. " \
                     "Three facts change everything you know."
            verdict = agent.evaluate(
                SimpleNamespace(workspace_id=ws_id, job_id=None, cycle_id=None, type="test", payload={}, artifacts={}),
                script=script, video_path="mock:mock-abc123/final-1.mp4",
            )
            assert recording.calls == []
            assert "[MOCK]" not in verdict.get("notes", "")
        finally:
            set_vision_provider(original)
