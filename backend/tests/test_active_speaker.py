"""Active-speaker mapping gates (Work 12 Lane F) -- contracts §10/§16.

Contracts §16 rows owned by this file: **active-speaker mapping** and
**unresolved speaker state**. The contract's rule is asymmetric and this file
holds it to that: a row is ``RESOLVED`` only when the evidence clears explicit
thresholds, and every other outcome is ``UNRESOLVED`` with a machine-readable
reason. Nothing here may invent a ``speaker_id``/``face_track_id`` -- the
"silently guessing active speakers" failure is asserted against directly, by
checking that an unresolved row's ids are ``None`` while the reason is present.

What is locked here:

* a clear single-face overlap resolves, and its evidence is LABELLED;
* every reason code is reachable: ``no_diarization``, ``no_face_track``,
  ``ambiguous_tie``, ``low_overlap``, ``low_confidence``;
* two faces that explain a window equally well stay unresolved -- "the bigger
  face wins" is not a rule this module has;
* motion evidence raises ``confidence`` but can NEVER settle a tie;
* ``SPEECH_ACTIVITY`` rows are VAD, not speakers;
* persistence + DTO shape, workspace isolation, and the routes.

The mapping engine is pure orchestration + measurement (no model), so it runs
in-process; only the optional motion evidence needs real media (``slow``).
"""

from __future__ import annotations

import pytest

from app.engine.intel import active_speaker as speaker
from tests import media_intel_fixtures as fx

# ---------------------------------------------------------------------------
# evidence doubles (no DB, no media): the mapper only reads times and labels
# ---------------------------------------------------------------------------


def _segment(speaker_id="SPEAKER_00", start=0.0, end=4.0, confidence=0.9, kind="SPEAKER"):
    return {"speaker_id": speaker_id, "kind": kind, "start_s": start,
            "end_s": end, "confidence": confidence}


def _face(label="FT_00", row_id="row-0", start=0.0, end=10.0, samples=None,
          truncated=False, reentry=0):
    entry = {"track_id": label, "id": row_id, "start_s": start, "end_s": end,
             "confidence_max": 0.9, "truncated": truncated,
             "reentry_count": reentry}
    if samples is not None:
        entry["sample_count"] = len(samples)
        entry["samples"] = samples
    return entry


def _only(report):
    assert len(report.rows) == 1, report.to_dict()
    return report.rows[0]


# ---------------------------------------------------------------------------
# the resolved case
# ---------------------------------------------------------------------------


def test_a_clear_single_face_overlap_resolves_with_labelled_evidence():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment(start=0.0, end=4.0)],
        tracks=[_face(start=0.0, end=10.0, samples=[0.0, 1.0, 2.0, 3.0])],
    )
    row = _only(report)

    assert row.status == speaker.STATUS_RESOLVED
    assert row.reason == ""
    assert row.speaker_id == "SPEAKER_00"
    assert row.face_track_label == "FT_00"
    assert row.face_track_id == "row-0"
    assert row.evidence == (speaker.EVIDENCE_OVERLAP,)
    assert row.metrics["overlap_ratio"] == pytest.approx(1.0, abs=1e-3)
    assert row.confidence is not None and row.confidence >= 0.55
    assert report.resolved == 1 and report.unresolved == 0
    assert report.thresholds["min_overlap"] == speaker.DEFAULT_MIN_OVERLAP


def test_a_lone_face_next_to_a_decoy_face_still_resolves():
    """Two faces on screen, but only one explains the speaker window."""
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment(start=0.0, end=4.0)],
        tracks=[_face("FT_00", "row-0", start=0.0, end=4.2),
                _face("FT_01", "row-1", start=6.0, end=10.0)],
    )
    row = _only(report)
    assert row.status == speaker.STATUS_RESOLVED
    assert row.face_track_label == "FT_00"
    assert row.metrics["runner_up_ratio"] == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# every unresolved reason
# ---------------------------------------------------------------------------


def test_no_diarization_is_unresolved_and_invents_nothing():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(segments=[], tracks=[_face()])
    row = _only(report)

    assert row.status == speaker.STATUS_UNRESOLVED
    assert row.reason == speaker.REASON_NO_DIARIZATION
    assert row.speaker_id is None
    assert row.face_track_id is None and row.face_track_label is None
    assert row.confidence is None
    assert report.unresolved == 1 and report.resolved == 0


