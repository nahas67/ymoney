"""Provider-contract gates for media intelligence (Work 12 Lane A).

Contracts §16 rows owned by this file: provider unavailable, cache reuse,
license metadata honesty, boot without ML packages -- plus the registry
fallback/commercial-refusal rules of contracts §1.1/§1.4.

The environment under test is the CI venv: NO ML packages at all. Every
assertion below is therefore an assertion about the honest-unavailable path,
which is the whole point of the provider layer (contracts §0).
"""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from app.engine.intel import base as intel_base
from app.engine.intel import factory as intel_factory
from app.engine.intel import registry as intel_registry

#: packages an implementation might import; none may be present in CI
ML_PACKAGES: tuple[str, ...] = (
    "torch", "whisperx", "mediapipe", "sam2", "rnnoise", "pyannote",
    "numpy", "cv2", "librosa",
)


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# fakes: real ABC subclasses injected through the real lazy-import path
# ---------------------------------------------------------------------------


class FakeProvider(intel_base.MediaIntelProvider):
    """A provider whose every answer comes from CLASS attributes.

    The registry instantiates a provider class with no arguments, so a fake is
    configured by building a subclass -- see :func:`make_fake`. ``key``/``kind``
    are plain class attributes, which satisfies the ABC's abstract properties.
    """

    key = "fake"
    kind = "alignment"
    kinds = ()
    _available = True
    _reason = ""
    _commercial_use = "UNVERIFIED"
    _audited_on = ""
    _health_raises = False
    _capabilities_raises = False
    _license_raises = False

    def health(self):
        if self._health_raises:
            raise RuntimeError("probe exploded")
        return intel_base.ProviderHealth(
            available=self._available,
            reason=self._reason if not self._available else "",
            version="fake-1",
            mode=intel_base.MODE_LOCAL,
            detail={"fake": True},
        )

    def capabilities(self):
        if self._capabilities_raises:
            raise RuntimeError("capabilities exploded")
        return {"stages": ["analyse"], "fake": True}

    def resource_requirements(self):
        return intel_base.ResourceSpec(gpu=False, ram_mb=64)

    def license_info(self):
        if self._license_raises:
            raise RuntimeError("license exploded")
        return intel_base.LicenseInfo(
            code_license="MIT",
            code_license_url="https://example.invalid/mit",
            model_license="SEE_MODEL_CARD",
            commercial_use=self._commercial_use,
            audited_on=self._audited_on,
        )

    def run(self, request, *, progress, should_cancel, deadline):
        progress(0.5)
        return intel_base.ProviderResult(ok=True, artifacts={}, metrics={}, warnings=[])

    def cost(self, spec):
        return {"gpu_ms": 0, "cpu_ms": 1, "cost_micros": 0}


def make_fake(**attrs) -> type:
    """A configured FakeProvider subclass (the registry instantiates no-arg)."""
    base_attrs = {
        "key": "fake",
        "kind": "alignment",
        "kinds": (),
        "_available": True,
        "_reason": "",
        "_commercial_use": "UNVERIFIED",
        "_audited_on": "",
        "_health_raises": False,
        "_capabilities_raises": False,
        "_license_raises": False,
    }
    base_attrs.update(attrs)
    return type("ConfiguredFake", (FakeProvider,), base_attrs)


def _install_fake(monkeypatch, name: str, provider_class: type) -> None:
    """Expose a fake class as ``impl.<name>`` and reset the instance cache."""
    module = types.ModuleType("app.engine.intel.impl." + name)
    module.PROVIDER = provider_class
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(intel_registry, "_instances", {})


def _chain(monkeypatch, **chains) -> None:
    monkeypatch.setattr(intel_registry, "PROVIDER_CHAINS", dict(chains))


# ---------------------------------------------------------------------------
# boot without models
# ---------------------------------------------------------------------------


def test_ci_env_has_no_ml_packages():
    """contracts §1.3: the CI venv installs none of the optional backends."""
    present = [name for name in ML_PACKAGES if _installed(name)]
    assert present == [], f"ML packages present, unavailable-path tests void: {present}"


def test_app_imports_and_openapi_probe_without_ml_packages():
    """``from app.main import app`` must work in a venv with zero ML packages."""
    from app.main import app

    paths = app.openapi()["paths"]
    assert len(paths) > 300
    assert "/api/v1/system/readiness" in paths


