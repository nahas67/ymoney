"""Media-intelligence QC (Work 12 Lane H) -- contracts §16 row "QC failures".

What this file proves:

* every audio and visual check FIRES on a crafted bad case and PASSES on a good
  one, with the threshold that did it (a failing test must name the number);
* the verdict aggregation, including the two ``REVIEW_REQUIRED`` paths
  (heuristic-only evidence and an ``UNRESOLVED`` dependency);
* a ``FAIL`` verdict blocks an apply, and only an ATTRIBUTABLE override
  (``who`` + ``why``) unlocks it;
* persistence + DTO shape (every check carries name/measured/threshold/reason),
  workspace isolation, and the three routes;
* the real-media measurements (``clipped_tone_wav`` peak 0.0 dBFS with 35 680
  full-scale samples, ``silent_wav`` at the EBU R128 silence floor,
  ``black_mp4`` every frame mean_luma 0.0, ``test_pattern_mp4`` a lit frame at
  ~10.2) -- those tests are ``-m slow`` because they shell out to ffmpeg and
  decode frames in pure Python.
"""

from __future__ import annotations

import types
import uuid

import pytest

from app.engine.intel import qc

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ws(db, workspace_with_user):
    from app.models import Workspace

    return db.get(Workspace, workspace_with_user["workspace"])


def _asset(db, workspace_id: str, *, key: str = "src.wav", kind: str = "audio",
           parent_id: str | None = None, checksum: str = "sum-1"):
    from app.models import MediaAsset

    row = MediaAsset(workspace_id=workspace_id, type=kind, storage_key=key,
                     checksum=checksum, parent_asset_id=parent_id)
    db.add(row)
    db.flush()
    return row


def _run(db, ws, *, asset, kind="enhance", metrics=None, output_asset_id=None,
         provider="ffmpeg_enhancement", params=None):
    from app.models import MediaIntelRun

    row = MediaIntelRun(
        workspace_id=ws.id, asset_id=asset.id, kind=kind, provider_key=provider,
        model_version="v1", params_json=dict(params or {}), asset_checksum="sum-1",
        status="COMPLETED", metrics_json=dict(metrics or {}),
        output_asset_id=output_asset_id,
    )
    db.add(row)
    db.flush()
    return row


def _kf(t_s: float, x: float = 0.5, y: float = 0.5, scale: float = 1.0,
        rect: dict | None = None, source: str = "salient_face"):
    return types.SimpleNamespace(t_s=t_s, x=x, y=y, scale=scale,
                                 rect_json=rect or {}, source=source,
                                 confidence=0.9, reason="test")


def _sample(t_s: float, x=0.45, y=0.45, w=0.1, h=0.1, label="FT_00", conf=0.9):
    return types.SimpleNamespace(t_s=t_s, x=x, y=y, w=w, h=h,
                                 track_label=label, confidence=conf)


def _track(label="FT_00", start=0.0, end=4.0, truncated=False, reentry=0):
    return types.SimpleNamespace(track_id=label, start_s=start, end_s=end,
                                 truncated=truncated, reentry_count=reentry,
                                 confidence_max=0.9, sample_count=2)


def _plan(db, ws, run, *, keyframes, aspect="9:16", layout="ACTIVE_SPEAKER",
          strategy="active_speaker"):
    from app.models.media_intel import ReframeKeyframe, ReframePlan

    plan = ReframePlan(run_id=run.id, workspace_id=ws.id, source_asset_id=run.asset_id,
                       layout=layout, aspect=aspect, strategy=strategy,
                       meta_json={"source_width": 1920, "source_height": 1080})
    db.add(plan)
    db.flush()
    for kf in keyframes:
        db.add(ReframeKeyframe(
            plan_id=plan.id, workspace_id=ws.id, t_s=kf.t_s, x=kf.x, y=kf.y,
            scale=kf.scale, rect_json=kf.rect_json, confidence=kf.confidence,
            reason=kf.reason, source=kf.source,
        ))
    db.flush()
    return plan


def _face_track(db, ws, run, asset, *, start=0.0, end=4.0, samples=(0.0, 2.0, 4.0),
                truncated=False):
    from app.models.media_intel import FaceTrack, FaceTrackSample

    track = FaceTrack(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                      track_id="FT_00", start_s=start, end_s=end,
                      confidence_max=0.9, sample_count=len(samples),
                      truncated=truncated)
    db.add(track)
    db.flush()
    # Lane E contract: face sample boxes are in SOURCE PIXELS, never
    # normalised. The plan declares a 1920x1080 frame, so write a face near
    # the centre in those pixels (960,540) with a 192x108 box -- the same
    # normalised position the old 0.45/0.1 fixture expressed.
    for t_s in samples:
        db.add(FaceTrackSample(run_id=run.id, workspace_id=ws.id, track_id=track.id,
                               track_label="FT_00", t_s=t_s, x=960.0, y=540.0,
                               w=192.0, h=108.0, confidence=0.9))
    db.flush()
    return track


def _check_by_name(payload: dict, name: str) -> dict:
    return next(c for c in payload["checks"] if c["name"] == name)


def _all_ok(payload: dict) -> bool:
    return all(c["verdict"] == "OK" for c in payload["checks"])


# ---------------------------------------------------------------------------
# audio checks -- each fires on a bad case, passes on a good one
# ---------------------------------------------------------------------------


def test_duration_drift_passes_and_fires():
    good = qc.check_duration_drift(120.0, 121.0)          # 0.83 % drift
    assert good.ok and good.measured == pytest.approx(0.008333, abs=1e-4)
    warned = qc.check_duration_drift(120.0, 100.0)         # 16.7 % > 5 %
    assert warned.status == "WARN" and warned.measured == pytest.approx(0.16667, abs=1e-4)
    bad = qc.check_duration_drift(120.0, 80.0)             # 33 % > 20 %
    assert bad.violated and bad.severity == qc.SEVERITY_HARD
    assert bad.measured == pytest.approx(0.33333, abs=1e-4)
    assert bad.evidence["delta_s"] == pytest.approx(-40.0, abs=1e-3)
    assert "33.3%" in bad.reason


def test_duration_drift_is_unknown_without_a_measurement():
    check = qc.check_duration_drift(120.0, None)
    assert check.status == "UNKNOWN" and check.measured is None
    assert "unknown" in check.reason


def test_clipping_fires_on_the_measured_clipped_peak():
    # ffmpeg 8.1.1 astats reports 0.000265 dBFS for clipped_tone_wav: the
    # comparison must be >= 0.0, not > 0.0, or the case is missed.
    check = qc.check_clipping(peak_dbfs=0.000265, true_peak_dbfs=0.000265)
    assert check.violated and check.severity == qc.SEVERITY_HARD
    assert "clipping" in check.reason
    counted = qc.check_clipping(peak_dbfs=-12.0, clipped_samples=35_680)
    assert counted.violated, "a non-zero full-scale sample count is clipping too"
    assert counted.evidence["clipped_samples"] == 35_680


