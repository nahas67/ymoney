"""Work 11 Lane X: professional export center (engine/exporter + api/v1/exports).

Locks contracts §11 without a network and without faking a render:

  * profile validation -- the 8 builtin presets, unknown preset -> 422,
    non-positive dims -> 422, fps outside 1..120 -> 422, unknown codec ->
    422, AUDIO_ONLY -> MP3|WAV only, CAPTIONS_ONLY -> SRT|VTT|ASS|TXT only,
    MP4 + CAPTIONS_ONLY -> 422
  * format registry honesty -- every entry carries ``available`` plus a
    NON-EMPTY ``reason`` when unavailable; Premiere/Resolve are
    NOT_AVAILABLE and NOTHING is written when they are requested
  * subtitle export -- SRT + VTT + ASS generated from a seeded caption track
    and parsed back (SRT through the real ``providers/dubbing.parse_srt``)
  * OTIO export -- the artifact is re-read with the REAL OTIO adapter and
    ``roundtrip_serialized`` agrees with the source doc
  * export retry/cancel -- FAILED/CANCELLED only -> attempt+1 + new job id;
    max 5 attempts -> 409; cancel only from QUEUED/RUNNING
  * the LOCKED export row shape, workspace isolation, RBAC, error hygiene
  * guarded ``_bootstrap_export_jobs`` idempotency

SLOW (marked) battery: a real lavfi MP4 is rendered with the repo's ffmpeg
pattern, exported to MP4, and asserted against ffprobe (streams / duration /
resolution / checksum / COMPLETE). A second real format is chosen by PROBE,
not by hope: when the encoder is present the test asserts the real export,
and when it is absent the test asserts the honest NOT_AVAILABLE path. Both
branches are real assertions -- nothing is faked and no test is disabled.
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.engine.exporter import formats as exporter_formats
from app.engine.exporter import jobs as exporter_jobs
from app.engine.exporter import profiles as exporter_profiles
from app.engine.exporter import verify as exporter_verify

CAPTIONS = [
    {"id": "cap1", "name": "cap1", "start": 0.0, "duration": 1.5,
     "text": "First spoken line", "source": {}, "effects": []},
    {"id": "cap2", "name": "cap2", "start": 1.5, "duration": 1.5,
     "text": "Second spoken line", "source": {}, "effects": []},
]

#: The frontend-locked export row (contracts §11). Exact key set, no more.
EXPORT_ROW_KEYS = {"id", "format", "profile", "target", "state", "progress",
                   "verification", "artifact", "error", "attempt",
                   "created_at", "finished_at"}


# ---------------------------------------------------------------------------
# fixtures / helpers (mirror tests/test_knowledge_api.py)
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"ex{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], data["user"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"}


def _make_user(client, ws_id, role):
    """Register a fresh user and grant them ``role`` on ws_id."""
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
    return {"Authorization": f"Bearer {token.json()['access_token']}"}, user_id


def _base(ws_id):
    return f"/api/v1/workspaces/{ws_id}/exports"


def _seed_workspace():
    """A throwaway workspace + owner (tests that never touch HTTP)."""
    from app.db import session_scope
    from app.models import User, Workspace, WorkspaceMember

    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"seed{tag}@test.local", password_hash="x")
        ws = Workspace(name=f"WS {tag}", slug=f"ws-{tag}", niche="n")
        s.add_all([user, ws])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        return ws.id, user.id


def _seed_timeline(ws_id, *, with_captions=True):
    """One timeline carrying a caption track (the subtitle source of truth)."""
    from app.db import session_scope
    from app.models import ContentTimeline

    doc = {"name": "main", "fps": 30.0, "duration_seconds": 3.0,
           "aspect_ratio": "9:16",
           "tracks": [{"id": "t_caption", "kind": "caption", "name": "Caption",
                       "clips": list(CAPTIONS) if with_captions else []}]}
    with session_scope() as s:
        row = ContentTimeline(workspace_id=ws_id, name="main", fps=30.0,
                              duration_seconds=3.0, tracks_json=doc)
        s.add(row)
        s.flush()
        return row.id


def _doc_of(ws_id, timeline_id):
    from app.db import session_scope
    from app.models import ContentTimeline

    with session_scope() as s:
        return dict(s.get(ContentTimeline, timeline_id).tracks_json or {})


def _seed_builtins():
    """Idempotently seed the 8 global builtin profiles; returns preset->id."""
    from app.db import session_scope

    with session_scope() as s:
        rows = exporter_profiles.seed_builtins(s)
        s.commit()
        return {r.preset: r.id for r in rows}


def _profile_id(preset):
    return _seed_builtins()[exporter_profiles.canonical_preset(preset)]


def _read_export(export_id):
    """Fresh-session re-read of one export row (repo convention)."""
    from app.db import session_scope
    from app.models import ExportJob

    with session_scope() as s:
        row = s.get(ExportJob, export_id)
        if row is None:
            return None
        return {"state": row.state, "error": row.error, "attempt": row.attempt,
                "format": row.format, "checksum": row.checksum,
                "verification": dict(row.verification_json or {}),
                "artifact_asset_id": row.artifact_asset_id, "job_id": row.job_id}


def _artifact_path(tmp_path, ws_id, asset_id):
    from app.db import session_scope
    from app.models import MediaAsset

    with session_scope() as s:
        asset = s.get(MediaAsset, asset_id)
        return tmp_path / "data/videos" / ws_id / asset.storage_key


def _enqueue(ws_id, profile_id, fmt, target_id, user_id, *, target_type="timeline"):
    from app.db import session_scope

    with session_scope() as s:
        return exporter_jobs.enqueue_export(
            s, ws_id, profile_id=profile_id, fmt=fmt, target_type=target_type,
            target_id=target_id, user_id=user_id)


def _run(ws_id, profile_id, fmt, target_id, user_id, *, target_type="timeline"):
    """enqueue_export + a synchronous run. Returns ``(export_id, result)``."""
    queued = _enqueue(ws_id, profile_id, fmt, target_id, user_id,
                      target_type=target_type)
    return queued["export_id"], exporter_jobs.run_export_now(ws_id, queued["export_id"])


def _caption_export(ws_id, user_id, fmt):
    timeline_id = _seed_timeline(ws_id)
    export_id, result = _run(ws_id, _profile_id("CAPTIONS_ONLY"), fmt,
                             timeline_id, user_id)
    row = _read_export(export_id)
    assert row["state"] == "COMPLETE", (fmt, row["error"])
    return export_id, result, row


# ---------------------------------------------------------------------------
# profile validation (contracts §11)
# ---------------------------------------------------------------------------


def test_eight_builtin_presets_present_and_shaped():
    assert exporter_profiles.PRESETS == (
        "YOUTUBE_4K", "YOUTUBE_1080P", "SHORTS_1080x1920", "INSTAGRAM_REEL",
        "TIKTOK", "ARCHIVE_MASTER", "AUDIO_ONLY", "CAPTIONS_ONLY")
    for preset in exporter_profiles.PRESETS:
        config = exporter_profiles.preset_config(preset)
        for key in ("width", "height", "fps", "video_codec", "bitrate_kbps",
                    "audio_codec", "audio_bitrate_kbps", "audio_channels",
                    "captions", "watermark", "color"):
            assert key in config, (preset, key)
        assert isinstance(config["captions"]["enabled"], bool)
        assert isinstance(config["watermark"]["enabled"], bool)
    assert exporter_profiles.preset_config("YOUTUBE_4K")["width"] == 3840
    assert exporter_profiles.preset_config("YOUTUBE_1080P")["height"] == 1080
    # source-passthrough presets carry no fixed geometry
    for preset in ("ARCHIVE_MASTER", "AUDIO_ONLY", "CAPTIONS_ONLY"):
        config = exporter_profiles.preset_config(preset)
        assert config["width"] is None and config["height"] is None
    # case-insensitive preset lookup must survive the lowercase `x`
    assert exporter_profiles.canonical_preset("SHORTS_1080X1920") == "SHORTS_1080x1920"


def test_unknown_preset_rejected():
    with pytest.raises(ValueError, match="unknown preset"):
        exporter_profiles.preset_config("TIKTOK_9x16_DELUXE")


@pytest.mark.parametrize("config,message", [
    ({"width": 0, "height": 1080}, "greater than 0"),
    ({"width": 1920, "height": -1}, "greater than 0"),
    ({"width": 1920}, "must both be set or both null"),
    ({"width": 1920, "height": 1080, "fps": 0}, "fps must be between"),
    ({"width": 1920, "height": 1080, "fps": 121}, "fps must be between"),
    ({"width": 1920, "height": 1080, "video_codec": "h265-plus"},
     "unknown video_codec"),
    ({"width": 1920, "height": 1080, "audio_codec": "atmos"},
     "unknown audio_codec"),
    ({"width": 1920, "height": 1080, "bitrate_kbps": 0}, "bitrate_kbps"),
])
def test_invalid_configs_rejected(config, message):
    with pytest.raises(ValueError, match=message):
        exporter_profiles.validate_config(config)


def test_profile_format_compatibility_matrix():
    audio = exporter_profiles.preset_config("AUDIO_ONLY")
    for fmt in ("MP3", "WAV"):
        exporter_profiles.check_profile_format("AUDIO_ONLY", audio, fmt)
    for fmt in ("MP4", "SRT", "JSON", "OTIO"):
        with pytest.raises(ValueError, match="AUDIO_ONLY supports only"):
            exporter_profiles.check_profile_format("AUDIO_ONLY", audio, fmt)

    captions = exporter_profiles.preset_config("CAPTIONS_ONLY")
    for fmt in ("SRT", "VTT", "ASS", "TXT"):
        exporter_profiles.check_profile_format("CAPTIONS_ONLY", captions, fmt)
    for fmt in ("MP4", "MOV", "MP3", "JSON", "OTIO"):
        with pytest.raises(ValueError, match="CAPTIONS_ONLY supports only"):
            exporter_profiles.check_profile_format("CAPTIONS_ONLY", captions, fmt)

    # a video preset may not name a codec the container cannot carry
    yt = exporter_profiles.preset_config("YOUTUBE_1080P")
    with pytest.raises(ValueError, match="not valid for WebM"):
        exporter_profiles.check_profile_format("YOUTUBE_1080P", yt, "WebM")
    exporter_profiles.check_profile_format("YOUTUBE_1080P", yt, "MP4")


def test_seeded_builtins_are_global_and_idempotent():
    from app.db import session_scope
    from app.models import ExportProfile

    first = _seed_builtins()
    second = _seed_builtins()
    assert first == second  # idempotent: no duplicate rows
    with session_scope() as s:
        rows = s.query(ExportProfile).all()
        assert len(rows) == len(exporter_profiles.PRESETS)
        assert all(r.workspace_id is None and r.is_builtin for r in rows)


def test_watermark_falls_back_to_brand_then_empty():
    from app.db import session_scope

    ws_id, _user_id = _seed_workspace()
    enabled = {"watermark": {"enabled": True, "text": "EXPLICIT"}}
    with session_scope() as s:
        # an explicit text always wins
        assert exporter_profiles.watermark_text(s, ws_id, enabled) == "EXPLICIT"
        # disabled -> no watermark at all
        assert exporter_profiles.watermark_text(
            s, ws_id, {"watermark": {"enabled": False}}) == ""
        # enabled with no text and no BrandDNA row degrades to empty
        assert exporter_profiles.watermark_text(
            s, ws_id, {"watermark": {"enabled": True}}) == ""
        # a BrandDNA effective snapshot supplies the brand name
        from app.models import BrandEffectiveConfig

        s.add(BrandEffectiveConfig(workspace_id=ws_id,
                                  effective_json={"effective": {"brand_name": "ACME"}}))
        s.flush()
        assert exporter_profiles.watermark_text(
            s, ws_id, {"watermark": {"enabled": True}}) == "ACME"


# ---------------------------------------------------------------------------
# format registry honesty
# ---------------------------------------------------------------------------


def test_list_formats_is_honest_for_every_entry():
    entries = exporter_formats.list_formats()
    assert [e["format"] for e in entries] == list(exporter_formats.ALL_FORMATS)
    for entry in entries:
        assert isinstance(entry["available"], bool)
        if not entry["available"]:
            # an unavailable format MUST say why -- never a silent false
            assert entry["reason"], entry
            assert len(entry["reason"]) > 10
        if entry["format"] in exporter_formats.TEXT_FORMATS:
            assert entry["available"] is True  # stdlib-only, always available


def test_nle_formats_are_honestly_unavailable():
    for fmt in ("PREMIERE", "RESOLVE"):
        available, reason = exporter_formats.get_format(fmt).available()
        assert available is False
        assert "no verified" in reason
        # the registry's own gate refuses BEFORE any bytes are written
        with pytest.raises(ValueError, match="not available"):
            exporter_formats.require_available(fmt)


def test_unsupported_nle_export_writes_no_file(tmp_path):
    """Premiere/Resolve must not produce a plausible-looking XML file."""
    out = tmp_path / "premiere.xml"
    ctx = exporter_formats.ExportContext(
        fmt="PREMIERE", out_path=out, doc={"tracks": [], "duration_seconds": 1.0},
        config={}, workspace_id="ws")
    with pytest.raises(ValueError, match="not available"):
        exporter_formats.require_available("PREMIERE")
    with pytest.raises(ValueError, match="not available"):
        exporter_formats.FORMAT_REGISTRY["PREMIERE"].export(ctx)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_otio_family_export_agrees_with_the_adapter():
    from app.engine import otio_adapter

    spec = exporter_formats.get_format("FCPXML")
    available, reason = spec.available()
    expected = otio_adapter.export_formats()["fcpxml"]
    assert available is (expected["status"] == "AVAILABLE")
    if not available:
        assert reason == expected["reason"]
    # OTIO is always available -- the otio_json adapter is in every build
    assert exporter_formats.get_format("OTIO").available() == (True, None)


def test_media_probe_reports_the_missing_encoder(monkeypatch):
    exporter_formats.reset_probe_cache()
    monkeypatch.setattr(exporter_formats, "_ffmpeg_encoders",
                        lambda: frozenset({"libx264", "aac"}))
    try:
        assert exporter_formats.probe_media_format("MP4") == (True, None)
        available, reason = exporter_formats.probe_media_format("WebM")
        assert available is False and "libvpx" in reason
        available, reason = exporter_formats.probe_media_format("WAV")
        assert available is False and "pcm_s16le" in reason
    finally:
        exporter_formats.reset_probe_cache()


def test_missing_ffmpeg_is_reported_not_assumed(monkeypatch):
    exporter_formats.reset_probe_cache()
    monkeypatch.setattr(exporter_formats.shutil, "which", lambda _n: None)
    try:
        for fmt in exporter_formats.MEDIA_FORMATS:
            available, reason = exporter_formats.probe_media_format(fmt)
            assert available is False
            assert "ffmpeg" in reason
    finally:
        exporter_formats.reset_probe_cache()


def test_format_lookup_is_case_insensitive():
    for text in ("WebM", "webm", "WEBM"):
        assert exporter_formats.get_format(text).name == "WebM"
        assert exporter_formats.FORMAT_SUFFIX[exporter_formats.get_format(text).name] == ".webm"
    with pytest.raises(ValueError, match="unknown format"):
        exporter_formats.get_format("AVI")


# ---------------------------------------------------------------------------
# subtitle export + parse-back roundtrip
# ---------------------------------------------------------------------------


def test_srt_export_roundtrips_through_parse_srt(tmp_path, monkeypatch):
    from app.providers.dubbing import parse_srt

    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    _export_id, _result, row = _caption_export(ws_id, user_id, "SRT")
    text = _artifact_path(tmp_path, ws_id, row["artifact_asset_id"]).read_text("utf-8")
    cues = parse_srt(text)
    assert [c.text for c in cues] == [c["text"] for c in CAPTIONS]
    assert [(c.start, c.end) for c in cues] == [(0.0, 1.5), (1.5, 3.0)]
    assert text.count("-->") == 2  # real 00:00:00,000 --> stamps


def test_vtt_export_roundtrips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    _export_id, _result, row = _caption_export(ws_id, user_id, "VTT")
    data = _artifact_path(tmp_path, ws_id, row["artifact_asset_id"]).read_bytes()
    assert data.decode("utf-8").startswith("WEBVTT")
    cues = exporter_formats.verify_vtt(data)
    assert cues == 2
    assert [c.text for c in
            exporter_formats.parse_vtt(data.decode("utf-8"))] == [
                c["text"] for c in CAPTIONS]
    names = [c["name"] for c in row["verification"]["checks"]]
    assert "text_roundtrip" in names


def test_ass_export_roundtrips_with_real_ass_timestamps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    _export_id, _result, row = _caption_export(ws_id, user_id, "ASS")
    text = _artifact_path(tmp_path, ws_id, row["artifact_asset_id"]).read_text("utf-8")
    # a real ASS file: [Script Info] + [Events] and DOT centiseconds
    # (SRT-style commas would be rejected by our own parse-back validator)
    assert "[Script Info]" in text and "[Events]" in text
    assert "00:00:00.000" in text
    assert "00:00:00,000" not in text
    cues = exporter_formats.verify_ass(text.encode("utf-8"))
    assert cues == 2
    assert [c.text for c in
            exporter_formats.parse_ass(text)] == [c["text"] for c in CAPTIONS]


def test_txt_transcript_export(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    _export_id, _result, row = _caption_export(ws_id, user_id, "TXT")
    text = _artifact_path(tmp_path, ws_id, row["artifact_asset_id"]).read_text("utf-8")
    assert text.splitlines() == [c["text"] for c in CAPTIONS]


def test_text_export_without_captions_fails_honestly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    timeline_id = _seed_timeline(ws_id, with_captions=False)
    export_id, result = _run(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT",
                             timeline_id, user_id)
    assert result["state"] == "FAILED"
    row = _read_export(export_id)
    assert "caption/text/voice track" in (row["error"] or "")
    # the export never reached persistence, so there is no verdict to claim
    assert not row["artifact_asset_id"]
    assert row["verification"] == {}


# ---------------------------------------------------------------------------
# OTIO export
# ---------------------------------------------------------------------------


def test_otio_export_artifact_parses_with_the_real_adapter(tmp_path, monkeypatch):
    import opentimelineio as otio

    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    export_id, _result = _run(ws_id, _profile_id("ARCHIVE_MASTER"), "OTIO",
                              timeline_id, user_id)
    row = _read_export(export_id)
    assert row["state"] == "COMPLETE", row["error"]
    data = _artifact_path(tmp_path, ws_id, row["artifact_asset_id"]).read_bytes()
    # the REAL adapter reads OUR bytes back -- not a hand-rolled imitation
    timeline = otio.adapters.read_from_string(data.decode("utf-8"), "otio_json")
    assert len(list(timeline.tracks)) == 1
    assert [c["name"] for c in row["verification"]["checks"]][-1] == "job_persisted"


def test_otio_roundtrip_serialized_preserves_structure():
    """roundtrip_serialized keeps the canonical structure across real OTIO IO.

    NOTE on scope: track kind, clip ids, start and duration all survive. The
    *extended* clip fields (text / volume / speed / transform) do NOT, and
    that is a pre-existing ``engine/otio_adapter.py`` limitation outside this
    lane: OTIO returns clip metadata as ``otio.core.AnyDictionary``, which is
    a MutableMapping but NOT a ``dict`` subclass, so the adapter's
    ``isinstance(extra, dict)`` guard drops every extra. This test locks the
    behaviour that IS guaranteed and the lane-X exporters never depend on the
    lossy part (``cues_from_timeline`` reads the source doc, not the OTIO
    roundtrip). Reported to the integration lane.
    """
    from app.engine.otio_adapter import roundtrip_serialized

    ws_id, _user_id = _seed_workspace()
    doc = _doc_of(ws_id, _seed_timeline(ws_id))
    back = roundtrip_serialized(doc)
    assert back["duration_seconds"] == pytest.approx(3.0)
    assert back["fps"] == pytest.approx(30.0)
    caption_tracks = [t for t in back["tracks"] if t["kind"] == "caption"]
    assert caption_tracks, back["tracks"]
    clips = caption_tracks[0]["clips"]
    assert [c["id"] for c in clips] == [c["id"] for c in CAPTIONS]
    assert [(c["start"], c["duration"]) for c in clips] == [
        (c["start"], c["duration"]) for c in CAPTIONS]


def test_json_envelope_export_carries_the_timeline(tmp_path, monkeypatch):
    import json as _json

    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, _headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    export_id, _result = _run(ws_id, _profile_id("ARCHIVE_MASTER"), "JSON",
                              timeline_id, user_id)
    row = _read_export(export_id)
    assert row["state"] == "COMPLETE", row["error"]
    payload = _json.loads(
        _artifact_path(tmp_path, ws_id, row["artifact_asset_id"]).read_text("utf-8"))
    assert payload["schema"] == "ymoney.export/1"
    assert payload["target"] == {"type": "timeline", "id": timeline_id}
    assert payload["profile"]["preset"] == "ARCHIVE_MASTER"
    assert payload["timeline"]["duration_seconds"] == 3.0


# ---------------------------------------------------------------------------
# retry / cancel
# ---------------------------------------------------------------------------


def test_retry_requires_failed_or_cancelled():
    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id)
    queued = _enqueue(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT", timeline_id,
                      user_id)
    export_id, first_job = queued["export_id"], queued["job_id"]
    assert first_job
    from app.db import session_scope

    # QUEUED is neither FAILED nor CANCELLED -> refused
    with session_scope() as s, pytest.raises(ValueError, match="only"):
        exporter_jobs.retry_export(s, ws_id, export_id)
    # run it to COMPLETE: a terminal state is still not retryable
    assert exporter_jobs.run_export_now(ws_id, export_id)["state"] == "COMPLETE"
    with session_scope() as s, pytest.raises(ValueError, match="only"):
        exporter_jobs.retry_export(s, ws_id, export_id)


def test_retry_bumps_attempt_and_changes_job_id():
    from app.db import session_scope

    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id, with_captions=False)  # fails honestly
    queued = _enqueue(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT", timeline_id,
                      user_id)
    export_id, first_job = queued["export_id"], queued["job_id"]
    assert exporter_jobs.run_export_now(ws_id, export_id)["state"] == "FAILED"

    with session_scope() as s:
        retried = exporter_jobs.retry_export(s, ws_id, export_id)
    assert retried["queued"] is True
    assert retried["attempt"] == 1
    assert retried["job_id"] != first_job
    row = _read_export(export_id)
    assert row["attempt"] == 1
    assert row["state"] == "QUEUED"
    assert row["error"] is None
    # runnable again (it fails for the same honest reason)
    assert exporter_jobs.run_export_now(ws_id, export_id)["state"] == "FAILED"


def test_retry_stops_after_five_attempts():
    from app.db import session_scope

    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id, with_captions=False)
    export_id = _enqueue(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT", timeline_id,
                         user_id)["export_id"]
    # each retry bumps the attempt and the run between them re-fails honestly
    for expected in range(1, exporter_jobs.MAX_ATTEMPTS + 1):
        assert exporter_jobs.run_export_now(ws_id, export_id)["state"] == "FAILED"
        with session_scope() as s:
            assert exporter_jobs.retry_export(s, ws_id, export_id)["attempt"] == expected
    assert _read_export(export_id)["attempt"] == exporter_jobs.MAX_ATTEMPTS
    # run it once more so the row is FAILED again, and the attempt ceiling --
    # not the state gate -- is what refuses the next retry
    assert exporter_jobs.run_export_now(ws_id, export_id)["state"] == "FAILED"
    with session_scope() as s, pytest.raises(ValueError, match="exhausted"):
        exporter_jobs.retry_export(s, ws_id, export_id)


def test_cancel_only_from_queued_or_running():
    from app.db import session_scope

    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id)
    export_id = _enqueue(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT", timeline_id,
                         user_id)["export_id"]
    with session_scope() as s:
        assert exporter_jobs.cancel_export(s, ws_id, export_id)["state"] == "CANCELLED"
    row = _read_export(export_id)
    assert row["state"] == "CANCELLED"
    assert "cancel" in (row["error"] or "").lower()
    # CANCELLED is terminal for cancel, but IS retryable
    with session_scope() as s, pytest.raises(ValueError, match="only"):
        exporter_jobs.cancel_export(s, ws_id, export_id)
    with session_scope() as s:
        assert exporter_jobs.retry_export(s, ws_id, export_id)["attempt"] == 1
    # COMPLETE is terminal for cancel
    complete_id, _result, _row = _caption_export(ws_id, user_id, "SRT")
    with session_scope() as s, pytest.raises(ValueError, match="only"):
        exporter_jobs.cancel_export(s, ws_id, complete_id)


def test_cancel_calls_jobs_cancel_job(monkeypatch):
    from app.db import session_scope
    from app.services import jobs as jobs_service

    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id)
    export_id = _enqueue(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT", timeline_id,
                         user_id)["export_id"]
    seen: list[str] = []
    original = jobs_service.cancel_job
    monkeypatch.setattr(jobs_service, "cancel_job",
                        lambda jid: (seen.append(jid), original(jid))[1])
    with session_scope() as s:
        exporter_jobs.cancel_export(s, ws_id, export_id)
    assert seen == [_read_export(export_id)["job_id"]]


def test_enqueue_uses_an_idempotency_key_per_attempt():
    from app.db import session_scope
    from app.models import Job

    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id)
    export_id = _enqueue(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT", timeline_id,
                         user_id)["export_id"]
    with session_scope() as s:
        job = s.get(Job, _read_export(export_id)["job_id"])
        assert job.type == exporter_jobs.JOB_TYPE
        assert job.idempotency_key == f"export:{export_id}:0"
        assert job.payload["export_id"] == export_id


# ---------------------------------------------------------------------------
# API: shapes, RBAC, isolation, errors
# ---------------------------------------------------------------------------


def test_formats_endpoint_is_the_only_availability_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    r = client.get(f"{_base(ws_id)}/formats", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["format"] for i in items] == list(exporter_formats.ALL_FORMATS)
    premiere = [i for i in items if i["format"] == "PREMIERE"][0]
    assert premiere["available"] is False and premiere["reason"]
    r2 = client.get(f"{_base(ws_id)}/formats?target_type=timeline", headers=headers)
    assert r2.status_code == 200
    assert [i["format"] for i in r2.json()["items"]] == [i["format"] for i in items]


def test_profiles_list_seeds_and_reports_builtins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    r = client.get(f"{_base(ws_id)}/profiles", headers=headers)
    assert r.status_code == 200, r.text
    assert {p["preset"] for p in r.json()["items"]} == set(exporter_profiles.PRESETS)
    # lazy first-GET seeding is idempotent
    r2 = client.get(f"{_base(ws_id)}/profiles", headers=headers)
    assert len(r2.json()["items"]) == len(r.json()["items"])


def test_profile_create_clone_and_builtin_update_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)

    r = client.post(f"{_base(ws_id)}/profiles",
                    json={"name": "My Tiktok", "preset": "TIKTOK"}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_builtin"] is False
    assert body["config"]["width"] == 1080
    profile_id = body["id"]

    r = client.put(f"{_base(ws_id)}/profiles/{profile_id}",
                   json={"config": {**body["config"], "width": 1440}},
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["width"] == 1440

    # an invalid override is rejected, never stored
    r = client.put(f"{_base(ws_id)}/profiles/{profile_id}",
                   json={"config": {**body["config"], "width": 0}}, headers=headers)
    assert r.status_code == 422, r.text
    assert "greater than 0" in r.json()["detail"]

    # a builtin global row is read-only -> 409 (clone the preset instead)
    builtin = [p for p in client.get(f"{_base(ws_id)}/profiles",
                                     headers=headers).json()["items"]
               if p["preset"] == "YOUTUBE_4K"][0]
    r = client.put(f"{_base(ws_id)}/profiles/{builtin['id']}",
                   json={"name": "hijack"}, headers=headers)
    assert r.status_code == 409
    assert "read-only" in r.json()["detail"]


def test_unknown_preset_is_422(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    r = client.post(f"{_base(ws_id)}/profiles",
                    json={"name": "Nope", "preset": "TIKTOK_9x16"}, headers=headers)
    assert r.status_code == 422, r.text
    assert "unknown preset" in r.json()["detail"]


def test_export_row_shape_is_locked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    r = client.post(_base(ws_id),
                    json={"profile_id": _profile_id("CAPTIONS_ONLY"),
                          "format": "SRT", "target_type": "timeline",
                          "target_id": timeline_id}, headers=headers)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["queued"] is True and created["export_id"] and created["job_id"]

    row = client.get(f"{_base(ws_id)}/{created['export_id']}",
                     headers=headers).json()
    assert set(row) == EXPORT_ROW_KEYS, sorted(set(row) ^ EXPORT_ROW_KEYS)
    assert set(row["profile"]) == {"name", "preset"}
    assert set(row["target"]) == {"type", "id"}
    assert row["state"] == "QUEUED" and row["progress"] == 0
    assert row["verification"] is None and row["artifact"] is None
    assert row["error"] is None and row["attempt"] == 0
    assert row["created_at"].endswith("Z") and row["finished_at"] is None

    listed = client.get(f"{_base(ws_id)}", headers=headers).json()["items"]
    assert listed and set(listed[0]) == EXPORT_ROW_KEYS

    # after a run the verification/artifact blocks fill in; keys stay identical
    exporter_jobs.run_export_now(ws_id, created["export_id"])
    done = client.get(f"{_base(ws_id)}/{created['export_id']}",
                      headers=headers).json()
    assert set(done) == EXPORT_ROW_KEYS
    assert done["state"] == "COMPLETE"
    assert set(done["verification"]) == {"complete", "checks"}
    assert set(done["artifact"]) == {"url", "size", "checksum"}
    assert done["artifact"]["url"].endswith(f"/{created['export_id']}/download")
    assert len(done["artifact"]["checksum"]) == 64
    assert done["finished_at"].endswith("Z")


def test_download_streams_the_verified_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    export_id, _result = _run(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT",
                              timeline_id, user_id)
    r = client.get(f"{_base(ws_id)}/{export_id}/download", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-subrip")
    assert "First spoken line" in r.text
    assert r.headers["X-Content-SHA256"] == _read_export(export_id)["checksum"]


def test_download_404_when_no_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    export_id = client.post(
        _base(ws_id),
        json={"profile_id": _profile_id("CAPTIONS_ONLY"), "format": "SRT",
              "target_type": "timeline", "target_id": timeline_id},
        headers=headers).json()["export_id"]
    r = client.get(f"{_base(ws_id)}/{export_id}/download", headers=headers)
    assert r.status_code == 404
    assert r.json()["detail"] == "artifact not found"


def test_export_rejects_mismatched_profile_and_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    r = client.post(_base(ws_id),
                    json={"profile_id": _profile_id("CAPTIONS_ONLY"),
                          "format": "MP4", "target_type": "timeline",
                          "target_id": timeline_id}, headers=headers)
    assert r.status_code == 422, r.text
    assert "CAPTIONS_ONLY supports only" in r.json()["detail"]


def test_export_rejects_audio_only_with_a_video_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    r = client.post(_base(ws_id),
                    json={"profile_id": _profile_id("AUDIO_ONLY"),
                          "format": "MP4", "target_type": "timeline",
                          "target_id": timeline_id}, headers=headers)
    assert r.status_code == 422, r.text
    assert "AUDIO_ONLY supports only" in r.json()["detail"]


def test_export_rejects_unavailable_format_with_its_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    r = client.post(_base(ws_id),
                    json={"profile_id": _profile_id("ARCHIVE_MASTER"),
                          "format": "PREMIERE", "target_type": "timeline",
                          "target_id": timeline_id}, headers=headers)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "not available" in detail and "no verified Premiere" in detail
    # nothing was queued
    assert client.get(f"{_base(ws_id)}", headers=headers).json()["items"] == []


def test_export_unknown_target_and_profile_are_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(_base(ws_id),
                    json={"profile_id": _profile_id("CAPTIONS_ONLY"),
                          "format": "SRT", "target_type": "timeline",
                          "target_id": missing}, headers=headers)
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"]
    r = client.post(_base(ws_id),
                    json={"profile_id": missing, "format": "SRT",
                          "target_type": "timeline", "target_id": timeline_id},
                    headers=headers)
    assert r.status_code == 404, r.text


def test_unsupported_target_type_is_422(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    r = client.post(_base(ws_id),
                    json={"profile_id": _profile_id("CAPTIONS_ONLY"),
                          "format": "SRT", "target_type": "campaign",
                          "target_id": timeline_id}, headers=headers)
    assert r.status_code == 422, r.text


def test_workspace_isolation_on_every_route(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_a, _user_a, headers_a = _register(client)
    ws_b, _user_b, headers_b = _register(client)
    timeline_id = _seed_timeline(ws_a)
    export_id = client.post(
        _base(ws_a),
        json={"profile_id": _profile_id("CAPTIONS_ONLY"), "format": "SRT",
              "target_type": "timeline", "target_id": timeline_id},
        headers=headers_a).json()["export_id"]
    for path, method in (("", "get"), (f"/{export_id}", "get"),
                         (f"/{export_id}/download", "get"),
                         (f"/{export_id}/retry", "post"),
                         (f"/{export_id}/cancel", "post")):
        r = getattr(client, method)(f"{_base(ws_b)}{path}", headers=headers_b)
        # cross-workspace reads as 404, NEVER 403 -- no id leaking
        expected = 200 if path == "" else 404
        assert r.status_code == expected, (path, r.status_code, r.text)
    assert client.get(f"{_base(ws_b)}", headers=headers_b).json()["items"] == []
    # and B cannot export A's timeline
    r = client.post(_base(ws_b),
                    json={"profile_id": _profile_id("CAPTIONS_ONLY"),
                          "format": "SRT", "target_type": "timeline",
                          "target_id": timeline_id}, headers=headers_b)
    assert r.status_code == 404, r.text


def test_rbac_floors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, owner_headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    body = {"profile_id": _profile_id("CAPTIONS_ONLY"), "format": "SRT",
            "target_type": "timeline", "target_id": timeline_id}

    viewer_headers, _ = _make_user(client, ws_id, "viewer")
    # viewer floor is enough for the read routes
    assert client.get(f"{_base(ws_id)}/formats",
                      headers=viewer_headers).status_code == 200
    assert client.get(f"{_base(ws_id)}/profiles",
                      headers=viewer_headers).status_code == 200
    # but creating an export needs the member floor
    assert client.post(_base(ws_id), json=body,
                       headers=viewer_headers).status_code == 403
    # and creating a profile needs the admin floor
    assert client.post(f"{_base(ws_id)}/profiles",
                       json={"name": "x", "preset": "TIKTOK"},
                       headers=viewer_headers).status_code == 403

    member_headers, _ = _make_user(client, ws_id, "member")
    assert client.post(_base(ws_id), json=body,
                       headers=member_headers).status_code == 201
    assert client.post(f"{_base(ws_id)}/profiles",
                       json={"name": "x", "preset": "TIKTOK"},
                       headers=member_headers).status_code == 403
    assert client.post(f"{_base(ws_id)}/profiles",
                       json={"name": "x", "preset": "TIKTOK"},
                       headers=owner_headers).status_code == 201


def test_project_capability_narrows_member(tmp_path, monkeypatch):
    """A project VIEWER member cannot export; EDITOR can (contracts §3)."""
    from app.db import session_scope
    from app.models import Project, ProjectMember, ProjectTarget

    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, owner_id, _owner_headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    member_headers, member_id = _make_user(client, ws_id, "member")

    with session_scope() as s:
        project = Project(workspace_id=ws_id, name="P", created_by=owner_id)
        s.add(project)
        s.flush()
        s.add(ProjectMember(project_id=project.id, user_id=owner_id, role="OWNER"))
        s.add(ProjectMember(project_id=project.id, user_id=member_id, role="VIEWER"))
        s.add(ProjectTarget(project_id=project.id, target_type="timeline",
                            target_id=timeline_id))
        s.commit()
        project_id = project.id

    body = {"profile_id": _profile_id("CAPTIONS_ONLY"), "format": "SRT",
            "target_type": "timeline", "target_id": timeline_id,
            "project_id": project_id}
    r = client.post(_base(ws_id), json=body, headers=member_headers)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "insufficient project role"

    with session_scope() as s:
        row = s.query(ProjectMember).filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_id).first()
        row.role = "EDITOR"
        s.commit()
    assert client.post(_base(ws_id), json=body,
                       headers=member_headers).status_code == 201


def test_retry_and_cancel_state_conflicts_are_409(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)
    export_id = client.post(
        _base(ws_id),
        json={"profile_id": _profile_id("CAPTIONS_ONLY"), "format": "SRT",
              "target_type": "timeline", "target_id": timeline_id},
        headers=headers).json()["export_id"]
    # a QUEUED export is not retryable and IS cancellable
    assert client.post(f"{_base(ws_id)}/{export_id}/retry",
                       headers=headers).status_code == 409
    assert client.post(f"{_base(ws_id)}/{export_id}/cancel",
                       headers=headers).status_code == 200
    assert client.post(f"{_base(ws_id)}/{export_id}/cancel",
                       headers=headers).status_code == 409
    assert client.post(f"{_base(ws_id)}/{export_id}/retry",
                       headers=headers).status_code == 200


def test_error_hygiene_generic_500(tmp_path, monkeypatch):
    """A RuntimeError behind a route is a generic 500 with no echo."""
    from app.api.v1 import exports as exports_mod

    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, _user_id, headers = _register(client)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("secret-internal-detail zzz")

    monkeypatch.setattr(exports_mod.exporter_jobs, "list_exports", _boom)
    r = client.get(f"{_base(ws_id)}", headers=headers)
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error"}
    assert "secret-internal-detail" not in r.text


def test_bootstrap_export_jobs_is_guarded_and_idempotent():
    from app.api.v1 import exports as exports_mod
    from app.services import jobs as jobs_service

    assert exporter_jobs.JOB_TYPE in jobs_service._handlers
    for _ in range(3):
        exports_mod._bootstrap_export_jobs()  # must never raise or duplicate
    assert list(jobs_service._handlers).count(exporter_jobs.JOB_TYPE) == 1


def test_verifier_wiring_is_additive():
    from app.engine.intelligence.verifier import KINDS, check_export

    assert "export" in KINDS
    # the five pre-existing kinds are still there (additive only)
    for kind in ("video", "publication", "campaign", "research", "community_reply"):
        assert kind in KINDS
    assert callable(check_export)


def test_check_export_replays_the_persisted_verdict():
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws_id, user_id = _seed_workspace()
    timeline_id = _seed_timeline(ws_id)
    export_id, _result = _run(ws_id, _profile_id("CAPTIONS_ONLY"), "SRT",
                              timeline_id, user_id)
    row = _read_export(export_id)
    assert row["state"] == "COMPLETE", row["error"]
    from app.db import session_scope
    with session_scope() as s:
        evidence = verify(s, ws_id, CompletionContract(kind="export",
                                                       subject_id=export_id))
        verified = evidence.verification_status
    assert verified == "VERIFIED", row
    # a foreign/unknown export id is BLOCKED, never verified
    with session_scope() as s:
        blocked = verify(s, ws_id, CompletionContract(kind="export",
                                                      subject_id="missing-id"))
    assert blocked.verification_status == "BLOCKED"


def test_webhook_allowlist_gained_the_export_kinds():
    from app.services.webhooks import WEBHOOK_EVENTS

    for kind in ("EXPORT_CREATED", "EXPORT_COMPLETED", "EXPORT_FAILED"):
        assert kind in WEBHOOK_EVENTS
    assert "webhook.test" in WEBHOOK_EVENTS  # additive only


def test_export_events_reach_the_activity_ledger():
    from app.db import session_scope
    from app.models import EventLog

    ws_id, user_id = _seed_workspace()
    _caption_export(ws_id, user_id, "SRT")
    with session_scope() as s:
        kinds = [row.kind for row in s.query(EventLog).filter(
            EventLog.workspace_id == ws_id).all()]
    assert "EXPORT_COMPLETED" in kinds


# ---------------------------------------------------------------------------
# SLOW: real media
# ---------------------------------------------------------------------------


def _lavfi_mp4(path: Path, *, seconds: float = 2.0):
    """Render a tiny real MP4 with the repo's ffmpeg pattern. False if absent."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-y", "-nostdin",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        capture_output=True, text=True, timeout=300, check=False)
    return proc.returncode == 0 and path.exists() and path.stat().st_size > 0