MODEL_BACKED_KEYS = frozenset({
    "whisperx_alignment", "pyannote_diarization", "mediapipe_faces", "sam2_segmentation",
})


def test_every_provider_reports_unavailable_with_a_reason():
    """No backend installed => available=False AND a non-empty reason."""
    intel_registry.clear_cache()
    items = intel_registry.list_providers()
    assert items, "registry must expose its chains"
    for item in items:
        health = item["health"]
        if item["key"] in MODEL_BACKED_KEYS:
            assert health["available"] is False, item["key"]
        if health["available"]:
            # A healthy local provider advertises real capabilities (an
            # `available` key is only meaningful on the unavailable branch).
            assert item["capabilities"], item["key"]
            assert item["license"]["commercial_use"], item["key"]
            continue
        assert health["reason"].strip(), f"{item['key']} unavailable without a reason"
        assert health["detail"].get("remediation"), f"{item['key']} without remediation"
        assert item["capabilities"].get("available") is False


def test_model_backed_kinds_do_not_resolve_without_models():
    """No ML packages installed => these kinds resolve to nothing, and every
    provider skipped along the way carries a reason."""
    intel_registry.clear_cache()
    for kind in ("alignment", "diarization", "face_tracking", "segmentation"):
        provider, reasons = intel_registry.resolve(kind)
        assert provider is None, f"{kind} resolved to {provider} with no model installed"
        assert reasons, f"{kind} resolved to None without any reason"
        for key, why in reasons.items():
            assert why.strip(), f"{kind}/{key} skipped without a reason"


def test_resolve_returns_none_plus_a_reason_for_every_kind():
    intel_registry.clear_cache()
    assert set(intel_registry.CAPABILITY_KINDS) >= {
        "alignment", "diarization", "speech_activity", "enhancement", "denoise",
        "face_tracking", "segmentation", "active_speaker", "reframe",
    }
    for kind in intel_registry.CAPABILITY_KINDS:
        provider, reasons = intel_registry.resolve(kind)
        if provider is not None:
            # A local ffmpeg/math provider may legitimately resolve here; when it
            # does, it must be healthy and reasons must still be a mapping.
            assert provider.health().available, f"{kind} resolved to an unhealthy provider"
            assert isinstance(reasons, dict), kind
            continue
        assert reasons, f"{kind} resolved to None without any reason"
        for key, why in reasons.items():
            assert why.strip(), f"{kind}/{key} skipped without a reason"


def test_unknown_kind_resolves_to_nothing():
    provider, reasons = intel_registry.resolve("does_not_exist")
    assert provider is None
    assert reasons == {}


# ---------------------------------------------------------------------------
# health never raises
# ---------------------------------------------------------------------------


def test_health_never_raises_and_reports_the_failure():
    broken = make_fake(_health_raises=True)()
    health = intel_base.safe_health(broken)
    assert health.available is False
    assert "health probe failed" in health.reason
    assert health.detail["error_type"] == "RuntimeError"


def test_unavailable_without_a_reason_is_repaired():
    """An honest layer always carries a reason; safe_health repairs a lie."""
    liar = make_fake(_available=False, _reason="")()
    health = intel_base.safe_health(liar)
    assert health.available is False
    assert "without a reason" in health.reason


def test_to_dict_survives_a_hostile_provider():
    hostile = make_fake(
        _capabilities_raises=True, _license_raises=True, _health_raises=True,
    )()
    assert intel_base.safe_health(hostile).available is False
    listing = hostile.to_dict()
    assert listing["health"]["available"] is False
    assert "error" in listing["capabilities"]
    assert listing["license"]["commercial_use"] == "UNVERIFIED"


# ---------------------------------------------------------------------------
# license honesty (contracts §1.4)
# ---------------------------------------------------------------------------


def test_license_metadata_is_present_and_never_optimistic():
    intel_registry.clear_cache()
    for item in intel_registry.list_providers(commercial_mode=True):
        license_info = item["license"]
        assert set(license_info) == {
            "code_license", "code_license_url", "model_license", "model_license_url",
            "model_gated", "commercial_use", "audited_on", "notes",
        }, item["key"]
        assert license_info["commercial_use"] in intel_base.COMMERCIAL_USE_VALUES
        # PERMITTED is only ever claimable WITH an audit date.
        if license_info["commercial_use"] == intel_base.COMMERCIAL_PERMITTED:
            assert license_info["audited_on"].strip(), (
                f"{item['key']} claims PERMITTED without an audit date"
            )


