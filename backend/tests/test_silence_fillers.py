"""Silence, dead air, fillers and non-destructive editing (Work 12 Lane D).

Contracts §16 rows owned by this file: silence detection, filler proposals,
timeline sync after cuts, editor operation compatibility. Plus the §7 rules they
depend on -- the padding/boundary rule, the KEEP/REMOVE/SHORTEN policy, the
``max_removal_ratio`` override, the time map, "nothing destructive before
approval", workspace isolation and the route surface -- and the §13 QC gate:
Lane H's ``assert_qc_allows_apply`` is consulted before a timeline is touched,
and an over-ratio plan is recorded as H's hard ``excessive_removed_speech``
violation with ``evidence.force_keep``.

Fast tests never touch the binary; ``@pytest.mark.slow`` runs the real
``silencedetect`` pass over the shared lavfi fixtures and the full HTTP flow.
"""

from __future__ import annotations

import uuid

import pytest

from app.engine.intel import silence_fillers as sf

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _record_qc(db, ws, run, *, removed_s: float = 0.0, speech_activity_s: float | None = None,
              max_removal_ratio: float = 0.25, source_duration_s: float = 8.0):
    """Record an audio QC verdict for a run using Lane H's OWN functions.

    No hand-written row: the check dict and the aggregate verdict come from
    ``app.engine.intel.qc``, so the fixture a test asserts on is the same shape
    H's QC route writes and ``assert_qc_allows_apply`` reads.
    """
    from app.engine.intel import qc as intel_qc
    from app.models import IntelQCResult

    check = intel_qc.check_excessive_removed_speech(
        removed_s, speech_activity_s,
        source_duration_s=source_duration_s, max_removal_ratio=max_removal_ratio,
    )
    summary = intel_qc.aggregate_verdict([check])
    row = IntelQCResult(workspace_id=str(ws.id), run_id=str(run.id), kind="audio",
                        verdict=str(summary["verdict"]), checks_json=[check.as_dict()])
    db.add(row)
    db.flush()
    return row


def _ws(db, workspace_with_user):
    from app.models import Workspace

    return db.get(Workspace, workspace_with_user["workspace"])


def _asset(db, workspace_id: str, *, duration: float | None = None,
           checksum: str = "sum-silence", name: str = "src.wav"):
    from app.models import MediaAsset
    from app.services.storage import STORAGE_ROOT

    # the storage key convention `services/storage.managed_path` expects: a
    # CWD-relative path INSIDE the workspace root (same shape content.py uses)
    key = str(STORAGE_ROOT / workspace_id / name)
    row = MediaAsset(workspace_id=workspace_id, type="audio", storage_key=key,
                     checksum=checksum, duration_seconds=duration)
    db.add(row)
    db.flush()
    return row


def _run(db, ws, asset, *, kind="silence", params=None):
    from app.services import media_intel_runs as runs

    dto = runs.create_run(
        db, ws, kind=kind, asset=asset,
        provider_key=sf.SILENCE_PROVIDER if kind == "silence" else sf.FILLER_PROVIDER,
        model_version=sf.SILENCE_MODEL if kind == "silence" else sf.FILLER_MODEL,
        params=params or {"policy": sf.DEFAULT_POLICY.to_dict()},
    )
    from app.models import MediaIntelRun

    return db.get(MediaIntelRun, dto["id"])


def _doc(voice_duration: float = 8.0, *, cuts: bool = True):
    """A doc with ONE voice clip 0..8 and caption cues across the same span."""
    from app.engine.timeline import add_clip, create_empty

    doc = create_empty("ws", duration_seconds=voice_duration)
    add_clip(doc, track="voice", clip_id="v1", name="narration", start=0.0,
             duration=voice_duration)
    if cuts:
        add_clip(doc, track="caption", clip_id="c1", name="before", start=0.0,
                 duration=2.5)
        add_clip(doc, track="caption", clip_id="c2", name="middle", start=2.5,
                 duration=3.0)
        add_clip(doc, track="caption", clip_id="c3", name="after", start=5.5,
                 duration=2.5)
    return doc


def _words(*rows):
    """``("uh", 1.0, 1.2)`` triples -> timed word units."""
    return [{"text": w, "start_s": s, "end_s": e, "unit": "word"} for w, s, e in rows]


# ---------------------------------------------------------------------------
# policy (contracts §7)
# ---------------------------------------------------------------------------


def test_policy_defaults_are_conservative():
    policy = sf.DEFAULT_POLICY
    assert policy.auto_apply is False, "auto_apply must default OFF"
    assert policy.min_silence_s == 0.8
    assert policy.keep_padding_s == 0.15
    assert policy.filler_policy == "remove"
    assert policy.max_removal_ratio == 0.25
    assert len(policy.policy_id) == 32


def test_policy_id_is_stable_and_sensitive():
    a = sf.EditPolicy(min_silence_s=0.8)
    b = sf.EditPolicy(min_silence_s=0.8)
    c = sf.EditPolicy(min_silence_s=1.2)
    assert a.policy_id == b.policy_id
    assert a.policy_id != c.policy_id


def test_policy_from_dict_rejects_unknown_keys_and_bad_values():
    with pytest.raises(ValueError, match="unknown policy key"):
        sf.EditPolicy.from_dict({"min_silnce_s": 1.0})
    with pytest.raises(ValueError, match="min_silence_s"):
        sf.EditPolicy.from_dict({"min_silence_s": 0})
    with pytest.raises(ValueError, match="filler_policy"):
        sf.EditPolicy.from_dict({"filler_policy": "nuke"})
    with pytest.raises(ValueError, match="max_removal_ratio"):
        sf.EditPolicy.from_dict({"max_removal_ratio": 0})
    with pytest.raises(ValueError, match="keep_padding_s"):
        sf.EditPolicy.from_dict({"keep_padding_s": -1})
    built = sf.EditPolicy.from_dict({"min_silence_s": 1.5, "filler_policy": "SHORTEN"})
    assert built.min_silence_s == 1.5 and built.filler_policy == "shorten"


# ---------------------------------------------------------------------------
# time map (contracts §7)
# ---------------------------------------------------------------------------


def test_time_map_segments_and_shifts():
    tm = sf.TimeMap.from_cut_ranges([(3.15, 4.85)], 8.0)
    assert [s["src_start"] for s in tm.segments] == [0.0, 4.85]
    assert [s["src_end"] for s in tm.segments] == [3.15, 8.0]
    assert tm.segments[0]["out_start"] == 0.0
    assert tm.segments[1]["out_start"] == 3.15
    assert tm.output_duration_s == 6.3
    assert tm.removed_duration_s == 1.7
    assert tm.removals == [(3.15, 4.85)]


def test_time_map_round_trip_is_lossless_on_kept_spans():
    tm = sf.TimeMap.from_cut_ranges([(3.15, 4.85)], 8.0)
    for t in (0.0, 1.0, 3.15, 5.0, 7.999, 8.0):
        assert tm.inverse_time(tm.map_time(t)) == pytest.approx(t, abs=1e-6)
    # inside the removed gap the clamp is forward and honestly lossy
    assert tm.map_time(4.0) == 3.15
    assert tm.map_time(4.86) == 3.16


def test_time_map_range_maps_to_edited_span():
    tm = sf.TimeMap.from_cut_ranges([(2.0, 3.0)], 8.0)
    assert tm.map_range(0.0, 8.0) == {"start": 0.0, "end": 7.0, "duration": 7.0}
    assert tm.map_range(3.0, 5.0) == {"start": 2.0, "end": 4.0, "duration": 2.0}


def test_time_map_merges_overlapping_and_tiny_cuts():
    tm = sf.TimeMap.from_cut_ranges([(2.0, 3.0), (2.5, 4.0), (5.0, 5.01)], 8.0)
    assert tm.removals == [(2.0, 4.0)], "overlapping cuts merge, sub-MIN_CUT_S drops"