def _seed_timeline_with_source(ws_id, seconds: float = 2.0):
    """A timeline whose single clip points at a REAL lavfi MP4 on disk."""
    from app.db import session_scope
    from app.models import ContentTimeline, MediaAsset
    from app.services.storage import STORAGE_ROOT

    path = STORAGE_ROOT / ws_id / "src" / "source.mp4"
    assert _lavfi_mp4(path, seconds=seconds), "ffmpeg could not build the source"
    with session_scope() as s:
        asset = MediaAsset(workspace_id=ws_id, type="video", origin="render",
                           storage_key="src/source.mp4", mime_type="video/mp4",
                           duration_seconds=seconds, width=320, height=240,
                           frame_rate=15.0)
        s.add(asset)
        s.flush()
        doc = {
            "name": "main", "fps": 15.0, "duration_seconds": seconds,
            "aspect_ratio": "4:3",
            "tracks": [{"id": "t_video", "kind": "video", "name": "Video",
                        "clips": [{"id": "c1", "name": "clip", "start": 0.0,
                                   "duration": seconds,
                                   "source": {"asset_id": asset.id},
                                   "effects": [], "source_start": 0.0,
                                   "volume": 1.0, "speed": 1.0, "fade_in": 0.0,
                                   "fade_out": 0.0, "transform": {}, "text": {},
                                   "transition_in": "cut",
                                   "transition_out": "cut"}]}],
        }
        row = ContentTimeline(workspace_id=ws_id, name="main", fps=15.0,
                              duration_seconds=seconds, tracks_json=doc)
        s.add(row)
        s.flush()
        return row.id


