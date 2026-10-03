"""Work 15.6 -- generated music is OPT-IN, brand-governed, and never load-bearing.

Work 15.5 built the provider and left ``generate_music`` with zero callers. This
lane supplies the two things it needed and the tests that hold them down.

The properties under test, each a way this integration could do real harm:

* **an unset policy silently generates music** and spends money nobody agreed to,
* **a BrandDNA hard rule is overridden** by a recommendation or a brief,
* **an unset preference is invented** as a default genre,
* **generated audio overwrites a source asset** (the whole point of lineage),
* **a paid SUBMISSION_UNKNOWN is auto-retried** (double charge),
* **a wildly wrong duration** is mixed under the narration anyway,
* **a music failure fails the video** instead of degrading to a warning.

No network, no ffmpeg requirement: the provider is an in-test double at the
``generate_music`` boundary, which is where the pipeline calls it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.engine.timeline import TRACK_KINDS, validate_timeline
from app.engine.ugc.pipeline import UGCVideoPipeline
from app.providers.music.base import (
    MUSIC_TRACK_KIND,
    MusicIntelligenceProvider,
    MusicProviderError,
    MusicResult,
    music_or_none,
)
from app.providers.music.policy import (
    MUSIC_PREF_KEYS,
    MusicPolicyRefused,
    brand_music_prefs,
    duration_within_tolerance,
    enforce_forbidden_genres,
    music_policy,
    music_settings,
    recommend_style,
)

# Eager module-scope import, BEFORE `pipeline_factory` patches
# ``app.services.storage.STORAGE_ROOT``. ``timeline_render`` binds STORAGE_ROOT
# with ``from ... import`` at ITS import time, so a first import happening while
# that patch is active would capture a tmp path permanently and break every
# later render test in the session (observed: two test_timeline_render.py tests
# failing only after this module ran). Collection-time import keeps it real.
from app.providers.video_engine.timeline_render import render_timeline  # noqa: F401
from app.services.paid_jobs import (
    PaidArtifactUndownloadable,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionState,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")

#: A live-provider test marker: honest skip, never a fake pass.
live_provider = pytest.mark.skipif(
    not os.environ.get("YMONEY_ELEVENLABS_API_KEY"),
    reason="set YMONEY_ELEVENLABS_API_KEY to run live music provider tests",
)


# ===========================================================================
# doubles
# ===========================================================================


class StubProvider(MusicIntelligenceProvider):
    """A provider double at the ``generate_music`` seam.

    Records every ``generate`` call so a test can assert EXACTLY ONE submit
    happened -- the property that makes a paid provider safe.
    """

    key = "stub_music"
    is_mock = True

    def __init__(self, *, duration: float = 6.0, state: SubmissionState | None = None,
                 raises: BaseException | None = None) -> None:
        #: One entry per submit that REACHED the provider, recorded at the
        #: ``generate_music`` seam -- the same place production would count one.
        self.calls: list = []
        self._duration = duration
        self._state = state or SubmissionState.SUCCEEDED
        self._raises = raises

    def available(self) -> bool:
        return True

    def estimate_cost(self, request) -> float:
        return 0.05

    def generate(self, request) -> MusicResult:
        if self._raises is not None:
            raise self._raises
        return MusicResult(
            provider=self.key, path="", duration_seconds=self._duration,
            prompt="stub", state=self._state,
            provenance={"model_id": "stub-model"})


@pytest.fixture()
def pipeline_factory(db_session, tmp_path, monkeypatch):
    """Build a UGC pipeline whose music provider is a controllable stub."""
    from app.models import UgcProjectRow, Workspace
    from app.models.assets import MediaAsset
    from app.models.brand import BrandDNARow
    from app.services import storage as storage_mod

    storage_root = tmp_path / "storage"
    monkeypatch.setattr(storage_mod, "STORAGE_ROOT", storage_root)

    def build(*, settings: dict | None = None, dna: dict | None = None,
              provider: StubProvider | None = None, segments: int = 2,
              seg_duration: float = 3.0, audio_dir: Path | None = None):
        ws = Workspace(name="Music WS", slug=f"mus-{os.urandom(4).hex()}",
                       niche="AI money", settings_json=settings or {})
        db_session.add(ws)
        db_session.flush()
        if dna is not None:
            db_session.add(BrandDNARow(workspace_id=ws.id, brand_id=None,
                                       dna_json=dna))
            db_session.flush()
        # a source video asset the bed is derived from (lineage parent)
        source = MediaAsset(workspace_id=ws.id, type="video", origin="upload",
                            provider="test", storage_key="source.mp4",
                            mime_type="video/mp4", duration_seconds=99.0,
                            file_size=1234)
        db_session.add(source)
        db_session.flush()
        row = UgcProjectRow(
            workspace_id=ws.id, preset="PRODUCT_DEMO",
            brief_json={"topic": "Aurora Lamp", "audience": "buyers",
                        "tone": "friendly"},
            status="RUNNING")
        db_session.add(row)
        db_session.flush()

        pipe = UGCVideoPipeline(db_session, ws.id, row)
        pipe.segments = [
            {"text": f"line {i}", "asset_id": f"voice-{i}", "duration": seg_duration}
            for i in range(segments)
        ]
        # the presenter output is the canonical video a bed is derived from
        pipe.presenter = {"type": "avatar", "rendered": True,
                          "output_asset_id": source.id}
        if provider is not None:
            if audio_dir is None:
                audio_dir = tmp_path / "provider_out"
            _install(provider, Path(audio_dir),
                     seg_duration * segments)
            monkeypatch.setattr("app.providers.music.generate_music",
                                _make_generate(provider))
        db_session.flush()
        return pipe, source

    return build


def _install(provider: StubProvider, audio_dir: Path, seconds: float) -> None:
    """Give the stub a real file to publish.

    ``generate`` is wrapped rather than replaced so the stub still owns its
    behaviour (and raises what it was told to raise); the call counter lives at
    the ``generate_music`` seam instead, which is what a real submit counts.
    """
    inner = provider.generate

    def _generate(request):
        result = inner(request)
        if result is not None and not result.path:
            path = _write_audio(audio_dir, result.duration_seconds or seconds)
            result.path = path
            result.file_size = Path(path).stat().st_size
        return result

    provider.generate = _generate


def _make_generate(provider: StubProvider):
    """Replace the package entry point the pipeline calls.

    ``music_or_none`` still governs the outcome, so the stage sees exactly the
    shapes production gives it (a result, or None after a classified failure).
    """
    def _generate(key, request, **kwargs):
        provider.calls.append(request)
        return music_or_none(provider, request)

    return _generate


def _write_audio(tmp_path: Path, seconds: float) -> str:
    """A real audio file the persistence path copies into workspace storage."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / f"bed_{seconds}.mp3"
    target.write_bytes(b"ID3-stub-audio-payload" * 8)
    return str(target)