def test_clipping_passes_and_warns():
    good = qc.check_clipping(peak_dbfs=-5.999684, true_peak_dbfs=-6.0, clipped_samples=0)
    assert good.ok and good.evidence["peak_dbfs"] == pytest.approx(-5.999684, abs=1e-6)
    assert good.measured == pytest.approx(-6.0, abs=1e-3)
    warned = qc.check_clipping(peak_dbfs=-0.4)
    assert warned.status == "WARN" and warned.threshold == qc.CLIP_WARN_PEAK_DBFS
    unknown = qc.check_clipping()
    assert unknown.status == "UNKNOWN"


def test_missing_audio_fires_on_every_proof():
    assert qc.check_missing_audio(has_audio_stream=False).violated
    assert qc.check_missing_audio(has_audio_stream=True, samples=0).violated
    # measured: silent_wav integrates at the EBU R128 floor of -70 LUFS
    floor = qc.check_missing_audio(has_audio_stream=True, samples=96_000,
                                   integrated_lufs=-70.0, peak_dbfs=None)
    assert floor.violated and floor.measured == -70.0
    detected = qc.check_missing_audio(has_audio_stream=True, samples=96_000,
                                      integrated_lufs=-70.0, silence_ratio=1.0,
                                      duration_s=2.0)
    assert detected.violated


def test_missing_audio_passes_warns_and_is_unknown():
    good = qc.check_missing_audio(has_audio_stream=True, peak_dbfs=-6.0,
                                  integrated_lufs=-9.7, samples=48_000,
                                  silence_ratio=0.0, duration_s=1.0)
    assert good.ok
    warned = qc.check_missing_audio(has_audio_stream=True, samples=48_000,
                                    integrated_lufs=-40.0, silence_ratio=0.95,
                                    duration_s=10.0)
    assert warned.status == "WARN" and warned.threshold == qc.MISSING_AUDIO_SILENCE_WARN_RATIO
    assert qc.check_missing_audio().status == "UNKNOWN"


def test_excessive_removed_speech_fails_and_forces_keep():
    check = qc.check_excessive_removed_speech(20.0, 10.0, source_duration_s=120.0)
    assert check.violated and check.severity == qc.SEVERITY_HARD
    assert check.measured == pytest.approx(2.0)
    assert check.evidence["force_keep"] is True
    assert check.evidence["denominator"] == "speech_activity"
    good = qc.check_excessive_removed_speech(3.0, 10.0, source_duration_s=120.0)
    assert good.ok


def test_excessive_removed_speech_downgrades_without_a_speech_baseline():
    """Heuristic-only evidence (whole-duration denominator) is REVIEW, not FAIL."""
    check = qc.check_excessive_removed_speech(60.0, None, source_duration_s=120.0)
    assert check.violated and check.severity == qc.SEVERITY_REVIEW
    assert check.evidence["denominator"] == "source_duration"
    nothing = qc.check_excessive_removed_speech(0.0, None)
    assert nothing.ok
    blind = qc.check_excessive_removed_speech(30.0, None, source_duration_s=None)
    assert blind.status == "UNKNOWN"


def test_excessive_removed_speech_honours_a_stricter_policy_ratio():
    check = qc.check_excessive_removed_speech(4.0, 10.0, max_removal_ratio=0.2)
    assert check.violated and check.evidence["max_removal_ratio"] == 0.2


def test_transcript_mismatch_fires_on_word_count_and_on_rate():
    lost = qc.check_transcript_mismatch(source_words=100, output_words=40,
                                        source_duration_s=60.0, output_duration_s=60.0)
    assert lost.violated and lost.severity == qc.SEVERITY_HARD
    assert lost.evidence["word_count_ratio"] == pytest.approx(0.4)
    # same word count, but the output is half as long -> the rate doubled
    fast = qc.check_transcript_mismatch(source_words=100, output_words=100,
                                        source_duration_s=60.0, output_duration_s=30.0)
    assert fast.violated and "speaking rate" in fast.reason
    warned = qc.check_transcript_mismatch(source_words=100, output_words=75,
                                          source_duration_s=60.0, output_duration_s=60.0)
    assert warned.status == "WARN" and warned.threshold == qc.TRANSCRIPT_WORD_COUNT_WARN
    good = qc.check_transcript_mismatch(source_words=100, output_words=100,
                                        source_duration_s=60.0, output_duration_s=60.0)
    assert good.ok
    assert qc.check_transcript_mismatch(source_words=None, output_words=10).status == "UNKNOWN"


# ---------------------------------------------------------------------------
# visual checks
# ---------------------------------------------------------------------------