def test_no_face_track_is_unresolved_for_every_speaker_window():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment("SPEAKER_00", 0.0, 2.0), _segment("SPEAKER_01", 3.0, 5.0)],
        tracks=[],
    )
    assert len(report.rows) == 2
    for row in report.rows:
        assert row.status == speaker.STATUS_UNRESOLVED
        assert row.reason == speaker.REASON_NO_FACE_TRACK
        assert row.face_track_id is None and row.face_track_label is None
        assert row.confidence is None
        # the speaker itself is known -- only the face is missing
        assert row.speaker_id in {"SPEAKER_00", "SPEAKER_01"}


def test_two_faces_explaining_a_window_equally_stay_ambiguous():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment(start=0.0, end=10.0)],
        tracks=[_face("FT_00", "row-0", 0.0, 6.0), _face("FT_01", "row-1", 0.0, 6.2)],
    )
    row = _only(report)

    assert row.status == speaker.STATUS_UNRESOLVED
    assert row.reason == speaker.REASON_AMBIGUOUS_TIE
    # a tie must NOT pick a winner, and certainly not the bigger face
    assert row.face_track_id is None and row.face_track_label is None
    assert row.metrics["margin"] <= speaker.DEFAULT_TIE_MARGIN
    assert row.metrics["runner_up_ratio"] > 0.5


def test_nothing_on_screen_is_low_overlap_not_a_guess():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment(start=0.0, end=4.0)],
        tracks=[_face("FT_00", "row-0", start=8.0, end=10.0)],
    )
    row = _only(report)
    assert row.status == speaker.STATUS_UNRESOLVED
    assert row.reason == speaker.REASON_LOW_OVERLAP
    assert row.face_track_id is None
    assert row.confidence is None
    assert row.metrics["overlap_ratio"] == pytest.approx(0.0, abs=1e-6)


def test_the_overlap_floor_is_judged_on_the_best_available_support():
    """A face on camera for the whole speaker turn resolves even when the coarse
    track window only covers part of it (sample coverage is the finer view)."""
    mapper = speaker.ActiveSpeakerMapper()
    row = _only(mapper.map(
        segments=[_segment(start=0.0, end=10.0)],
        tracks=[_face("FT_00", "row-0", 0.0, 4.0)],   # window covers 40 %
        samples=[{"track_id": "row-0", "track_label": "FT_00", "t_s": t}
                 for t in (0.0, 0.5, 1.0)],          # every sample is in-turn
    ))
    assert row.metrics["overlap_ratio"] == pytest.approx(0.4, abs=1e-3)
    assert row.status == speaker.STATUS_RESOLVED
    assert row.confidence is not None and row.confidence >= 0.55


def test_thin_evidence_above_the_overlap_floor_is_low_confidence():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment(start=0.0, end=10.0)],
        tracks=[_face("FT_00", "row-0", start=0.0, end=4.5)],
    )
    row = _only(report)
    assert row.status == speaker.STATUS_UNRESOLVED
    assert row.reason == speaker.REASON_LOW_CONFIDENCE
    assert row.face_track_id is None
    assert 0.35 <= row.confidence < speaker.DEFAULT_MIN_CONFIDENCE, row.confidence
    assert row.metrics["overlap_ratio"] == pytest.approx(0.45, abs=1e-3)


def test_every_reason_code_in_the_contract_is_reachable():
    assert set(speaker.UNRESOLVED_REASONS) == {
        "no_diarization", "no_face_track", "ambiguous_tie", "low_overlap",
        "low_confidence",
    }


# ---------------------------------------------------------------------------
# motion: evidence, never a decision
# ---------------------------------------------------------------------------