def test_time_map_from_persisted_round_trips(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id)
    policy = sf.EditPolicy(keep_padding_s=0.2)
    row = sf.build_time_map(db_session, ws.id, asset.id, policy, [(3.2, 4.8)], 8.0)
    db_session.commit()
    again = sf.build_time_map(db_session, ws.id, asset.id, policy, [(1.0, 1.5)], 8.0)
    assert again.id == row.id, "one map per (workspace, asset, policy) -- not a pile-up"
    restored = sf.TimeMap.from_persisted(again)
    assert restored.removals == [(1.0, 1.5)]
    assert sf.TimeMap.from_persisted(None) is None


# ---------------------------------------------------------------------------
# cut ranges (pure policy -> cut)
# ---------------------------------------------------------------------------


def test_cut_range_for_keeps_and_shorts():
    policy = sf.EditPolicy(shorten_to_s=0.4)
    assert sf.cut_range_for("REMOVE_RANGE", 1.0, 2.0, policy) == (1.0, 2.0)
    assert sf.cut_range_for("SHORTEN_RANGE", 1.0, 2.0, policy) == (1.4, 2.0)
    assert sf.cut_range_for("KEEP", 1.0, 2.0, policy) == (1.0, 1.0)
    # a range shorter than the kept head can never be shortened into a cut
    assert sf.cut_range_for("SHORTEN_RANGE", 1.0, 1.2, policy) == (1.2, 1.2)


# ---------------------------------------------------------------------------
# filler detection (contracts §7) -- no ffmpeg needed
# ---------------------------------------------------------------------------


def test_filler_lexicon_is_versioned_and_documented():
    assert sf.FILLER_LEXICON_VERSION == "1.0.0"
    assert "um" in sf.FILLER_LEXICON and "you know" in sf.FILLER_LEXICON
    assert set(sf.FILLER_LEXICON).isdisjoint(sf.WEAK_FILLER_LEXICON)


def test_word_level_filler_stutter_and_false_start():
    units = _words(
        ("um", 0.5, 0.7), ("the", 1.0, 1.2), ("the", 1.25, 1.45),
        ("so", 1.6, 1.9), ("the", 2.0, 2.2), ("plan", 2.3, 2.6), ("for", 2.7, 2.9),
        ("q4", 3.0, 3.2), ("ok", 3.3, 3.5),
        ("so", 3.6, 3.9), ("the", 4.0, 4.2), ("plan", 4.3, 4.6), ("for", 4.7, 4.9),
        ("q4", 5.0, 5.2),
    )
    findings = sf.find_fillers(units)
    reasons = [f["reason"] for f in findings]
    assert reasons == ["FILLER_WORD", "STUTTER", "REPEATED_PHRASE"]
    filler = next(f for f in findings if f["reason"] == "FILLER_WORD")
    assert filler["match"] == "um"
    assert (filler["start_s"], filler["end_s"]) == (0.5, 0.7)
    assert filler["confidence"] == pytest.approx(sf.CONFIDENCE["word"]["lexicon"])
    assert filler["evidence"]["lexicon_version"] == sf.FILLER_LEXICON_VERSION
    assert filler["evidence"]["rule"] == "FILLER_WORD"
    stutter = next(f for f in findings if f["reason"] == "STUTTER")
    assert stutter["match"] == "the the"
    assert (stutter["start_s"], stutter["end_s"]) == (1.0, 1.45)
    # the false start is the SECOND take (3.6), never the first (1.6)
    repeat = next(f for f in findings if f["reason"] == "REPEATED_PHRASE")
    assert repeat["start_s"] == 3.6
    assert repeat["match"] == "so the plan for"
    assert repeat["evidence"]["phrase_words"] == 4
    assert repeat["evidence"]["gap_s"] == 0.7
    assert repeat["evidence"]["first_start_s"] == 1.6


def test_a_repeat_outside_the_window_is_not_a_false_start():
    units = _words(
        ("so", 0.0, 0.3), ("the", 0.4, 0.6), ("plan", 0.7, 1.0), ("for", 1.1, 1.3),
        ("q4", 1.4, 1.6), ("ok", 1.7, 1.9),
        ("so", 20.0, 20.3), ("the", 20.4, 20.6), ("plan", 20.7, 21.0), ("for", 21.1, 21.3),
        ("q4", 21.4, 21.6),
    )
    assert [f["reason"] for f in sf.find_fillers(units)] == []


def test_weak_lexicon_is_off_by_default_and_lower_confidence_when_on():
    units = _words(("we", 0.0, 0.3), ("basically", 0.4, 0.8), ("done", 0.9, 1.1))
    assert [f["reason"] for f in sf.find_fillers(units)] == []
    on = sf.find_fillers(units, sf.EditPolicy(include_weak_fillers=True))
    assert [f["match"] for f in on] == ["basically"]
    assert on[0]["evidence"]["weak_lexicon"] is True
    assert on[0]["confidence"] == pytest.approx(
        sf.CONFIDENCE["word"]["lexicon"] * sf.WEAK_CONFIDENCE_FACTOR
    )


def test_cue_level_evidence_is_lower_confidence_than_word_level():
    word = sf.find_fillers(_words(("um", 1.0, 1.2)))[0]
    cue = sf.find_fillers([{"text": "um", "start_s": 1.0, "end_s": 1.4,
                            "unit": "cue"}])[0]
    assert word["unit"] == "word"
    assert cue["unit"] == "cue"
    assert word["confidence"] > cue["confidence"]
    assert cue["evidence"]["unit"] == "cue"


def test_no_timed_source_yields_no_finding():
    assert sf.find_fillers([]) == []
    assert sf.find_fillers([{"text": "   ", "start_s": 0.0, "end_s": 1.0}]) == []


# ---------------------------------------------------------------------------
# policy decisions: KEEP / REMOVE_RANGE / SHORTEN_RANGE
# ---------------------------------------------------------------------------


def test_filler_policy_remove_shorten_and_keep():
    finding = {"reason": "FILLER_WORD", "start_s": 1.0, "end_s": 2.0,
               "confidence": 0.75, "evidence": {}}
    removed = sf.proposal_for_finding(finding, sf.EditPolicy(filler_policy="remove"))
    assert removed["kind"] == "REMOVE_RANGE"
    assert (removed["evidence"]["cut_start_s"], removed["evidence"]["cut_end_s"]) == (1.0, 2.0)

    shortened = sf.proposal_for_finding(finding, sf.EditPolicy(filler_policy="shorten"))
    assert shortened["kind"] == "SHORTEN_RANGE"
    assert shortened["evidence"]["kept_head_s"] == 0.4
    assert shortened["evidence"]["cut_start_s"] == 1.4

    kept = sf.proposal_for_finding(finding, sf.EditPolicy(filler_policy="keep"))
    assert kept["kind"] == "KEEP"
    assert kept["evidence"]["cut_duration_s"] == 0.0


def test_shorten_on_a_range_that_cannot_give_anything_is_keep_not_a_no_op_op():
    finding = {"reason": "FILLER_WORD", "start_s": 1.0, "end_s": 1.1,
               "confidence": 0.75, "evidence": {}}
    proposal = sf.proposal_for_finding(
        finding, sf.EditPolicy(filler_policy="shorten", shorten_to_s=0.4)
    )
    assert proposal["kind"] == "KEEP"
    assert proposal["evidence"]["rule"] == "shorten_noop"
    assert proposal["evidence"]["cut_duration_s"] == 0.0


# ---------------------------------------------------------------------------
# proposals: nothing destructive before approval
# ---------------------------------------------------------------------------


