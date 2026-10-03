"""Audio enhancement: derived-only pipeline, denoise adapter, honest stages.

Contracts §16 rows owned by this file: ``audio derived-asset lineage`` and
``denoise adapter`` (Lane C), plus the §6 stage contract they depend on --
every stage reports ``applied | skipped | unavailable(reason) | failed(reason)``,
the before/after measurements are real, and no perceptual claim is made without
a measured basis.

The never-overwrite proof
-------------------------
``test_source_bytes_and_checksum_are_untouched`` hashes the source file before
and after a full ten-stage run and compares BOTH the sha256 and the raw bytes.
Lineage is then asserted in both places the repo records it: the additive
``parent_asset_id``/``derivation_json`` columns AND the conventional
``meta_json`` lineage dict.

Adapter isolation
-----------------
``rnnoise`` is ABSENT from this ffmpeg build and no python binding is
installed, so the RNNoise adapter's honest-unavailable path is the REAL CI path
and denoise falls through the registry chain to ``ffmpeg_enhancement``'s
``afftdn`` pass. The tests assert the fallback happened, that the reason says
so, and that the engine never names a backend directly.

Fast tests never touch ffmpeg media: the PCM helpers are exercised against WAVs
the test itself writes with ``wave``/``array``. Real lavfi media (loudness,
clipping, silence, duration) lives in the ``slow`` battery.
"""

from __future__ import annotations

import array
import hashlib
import math
import uuid
import wave
from pathlib import Path

import pytest

from app.engine.intel import audio_enhance as ae
from app.engine.intel import registry as intel_registry
from app.engine.intel.impl import ffmpeg_enhancement as fx
from app.engine.intel.impl import rnnoise_denoise as rnnoise
from app.services import storage as storage_service
from tests.media_intel_fixtures import (
    ffmpeg_filter_ok,
    fixture_paths_available,
    patterned_speechlike_wav,
)

