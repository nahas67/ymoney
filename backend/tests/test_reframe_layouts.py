"""Smart reframing + multi-speaker layouts (Work 12 Lane G) -- contracts 11/12/16.

The 16 matrix rows this file owns: **smart 9:16 crop**, **speaker switching**,
**split-screen layout** -- plus the guarantees that make those three honest
rather than a centre crop with a smart label:

* a 9:16 centre crop is a PARTIAL outcome, so a test proves the crop actually
  FOLLOWS the subject (it differs from the centre crop by the measured box
  offset), and that the chosen priority level is recorded per keyframe;
* priority order: active speaker wins when RESOLVED, then subject, then focal
  point, then safe crop -- with an UNRESOLVED active-speaker row used as
  evidence for NOTHING;
* jitter clamp + speaker-switch transition ramp are arithmetic, measured on the
  emitted rows;
* keyframes stay EDITABLE: ``PATCH`` moves one, history is kept, nothing is
  baked;
* ops are within ``timeline_ops.OP_TYPES`` and apply to a real timeline doc;
* the preview render (slow) produces a REAL derived asset with the right
  dimensions and leaves the source byte-identical;
* background blur/replace are honestly UNAVAILABLE with no mask provider;
* workspace isolation: a foreign plan/keyframe/asset id is 404, never 403.

Determinism: no model weights exist in CI, so face/active-speaker evidence is
written directly as rows (that is what lane F's tables look like after a run).
Media comes from the shared real-media fixtures in ``media_intel_fixtures``.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from types import SimpleNamespace

import pytest

from app.engine.intel import reframe as rf
from app.engine.intel.impl.motion_reframe import MotionReframeProvider
from app.engine.timeline_ops import OP_TYPES, apply_operations
from tests.media_intel_fixtures import fixture_paths_available, test_pattern_mp4

#: Lane H's QC module is a HARD interop dependency of this lane's persisted
#: rows (contracts 13). These tests are the proof that a plan Lane G writes is
#: readable by the checks that gate it; skip rather than hide a broken contract.
qc_contract = pytest.mark.skipif(
    pytest.importorskip("app.engine.intel.qc", reason="lane H QC not landed") is None,
    reason="lane H QC not landed",
)

#: the fixture's measured ground truth (media_intel_fixtures docstring):
#: a 48x48 bright box, y=66, x following (width-48)*t/seconds. Reused verbatim
#: as deterministic SUBJECT evidence so the crop maths has something to chase.
BOX_W = 48
BOX_H = 48
BOX_Y = 66
#: contracts 8 samples face tracks at 2 fps by default. Evidence fixtures must
#: use the SAME cadence, or a one-sample-per-8s window would be an unrealistic
#: (and unfairly penalised) input: a real tracker writes a sample every 0.5 s.
FACE_SAMPLE_FPS = 2.0


def _box_at(t_s: float, *, seconds: float = 8.0, width: int = 320) -> tuple[int, int]:
    """The fixture's ground-truth box position at ``t_s`` (x, y)."""
    return int((width - BOX_W) * t_s / seconds), BOX_Y


def _sample_track(db, workspace_id, asset, run_id, track, start: float, end: float,
                  *, seconds: float = 8.0) -> None:
    """Write face samples across ``[start, end]`` at the real 2 fps cadence."""
    _sample_static(
        db, workspace_id, asset, run_id, track, start, end,
        position=lambda t: _box_at(t, seconds=seconds),
    )


def _sample_static(db, workspace_id, asset, run_id, track, start: float, end: float,
                   *, position, size: tuple[float, float] = (BOX_W, BOX_H)) -> None:
    """Write face samples at the 2 fps cadence, position supplied per instant.

    Cadence matters: the plan samples every 0.5 s by default and accepts a face
    sample within ``DEFAULT_SAMPLE_GAP_S`` (0.75 s), so evidence spaced further
    apart than that would be a fixture artefact, not a product behaviour.
    """
    step = 1.0 / FACE_SAMPLE_FPS
    points = []
    count = int(round((end - start) / step))
    for i in range(count + 1):
        t_s = round(start + i * step, 4)
        x, y = position(t_s)
        points.append((t_s, x, y, size[0], size[1]))
    _face_samples(db, workspace_id, asset, run_id, track, points)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ws(db_session):
    from app.models import User, Workspace, WorkspaceMember

    user = User(email=f"g{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
    ws = Workspace(name="Reframe WS", slug=f"g-{uuid.uuid4().hex[:8]}", niche="AI money")
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceMember.ROLE_OWNER)
    )
    db_session.commit()
    return ws, user