# ===========================================================================
# A. the policy gate -- opt-in, and unset means NO generation
# ===========================================================================


def test_an_unset_policy_does_not_allow_generation():
    """The load-bearing default: nothing configured means nothing generated."""
    policy = music_policy(None, None)
    assert policy.generate is False
    assert policy.reason == "policy_not_enabled"


def test_an_unset_policy_on_a_real_workspace_row_does_not_allow_generation():
    """A Workspace with no settings at all is the same as no policy."""
    class _Ws:
        settings_json = {}

    assert music_policy(_Ws(), None).generate is False
    assert music_policy({"other": {"generate": True}}, None).generate is False


def test_the_opt_in_uses_the_learning_assist_settings_shape():
    """`settings_json["music"]["generate"] is True`, mirroring learning_assist."""
    assert music_settings({"music": {"generate": True}}) == {"generate": True}
    policy = music_policy({"music": {"generate": True}}, None)
    assert policy.generate is True and policy.reason == "policy_enabled"
    for falsy in ({"music": {"generate": False}}, {"music": {}},
                  {"music": {"generate": "maybe"}}):
        assert music_policy(falsy, None).generate is False, falsy


def test_unset_preferences_never_become_a_default_genre_or_mood():
    """No preference stated must yield no genre, no mood, no brief, no energy."""
    policy = music_policy({"music": {"generate": True}}, None)
    assert policy.prefs == {}
    assert policy.genre == "" and policy.mood == "" and policy.brief == ""
    assert policy.energy == "" and policy.intensity == ""
    assert policy.preferred_genres == []
    assert policy.instrumental is None   # unstated, not "instrumental"