def test_motion_lifts_confidence_but_never_settles_a_tie():
    mapper = speaker.ActiveSpeakerMapper()
    segments = [_segment(start=0.0, end=10.0)]
    tracks = [_face("FT_00", "row-0", 0.0, 6.0), _face("FT_01", "row-1", 0.0, 6.2)]

    quiet = _only(mapper.map(segments=segments, tracks=tracks))
    loud = _only(mapper.map(segments=segments, tracks=tracks,
                            motion_energy=[0.05] * 40))

    assert quiet.status == loud.status == speaker.STATUS_UNRESOLVED
    assert quiet.reason == loud.reason == speaker.REASON_AMBIGUOUS_TIE
    assert loud.face_track_id is None
    assert loud.confidence > quiet.confidence, (quiet.confidence, loud.confidence)
    assert speaker.EVIDENCE_MOTION in loud.evidence
    assert speaker.EVIDENCE_MOTION not in quiet.evidence
    assert loud.metrics["motion_support"] > quiet.metrics["motion_support"]


def test_a_resolved_row_reports_motion_as_extra_evidence():
    mapper = speaker.ActiveSpeakerMapper()
    row = _only(mapper.map(
        segments=[_segment(start=0.0, end=4.0)],
        tracks=[_face(start=0.0, end=6.0)],
        motion_energy=[0.02] * 40,
    ))
    assert row.status == speaker.STATUS_RESOLVED
    assert set(row.evidence) == {speaker.EVIDENCE_OVERLAP, speaker.EVIDENCE_MOTION}
    assert row.metrics["motion_support"] > 0.0


def test_requesting_motion_that_cannot_be_measured_warns_instead_of_inventing():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment(start=0.0, end=4.0)],
        tracks=[_face(start=0.0, end=10.0)],
        use_motion=True,
        media_path=None,   # nothing to measure
    )
    row = _only(report)
    assert report.motion_measured is False
    assert any("motion evidence unavailable" in w for w in report.warnings), report.warnings
    assert speaker.EVIDENCE_MOTION not in row.evidence
    # the overlap evidence alone still resolves it -- motion was optional
    assert row.status == speaker.STATUS_RESOLVED


# ---------------------------------------------------------------------------
# interop with Lane E's face tracker (the three facts)
# ---------------------------------------------------------------------------


def test_sample_geometry_is_pixel_space_and_never_compared():
    """Lane E stores boxes in SOURCE pixels, not normalised -- so the mapper
    must not consume them at all. Identical timings with absurd geometry must
    give a byte-identical decision."""
    mapper = speaker.ActiveSpeakerMapper()
    segments = [_segment(start=0.0, end=4.0)]
    track = _face("FT_00", "row-0", 0.0, 10.0)

    def sample(x, y, w, h, t):
        return {"track_id": "row-0", "track_label": "FT_00", "t_s": t,
                "x": x, "y": y, "w": w, "h": h, "confidence": 0.9}

    normalised = [sample(0.10, 0.10, 0.20, 0.20, t) for t in (0.0, 2.0, 4.0)]
    pixels = [sample(64, 36, 128, 72, t) for t in (0.0, 2.0, 4.0)]
    offscreen = [sample(-9000, -9000, 1, 1, t) for t in (0.0, 2.0, 4.0)]

    a = _only(mapper.map(segments=segments, tracks=[track], samples=normalised))
    b = _only(mapper.map(segments=segments, tracks=[track], samples=pixels))
    c = _only(mapper.map(segments=segments, tracks=[track], samples=offscreen))
    assert a.to_dict() == b.to_dict() == c.to_dict()
    assert a.status == speaker.STATUS_RESOLVED


def test_samples_indexed_by_track_label_still_give_coverage():
    """Lane E's sample rows carry BOTH the row id (FK) and ``track_label``."""
    mapper = speaker.ActiveSpeakerMapper()
    row = _only(mapper.map(
        segments=[_segment(start=0.0, end=10.0)],
        # a track whose coarse window covers nothing, so only samples can help
        tracks=[_face("FT_00", "row-0", 0.0, 0.0)],
        samples=[{"track_id": "row-0", "track_label": "FT_00", "t_s": t}
                 for t in (1.0, 2.0, 3.0, 4.0)],
    ))
    assert row.metrics["overlap_ratio"] == pytest.approx(0.4, abs=1e-3)
    assert row.status == speaker.STATUS_RESOLVED

    # and the same rows addressed by LABEL only (no row id) behave identically
    by_label = _only(mapper.map(
        segments=[_segment(start=0.0, end=10.0)],
        tracks=[_face("FT_00", "row-0", 0.0, 0.0)],
        samples=[{"track_id": None, "track_label": "FT_00", "t_s": t}
                 for t in (1.0, 2.0, 3.0, 4.0)],
    ))
    assert by_label.to_dict() == row.to_dict()