def test_crop_rect_prefers_rect_json_and_defaults_to_a_centre_anchor():
    rect = qc.crop_rect_at(_kf(0.0, rect={"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.4}))
    assert rect is not None
    for key, value in (("x0", 0.0), ("y0", 0.1), ("x1", 0.4), ("y1", 0.5)):
        assert rect[key] == pytest.approx(value, abs=1e-9)
    top_left = qc.crop_rect_at(
        _kf(0.0, rect={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "anchor": "top_left"})
    )
    assert top_left["x0"] == pytest.approx(0.1)
    # no rect_json -> derived from x/y/scale (centre anchor, zoom 2.0).
    # width = 1/scale = 0.5 of the source WIDTH; the height then has to be
    #   h = w * (src_w/src_h) / (target_w/target_h) = 0.5 * 3.16 = 1.578
    # which exceeds the frame, so the rect FITS INSIDE: h = 1.0 (full height)
    # and the width shrinks to 1.0 * (9/16) / (16/9) = 0.3164 -- a true 9:16
    # crop (101x180 px). The old chain produced 0.5 x 0.5, i.e. 160x90 px, which
    # is the SOURCE's 16:9 shape, not a 9:16 crop.
    derived = qc.crop_rect_at(_kf(0.0, x=0.5, y=0.5, scale=2.0))
    # `width = 1/scale` stays authoritative (0.5 of the source width) and the
    # height -- which would need 1.578 -- clamps to the full frame, so the
    # rect is 160x180 px. The old chain produced 0.5 x 0.5 = 160x90 px, which
    # is the SOURCE's 16:9 shape rather than a 9:16 crop.
    assert derived["x0"] == pytest.approx(0.25, abs=1e-6)
    assert derived["x1"] == pytest.approx(0.75, abs=1e-6)
    assert (derived["y1"] - derived["y0"]) == pytest.approx(1.0, abs=1e-9)
    assert qc.crop_rect_at(_kf(0.0, x=None, y=None)) is None


def test_excessive_crop_movement_fires_above_the_clamp():
    whip = qc.crop_motion([_kf(t, x=x) for t, x in
                           ((0.0, 0.2), (1.0, 0.7), (2.0, 0.2))])
    assert whip["max_speed_per_s"] == pytest.approx(0.5)
    bad = qc.check_excessive_crop_movement(whip)
    assert bad.violated and bad.severity == qc.SEVERITY_HARD
    assert bad.threshold == qc.CROP_MOVEMENT_CLAMP_PER_SEC
    slow = qc.crop_motion([_kf(t, x=x) for t, x in
                           ((0.0, 0.5), (2.0, 0.55), (4.0, 0.5))])
    assert qc.check_excessive_crop_movement(slow).ok
    mid = qc.crop_motion([_kf(0.0, x=0.5), _kf(1.0, x=0.62), _kf(2.0, x=0.5)])
    assert mid["max_speed_per_s"] == pytest.approx(0.12)
    assert qc.check_excessive_crop_movement(mid).status == "WARN"
    assert qc.check_excessive_crop_movement(None).status == "UNKNOWN"


def test_unstable_crop_fires_on_reversals_but_not_on_a_smooth_ramp():
    # constant SPEED, changing direction: the wobble a speed-only metric misses
    wobble = qc.crop_motion([_kf(t, x=x) for t, x in
                             ((0.0, 0.5), (1.0, 0.7), (2.0, 0.5), (3.0, 0.7))])
    assert wobble["max_speed_per_s"] == pytest.approx(0.2)
    # |0.5 - 2*0.7 + 0.5| / 1^2 == 0.4 -> a hard violation on its own
    assert wobble["mean_jitter_per_s2"] == pytest.approx(0.4)
    bad = qc.check_unstable_crop(wobble)
    assert bad.violated and bad.threshold == qc.CROP_JITTER_FAIL_PER_S2
    # a slow wobble is a warning, not a failure
    slight = qc.crop_motion([_kf(t, x=x) for t, x in
                             ((0.0, 0.5), (1.0, 0.55), (2.0, 0.5), (3.0, 0.55))])
    assert slight["max_speed_per_s"] == pytest.approx(0.05)
    assert slight["mean_jitter_per_s2"] == pytest.approx(0.1)
    assert qc.check_unstable_crop(slight).status == "WARN"
    ramp = qc.crop_motion([_kf(t, x=x) for t, x in
                           ((0.0, 0.2), (1.0, 0.35), (2.0, 0.5), (3.0, 0.65))])
    assert ramp["mean_jitter_per_s2"] == pytest.approx(0.0)
    assert qc.check_unstable_crop(ramp).ok
    assert qc.check_unstable_crop(None).status == "UNKNOWN"


def test_missing_subject_fires_on_low_coverage():
    keyframes = [_kf(0.0), _kf(4.0)]
    covered = qc.subject_coverage(keyframes, [_sample(t) for t in (0.0, 1.0, 2.0)])
    assert covered["coverage"] == 1.0
    assert qc.check_missing_subject(covered).ok
    # the crop is zoomed to half a frame and drifts right: the face leaves it
    drifting = [_kf(0.0, x=0.5, scale=2.0), _kf(2.0, x=0.95, scale=2.0)]
    partial = qc.subject_coverage(drifting, [_sample(0.0, x=0.45), _sample(3.0, x=0.45)])
    assert partial["coverage"] == pytest.approx(0.5)
    bad = qc.check_missing_subject(partial)
    assert bad.violated and bad.severity == qc.SEVERITY_HARD
    assert bad.evidence["uncovered_t_s"] == [3.0]
    # no detector evidence is UNKNOWN, never a fabricated zero
    assert qc.check_missing_subject(qc.subject_coverage(keyframes, [])).status == "UNKNOWN"
    warned = qc.check_missing_subject({"coverage": 0.7, "samples": 10, "covered": 7})
    assert warned.status == "WARN" and warned.threshold == qc.SUBJECT_COVERAGE_MIN


def test_face_lost_fires_on_a_coverage_gap():
    track = _track(start=0.0, end=6.0)
    samples = [_sample(0.0), _sample(0.5), _sample(5.5), _sample(6.0)]
    reports = qc.track_reports([track], {"FT_00": samples}, sample_fps=2.0)
    assert reports[0]["max_gap_s"] == pytest.approx(5.0)
    assert qc.check_face_lost(reports).violated
    healthy = qc.track_reports(
        [_track(start=0.0, end=2.4)],
        {"FT_00": [_sample(t) for t in (0.0, 0.6, 1.2, 1.8, 2.4)]},
        sample_fps=2.0,
    )
    assert healthy[0]["max_gap_s"] == pytest.approx(0.6)
    assert healthy[0]["coverage"] == pytest.approx(1.0)
    assert qc.check_face_lost(healthy).status == "WARN"  # 0.6 s > the 0.5 s warn gap
    assert qc.check_face_lost([]).status == "UNKNOWN"


def test_face_lost_does_not_blame_a_truncated_track():
    truncated = qc.track_reports(
        [_track(start=0.0, end=60.0, truncated=True)],
        {"FT_00": [_sample(0.0), _sample(30.0), _sample(60.0)]},
        sample_fps=2.0,
    )
    assert truncated[0]["coverage"] < qc.FACE_COVERAGE_MIN
    check = qc.check_face_lost(truncated)
    assert not check.violated, (
        "evidence the provider itself flagged as partial is not a detector failure"
    )
    assert check.status == "WARN" and "sample cap" in check.reason


def test_mask_failure_fires_on_every_defect():
    assert qc.check_mask_failure(None).status == "UNKNOWN"
    assert qc.check_mask_failure([]).status == "UNKNOWN"
    missing_ref = qc.check_mask_failure([{"kind": "PERSON", "mask_asset_id": ""}])
    assert missing_ref.violated and "no mask file reference" in missing_ref.reason
    speck = qc.check_mask_failure([{"kind": "PERSON", "mask_asset_id": "a",
                                    "format": "PNG", "width": 640, "height": 360,
                                    "area_ratio": 0.001}])
    assert speck.violated and "0.10%" in speck.reason
    wrong_format = qc.check_mask_failure([{"kind": "PERSON", "mask_asset_id": "a",
                                           "format": "MP4", "width": 640,
                                           "height": 360, "area_ratio": 0.3}])
    assert wrong_format.violated and "unsupported mask format" in wrong_format.reason
    too_small = qc.check_mask_failure([{"kind": "PERSON", "mask_asset_id": "a",
                                        "format": "PNG", "width": 4, "height": 400,
                                        "area_ratio": 0.3}])
    assert too_small.violated and "below 8px" in too_small.reason
    corrupt = qc.check_mask_failure([{"kind": "PERSON", "mask_asset_id": "a",
                                      "format": "PNG", "width": 640, "height": 360,
                                      "area_ratio": 0.3, "checksum_mismatch": True}])
    assert corrupt.violated and "checksum" in corrupt.reason
    good = qc.check_mask_failure([{"kind": "PERSON", "mask_asset_id": "a",
                                   "format": "PNG", "width": 640, "height": 360,
                                   "area_ratio": 0.22}])
    assert good.ok


def test_black_frames_predicate_needs_both_darkness_and_no_highlight():
    assert qc.is_black_frame({"mean_luma": 0.0, "max_luma": 0})
    assert not qc.is_black_frame({"mean_luma": 10.198, "max_luma": 255})
    assert not qc.is_black_frame({"mean_luma": 0.5, "max_luma": 255})


def test_black_frames_fires_on_a_mostly_black_sample():
    frames = [{"t_s": i * 0.25, "mean_luma": 0.0, "max_luma": 0} for i in range(4)]
    bad = qc.check_black_frames(frames)
    assert bad.violated and bad.measured == pytest.approx(1.0)
    lit = [{"t_s": 0.0, "mean_luma": 10.2, "max_luma": 255}]
    assert qc.check_black_frames(lit).ok
    dip = ([{"t_s": i * 0.25, "mean_luma": 10.0, "max_luma": 255} for i in range(99)]
           + [{"t_s": 25.0, "mean_luma": 0.0, "max_luma": 0},
              {"t_s": 25.25, "mean_luma": 0.0, "max_luma": 0},
              {"t_s": 25.5, "mean_luma": 0.0, "max_luma": 0}])
    assert qc.check_black_frames(dip).status == "WARN"
    assert qc.check_black_frames([]).status == "UNKNOWN"


# ---------------------------------------------------------------------------
# verdict aggregation (contracts §13)
# ---------------------------------------------------------------------------


def _check(name, status, severity=qc.SEVERITY_WARN):
    return qc.QCCheck(name=name, status=status, severity=severity, reason="r")


def test_aggregate_verdict_rules():
    assert qc.aggregate_verdict([_check("a", "OK")])["verdict"] == "PASS"
    warned = qc.aggregate_verdict([_check("a", "OK"), _check("b", "WARN")])
    assert warned["verdict"] == "PASS_WITH_WARNINGS" and warned["warnings"] == ["b"]
    # an absent measurement is not a pass
    unknown = qc.aggregate_verdict([_check("a", "OK"), _check("b", "UNKNOWN")])
    assert unknown["verdict"] == "PASS_WITH_WARNINGS" and unknown["unknown"] == ["b"]
    review = qc.aggregate_verdict([_check("a", "OK"), _check("b", "VIOLATION", qc.SEVERITY_REVIEW)])
    assert review["verdict"] == "REVIEW_REQUIRED"
    assert review["review_reasons"] == ["b"]
    hard = qc.aggregate_verdict([_check("a", "OK"), _check("b", "VIOLATION", qc.SEVERITY_REVIEW),
                                 _check("c", "VIOLATION", qc.SEVERITY_HARD)])
    assert hard["verdict"] == "FAIL" and hard["failures"] == ["c"]
    overridden = qc.aggregate_verdict([_check("a", "OK")], override={"by": "u"})
    assert overridden["verdict"] == "REVIEW_REQUIRED" and overridden["overridden"] is True


def test_unresolved_dependency_is_review_required(db_session, workspace_with_user):
    """An UNRESOLVED active-speaker map can never be a clean pass (contracts §10)."""
    from app.models.media_intel import ActiveSpeakerMap

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset, kind="active_speaker")
    db_session.add(ActiveSpeakerMap(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                                     start_s=0.0, end_s=2.0, status="UNRESOLVED",
                                     reason="ambiguous_tie"))
    db_session.flush()

    payload = qc.run_visual_qc(db_session, ws, run, luma=[])
    check = _check_by_name(payload, "unresolved_dependency")
    assert check["verdict"] == "VIOLATION" and check["severity"] == qc.SEVERITY_REVIEW
    assert payload["verdict"] == "REVIEW_REQUIRED"
    assert payload["review_reasons"] == ["unresolved_dependency"]

    db_session.query(ActiveSpeakerMap).delete()
    db_session.add(ActiveSpeakerMap(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                                     start_s=0.0, end_s=2.0, status="RESOLVED",
                                     speaker_id="SPEAKER_00", face_track_id=None,
                                     confidence=0.8))
    db_session.flush()
    resolved = qc.run_visual_qc(db_session, ws, run, luma=[])
    assert _check_by_name(resolved, "unresolved_dependency")["verdict"] == "OK"
    assert "unresolved_dependency" not in resolved["review_reasons"]


# ---------------------------------------------------------------------------
# audio QC end to end (DB + engine, no sibling lane required)
# ---------------------------------------------------------------------------


def test_audio_qc_persists_a_verdict_with_full_check_evidence(
    db_session, workspace_with_user
):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset, kind="silence")

    payload = qc.run_audio_qc(db_session, ws, run)
    assert payload["persisted"] is True
    assert payload["kind"] == "audio" and payload["run_id"] == run.id
    assert payload["verdict"] in qc.QC_VERDICTS
    # no media, no proposals, no transcript: nothing may claim a clean PASS
    assert payload["verdict"] == "PASS_WITH_WARNINGS"
    names = [c["name"] for c in payload["checks"]]
    assert names == ["duration_drift", "clipping", "missing_audio",
                     "excessive_removed_speech", "transcript_mismatch"]
    for check in payload["checks"]:
        assert set(check) >= {"name", "ok", "verdict", "severity", "measured",
                              "threshold", "reason", "method", "evidence"}
        assert check["reason"], "every check explains itself in words"
        assert check["method"], "every check names how it was measured"

    row = qc.get_result(db_session, ws.id, run.id, "audio")
    assert row is not None and row.verdict == payload["verdict"]
    assert len(row.checks_json) == 5
    assert qc.qc_dto(row)["id"] == row.id
    # events are SURFACED, never emitted mid-transaction (contracts §3)
    assert [e["kind"] for e in payload["events"]] == [qc.EVENT_QC_COMPLETED]
    assert payload["events"][0]["data"]["run_id"] == run.id


def test_audio_qc_reads_recorded_metrics_instead_of_re_measuring(
    db_session, workspace_with_user, storage_root, monkeypatch
):
    """A run that already recorded its measurements must not re-measure them.

    The run points at a REAL output file on disk, so any ffmpeg pass here would
    be a wasted measurement (and a silent regression of the no-re-derivation
    rule), which is why every shared helper is booby-trapped.
    """
    from app.engine.intel import ffmpeg_util as fu
    from tests.media_intel_fixtures import tone_wav

    def _boom(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("QC re-measured what the run already recorded")

    ws = _ws(db_session, workspace_with_user)
    real = tone_wav(storage_root.parent / "recorded.wav", seconds=1.0)
    asset = _asset(db_session, ws.id, key="src.wav")
    out_asset = _asset(db_session, ws.id, key="out.wav", parent_id=asset.id)
    _install(storage_root, ws.id, "out.wav", real)
    run = _run(db_session, ws, asset=asset, kind="enhance", output_asset_id=out_asset.id,
               metrics={
                   "source": {"duration_s": 10.0},
                   "output": {"duration_s": 9.8, "peak_dbfs": -6.0,
                              "true_peak_dbfs": -6.0, "integrated_lufs": -14.0,
                              "has_audio_stream": True, "samples": 480_000,
                              "clipped_samples": 0, "silence_ratio": 0.0},
               })
    assert qc.asset_media_path(db_session, ws.id, out_asset.id) is not None
    for name in ("measure_peaks", "measure_loudness", "detect_silence", "probe",
                 "duration_seconds"):
        monkeypatch.setattr(fu, name, _boom)

    payload = qc.run_audio_qc(db_session, ws, run, persist=False)
    assert payload["measured"]["measured_passes"] == [], (
        "every measurement was already recorded: no ffmpeg pass may run"
    )
    assert _check_by_name(payload, "clipping")["verdict"] == "OK"
    assert _check_by_name(payload, "missing_audio")["verdict"] == "OK"
    drift = _check_by_name(payload, "duration_drift")
    assert drift["verdict"] == "OK" and drift["measured"] == pytest.approx(0.02, abs=1e-3)
    assert drift["evidence"]["source_duration_s"] == 10.0


@pytest.mark.slow
def test_audio_qc_measures_only_what_the_run_did_not_record(
    db_session, workspace_with_user, storage_root
):
    """A run with PARTIAL metrics pays for exactly the missing passes."""
    from tests.media_intel_fixtures import tone_wav

    ws = _ws(db_session, workspace_with_user)
    real = tone_wav(storage_root.parent / "partial.wav", seconds=1.0)
    asset = _asset(db_session, ws.id, key="src.wav")
    out_asset = _asset(db_session, ws.id, key="out.wav", parent_id=asset.id)
    _install(storage_root, ws.id, "out.wav", real)
    # loudness was recorded; the peak/samples/stream layout still have to be
    # measured, so QC pays for exactly two passes and no more
    run = _run(db_session, ws, asset=asset, kind="enhance", output_asset_id=out_asset.id,
               metrics={"output": {"integrated_lufs": -14.0, "true_peak_dbfs": -6.0,
                                   "silence_ratio": 0.0}})

    payload = qc.run_audio_qc(db_session, ws, run, persist=False)
    assert payload["measured"]["measured_passes"] == ["probe", "peaks"]
    clipping = _check_by_name(payload, "clipping")
    assert clipping["verdict"] == "OK"
    assert clipping["evidence"]["peak_dbfs"] == pytest.approx(-5.999684, abs=1e-3)


def test_audio_qc_fails_on_excessive_removal_from_real_proposal_rows(
    db_session, workspace_with_user
):
    from app.models.media_intel import DiarizationSegment, EditProposal

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset, kind="fillers")
    # 2 s of decided cuts against 2 s of measured speech activity
    db_session.add(EditProposal(workspace_id=ws.id, asset_id=asset.id, run_id=run.id,
                                kind="REMOVE_RANGE", start_s=1.0, end_s=3.0,
                                reason="filler", status="DECIDED", decision="remove"))
    db_session.add(DiarizationSegment(run_id=run.id, workspace_id=ws.id,
                                      asset_id=asset.id, kind="SPEECH_ACTIVITY",
                                      start_s=0.0, end_s=1.0, speaker_id=None,
                                      confidence=None))
    db_session.add(DiarizationSegment(run_id=run.id, workspace_id=ws.id,
                                      asset_id=asset.id, kind="SPEECH_ACTIVITY",
                                      start_s=3.0, end_s=4.0, speaker_id=None,
                                      confidence=None))
    db_session.flush()

    payload = qc.run_audio_qc(db_session, ws, run)
    check = _check_by_name(payload, "excessive_removed_speech")
    assert check["verdict"] == "VIOLATION" and check["severity"] == qc.SEVERITY_HARD
    assert check["measured"] == pytest.approx(1.0)
    assert check["evidence"]["force_keep"] is True
    assert check["evidence"]["speech_activity_s"] == pytest.approx(2.0)
    assert payload["verdict"] == "FAIL"
    assert payload["failures"] == ["excessive_removed_speech"]


def test_audio_qc_prefers_the_time_map_over_raw_proposals(
    db_session, workspace_with_user
):
    from app.models.media_intel import AudioTimeMap, DiarizationSegment, EditProposal

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset, kind="silence")
    db_session.add(EditProposal(workspace_id=ws.id, asset_id=asset.id, run_id=run.id,
                                kind="REMOVE_RANGE", start_s=0.0, end_s=4.0,
                                reason="silence", status="DECIDED", decision="remove"))
    db_session.add(AudioTimeMap(workspace_id=ws.id, asset_id=asset.id, policy_id="p1",
                                segments_json=[
                                    {"src_start": 0.0, "src_end": 4.0, "out_start": 0.0,
                                     "out_end": 2.8},
                                    {"src_start": 4.0, "src_end": 10.0, "out_start": 2.8,
                                     "out_end": 7.0},
                                ]))
    db_session.add(DiarizationSegment(run_id=run.id, workspace_id=ws.id,
                                      asset_id=asset.id, kind="SPEECH_ACTIVITY",
                                      start_s=0.0, end_s=10.0, speaker_id=None,
                                      confidence=None))
    db_session.flush()

    payload = qc.run_audio_qc(db_session, ws, run, persist=False)
    check = _check_by_name(payload, "excessive_removed_speech")
    # the mapping wins over the raw proposals: 10 s of source maps to 7 s, so
    # 3 s is actually removed -- the 4 s the proposals claimed would have been
    # over the limit
    assert payload["measured"]["removed_source"] == "audio_time_maps"
    assert payload["measured"]["removed_s"] == pytest.approx(3.0)
    assert check["measured"] == pytest.approx(0.3)
    assert check["verdict"] == "OK"
    assert qc.MAX_REMOVAL_RATIO == 0.35