def test_detection_only_writes_proposed_rows(db_session, workspace_with_user, tmp_path):
    """A detection pass never decides anything: PROPOSED + decision NULL."""
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)

    result = sf.detect_silence(db_session, ws, asset, run, path,
                               sf.EditPolicy(min_silence_s=0.5))
    db_session.commit()
    assert result["applied"] is False
    assert result["auto_apply"] is False
    rows = sf.proposals_for_run(db_session, run.id)
    assert rows, "the stubbed fixture has one measurable gap"
    for row in rows:
        assert row.status == "PROPOSED"
        assert row.decision is None
        assert row.kind in ("REMOVE_RANGE", "SHORTEN_RANGE", "KEEP")
        assert row.reason in sf.REASONS
        assert 0.0 <= row.confidence <= 1.0
    # the SOURCE asset row is untouched: still one asset, same checksum/key
    from app.models import MediaAsset

    again = db_session.get(MediaAsset, asset.id)
    assert again.storage_key == asset.storage_key
    assert again.parent_asset_id is None, "a detection pass derives no asset"


def test_max_removal_ratio_forces_keep_and_flags_qc(db_session, workspace_with_user,
                                                   tmp_path):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)
    from app.models import IntelQCResult

    generous = sf.detect_silence(db_session, ws, asset, run, path,
                                 sf.EditPolicy(min_silence_s=0.5,
                                               max_removal_ratio=0.95))
    assert generous["removal_ratio"]["triggered"] is False
    assert generous["qc"]["verdict"] == "PASS"
    assert {r.kind for r in sf.proposals_for_run(db_session, run.id)} == {"REMOVE_RANGE"}

    run2_asset = _asset(db_session, ws.id, duration=8.0, checksum="sum-silence-2")
    run2 = _run(db_session, ws, run2_asset, kind="silence", params={"policy": "strict"})
    strict = sf.detect_silence(db_session, ws, run2_asset, run2, path,
                               sf.EditPolicy(min_silence_s=0.5, max_removal_ratio=0.10))
    assert strict["removal_ratio"]["triggered"] is True
    assert strict["qc"]["check"] == "excessive_removed_speech"
    assert strict["qc"]["verdict"] == "FAIL" and strict["qc"]["force_keep"] is True
    assert strict["qc"]["source"] == "app.engine.intel.qc"
    rows = sf.proposals_for_run(db_session, run2.id)
    assert rows and {r.kind for r in rows} == {"KEEP"}
    assert {r.reason for r in rows} == {"MAX_REMOVAL_RATIO"}
    assert all(r.decision is None for r in rows), "forced KEEP is still unapproved"
    verdicts = db_session.query(IntelQCResult).filter(
        IntelQCResult.run_id == run2.id).all()
    # Lane H's own aggregate. Its severity is REVIEW, not HARD, precisely
    # because this lane has NO speech-activity measurement to divide by --
    # claiming a hard failure on an unmeasured denominator would be dishonest.
    assert [v.verdict for v in verdicts] == ["REVIEW_REQUIRED"]
    check = verdicts[0].checks_json[0]
    assert check["name"] == "excessive_removed_speech"
    assert check["verdict"] == "VIOLATION" and check["ok"] is False
    assert check["evidence"]["force_keep"] is True, "H's force_keep signal"
    assert check["evidence"]["denominator"] == "source_duration"
    assert check["measured"] > check["threshold"]
    # the forced proposals carry the SAME signal in their own evidence
    evidence = sf.list_proposals(db_session, ws.id, run_id=run2.id)[0]["evidence"]
    assert evidence["force_keep"] is True
    assert evidence["qc_check"]["name"] == "excessive_removed_speech"
    assert evidence["max_removal_ratio"]["triggered"] is True


def test_measured_speech_activity_upgrades_the_verdict_to_a_hard_fail(
        db_session, workspace_with_user, tmp_path):
    """With a real VAD denominator, Lane H's check is HARD and the run FAILs."""
    from app.engine.intel import qc as intel_qc
    from app.models import DiarizationSegment, IntelQCResult

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)
    # the fixture is 3 s speech / 2 s gap / 3 s speech
    db_session.add_all([
        DiarizationSegment(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                           kind="SPEECH_ACTIVITY", speaker_id=None,
                           start_s=0.0, end_s=3.0),
        DiarizationSegment(run_id=run.id, workspace_id=ws.id, asset_id=asset.id,
                           kind="SPEECH_ACTIVITY", speaker_id=None,
                           start_s=5.0, end_s=8.0),
    ])
    db_session.flush()
    assert sf.speech_activity_seconds(db_session, ws.id, asset.id) == 6.0
    assert sf.speech_activity_seconds(db_session, ws.id, "nope") is None

    result = sf.detect_silence(db_session, ws, asset, run, path,
                               sf.EditPolicy(min_silence_s=0.5, max_removal_ratio=0.20))
    assert result["removal_ratio"]["triggered"] is True
    assert result["removal_ratio"]["speech_activity_s"] == 6.0
    assert result["removal_ratio"]["denominator"] == "speech_activity"
    assert result["removal_ratio"]["qc_verdict"] == "FAIL"
    row = db_session.query(IntelQCResult).filter(
        IntelQCResult.run_id == run.id).one()
    check = row.checks_json[0]
    assert check["severity"] == intel_qc.SEVERITY_HARD
    assert check["evidence"]["force_keep"] is True
    assert check["evidence"]["denominator"] == "speech_activity"
    # ... and the gate now genuinely blocks an apply for this run
    with pytest.raises(intel_qc.QCApplyBlocked) as excinfo:
        intel_qc.assert_qc_allows_apply(db_session, ws, run.id)
    assert excinfo.value.failures == ["excessive_removed_speech"]
    assert excinfo.value.needs_override is True


def test_proposals_never_carry_a_cut_before_a_decision(db_session, workspace_with_user,
                                                       tmp_path):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)
    result = sf.detect_silence(db_session, ws, asset, run, path,
                               sf.EditPolicy(min_silence_s=0.5))
    db_session.commit()
    assert all(p["operations"] == [] for p in result["proposals"])
    with pytest.raises(ValueError, match="no decided proposal"):
        sf.plan_apply(db_session, ws.id, asset_id=asset.id)


def test_decide_then_apply_is_the_only_path_to_a_timeline_mutation(
        db_session, workspace_with_user, tmp_path):
    actor = workspace_with_user["user"]
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)
    sf.detect_silence(db_session, ws, asset, run, path, sf.EditPolicy(min_silence_s=0.5))
    db_session.commit()
    row = sf.proposals_for_run(db_session, run.id)[0]

    with pytest.raises(KeyError):
        sf.decide_proposal(db_session, ws.id, "nope", "remove")
    with pytest.raises(ValueError, match="decision must be one of"):
        sf.decide_proposal(db_session, ws.id, row.id, "nuke")

    decided = sf.decide_proposal(db_session, ws.id, row.id, "remove", user_id=actor)
    assert decided.status == "DECIDED" and decided.decision == "remove"
    assert decided.decided_at is not None and decided.decided_by == actor
    plan = sf.plan_apply(db_session, ws.id, proposal_ids=[row.id])
    assert [r.id for r in plan["rows"]] == [row.id]

    # APPLIED is terminal: the timeline it reached no longer matches the proposal
    sf.mark_applied(db_session, [row.id])
    with pytest.raises(ValueError, match="already APPLIED"):
        sf.decide_proposal(db_session, ws.id, row.id, "keep")
    with pytest.raises(ValueError, match="no decided proposal"):
        sf.plan_apply(db_session, ws.id, proposal_ids=[row.id])


def test_apply_refuses_proposals_from_two_assets(db_session, workspace_with_user,
                                                 tmp_path):
    ws = _ws(db_session, workspace_with_user)
    ids = []
    for i in range(2):
        asset = _asset(db_session, ws.id, duration=8.0, checksum=f"c{i}")
        run = _run(db_session, ws, asset, params={"n": i})
        path = _stub_media(db_session, ws.id, asset, tmp_path)
        sf.detect_silence(db_session, ws, asset, run, path,
                          sf.EditPolicy(min_silence_s=0.5))
        ids += [r.id for r in sf.proposals_for_run(db_session, run.id)]
    for pid in ids:
        sf.decide_proposal(db_session, ws.id, pid, "remove")
    with pytest.raises(ValueError, match="different assets"):
        sf.plan_apply(db_session, ws.id, proposal_ids=ids)