def test_unresolved_crossing_is_provenance_not_a_status_input():
    """It has NO column: it arrives as track labels from the run manifest."""
    mapper = speaker.ActiveSpeakerMapper()
    segments = [_segment(start=0.0, end=4.0)]
    tracks = [_face("FT_00", "row-0", 0.0, 10.0, truncated=True, reentry=2)]

    clean = _only(mapper.map(segments=segments, tracks=tracks))
    flagged = _only(mapper.map(segments=segments, tracks=tracks,
                               unresolved_crossings=["FT_00"]))

    # the decision is unchanged -- contracts §10 defines no threshold for it
    assert clean.status == flagged.status == speaker.STATUS_RESOLVED
    assert clean.face_track_id == flagged.face_track_id
    assert clean.confidence == flagged.confidence
    # but the provenance is visible on the row
    assert flagged.metrics["unresolved_crossings"] == ["FT_00"]
    assert flagged.metrics["best_track_crossing"] is True
    assert flagged.metrics["best_track_truncated"] is True
    assert flagged.metrics["best_track_reentries"] == 2
    assert clean.metrics["best_track_crossing"] is False


def test_a_track_row_is_never_read_as_if_it_had_a_crossing_column():
    """Regression guard: ``unresolved_crossing`` is not a column, so a dict that
    carries the key must not change the mapper's decision."""
    mapper = speaker.ActiveSpeakerMapper()
    base = _face("FT_00", "row-0", 0.0, 10.0)
    liar = {**base, "unresolved_crossing": True}
    a = _only(mapper.map(segments=[_segment(start=0.0, end=4.0)], tracks=[base]))
    b = _only(mapper.map(segments=[_segment(start=0.0, end=4.0)], tracks=[liar]))
    assert a.to_dict() == b.to_dict()


def test_face_track_crossings_reads_the_run_manifest_only(db_session, workspace_with_user):
    from app.models import MediaAsset
    from app.services import media_intel_runs as runs

    ws = workspace_with_user["workspace"]
    asset = MediaAsset(workspace_id=ws, type="video", origin="upload",
                       storage_key="in.mp4", checksum="sum-x")
    db_session.add(asset)
    db_session.flush()
    dto = runs.create_run(db_session, ws, kind="face_tracking", asset=asset,
                          provider_key="mediapipe_faces", model_version="0.10",
                          params={"fps": 2})
    run = runs.get_run(db_session, ws, dto["id"])
    assert speaker.face_track_crossings(run) == ()

    run.metrics_json = {"unresolved_crossings": ["FT_02", "FT_05"], "tracks": 3}
    db_session.flush()
    assert speaker.face_track_crossings(run) == ("FT_02", "FT_05")

    # a malformed manifest is never read as "no crossings" silently: it stays empty
    run.metrics_json = {"unresolved_crossings": "nonsense"}
    db_session.flush()
    assert speaker.face_track_crossings(run) == ("nonsense",)


def test_crossings_surface_in_the_report_and_the_run_metrics(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _asset(client, ws_id, headers)
    _seed_evidence(ws_id, asset_id, crossings=["FT_00"])

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker",
                           json={"asset_id": asset_id}, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["unresolved_crossings"] == ["FT_00"]
    assert payload["items"][0]["metrics"]["best_track_crossing"] is True
    assert payload["run"]["metrics"]["face_unresolved_crossings"] == ["FT_00"]
    assert any("unresolved crossing" in w for w in payload["warnings"]), payload["warnings"]


def test_speech_activity_rows_are_not_speakers():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[{"speaker_id": None, "kind": "SPEECH_ACTIVITY",
                   "start_s": 0.0, "end_s": 3.0, "confidence": 0.5}],
        tracks=[_face()],
    )
    row = _only(report)
    assert row.reason == speaker.REASON_NO_DIARIZATION
    assert report.resolved == 0


def test_zero_length_and_inverted_segments_are_dropped():
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment("SPEAKER_00", 2.0, 2.0), _segment("SPEAKER_01", 5.0, 4.0)],
        tracks=[_face()],
    )
    assert _only(report).reason == speaker.REASON_NO_DIARIZATION