def test_a_refused_policy_always_carries_a_reason():
    """A refusal nobody can explain is indistinguishable from a bug."""
    for settings, dna in ((None, None), ({"music": {"generate": False}}, None),
                          ({"music": {"generate": True}},
                           {"music_prefs": {"enabled": False}})):
        policy = music_policy(settings, dna)
        assert policy.generate is False
        assert policy.reason and policy.reason != "policy_enabled"


# ===========================================================================
# BrandDNA mapping -- only keys the typed document really defines
# ===========================================================================


def test_prefs_are_read_from_the_real_brand_dna_document():
    """The typed BrandDNA's own music_prefs, not an invented field."""
    from app.engine.brand.dna import BrandDNA

    dna = BrandDNA.model_validate({"music_prefs": {
        "mood": "calm", "genre": "ambient minimal", "energy": "low",
        "instrumental": True, "forbidden_genres": ["Rock"]}})
    assert brand_music_prefs(dna)["genre"] == "ambient minimal"
    assert "music_prefs" in BrandDNA.model_fields
    policy = music_policy({"music": {"generate": True}}, dna)
    assert policy.mood == "calm" and policy.energy == "low"
    assert policy.forbidden_genres == ("rock",)
    assert policy.instrumental is True


def test_unrecognised_prefs_are_dropped_not_echoed():
    """A typo must show as a missing value, not a preference that does nothing."""
    prefs = brand_music_prefs({"music_prefs": {"mood": "calm", "loudness": 11}})
    assert prefs == {"mood": "calm"}
    assert set(prefs) <= set(MUSIC_PREF_KEYS)


def test_prefs_travel_onto_the_request_only_when_stated():
    """apply_to writes stated preferences and leaves the rest to the provider."""
    from app.providers.music.base import MusicRequest

    request = MusicRequest(workspace_id="w", duration_seconds=10.0)
    music_policy({"music": {"generate": True}},
                 {"music_prefs": {"genre": "lofi chill"}}).apply_to(request)
    assert request.genre == "lofi chill"
    assert request.brand_music_prefs.get("genre") == "lofi chill"
    assert request.style()["genre"] == "lofi chill"
    # unstated dims stay empty rather than being filled in
    assert request.style()["mood"] == ""


# ===========================================================================
# C. brand hard rules beat any recommendation
# ===========================================================================


def test_a_forbidden_genre_is_refused_even_when_explicitly_requested():
    """The hard rule: an explicit request for a forbidden genre still loses."""
    policy = music_policy({"music": {"generate": True}},
                          {"music_prefs": {"forbidden_genres": ["Death Metal"]}})
    with pytest.raises(MusicPolicyRefused) as exc:
        enforce_forbidden_genres(["death metal"], policy)
    assert "death metal" in str(exc.value)


def test_a_recommendation_naming_a_forbidden_genre_is_dropped_not_rewritten():
    """A recommendation is filtered, and the conflict stays visible."""
    policy = music_policy({"music": {"generate": True}},
                          {"music_prefs": {"forbidden_genres": ["dubstep"]}})
    out = recommend_style({"genre": "dubstep", "mood": "loud"}, policy)
    assert out["rejected"] is True
    assert out["genre"] == "" and out["mood"] == ""
    assert "forbidden" in out["reason"]


def test_a_clean_recommendation_is_used_and_still_brand_subordinate():
    policy = music_policy({"music": {"generate": True}},
                          {"music_prefs": {"genre": "lofi chill",
                                           "forbidden_genres": ["dubstep"]}})
    out = recommend_style({"genre": "ambient pad", "mood": "calm"}, policy)
    assert out["rejected"] is False
    # the brand's own genre wins over the recommendation
    assert out["genre"] == "lofi chill" and out["mood"] == "calm"