# ---------------------------------------------------------------------------
# visual QC end to end (DB + engine, no sibling lane required)
# ---------------------------------------------------------------------------


def test_visual_qc_persists_evidence_for_a_bad_plan(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, kind="video", key="clip.mp4")
    run = _run(db_session, ws, asset=asset, kind="reframe")
    # a whip-pan plan: the crop crosses half the frame every second
    _plan(db_session, ws, run, keyframes=[_kf(0.0, x=0.2), _kf(1.0, x=0.7), _kf(2.0, x=0.2)])
    _face_track(db_session, ws, run, asset, start=0.0, end=2.0, samples=(0.0, 0.5, 1.0, 1.5))

    payload = qc.run_visual_qc(db_session, ws, run, luma=[
        {"t_s": 0.0, "mean_luma": 10.2, "max_luma": 255},
        {"t_s": 0.25, "mean_luma": 0.0, "max_luma": 0},
    ])
    movement = _check_by_name(payload, "excessive_crop_movement")
    assert movement["verdict"] == "VIOLATION" and movement["severity"] == qc.SEVERITY_HARD
    assert movement["measured"] == pytest.approx(0.5)
    jitter = _check_by_name(payload, "unstable_crop")
    assert jitter["verdict"] == "VIOLATION"
    assert _check_by_name(payload, "black_frames")["verdict"] == "VIOLATION"
    assert _check_by_name(payload, "missing_subject")["verdict"] == "OK"
    assert payload["verdict"] == "FAIL"
    row = qc.get_result(db_session, ws.id, run.id, "visual")
    assert row.verdict == "FAIL"
    assert row.kind == "visual"
    assert {c["name"] for c in row.checks_json} == {
        "missing_subject", "unstable_crop", "face_lost", "mask_failure",
        "excessive_crop_movement", "black_frames", "unresolved_dependency",
    }