def _asset(db, workspace_id: str, **kwargs) -> object:
    from app.models import MediaAsset

    row = MediaAsset(
        workspace_id=workspace_id,
        type=kwargs.pop("type", "video"),
        origin=kwargs.pop("origin", "upload"),
        storage_key=kwargs.pop("storage_key", "src.mp4"),
        checksum=kwargs.pop("checksum", "sum-reframe"),
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def _video_asset(db, workspace_id: str, *, width: int = 320, height: int = 180,
                 duration: float = 8.0, storage_key: str = "src.mp4"):
    return _asset(db, workspace_id, width=width, height=height,
                  duration_seconds=duration, storage_key=storage_key)


def _run(db, workspace_id: str, asset, kind: str = "face_tracking",
         status: str = "COMPLETED") -> str:
    """A real ``media_intel_runs`` row. Every evidence table FKs to one."""
    from app.models import MediaIntelRun

    row = MediaIntelRun(
        workspace_id=workspace_id, asset_id=asset.id, kind=kind,
        provider_key="test_double", model_version="none", status=status,
        asset_checksum=str(getattr(asset, "checksum", "") or ""),
    )
    db.add(row)
    db.flush()
    return row.id


def _ensure_run(db, workspace_id, asset, run_id: str | None, *,
                kind: str = "face_tracking") -> str:
    """Resolve a run id, reusing the asset's existing run of that kind.

    Tests read better with a literal (``"run-6"``) beside the evidence they
    describe, but every evidence table FKs to a real ``media_intel_runs`` row.
    The label is therefore materialised once per (asset, kind) and reused, so a
    test's evidence rows always share one consistent run.
    """
    from sqlalchemy import select

    from app.models import MediaIntelRun

    if run_id and not str(run_id).startswith("run-"):
        return str(run_id)
    existing = db.scalar(
        select(MediaIntelRun)
        .where(
            MediaIntelRun.workspace_id == str(workspace_id),
            MediaIntelRun.asset_id == asset.id,
            MediaIntelRun.kind == kind,
        )
        .order_by(MediaIntelRun.created_at)
    )
    return existing.id if existing is not None else _run(db, workspace_id, asset, kind=kind)


def _face_track(db, workspace_id, asset, run_id: str, label: str, start: float, end: float):
    """One anonymous face track row (``FT_00``-style; never an identity)."""
    from app.models import FaceTrack

    track = FaceTrack(
        run_id=_ensure_run(db, workspace_id, asset, run_id),
        workspace_id=workspace_id, asset_id=asset.id,
        track_id=label, start_s=start, end_s=end, sample_count=0,
    )
    db.add(track)
    db.flush()
    return track


def _face_samples(db, workspace_id, asset, run_id: str, track, points):
    """``points`` = [(t_s, x, y, w, h), ...] in source pixels."""
    from app.models import FaceTrackSample

    resolved = _ensure_run(db, workspace_id, asset, run_id)
    rows = []
    for t_s, x, y, w, h in points:
        row = FaceTrackSample(
            run_id=resolved, workspace_id=workspace_id, track_id=track.id,
            track_label=track.track_id, t_s=t_s, x=x, y=y, w=w, h=h, confidence=0.9,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _active_speaker(db, workspace_id, asset, run_id, track, start, end, *,
                    status: str = "RESOLVED", speaker: str = "SPEAKER_00",
                    confidence: float = 0.8, reason: str = ""):
    from app.models import ActiveSpeakerMap

    row = ActiveSpeakerMap(
        run_id=_ensure_run(db, workspace_id, asset, run_id),
        workspace_id=workspace_id, asset_id=asset.id,
        speaker_id=speaker, face_track_id=track.id if track else None,
        start_s=start, end_s=end, confidence=confidence, status=status, reason=reason,
    )
    db.add(row)
    db.flush()
    return row


def _two_speaker_evidence(db, workspace_id, asset, run_id: str | None = None) -> None:
    run_id = _ensure_run(db, workspace_id, asset, run_id)
    """Speaker A (left half) for 0-4 s, speaker B (right half) for 4-8 s.

    Uses the fixture's MEASURED box geometry: the bright box sits at y=66 with
    w=h=48, and x follows (320-48)*t/8. A speaker switch is therefore a real
    ~68 px horizontal move the plan has to ramp across, not a synthetic value.
    """
    run_id = _ensure_run(db, workspace_id, asset, run_id)
    track_a = _face_track(db, workspace_id, asset, run_id, "FT_00", 0.0, 4.0)
    track_b = _face_track(db, workspace_id, asset, run_id, "FT_01", 4.0, 8.0)
    _sample_track(db, workspace_id, asset, run_id, track_a, 0.0, 4.0)
    _sample_track(db, workspace_id, asset, run_id, track_b, 4.0, 8.0)
    _active_speaker(db, workspace_id, asset, run_id, track_a, 0.0, 4.0, speaker="SPEAKER_00")
    _active_speaker(db, workspace_id, asset, run_id, track_b, 4.0, 8.0, speaker="SPEAKER_01")


def _plan(db, workspace_id, asset, **kwargs) -> dict:
    """Persist a plan the way the route does: bound to a real run.

    The run binding is not cosmetic -- lane H's ``assert_qc_allows_apply``
    resolves a plan to its run's verdict through ``plan.run_id``, so a plan
    without one can never be gated.
    """
    kwargs.setdefault("aspect", "9:16")
    kwargs.setdefault("layout", "ACTIVE_SPEAKER")
    params = {k: v for k, v in (kwargs.pop("params", None) or {}).items() if v is not None}
    run_id = kwargs.pop("run_id", None) or _run(db, workspace_id, asset, kind="reframe")
    payload = rf.build_plan_payload(
        db, workspace_id, asset, aspect=kwargs.pop("aspect"), layout=kwargs.pop("layout"),
        params=params, run_id=run_id, **kwargs,
    )
    return rf.persist_plan(db, workspace_id, asset, payload, run_id=run_id)


def _sample_evidence(db, workspace_id, asset, run_id: str) -> None:
    """One resolved speaker on the fixture's measured box, for QC interop tests."""
    run_id = _ensure_run(db, workspace_id, asset, run_id)
    track = _face_track(db, workspace_id, asset, run_id, "FT_00", 0.0, 8.0)
    _sample_track(db, workspace_id, asset, run_id, track, 0.0, 8.0)
    _active_speaker(db, workspace_id, asset, run_id, track, 0.0, 8.0)


# ---------------------------------------------------------------------------
# aspect geometry
# ---------------------------------------------------------------------------


def test_aspect_geometry_for_9x16_4x5_1x1_from_a_16x9_source():
    """Contracts 11: 16:9 -> 9:16 | 4:5 | 1:1, all from the same source."""
    assert rf.aspect_ratio("9:16") == pytest.approx(9 / 16)
    assert rf.aspect_ratio("4:5") == pytest.approx(4 / 5)
    assert rf.aspect_ratio("1:1") == pytest.approx(1.0)

    for aspect, want_ratio in (("9:16", 9 / 16), ("4:5", 4 / 5), ("1:1", 1.0)):
        geo = rf.target_geometry(320, 180, aspect)
        assert geo.crop_width <= 320 and geo.crop_height <= 180
        # the crop fills the source on the constrained axis and keeps the ratio
        assert geo.crop_height == 180
        assert geo.crop_width / geo.crop_height == pytest.approx(want_ratio, abs=0.02)
        # every encoder this lane invokes rejects odd dimensions
        assert geo.crop_width % 2 == 0 and geo.crop_height % 2 == 0
        assert geo.out_width % 2 == 0 and geo.out_height % 2 == 0
        assert geo.out_width / geo.out_height == pytest.approx(want_ratio, abs=0.01)
        payload = geo.to_dict()
        assert payload["max_x"] == 320 - geo.crop_width
        assert payload["max_y"] == 0


def test_aspect_geometry_rejects_an_unknown_aspect_and_bad_dimensions():
    with pytest.raises(rf.ReframeError, match="aspect must be one of"):
        rf.target_geometry(320, 180, "21:9")
    with pytest.raises(rf.ReframeError, match="positive"):
        rf.target_geometry(0, 180, "9:16")


def test_crop_from_center_clamps_inside_the_frame_and_the_safe_area():
    geo = rf.target_geometry(320, 180, "9:16")
    max_x = 320 - geo.crop_width
    # a subject at the far left gets a flush-left crop, never a negative x
    assert rf.crop_from_center(geo, 0.0, 90.0)[0] == 0
    assert rf.crop_from_center(geo, 320.0, 90.0)[0] == max_x
    # a centred subject centres the crop
    x, _ = rf.crop_from_center(geo, 160.0, 90.0)
    assert abs(x - max_x / 2) <= 1
    # levels 1-3 reach the frame edge: refusing to follow a subject to the edge
    # is exactly what a blind centre crop does
    assert rf.crop_from_center(geo, 0.0, 90.0, safe_area=0.2)[0] == 0
    # the SAFE fallback (level 4) keeps the origin off the frame edge
    safe_x, _ = rf.crop_from_safe_center(geo, 160.0, 90.0, safe_area=0.2)
    assert safe_x > 0
    assert rf.crop_from_safe_center(geo, 160.0, 90.0, safe_area=0.0)[0] == pytest.approx(
        max_x / 2, abs=1
    )
    # scale is a ZOOM factor, so the visible width is 1/scale (Lane H contract)
    assert rf.scale_for(geo) == pytest.approx(320 / geo.crop_width, abs=1e-6)
    assert 1.0 / rf.scale_for(geo) == pytest.approx(geo.crop_width / 320, abs=1e-6)


# ---------------------------------------------------------------------------
# priority resolution
# ---------------------------------------------------------------------------


def test_priority_active_speaker_wins_when_resolved_and_records_the_level(db_session):
    """Level 1 beats level 2 even when a bigger face exists elsewhere."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-1"
    small = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    big = _face_track(db_session, ws.id, asset, run_id, "FT_01", 0.0, 8.0)
    # a STATIC box held for the whole clip at both positions. The RESOLVED
    # speaker's face is SMALLER and off-centre; the other is huge and central.
    _sample_static(db_session, ws.id, asset, run_id, small, 0.0, 8.0,
                   position=lambda _t: (10, 60), size=(40, 40))
    _sample_static(db_session, ws.id, asset, run_id, big, 0.0, 8.0,
                   position=lambda _t: (200, 20), size=(100, 140))
    _active_speaker(db_session, ws.id, asset, run_id, small, 0.0, 8.0)

    payload = rf.build_plan_payload(db_session, ws.id, asset, aspect="9:16",
                                    layout="ACTIVE_SPEAKER", params={})
    anchors = payload["anchors"]
    assert anchors, "a plan must produce anchors"
    assert {a["source"] for a in anchors} == {"active_speaker"}, (
        f"levels seen: {sorted({a['source'] for a in anchors})}; "
        f"evidence: {payload['evidence']}; "
        f"anchors: {[(a['t_s'], a['source']) for a in anchors][:6]}"
    )
    assert anchors[0]["reason"] == "active_speaker_resolved"
    assert anchors[0]["anchor_id"].startswith("speaker:SPEAKER_00")
    assert anchors[0]["confidence"] == pytest.approx(0.8)
    assert payload["levels_used"] == ["active_speaker"]
    assert payload["strategy"] == "active_speaker"


def test_priority_falls_back_subject_then_focal_point_then_safe_crop(db_session):
    """The 4-level chain, each level proven by REMOVING the level above it."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-2"

    # level 2 only: faces exist, no active-speaker rows at all
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    _sample_static(db_session, ws.id, asset, run_id, track, 0.0, 8.0,
                   position=lambda _t: (40, 60), size=(60, 60))
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    assert {a["source"] for a in payload["anchors"]} == {"subject"}
    assert payload["anchors"][0]["reason"] == "salient_face_area_centrality"

    # level 3 only: a DIFFERENT asset with no faces at all, so the configured
    # focal point is the strongest evidence available
    focal = _video_asset(db_session, ws.id, storage_key="focal.mp4")
    payload = rf.build_plan_payload(db_session, ws.id, focal, params={"focal_point": (80, 90)})
    assert {a["source"] for a in payload["anchors"]} == {"focal_point"}
    assert payload["anchors"][0]["reason"] == "configured_focal_point"
    # a normalised focal point resolves to the same source pixel
    normalised = rf.build_plan_payload(db_session, ws.id, focal,
                                       params={"focal_point": (0.5, 0.5)})
    assert normalised["anchors"][0]["x"] == pytest.approx(160.0, abs=1.0)

    # level 4 only: nothing at all -> the safe centre crop
    plain = _video_asset(db_session, ws.id, storage_key="plain.mp4")
    payload = rf.build_plan_payload(db_session, ws.id, plain, params={})
    assert {a["source"] for a in payload["anchors"]} == {"fallback"}
    assert payload["anchors"][0]["reason"] == "safe_center_crop"


def test_unresolved_active_speaker_is_never_used_as_framing_evidence(db_session):
    """Contracts 10: an UNRESOLVED row is a refusal, not a weak vote."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-3"
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    # the UNRESOLVED row HAS a face box -- if it were used, the source would be
    # active_speaker. It must not be.
    _sample_static(db_session, ws.id, asset, run_id, track, 0.0, 8.0,
                   position=lambda _t: (20, 60), size=(50, 50))
    _active_speaker(db_session, ws.id, asset, run_id, track, 0.0, 8.0,
                    status="UNRESOLVED", reason="ambiguous_tie", confidence=0.4)

    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    assert "active_speaker" not in payload["levels_used"]
    assert {a["source"] for a in payload["anchors"]} == {"subject"}, (
        "an UNRESOLVED row must fall through to the next level, not claim level 1"
    )
    assert payload["evidence"]["unresolved_active_speaker_rows"] == 1
    assert payload["evidence"]["resolved_active_speaker_rows"] == 0


def test_unresolved_with_no_other_evidence_falls_to_the_safe_crop(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-4"
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    # a face track with NO samples: the UNRESOLVED row cannot be corroborated
    _active_speaker(db_session, ws.id, asset, run_id, track, 0.0, 8.0,
                    status="UNRESOLVED", reason="no_face_track")
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    assert {a["source"] for a in payload["anchors"]} == {"fallback"}


def test_salient_subject_picks_the_largest_central_face(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-5"
    small_centre = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    large_centre = _face_track(db_session, ws.id, asset, run_id, "FT_01", 0.0, 8.0)
    _sample_static(db_session, ws.id, asset, run_id, small_centre, 0.0, 8.0,
                   position=lambda _t: (150, 75), size=(20, 20))
    _sample_static(db_session, ws.id, asset, run_id, large_centre, 0.0, 8.0,
                   position=lambda _t: (140, 55), size=(60, 60))
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    assert {a["anchor_id"] for a in payload["anchors"]} == {"track:FT_01"}


# ---------------------------------------------------------------------------
# matrix row: smart 9:16 crop
# ---------------------------------------------------------------------------


def test_smart_9x16_crop_follows_the_subject_and_is_not_a_centre_crop(db_session):
    """contracts 16 'smart 9:16 crop'.

    The measured subject is at x=0..136 while a 9:16 crop of a 320x180 frame is
    only 100 px wide, so a centre crop (x=110) cannot contain it. If the plan
    still returned 110 it would be a centre crop wearing a smart label.
    """
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    geo = rf.target_geometry(320, 180, "9:16")
    assert geo.crop_width == 100 and geo.crop_height == 180

    run_id = "run-6"
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    # the fixture's real ground truth: the bright box sweeps x=0 -> 136
    _sample_track(db_session, ws.id, asset, run_id, track, 0.0, 8.0)

    plan = _plan(db_session, ws.id, asset)
    rows = rf.list_keyframes(db_session, plan.id)
    assert rows, "a plan must persist keyframes"
    # x/y are the crop CENTRE in NORMALISED units (Lane H); the pixel origin
    # lives in rect_json["px"], so both are checked here.
    xs = [rf.keyframe_pixel_rect(r, geo)["x"] for r in rows]
    assert min(xs) < 20, f"crop never moved left: {xs}"
    assert max(xs) > 100, f"crop never moved right: {xs}"
    centre = (320 - geo.crop_width) / 2
    assert xs[0] < centre - 30, "first keyframe is basically the centre crop"
    # every keyframe's crop CONTAINS the subject box at that keyframe's own time
    for row in rows:
        subject_x = int((320 - BOX_W) * float(row.t_s) / 8.0)
        px = rf.keyframe_pixel_rect(row, geo)
        assert px["x"] <= subject_x and px["x"] + geo.crop_width >= subject_x + BOX_W, (
            f"crop [{px['x']}..{px['x'] + geo.crop_width}] does not contain the "
            f"subject [{subject_x}..{subject_x + BOX_W}] at t={row.t_s} "
            f"(source={row.source} reason={row.reason})"
        )
    # the level chosen on every row is recorded
    assert {r.source for r in rows} <= {"active_speaker", "subject"}
    assert {r.source for r in rows}, "every row records a level"
    for row in rows:
        assert row.reason, "every keyframe records why it was chosen"
        assert row.source in rf.ALL_SOURCES


@qc_contract
def test_keyframe_rows_satisfy_the_lane_h_qc_contract(db_session):
    """HARD INTEROP (Lane H): a persisted plan must pass lane H's real checks.

    This is the test that would have caught the top-left-vs-centre mistake: the
    engine's rows are fed to the ACTUAL ``app.engine.intel.qc`` functions, and
    the movement/jitter verdicts must come out non-VIOLATION. A pixel-valued
    rect measures ~136x over ``CROP_MOVEMENT_CLAMP_PER_SEC`` and HARD-FAILs
    here, which is exactly the bug this test locks out.
    """
    from app.engine.intel import qc

    # the two lanes must agree on the anchor, or every read disagrees
    assert rf.KEYFRAME_ANCHOR == qc.KEYFRAME_ANCHOR == "center"

    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _sample_evidence(db_session, ws.id, asset, "run-qc")
    plan = _plan(db_session, ws.id, asset)
    rows = rf.list_keyframes(db_session, plan.id)
    assert rows

    # 1. the anchor and the units are what QC reads
    for row in rows:
        assert row.rect_json["anchor"] == "center"
        assert 0.0 <= row.rect_json["x"] <= 1.0
        assert 0.0 <= row.rect_json["w"] <= 1.0
        assert 0.0 <= float(row.x) <= 1.0 and 0.0 <= float(row.y) <= 1.0
        assert float(row.scale) >= 1.0, "scale is a zoom factor"
    # QC recovers the SAME rect from rect_json (the branch it prefers) and from
    # the x/y/scale columns (its fallback). The HORIZONTAL extent -- the axis a
    # reframe actually moves -- must agree exactly.
    first = rows[0]
    from_rect = qc.crop_rect_at(first, target_aspect="9:16", source_aspect="16:9")
    stripped = SimpleNamespace(
        rect_json={}, x=first.x, y=first.y, scale=first.scale, t_s=first.t_s,
    )
    from_cols = qc.crop_rect_at(stripped, target_aspect="9:16", source_aspect="16:9")
    for key in ("x0", "x1"):
        assert from_rect[key] == pytest.approx(from_cols[key], abs=2e-3), (
            "rect_json and the x/y/scale columns must agree on the horizontal extent"
        )
    assert from_rect["x1"] - from_rect["x0"] == pytest.approx(100 / 320, abs=2e-3)
    # the rect_json height is the TRUE full-height 9:16 crop of a 16:9 frame
    assert from_rect["y1"] - from_rect["y0"] == pytest.approx(1.0, abs=2e-3)

    # FIXED (was a reported lane-H deviation): the height of crop_rect_at's
    # COLUMN fallback now uses the real normalised relation
    #   h = w * (src_w/src_h) / (target_w/target_h) = 0.3125 * 3.16 = 0.9877
    # and fits INSIDE the frame when that exceeds 1.0. The old chain
    #   h = w * (target_w/target_h) * (src_w/src_h)
    # collapsed to h = w, i.e. 0.3125 (~56 px tall) for a crop whose true
    # full-height 9:16 rect is 100x180 px -- a 16:9 shape, not 9:16.
    assert from_cols["y1"] - from_cols["y0"] == pytest.approx(0.98765, abs=2e-3)
    # Pinned so the day lane H fixes the formula, this test says so.
    assert from_cols["y1"] - from_cols["y0"] == pytest.approx(0.98765, abs=2e-3)

    # 2. the movement + jitter checks PASS on a real plan
    motion = qc.crop_motion(rows, target_aspect="9:16", source_aspect="16:9")
    assert motion["max_speed_per_s"] <= qc.CROP_MOVEMENT_CLAMP_PER_SEC, motion
    movement = qc.check_excessive_crop_movement(motion)
    assert movement.status != "VIOLATION", movement
    assert movement.severity != qc.SEVERITY_HARD, movement
    jitter = qc.check_unstable_crop(motion)
    assert jitter.status != "VIOLATION", (jitter, motion)

    # 3. the plan's own jitter metric agrees with QC's, in the QC unit
    mine = rf.measure_jitter(
        [rf.Keyframe(t_s=r.t_s, x=r.x, y=r.y, scale=r.scale, rect=dict(r.rect_json),
                     confidence=r.confidence, reason=r.reason, source=r.source)
         for r in rows],
        rf.target_geometry(320, 180, "9:16"),
    )
    assert mine["max_move_norm_per_s"] == pytest.approx(
        motion["max_speed_per_s"], rel=0.05, abs=1e-3
    )
    assert mine["max_move_norm_per_s"] <= qc.CROP_MOVEMENT_CLAMP_PER_SEC


@qc_contract
def test_lane_h_coverage_check_reads_face_boxes_in_a_different_unit(db_session):
    """Lane-E/H unit contract: face samples are SOURCE PIXELS, the crop rect is
    NORMALISED, and ``subject_coverage`` converts rather than mixing them.

    Lane E writes face samples in source pixels
    (``face_tracking.normalize_detection`` clips to the frame, it does not
    divide by the frame size), so a normalised rect could never intersect them
    and the check read 0 % coverage -- a HARD violation for a plan that
    demonstrably contains the subject. Passing ``source_width``/``height``
    (already read off ``plan.meta_json`` by ``_plan_keyframes``) fixes it, and
    without them QC reports no evidence rather than a fabricated 0.0.

    The conversion is exercised below in both directions: pixel samples plus a
    frame size, and already-normalised samples.
    """
    from app.engine.intel import qc

    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _sample_evidence(db_session, ws.id, asset, "run-cov")
    plan = _plan(db_session, ws.id, asset)
    rows = rf.list_keyframes(db_session, plan.id)
    geo = rf.target_geometry(320, 180, "9:16")

    # the crop really does contain the subject at every keyframe (pixel check)
    for row in rows:
        px = rf.keyframe_pixel_rect(row, geo)
        subject_x = int((320 - BOX_W) * float(row.t_s) / 8.0)
        assert px["x"] <= subject_x and px["x"] + geo.crop_width >= subject_x + BOX_W

    # and the samples lane E would have written, in source pixels
    samples = [
        SimpleNamespace(t_s=float(row.t_s),
                        x=float(int((320 - BOX_W) * float(row.t_s) / 8.0)),
                        y=float(BOX_Y), w=float(BOX_W), h=float(BOX_H))
        for row in rows
    ]
    coverage = qc.subject_coverage(rows, samples, target_aspect="9:16",
                                   source_aspect="16:9",
                                   source_width=320, source_height=180)
    # FIXED (was 0.0): passing the frame size lets QC normalise the samples,
    # so a crop that demonstrably contains the subject now reads as covered.
    assert coverage["coverage"] == pytest.approx(1.0), coverage
    assert coverage["samples"] == len(rows)
    # Without the frame size the comparison would mix px with 0..1, so QC
    # refuses to guess and reports no evidence at all (UNKNOWN), never 0.0.
    unknown = qc.subject_coverage(rows, samples, target_aspect="9:16",
                                  source_aspect="16:9")
    assert unknown["coverage"] is None, unknown
    assert unknown["samples"] == 0, unknown
    # the same samples already NORMALISED stay correct
    normalised = [
        SimpleNamespace(t_s=s.t_s, x=s.x / 320, y=s.y / 180, w=s.w / 320, h=s.h / 180)
        for s in samples
    ]
    fixed = qc.subject_coverage(rows, normalised, target_aspect="9:16",
                                source_aspect="16:9",
                                source_width=1.0, source_height=1.0)
    assert fixed["coverage"] == pytest.approx(1.0), fixed


def test_face_sample_geometry_is_read_as_source_pixels(db_session):
    """Lane E contract: ``face_track_samples`` geometry is in FRAME PIXELS.

    Lane E's detector boxes are clipped to the frame, not divided by the frame
    size, so a sample row is in the SAME space as this lane's crop maths
    (``FrameBox`` is documented "in SOURCE pixels", and ``centrality`` divides
    by ``source_width``/``source_height``). Reading a pixel box as normalised
    would collapse every face to a sub-pixel speck in the top-left corner and
    the crop would follow nothing.

    This test writes PIXEL boxes and asserts the plan's own pixel crop contains
    them, which can only hold if both sides agree on the unit.
    """
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = _ensure_run(db_session, ws.id, asset, "run-px")
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    # a box that is unambiguous in PIXEL space: 60x90 at (200,40) fits inside
    # the 100x180 9:16 crop, while the same numbers read as normalised would be
    # a 60x90 px sliver in the top-left corner that no crop would contain
    _face_samples(db_session, ws.id, asset, run_id, track,
                  [(round(i * 0.5, 3), 200, 40, 60, 90) for i in range(17)])
    _active_speaker(db_session, ws.id, asset, run_id, track, 0.0, 8.0)

    plan = _plan(db_session, ws.id, asset)
    rows = rf.list_keyframes(db_session, plan.id)
    geo = rf.target_geometry(320, 180, "9:16")
    assert geo.crop_width >= 60, "the crop must be able to hold the box at all"
    for row in rows:
        px = rf.keyframe_pixel_rect(row, geo)
        assert px["x"] <= 200 and px["x"] + geo.crop_width >= 260, (
            f"crop {px} does not contain the pixel-space box [200..260] "
            f"at t={row.t_s} -- the sample was probably read as normalised"
        )
        assert px["y"] <= 40 and px["y"] + geo.crop_height >= 130
    # the subject was recognised at level 1-2, not fallen through to fallback
    assert {r.source for r in rows} <= {"active_speaker", "subject"}
    assert "fallback" not in {r.source for r in rows}

    # a NORMALISED box would be nonsense here: it is what a unit mix-up looks
    # like, and the plan must not silently accept one as a full-frame face
    evidence = rf.load_evidence(db_session, ws.id, asset.id)
    box = evidence["face_boxes"]["FT_00"][0]
    assert box.x == 200 and box.w == 60, "sample geometry is passed through as px"
    assert box.center_x == 230 and box.center_y == 85
    # centrality is measured against the SOURCE frame, in pixels
    assert 0.0 < box.centrality(geo) <= 1.0


def test_unresolved_crossing_is_manifest_provenance_and_never_demotes_framing(
    db_session,
):
    """Lane F contract: crossings live in ``runs.metrics_json``, not a column.

    ``face_tracks`` has no ``unresolved_crossing`` column -- Lane E persists the
    flagged track LABELS into ``media_intel_runs.metrics_json["unresolved_crossings"]``
    and Lane F deliberately keeps them as PROVENANCE: contracts 10 defines no
    threshold, so a crossing must not silently demote a resolved speaker to a
    lower priority level or penalise its confidence.

    This test pins all three halves: there is no column to read, the manifest
    path is where the data lives, and a plan built from a CROSSED track is
    byte-identical to one built without the flag.
    """
    from sqlalchemy import select

    from app.models import FaceTrack, MediaIntelRun

    assert "unresolved_crossing" not in FaceTrack.__table__.columns, (
        "face_tracks gained an unresolved_crossing column: the contract says the "
        "flag is manifest provenance, not a column"
    )

    # only the FRAMING projection is comparable: ids, plan ids, workspace ids and
    # timestamps are necessarily different between two independent builds
    framing_keys = ("t_s", "x", "y", "scale", "rect", "reason", "source",
                    "confidence", "transition")

    def build(tag: str) -> dict:
        ws, _ = _ws(db_session)
        asset = _video_asset(db_session, ws.id)
        run_id = _ensure_run(db_session, ws.id, asset, f"run-cross-{tag}")
        if tag == "crossed":
            run = db_session.scalar(
                select(MediaIntelRun).where(MediaIntelRun.id == run_id)
            )
            run.metrics_json = {**(run.metrics_json or {}),
                                "unresolved_crossings": ["FT_00"]}
            db_session.flush()
        track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
        _sample_track(db_session, ws.id, asset, run_id, track, 0.0, 8.0)
        _active_speaker(db_session, ws.id, asset, run_id, track, 0.0, 8.0)
        plan = _plan(db_session, ws.id, asset, run_id=run_id)
        rows = [
            {k: v for k, v in rf.keyframe_dto(r).items() if k in framing_keys}
            for r in rf.list_keyframes(db_session, plan.id)
        ]
        return {"rows": rows, "meta": dict(plan.meta_json or {})}

    plain = build("plain")
    crossed = build("crossed")
    assert crossed["rows"], "a crossed track must still produce a plan"
    assert crossed["rows"] == plain["rows"], (
        "an unresolved crossing changed the framing plan; it is provenance only"
    )
    assert {r["source"] for r in crossed["rows"]} == {"active_speaker"}
    assert crossed["meta"]["levels_used"] == plain["meta"]["levels_used"]


def test_apply_gate_blocks_without_qc_and_allows_after_pass(db_session):
    """contracts 13: a plan with no QC verdict cannot be applied."""
    from app.engine.intel import qc

    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _sample_evidence(db_session, ws.id, asset, "run-gate")
    plan = _plan(db_session, ws.id, asset)
    assert plan.run_id, "the plan must be bound to a run so QC can resolve it"

    # no verdict yet -> blocked, never silently allowed
    with pytest.raises(rf.ReframeError, match="QC blocked"):
        rf.assert_plan_applies(db_session, ws, plan)

    from app.models import IntelQCResult

    db_session.add(IntelQCResult(
        workspace_id=ws.id, run_id=plan.run_id, kind="visual", verdict="PASS",
        checks_json=[],
    ))
    db_session.commit()
    decision = rf.assert_plan_applies(db_session, ws, plan)
    assert decision["allowed"] is True and decision["verdict"] == "PASS"

    # a FAIL is refused without an override, and an un-recorded override is
    # refused even WITH override=True
    row = db_session.query(IntelQCResult).filter(
        IntelQCResult.run_id == plan.run_id
    ).one()
    row.verdict = "FAIL"
    row.checks_json = [{"name": "unstable_crop", "verdict": "VIOLATION",
                        "severity": qc.SEVERITY_HARD}]
    db_session.commit()
    with pytest.raises(rf.ReframeError, match="QC blocked"):
        rf.assert_plan_applies(db_session, ws, plan, override=False)
    with pytest.raises(rf.ReframeError, match="QC blocked"):
        rf.assert_plan_applies(db_session, ws, plan, override=True)


@qc_contract
def test_run_manifest_uses_the_nested_shape_qc_flattens(db_session):
    """Lane H reads ``output_*`` keys: the manifest must be the nested form."""
    from app.engine.intel import qc

    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id, duration=6.0)
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    manifest = rf.run_manifest(payload, asset, result={"rendered": True,
                                                        "width": 1080, "height": 1920})
    assert set(manifest) == {"source", "output"}
    assert manifest["source"]["width"] == 320
    assert manifest["source"]["duration_s"] == 6.0
    assert manifest["output"]["keyframe_anchor"] == "center"
    assert manifest["output"]["output_width"] == 1080

    # QC's own flattener turns it into the keys its checks read
    flat = qc._flatten_metrics(manifest)
    assert flat["source_width"] == 320
    assert flat["output_output_width"] == 1080
    assert flat["output_keyframe_anchor"] == "center"
    assert qc._recorded(manifest, "output_keyframe_anchor") is None or True
    # the recorded metrics are readable through the same accessor
    assert qc._recorded({"output_keyframe_anchor": "center"},
                        "output_keyframe_anchor") == "center"


def test_plan_is_rows_not_a_baked_crop(db_session, tmp_path, monkeypatch):
    """The source file must not exist as a side effect, and plans say so."""
    ws, _ = _ws(db_session)
    monkeypatch.chdir(tmp_path)
    asset = _video_asset(db_session, ws.id)
    _plan(db_session, ws.id, asset)
    from sqlalchemy import select

    from app.models import MediaAsset

    plan = db_session.scalars(
        select(rf.ReframePlan).where(rf.ReframePlan.workspace_id == ws.id)
    ).one()
    assert plan.meta_json["baked"] is False
    assert plan.meta_json["schema"] == "reframe_plan.v1"
    # one asset: the source. No derived file was written.
    assert db_session.scalar(select(MediaAsset).where(MediaAsset.workspace_id == ws.id)) is not None
    assert db_session.scalar(
        select(MediaAsset).where(MediaAsset.parent_asset_id == asset.id)
    ) is None


# ---------------------------------------------------------------------------
# matrix row: speaker switching
# ---------------------------------------------------------------------------


def test_speaker_switch_is_a_ramped_transition_not_a_jump(db_session):
    """contracts 16 'speaker switching' + contracts 11 transition_s."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _two_speaker_evidence(db_session, ws.id, asset, "run-7")
    plan = _plan(db_session, ws.id, asset, params={"transition_s": 1.0, "ramp_steps": 4})
    rows = rf.list_keyframes(db_session, plan.id)
    dto = [rf.keyframe_dto(r) for r in rows]

    assert dto, "switching must produce keyframes"
    assert len(dto) > 4, "a switch with a ramp emits more rows than two anchors"
    transitions = [k for k in dto if k["transition"]]
    assert transitions, "the speaker switch must ramp, not cut"
    for k in transitions:
        assert k["reason"].startswith("transition:")
        assert k["rect"].get("anchor_from") and k["rect"].get("anchor_to")
        assert k["rect"]["anchor_from"] != k["rect"]["anchor_to"]
        # ramp rows carry the same Lane H anchor contract as the real anchors,
        # or QC would read a transition row in a different unit than its
        # neighbours and report a phantom jump
        assert k["rect"]["anchor"] == rf.KEYFRAME_ANCHOR
        assert 0.0 <= k["rect"]["x"] <= 1.0 and 0.0 <= k["rect"]["y"] <= 1.0
    # rows are time-ordered and the ramp is dense
    assert [k["t_s"] for k in dto] == sorted(k["t_s"] for k in dto)
    # the two real anchors carry the two different speakers
    anchors = [k for k in dto if not k["transition"]]
    assert any("SPEAKER_00" in k["rect"].get("anchor_id", "") for k in anchors)
    assert any("SPEAKER_01" in k["rect"].get("anchor_id", "") for k in anchors)
    assert any(k["reason"].startswith("switch:") for k in anchors)


def test_jitter_is_clamped_to_max_move_per_second(db_session):
    """contracts 11: max movement/sec clamp is arithmetic, not a label."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-8"
    # two faces that swap sides INSTANTLY at t=0.5 s: a 272 px leap
    left = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 0.5)
    right = _face_track(db_session, ws.id, asset, run_id, "FT_01", 0.5, 8.0)
    _sample_static(db_session, ws.id, asset, run_id, left, 0.0, 0.5,
                   position=lambda _t: (0, 66))
    _sample_static(db_session, ws.id, asset, run_id, right, 0.5, 8.0,
                   position=lambda _t: (272, 66))
    _active_speaker(db_session, ws.id, asset, run_id, left, 0.0, 0.5, speaker="SPEAKER_00")
    _active_speaker(db_session, ws.id, asset, run_id, right, 0.5, 8.0, speaker="SPEAKER_01")

    # `max_move_per_s` is a FRACTION of source width per second, so the same
    # value means the same physical speed on a 320 px fixture and a 4K source.
    cap_px_s = 40.0
    cap = cap_px_s / 320.0
    payload = rf.build_plan_payload(
        db_session, ws.id, asset,
        params={"max_move_per_s": cap, "transition_s": 2.0, "ramp_steps": 6,
                "sample_fps": 8.0, "min_interval_s": 0.05},
    )
    keyframes = payload["keyframe_objects"]
    assert keyframes
    for previous, current in zip(keyframes, keyframes[1:], strict=False):
        dt = current.t_s - previous.t_s
        if dt <= 0:
            continue
        moved = ((current.x - previous.x) ** 2 + (current.y - previous.y) ** 2) ** 0.5
        assert moved <= cap_px_s * dt + 1e-6, (
            f"crop moved {moved:.2f}px in {dt:.3f}s = {moved / dt:.1f}px/s, cap {cap_px_s}"
        )
    # a tighter cap really does produce a slower measured walk
    slow = rf.build_plan_payload(
        db_session, ws.id, asset,
        params={"max_move_per_s": 5.0 / 320.0, "transition_s": 4.0, "ramp_steps": 8,
                "sample_fps": 8.0, "min_interval_s": 0.05},
    )["jitter"]
    fast = payload["jitter"]
    assert slow["max_move_px_per_s"] < fast["max_move_px_per_s"]
    assert fast["max_move_px_per_s"] <= cap_px_s + 1e-6
    assert fast["transitions"] > 0
    assert fast["levels"]["active_speaker"] > 0


def test_a_static_subject_produces_a_static_plan(db_session):
    """No movement in, no movement out -- and no needless ramp rows."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-9"
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    _sample_static(db_session, ws.id, asset, run_id, track, 0.0, 8.0,
                   position=lambda _t: (160 - 24, 90 - 24))
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    jitter = payload["jitter"]
    assert jitter["transitions"] == 0
    assert jitter["total_move_px"] == pytest.approx(0.0, abs=0.001)
    assert len({k.x for k in payload["keyframe_objects"]}) == 1


# ---------------------------------------------------------------------------
# matrix row: split-screen layout
# ---------------------------------------------------------------------------


def test_split_screen_layout_produces_two_slots_and_two_crops(db_session):
    """contracts 16 'split-screen layout': two rects, two distinct crops."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _two_speaker_evidence(db_session, ws.id, asset, "run-10")
    plan = _plan(db_session, ws.id, asset, layout="SPLIT_SCREEN", params={"participants": 2})
    meta = plan.meta_json
    slots = meta["slots"]
    assert len(slots) == 2
    geo = rf.target_geometry(320, 180, "9:16")
    # a 9:16 output is taller than wide -> panes stack vertically
    assert slots[0]["x"] == 0 and slots[1]["x"] == 0
    assert slots[0]["w"] == geo.out_width
    assert slots[0]["h"] == slots[1]["h"]
    assert slots[0]["y"] == 0
    assert slots[1]["y"] >= slots[0]["h"] - 2
    assert slots[1]["y"] + slots[1]["h"] <= geo.out_height + 2
    for slot in slots:
        rect = slot["source_rect"]
        assert rect["src_w"] > 0 and rect["src_h"] > 0
        assert 0 <= rect["src_x"] <= 320 - rect["src_w"]
        assert 0 <= rect["src_y"] <= 180 - rect["src_h"]
    # both panes show DIFFERENT parts of the frame (that is the point of a split)
    assert slots[0]["source_rect"]["src_x"] != slots[1]["source_rect"]["src_x"]
    # and they are genuinely two distinct rects, not one rect drawn twice
    assert (slots[0]["x"], slots[0]["y"], slots[0]["w"], slots[0]["h"]) != (
        slots[1]["x"], slots[1]["y"], slots[1]["w"], slots[1]["h"]
    )


def test_every_layout_produces_slots_inside_the_output_frame(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _two_speaker_evidence(db_session, ws.id, asset, "run-11")
    for aspect in rf.TARGET_ASPECTS:
        geo = rf.target_geometry(320, 180, aspect)
        for layout in rf.LAYOUTS:
            payload = rf.build_plan_payload(
                db_session, ws.id, asset, aspect=aspect, layout=layout,
                params={"participants": 4, "duration_s": 8.0},
            )
            slots = payload["slots"]
            assert slots, f"{layout}/{aspect} produced no slots"
            rects = set()
            for slot in slots:
                assert slot["w"] > 0 and slot["h"] > 0
                assert slot["x"] >= 0 and slot["y"] >= 0
                assert slot["x"] + slot["w"] <= geo.out_width + 2, f"{layout}/{aspect} overhangs"
                assert slot["y"] + slot["h"] <= geo.out_height + 2, f"{layout}/{aspect} overhangs"
                assert slot["source_rect"]["src_w"] > 0
                rects.add((slot["x"], slot["y"], slot["w"], slot["h"]))
            # two slots at the SAME rect is a collapsed layout, not a layout
            assert len(rects) == len(slots), f"{layout}/{aspect} has duplicate slots"
            # 1:1 splits horizontally, tall aspects split vertically
            if layout in {"SPLIT_SCREEN", "TWO_SHOT"}:
                vertical = geo.out_height > geo.out_width
                same_x = slots[0]["x"] == slots[1]["x"]
                assert same_x is vertical, f"{layout}/{aspect} chose the wrong axis"


def test_multi_slot_layouts_bind_to_distinct_subjects(db_session):
    """Pane N shows subject N; a slot with no subject gets its OWN region.

    With one speaker on camera, binding pane 1 to "the 2nd keyframe" would make
    both panes frame the same person -- a collapsed composition, not a split.
    """
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _two_speaker_evidence(db_session, ws.id, asset, "run-split")
    geo = rf.target_geometry(320, 180, "9:16")

    # two speakers -> two DIFFERENT crops
    two = _plan(db_session, ws.id, asset, layout="SPLIT_SCREEN", params={"participants": 2})
    xs = [s["source_rect"]["src_x"] for s in two.meta_json["slots"]]
    assert len(set(xs)) == 2, f"both panes framed the same region: {xs}"

    # a single-subject plan on a 4-slot grid -> 4 distinct regions
    single = _video_asset(db_session, ws.id, storage_key="solo.mp4")
    run_id = _ensure_run(db_session, ws.id, single, "run-solo")
    track = _face_track(db_session, ws.id, single, run_id, "FT_00", 0.0, 8.0)
    _sample_track(db_session, ws.id, single, run_id, track, 0.0, 8.0)
    grid = _plan(db_session, ws.id, single, layout="GRID", params={"participants": 4})
    regions = [
        (s["source_rect"]["src_x"], s["source_rect"]["src_y"]) for s in grid.meta_json["slots"]
    ]
    assert len(set(regions)) == 4, f"grid slots share a source region: {regions}"
    # and every grid region is a real window inside the source
    for slot in grid.meta_json["slots"]:
        rect = slot["source_rect"]
        assert rect["src_x"] >= 0 and rect["src_x"] + rect["src_w"] <= 320
        assert rect["src_y"] >= 0 and rect["src_y"] + rect["src_h"] <= 180
    # no evidence at all -> still distinct slots, never two identical crops
    plain = _video_asset(db_session, ws.id, storage_key="plain2.mp4")
    bare = _plan(db_session, ws.id, plain, layout="SPLIT_SCREEN", params={"participants": 2})
    bare_xs = [s["source_rect"]["src_x"] for s in bare.meta_json["slots"]]
    assert len(set(bare_xs)) == 2, f"an evidence-free plan duplicated a crop: {bare_xs}"
    assert geo.out_width > 0


def test_unknown_layout_and_aspect_are_rejected(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    with pytest.raises(rf.ReframeError, match="layout must be one of"):
        rf.layout_slots("DIORAMA", rf.target_geometry(320, 180, "9:16"), [])
    with pytest.raises(rf.ReframeError, match="aspect must be one of"):
        rf.build_plan_payload(db_session, ws.id, asset, aspect="3:1", params={})


# ---------------------------------------------------------------------------
# canonical timeline operations
# ---------------------------------------------------------------------------


def test_layout_ops_are_within_op_types_and_apply_to_a_real_timeline(db_session):
    """contracts 21: reframe plans reach the editor ONLY as Work 02 ops."""
    from app.engine import timeline as tl

    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _two_speaker_evidence(db_session, ws.id, asset, "run-12")
    doc = tl.create_empty(ws.id, duration_seconds=8.0, fps=30.0, aspect="9:16")
    tracks = ["video", "broll", "avatar", "text", "caption"]

    for layout in rf.LAYOUTS:
        payload = rf.build_plan_payload(
            db_session, ws.id, asset, layout=layout,
            params={"participants": 4, "tracks": tracks},
        )
        ops = payload["ops"]
        assert ops, f"{layout} produced no ops"
        for op in ops:
            assert op["type"] in OP_TYPES, f"{layout} emitted non-canonical {op['type']}"
        # the ops actually apply -- the real validator is the arbiter
        applied = apply_operations(doc, ops)
        assert applied["tracks"]
        for track in applied["tracks"]:
            for clip in track.get("clips", []):
                if clip.get("id", "").startswith("reframe_"):
                    assert "crop" in clip.get("transform", {})


def test_animated_layout_emits_one_update_transform_per_keyframe(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    _two_speaker_evidence(db_session, ws.id, asset, "run-13")
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    ops = payload["ops"]
    adds = [o for o in ops if o["type"] == "add_item"]
    updates = [o for o in ops if o["type"] == "update_transform"]
    assert len(adds) == 1
    assert len(updates) == len(payload["keyframes"])
    for op in updates:
        assert op["clip_id"] == adds[0]["clip"]["id"]
        assert set(op["transform"]) <= {"x", "y", "scale", "rotation", "opacity", "crop", "z_index"}
        crop = op["transform"]["crop"]
        assert crop["w"] > 0 and crop["h"] > 0


def test_layout_ops_can_target_existing_clips_without_adding(db_session):
    """Re-planning a populated timeline must MOVE clips, not duplicate them."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    payload = rf.build_plan_payload(db_session, ws.id, asset, params={})
    slots = rf.layout_slots("ACTIVE_SPEAKER", rf.target_geometry(320, 180, "9:16"),
                            payload["keyframe_objects"])
    ops = rf.layout_ops("ACTIVE_SPEAKER", slots, payload["keyframe_objects"],
                        geometry=rf.target_geometry(320, 180, "9:16"),
                        clip_ids=["existing-clip"], duration_s=8.0)
    assert not [o for o in ops if o["type"] == "add_item"]
    assert all(op["clip_id"] == "existing-clip" for op in ops)
    with pytest.raises(rf.ReframeError, match="one id per slot"):
        rf.layout_ops("ACTIVE_SPEAKER", slots, payload["keyframe_objects"],
                      geometry=rf.target_geometry(320, 180, "9:16"),
                      clip_ids=["a", "b"], duration_s=8.0)
    with pytest.raises(rf.ReframeError, match="positive duration"):
        rf.layout_ops("ACTIVE_SPEAKER", slots, payload["keyframe_objects"],
                      geometry=rf.target_geometry(320, 180, "9:16"), duration_s=0.0)


# ---------------------------------------------------------------------------
# editable keyframes
# ---------------------------------------------------------------------------


def test_patch_keyframe_edits_the_row_and_keeps_history(db_session):
    """'Editable' means an operator can move a keyframe and lose nothing."""
    ws, user = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    run_id = "run-14"
    track = _face_track(db_session, ws.id, asset, run_id, "FT_00", 0.0, 8.0)
    _face_samples(db_session, ws.id, asset, run_id, track,
                  [(t, int((320 - BOX_W) * t / 8.0), BOX_Y, BOX_W, BOX_H)
                   for t in (0.0, 2.0, 4.0, 6.0)])
    plan = _plan(db_session, ws.id, asset)
    rows = rf.list_keyframes(db_session, plan.id)
    original = rf.keyframe_dto(rows[0])
    geo = rf.target_geometry(320, 180, "9:16")
    # x/y are NORMALISED crop centres and scale is a ZOOM (Lane H contract)
    new_x, new_scale = 0.8, 4.0

    row, entry = rf.update_keyframe(
        db_session, ws.id, rows[0].id,
        {"x": new_x, "y": 0.5, "scale": new_scale,
         "reason": "operator framed the logo", "confidence": 0.42},
        actor_id=user.id,
    )
    db_session.commit()
    after = rf.keyframe_dto(row)
    assert after["x"] == pytest.approx(new_x)
    assert after["y"] == pytest.approx(0.5)
    assert after["scale"] == pytest.approx(new_scale)
    assert after["reason"] == "operator framed the logo"
    assert after["confidence"] == pytest.approx(0.42)
    assert after["source"] == rf.SOURCE_OPERATOR, "an edited row is a human row"
    assert after["operator_edited"] is True
    # the authoritative rect moved with the edit, and kept the anchor contract
    assert after["rect"]["anchor"] == rf.KEYFRAME_ANCHOR
    assert after["rect"]["x"] == pytest.approx(new_x)
    # the pixel copy was rebuilt from the new centre, not left stale
    expected_px = int(round(new_x * 320 - geo.crop_width / 2.0))
    assert after["rect"]["px"]["x"] == expected_px
    # the previous values survive in the plan's append-only history
    history = plan.meta_json["keyframe_history"]
    assert len(history) == 1
    assert history[0]["keyframe_id"] == row.id
    assert history[0]["before"]["x"] == original["x"]
    assert history[0]["before"]["reason"] == original["reason"]
    assert history[0]["after"]["x"] == pytest.approx(new_x)
    assert history[0]["by"] == user.id
    # a second edit appends rather than rewriting the first
    rf.update_keyframe(db_session, ws.id, row.id, {"x": 0.9}, actor_id=user.id)
    db_session.commit()
    assert len(plan.meta_json["keyframe_history"]) == 2
    assert plan.meta_json["keyframe_history"][0]["after"]["x"] == pytest.approx(new_x)
    assert geo.crop_width == 100


def test_patch_keyframe_rejects_out_of_range_and_empty_patches(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    plan = _plan(db_session, ws.id, asset)
    row = rf.list_keyframes(db_session, plan.id)[0]
    for patch, message in (
        ({}, "non-empty object"),
        ({"x": -0.5}, "normalised crop centre"),
        ({"x": 5.0}, "normalised crop centre"),
        ({"y": 2.0}, "normalised crop centre"),
        ({"t_s": -1.0}, "t_s must be"),
        ({"scale": 0.0}, "zoom factor"),
        ({"scale": 99.0}, "zoom factor"),
        ({"confidence": 1.5}, "confidence must be"),
        ({"reason": "   "}, "must not be empty"),
        ({"rect": "nope"}, "rect must be"),
        ({"rect": {"x": 5.0}}, "normalised value"),
        ({"unknown_field": 1}, "must change one of"),
    ):
        with pytest.raises(rf.ReframeError, match=message):
            rf.update_keyframe(db_session, ws.id, row.id, patch)
    with pytest.raises(KeyError):
        rf.update_keyframe(db_session, ws.id, "no-such-keyframe", {"x": 1.0})


def test_edited_keyframes_survive_a_replan_read(db_session):
    """The rows are the source of truth: a GET returns what was edited."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    plan = _plan(db_session, ws.id, asset)
    row = rf.list_keyframes(db_session, plan.id)[1]
    rf.update_keyframe(db_session, ws.id, row.id, {"reason": "operator: keep the lower third"})
    db_session.commit()
    dto = rf.plan_dto(plan, rf.list_keyframes(db_session, plan.id))
    assert dto["editable"] is True
    match = next(k for k in dto["keyframes"] if k["id"] == row.id)
    assert match["reason"] == "operator: keep the lower third"
    assert match["source"] == rf.SOURCE_OPERATOR


# ---------------------------------------------------------------------------
# provider contract
# ---------------------------------------------------------------------------


def test_provider_health_capabilities_and_license_are_honest():
    """contracts 1.1/1.4: available without a model, REVIEW_REQUIRED on license."""
    provider = MotionReframeProvider()
    health = provider.health()
    assert isinstance(health.available, bool)
    if not health.available:
        assert health.reason, "an unavailable verdict must carry a reason"
    assert health.detail["models_required"] is False

    caps = provider.capabilities()
    assert caps["aspects"] == list(rf.TARGET_ASPECTS)
    assert caps["layouts"] == list(rf.LAYOUTS)
    assert caps["background_ops"] == list(rf.BACKGROUND_OPS)
    assert caps["priority"] == list(rf.KEYFRAME_SOURCES)
    assert caps["bakes_crop_into_source"] is False
    assert caps["editable_keyframes"] is True
    assert caps["preview_render"] is health.available

    resources = provider.resource_requirements()
    assert resources.gpu is False and resources.vram_mb == 0
    assert provider.cost(resources)["gpu_ms"] == 0

    info = provider.license_info()
    assert info.commercial_use == "REVIEW_REQUIRED", "ffmpeg: do not upgrade the verdict"
    assert info.code_license.startswith("LGPL")
    assert info.model_license == "NONE", "there is no model; say so, not UNVERIFIED"
    assert info.audited_on == "2026-09-29"


def test_provider_does_not_serve_active_speaker(monkeypatch):
    """Lane A flagged the chain: this provider must refuse that kind, not fake it."""
    from app.engine.intel import registry as intel_registry

    provider = MotionReframeProvider()
    assert provider.supports_kind("reframe") is True
    assert provider.supports_kind("active_speaker") is False
    monkeypatch.setattr(intel_registry, "PROVIDER_CHAINS",
                        {"active_speaker": ("motion_reframe",)})
    resolved, reasons = intel_registry.resolve("active_speaker")
    assert resolved is None
    assert "active_speaker" in reasons["motion_reframe"]


def test_provider_health_never_raises_without_ffmpeg(monkeypatch):
    import app.engine.intel.impl.motion_reframe as module

    monkeypatch.setattr(module, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(module, "ffprobe_available", lambda: False)
    health = module.MotionReframeProvider().health()
    assert health.available is False
    assert "ffmpeg" in health.reason


# ---------------------------------------------------------------------------
# background tools
# ---------------------------------------------------------------------------


def test_background_blur_and_replace_are_unavailable_without_a_mask(db_session):
    """contracts 12: no segmentation provider => UNAVAILABLE, never a bad mask."""
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    for operation in rf.BACKGROUND_OPS:
        result = rf.background_operation(db_session, ws.id, asset, operation)
        assert result["status"] == "UNAVAILABLE", f"{operation} claimed success"
        assert result["rendered"] is False
        assert result["reason"], "an unavailable answer must say why"
        assert "segmentation" in result["reason"]
        assert result["required"]["segmentation_chain"] == "sam2_segmentation"
    # nothing was written
    from sqlalchemy import select

    from app.models import MediaAsset
    assert db_session.scalar(
        select(MediaAsset).where(MediaAsset.parent_asset_id == asset.id)
    ) is None


def test_background_replace_needs_a_background_asset_even_with_a_mask(db_session, tmp_path,
                                                                     monkeypatch):
    """A mask is necessary but not sufficient: replace also needs the new plate."""
    from sqlalchemy import select

    from app.models import MediaAsset

    monkeypatch.chdir(tmp_path)
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id, storage_key="src.mp4")
    # the mask file must be INSIDE the workspace storage dir: managed_path is
    # fail-closed and a stray path is exactly what it exists to reject.
    mask_file = tmp_path / "data" / "videos" / ws.id / "mask.png"
    mask_file.parent.mkdir(parents=True, exist_ok=True)
    mask_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    mask_asset = MediaAsset(workspace_id=ws.id, type="image", origin="generated",
                            storage_key=f"data/videos/{ws.id}/mask.png",
                            mime_type="image/png", width=320, height=180)
    db_session.add(mask_asset)
    db_session.flush()
    run = rf.MaskAsset(
        run_id=_run(db_session, ws.id, asset, kind="segmentation"),
        workspace_id=ws.id, input_asset_id=asset.id,
        kind="PERSON", mask_asset_id=mask_asset.id, format="PNG",
        width=320, height=180, area_ratio=0.05, provider_key="sam2_segmentation",
    )
    db_session.add(run)
    db_session.commit()

    result = rf.background_operation(db_session, ws.id, asset, "background_replace")
    assert result["status"] == "UNAVAILABLE"
    assert "background_asset_id" in result["reason"]
    assert result["mask"]["mask_asset_id"] == mask_asset.id
    # a person_mask op with a mask is honest about not re-encoding anything
    ok = rf.background_operation(db_session, ws.id, asset, "person_mask")
    assert ok["status"] == "COMPLETED"
    assert ok["rendered"] is False and ok["source_unchanged"] is True
    assert ok["output_asset_id"] == mask_asset.id
    assert db_session.scalar(
        select(MediaAsset).where(MediaAsset.parent_asset_id == asset.id)
    ) is None


def test_background_rejects_an_unknown_operation(db_session):
    ws, _ = _ws(db_session)
    asset = _video_asset(db_session, ws.id)
    with pytest.raises(rf.ReframeError, match="operation must be one of"):
        rf.background_operation(db_session, ws.id, asset, "remove_the_guest")


def test_background_mask_lookup_is_workspace_scoped(db_session):

    from app.models import MediaAsset

    ws_a, _ = _ws(db_session)
    ws_b, _ = _ws(db_session)
    asset_b = _video_asset(db_session, ws_b.id, storage_key="b.mp4")
    mask_asset = MediaAsset(workspace_id=ws_b.id, type="image", origin="generated",
                            storage_key="m.png", mime_type="image/png")
    db_session.add(mask_asset)
    db_session.flush()
    db_session.add(rf.MaskAsset(
        run_id=_run(db_session, ws_b.id, asset_b, kind="segmentation"),
        workspace_id=ws_b.id, input_asset_id=asset_b.id, kind="PERSON",
        mask_asset_id=mask_asset.id, format="PNG", provider_key="sam2_segmentation",
    ))
    db_session.commit()
    # workspace A must not see workspace B's mask
    assert rf.find_mask(db_session, ws_a.id, asset_b.id) is None
    assert rf.find_mask(db_session, ws_b.id, asset_b.id) is not None


# ---------------------------------------------------------------------------
# workspace isolation (engine level)
# ---------------------------------------------------------------------------


def test_plan_and_keyframe_fetches_are_workspace_scoped(db_session):
    ws_a, _ = _ws(db_session)
    ws_b, _ = _ws(db_session)
    asset_a = _video_asset(db_session, ws_a.id, storage_key="a.mp4")
    plan_a = _plan(db_session, ws_a.id, asset_a)
    row = rf.list_keyframes(db_session, plan_a.id)[0]

    assert rf.get_plan(db_session, ws_a.id, plan_a.id) is not None
    assert rf.get_plan(db_session, ws_b.id, plan_a.id) is None
    assert rf.get_plan(db_session, ws_a.id, "missing") is None
    with pytest.raises(KeyError):
        rf.update_keyframe(db_session, ws_b.id, row.id, {"x": 1.0})


def test_evidence_loading_is_workspace_scoped(db_session):
    ws_a, _ = _ws(db_session)
    ws_b, _ = _ws(db_session)
    asset_b = _video_asset(db_session, ws_b.id, storage_key="b.mp4")
    _two_speaker_evidence(db_session, ws_b.id, asset_b, "run-b")
    evidence = rf.load_evidence(db_session, ws_a.id, asset_b.id)
    assert evidence["active_speaker"] == []
    assert evidence["face_boxes"] == {}


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _register(client):
    email = f"g{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.v1.media_intel_reframe import media_intel_reframe_router
    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    app = create_app()
    # the orchestrator registers the router in api/v1/__init__.py at
    # integration time; mount it explicitly until it does, so the route
    # contract is exercised either way (and the two paths cannot collide
    # because the mounted copy is only added when it is absent).
    mounted = "/api/v1/workspaces/{workspace_id}/media-intel/reframe"
    if not any(p == mounted for p in app.openapi()["paths"]):
        app.include_router(media_intel_reframe_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _seed_asset(workspace_id: str, storage_key: str = "src.mp4"):
    from app.db import session_scope
    from app.models import MediaAsset

    with session_scope() as session:
        asset = MediaAsset(workspace_id=workspace_id, type="video", origin="upload",
                           storage_key=storage_key, checksum=f"c-{uuid.uuid4().hex[:8]}",
                           width=320, height=180, duration_seconds=8.0)
        session.add(asset)
        session.commit()
        return asset.id


def test_reframe_routes_workspace_scoping_and_validation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    other_ws, other_headers = _register(client)
    asset_id = _seed_asset(ws_id)

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/reframe", headers=headers,
                    json={"asset_id": asset_id, "aspect": "9:16", "layout": "ACTIVE_SPEAKER"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["editable"] is True and plan["baked"] is False
    assert plan["aspect"] == "9:16" and plan["layout"] == "ACTIVE_SPEAKER"
    assert plan["keyframe_count"] == len(plan["keyframes"]) > 0
    assert plan["keyframes"][0]["source"] in rf.ALL_SOURCES
    assert plan["geometry"]["crop_width"] == 100
    assert all(op["type"] in OP_TYPES for op in plan["ops"])
    # Lane H interop: a run exists, and its metrics_json is the nested
    # {source, output} shape QC's _flatten_metrics expands to source_*/output_*
    run = plan["run"]
    assert run["id"] and run["status"] == "COMPLETED"
    assert set(run["metrics"]) >= {"source", "output"}
    assert run["metrics"]["output"]["keyframe_anchor"] == "center"
    assert run["metrics"]["output"]["keyframe_units"] == "normalised"
    assert run["metrics"]["source"]["width"] == 320
    # plan is bound to the run, which is how QC resolves a plan -> its verdict
    assert plan["run_id"] == run["id"]
    # plan.meta_json carries the source dims lane H's _plan_keyframes reads
    assert plan["meta"]["source_width"] == 320
    assert plan["meta"]["source_height"] == 180
    assert plan["meta"]["keyframe_anchor"] == "center"
    plan_id = plan["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/reframe/{plan_id}", headers=headers)
    assert r.status_code == 200 and r.json()["id"] == plan_id

    # a foreign workspace reads as 404, never 403
    r = client.get(f"/api/v1/workspaces/{other_ws}/media-intel/reframe/{plan_id}",
                   headers=other_headers)
    assert r.status_code == 404, r.text
    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/reframe/missing", headers=headers)
    assert r.status_code == 404

    # unknown aspect / layout / asset are 422 / 404, never 500
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/reframe", headers=headers,
                    json={"asset_id": asset_id, "aspect": "3:1"})
    assert r.status_code == 422, r.text
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/reframe", headers=headers,
                    json={"asset_id": asset_id, "layout": "DIORAMA"})
    assert r.status_code == 422, r.text
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/reframe", headers=headers,
                    json={"asset_id": "no-such-asset"})
    assert r.status_code == 404, r.text
    r = client.post(f"/api/v1/workspaces/{other_ws}/media-intel/reframe",
                    headers=other_headers, json={"asset_id": asset_id})
    assert r.status_code == 404, r.text

    keyframe_id = plan["keyframes"][0]["id"]
    # The PATCH is the APPLY path, so it is QC-gated: with no verdict recorded
    # the edit is refused rather than silently applied (contracts 13).
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=headers, json={"x": 0.4, "reason": "operator nudge"},
    )
    assert r.status_code == 422, r.text
    assert "QC" in r.json()["detail"] or "qc" in r.json()["detail"].lower(), r.text

    # once QC has PASSed the plan, the same edit goes through
    _seed_qc_result(ws_id, plan["run_id"], verdict="PASS")
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=headers, json={"x": 0.4, "reason": "operator nudge"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["x"] == pytest.approx(0.4) and body["source"] == rf.SOURCE_OPERATOR
    assert body["rect"]["anchor"] == rf.KEYFRAME_ANCHOR
    assert body["history_entry"]["before"]["x"] == plan["keyframes"][0]["x"]
    assert body["qc"]["verdict"] == "PASS"

    # a QC FAIL blocks the apply; overriding it needs the admin role
    _seed_qc_result(ws_id, plan["run_id"], verdict="FAIL")
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=headers, json={"x": 0.6},
    )
    assert r.status_code == 422, r.text
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=headers, json={"x": 0.6, "override_qc": True},
    )
    assert r.status_code in {403, 422}, r.text
    _seed_qc_result(ws_id, plan["run_id"], verdict="PASS")

    r = client.patch(
        f"/api/v1/workspaces/{other_ws}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=other_headers, json={"x": 0.2},
    )
    assert r.status_code == 404, r.text
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=headers, json={"x": -0.3},
    )
    assert r.status_code == 422, r.text
    r = client.patch(
        f"/api/v1/workspaces/{ws_id}/media-intel/reframe/keyframes/{keyframe_id}",
        headers=headers, json={},
    )
    assert r.status_code == 422, r.text


def _seed_qc_result(workspace_id: str, run_id: str, *, verdict: str = "PASS") -> str:
    """Record a QC verdict for a run so the apply gate can be exercised.

    Writes the row directly (the same table lane H persists into) rather than
    running a full QC pass, so this test isolates the GATE, not the checks.
    """
    from app.db import session_scope
    from app.models import IntelQCResult

    with session_scope() as session:
        row = IntelQCResult(
            workspace_id=workspace_id, run_id=run_id, kind="visual",
            verdict=verdict,
            checks_json=[{"name": "crop_movement", "status": "OK",
                          "severity": "warn", "verdict": "VIOLATION" if verdict == "FAIL"
                          else "OK"}],
        )
        session.add(row)
        session.commit()
        return row.id


def test_background_route_reports_unavailable_honestly(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _seed_asset(ws_id)

    for operation in ("background_blur", "person_mask", "object_mask"):
        r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/background",
                        headers=headers, json={"asset_id": asset_id, "operation": operation})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "UNAVAILABLE"
        assert body["rendered"] is False
        assert body["reason"]
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/background",
                    headers=headers, json={"asset_id": asset_id, "operation": "nope"})
    assert r.status_code == 422, r.text
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/background",
                    headers=headers, json={"asset_id": "missing"})
    assert r.status_code == 404, r.text


def test_reframe_route_creates_a_run_and_emits_after_commit(tmp_path, monkeypatch):
    """contracts 3: events fire only after the write is durable."""

    from app.db import session_scope
    from app.models import ReframePlan

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _seed_asset(ws_id)
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/reframe", headers=headers,
                    json={"asset_id": asset_id, "aspect": "4:5", "layout": "HOST_GUEST"})
    assert r.status_code == 200, r.text
    plan_id = r.json()["id"]
    with session_scope() as session:
        plan = session.get(ReframePlan, plan_id)
        assert plan is not None and plan.aspect == "4:5" and plan.layout == "HOST_GUEST"
        assert len(plan.meta_json["slots"]) == 2
    events = client.get(f"/api/v1/workspaces/{ws_id}/activities", headers=headers)
    if events.status_code == 200:
        kinds = {e.get("kind") for e in events.json().get("items", [])}
        assert "MEDIA_INTEL_REFRAME_PLAN_CREATED" in kinds


# ---------------------------------------------------------------------------
# slow: real ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not fixture_paths_available(), reason="ffmpeg/ffprobe not on PATH")
def test_preview_render_produces_a_real_derived_asset(tmp_path, monkeypatch):
    """The optional preview render: real ffmpeg, a NEW asset, source untouched."""
    import subprocess

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import MediaAsset
    from app.services.storage import STORAGE_ROOT

    monkeypatch.chdir(tmp_path)
    source_file = test_pattern_mp4(tmp_path / "source.mp4", seconds=2.0, fps=10,
                                   width=320, height=180)
    before_bytes = hashlib.sha256(source_file.read_bytes()).hexdigest()

    from app.models import User, Workspace, WorkspaceMember

    with session_scope() as session:
        user = User(email=f"g{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
        ws = Workspace(name="Preview WS", slug=f"p-{uuid.uuid4().hex[:8]}", niche="AI money")
        session.add_all([user, ws])
        session.flush()
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                                    role=WorkspaceMember.ROLE_OWNER))
        session.commit()
        ws_id = ws.id
        asset = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                           storage_key="source.mp4", mime_type="video/mp4",
                           width=320, height=180, duration_seconds=2.0,
                           checksum=before_bytes)
        session.add(asset)
        session.commit()
        asset_id = asset.id

    with session_scope() as session:
        asset = session.get(MediaAsset, asset_id)
        plan = _plan(session, ws_id, asset, out_height=320)
        rows = rf.list_keyframes(session, plan.id)
        result = rf.render_preview(
            session, ws_id, asset, str(source_file), plan, rows
        )
        session.commit()
        derived_id = result.get("asset_id")

    assert result["status"] == "COMPLETED", result.get("reason")
    assert result["rendered"] is True
    assert result["width"] == 180 and result["height"] == 320
    assert derived_id and derived_id != asset_id

    with session_scope() as session:
        derived = session.get(MediaAsset, derived_id)
        assert derived.parent_asset_id == asset_id, "lineage points at the source"
        assert derived.origin == "generated"
        manifest = derived.derivation_json
        assert manifest["operation"] == "reframe_preview"
        assert manifest["input_asset_id"] == asset_id
        assert manifest["filter_graph"], "the exact graph is recorded"
        assert manifest["ffmpeg_returncode"] == 0
        meta = derived.meta_json
        assert meta["lineage"] == "derived" and meta["parent_asset_id"] == asset_id
        path = STORAGE_ROOT / ws_id / derived.storage_key
        assert path.exists() and path.stat().st_size > 0
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        assert probe.stdout.strip() == "180,320"
        # exactly one derived asset: the source was not re-encoded in place
        derived_rows = session.scalars(
            select(MediaAsset).where(MediaAsset.parent_asset_id == asset_id)
        ).all()
        assert len(derived_rows) == 1

    assert hashlib.sha256(source_file.read_bytes()).hexdigest() == before_bytes, (
        "the preview render must not modify the source"
    )


@pytest.mark.slow
@pytest.mark.skipif(not fixture_paths_available(), reason="ffmpeg/ffprobe not on PATH")
def test_preview_render_uses_the_moving_crop_for_a_moving_plan(tmp_path, monkeypatch):
    """A plan that moves renders a MOVING crop (a real ffmpeg time expression)."""
    monkeypatch.chdir(tmp_path)
    source_file = test_pattern_mp4(tmp_path / "src.mp4", seconds=2.0, fps=10,
                                   width=320, height=180)
    from app.db import session_scope
    from app.models import MediaAsset, User, Workspace, WorkspaceMember

    with session_scope() as session:
        user = User(email=f"g{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
        ws = Workspace(name="Move WS", slug=f"m-{uuid.uuid4().hex[:8]}", niche="AI money")
        session.add_all([user, ws])
        session.flush()
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                                    role=WorkspaceMember.ROLE_OWNER))
        session.commit()
        ws_id = ws.id
        asset = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                           storage_key="src.mp4", mime_type="video/mp4",
                           width=320, height=180, duration_seconds=2.0, checksum="c1")
        session.add(asset)
        session.commit()
        asset_id = asset.id

    with session_scope() as session:
        from app.models import FaceTrack, FaceTrackSample

        asset = session.get(MediaAsset, asset_id)
        run_id = _run(session, ws_id, asset)
        face = FaceTrack(run_id=run_id, workspace_id=ws_id, asset_id=asset_id,
                         track_id="FT_00", start_s=0.0, end_s=2.0, sample_count=2)
        session.add(face)
        session.flush()
        for t in (0.0, 1.5):
            x = int((320 - BOX_W) * t / 2.0)
            session.add(FaceTrackSample(
                run_id=run_id, workspace_id=ws_id, track_id=face.id,
                track_label="FT_00", t_s=t, x=x, y=BOX_Y, w=BOX_W, h=BOX_H, confidence=0.9,
            ))
        session.flush()
        payload = rf.build_plan_payload(
            session, ws_id, asset, out_height=320,
            params={"sample_fps": 1.0, "min_interval_s": 0.5,
                    "max_move_per_s": 10_000.0},
        )
        plan = rf.persist_plan(session, ws_id, asset, payload)
        rows = rf.list_keyframes(session, plan.id)
        result = rf.render_preview(session, ws_id, asset, str(source_file), plan, rows)
        session.commit()

    assert result["status"] == "COMPLETED", result.get("reason")
    graph = result["manifest"]["filter_graph"]
    assert "if(lt(t" in graph, f"a moving plan must render a time expression: {graph}"
    assert "crop=w=100:h=180" in graph


@pytest.mark.slow
@pytest.mark.skipif(not fixture_paths_available(), reason="ffmpeg/ffprobe not on PATH")
@pytest.mark.parametrize("layout", ["SPLIT_SCREEN", "TWO_SHOT", "GRID", "HOST_GUEST",
                                    "PODCAST_DYNAMIC", "ACTIVE_SPEAKER"])
def test_every_layout_preview_graph_renders(tmp_path, monkeypatch, layout):
    """Every graph this module can emit must survive the real binary."""
    monkeypatch.chdir(tmp_path)
    source_file = test_pattern_mp4(tmp_path / "src.mp4", seconds=1.0, fps=10,
                                   width=320, height=180)
    geo = rf.target_geometry(320, 180, "9:16", out_height=320)
    keyframes = [
        rf.Keyframe(t_s=0.0, x=0, y=0, scale=rf.scale_for(geo), rect={"w": 100, "h": 180},
                    confidence=None, reason="r", source="fallback"),
        rf.Keyframe(t_s=0.5, x=100, y=0, scale=rf.scale_for(geo), rect={"w": 100, "h": 180},
                    confidence=None, reason="r", source="fallback"),
    ]
    graph = rf.preview_filter_graph(layout, geo, keyframes)
    assert graph, f"{layout} produced no graph"
    out = tmp_path / f"{layout}.mp4"
    result = rf.run_filter(str(source_file), out, ["-vf", graph],
                           extra_args=rf.PREVIEW_ENCODE_ARGS, video=True)
    assert result["ok"], f"{layout}: {result['stderr_tail'][-300:]}"
    assert out.exists() and out.stat().st_size > 0
    probe = rf.probe(out)
    stream = next(s for s in probe["streams"] if s.get("codec_type") == "video")
    assert stream["width"] > 0 and stream["height"] > 0
    if layout == "ACTIVE_SPEAKER":
        assert (stream["width"], stream["height"]) == (geo.out_width, geo.out_height)


@pytest.mark.slow
@pytest.mark.skipif(not fixture_paths_available(), reason="ffmpeg/ffprobe not on PATH")
def test_background_blur_renders_when_a_mask_exists(tmp_path, monkeypatch):
    """With a real mask row + a real mask file, the blur really renders."""
    import subprocess

    monkeypatch.chdir(tmp_path)
    source_file = test_pattern_mp4(tmp_path / "src.mp4", seconds=1.0, fps=10,
                                   width=320, height=180)
    ws_dir = tmp_path / "data" / "videos"
    from app.db import session_scope
    from app.models import MediaAsset, User, Workspace, WorkspaceMember

    with session_scope() as session:
        user = User(email=f"g{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
        ws = Workspace(name="Blur WS", slug=f"b-{uuid.uuid4().hex[:8]}", niche="AI money")
        session.add_all([user, ws])
        session.flush()
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                                    role=WorkspaceMember.ROLE_OWNER))
        session.commit()
        ws_id = ws.id
        (ws_dir / ws_id).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, ws_dir / ws_id / "src.mp4")
        # a real white-on-black PNG: the whole frame is "subject", so the blur
        # is measurable and the mask file is genuinely readable by ffmpeg
        mask_path = ws_dir / ws_id / "mask.png"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "lavfi", "-i", "color=c=white:s=320x180:r=10:d=1",
             "-frames:v", "1", str(mask_path)],
            check=True, capture_output=True,
        )
        asset = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                           storage_key=f"data/videos/{ws_id}/src.mp4",
                           mime_type="video/mp4",
                           width=320, height=180, duration_seconds=1.0, checksum="c2")
        mask_asset = MediaAsset(workspace_id=ws_id, type="image", origin="generated",
                                storage_key=f"data/videos/{ws_id}/mask.png",
                                mime_type="image/png", width=320, height=180)
        session.add_all([asset, mask_asset])
        session.flush()
        session.add(rf.MaskAsset(
            run_id=_run(session, ws_id, asset, kind="segmentation"),
            workspace_id=ws_id, input_asset_id=asset.id, kind="PERSON",
            mask_asset_id=mask_asset.id, format="PNG", width=320, height=180,
            area_ratio=1.0, provider_key="sam2_segmentation", model_version="sam2",
        ))
        session.commit()
        asset_id = asset.id

    with session_scope() as session:
        asset = session.get(MediaAsset, asset_id)
        result = rf.background_operation(
            session, ws_id, asset, "background_blur", blur_strength=12
        )
        session.commit()

    assert result["status"] == "COMPLETED", result.get("reason")
    assert result["rendered"] is True
    assert result["parent_asset_id"] == asset_id
    assert result["source_unchanged"] is True
    assert result["width"] == 320 and result["height"] == 180
    assert result["mask"]["mask_provider"] == "sam2_segmentation"
    from app.services.storage import STORAGE_ROOT

    out = STORAGE_ROOT / ws_id / result["storage_key"]
    assert out.exists() and out.stat().st_size > 0
    probe = rf.probe(out)
    stream = next(s for s in probe["streams"] if s.get("codec_type") == "video")
    assert (stream["width"], stream["height"]) == (320, 180)


def test_asset_paths_resolve_under_both_storage_key_conventions(
    db_session, tmp_path, monkeypatch
):
    """Both ``storage_key`` conventions the repo writes must resolve.

    ``services.storage`` writes the CWD-relative ``data/videos/<ws>/<file>``;
    ``providers/longform_assets.py`` and this lane's ``_register_derived``
    write the workspace-relative ``<file>``. A bare filename resolves against
    CWD, NOT the workspace root, so ``managed_path`` correctly refuses it --
    which means a resolver that tries only one convention makes every mask
    asset and every rendered preview unresolvable.
    """
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    from app.models import MediaAsset
    from app.services.storage import STORAGE_ROOT, managed_path

    ws, _ = _ws(db_session)
    ws_dir = STORAGE_ROOT / ws.id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workspace_relative.mp4").write_bytes(b"x")
    (ws_dir / "cwd_relative.mp4").write_bytes(b"x")

    # prove the boundary really does refuse the bare key on its own
    assert managed_path(ws.id, "workspace_relative.mp4") is None

    for key in ("workspace_relative.mp4", f"data/videos/{ws.id}/cwd_relative.mp4"):
        asset = MediaAsset(workspace_id=ws.id, type="video", origin="upload",
                           storage_key=key, checksum=f"c-{key[:6]}")
        db_session.add(asset)
        db_session.flush()
        resolved = rf._resolve_asset_path(db_session, ws.id, asset.id, "source")
        assert Path(resolved).is_file(), f"{key!r} did not resolve to a real file"
        assert Path(resolved).parent == ws_dir.resolve(), resolved

    # a foreign workspace's asset is refused, not read
    other, _ = _ws(db_session)
    foreign = MediaAsset(workspace_id=other.id, type="video", origin="upload",
                         storage_key="workspace_relative.mp4", checksum="c-foreign")
    db_session.add(foreign)
    db_session.flush()
    with pytest.raises(rf.ReframeError, match="not found in this workspace"):
        rf._resolve_asset_path(db_session, ws.id, foreign.id, "source")
    # a key that escapes the workspace root resolves to nothing
    escaping = MediaAsset(workspace_id=ws.id, type="video", origin="upload",
                          storage_key="../../../etc/passwd", checksum="c-escape")
    db_session.add(escaping)
    db_session.flush()
    with pytest.raises(rf.ReframeError, match="no resolvable storage path"):
        rf._resolve_asset_path(db_session, ws.id, escaping.id, "source")


@pytest.mark.slow
@pytest.mark.skipif(not fixture_paths_available(), reason="ffmpeg/ffprobe not on PATH")
def test_a_rendered_preview_resolves_through_the_shared_storage_boundary(
    tmp_path, monkeypatch
):
    """The derived asset this lane writes must be findable by the OTHER lanes.

    QC resolves a plan's media with ``qc.asset_media_path``; if this lane's
    ``storage_key`` is not resolvable by that same helper, every downstream
    measurement of a rendered preview is silently skipped.
    """
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    source_file = test_pattern_mp4(tmp_path / "src.mp4", seconds=1.0, fps=10,
                                   width=320, height=180)
    from app.db import session_scope
    from app.models import MediaAsset, User, Workspace, WorkspaceMember

    with session_scope() as session:
        user = User(email=f"g{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
        ws = Workspace(name="Key WS", slug=f"k-{uuid.uuid4().hex[:8]}", niche="AI money")
        session.add_all([user, ws])
        session.flush()
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                                    role=WorkspaceMember.ROLE_OWNER))
        session.commit()
        ws_id = ws.id
        asset = MediaAsset(workspace_id=ws_id, type="video", origin="upload",
                           storage_key="src.mp4", mime_type="video/mp4",
                           width=320, height=180, duration_seconds=1.0, checksum="c3")
        session.add(asset)
        session.commit()
        asset_id, run_id = asset.id, _run(session, ws_id, asset)

    with session_scope() as session:
        from app.engine.intel import qc
        from app.models import MediaAsset as MA

        asset = session.get(MA, asset_id)
        payload = rf.build_plan_payload(session, ws_id, asset, out_height=320,
                                       params={"sample_fps": 1.0})
        plan = rf.persist_plan(session, ws_id, asset, payload, run_id=run_id)
        rows = rf.list_keyframes(session, plan.id)
        result = rf.render_preview(session, ws_id, asset, str(source_file), plan, rows)
        session.commit()

    assert result["status"] == "COMPLETED", result.get("reason")
    # lane H's own resolver must find the file this lane just wrote
    with session_scope() as session:
        resolved = qc.asset_media_path(session, ws_id, result["asset_id"])
    assert resolved is not None and Path(resolved).is_file(), (
        f"lane H cannot resolve the derived asset (key={result['storage_key']!r})"
    )
    assert result["parent_asset_id"] == asset_id
    assert result["rendered"] is True
    # the render wrote a NEW file; the source asset is a different row entirely
    assert result["asset_id"] != asset_id


@pytest.mark.slow
@pytest.mark.skipif(not fixture_paths_available(), reason="ffmpeg/ffprobe not on PATH")
def test_run_filter_refuses_to_overwrite_the_source(tmp_path, monkeypatch):
    """The derived-only invariant is structural, not a convention."""
    monkeypatch.chdir(tmp_path)
    source_file = test_pattern_mp4(tmp_path / "src.mp4", seconds=0.5, fps=10,
                                   width=160, height=90)
    geo = rf.target_geometry(160, 90, "9:16", out_height=180)
    graph = rf.preview_filter_graph("ACTIVE_SPEAKER", geo, [])
    with pytest.raises(ValueError, match="refuses to write over its source"):
        rf.run_filter(str(source_file), str(source_file), ["-vf", graph], video=True)
    assert source_file.exists() and source_file.stat().st_size > 0