def test_a_brand_refusal_outranks_the_workspace_opt_in():
    """The strongest rule: a brand that says no is never overridden."""
    policy = music_policy({"music": {"generate": True}},
                          {"music_prefs": {"enabled": False}})
    assert policy.generate is False
    assert policy.reason == "brand_disabled_music"
    assert policy.brand_disabled is True


def test_a_disabled_music_preference_label_also_refuses():
    """`brand_templates.music_preference` is a refusal surface too."""
    for label in ("none", "off", "mute"):
        policy = music_policy({"music": {"generate": True, "preference": label}}, None)
        assert policy.generate is False and policy.brand_disabled is True


# ===========================================================================
# B. the pipeline stage
# ===========================================================================


def test_the_stage_records_a_refusal_and_never_calls_the_provider(pipeline_factory):
    """Unset policy: the provider is not even constructed."""
    provider = StubProvider()
    pipe, _ = pipeline_factory(provider=provider, settings=None)
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] == "policy_not_enabled"
    assert provider.calls == []
    assert pipe.lineage["music"]["reason"] == "policy_not_enabled"
    assert pipe.music == {}


def test_the_stage_generates_only_when_policy_and_budget_allow(pipeline_factory):
    """The happy path, end to end, without a network call."""
    provider = StubProvider(duration=6.0)
    pipe, source = pipeline_factory(
        settings={"music": {"generate": True}},
        dna={"music_prefs": {"genre": "ambient minimal"}}, provider=provider)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is True, out
    assert out["asset_id"]
    assert out["provider"] == "stub_music"
    assert len(provider.calls) == 1
    # the provider actually saw the brand preference
    assert provider.calls[0].style()["genre"] == "ambient minimal"


def test_the_generated_asset_is_canonical_with_lineage(pipeline_factory, tmp_path):
    """A MediaAsset of type audio with parent + derivation, not a bespoke row."""
    from app.models.assets import MediaAsset

    provider = StubProvider(duration=6.0)
    pipe, source = pipeline_factory(settings={"music": {"generate": True}},
                                   provider=provider, audio_dir=tmp_path)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is True, out
    asset = pipe.session.get(MediaAsset, out["asset_id"])
    assert asset.type == "audio"          # canonical type
    assert asset.origin == "generated"
    assert asset.parent_asset_id == source.id
    assert asset.derivation_json["provider"] == "stub_music"
    assert asset.derivation_json["model"] == "stub-model"
    # the cost is always flagged as an ESTIMATE, never presented as a bill
    assert asset.derivation_json["cost_is_estimate"] is True
    assert "estimated_cost_usd" in asset.derivation_json
    assert asset.storage_key != source.storage_key


def test_a_real_provider_estimate_is_recorded_on_the_asset(pipeline_factory,
                                                           tmp_path, monkeypatch):
    """A paid provider's own non-zero estimate reaches the asset's derivation."""
    from app.models.assets import MediaAsset

    provider = StubProvider(duration=6.0)
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider, audio_dir=tmp_path)
    monkeypatch.setattr("app.providers.music.get_music_provider",
                        lambda key, **kw: provider)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is True, out
    asset = pipe.session.get(MediaAsset, out["asset_id"])
    assert asset.derivation_json["estimated_cost_usd"] == 0.05
    assert asset.derivation_json["cost_is_estimate"] is True


def test_generated_music_never_replaces_a_source_asset(pipeline_factory, tmp_path):
    """THE lineage property: the source row is untouched and still original."""
    from app.models.assets import MediaAsset

    provider = StubProvider(duration=6.0)
    pipe, source = pipeline_factory(settings={"music": {"generate": True}},
                                   provider=provider, audio_dir=tmp_path)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is True, out

    pipe.session.expire_all()
    after = pipe.session.get(MediaAsset, source.id)
    assert after.origin == "upload"
    assert after.type == "video"
    assert after.duration_seconds == 99.0
    assert after.file_size == 1234
    assert after.checksum == ""
    assert after.parent_asset_id is None
    assert after.derivation_json == {}
    assert after.storage_key == source.storage_key
    # a separate, derived row exists and points back at the source
    generated = pipe.session.get(MediaAsset, out["asset_id"])
    assert generated.id != source.id
    assert generated.origin == "generated"
    assert generated.parent_asset_id == source.id