def test_unverified_license_helper_defaults_to_unverified():
    license_info = intel_base.unverified_license()
    assert license_info.commercial_use == intel_base.COMMERCIAL_UNVERIFIED
    assert license_info.audited_on == ""
    assert license_info.code_license == intel_base.UNVERIFIED_CODE_LICENSE
    assert license_info.model_license == intel_base.UNVERIFIED_MODEL_LICENSE


# ---------------------------------------------------------------------------
# registry order, fallback and commercial refusal
# ---------------------------------------------------------------------------


def test_registry_falls_back_to_the_next_healthy_provider(monkeypatch):
    _install_fake(monkeypatch, "fake_bad", make_fake(
        key="fake_bad", _available=False, _reason="model weights missing",
    ))
    _install_fake(monkeypatch, "fake_good", make_fake(key="fake_good"))
    _chain(monkeypatch, alignment=("fake_bad", "fake_good"))
    provider, reasons = intel_registry.resolve("alignment")
    assert provider is not None and provider.key == "fake_good"
    assert reasons == {"fake_bad": "model weights missing"}


def test_registry_prefers_the_first_healthy_provider_in_chain_order(monkeypatch):
    _install_fake(monkeypatch, "fake_first", make_fake(key="fake_first"))
    _install_fake(monkeypatch, "fake_second", make_fake(key="fake_second"))
    _chain(monkeypatch, alignment=("fake_first", "fake_second"))
    provider, reasons = intel_registry.resolve("alignment")
    assert provider.key == "fake_first"
    assert reasons == {}


def test_provider_that_does_not_serve_the_kind_is_skipped(monkeypatch):
    _install_fake(monkeypatch, "fake_wrong", make_fake(key="fake_wrong", kind="diarization"))
    _install_fake(monkeypatch, "fake_right", make_fake(key="fake_right"))
    _chain(monkeypatch, alignment=("fake_wrong", "fake_right"))
    provider, reasons = intel_registry.resolve("alignment")
    assert provider.key == "fake_right"
    assert "does not serve 'alignment'" in reasons["fake_wrong"]


def test_commercial_mode_blocks_a_non_permitted_provider(monkeypatch):
    _install_fake(monkeypatch, "fake_unverified", make_fake(
        key="fake_unverified", _commercial_use="UNVERIFIED",
    ))
    _chain(monkeypatch, alignment=("fake_unverified",))

    provider, _ = intel_registry.resolve("alignment", commercial_mode=False)
    assert provider.key == "fake_unverified"

    provider, reasons = intel_registry.resolve("alignment", commercial_mode=True)
    assert provider is None
    assert "license not cleared for commercial use" in reasons["fake_unverified"]
    assert "UNVERIFIED" in reasons["fake_unverified"]


@pytest.mark.parametrize("verdict", ["REVIEW_REQUIRED", "PROHIBITED", "UNVERIFIED"])
def test_commercial_mode_blocks_every_non_permitted_verdict(monkeypatch, verdict):
    _install_fake(monkeypatch, "fake_x", make_fake(key="fake_x", _commercial_use=verdict))
    _chain(monkeypatch, alignment=("fake_x",))
    provider, reasons = intel_registry.resolve("alignment", commercial_mode=True)
    assert provider is None
    assert verdict in reasons["fake_x"]


def test_commercial_mode_allows_an_audited_permitted_provider(monkeypatch):
    _install_fake(monkeypatch, "fake_ok", make_fake(
        key="fake_ok",
        _commercial_use=intel_base.COMMERCIAL_PERMITTED,
        _audited_on="2026-09-29",
    ))
    _chain(monkeypatch, alignment=("fake_ok",))
    provider, reasons = intel_registry.resolve("alignment", commercial_mode=True)
    assert provider is not None and provider.key == "fake_ok"
    assert reasons == {}
    assert intel_registry.license_block_reason(provider) == ""