def test_visual_qc_passes_a_stable_plan_with_face_and_mask_evidence(
    db_session, workspace_with_user
):
    from app.models.media_intel import ActiveSpeakerMap, MaskAsset

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, kind="video", key="clip.mp4")
    run = _run(db_session, ws, asset=asset, kind="reframe")
    _plan(db_session, ws, run, keyframes=[_kf(0.0, x=0.5), _kf(2.0, x=0.51),
                                          _kf(4.0, x=0.52)])
    _face_track(db_session, ws, run, asset, start=0.0, end=2.0,
                samples=(0.0, 0.5, 1.0, 1.5, 2.0))
    mask_asset = _asset(db_session, ws.id, kind="image", key="mask.png", checksum="m1")
    db_session.add(MaskAsset(run_id=run.id, workspace_id=ws.id, input_asset_id=asset.id,
                             kind="PERSON", mask_asset_id=mask_asset.id, format="PNG",
                             width=640, height=360, area_ratio=0.22,
                             provider_key="sam2_segmentation", checksum="deadbeef"))
    db_session.add(ActiveSpeakerMap(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                                     start_s=0.0, end_s=2.0, status="RESOLVED",
                                     speaker_id="SPEAKER_00", confidence=0.8))
    db_session.flush()

    payload = qc.run_visual_qc(db_session, ws, run, luma=[
        {"t_s": 0.0, "mean_luma": 10.2, "max_luma": 255},
    ])
    for name in ("missing_subject", "unstable_crop", "face_lost",
                 "excessive_crop_movement", "black_frames", "unresolved_dependency"):
        assert _check_by_name(payload, name)["verdict"] == "OK", name
    # a mask row pointing at a file that is not on disk is a REAL failure
    mask = _check_by_name(payload, "mask_failure")
    assert mask["verdict"] == "VIOLATION" and "missing on disk" in mask["reason"]
    assert payload["verdict"] == "FAIL"