requires_ffmpeg = pytest.mark.skipif(
    not fixture_paths_available(), reason="ffmpeg/ffprobe are required for real-media tests"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_pcm16(path: Path, samples: list[int], rate: int = 48_000) -> Path:
    """A real 16-bit PCM WAV from raw sample integers (stdlib only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(array.array("h", samples).tobytes())
    return path


def _quiet_bed_with_click(path: Path, *, tone_hz: float = 440.0, rate: int = 48_000,
                          click_at_s: float = 0.5) -> Path:
    """A QUIET steady bed with ONE 3-sample full-scale click.

    The bed sits at -34 dBFS (above the breath floor, below the click's crest)
    so the test has exactly one thing to find and no accidental breath windows:
    ground truth for the crest-ratio detector without any ffmpeg involvement.
    """
    samples: list[int] = []
    for index in range(rate):
        samples.append(int(0.02 * 32767 * math.sin(2 * math.pi * tone_hz * index / rate)))
    click_index = int(click_at_s * rate)
    for offset in range(3):
        samples[click_index + offset] = 32767 if offset % 2 == 0 else -32768
    return _write_pcm16(path, samples, rate=rate)


def _silent_pcm16(path: Path, *, seconds: float = 0.5, rate: int = 48_000) -> Path:
    return _write_pcm16(path, [0] * int(rate * seconds), rate=rate)


def _storage(tmp_path, monkeypatch) -> Path:
    """Point STORAGE_ROOT at a temp dir for the duration of one test."""
    root = tmp_path / "videos"
    monkeypatch.setattr(storage_service, "STORAGE_ROOT", root)
    return root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# stage vocabulary (contracts §6)
# ---------------------------------------------------------------------------


def test_stage_vocabulary_is_the_contract_list_in_order():
    assert ae.STAGES == (
        "denoise", "dereverb", "voice_isolation", "silence_detection",
        "filler_detection", "breath_click_detection", "loudness_normalization",
        "music_ducking", "compression", "limiting",
    )
    assert fx.STAGES == ae.STAGES
    assert ae.STAGE_STATUSES == ("applied", "skipped", "unavailable", "failed")


def test_filter_and_analysis_stages_partition_the_vocabulary():
    assert set(ae.STAGES) == fx.FILTER_STAGES | fx.ANALYSIS_STAGES
    assert not fx.FILTER_STAGES & fx.ANALYSIS_STAGES


# ---------------------------------------------------------------------------
# license honesty (contracts §1.4)
# ---------------------------------------------------------------------------


def test_ffmpeg_license_encodes_the_audit_verbatim():
    info = fx.FfmpegEnhancementProvider().license_info()
    assert info.commercial_use == "REVIEW_REQUIRED"
    assert info.code_license == "LGPL-2.1-or-later"
    assert info.model_license == "N/A"
    assert info.audited_on == "2026-09-29"
    assert "external process" in info.notes
    assert "GPL" in info.notes


def test_rnnoise_license_is_permitted_with_the_weights_by_inclusion_note():
    info = rnnoise.RnnoiseDenoiseProvider().license_info()
    assert info.code_license == "BSD-3-Clause"
    assert info.model_license == "BSD-3-Clause"
    assert info.commercial_use == "PERMITTED"
    assert info.model_gated is False
    assert info.audited_on == "2026-09-29"
    assert "rnn_data.c" in info.notes
    assert "COPYING" in info.notes


def test_both_adapters_report_cpu_only_requirements():
    for provider in (fx.FfmpegEnhancementProvider(), rnnoise.RnnoiseDenoiseProvider()):
        spec = provider.resource_requirements()
        assert spec.gpu is False
        cost = provider.cost(spec)
        assert cost["gpu_ms"] == 0
        assert cost["cost_micros"] == 0


# ---------------------------------------------------------------------------
# honest unavailability
# ---------------------------------------------------------------------------


def test_rnnoise_is_honestly_unavailable_and_says_what_takes_over():
    health = rnnoise.RnnoiseDenoiseProvider().health()
    assert health.available is False
    assert "not available" in health.reason.lower()
    assert "afftdn" in health.reason
    assert health.detail["fallback"] == "ffmpeg_enhancement (afftdn)"


def test_rnnoise_run_refuses_instead_of_silently_passing_audio_through():
    from app.engine.intel.base import IntelRequest, ProviderUnavailable

    provider = rnnoise.RnnoiseDenoiseProvider()
    with pytest.raises(ProviderUnavailable):
        provider.run(
            IntelRequest(workspace_id="w", asset_id="a", storage_path="in.wav",
                         params={"output_path": "out.wav"}),
            progress=lambda _f: None, should_cancel=lambda: False, deadline=None,
        )


@requires_ffmpeg
def test_dereverb_reports_unavailable_because_no_filter_exists():
    assert not ffmpeg_filter_ok("dereverb")
    support = fx.stage_support("dereverb")
    assert support["available"] is False
    assert support["method"] == ""
    assert "no de-reverberation method available" in support["reason"]


def test_filler_detection_is_owned_by_another_lane_and_never_reimplemented():
    support = fx.stage_support("filler_detection")
    assert support["available"] is False
    assert "silence/filler lane" in support["reason"]
    assert "edit_proposals" in support["reason"]
    # and the module has no filler heuristic of its own
    assert not hasattr(fx, "find_fillers")


@requires_ffmpeg
def test_health_is_available_and_reports_the_working_stage_map():
    health = fx.FfmpegEnhancementProvider().health()
    assert health.available is True
    assert health.mode == "subprocess"
    stages = health.detail["stages"]
    assert set(stages) == set(ae.STAGES)
    assert stages["dereverb"]["available"] is False
    assert stages["dereverb"]["reason"]
    assert stages["denoise"]["method"] in {"afftdn", "arnndn"}


def test_health_never_raises_and_carries_a_reason_without_ffmpeg(monkeypatch):
    fx.reset_probes()
    monkeypatch.setattr(fx, "ffmpeg_available", lambda: False)
    health = fx.FfmpegEnhancementProvider().health()
    assert health.available is False
    assert "ffmpeg is not on PATH" in health.reason
    assert fx.stage_support("denoise")["available"] is False
    assert fx.stage_support("loudness_normalization")["available"] is False
    fx.reset_probes()


# ---------------------------------------------------------------------------
# the denoise adapter (contracts §16 "denoise adapter")
# ---------------------------------------------------------------------------


def test_denoise_chain_prefers_the_neural_adapter_then_falls_back():
    assert intel_registry.chain_for("denoise") == ("rnnoise_denoise", "ffmpeg_enhancement")
    provider, reasons = intel_registry.resolve("denoise")
    assert provider is not None
    assert provider.key == "ffmpeg_enhancement"
    assert "afftdn" in reasons["rnnoise_denoise"]


@requires_ffmpeg
def test_denoise_records_afftdn_and_the_fallback_reason():
    support = fx.stage_support("denoise")
    assert support["available"] is True
    assert support["method"] == "afftdn"
    assert "arnndn" in support["reason"]  # and says why it was not used


@requires_ffmpeg
def test_denoise_prefers_arnndn_only_with_a_real_model_file(tmp_path):
    missing = fx.stage_support("denoise", {"denoise_model_path": str(tmp_path / "no.ffnn")})
    assert missing["method"] == "afftdn"
    model = tmp_path / "tiny.ffnn"
    model.write_bytes(b"not-a-real-model-but-present")
    with_model = fx.stage_support("denoise", {"denoise_model_path": str(model)})
    assert with_model["method"] == "arnndn"
    assert with_model["reason"].endswith("tiny.ffnn")


@requires_ffmpeg
def test_voice_isolation_is_labelled_band_isolation_not_neural():
    support = fx.stage_support("voice_isolation")
    assert support["available"] is True
    assert support["method"] == "band_isolation"
    assert support["neural"] is False
    assert "NOT neural" in support["reason"]


# ---------------------------------------------------------------------------
# adapter isolation
# ---------------------------------------------------------------------------


def test_engine_never_names_a_denoise_backend():
    """The engine must reach denoise through the registry CHAIN, not by import."""
    source = Path(ae.__file__).read_text(encoding="utf-8")
    assert "import rnnoise_denoise" not in source
    assert "from app.engine.intel.impl.rnnoise_denoise" not in source
    assert "import rnnoise" not in source
    assert "resolve_or_unavailable(self.denoise_capability)" in source
    # and the only provider module it touches is the shared ffmpeg helper
    assert "app.engine.intel.impl" not in source


def test_pipeline_resolves_providers_through_the_registry(monkeypatch, db_session):
    class _Double:
        key = "double"
        kind = "enhancement"

        def run(self, request, *, progress, should_cancel, deadline):  # pragma: no cover
            raise AssertionError("not used")

    seen: dict = {}

    def _resolve(kind, **kwargs):
        seen["kind"] = kind
        return _Double(), {}

    monkeypatch.setattr(intel_registry, "resolve_or_unavailable", _resolve)
    monkeypatch.setattr(ae, "safe_health", lambda p: type(
        "H", (), {"available": True, "reason": "", "to_dict": lambda s: {"available": True}}
    )())
    pipeline = ae.AudioEnhancementPipeline(db_session)
    provider, _reasons = pipeline.resolve()
    assert provider.key == "double"
    assert seen["kind"] == ae.CAPABILITY
    provider, _reasons = pipeline.resolve_denoise()
    assert seen["kind"] == "denoise"


# ---------------------------------------------------------------------------
# stage status honesty
# ---------------------------------------------------------------------------


def test_unknown_stage_is_skipped_with_a_reason_never_ignored():
    enabled, skipped = ae.normalise_stages(["denoise", "not_a_stage", "dereverb"])
    assert enabled == ["denoise", "dereverb"]  # contract order, deduplicated
    assert [s["status"] for s in skipped] == ["skipped"]
    assert "unknown stage" in skipped[0]["reason"]


def test_negative_stage_status_requires_a_reason():
    for status in ("skipped", "unavailable", "failed"):
        with pytest.raises(ValueError):
            ae._stage_record("denoise", status, "")
    record = ae._stage_record("dereverb", "unavailable", "no such filter")
    assert record["status"] == "unavailable"
    assert record["reason"] == "no such filter"


def test_stage_matrix_orders_rows_by_the_contract():
    enabled, _ = ae.normalise_stages(["limiting", "denoise", "breath_click_detection"])
    ordered = sorted(enabled, key=ae.STAGES.index)
    assert ordered == ["denoise", "breath_click_detection", "limiting"]


# ---------------------------------------------------------------------------
# stdlib PCM helpers (no numpy, no soundfile)
# ---------------------------------------------------------------------------


def test_count_clipped_samples_measures_a_real_wav(tmp_path):
    path = _write_pcm16(tmp_path / "clip.wav", [0, 32767, -32768, 100, 32767])
    assert ae.count_clipped_samples(path) == 3
    assert ae.count_clipped_samples(_write_pcm16(tmp_path / "ok.wav", [0, 1, -1, 2])) == 0


def test_count_clipped_samples_is_none_when_it_cannot_measure(tmp_path):
    not_audio = tmp_path / "notes.txt"
    not_audio.write_text("not audio", encoding="utf-8")
    assert ae.count_clipped_samples(not_audio) is None
    assert ae.count_clipped_samples(tmp_path / "missing.wav") is None


def test_read_pcm16_reports_why_a_non_pcm_file_is_unusable(tmp_path):
    not_audio = tmp_path / "clip.mp3"
    not_audio.write_bytes(b"\x00\x01\x02")
    info = ae.read_pcm16(not_audio)
    assert info["available"] is False
    assert info["reason"]
    assert info["samples"] == []


def test_breath_click_finds_a_synthetic_click_and_labels_itself(tmp_path):
    analysis = ae.detect_breath_click(_quiet_bed_with_click(tmp_path / "click.wav"))
    assert analysis["available"] is True
    assert analysis["method"] == "heuristic_pcm"
    assert analysis["confidence"] == ae.CONFIDENCE_HEURISTIC  # never "high"
    assert analysis["basis"]
    clicks = [e for e in analysis["events"] if e["kind"] == "click"]
    assert len(clicks) == 1
    assert clicks[0]["start_s"] == pytest.approx(0.5, abs=0.05)
    assert clicks[0]["level_dbfs"] is not None


def test_breath_click_reports_no_events_for_a_steady_tone(tmp_path):
    tone = _write_pcm16(
        tmp_path / "tone.wav",
        [int(0.25 * 32767 * math.sin(2 * math.pi * 440 * i / 48_000)) for i in range(48_000)],
    )
    analysis = ae.detect_breath_click(tone)
    assert analysis["available"] is True
    assert analysis["events"] == []


def test_breath_click_is_honest_about_unreadable_input(tmp_path):
    not_audio = tmp_path / "clip.mp3"
    not_audio.write_bytes(b"\x00\x01\x02")
    analysis = ae.detect_breath_click(not_audio)
    assert analysis["available"] is False
    assert analysis["reason"]
    assert analysis["events"] == []


def test_breath_click_handles_digital_silence_without_dividing_by_zero(tmp_path):
    analysis = ae.detect_breath_click(_silent_pcm16(tmp_path / "silence.wav"))
    assert analysis["available"] is True
    assert analysis["events"] == []


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def test_measure_audio_never_invents_a_number(tmp_path):
    not_audio = tmp_path / "nope.wav"
    not_audio.write_bytes(b"\x00")
    measured = ae.measure_audio(not_audio)
    assert measured["integrated_lufs"] is None
    assert measured["clipped_samples"] is None
    assert measured["pcm_readable"] is False
    assert measured["pcm_reason"]


def test_measure_audio_on_a_real_wav(tmp_path):
    path = _write_pcm16(tmp_path / "x.wav", [0, 16384, -16384] * 16_000)
    measured = ae.measure_audio(path)
    assert measured["pcm_readable"] is True
    assert measured["sample_rate"] == 48_000
    assert measured["clipped_samples"] == 0
    assert measured["peak_dbfs"] == pytest.approx(-6.0, abs=0.1)


def test_before_after_keeps_deltas_none_without_an_output():
    before = {"peak_dbfs": -6.0, "clipped_samples": 2}
    block = ae.before_after(before, None)
    assert block["measured"] is False
    assert block["deltas"] == {}
    with_output = ae.before_after(before, {"peak_dbfs": -3.0, "clipped_samples": 0})
    assert with_output["measured"] is True
    assert with_output["deltas"]["peak_dbfs"] == 3.0
    assert with_output["deltas"]["clipped_samples"] == -2


# ---------------------------------------------------------------------------
# claims discipline (contracts §6: no claim without a measured basis)
# ---------------------------------------------------------------------------


def test_no_claim_without_a_measurement():
    stages = [{"stage": "loudness_normalization", "status": "applied"}]
    assert ae._build_claims({"integrated_lufs": -20.0}, None, stages) == []


def test_loudness_claim_requires_moving_toward_the_target():
    toward = [{"stage": "loudness_normalization", "status": "applied"}]
    closer = ae._build_claims({"integrated_lufs": -30.0}, {"integrated_lufs": -16.0},
                              toward, {"target_lufs": -14.0})
    assert len(closer) == 1
    assert closer[0]["metric"] == "integrated_lufs"
    assert closer[0]["measured"]["target"] == -14.0

    away = ae._build_claims({"integrated_lufs": -14.5}, {"integrated_lufs": -30.0},
                            toward, {"target_lufs": -14.0})
    assert away == []


def test_clipping_claim_needs_both_counts():
    stages = [{"stage": "limiting", "status": "applied"}]
    claims = ae._build_claims({"clipped_samples": 10}, {"clipped_samples": 0}, stages)
    assert [c["metric"] for c in claims] == ["clipped_samples"]
    # unchanged count is not a finding
    assert ae._build_claims({"clipped_samples": 0}, {"clipped_samples": 0}, stages) == []


def test_no_stage_means_no_claims_even_with_measurements():
    stages = [{"stage": "denoise", "status": "unavailable"}]
    claims = ae._build_claims({"rms_dbfs": -20.0}, {"rms_dbfs": -18.0}, stages)
    assert claims == []


# ---------------------------------------------------------------------------
# derived-only guarantees
# ---------------------------------------------------------------------------


def test_derived_key_is_inside_workspace_storage_and_unique_per_run():
    key_a = ae.derived_filename("clips/talk.wav", "aaaa1111-bbbb-2222")
    key_b = ae.derived_filename("clips/talk.wav", "cccc3333-dddd-4444")
    assert key_a == "talk_aaaa1111.wav"
    assert key_a != key_b
    assert "/" not in key_a and ".." not in key_a
    assert key_a != Path("clips/talk.wav").name  # never the source filename


def test_derived_filename_sanitises_the_source_stem():
    # a traversal attempt collapses to its last component: no "..", no separator
    name = ae.derived_filename("../../etc/passwd", "0123456789abcdef")
    assert name == "passwd_01234567.wav"
    assert ".." not in name and "/" not in name
    assert ae.derived_filename("weird name!!.mp3", "0123456789abcdef") == \
        "weird_name___01234567.mp3"
    assert ae.derived_filename("noext", "0123456789abcdef").endswith(".wav")


def test_pipeline_refuses_a_derived_path_equal_to_the_source(tmp_path, monkeypatch):
    _storage(tmp_path, monkeypatch)
    with pytest.raises(ae.AudioEnhancementError):
        ae._guard_derived_path("w1", "src.wav", "src.wav")


def test_pipeline_refuses_a_path_outside_workspace_storage(tmp_path, monkeypatch):
    _storage(tmp_path, monkeypatch)
    for key in ("../other-ws/src.wav", "/etc/passwd", "", "  "):
        with pytest.raises(ae.AudioEnhancementError):
            ae._guard_derived_path("w1", key, "src.wav")


def test_shared_ffmpeg_helper_refuses_dst_equal_src(tmp_path):
    from app.engine.intel.ffmpeg_util import run_filter

    path = tmp_path / "same.wav"
    path.write_bytes(b"\x00")
    with pytest.raises(ValueError, match="derived-only"):
        run_filter(path, path, "anull")


def test_provider_publish_refuses_to_overwrite_the_source(tmp_path):
    source = tmp_path / "src.wav"
    source.write_bytes(b"\x00")
    with pytest.raises(ValueError, match="over its source"):
        fx._publish(source, source, source)


def test_pipeline_guard_accepts_a_normal_derived_path(tmp_path, monkeypatch):
    root = _storage(tmp_path, monkeypatch)
    key = f"{ae.DERIVED_PREFIX}/run/src_dead.wav"
    resolved = ae._guard_derived_path("w1", key, "src.wav")
    assert resolved == root / "w1" / key


# ---------------------------------------------------------------------------
# emission discipline (contracts §3)
# ---------------------------------------------------------------------------


def test_engine_never_calls_record_event_or_track_cost():
    """The engine RETURNS its events; the route publishes them after commit."""
    source = Path(ae.__file__).read_text(encoding="utf-8")
    assert "record_event(" not in source
    assert "track_cost(" not in source
    assert "session_scope" not in source
    assert "from app.services.events" not in source
    assert "from app.services.cost" not in source


def test_router_emits_only_after_commit():
    source = Path(ae.__file__).parent.parent.parent / "api" / "v1" / "media_intel_audio.py"
    text = source.read_text(encoding="utf-8")
    commit_at = text.index("db.commit()")
    emit_at = text.index("_emit_events(")
    assert commit_at < emit_at
    assert text.count("_emit_events(") == 2  # definition + the one call site


def test_reported_event_kinds_are_declared():
    assert ae.EVENTS == (
        "MEDIA_INTEL_AUDIO_ENHANCE_COMPLETED",
        "MEDIA_INTEL_AUDIO_ENHANCE_PARTIAL",
    )


def test_job_kind_is_the_contract_vocabulary():
    from app.engine.intel import jobs as intel_jobs

    assert ae.JOB_KIND == intel_jobs.JOB_ENHANCE
    assert intel_jobs.JOB_KIND_FOR["enhancement"] == intel_jobs.JOB_ENHANCE
    assert intel_jobs.JOB_KIND_FOR["denoise"] == intel_jobs.JOB_ENHANCE


@requires_ffmpeg
@pytest.mark.slow
def test_manifest_exposes_the_measurements_the_qc_lane_reads(
        db_session, workspace_with_user, tmp_path, monkeypatch):
    """QC reads the run's OWN metrics; it must be able to find them."""
    from app.engine.intel.qc import _flatten_metrics
    from tests.media_intel_fixtures import clipped_tone_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "clip.wav"
    clipped_tone_wav(source)
    asset = _asset(db_session, ws.id, "clip.wav", _sha256(source), 2.0)

    payload = ae.enhance(db_session, ws, asset, stages=["loudness_normalization"],
                         params={"target_lufs": -14.0},
                         requested_by=workspace_with_user["user"])
    manifest = payload["manifest"]
    assert manifest["source"]["clipped_samples"] == 35_680
    assert manifest["output"]["integrated_lufs"] is not None

    flat = _flatten_metrics(manifest)
    assert flat["source_duration_s"] == 2.0
    assert flat["output_duration_s"] == 2.0
    assert flat["source_peak_dbfs"] == pytest.approx(0.0, abs=0.01)
    assert flat["output_clipped_samples"] == 0
    assert flat["output_true_peak_dbfs"] <= -1.0


# ---------------------------------------------------------------------------
# API surface (contracts §14)
# ---------------------------------------------------------------------------


def test_router_is_mounted_where_the_contract_says():
    from app.api.v1.media_intel_audio import media_intel_audio_router

    assert media_intel_audio_router.prefix == "/workspaces/{workspace_id}/media-intel"
    assert "media-intel-audio" in media_intel_audio_router.tags
    routes = {(r.path, tuple(sorted(r.methods))) for r in media_intel_audio_router.routes}
    assert ("/workspaces/{workspace_id}/media-intel/audio/enhance", ("POST",)) in routes
    assert ("/workspaces/{workspace_id}/media-intel/audio/enhance/{run_id}", ("GET",)) in routes
    # the silence/filler routes belong to another lane
    assert not any("silence" in path or "filler" in path for path, _ in routes)


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    _storage(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    from app.api.v1.media_intel_audio import media_intel_audio_router
    from app.main import create_app

    app = create_app()
    app.include_router(media_intel_audio_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _register(client):
    r = client.post("/api/v1/auth/register",
                    json={"email": f"c{uuid.uuid4().hex[:8]}@test.local",
                          "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], data["user"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"}


def _make_member(client, ws_id, role):
    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"{role}{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role))
    token = client.post("/api/v1/auth/login",
                        json={"email": email, "password": "supersecret123"})
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


def test_enhance_route_requires_authentication(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/v1/workspaces/w1/media-intel/audio/enhance",
                      json={"asset_id": "a", "stages": ["denoise"]}).status_code == 401


def test_foreign_asset_is_404_not_403(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _user, headers = _register(client)
    other_ws, _u2, _h2 = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/audio/enhance",
                    headers=headers,
                    json={"asset_id": "00000000-0000-0000-0000-000000000000",
                          "stages": ["denoise"]})
    assert r.status_code == 404
    assert r.json()["detail"] == "asset not found"
    assert other_ws != ws_id


def test_enhance_route_requires_a_stage(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import MediaAsset

    client = _client(tmp_path, monkeypatch)
    ws_id, _user, headers = _register(client)
    with session_scope() as s:
        asset = MediaAsset(workspace_id=ws_id, type="audio", storage_key="src.wav")
        s.add(asset)
        s.commit()
        asset_id = asset.id
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/audio/enhance",
                    headers=headers, json={"asset_id": asset_id, "stages": []})
    assert r.status_code == 422
    assert "no stages requested" in r.json()["detail"]


def test_viewer_cannot_enhance(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import MediaAsset

    client = _client(tmp_path, monkeypatch)
    ws_id, _user, _owner_headers = _register(client)
    with session_scope() as s:
        asset = MediaAsset(workspace_id=ws_id, type="audio", storage_key="src.wav")
        s.add(asset)
        s.commit()
        asset_id = asset.id
    viewer = _make_member(client, ws_id, "viewer")
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/audio/enhance",
                    headers=viewer, json={"asset_id": asset_id, "stages": ["denoise"]})
    assert r.status_code == 403


def test_get_run_is_404_for_another_workspace(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _user, headers = _register(client)
    _other_ws, _u2, _h2 = _register(client)
    r = client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/audio/enhance/"
        f"00000000-0000-0000-0000-000000000000", headers=headers)
    assert r.status_code == 404
    assert r.json()["detail"] == "run not found"


# ---------------------------------------------------------------------------
# SLOW: real lavfi media through the whole pipeline
# ---------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.slow
def test_source_bytes_and_checksum_are_untouched(db_session, workspace_with_user,
                                                 tmp_path, monkeypatch):
    """The never-overwrite proof: a full ten-stage run changes NOTHING upstream."""
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    before_bytes = source.read_bytes()
    before_sha = _sha256(source)

    asset = _asset(db_session, ws.id, "src.wav", before_sha, 6.0)
    payload = ae.enhance(
        db_session, ws, asset,
        stages=list(ae.STAGES), requested_by=workspace_with_user["user"],
    )

    assert payload["run"]["status"] == "COMPLETED"
    assert payload["run"]["output_asset_id"]
    assert source.read_bytes() == before_bytes
    assert _sha256(source) == before_sha
    db_session.commit()


@requires_ffmpeg
@pytest.mark.slow
def test_derived_asset_carries_lineage_in_both_conventions(
        db_session, workspace_with_user, tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    asset = _asset(db_session, ws.id, "src.wav", _sha256(source), 6.0)

    payload = ae.enhance(db_session, ws, asset, stages=["loudness_normalization"],
                         requested_by=workspace_with_user["user"])
    run = payload["run"]
    db_session.refresh(asset)
    db_session.commit()

    from app.models import MediaAsset

    derived = db_session.get(MediaAsset, run["output_asset_id"])
    assert derived is not None
    assert derived.id != asset.id
    assert derived.parent_asset_id == asset.id
    assert derived.workspace_id == ws.id
    assert derived.origin == "generated"
    assert derived.type == asset.type
    assert derived.provider == "ffmpeg_enhancement"
    assert derived.checksum and derived.file_size
    assert ae.DERIVED_PREFIX in derived.storage_key
    assert storage_service.validate_storage_key(ws.id, derived.storage_key) == \
        derived.storage_key

    # additive lineage column
    derivation = dict(derived.derivation_json or {})
    assert derivation["source_asset_id"] == asset.id
    assert derivation["run_id"] == run["id"]
    assert derivation["provider"] == "ffmpeg_enhancement"
    assert [s["stage"] for s in derivation["stages"]] == ["loudness_normalization"]
    assert derivation["stages"][0]["method"] == "loudnorm_two_pass"
    assert derivation["quality"]["before"]["integrated_lufs"] is not None
    assert derivation["quality"]["after"]["integrated_lufs"] is not None

    # conventional meta_json lineage dict
    lineage = dict(derived.meta_json or {}).get("lineage") or {}
    assert lineage["source_asset_id"] == asset.id
    assert lineage["run_id"] == run["id"]
    assert lineage["provider"] == "ffmpeg_enhancement"

    # the SOURCE row is untouched: same key, same checksum, no parent
    db_session.refresh(asset)
    assert asset.parent_asset_id is None
    assert asset.storage_key == "src.wav"
    assert asset.derivation_json in (None, {})


@requires_ffmpeg
@pytest.mark.slow
def test_denoise_falls_back_to_afftdn_and_says_why(db_session, workspace_with_user,
                                                   tmp_path, monkeypatch):
    from tests.media_intel_fixtures import noisy_tone_wav, wav_peak_dbfs

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "noisy.wav"
    noisy_tone_wav(source, seconds=3.0)
    peak_before = wav_peak_dbfs(source)
    asset = _asset(db_session, ws.id, "noisy.wav", _sha256(source), 3.0)

    payload = ae.enhance(db_session, ws, asset, stages=["denoise"],
                         requested_by=workspace_with_user["user"])
    stages = {s["stage"]: s for s in payload["stages"]}
    assert stages["denoise"]["status"] == "applied"
    assert stages["denoise"]["method"] == "afftdn"          # the real fallback
    assert stages["denoise"]["neural"] is False
    assert payload["run"]["provider_key"] == "ffmpeg_enhancement"
    manifest = payload["manifest"]
    assert manifest["license"]["commercial_use"] == "REVIEW_REQUIRED"

    from app.models import MediaAsset

    derived = db_session.get(MediaAsset, payload["run"]["output_asset_id"])
    derived_path = storage_service.managed_path(ws.id, str(root / ws.id / derived.storage_key))
    assert wav_peak_dbfs(derived_path) < peak_before  # real spectral work happened
    assert payload["before_after"]["measured"] is True


@requires_ffmpeg
@pytest.mark.slow
def test_loudness_normalization_moves_measured_lufs_toward_the_target(
        db_session, workspace_with_user, tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    asset = _asset(db_session, ws.id, "src.wav", _sha256(source), 6.0)

    payload = ae.enhance(db_session, ws, asset, stages=["loudness_normalization"],
                         params={"target_lufs": -14.0},
                         requested_by=workspace_with_user["user"])
    before = payload["before_after"]["before"]
    after = payload["before_after"]["after"]
    assert before["integrated_lufs"] is not None
    assert abs(after["integrated_lufs"] - (-14.0)) <= 1.0
    assert abs(after["integrated_lufs"] - (-14.0)) < abs(before["integrated_lufs"] + 14.0)
    claims = [c for c in payload["claims"] if c["metric"] == "integrated_lufs"]
    assert claims and claims[0]["measured"]["target"] == -14.0


@requires_ffmpeg
@pytest.mark.slow
def test_clipping_and_duration_are_measured_on_real_media(
        db_session, workspace_with_user, tmp_path, monkeypatch):
    from tests.media_intel_fixtures import clipped_tone_wav, wav_clipped_samples, wav_peak_dbfs

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "clip.wav"
    clipped_tone_wav(source)  # peak 0.0 dBFS, 35 680 full-scale samples, 2.0 s
    assert wav_clipped_samples(source) == 35_680
    asset = _asset(db_session, ws.id, "clip.wav", _sha256(source), 2.0)

    payload = ae.enhance(db_session, ws, asset,
                         stages=["loudness_normalization", "limiting"],
                         params={"target_lufs": -14.0, "true_peak_limit_db": -1.0},
                         requested_by=workspace_with_user["user"])
    before = payload["before_after"]["before"]
    after = payload["before_after"]["after"]

    assert before["clipped_samples"] == 35_680
    assert after["clipped_samples"] == 0
    assert wav_peak_dbfs(source) == pytest.approx(0.0, abs=0.01)  # source untouched
    # duration preserved within the declared tolerance
    assert abs(after["duration_s"] - before["duration_s"]) <= ae.DURATION_TOLERANCE_S
    # the limiter's true-peak ceiling is a MEASURED bound
    bound = payload["manifest"]["true_peak_limit"]
    assert bound["requested"] is True
    assert bound["measured"] is True
    assert bound["exceeded"] is False
    assert after["true_peak_dbfs"] <= -1.0 + 0.5


@requires_ffmpeg
@pytest.mark.slow
def test_latency_and_processing_time_are_recorded(db_session, workspace_with_user,
                                                  tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    asset = _asset(db_session, ws.id, "src.wav", _sha256(source), 6.0)

    payload = ae.enhance(db_session, ws, asset,
                         stages=["denoise", "loudness_normalization", "limiting"],
                         requested_by=workspace_with_user["user"])
    run = payload["run"]
    assert run["processing_ms"] > 0
    assert run["gpu_ms"] == 0
    assert payload["manifest"]["processing_ms"] == run["processing_ms"]
    assert payload["manifest"]["cost"]["cpu_ms"] > 0
    per_stage = [s["processing_ms"] for s in payload["stages"] if s["status"] == "applied"]
    assert per_stage and all(value >= 0 for value in per_stage)
    assert sum(per_stage) <= run["processing_ms"] + 1


@requires_ffmpeg
@pytest.mark.slow
def test_one_stage_changes_only_its_own_row(db_session, workspace_with_user,
                                            tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    asset = _asset(db_session, ws.id, "src.wav", _sha256(source), 6.0)

    denoise_only = ae.enhance(db_session, ws, asset, stages=["denoise"],
                              requested_by=workspace_with_user["user"])
    both = ae.enhance(db_session, ws, asset, stages=["denoise", "limiting"],
                      requested_by=workspace_with_user["user"])

    first = {s["stage"]: s["status"] for s in denoise_only["stages"]}
    second = {s["stage"]: s["status"] for s in both["stages"]}
    assert first == {"denoise": "applied"}
    assert second == {"denoise": "applied", "limiting": "applied"}
    # the shared stage kept its own recorded method and status
    denoise_rows = [s for s in both["stages"] if s["stage"] == "denoise"]
    assert denoise_rows[0]["method"] == "afftdn"
    assert denoise_rows[0]["status"] == "applied"


@requires_ffmpeg
@pytest.mark.slow
def test_cache_reuse_returns_the_prior_run_without_recompute(
        db_session, workspace_with_user, tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    asset = _asset(db_session, ws.id, "src.wav", _sha256(source), 6.0)

    first = ae.enhance(db_session, ws, asset, stages=["limiting"],
                       requested_by=workspace_with_user["user"])
    db_session.commit()
    second = ae.enhance(db_session, ws, asset, stages=["limiting"],
                        requested_by=workspace_with_user["user"])
    db_session.commit()

    assert second["cache_hit"] is True
    assert second["run"]["id"] == first["run"]["id"]
    assert second["run"]["output_asset_id"] == first["run"]["output_asset_id"]
    assert second["events"] == []

    forced = ae.enhance(db_session, ws, asset, stages=["limiting"], force=True,
                        requested_by=workspace_with_user["user"])
    db_session.commit()
    assert forced["cache_hit"] is False
    assert forced["run"]["id"] != first["run"]["id"]


@requires_ffmpeg
@pytest.mark.slow
def test_silence_detection_measures_real_ranges(db_session, workspace_with_user,
                                                tmp_path, monkeypatch):
    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "speech.wav"
    patterned_speechlike_wav(source)  # 8.0 s with one 2.0 s gap at 3.0 s
    asset = _asset(db_session, ws.id, "speech.wav", _sha256(source), 8.0)

    payload = ae.enhance(db_session, ws, asset, stages=["silence_detection"],
                         requested_by=workspace_with_user["user"])
    assert payload["run"]["status"] == "COMPLETED"
    assert payload["run"]["output_asset_id"] is None  # analysis only: no derived media
    silence = payload["analysis"]["silence_detection"]
    assert silence["method"] == "silencedetect"
    assert silence["range_count"] == 1
    assert silence["ranges"][0]["start_s"] == pytest.approx(3.0, abs=0.05)
    assert silence["total_silence_s"] == pytest.approx(2.0, abs=0.05)
    assert payload["claims"] == []  # no transform ran, so no claim is made


@requires_ffmpeg
@pytest.mark.slow
def test_derived_media_never_escapes_the_workspace(db_session, workspace_with_user,
                                                  tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    root = _storage(tmp_path, monkeypatch)
    ws = _workspace(db_session, workspace_with_user)
    media_dir = root / ws.id
    media_dir.mkdir(parents=True, exist_ok=True)
    source = media_dir / "src.wav"
    loud_ripple_wav(source, seconds=6.0)
    asset = _asset(db_session, ws.id, "src.wav", _sha256(source), 6.0)

    payload = ae.enhance(db_session, ws, asset, stages=["limiting"],
                         requested_by=workspace_with_user["user"])
    from app.models import MediaAsset

    derived = db_session.get(MediaAsset, payload["run"]["output_asset_id"])
    absolute = storage_service.managed_path(ws.id, str(root / ws.id / derived.storage_key))
    assert absolute is not None
    assert absolute.is_file()
    assert str(absolute).startswith(str((root / ws.id).resolve()))


@requires_ffmpeg
@pytest.mark.slow
def test_enhance_route_end_to_end(tmp_path, monkeypatch):
    from tests.media_intel_fixtures import loud_ripple_wav

    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)

    from app.db import session_scope
    from app.models import MediaAsset

    with session_scope() as s:
        media_dir = storage_service.STORAGE_ROOT / ws_id
        media_dir.mkdir(parents=True, exist_ok=True)
        source = media_dir / "src.wav"
        loud_ripple_wav(source, seconds=6.0)
        before = _sha256(source)
        asset = MediaAsset(workspace_id=ws_id, type="audio", origin="upload",
                           storage_key="src.wav", mime_type="audio/wav",
                           checksum=before, duration_seconds=6.0)
        s.add(asset)
        s.commit()
        asset_id = asset.id

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/audio/enhance",
                    headers=headers,
                    json={"asset_id": asset_id,
                          "stages": ["denoise", "dereverb", "loudness_normalization"]})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["run"]["status"] == "COMPLETED"
    assert payload["run"]["output_asset_id"]
    stages = {s["stage"]: s for s in payload["stages"]}
    assert stages["denoise"]["status"] == "applied"
    assert stages["dereverb"]["status"] == "unavailable"
    assert stages["dereverb"]["reason"]
    assert payload["events_published"] == ["MEDIA_INTEL_AUDIO_ENHANCE_COMPLETED"]
    assert payload["stages_supported"] == list(ae.STAGES)
    assert payload["before_after"]["measured"] is True

    # the source file is byte-identical after the HTTP round trip
    assert _sha256(storage_service.STORAGE_ROOT / ws_id / "src.wav") == before

    detail = client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/audio/enhance/{payload['run']['id']}",
        headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["run"]["id"] == payload["run"]["id"]
    assert {s["stage"] for s in body["stages"]} == {s["stage"] for s in payload["stages"]}
    assert {s["stage"]: s["status"] for s in body["stages"]} == \
        {s["stage"]: s["status"] for s in payload["stages"]}
    assert body["manifest"]["license"]["commercial_use"] == "REVIEW_REQUIRED"
    assert body["run"]["requested_by"] == user_id


# ---------------------------------------------------------------------------
# shared fixtures used by this file
# ---------------------------------------------------------------------------


def _workspace(db, workspace_with_user):
    from app.models import Workspace

    return db.get(Workspace, workspace_with_user["workspace"])


def _asset(db, workspace_id: str, storage_key: str, checksum: str, duration: float):
    from app.models import MediaAsset

    row = MediaAsset(workspace_id=workspace_id, type="audio", origin="upload",
                     storage_key=storage_key, mime_type="audio/wav",
                     checksum=checksum, duration_seconds=duration)
    db.add(row)
    db.flush()
    return row