def _result_with_path(tmp: Path, seconds: float) -> MusicResult:
    return MusicResult(
        provider="stub_music", path=_write_audio(tmp, seconds),
        duration_seconds=seconds, prompt="p", state=SubmissionState.SUCCEEDED,
        file_size=168, audio_codec="mp3", checksum="abc",
        provenance={"model_id": "stub-model"})


def test_the_budget_gate_refuses_before_any_billable_call(pipeline_factory, monkeypatch):
    """Rejected budget -> no provider call, video still renders."""
    from app.services import cost as cost_mod

    def _refuse(workspace_id, estimated):
        raise cost_mod.BudgetExceededError("daily budget exhausted")

    monkeypatch.setattr(cost_mod, "assert_can_spend", _refuse)
    monkeypatch.setattr("app.services.cost.assert_can_spend", _refuse)
    provider = StubProvider()
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] == "budget_rejected"
    assert "budget exhausted" in out["detail"]
    assert provider.calls == []          # nothing was spent
    assert pipe.music == {}


def test_a_cancelled_job_never_spends_money(pipeline_factory):
    """Cancellation is checked before the billable call."""
    from app.services.jobs import JobContext

    provider = StubProvider()
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider)
    pipe.session.commit()
    pipe.job_ctx = JobContext(job_id="j", type="ugc_pipeline",
                              workspace_id=pipe.workspace_id, cycle_id=None,
                              payload={}, attempt=1, cancelled=lambda: True)
    out = pipe.stage_music()
    assert out["reason"] == "cancelled"
    assert provider.calls == []


def test_a_wildly_wrong_duration_is_refused(pipeline_factory):
    """A 60s bed for a 6s video is not a soundtrack; it is a mistake."""
    provider = StubProvider(duration=60.0)
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] == "duration_mismatch"
    assert out["measured_duration"] == 60.0
    assert out["expected_duration"] == 6.0
    assert pipe.music == {}


def test_duration_tolerance_accepts_a_track_that_fits():
    assert duration_within_tolerance(6.0, 6.0) is True
    assert duration_within_tolerance(7.0, 6.0) is True    # within 25%
    assert duration_within_tolerance(60.0, 6.0) is False
    assert duration_within_tolerance(0.0, 6.0) is False
    assert duration_within_tolerance(6.0, 0.0) is False


def test_a_forbidden_genre_in_the_brief_blocks_generation(pipeline_factory):
    """Even an explicit brief request cannot outrank the brand's rule."""
    provider = StubProvider(duration=6.0)
    pipe, _ = pipeline_factory(
        settings={"music": {"generate": True}},
        dna={"music_prefs": {"forbidden_genres": ["polka"]}}, provider=provider)
    pipe.brief["music"] = {"genre": "polka"}
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] == "forbidden_genre"
    assert provider.calls == []


def test_a_paid_submission_unknown_is_recorded_and_never_resubmitted(pipeline_factory):
    """The money guard, end to end: one call, loud state, no second attempt."""
    provider = StubProvider(
        raises=PaidSubmissionUnconfirmed(provider="stub_music",
                                         detail="read timeout"))
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] == "submission_unknown"
    assert out["must_not_resubmit"] is True
    assert out["state"] == "SUBMISSION_UNKNOWN"
    assert len(provider.calls) == 1       # exactly one submit, never two
    assert pipe.lineage["music"]["must_not_resubmit"] is True


@pytest.mark.parametrize("exc", [
    PaidJobRejected(provider="stub_music", status_code=402, detail="no credit"),
    PaidArtifactUndownloadable(provider="stub_music", remote_id="r1"),
    MusicProviderError("proxy failed"),
])
def test_every_provider_failure_degrades_without_a_bed(pipeline_factory, exc):
    """A soundtrack failure is a warning on the video, never a failed video."""
    provider = StubProvider(raises=exc)
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] == "unavailable_or_failed"
    assert pipe.music == {}
    assert len(provider.calls) == 1


def test_an_unconfigured_provider_degrades_to_a_recorded_unavailable(pipeline_factory):
    """No provider at all is reported, not silently replaced with mock audio."""
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}})
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    assert out["reason"] in ("unavailable_or_failed", "duration_mismatch")