def _media_profile(ws_id, user_id, name, config):
    from app.db import session_scope

    with session_scope() as s:
        row = exporter_profiles.create_profile(s, ws_id, name=name, config=config,
                                               created_by=user_id)
        s.commit()
        return row.id


MP4_CONFIG = {
    "width": 640, "height": 360, "fps": 15.0, "video_codec": "libx264",
    "bitrate_kbps": 1500, "audio_codec": "aac", "audio_bitrate_kbps": 128,
    "audio_channels": 2, "captions": {"enabled": False, "formats": []},
    "watermark": {"enabled": True, "text": "YMONEY"},
    "color": {"matrix": "bt709", "transfer": "bt709"},
}
WAV_CONFIG = {
    "width": None, "height": None, "fps": None, "video_codec": None,
    "bitrate_kbps": None, "audio_codec": None, "audio_bitrate_kbps": 192,
    "audio_channels": 2, "captions": {"enabled": False, "formats": []},
    "watermark": {"enabled": False}, "color": None,
}
# a WebM-legal profile: VP8/VP9 only (libx264 is NOT a WebM video codec)
WEBM_CONFIG = {
    "width": 320, "height": 240, "fps": 15.0, "video_codec": "libvpx",
    "bitrate_kbps": 800, "audio_codec": "libopus", "audio_bitrate_kbps": 96,
    "audio_channels": 1, "captions": {"enabled": False, "formats": []},
    "watermark": {"enabled": False}, "color": None,
}