def test_mapping_is_deterministic_for_equal_support():
    mapper = speaker.ActiveSpeakerMapper()
    segments = [_segment(start=0.0, end=4.0)]
    tracks = [_face("FT_00", "row-0", 0.0, 8.0), _face("FT_01", "row-1", 0.0, 8.0)]
    first = mapper.map(segments=segments, tracks=tracks).to_dict()
    second = mapper.map(segments=segments, tracks=list(reversed(tracks))).to_dict()
    assert first == second
    assert first["items"][0]["reason"] == speaker.REASON_AMBIGUOUS_TIE


# ---------------------------------------------------------------------------
# persistence + DTO shape
# ---------------------------------------------------------------------------


def _seed_workspace(db_session, workspace_with_user):
    from app.models import MediaAsset
    from app.services import media_intel_runs as runs

    ws = workspace_with_user["workspace"]
    asset = MediaAsset(workspace_id=ws, type="audio", origin="upload",
                       storage_key="in.wav", checksum="sum-speaker")
    db_session.add(asset)
    db_session.flush()
    dto = runs.create_run(db_session, ws, kind="active_speaker", asset=asset,
                          provider_key="active_speaker_mapper", model_version="")
    run = runs.get_run(db_session, ws, dto["id"])
    db_session.commit()
    return ws, asset.id, run.id


def _seed_face_track(db_session, workspace_id, asset_id, label="FT_00",
                     start=0.0, end=4.2) -> str:
    """A real ``face_tracks`` row, so a RESOLVED row can satisfy the FK."""
    from app.models import FaceTrack, MediaAsset
    from app.services import media_intel_runs as runs

    asset = db_session.get(MediaAsset, asset_id)
    dto = runs.create_run(db_session, workspace_id, kind="face_tracking", asset=asset,
                          provider_key="mediapipe_faces", model_version="0.10",
                          params={"fps": 2, "label": label})
    run = runs.get_run(db_session, workspace_id, dto["id"])
    track = FaceTrack(run_id=run.id, workspace_id=workspace_id, asset_id=asset_id,
                      track_id=label, start_s=start, end_s=end, confidence_max=0.9,
                      sample_count=3)
    db_session.add(track)
    db_session.flush()
    return track.id


def test_report_persists_rows_with_no_ids_when_unresolved(db_session, workspace_with_user):
    ws, asset_id, run_id = _seed_workspace(db_session, workspace_with_user)
    track_row = _seed_face_track(db_session, ws, asset_id)
    mapper = speaker.ActiveSpeakerMapper()
    report = mapper.map(
        segments=[_segment("SPEAKER_00", 0.0, 4.0), _segment("SPEAKER_01", 5.0, 9.0)],
        tracks=[_face("FT_00", track_row, 0.0, 4.2), _face("FT_01", "row-1", 8.0, 10.0)],
    )
    stored = speaker.persist_report(db_session, workspace_id=ws, run_id=run_id,
                                    asset_id=asset_id, report=report)
    db_session.commit()
    assert len(stored) == 2

    rows = speaker.list_rows(db_session, ws, run_id)
    assert len(rows) == 2
    first, second = rows
    assert first.status == speaker.STATUS_RESOLVED and first.reason == ""
    assert first.speaker_id == "SPEAKER_00"
    assert first.confidence >= speaker.DEFAULT_MIN_CONFIDENCE
    # the resolved row points at the face track ROW (the FK contract)
    assert first.face_track_id is not None
    assert second.status == speaker.STATUS_UNRESOLVED
    assert second.reason == speaker.REASON_LOW_OVERLAP
    assert second.face_track_id is None and second.speaker_id == "SPEAKER_01"

    for row in rows:
        dto = speaker.row_dto(row)
        assert set(dto) == {
            "id", "run_id", "workspace_id", "asset_id", "speaker_id", "face_track_id",
            "start_s", "end_s", "confidence", "status", "reason",
        }
        assert dto["status"] in speaker.ACTIVE_SPEAKER_STATUSES
        if dto["status"] == speaker.STATUS_UNRESOLVED:
            assert dto["reason"] in speaker.UNRESOLVED_REASONS
            assert dto["face_track_id"] is None

    # no sensitive vocabulary anywhere in the response (contracts §0)
    payload = repr([speaker.row_dto(r) for r in rows]).lower()
    for word in ("gender", "female", "male", "age", "race", "identity", "name"):
        assert word not in payload