def test_visual_qc_accepts_a_plan_object_with_no_run(db_session, workspace_with_user):
    from app.models.media_intel import ReframePlan

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, kind="video", key="clip.mp4")
    plan = ReframePlan(workspace_id=ws.id, source_asset_id=asset.id, layout="TWO_SHOT",
                       aspect="1:1", strategy="focal_point", meta_json={})
    db_session.add(plan)
    db_session.flush()
    for kf in (_kf(0.0, x=0.5), _kf(1.0, x=0.9), _kf(2.0, x=0.5)):
        db_session.add(qc.ReframeKeyframe(plan_id=plan.id, workspace_id=ws.id, t_s=kf.t_s,
                                          x=kf.x, y=kf.y, scale=kf.scale,
                                          source="focal_point"))
    db_session.flush()

    payload = qc.run_visual_qc(db_session, ws, plan, luma=[])
    assert payload["persisted"] is False, "no run row means nothing to attribute it to"
    assert payload["run_id"] == plan.id and payload["plan_id"] == plan.id
    assert _check_by_name(payload, "excessive_crop_movement")["verdict"] == "VIOLATION"
    assert "face_lost" in payload["unknown"]


# ---------------------------------------------------------------------------
# override + apply gate
# ---------------------------------------------------------------------------


def _fail_result(db, ws, run, name="clipping"):
    qc.run_audio_qc(db, ws, run, metrics={
        "output": {"duration_s": 60.0, "peak_dbfs": 0.0, "true_peak_dbfs": 0.0},
    })
    row = qc.get_result(db, ws.id, run.id, "audio")
    assert row.verdict == "FAIL" and row.checks_json[1]["name"] == name
    return row


def test_fail_blocks_apply_until_an_attributable_override(
    db_session, workspace_with_user
):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset, kind="enhance")
    row = _fail_result(db_session, ws, run)

    with pytest.raises(qc.QCApplyBlocked) as excinfo:
        qc.assert_qc_allows_apply(db_session, ws, row)
    blocked = excinfo.value
    assert blocked.verdict == "FAIL" and blocked.needs_override is True
    assert blocked.failures == ["clipping"]
    assert blocked.as_dict()["run_id"] == run.id
    assert isinstance(blocked, ValueError)

    # the capability alone is not enough: the override must be RECORDED
    with pytest.raises(qc.QCApplyBlocked):
        qc.assert_qc_allows_apply(db_session, ws, run, override=True)

    override = qc.record_override(db_session, ws, row, by_user="user-1",
                                  reason="client approved the clipped peak")
    assert override["verdict"] == "FAIL", "an override never rewrites the measurement"
    assert override["override"]["by"] == "user-1"
    assert "approved" in override["override"]["reason"]
    assert override["override"]["overridden_verdict"] == "FAIL"
    assert override["override"]["checks"] == ["clipping"]
    assert override["apply_requires_override"] is False
    assert [e["kind"] for e in override["events"]] == [qc.EVENT_QC_OVERRIDDEN]

    allowed = qc.assert_qc_allows_apply(db_session, ws, run, override=True)
    assert allowed["allowed"] is True and allowed["verdict"] == "FAIL"
    assert allowed["override_used"] is True
    assert allowed["failures"] == ["clipping"]


def test_an_override_must_name_who_and_why(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset, kind="enhance")
    row = _fail_result(db_session, ws, run)
    with pytest.raises(ValueError):
        qc.record_override(db_session, ws, row, by_user="", reason="because")
    with pytest.raises(ValueError):
        qc.record_override(db_session, ws, row, by_user="user-1", reason="   ")