# ===========================================================================
# evidence: the bed reaches the timeline / render path
# ===========================================================================


def test_the_bed_lands_on_the_canonical_music_track_that_the_renderer_mixes(
        pipeline_factory, tmp_path):
    """The render already mixes track kind 'music'; the bed must be ON it."""
    provider = StubProvider(duration=6.0)
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider, audio_dir=tmp_path)
    pipe.session.commit()
    pipe.cta_text = "follow for more"
    out = pipe.stage_music()
    assert out["generated"] is True, out

    row = pipe.stage_timeline()
    doc = dict(row.tracks_json)
    validate_timeline(doc)
    music_tracks = [t for t in doc["tracks"] if t["kind"] == MUSIC_TRACK_KIND]
    assert len(music_tracks) == 1
    clips = music_tracks[0]["clips"]
    assert len(clips) == 1
    assert clips[0]["source"]["asset_id"] == out["asset_id"]
    assert clips[0]["volume"] <= 0.25          # a bed, not a narration track
    assert MUSIC_TRACK_KIND in TRACK_KINDS

    # the renderer's audio mix picks up exactly this track kind
    import inspect

    source = inspect.getsource(render_timeline)
    assert '("voice", "music", "sfx")' in source


def test_no_bed_leaves_the_music_track_empty_and_the_timeline_valid(pipeline_factory):
    """A refused run still produces a valid timeline with an empty music track."""
    pipe, _ = pipeline_factory(settings=None)
    pipe.session.commit()
    out = pipe.stage_music()
    assert out["generated"] is False
    pipe.cta_text = "follow for more"
    row = pipe.stage_timeline()
    doc = dict(row.tracks_json)
    validate_timeline(doc)
    assert [c for t in doc["tracks"] if t["kind"] == MUSIC_TRACK_KIND
            for c in t["clips"]] == []


def test_qc_sees_the_music_decision_and_never_fails_on_it(pipeline_factory, tmp_path):
    """QC reports the bed as pass/warning, and a missing bed is not a FAIL."""
    provider = StubProvider(duration=6.0)
    pipe, _ = pipeline_factory(settings={"music": {"generate": True}},
                               provider=provider, audio_dir=tmp_path)
    pipe.session.commit()
    pipe.cta_text = "follow for more"
    pipe.stage_music()
    pipe.stage_timeline()
    report = pipe.run_qc()
    assert "music_decision" in report.checks
    assert report.checks["music_decision"]["status"] == "pass"
    assert "music clip(s) on the canonical track" in \
        report.checks["music_decision"]["detail"]
    assert report.status != "FAIL"


def test_the_stage_is_wired_into_run_between_voice_and_timeline():
    """`generate_music` must be reachable from the real orchestration.

    This is the gap Work 15.5 left open: a provider with zero callers. Reading
    the stage list out of ``run`` is the cheapest honest proof that the call path
    is live, without paying for a full pipeline run in every test.
    """
    import inspect

    from app.providers.music import generate_music

    source = inspect.getsource(UGCVideoPipeline.run)
    assert "self.stage_music()" in source
    # the stage must sit after the narration and before the timeline doc
    assert source.index("self.stage_voice()") < source.index("self.stage_music()")
    assert source.index("self.stage_music()") < source.index("self.stage_timeline()")
    assert '"music"' in source          # reported in the result's stage list
    assert callable(generate_music)


def test_qc_explains_why_there_is_no_bed_without_downgrading_the_run(pipeline_factory):
    """A policy refusal is reported, not warned about.

    Warning here would turn every opt-out run into PASS_WITH_WARNINGS for
    something nobody asked for; the reason still has to be readable.
    """
    pipe, _ = pipeline_factory(settings=None)
    pipe.session.commit()
    pipe.cta_text = "follow for more"
    pipe.stage_music()
    pipe.stage_timeline()
    report = pipe.run_qc()
    check = report.checks["music_decision"]
    assert check["status"] == "pass"
    assert "policy_not_enabled" in check["detail"]
    # the music check contributes nothing to the rollup: the run's status is
    # exactly what it would be with no music stage at all
    from app.engine.ugc.qc import _rollup

    without = {k: v for k, v in report.checks.items() if k != "music_decision"}
    assert report.status == _rollup(without)