# ---------------------------------------------------------------------------
# op generation (canonical Work 02 vocabulary)
# ---------------------------------------------------------------------------


def test_generated_ops_are_inside_op_types():
    tm = sf.TimeMap.from_cut_ranges([(3.15, 4.85)], 8.0)
    ops = sf.build_cut_operations(_doc(), tm)
    assert ops
    from app.engine.timeline_ops import OP_TYPES

    assert {op["type"] for op in ops} <= set(OP_TYPES)
    assert {op["type"] for op in ops} == {
        "split_item", "delete_item", "move_item", "update_caption",
    }


def test_cut_is_split_delete_move_not_a_doc_rewrite():
    tm = sf.TimeMap.from_cut_ranges([(3.15, 4.85)], 8.0)
    ops = sf.build_cut_operations(_doc(), tm)
    assert ops[0] == {"type": "split_item", "track": "voice", "clip_id": "v1", "at": 3.15}
    assert ops[1] == {"type": "split_item", "track": "voice", "clip_id": "v1__b",
                      "at": 4.85}
    assert ops[2] == {"type": "delete_item", "track": "voice", "clip_id": "v1__b"}
    assert ops[3] == {"type": "move_item", "track": "voice", "clip_id": "v1__b__b",
                      "start": 3.15}


def test_a_cut_consuming_the_head_slides_the_tail_back():
    tm = sf.TimeMap.from_cut_ranges([(0.0, 2.0)], 8.0)
    ops = sf.build_cut_operations(_doc(), tm)
    assert [op["type"] for op in ops[:3]] == ["split_item", "delete_item", "move_item"]
    assert ops[2]["clip_id"] == "v1__b" and ops[2]["start"] == 0.0


def test_a_cut_consuming_the_tail_keeps_the_head_in_place():
    tm = sf.TimeMap.from_cut_ranges([(6.0, 8.0)], 8.0)
    ops = sf.build_cut_operations(_doc(), tm)
    assert [op["type"] for op in ops[:2]] == ["split_item", "delete_item"]
    assert ops[1]["clip_id"] == "v1__b"
    assert not any(op["type"] == "move_item" and op["track"] == "voice" for op in ops)


def test_a_fully_consumed_audio_clip_is_deleted_whole():
    doc = _doc()
    voice = next(t for t in doc["tracks"] if t["kind"] == "voice")
    voice["clips"].append({"id": "v2", "name": "bump", "start": 8.0, "duration": 1.0,
                           "source": {}, "effects": [], "source_start": 0.0,
                           "volume": 1.0, "speed": 1.0, "fade_in": 0.0,
                           "fade_out": 0.0, "transform": {}, "text": {},
                           "transition_in": "cut", "transition_out": "cut"})
    doc["duration_seconds"] = 9.0
    tm = sf.TimeMap.from_cut_ranges([(8.0, 9.0)], 9.0)
    ops = sf.build_cut_operations(doc, tm)
    assert {"type": "delete_item", "track": "voice", "clip_id": "v2"} in ops


def test_captions_inside_a_cut_are_deleted_not_moved():
    tm = sf.TimeMap.from_cut_ranges([(2.5, 5.5)], 8.0)
    ops = sf.build_cut_operations(_doc(), tm)
    deleted = [op for op in ops if op["type"] == "delete_item" and op["track"] == "caption"]
    assert {op["clip_id"] for op in deleted} == {"c2"}


def test_two_cuts_in_one_clip_produce_two_independent_cut_sequences():
    """The second cut must land on the SHIFTED document, not on source time."""
    tm = sf.TimeMap.from_cut_ranges([(1.0, 2.0), (5.0, 6.0)], 8.0)
    ops = sf.build_cut_operations(_doc(), tm)
    voice = [op for op in ops if op["track"] == "voice"]
    assert [op["type"] for op in voice] == [
        "split_item", "split_item", "delete_item", "move_item",      # cut (1, 2)
        "split_item", "split_item", "delete_item", "move_item",      # cut (5, 6)
    ]
    assert (voice[0]["at"], voice[1]["at"]) == (1.0, 2.0)
    assert voice[3]["clip_id"] == "v1__b__b" and voice[3]["start"] == 1.0
    # 1.0 s was already removed, so the second cut's split points are 4.0 / 5.0
    assert (voice[4]["at"], voice[5]["at"]) == (4.0, 5.0)
    assert voice[4]["clip_id"] == "v1__b__b"
    assert voice[-1]["clip_id"] == "v1__b__b__b__b" and voice[-1]["start"] == 4.0

    from app.engine.timeline_ops import apply_operations

    after = apply_operations(_doc(), ops)
    clips = sorted(next(t for t in after["tracks"]
                        if t["kind"] == "voice")["clips"], key=lambda c: c["start"])
    assert [(c["start"], c["duration"]) for c in clips] == [
        (0.0, 1.0), (1.0, 3.0), (4.0, 2.0),
    ]
    assert sum(c["duration"] for c in clips) == pytest.approx(6.0)


def test_build_cut_operations_refuses_a_batch_over_the_work02_ceiling():
    from app.engine.timeline import add_clip, create_empty

    doc = create_empty("ws", duration_seconds=1200.0)
    for i in range(120):
        add_clip(doc, track="voice", clip_id=f"v{i}", name="c", start=i * 10.0,
                 duration=10.0)
    add_clip(doc, track="caption", clip_id="cap0", name="x", start=0.0, duration=5.0)
    cuts = [(i * 10.0 + 2.0, i * 10.0 + 3.0) for i in range(110)]
    with pytest.raises(ValueError, match="ceiling"):
        sf.build_cut_operations(doc, sf.TimeMap.from_cut_ranges(cuts, 1200.0))


def test_no_cuts_means_no_operations():
    assert sf.build_cut_operations(_doc(), sf.TimeMap.from_cut_ranges([], 8.0)) == []


# ---------------------------------------------------------------------------
# timeline sync after cuts (contracts §16 row)
# ---------------------------------------------------------------------------


def test_timeline_sync_after_cuts():
    """1.7 s removed: captions shift by EXACTLY 1.7 s, audio shrinks by 1.7 s."""
    from app.engine.timeline_ops import apply_operations

    doc = _doc(8.0)
    before_voice = sum(c["duration"] for c in
                       next(t for t in doc["tracks"] if t["kind"] == "voice")["clips"])
    before_caps = {c["id"]: (c["start"], c["duration"]) for c in
                   next(t for t in doc["tracks"] if t["kind"] == "caption")["clips"]}
    assert before_voice == 8.0

    policy = sf.EditPolicy(keep_padding_s=0.15)
    tm = sf.TimeMap.from_cut_ranges([(3.15, 4.85)], 8.0)
    after = apply_operations(doc, sf.build_cut_operations(doc, tm, policy))

    voice_clips = next(t for t in after["tracks"] if t["kind"] == "voice")["clips"]
    caps = {c["id"]: (c["start"], c["duration"]) for c in
            next(t for t in after["tracks"] if t["kind"] == "caption")["clips"]}

    # audio: the removed 1.7 s is gone, and the pieces still tile 0..6.3
    assert sum(c["duration"] for c in voice_clips) == pytest.approx(6.3)
    cursor = 0.0
    for clip in sorted(voice_clips, key=lambda c: c["start"]):
        assert clip["start"] == pytest.approx(cursor)
        cursor += clip["duration"]
    assert cursor == pytest.approx(6.3)
    # source offsets are preserved: the tail still reads the SOURCE at 4.85
    tail = max(voice_clips, key=lambda c: c["start"])
    assert tail["source_start"] == pytest.approx(4.85)

    # captions: c1 is before the cut (untouched), c3 after it (shifted by 1.7)
    assert caps["c1"][0] == pytest.approx(before_caps["c1"][0])
    assert caps["c1"][1] == pytest.approx(before_caps["c1"][1])
    assert caps["c3"][0] == pytest.approx(before_caps["c3"][0] - 1.7)
    assert caps["c3"][1] == pytest.approx(before_caps["c3"][1])
    # c2 spans the cut: 2.5..5.5 loses its middle, so it shrinks and starts earlier
    assert caps["c2"][1] < before_caps["c2"][1]
    assert caps["c2"][0] == pytest.approx(2.5)
    # every caption is still ordered and inside the shrunken document
    assert after["duration_seconds"] == pytest.approx(6.3)