def test_list_providers_marks_the_selection_and_chain(monkeypatch):
    _install_fake(monkeypatch, "fake_ok", make_fake(key="fake_ok", kinds=("denoise",)))
    _chain(monkeypatch, alignment=("fake_ok",), denoise=("fake_ok",))
    items = intel_registry.list_providers()
    assert [i["key"] for i in items] == ["fake_ok"]
    assert items[0]["chain"] == ["fake_ok"]
    assert items[0]["kinds"] == ["alignment", "denoise"]
    assert items[0]["selected"] is True
    assert items[0]["health"]["available"] is True


def test_resolve_or_unavailable_never_returns_none(monkeypatch):
    _install_fake(monkeypatch, "fake_bad", make_fake(
        key="fake_bad", _available=False, _reason="not installed",
    ))
    _chain(monkeypatch, alignment=("fake_bad",))
    provider, reasons = intel_registry.resolve_or_unavailable("alignment")
    assert provider is not None
    assert provider.health().available is False
    assert "fake_bad: not installed" in provider.health().reason
    assert reasons == {"fake_bad": "not installed"}


# ---------------------------------------------------------------------------
# factory never raises
# ---------------------------------------------------------------------------


def test_build_provider_never_raises_for_any_input():
    for kind, name in (("", ""), ("alignment", ""), ("nope", "also_nope"), ("", "x" * 200)):
        provider = intel_factory.build_provider(kind, name)
        assert provider is not None
        assert intel_base.safe_health(provider).available is False


def test_unavailable_adapter_reports_and_refuses_to_run():
    adapter = intel_factory.UnavailableAdapter(reason="nothing installed")
    health = intel_base.safe_health(adapter)
    assert health.available is False
    assert health.reason == "nothing installed"
    assert adapter.cost(adapter.resource_requirements())["billed"] is False
    request = intel_base.IntelRequest(workspace_id="w", asset_id="a", storage_path="x")
    with pytest.raises(intel_base.ProviderUnavailable):
        adapter.run(
            request,
            progress=lambda _pct: None,
            should_cancel=lambda: False,
            deadline=None,
        )


def test_provider_unavailable_is_a_value_error():
    """Routes map a domain ValueError to 422, never to a 500."""
    assert issubclass(intel_base.ProviderUnavailable, ValueError)


# ---------------------------------------------------------------------------
# cooperative control (contracts §1.1)
# ---------------------------------------------------------------------------


def test_check_control_raises_on_cancel_and_deadline():
    import time

    with pytest.raises(intel_base.ProviderCancelled):
        intel_base.check_control(lambda: True, None)
    with pytest.raises(intel_base.ProviderTimeout):
        intel_base.check_control(lambda: False, time.monotonic() - 1)
    intel_base.check_control(lambda: False, None)  # no cancel, no deadline -> no raise


def test_deadline_in_passes_through_none():
    import time

    assert intel_base.deadline_in(None) is None
    assert intel_base.deadline_in(5) > time.monotonic()


# ---------------------------------------------------------------------------
# shared ffmpeg helpers (the API every later lane builds on)
# ---------------------------------------------------------------------------


def test_ffmpeg_helpers_return_safe_values_on_failure():
    """No file, no binary, bad args -> empty/None, never an exception."""
    from app.engine.intel import ffmpeg_util as ff

    missing = "definitely-not-here.wav"
    assert ff.probe(missing) == {}
    assert ff.duration_seconds(missing) is None
    assert ff.measure_loudness(missing) == {
        "integrated_lufs": None, "lra": None, "peak_dbfs": None, "true_peak_dbfs": None,
    }
    assert ff.measure_peaks(missing) == {
        "peak_dbfs": None, "samples": None, "flat_factor": None, "clipped_samples": None,
    }
    assert ff.detect_silence(missing) == []
    assert ff.extract_frames(missing, 2, "frames") == []
    assert ff.frame_difference_energy(missing, 2) == []
    assert ff.frame_difference_energy([], 2) == []
    assert ff.concat_audio([], "out.wav")["ok"] is False
    assert isinstance(ff.ffmpeg_available(), bool)
    assert isinstance(ff.ffprobe_available(), bool)


def test_run_filter_refuses_to_overwrite_its_source(tmp_path):
    """The derived-only invariant is enforced, not just documented."""
    from app.engine.intel import ffmpeg_util as ff

    src = tmp_path / "source.wav"
    src.write_bytes(b"not-really-audio")
    with pytest.raises(ValueError, match="derived-only"):
        ff.run_filter(src, src, "anull")
    with pytest.raises(ValueError):
        ff.concat_audio([src], src)