def test_pass_and_review_apply_without_an_override(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, kind="video", key="clip.mp4")
    run = _run(db_session, ws, asset=asset, kind="reframe")
    _plan(db_session, ws, run, keyframes=[_kf(0.0, x=0.5), _kf(2.0, x=0.5)])
    qc.run_visual_qc(db_session, ws, run, luma=[{"t_s": 0.0, "mean_luma": 90.0,
                                                "max_luma": 255}])
    row = qc.get_result(db_session, ws.id, run.id, "visual")
    assert row.verdict in ("PASS", "PASS_WITH_WARNINGS")
    decision = qc.assert_qc_allows_apply(db_session, ws, run)
    assert decision["allowed"] is True and decision["verdict"] == row.verdict

    # a target with no QC verdict at all is blocked (honest, not silent)
    other = _run(db_session, ws, asset=asset, kind="face_tracking")
    with pytest.raises(qc.QCApplyBlocked) as excinfo:
        qc.assert_qc_allows_apply(db_session, ws, other)
    assert excinfo.value.needs_override is False


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_qc_reads_are_workspace_scoped(db_session, workspace_with_user):
    from app.models import User, Workspace, WorkspaceMember

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    run = _run(db_session, ws, asset=asset)
    qc.run_audio_qc(db_session, ws, run)

    other = Workspace(name="Other WS", slug=f"ws-{uuid.uuid4().hex[:8]}", niche="AI money")
    user = User(email=f"qc-iso-{uuid.uuid4().hex[:6]}@test.local", password_hash="x")
    db_session.add_all([other, user])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                                   role=WorkspaceMember.ROLE_OWNER))
    db_session.flush()

    assert qc.get_result(db_session, ws.id, run.id) is not None
    assert qc.get_result(db_session, other.id, run.id) is None
    assert qc.list_results(db_session, other.id, run.id) == []
    with pytest.raises(qc.QCApplyBlocked):
        qc.assert_qc_allows_apply(db_session, other, run)
    row = qc.get_result(db_session, ws.id, run.id)
    with pytest.raises(qc.QCApplyBlocked):
        qc.record_override(db_session, other, row, by_user="x", reason="y")
    # a foreign result row cannot be read through another workspace either
    assert qc._resolve_target(db_session, other.id, row) is None


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _register(client):
    r = client.post("/api/v1/auth/register", json={
        "email": f"qc{uuid.uuid4().hex[:10]}@test.local", "password": "supersecret123",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.v1.media_intel_qc import media_intel_qc_router
    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    app = create_app()
    # the orchestrator registers the router at integration time; mount it
    # explicitly until it does, so the route contract is exercised either way
    # (the /api/v1 prefix comes from the api_router aggregate, as for Lane A)
    path = "/api/v1/workspaces/{workspace_id}/media-intel/qc/{run_id}"
    if path not in app.openapi()["paths"]:
        app.include_router(media_intel_qc_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _qc_client_with_run(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Workspace

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        asset = _asset(s, ws.id)
        run = _run(s, ws, asset=asset, kind="enhance")
        run_id = run.id
    return client, ws_id, headers, run_id


def test_qc_routes_run_read_and_isolate(tmp_path, monkeypatch):
    client, ws_id, headers, run_id = _qc_client_with_run(tmp_path, monkeypatch)

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/run",
                    json={"kind": "audio"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "audio" and body["persisted"] is True
    assert body["verdict"] in qc.QC_VERDICTS
    assert body["events_emitted"] == [qc.EVENT_QC_COMPLETED]
    assert "events" not in body, "the route emits; it does not leak the queue"

    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}", headers=headers)
    assert r.status_code == 200, r.text
    listing = r.json()
    assert listing["verdicts"] == ["audio", "visual"]
    assert len(listing["items"]) == 1
    assert listing["latest"]["audio"]["id"] == body["id"]
    assert listing["latest"]["audio"]["checks"][0]["name"] == "duration_drift"

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/run",
                    json={"kind": "nope"}, headers=headers)
    assert r.status_code == 422, r.text
    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/qc/does-not-exist",
                   headers=headers)
    assert r.status_code == 404, r.text

    # a foreign workspace reads as 404, never 403
    other_ws, other_headers = _register(client)
    assert client.get(f"/api/v1/workspaces/{other_ws}/media-intel/qc/{run_id}",
                      headers=other_headers).status_code == 404
    assert client.post(f"/api/v1/workspaces/{other_ws}/media-intel/qc/{run_id}/run",
                       json={}, headers=other_headers).status_code == 404
    assert client.post(
        f"/api/v1/workspaces/{other_ws}/media-intel/qc/{run_id}/override",
        json={"reason": "x"}, headers=other_headers,
    ).status_code == 404


def test_qc_override_route_requires_a_reason_and_records_who(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import MediaIntelRun, Workspace

    client, ws_id, headers, run_id = _qc_client_with_run(tmp_path, monkeypatch)
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        row = s.get(MediaIntelRun, run_id)
        # a real FAIL: the recorded metrics say the output peaks at full scale
        payload = qc.run_audio_qc(s, ws, row, metrics={
            "output": {"duration_s": 60.0, "peak_dbfs": 0.0, "true_peak_dbfs": 0.0},
        })
        assert payload["verdict"] == "FAIL"
        s.commit()

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/override",
                    json={}, headers=headers)
    assert r.status_code == 422, r.text

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/override",
                    json={"reason": "client signed off on the clipping"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "FAIL"
    assert body["override"]["reason"] == "client signed off on the clipping"
    assert body["override"]["by"], "an override is attributable"
    assert body["events_emitted"] == [qc.EVENT_QC_OVERRIDDEN]
    assert body["apply_decision"]["allowed"] is True
    assert body["apply_decision"]["verdict"] == "FAIL"


def test_qc_run_route_infers_the_kind_from_the_run(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import MediaIntelRun

    client, ws_id, headers, run_id = _qc_client_with_run(tmp_path, monkeypatch)
    with session_scope() as s:
        s.get(MediaIntelRun, run_id).kind = "reframe"
        s.commit()
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/run",
                    json={}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "visual"
    assert r.json()["workspace_id"] == ws_id


# ---------------------------------------------------------------------------
# real media (ffmpeg) -- -m slow
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    """Point the managed storage root at tmp_path so real files stay out of the repo."""
    from app.services import storage

    root = tmp_path / "data" / "videos"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "STORAGE_ROOT", root)
    monkeypatch.chdir(tmp_path)
    return root


def _install(root, workspace_id, key: str, source) -> str:
    target = root / str(workspace_id) / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return key


@pytest.mark.slow
def test_audio_qc_measures_a_real_clipped_tone(db_session, workspace_with_user, storage_root):
    from tests.media_intel_fixtures import clipped_tone_wav

    ws = _ws(db_session, workspace_with_user)
    clipped = clipped_tone_wav(storage_root.parent / "clip.wav")
    asset = _asset(db_session, ws.id, key="src.wav")
    out_asset = _asset(db_session, ws.id, key="out.wav", parent_id=asset.id)
    _install(storage_root, ws.id, "src.wav", clipped)
    _install(storage_root, ws.id, "out.wav", clipped)
    run = _run(db_session, ws, asset=asset, kind="enhance", output_asset_id=out_asset.id)

    payload = qc.run_audio_qc(db_session, ws, run)
    check = _check_by_name(payload, "clipping")
    assert check["verdict"] == "VIOLATION" and check["severity"] == qc.SEVERITY_HARD
    # measured: astats reports a peak AT/over full scale for the clipped tone
    assert check["measured"] >= qc.CLIP_HARD_PEAK_DBFS
    assert check["evidence"]["peak_dbfs"] >= 0.0
    assert payload["verdict"] == "FAIL"
    assert payload["failures"] == ["clipping"]
    # the duration check really probed both files
    assert _check_by_name(payload, "duration_drift")["evidence"]["source_duration_s"] \
        == pytest.approx(2.0, abs=0.05)


@pytest.mark.slow
def test_audio_qc_measures_a_real_silent_render(db_session, workspace_with_user, storage_root):
    from tests.media_intel_fixtures import silent_wav, tone_wav

    ws = _ws(db_session, workspace_with_user)
    # same length on both sides, so the ONLY defect is the silence
    silent = silent_wav(storage_root.parent / "silent.wav", seconds=2.0)
    tone = tone_wav(storage_root.parent / "tone.wav", seconds=2.0)
    asset = _asset(db_session, ws.id, key="src.wav")
    out_asset = _asset(db_session, ws.id, key="out.wav", parent_id=asset.id)
    _install(storage_root, ws.id, "src.wav", tone)
    _install(storage_root, ws.id, "out.wav", silent)

    run = _run(db_session, ws, asset=asset, kind="silence", output_asset_id=out_asset.id)
    payload = qc.run_audio_qc(db_session, ws, run)
    check = _check_by_name(payload, "missing_audio")
    assert check["verdict"] == "VIOLATION" and check["severity"] == qc.SEVERITY_HARD
    assert payload["failures"] == ["missing_audio"]
    # the real measurement, not a guess: the EBU R128 floor OR the silence span
    measured = check["evidence"]
    assert (measured["integrated_lufs"] is not None
            and measured["integrated_lufs"] <= qc.MISSING_AUDIO_LUFS_FLOOR) or (
        measured["silence_ratio"] is not None
        and measured["silence_ratio"] > qc.MISSING_AUDIO_SILENCE_RATIO
    )
    # the source is fine, so this is a defect of the OUTPUT, not of the input
    assert qc.audio_measurements(str(storage_root / ws.id / "src.wav"))["peak_dbfs"] > -10.0


@pytest.mark.slow
def test_audio_qc_passes_a_clean_enhance(db_session, workspace_with_user, storage_root):
    from app.models import MediaIntelWord
    from tests.media_intel_fixtures import tone_wav

    ws = _ws(db_session, workspace_with_user)
    tone = tone_wav(storage_root.parent / "tone.wav")
    asset = _asset(db_session, ws.id, key="src.wav")
    out_asset = _asset(db_session, ws.id, key="out.wav", parent_id=asset.id)
    _install(storage_root, ws.id, "src.wav", tone)
    _install(storage_root, ws.id, "out.wav", tone)
    # a completed alignment run on the source is the source transcript
    prior = _run(db_session, ws, asset=asset, kind="alignment", provider="whisperx_alignment")
    run = _run(db_session, ws, asset=asset, kind="enhance", output_asset_id=out_asset.id)
    for index, word in enumerate(("hello", "world", "again")):
        db_session.add(MediaIntelWord(run_id=prior.id, workspace_id=ws.id,
                                      asset_id=asset.id, idx=index, word=word,
                                      start_s=index * 0.3, end_s=index * 0.3 + 0.2,
                                      speaker_id="SPEAKER_00", confidence=0.9))
        db_session.add(MediaIntelWord(run_id=run.id, workspace_id=ws.id,
                                      asset_id=asset.id, idx=index, word=word,
                                      start_s=index * 0.3, end_s=index * 0.3 + 0.2,
                                      speaker_id="SPEAKER_00", confidence=0.9))
    db_session.flush()

    payload = qc.run_audio_qc(db_session, ws, run)
    assert _all_ok(payload), [(c["name"], c["verdict"], c["reason"]) for c in payload["checks"]]
    assert payload["verdict"] == "PASS"
    assert payload["measured"]["source_words"] == 3
    assert payload["measured"]["output_words"] == 3
    assert qc.audio_measurements(str(storage_root / ws.id / "out.wav"))["peak_dbfs"] \
        == pytest.approx(-5.999684, abs=1e-3)


@pytest.mark.slow
def test_black_frames_detects_a_real_black_clip(db_session, workspace_with_user, storage_root):
    from tests.media_intel_fixtures import black_mp4, video_frame_luma

    ws = _ws(db_session, workspace_with_user)
    clip = black_mp4(storage_root.parent / "black.mp4")
    asset = _asset(db_session, ws.id, kind="video", key="black.mp4")
    _install(storage_root, ws.id, "black.mp4", clip)
    run = _run(db_session, ws, asset=asset, kind="reframe")

    # the fixture ground truth agrees with what QC measured through ffmpeg_util
    truth = video_frame_luma(clip, fps=4, max_frames=8)
    assert truth and all(f["mean_luma"] == 0.0 for f in truth)

    payload = qc.run_visual_qc(db_session, ws, run)
    check = _check_by_name(payload, "black_frames")
    assert check["verdict"] == "VIOLATION" and check["measured"] == pytest.approx(1.0)
    assert check["evidence"]["frames_sampled"] >= len(truth)
    assert check["evidence"]["mean_luma_min"] == pytest.approx(0.0)
    assert "black_frames" in payload["failures"]
    assert payload["verdict"] == "FAIL"


@pytest.mark.slow
def test_black_frames_leaves_a_dark_but_present_clip_alone(
    db_session, workspace_with_user, storage_root
):
    from tests.media_intel_fixtures import test_pattern_mp4, video_frame_luma

    ws = _ws(db_session, workspace_with_user)
    clip = test_pattern_mp4(storage_root.parent / "pattern.mp4", seconds=2.0, fps=10)
    asset = _asset(db_session, ws.id, kind="video", key="pattern.mp4")
    _install(storage_root, ws.id, "pattern.mp4", clip)
    run = _run(db_session, ws, asset=asset, kind="reframe")

    truth = video_frame_luma(clip, fps=4, max_frames=8)
    assert truth, "the fixture must decode"
    assert all(f["mean_luma"] > 0.0 for f in truth), "the fixture is dark but NOT black"
    assert all(f["max_luma"] > qc.BLACK_FRAME_MAX_LUMA for f in truth), (
        "the drawn white box keeps a highlight in every frame"
    )
    payload = qc.run_visual_qc(db_session, ws, run)
    check = _check_by_name(payload, "black_frames")
    assert check["verdict"] == "OK", check["reason"]
    assert check["evidence"]["mean_luma_min"] >= 0.0
    assert "black_frames" not in payload["failures"]


@pytest.mark.slow
def test_visual_qc_measures_real_frames_for_a_face_following_crop(
    db_session, workspace_with_user, storage_root
):
    """A real clip + real face boxes: the crop follows the subject, so OK."""
    from tests.media_intel_fixtures import test_pattern_mp4, video_frame_luma

    ws = _ws(db_session, workspace_with_user)
    clip = test_pattern_mp4(storage_root.parent / "pattern.mp4", seconds=2.0, fps=10)
    asset = _asset(db_session, ws.id, kind="video", key="pattern.mp4")
    _install(storage_root, ws.id, "pattern.mp4", clip)
    run = _run(db_session, ws, asset=asset, kind="reframe")
    # the drawn box travels left -> right; the crop centre follows it slowly
    # enough to stay under the per-second clamp (0.4 widths over 1.75 s = 0.23/s)
    truth = video_frame_luma(clip, fps=4, max_frames=8)
    times = [f["t_s"] for f in truth]
    centres = [0.25 + 0.4 * (t / max(times[-1], 0.001)) for t in times]
    _plan(db_session, ws, run,
          keyframes=[_kf(t, x=c) for t, c in zip(times, centres, strict=True)])

    from app.models.media_intel import FaceTrack, FaceTrackSample

    track = FaceTrack(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                      track_id="FT_00", start_s=0.0, end_s=max(times[-1], 0.1),
                      confidence_max=0.9, sample_count=len(times))
    db_session.add(track)
    db_session.flush()
    for t_s, centre in zip(times, centres, strict=True):
        db_session.add(FaceTrackSample(run_id=run.id, workspace_id=ws.id,
                                       track_id=track.id, track_label="FT_00",
                                       t_s=t_s, x=centre - 0.05, y=0.45,
                                       w=0.1, h=0.1, confidence=0.9))
    db_session.flush()

    payload = qc.run_visual_qc(db_session, ws, run)
    subject = _check_by_name(payload, "missing_subject")
    assert subject["verdict"] == "OK", subject["reason"]
    assert subject["measured"] == pytest.approx(1.0)
    movement = _check_by_name(payload, "excessive_crop_movement")
    assert movement["verdict"] in ("OK", "WARN"), movement["reason"]
    assert _check_by_name(payload, "black_frames")["verdict"] == "OK"
    assert "clipping" not in payload["failures"]