def test_timeline_stays_valid_after_every_kept_clip_is_cut():
    from app.engine.timeline_ops import apply_operations

    doc = _doc(8.0)
    tm = sf.TimeMap.from_cut_ranges([(0.0, 3.0), (3.0, 5.0), (5.0, 8.0)], 8.0)
    after = apply_operations(doc, sf.build_cut_operations(doc, tm))
    voice = next(t for t in after["tracks"] if t["kind"] == "voice")["clips"]
    assert voice == [], "the whole asset was removed -- nothing may remain"
    assert next(t for t in after["tracks"] if t["kind"] == "caption")["clips"] == []


def test_untouched_clips_after_a_cut_are_moved_not_re_timed():
    from app.engine.timeline_ops import apply_operations

    doc = _doc(8.0)
    tm = sf.TimeMap.from_cut_ranges([(1.0, 2.0)], 8.0)
    ops = sf.build_cut_operations(doc, tm)
    assert {"type": "move_item", "track": "caption", "clip_id": "c3",
            "start": 4.5} in ops
    after = apply_operations(doc, ops)
    caps = {c["id"]: c for c in next(t for t in after["tracks"] if t["kind"] == "caption")["clips"]}
    assert caps["c3"]["start"] == pytest.approx(4.5)
    assert caps["c3"]["duration"] == pytest.approx(2.5)


def test_scenes_are_mapped_through_the_same_time_map(db_session, workspace_with_user):
    from app.models.assets import Scene

    ws = _ws(db_session, workspace_with_user)
    tl = _timeline(db_session, ws.id, _doc(8.0).get("tracks"))
    db_session.add_all([
        Scene(workspace_id=ws.id, timeline_id=tl.id, index=0, title="a",
              start_seconds=0.0, end_seconds=3.0),
        Scene(workspace_id=ws.id, timeline_id=tl.id, index=1, title="b",
              start_seconds=5.0, end_seconds=8.0),
        Scene(workspace_id=ws.id, timeline_id=tl.id, index=2, title="c",
              start_seconds=3.5, end_seconds=4.0),
    ])
    db_session.flush()
    tm = sf.TimeMap.from_cut_ranges([(3.15, 4.85)], 8.0)
    assert sf.map_scenes(db_session, ws.id, tl.id, tm) == 2, "scene 'a' is before the cut"
    rows = {sc.title: (sc.start_seconds, sc.end_seconds)
            for sc in db_session.query(Scene).all()}
    assert rows["a"] == (0.0, 3.0)
    assert rows["b"] == (3.3, 6.3)
    assert rows["c"] == (3.15, 3.15), "a scene a cut consumed collapses, it is not deleted"


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_proposals_and_time_maps_are_workspace_scoped(db_session, workspace_with_user,
                                                      tmp_path):
    from app.models import User, Workspace, WorkspaceMember

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)
    sf.detect_silence(db_session, ws, asset, run, path, sf.EditPolicy(min_silence_s=0.5))
    row = sf.proposals_for_run(db_session, run.id)[0]
    db_session.commit()

    other = Workspace(name="Other", slug=f"ws-{uuid.uuid4().hex[:8]}", niche="AI money")
    user = User(email=f"d{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
    db_session.add_all([other, user])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                                   role=WorkspaceMember.ROLE_OWNER))
    db_session.flush()

    assert sf.get_proposal(db_session, ws.id, row.id) is not None
    assert sf.get_proposal(db_session, other.id, row.id) is None
    assert [p["id"] for p in sf.list_proposals(db_session, other.id)] == []
    assert sf.load_time_map(db_session, other.id, asset.id) is None
    with pytest.raises(KeyError):
        sf.plan_apply(db_session, other.id, proposal_ids=[row.id])
    with pytest.raises(KeyError):
        sf.decide_proposal(db_session, other.id, row.id, "remove")


# ---------------------------------------------------------------------------
# word-source resolution
# ---------------------------------------------------------------------------


def test_word_rows_are_preferred_and_the_source_is_reported(db_session,
                                                            workspace_with_user):
    from app.models import MediaIntelWord

    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset, kind="alignment",
               params={"kind": "alignment"})
    for i, (word, start, end) in enumerate([("um", 1.0, 1.2), ("hello", 1.5, 1.9)]):
        db_session.add(MediaIntelWord(run_id=run.id, workspace_id=ws.id,
                                      asset_id=asset.id, idx=i, word=word,
                                      start_s=start, end_s=end))
    db_session.flush()
    source = sf.resolve_units(db_session, ws.id, asset.id)
    assert source["source"] == "media_intel_words"
    assert source["unit"] == "word"
    assert source["available"] is True
    assert source["run_id"] == run.id
    assert len(source["units"]) == 2