def test_filter_args_pick_the_stream_type():
    from app.engine.intel import ffmpeg_util as ff

    assert ff._filter_args("loudnorm=I=-16", None) == ["-af", "loudnorm=I=-16"]
    assert ff._filter_args("crop=1080:1920:0:0", None) == ["-vf", "crop=1080:1920:0:0"]
    assert ff._filter_args("loudnorm", video=True) == ["-vf", "loudnorm"]
    assert ff._filter_args(["-af", "anull"], None) == ["-af", "anull"]
    assert ff._filter_args([], None) == []


@pytest.mark.slow
def test_ffmpeg_measurements_on_real_media(tmp_path):
    """Real lavfi audio: loudness, peaks, silence, a derived file, motion."""
    import subprocess

    from app.engine.intel import ffmpeg_util as ff

    assert ff.ffmpeg_available(), "this slow test needs ffmpeg on PATH"
    src = tmp_path / "source.wav"
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-f", "lavfi", "-t", "2",
         "-i", "anullsrc=r=44100:cl=mono",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]", "-map", "[out]", str(src)],
        capture_output=True, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr[-400:]

    assert ff.duration_seconds(src) == 4.0
    loud = ff.measure_loudness(src)
    assert loud["integrated_lufs"] is not None and loud["integrated_lufs"] < 0
    assert loud["true_peak_dbfs"] is not None
    assert loud["peak_dbfs"] is None, "ebur128 measures only the true peak"
    peaks = ff.measure_peaks(src)
    assert peaks["samples"] and peaks["samples"] > 0
    assert peaks["clipped_samples"] in (None, 0), "never invented"
    silences = ff.detect_silence(src, noise_db=-50, min_duration=0.8)
    assert silences and silences[0]["start_s"] == 2.0

    derived = tmp_path / "derived.wav"
    result = ff.run_filter(src, derived, "loudnorm=I=-16:TP=-1.5")
    assert result["ok"] is True, result["stderr_tail"][-300:]
    assert derived.exists() and derived.stat().st_size > 0
    assert src.read_bytes() != derived.read_bytes(), "the source must be untouched"

    video = tmp_path / "clip.mp4"
    made = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "testsrc=size=160x120:rate=5:duration=2", str(video)],
        capture_output=True, timeout=120, check=False,
    )
    assert made.returncode == 0, made.stderr[-400:]
    frames = ff.extract_frames(video, 2, tmp_path / "frames", width=80)
    assert frames and all(p.endswith(".png") for p in frames)
    energy = ff.frame_difference_energy(video, 2)
    assert energy and all(0.0 <= v <= 1.0 for v in energy)


# ---------------------------------------------------------------------------
# cache reuse (contracts §16 matrix row owned by Lane A)
# ---------------------------------------------------------------------------


def _asset(db, workspace_id: str, checksum: str = "sum-1"):
    from app.models import MediaAsset

    row = MediaAsset(workspace_id=workspace_id, type="audio",
                     storage_key="src.wav", checksum=checksum)
    db.add(row)
    db.flush()
    return row


def test_cache_reuse_returns_the_prior_run_without_new_work(db_session, workspace_with_user):
    from app.models import MediaIntelRun, Workspace
    from app.services import media_intel_runs as runs

    ws = db_session.get(Workspace, workspace_with_user["workspace"])
    asset = _asset(db_session, ws.id)

    first = runs.create_run(db_session, ws, kind="alignment", asset=asset,
                            provider_key="whisperx_alignment", model_version="v1",
                            params={"model": "large"})
    assert first["cache_hit"] is False
    row = runs.get_run(db_session, ws.id, first["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row, metrics={"words": 12})
    db_session.commit()

    second = runs.create_run(db_session, ws, kind="alignment", asset=asset,
                             provider_key="whisperx_alignment", model_version="v1",
                             params={"model": "large"})
    assert second["cache_hit"] is True
    assert second["id"] == first["id"]
    assert second["status"] == "COMPLETED"
    # exactly ONE run row for this workspace: a hit must not create a second one
    scoped = db_session.query(MediaIntelRun).filter(
        MediaIntelRun.workspace_id == ws.id,
        MediaIntelRun.asset_id == asset.id,
    ).all()
    assert len(scoped) == 1