def test_active_speaker_rows_are_workspace_scoped(db_session, workspace_with_user):
    ws, asset_id, run_id = _seed_workspace(db_session, workspace_with_user)
    track_row = _seed_face_track(db_session, ws, asset_id)
    report = speaker.ActiveSpeakerMapper().map(
        segments=[_segment(start=0.0, end=4.0)], tracks=[_face("FT_00", track_row)]
    )
    speaker.persist_report(db_session, workspace_id=ws, run_id=run_id,
                           asset_id=asset_id, report=report)
    db_session.commit()

    assert speaker.list_rows(db_session, ws, run_id)
    assert speaker.list_rows(db_session, "ws-someone-else", run_id) == []
    assert speaker.load_diarization(db_session, "ws-someone-else", run_id) == []
    assert speaker.load_face_tracks(db_session, "ws-someone-else", run_id) == ([], [])
    assert speaker.latest_run_of_kind(db_session, "ws-someone-else", asset_id,
                                      "active_speaker") is None


# ---------------------------------------------------------------------------
# routes (contracts §14)
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.api.v1.media_intel_visual import visual_router
    from app.main import create_app

    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    if not any(p.endswith("/media-intel/active-speaker") for p in paths):
        app.include_router(visual_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _register(client):
    import uuid

    email = f"asr{uuid.uuid4().hex[:8]}@test.local"
    response = client.post("/api/v1/auth/register",
                           json={"email": email, "password": "supersecret123"})
    assert response.status_code == 200, response.text
    data = response.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _asset(client, ws_id, headers, key="in.mp3"):
    response = client.post(
        f"/api/v1/workspaces/{ws_id}/assets/media",
        json={"type": "audio", "origin": "upload", "storage_key": key,
              "mime_type": "audio/mpeg"},
        headers=headers,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def test_active_speaker_route_reports_unresolved_without_diarization(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _asset(client, ws_id, headers)

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker",
                           json={"asset_id": asset_id}, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["run"]["kind"] == "active_speaker"
    assert payload["run"]["status"] == "COMPLETED"
    assert payload["items"]
    item = payload["items"][0]
    assert item["status"] == "UNRESOLVED" and item["reason"] == "no_diarization"
    assert item["speaker_id"] is None and item["face_track_id"] is None
    assert payload["evidence_sources"] == ["overlap"]

    listing = client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker/{payload['run']['id']}",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.json()["unresolved"] == len(listing.json()["items"])
    assert client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker/missing",
        headers=headers,
    ).status_code == 404


def test_active_speaker_route_resolves_when_the_evidence_is_there(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _asset(client, ws_id, headers)
    _seed_evidence(ws_id, asset_id)

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker",
                           json={"asset_id": asset_id}, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    items = payload["items"]
    assert items and items[0]["status"] == "RESOLVED"
    assert items[0]["speaker_id"] == "SPEAKER_00"
    assert items[0]["face_track_id"]
    assert payload["thresholds"]["min_overlap"] == speaker.DEFAULT_MIN_OVERLAP

    # and the GET reads the persisted rows back
    listing = client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker/{payload['run']['id']}",
        headers=headers,
    ).json()
    assert listing["resolved"] == 1
    assert listing["items"][0]["face_track_id"] == items[0]["face_track_id"]


def test_active_speaker_route_404s_a_foreign_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    ws_b, headers_b = _register(client)
    asset_b = _asset(client, ws_b, headers_b)

    run_b = client.post(f"/api/v1/workspaces/{ws_b}/media-intel/active-speaker",
                        json={"asset_id": asset_b}, headers=headers_b).json()["run"]
    assert client.get(
        f"/api/v1/workspaces/{ws_a}/media-intel/active-speaker/{run_b['id']}",
        headers=headers_a,
    ).status_code == 404
    denied = client.post(f"/api/v1/workspaces/{ws_b}/media-intel/active-speaker",
                         json={"asset_id": asset_b}, headers=headers_a)
    assert denied.status_code in (403, 404)


def test_active_speaker_route_rejects_a_foreign_source_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    ws_b, headers_b = _register(client)
    asset_a = _asset(client, ws_a, headers_a)
    asset_b = _asset(client, ws_b, headers_b)
    run_b = _seed_evidence(ws_b, asset_b)

    response = client.post(
        f"/api/v1/workspaces/{ws_a}/media-intel/active-speaker",
        json={"asset_id": asset_a, "diarization_run_id": run_b["diarization"]},
        headers=headers_a,
    )
    assert response.status_code == 404, response.text


def _seed_evidence(workspace_id: str, asset_id: str,
                   crossings: list[str] | None = None) -> dict:
    """Write a COMPLETED diarization + face_tracking run for one asset.

    ``crossings`` goes into the face run's ``metrics_json`` exactly the way Lane
    E persists it -- there is no column for it.
    """
    from app.db import session_scope
    from app.models import DiarizationSegment, FaceTrack, FaceTrackSample
    from app.services import media_intel_runs as runs

    with session_scope() as db:
        from app.models import MediaAsset

        asset = db.get(MediaAsset, asset_id)
        diar = runs.create_run(db, workspace_id, kind="diarization", asset=asset,
                               provider_key="pyannote_diarization", model_version="3.1",
                               params={"seed": 1})
        diar_row = runs.get_run(db, workspace_id, diar["id"])
        runs.start_run(db, diar_row)
        db.add(DiarizationSegment(run_id=diar_row.id, workspace_id=workspace_id,
                                  asset_id=asset_id, speaker_id="SPEAKER_00",
                                  kind="SPEAKER", start_s=0.0, end_s=4.0, confidence=0.9))
        runs.complete_run(db, diar_row, metrics={"segments": 1})

        face = runs.create_run(db, workspace_id, kind="face_tracking", asset=asset,
                               provider_key="mediapipe_faces", model_version="0.10",
                               params={"fps": 2})
        face_row = runs.get_run(db, workspace_id, face["id"])
        runs.start_run(db, face_row)
        track = FaceTrack(run_id=face_row.id, workspace_id=workspace_id,
                          asset_id=asset_id, track_id="FT_00", start_s=0.0, end_s=4.2,
                          confidence_max=0.9, sample_count=3)
        db.add(track)
        db.flush()
        for index, t_s in enumerate((0.0, 2.0, 4.0)):
            db.add(FaceTrackSample(run_id=face_row.id, workspace_id=workspace_id,
                                   track_id=track.id, track_label="FT_00", t_s=t_s,
                                   x=10.0, y=10.0, w=40.0, h=40.0, confidence=0.9))
        runs.complete_run(db, face_row, metrics={
            "tracks": 1,
            "unresolved_crossings": list(crossings or []),
        })
        return {"diarization": diar_row.id, "face_tracking": face_row.id}


# ---------------------------------------------------------------------------
# slow: real motion evidence measured from real media
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not fx.fixture_paths_available(), reason="ffmpeg not installed")
def test_motion_energy_is_measured_from_real_frames(tmp_path):
    mapper = speaker.ActiveSpeakerMapper()
    moving = fx.test_pattern_mp4(tmp_path / "moving.mp4", seconds=2.0, fps=10)
    still = fx.test_pattern_mp4(tmp_path / "still.mp4", seconds=2.0, fps=10,
                                moving_box=False)

    energy, reason = mapper.measure_motion(moving, fps=4)
    assert energy and not reason
    assert all(0.0 <= value <= 1.0 for value in energy)

    quiet, quiet_reason = mapper.measure_motion(still, fps=4)
    assert quiet and not quiet_reason
    assert max(quiet) == 0.0, "a static clip has no frame-difference energy"

    # and it flows into the mapping as labelled evidence
    report = mapper.map(
        segments=[_segment(start=0.0, end=1.5)],
        tracks=[_face(start=0.0, end=6.0)],
        use_motion=True,
        media_path=moving,
    )
    row = _only(report)
    assert report.motion_measured is True
    assert speaker.EVIDENCE_MOTION in row.evidence
    assert row.status == speaker.STATUS_RESOLVED
    assert row.metrics["motion_support"] > 0.0