@pytest.mark.slow
def test_real_mp4_export_is_verified_by_ffprobe(tmp_path, monkeypatch):
    """Real lavfi MP4 -> MP4 export -> ffprobe streams/duration/resolution.

    When the MP4 probe says unavailable the same test asserts the honest
    NOT_AVAILABLE 422 instead -- both branches are real assertions.
    """
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)
    timeline_id = _seed_timeline(ws_id)

    available, reason = exporter_formats.probe_media_format("MP4")
    if not available:
        r = client.post(_base(ws_id),
                        json={"profile_id": _profile_id("ARCHIVE_MASTER"),
                              "format": "MP4", "target_type": "timeline",
                              "target_id": timeline_id}, headers=headers)
        assert r.status_code == 422, r.text
        assert reason in r.json()["detail"]
        return

    timeline_id = _seed_timeline_with_source(ws_id, seconds=2.0)
    profile_id = _media_profile(ws_id, user_id, "MP4 640x360", MP4_CONFIG)
    r = client.post(_base(ws_id),
                    json={"profile_id": profile_id, "format": "MP4",
                          "target_type": "timeline", "target_id": timeline_id},
                    headers=headers)
    assert r.status_code == 201, r.text
    export_id = r.json()["export_id"]
    result = exporter_jobs.run_export_now(ws_id, export_id)
    assert result["state"] == "COMPLETE", result

    row = client.get(f"{_base(ws_id)}/{export_id}", headers=headers).json()
    assert row["state"] == "COMPLETE"
    assert row["verification"]["complete"] is True
    checks = {c["name"]: c for c in row["verification"]["checks"]}
    for name in ("file_exists", "streams_present", "duration_matches",
                 "resolution_matches", "checksum_recorded", "job_persisted"):
        assert checks[name]["passed"] is True, (name, checks[name])

    # the probe evidence is real, not asserted
    probe = _read_export(export_id)["verification"]["probe"]
    assert probe["video_streams"] == 1 and probe["audio_streams"] == 1
    assert (probe["width"], probe["height"]) == (640, 360)
    assert abs(probe["duration_seconds"] - 2.0) <= 0.5
    assert probe["codecs"]

    # the checksum is a real sha256 of the bytes on disk
    path = _artifact_path(tmp_path, ws_id,
                          _read_export(export_id)["artifact_asset_id"])
    assert path.exists() and path.stat().st_size > 0
    assert exporter_verify.sha256_file(path) == row["artifact"]["checksum"]
    assert row["artifact"]["size"] == path.stat().st_size

    # and the download route streams exactly those bytes
    dl = client.get(f"{_base(ws_id)}/{export_id}/download", headers=headers)
    assert dl.status_code == 200, dl.text
    assert dl.headers["content-type"].startswith("video/mp4")
    assert len(dl.content) == path.stat().st_size