# ===========================================================================
# the API surface
# ===========================================================================


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, tag: str) -> dict:
    r = client.post("/api/v1/auth/register",
                    json={"email": f"{tag}{os.urandom(4).hex()}@test.local",
                          "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"headers": {"Authorization": f"Bearer {data['access_token']}"},
            "ws": data["workspace"]["id"]}


def test_the_api_reports_an_unset_policy_as_not_enabled(client):
    """The UI must never see a green light nobody turned on."""
    ctx = _register(client, "musoff")
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/music/policy",
                   headers=ctx["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generate"] is False
    assert body["reason"] == "policy_not_enabled"
    assert body["forbidden_genres"] == []
    assert body["prefs"] == {}


def test_the_api_can_turn_the_opt_in_on_and_off(client):
    ctx = _register(client, "muson")
    base = f"/api/v1/workspaces/{ctx['ws']}/music/policy"
    r = client.put(base, headers=ctx["headers"], json={"generate": True})
    assert r.status_code == 200, r.text
    assert r.json()["generate"] is True and r.json()["reason"] == "policy_enabled"
    r = client.put(base, headers=ctx["headers"], json={"generate": False})
    assert r.json()["generate"] is False


def test_the_api_refuses_an_unknown_provider(client):
    ctx = _register(client, "musprov")
    r = client.put(f"/api/v1/workspaces/{ctx['ws']}/music/policy",
                   headers=ctx["headers"],
                   json={"provider_key": "sunrise-audio"})
    assert r.status_code == 422, r.text
    assert "unknown provider" in r.json()["detail"]


def test_the_api_requires_auth_and_workspace_scope(client):
    ctx = _register(client, "musauth")
    assert client.get(f"/api/v1/workspaces/{ctx['ws']}/music/policy").status_code == 401
    other = _register(client, "musother")
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/music/policy",
                   headers=other["headers"])
    assert r.status_code == 403, r.text


def test_the_api_reports_provider_availability_honestly(client):
    ctx = _register(client, "musavail")
    r = client.get(f"/api/v1/workspaces/{ctx['ws']}/music/providers",
                   headers=ctx["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["key"] for i in body["items"]] == ["elevenlabs_music"]
    entry = body["items"][0]
    assert isinstance(entry["available"], bool)
    # no credential in this environment -> honestly unavailable, not faked
    if not entry["available"]:
        assert entry["detail"]


def test_the_api_reports_brand_prefs_without_inventing_them(client, db_session):
    from app.models import BrandDNARow

    ctx = _register(client, "musbrand")
    assert client.get(f"/api/v1/workspaces/{ctx['ws']}/music/prefs",
                      headers=ctx["headers"]).json()["prefs"] == {}
    db_session.add(BrandDNARow(
        workspace_id=ctx["ws"], brand_id=None,
        dna_json={"music_prefs": {"genre": "lofi chill",
                                  "forbidden_genres": ["polka"]}}))
    db_session.commit()
    body = client.get(f"/api/v1/workspaces/{ctx['ws']}/music/prefs",
                      headers=ctx["headers"]).json()
    assert body["prefs"]["genre"] == "lofi chill"
    policy = client.get(f"/api/v1/workspaces/{ctx['ws']}/music/policy",
                        headers=ctx["headers"]).json()
    assert policy["forbidden_genres"] == ["polka"]


# ===========================================================================
# live provider (honestly skipped without a credential)
# ===========================================================================


@live_provider
@requires_ffmpeg
def test_a_live_generation_produces_a_verified_bed(tmp_path):
    """Only runs with a real credential; never faked into a pass."""
    from app.providers.music import MusicRequest, generate_music
    from tests.test_work15_5_music import _video

    result = generate_music(
        "elevenlabs_music",
        MusicRequest(workspace_id=str(tmp_path), duration_seconds=2.0,
                     video_path=str(_video(tmp_path, 1)),
                     brand_music_preference="low_ambient"),
        api_key=os.environ["YMONEY_ELEVENLABS_API_KEY"])
    assert result is not None and result.usable