def test_cue_fallback_is_used_and_declared(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    tl = _timeline(db_session, ws.id, _doc(8.0).get("tracks"))
    source = sf.resolve_units(db_session, ws.id, asset.id, timeline_id=tl.id)
    assert source["source"] == "caption_cues"
    assert source["unit"] == "cue"
    assert source["units"] == [] or source["units"][0]["unit"] == "cue"


def test_no_timed_source_is_honest_about_it(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    source = sf.resolve_units(db_session, ws.id, asset.id)
    assert source["available"] is False
    assert source["source"] == "none"
    assert "invented" in source["reason"]


def test_request_cues_are_the_last_resort(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    source = sf.resolve_units(db_session, ws.id, asset.id,
                              cues=[{"text": "um", "start_s": 1.0, "end_s": 1.3}])
    assert source["source"] == "request_cues"
    assert source["unit"] == "cue"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _stub_media(db, workspace_id: str, asset, tmp_path) -> str:
    """A REAL 8 s patterned WAV written into the workspace storage root.

    The fast battery must not shell out to ffmpeg per test, so the bytes are
    built once per session and copied per test (the ffmpeg truth of the same
    fixture is asserted in the slow battery).
    """
    from pathlib import Path

    source = _session_wav(tmp_path)
    dest = Path(str(asset.storage_key))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    return str(dest)


_SESSION_WAV = None


def _session_wav(tmp_path):
    global _SESSION_WAV
    if _SESSION_WAV is None:
        from tests.media_intel_fixtures import patterned_speechlike_wav

        _SESSION_WAV = patterned_speechlike_wav(tmp_path / "shared-speechlike.wav")
    return _SESSION_WAV


def _timeline(db, workspace_id: str, tracks):
    from app.engine.timeline import validate_timeline
    from app.models import ContentTimeline

    doc = {"tracks": tracks, "duration_seconds": 8.0, "fps": 30.0, "aspect_ratio": "9:16"}
    validate_timeline(doc)
    row = ContentTimeline(workspace_id=workspace_id, name="d", fps=30.0,
                          duration_seconds=8.0, tracks_json=doc)
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _register(client, email=None):
    email = email or f"d{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from app.api.v1.media_intel_edits import media_intel_edits_router
    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    app = create_app()
    prefix = "/api/v1/workspaces/{workspace_id}/media-intel/audio/silence"
    if not any(p.startswith(prefix) for p in app.openapi()["paths"]):
        # The orchestrator registers the router in api/v1/__init__.py at
        # integration time; mount it under the same /api/v1 prefix until it
        # does, so the route contract is exercised either way.
        holder = APIRouter(prefix="/api/v1")
        holder.include_router(media_intel_edits_router)
        app.include_router(holder)
    return TestClient(app, raise_server_exceptions=False)


def _seed_proposals(tmp_path, monkeypatch, client):
    """A registered client + a DECIDED proposal + a timeline to apply it to."""
    from app.db import session_scope
    from app.models import Workspace

    ws_id, headers = _register(client)
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        asset = _asset(s, ws_id, duration=8.0)
        run = _run(s, ws, asset)
        path = _stub_media(s, ws_id, asset, tmp_path)
        sf.detect_silence(s, ws, asset, run, path, sf.EditPolicy(min_silence_s=0.5))
        row = sf.proposals_for_run(s, run.id)[0]
        sf.decide_proposal(s, ws_id, row.id, "remove")
        tl = _timeline(s, ws_id, _doc(8.0).get("tracks"))
        # The QC verdict the apply gate demands, recorded with H's own helpers
        # from the REAL measured numbers: 1.7 s of 6.0 s of speech is 0.283,
        # inside QC's own 0.35 ceiling, so the verdict is PASS.
        qc = _record_qc(s, ws, run, removed_s=1.7, speech_activity_s=6.0,
                        max_removal_ratio=0.35)
        payload = {"ws": ws_id, "asset": asset.id, "proposal": row.id,
                   "timeline": tl.id, "version": tl.version, "run": run.id,
                   "qc": qc.id}
    return ws_id, headers, payload


def test_routes_are_registered_with_the_contract_prefix(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    paths = client.app.openapi()["paths"]
    for path in ("/api/v1/workspaces/{workspace_id}/media-intel/audio/silence",
                 "/api/v1/workspaces/{workspace_id}/media-intel/audio/fillers",
                 "/api/v1/workspaces/{workspace_id}/media-intel/proposals",
                 "/api/v1/workspaces/{workspace_id}/media-intel/proposals/{proposal_id}/decide",
                 "/api/v1/workspaces/{workspace_id}/media-intel/proposals/apply",
                 "/api/v1/workspaces/{workspace_id}/media-intel/time-map"):
        assert path in paths, path
    assert "media-intel-edits" in paths[
        "/api/v1/workspaces/{workspace_id}/media-intel/proposals"]["get"]["tags"]


def test_proposals_listing_and_decide_route(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, data = _seed_proposals(tmp_path, monkeypatch, client)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"

    r = client.get(f"{base}/proposals?asset_id={data['asset']}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    proposal = body["items"][0]
    assert proposal["reason"] in sf.REASONS
    assert proposal["evidence"]["detector"] == sf.SILENCE_PROVIDER
    assert proposal["confidence"] is not None

    r = client.post(f"{base}/proposals/{data['proposal']}/decide", headers=headers,
                    json={"decision": "keep"})
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "keep" and r.json()["status"] == "DECIDED"

    r = client.post(f"{base}/proposals/{data['proposal']}/decide", headers=headers,
                    json={"decision": "nuke"})
    assert r.status_code == 422, r.text
    r = client.get(f"{base}/proposals/{data['proposal']}", headers=headers)
    assert r.status_code == 404, r.text


def test_foreign_ids_are_404_not_403(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _ws1, headers1, data = _seed_proposals(tmp_path, monkeypatch, client)
    ws2, headers2 = _register(client)
    base = f"/api/v1/workspaces/{ws2}/media-intel"

    assert client.get(f"{base}/proposals", headers=headers2).json()["total"] == 0
    r = client.post(f"{base}/proposals/{data['proposal']}/decide", headers=headers2,
                    json={"decision": "remove"})
    assert r.status_code == 404, r.text
    r = client.post(f"{base}/proposals/apply", headers=headers2,
                    json={"timeline_id": data["timeline"], "base_version": 1,
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 404, r.text
    r = client.get(f"{base}/time-map?asset_id={data['asset']}", headers=headers2)
    assert r.status_code == 404, r.text
    r = client.post(f"{base}/audio/silence", headers=headers2,
                    json={"asset_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 404, r.text
    # the owner still sees their own proposal
    assert client.get(f"/api/v1/workspaces/{_ws1}/media-intel/proposals",
                      headers=headers1).json()["total"] == 1


def test_detect_routes_reject_a_bad_policy_with_422(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, data = _seed_proposals(tmp_path, monkeypatch, client)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"
    r = client.post(f"{base}/audio/silence", headers=headers,
                    json={"asset_id": data["asset"], "policy": {"min_silnce_s": 1}})
    assert r.status_code == 422, r.text
    r = client.post(f"{base}/audio/fillers", headers=headers,
                    json={"asset_id": data["asset"], "policy": {"filler_policy": "nuke"}})
    assert r.status_code == 422, r.text


def test_apply_through_the_canonical_operations_path(tmp_path, monkeypatch):
    """Editor operation compatibility: same route, same 409 gate, same outcome."""
    from app.db import session_scope
    from app.models import ContentTimeline

    client = _client(tmp_path, monkeypatch)
    ws_id, headers, data = _seed_proposals(tmp_path, monkeypatch, client)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"
    from app.engine.timeline_ops import OP_TYPES

    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": data["timeline"],
                          "base_version": data["version"],
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == len(body["operations"]) > 0
    assert {op["type"] for op in body["operations"]} <= set(OP_TYPES)
    # the real silencedetect end is 5.000021, so the padded cut is 1.700021 s
    assert body["time_map"]["removed_duration_s"] == pytest.approx(1.7, abs=1e-3)
    assert body["time_map"]["output_duration_s"] == pytest.approx(6.3, abs=1e-3)
    assert body["policy_id"]

    # the canonical editor route sees the SAME mutated document
    r = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{data['timeline']}",
                   headers=headers)
    assert r.status_code == 200, r.text
    timeline = r.json()
    assert timeline["version"] == data["version"] + 1
    voice = next(t for t in timeline["tracks"] if t["kind"] == "voice")["clips"]
    assert sum(c["duration"] for c in voice) == pytest.approx(6.3, abs=1e-3)
    caps = {c["id"]: c for c in
            next(t for t in timeline["tracks"] if t["kind"] == "caption")["clips"]}
    assert caps["c3"]["start"] == pytest.approx(3.8, abs=1e-3), \
        "shifted by exactly the removed duration"

    with session_scope() as s:
        row = s.get(ContentTimeline, data["timeline"])
        assert row.version == data["version"] + 1, "the Work 02 save really happened"
    # proposals are terminal APPLIED and the time map is persisted
    r = client.get(f"{base}/proposals", headers=headers)
    assert [p["status"] for p in r.json()["items"]] == ["APPLIED"]
    r = client.get(f"{base}/time-map?asset_id={data['asset']}&source_time=6.0",
                   headers=headers)
    assert r.status_code == 200, r.text
    mapped = r.json()["mapped"]["time"]
    assert mapped["source_s"] == 6.0
    assert mapped["edited_s"] == pytest.approx(4.3, abs=1e-3)
    assert mapped["inverse_s"] == pytest.approx(6.0, abs=1e-3)


def test_apply_with_a_stale_base_version_is_409_and_changes_nothing(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import ContentTimeline

    client = _client(tmp_path, monkeypatch)
    ws_id, headers, data = _seed_proposals(tmp_path, monkeypatch, client)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"

    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": data["timeline"], "base_version": 99,
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "stale timeline version — reload latest"
    with session_scope() as s:
        row = s.get(ContentTimeline, data["timeline"])
        assert row.version == data["version"], "no save happened"
        assert sum(c["duration"] for c in
                   next(t for t in row.tracks_json["tracks"]
                        if t["kind"] == "voice")["clips"]) == pytest.approx(8.0)
    r = client.get(f"{base}/proposals", headers=headers)
    assert [p["status"] for p in r.json()["items"]] == ["DECIDED"]
    r = client.get(f"{base}/time-map?asset_id={data['asset']}", headers=headers)
    assert r.status_code == 404, r.text


def test_apply_refuses_an_undecided_proposal(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Workspace

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        asset = _asset(s, ws_id, duration=8.0)
        run = _run(s, ws, asset)
        path = _stub_media(s, ws_id, asset, tmp_path)
        sf.detect_silence(s, ws, asset, run, path, sf.EditPolicy(min_silence_s=0.5))
        pid = sf.proposals_for_run(s, run.id)[0].id
        tl = _timeline(s, ws_id, _doc(8.0).get("tracks"))
        _record_qc(s, ws, run, removed_s=1.7, speech_activity_s=6.0,
                   max_removal_ratio=0.35)
        payload = {"proposal": pid, "timeline": tl.id, "version": tl.version}
    base = f"/api/v1/workspaces/{ws_id}/media-intel"
    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": payload["timeline"],
                          "base_version": payload["version"],
                          "proposal_ids": [payload["proposal"]]})
    assert r.status_code == 409, r.text
    assert "decision" in r.json()["detail"]


def test_apply_is_refused_when_qc_never_ran(tmp_path, monkeypatch):
    """No verdict == never reviewed: the gate refuses before touching the timeline."""
    from app.db import session_scope
    from app.models import ContentTimeline, IntelQCResult, Workspace

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        asset = _asset(s, ws_id, duration=8.0)
        run = _run(s, ws, asset)
        path = _stub_media(s, ws_id, asset, tmp_path)
        sf.detect_silence(s, ws, asset, run, path, sf.EditPolicy(min_silence_s=0.5))
        sf.decide_proposal(s, ws_id, sf.proposals_for_run(s, run.id)[0].id, "remove")
        tl = _timeline(s, ws_id, _doc(8.0).get("tracks"))
        data = {"timeline": tl.id, "version": tl.version, "run": run.id,
                "proposal": sf.proposals_for_run(s, run.id)[0].id}
    base = f"/api/v1/workspaces/{ws_id}/media-intel"
    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": data["timeline"],
                          "base_version": data["version"],
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["verdict"] == ""
    assert detail["needs_override"] is False, "an override can never bypass this"
    assert "QC" in detail["detail"]
    with session_scope() as s:
        row = s.get(ContentTimeline, data["timeline"])
        assert row.version == data["version"], "the timeline was never touched"
        assert s.query(IntelQCResult).filter_by(run_id=data["run"]).count() == 0, \
            "this run has no verdict -- and the gate said so instead of inventing one"


def test_a_qc_fail_blocks_apply_until_a_recorded_override(tmp_path, monkeypatch):
    """Lane H's hard FAIL is honoured: 409, then an override lets it through."""
    from app.db import session_scope
    from app.engine.intel import qc as intel_qc
    from app.models import ContentTimeline, IntelQCResult, MediaIntelRun, Workspace

    client = _client(tmp_path, monkeypatch)
    ws_id, headers, data = _seed_proposals(tmp_path, monkeypatch, client)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"

    # replace the PASS verdict with Lane H's hard FAIL for this run: 1.7 s removed
    # from only 2.0 s of measured speech is 85 %, far over the 25 % ceiling
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        run = s.get(MediaIntelRun, data["run"])
        s.query(IntelQCResult).filter_by(run_id=run.id).delete()
        s.flush()
        _record_qc(s, ws, run, removed_s=1.7, speech_activity_s=2.0,
                   max_removal_ratio=0.25)
        verdict = s.query(IntelQCResult).filter_by(run_id=run.id).one().verdict
    assert verdict == "FAIL"

    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": data["timeline"],
                          "base_version": data["version"],
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["verdict"] == "FAIL"
    assert detail["failures"] == ["excessive_removed_speech"]
    assert detail["needs_override"] is True
    with session_scope() as s:
        assert s.get(ContentTimeline, data["timeline"]).version == data["version"]

    # override=true alone is not enough: an attributable override must exist
    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": data["timeline"],
                          "base_version": data["version"], "override": True,
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 409, r.text
    assert "no recorded override" in r.json()["detail"]["detail"]

    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        result = s.query(IntelQCResult).filter_by(run_id=data["run"]).one()
        intel_qc.record_override(s, ws, result, by_user="operator",
                                 reason="reviewed the transcript by hand")
    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": data["timeline"],
                          "base_version": data["version"], "override": True,
                          "proposal_ids": [data["proposal"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["qc"][0]["verdict"] == "FAIL"
    assert body["qc"][0]["override_used"] is True
    with session_scope() as s:
        assert s.get(ContentTimeline, data["timeline"]).version == data["version"] + 1


def test_filler_route_reports_its_text_source(tmp_path, monkeypatch):
    from app.db import session_scope

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    with session_scope() as s:
        aid = _asset(s, ws_id, duration=8.0).id
    base = f"/api/v1/workspaces/{ws_id}/media-intel"
    cues = [{"text": "um so the plan", "start_s": 0.0, "end_s": 1.0},
            {"text": "for q4 ok", "start_s": 1.0, "end_s": 2.0}]
    r = client.post(f"{base}/audio/fillers", headers=headers,
                    json={"asset_id": aid, "cues": cues,
                          "policy": {"max_removal_ratio": 0.9}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_source"]["source"] == "request_cues"
    assert body["text_source"]["unit"] == "cue"
    assert body["text_source"]["lexicon_version"] == sf.FILLER_LEXICON_VERSION
    assert body["auto_apply"] is False
    assert body["counts_by_reason"] == {"FILLER_WORD": 1}
    assert all(p["status"] == "PROPOSED" for p in body["proposals"])
    assert body["events"][0]["kind"] == sf.EVENT_FILLERS_PROPOSED

    # a cue is the smallest timed unit, so one filler claims the WHOLE cue: with
    # the default 25 % ceiling that is 1.0 s of a 2.0 s span and MUST be refused
    r = client.post(f"{base}/audio/fillers", headers=headers,
                    json={"asset_id": aid, "cues": cues, "force": True})
    assert r.status_code == 200, r.text
    strict = r.json()
    assert strict["counts_by_reason"] == {"MAX_REMOVAL_RATIO": 1}
    assert strict["qc"]["verdict"] == "FAIL"
    assert strict["qc"]["force_keep"] is True
    assert all(p["kind"] == "KEEP" for p in strict["proposals"])
    assert all(p["evidence"]["force_keep"] is True for p in strict["proposals"])


def test_detection_surfaces_events_instead_of_emitting_them(db_session,
                                                            workspace_with_user,
                                                            tmp_path, monkeypatch):
    """The emission rule: the engine RETURNS events; the ROUTE emits post-commit."""
    from app.services import events as events_service

    seen: list[str] = []
    monkeypatch.setattr(
        events_service, "record_event",
        lambda ws_id, kind, message, **kw: seen.append(kind) or {},
    )
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws.id, duration=8.0)
    run = _run(db_session, ws, asset)
    path = _stub_media(db_session, ws.id, asset, tmp_path)
    seen.clear()
    result = sf.detect_silence(db_session, ws, asset, run, path,
                               sf.EditPolicy(min_silence_s=0.5))
    assert [e["kind"] for e in result["events"]] == [sf.EVENT_SILENCE_PROPOSED]
    assert sf.EVENT_SILENCE_PROPOSED not in seen, (
        "an engine must never call record_event mid-transaction (contracts §3)"
    )
    # the proposals themselves ARE durable: only the telemetry is deferred
    assert sf.proposals_for_run(db_session, run.id)


# ---------------------------------------------------------------------------
# slow: real ffmpeg measurement (contracts §16 "silence detection")
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_silence_detection_measured_truth(tmp_path):
    """The fixture truth: 8.000 s with EXACTLY one silence 3.0 -> 5.0."""
    from app.engine.intel import ffmpeg_util
    from tests.media_intel_fixtures import (
        ffmpeg_filter_ok,
        patterned_speechlike_wav,
        wav_silence_ranges,
    )

    assert ffmpeg_util.ffmpeg_available(), "this slow test needs ffmpeg on PATH"
    assert ffmpeg_filter_ok("silencedetect")
    src = patterned_speechlike_wav(tmp_path / "speechlike.wav")
    assert ffmpeg_util.duration_seconds(src) == pytest.approx(8.0, abs=0.01)

    detection = sf.detect_dead_air(str(src), sf.EditPolicy(min_silence_s=0.8))
    assert detection["measured"] is True and detection["reason"] == ""
    assert len(detection["ranges"]) == 1
    span = detection["ranges"][0]
    assert span["start_s"] == pytest.approx(3.0, abs=1e-3)
    assert span["end_s"] == pytest.approx(5.0, abs=1e-3)
    assert span["duration_s"] == pytest.approx(2.0, abs=1e-3)
    assert span["at_media_start"] is False and span["at_media_end"] is False
    # padding is applied on the side that faces speech
    assert span["cut_start_s"] == pytest.approx(3.15, abs=1e-3)
    assert span["cut_end_s"] == pytest.approx(4.85, abs=1e-3)
    assert span["kind"] == "REMOVE_RANGE" and span["reason"] == "DEAD_AIR"

    # the fixture's own parser agrees -- two independent readers, one truth
    assert wav_silence_ranges(src) == [{
        "start_s": pytest.approx(3.0, abs=1e-3), "end_s": pytest.approx(5.0, abs=1e-3),
        "duration_s": pytest.approx(2.0, abs=1e-3),
    }]


@pytest.mark.slow
def test_leading_silence_is_reported_at_the_media_boundary(tmp_path):
    """The boundary rule: a silent head is a boundary, and it keeps its lead-in."""
    from tests.media_intel_fixtures import ffmpeg_run, silent_wav

    silent = silent_wav(tmp_path / "silent.wav", seconds=2.0)
    detection = sf.detect_dead_air(str(silent), sf.EditPolicy(min_silence_s=0.5))
    assert detection["measured"] is True
    assert len(detection["ranges"]) == 1
    head = detection["ranges"][0]
    assert head["start_s"] == 0.0 and head["at_media_start"] is True
    assert head["at_media_end"] is True
    # both sides are media boundaries, so no padding is invented
    assert head["cut_start_s"] == 0.0
    assert head["cut_end_s"] == pytest.approx(2.0)

    lead = tmp_path / "lead.wav"
    ffmpeg_run(["-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1",
                "-f", "lavfi", "-i", "aevalsrc=0.5*sin(2*PI*440*t):s=48000:d=2",
                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
                "-map", "[out]", "-c:a", "pcm_s16le", "-f", "wav", str(lead)])
    detection = sf.detect_dead_air(str(lead), sf.EditPolicy(min_silence_s=0.5))
    ranges = detection["ranges"]
    assert ranges and ranges[0]["start_s"] == 0.0
    assert ranges[0]["at_media_start"] is True
    assert ranges[0]["cut_start_s"] == 0.0, "no padding exists before the media start"
    assert ranges[0]["cut_end_s"] == pytest.approx(0.85, abs=1e-3), \
        "0.15 s of lead-in is kept"
    assert ranges[0]["kind"] == "REMOVE_RANGE"


@pytest.mark.slow
def test_a_tone_has_no_dead_air_and_the_source_is_never_touched(tmp_path):
    from tests.media_intel_fixtures import tone_wav, wav_peak_dbfs

    src = tone_wav(tmp_path / "tone.wav", seconds=2.0, amplitude_db=-6.0)
    before = src.read_bytes()
    detection = sf.detect_dead_air(str(src), sf.EditPolicy(min_silence_s=0.5))
    assert detection["measured"] is True
    assert detection["ranges"] == []
    assert "threshold" in detection["reason"]
    assert src.read_bytes() == before, "detection never writes the source"
    assert wav_peak_dbfs(src) == pytest.approx(-6.0, abs=0.2)


@pytest.mark.slow
def test_silence_route_end_to_end_and_apply(tmp_path, monkeypatch):
    """Full HTTP flow over REAL media: detect -> decide -> apply -> time map."""
    from app.db import session_scope
    from app.models import MediaAsset
    from tests.media_intel_fixtures import patterned_speechlike_wav

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    from app.services.storage import STORAGE_ROOT

    src = patterned_speechlike_wav(tmp_path / "upload.wav")
    stored = STORAGE_ROOT / ws_id / "speechlike.wav"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(src.read_bytes())
    with session_scope() as s:
        asset = MediaAsset(workspace_id=ws_id, type="audio", storage_key=str(stored),
                           checksum="slow-1", duration_seconds=8.0)
        s.add(asset)
        s.flush()
        aid, timeline_id = asset.id, _timeline(s, ws_id, _doc(8.0).get("tracks")).id
    before_bytes = stored.read_bytes()
    base = f"/api/v1/workspaces/{ws_id}/media-intel"

    r = client.post(f"{base}/audio/silence", headers=headers,
                    json={"asset_id": aid, "timeline_id": timeline_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cache_hit"] is False
    assert body["detection"]["measured"] is True
    assert body["counts_by_reason"] == {"DEAD_AIR": 1}
    proposal = body["proposals"][0]
    assert proposal["kind"] == "REMOVE_RANGE" and proposal["status"] == "PROPOSED"
    assert proposal["decision"] is None
    assert proposal["evidence"]["cut_start_s"] == pytest.approx(3.15, abs=1e-3)
    assert (proposal["start_s"], proposal["end_s"]) == (
        pytest.approx(3.15, abs=1e-3), pytest.approx(4.85, abs=1e-3),
    ), "a removable proposal's range IS the padded cut"
    # a preview is attached when a timeline is known, and it is canonical
    from app.engine.timeline_ops import OP_TYPES

    assert {op["type"] for op in proposal["operations"]} <= set(OP_TYPES)
    assert body["run"]["status"] == "COMPLETED"

    # a second identical request is a CACHE HIT: no recompute, same proposals
    again = client.post(f"{base}/audio/silence", headers=headers,
                        json={"asset_id": aid, "timeline_id": timeline_id})
    assert again.status_code == 200, again.text
    assert again.json()["cache_hit"] is True
    assert [p["id"] for p in again.json()["proposals"]] == [proposal["id"]]

    # Lane H's REAL audio QC over the same media, then its gate consulted by the
    # apply route. This is the full integration: detect -> QC -> decide -> apply.
    from app.engine.intel import qc as intel_qc
    from app.models import MediaIntelRun, Workspace

    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        run_row = s.get(MediaIntelRun, body["run"]["id"])
        qc_result = intel_qc.run_audio_qc(s, ws, run_row)
    assert qc_result["verdict"] in intel_qc.QC_VERDICTS
    assert qc_result["verdict"] != "FAIL", qc_result
    assert "excessive_removed_speech" in [c["name"] for c in qc_result["checks"]]

    r = client.post(f"{base}/proposals/{proposal['id']}/decide", headers=headers,
                    json={"decision": "remove"})
    assert r.status_code == 200, r.text
    version = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{timeline_id}",
                         headers=headers).json()["version"]
    r = client.post(f"{base}/proposals/apply", headers=headers,
                    json={"timeline_id": timeline_id, "base_version": version,
                          "proposal_ids": [proposal["id"]]})
    assert r.status_code == 200, r.text
    applied = r.json()
    assert applied["qc"][0]["allowed"] is True
    assert applied["qc"][0]["verdict"] == qc_result["verdict"]
    assert applied["time_map"]["removed_duration_s"] == pytest.approx(1.7, abs=1e-3)
    assert applied["time_map"]["output_duration_s"] == pytest.approx(6.3, abs=1e-3)

    timeline = client.get(f"/api/v1/workspaces/{ws_id}/timelines/{timeline_id}",
                          headers=headers).json()
    voice = next(t for t in timeline["tracks"] if t["kind"] == "voice")["clips"]
    caps = {c["id"]: c for c in
            next(t for t in timeline["tracks"] if t["kind"] == "caption")["clips"]}
    assert sum(c["duration"] for c in voice) == pytest.approx(6.3, abs=1e-3)
    assert caps["c1"]["start"] == pytest.approx(0.0)
    assert caps["c3"]["start"] == pytest.approx(3.8, abs=1e-3)
    # the source media is byte-identical: the cut exists only as timeline ops
    assert stored.read_bytes() == before_bytes
    with session_scope() as s:
        assert s.query(MediaAsset).filter(
            MediaAsset.workspace_id == ws_id).count() == 1, "no derived asset, no re-encode"