@pytest.mark.slow
def test_second_real_format_is_probe_driven(tmp_path, monkeypatch):
    """WAV and WebM: assert the real export when the probe says available,
    otherwise assert the honest NOT_AVAILABLE path. Never a fake."""
    monkeypatch.chdir(tmp_path)
    client = _client(tmp_path, monkeypatch)
    ws_id, user_id, headers = _register(client)

    if not exporter_formats.probe_media_format("MP4")[0]:
        # no source can be built either -- assert the honest refusal instead
        timeline_id = _seed_timeline(ws_id)
        available, reason = exporter_formats.probe_media_format("WAV")
        assert available is False and "ffmpeg" in reason
        r = client.post(_base(ws_id),
                        json={"profile_id": _profile_id("AUDIO_ONLY"),
                              "format": "WAV", "target_type": "timeline",
                              "target_id": timeline_id}, headers=headers)
        assert r.status_code == 422 and reason in r.json()["detail"]
        return

    timeline_id = _seed_timeline_with_source(ws_id, seconds=1.0)
    cases = {
        "WAV": (_media_profile(ws_id, user_id, "WAV master", WAV_CONFIG), "audio"),
        "WebM": (_media_profile(ws_id, user_id, "WebM 320x240", WEBM_CONFIG),
                 "video"),
    }
    exercised = 0
    for fmt, (profile_id, kind) in cases.items():
        available, reason = exporter_formats.probe_media_format(fmt)
        r = client.post(_base(ws_id),
                        json={"profile_id": profile_id, "format": fmt,
                              "target_type": "timeline", "target_id": timeline_id},
                        headers=headers)
        if not available:
            # honest branch: 422 carrying the probe's own reason
            assert r.status_code == 422, (fmt, r.text)
            assert reason in r.json()["detail"]
            continue
        assert r.status_code == 201, (fmt, r.text)
        export_id = r.json()["export_id"]
        result = exporter_jobs.run_export_now(ws_id, export_id)
        assert result["state"] == "COMPLETE", (fmt, result)
        row = client.get(f"{_base(ws_id)}/{export_id}",
                         headers=headers).json()
        checks = {c["name"]: c for c in row["verification"]["checks"]}
        assert checks["streams_present"]["passed"] is True, (fmt, checks)
        if kind == "audio":
            assert "video=0" in checks["streams_present"]["detail"], fmt
        else:
            assert "video=1" in checks["streams_present"]["detail"], fmt
        assert len(row["artifact"]["checksum"]) == 64
        exercised += 1
    # at least one real second format must have been proven end to end
    assert exercised >= 1, "no second real media format was exercised"
