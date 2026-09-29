"""Speaker-aware dubbing plans: voice mapping, timing fit, review flags, persistence."""
from __future__ import annotations

import pytest

from app.engine.dubbing.plan import (
    DEFAULT_SINGLE_SPEAKER,
    MAX_SPEAKING_RATE,
    MIN_SPEAKING_RATE,
    DubbingPlan,
    PlanError,
    SegmentPlan,
    SpeakerPlan,
    build_plan,
    fit_plan,
    list_plans,
    load_plan,
    plan_from_dict,
    plan_to_dict,
    save_plan,
)


def _cue(index, start, end, text, speaker="", **kw):
    row = {"index": index, "start": start, "end": end, "text": text}
    if speaker:
        row["speaker_id"] = speaker
    row.update(kw)
    return row


def _two_speaker_cues():
    return [
        _cue(1, 0.0, 3.0, "hello there", "host", source_voice="en-US-guy"),
        _cue(2, 3.0, 6.0, "welcome back", "guest", source_voice="en-US-aria"),
    ]


VOICE_MAP = {"host": "es-HostNeural", "guest": "es-GuestNeural"}


# --- speaker / voice mapping ------------------------------------------------


def test_two_speakers_get_distinct_target_voices():
    plan = build_plan(_two_speaker_cues(), "es", VOICE_MAP, {})
    assert len(plan.speakers) == 2
    voices = [s.target_voice for s in plan.speakers]
    assert voices == ["es-HostNeural", "es-GuestNeural"]
    assert len(set(voices)) == 2  # one voice per speaker, never shared
    # source voice + language land on the speaker row
    assert plan.speakers[0].source_voice == "en-US-guy"
    assert plan.speakers[0].language == "es"
    # every segment is bound to exactly one speaker
    assert {s.speaker_id for s in plan.segments} == {"host", "guest"}
    assert plan.status == "DRAFT" and plan.needs_review is False


def test_shared_target_voice_is_rejected():
    with pytest.raises(PlanError, match="one voice per speaker"):
        build_plan(
            _two_speaker_cues(), "es", {"host": "es-Same", "guest": "es-Same"}, {}
        )


def test_voice_map_accepts_mapping_entry():
    plan = build_plan(
        _two_speaker_cues(),
        "es",
        {
            "host": {"target_voice": "es-HostNeural", "source_voice": "en-US-guy",
                     "speaking_rate": 1.1,
                     "pronunciation_rules": {"YMONEY": "wai-mo-ney"}},
            "guest": {"target_voice": "es-GuestNeural"},
        },
        {},
    )
    host = plan.speaker("host")
    assert host.target_voice == "es-HostNeural"
    assert host.source_voice == "en-US-guy"
    assert host.speaking_rate == 1.1
    assert host.pronunciation_rules["YMONEY"] == "wai-mo-ney"


def test_glossary_becomes_pronunciation_rules_on_every_speaker():
    plan = build_plan(
        _two_speaker_cues(), "es", VOICE_MAP,
        glossary={"autopilot": "auto-pi-lot"},
        pronunciation_rules={"YouTube": "yoo-toob"},
    )
    for speaker in plan.speakers:
        assert speaker.pronunciation_rules["autopilot"] == "auto-pi-lot"
        assert speaker.pronunciation_rules["YouTube"] == "yoo-toob"
    # explicit per-speaker rules beat shared ones
    plan2 = build_plan(
        _two_speaker_cues(), "es",
        {"host": {"target_voice": "es-HostNeural",
                  "pronunciation_rules": {"autopilot": "OWN"}},
         "guest": "es-GuestNeural"},
        glossary={"autopilot": "auto-pi-lot"},
    )
    assert plan2.speaker("host").pronunciation_rules["autopilot"] == "OWN"
    assert plan2.speaker("guest").pronunciation_rules["autopilot"] == "auto-pi-lot"


def test_unlabelled_cues_use_single_speaker_default():
    plan = build_plan(
        [_cue(1, 0.0, 2.0, "one"), _cue(2, 2.0, 4.0, "two")],
        "es", {"speaker_1": "es-Solo"}, {},
    )
    assert [s.speaker_id for s in plan.speakers] == [DEFAULT_SINGLE_SPEAKER]
    assert {s.speaker_id for s in plan.segments} == {DEFAULT_SINGLE_SPEAKER}


def test_mixed_speaker_labels_rejected():
    with pytest.raises(PlanError, match="label every cue"):
        build_plan([_cue(1, 0.0, 2.0, "a", "host"), _cue(2, 2.0, 4.0, "b")],
                   "es", VOICE_MAP, {})


def test_unmapped_speaker_voice_goes_to_review_not_fake_voice():
    plan = build_plan(_two_speaker_cues(), "es", {"host": "es-HostNeural"}, {})
    assert plan.status == "REVIEW" and plan.needs_review is True
    guest = plan.speaker("guest")
    assert guest.target_voice == ""  # never fabricated
    flagged = [s for s in plan.segments if s.speaker_id == "guest"]
    assert flagged and flagged[0].voice_review is True
    assert "voice_map['guest']" in flagged[0].review_reason
    # mapped speaker stays clean
    assert not [s for s in plan.segments if s.speaker_id == "host" and s.needs_review]


def test_missing_target_lang_rejected():
    with pytest.raises(PlanError, match="target_lang"):
        build_plan(_two_speaker_cues(), "", VOICE_MAP, {})


def test_empty_or_invalid_cues_rejected():
    with pytest.raises(PlanError, match="no cues"):
        build_plan([], "es", VOICE_MAP, {})
    with pytest.raises(PlanError, match="must be after start"):
        build_plan([_cue(1, 5.0, 5.0, "bad", "host")], "es", VOICE_MAP, {})
    with pytest.raises(PlanError, match="unique"):
        build_plan(
            [_cue(1, 0.0, 1.0, "a", "host"), _cue(1, 1.0, 2.0, "b", "guest")],
            "es", VOICE_MAP, {},
        )


# --- timing fit -------------------------------------------------------------


def _ready_plan():
    return build_plan(
        [_cue(1, 0.0, 2.0, "one", "host"), _cue(2, 2.0, 6.0, "two", "guest")],
        "es", VOICE_MAP, {},
    )


def test_fit_normal_rates_within_sane_window():
    plan = fit_plan(_ready_plan(), {1: 2.5, 2: 3.0})  # 1.25x and 0.75x
    seg1, seg2 = plan.segments
    assert seg1.planned_rate == 1.25
    assert seg2.planned_rate == 0.75  # slowed to the floor, then padded
    assert seg1.needs_review is False and seg2.needs_review is False
    assert plan.status == "READY" and plan.needs_review is False
    assert plan.review_count == 0


def test_fit_never_emits_absurd_rate_and_flags_review():
    plan = fit_plan(_ready_plan(), {1: 9.0, 2: 4.0})  # needs 4.5x and 1.0x
    seg1 = plan.segments[0]
    assert seg1.audio_seconds == 9.0
    assert seg1.needs_review is True
    assert seg1.planned_rate <= MAX_SPEAKING_RATE  # clamped, never chipmunk
    assert seg1.planned_rate >= MIN_SPEAKING_RATE
    assert "ceiling" in seg1.review_reason and "4.50x" in seg1.review_reason
    # second segment fits and stays clean
    assert plan.segments[1].needs_review is False
    assert plan.status == "REVIEW" and plan.needs_review is True
    assert plan.review_count == 1
    assert plan.summary()["review_samples"]


def test_fit_clamps_per_speaker_and_updates_speaker_rate():
    plan = build_plan(
        [_cue(1, 0.0, 2.0, "a", "host"), _cue(2, 2.0, 4.0, "b", "host")],
        "es",
        {"host": {"target_voice": "es-HostNeural",
                  "timing_constraints": {"min_rate": 0.9, "max_rate": 1.1}}},
        {},
    )
    fit_plan(plan, {1: 2.0, 2: 3.8})  # 1.0x ok; 1.9x exceeds the speaker ceiling
    assert plan.segments[0].planned_rate == 1.0
    assert plan.segments[1].planned_rate == 1.1  # clamped to speaker ceiling
    assert plan.segments[1].needs_review is True
    assert plan.speaker("host").speaking_rate == 1.1


def test_fit_leaves_unmeasured_segments_untouched():
    plan = fit_plan(_ready_plan(), {1: 2.0})
    assert plan.segments[0].audio_seconds == 2.0
    assert plan.segments[1].audio_seconds is None
    assert plan.segments[1].planned_rate == 1.0
    assert plan.segments[1].needs_review is False


def test_fit_probe_supplies_missing_durations():
    plan = _ready_plan()
    fit_plan(plan, None, probe=lambda seg: 1.0 if seg.index == 1 else 5.0)
    assert plan.segments[0].audio_seconds == 1.0
    assert plan.segments[1].audio_seconds == 5.0


def test_fit_empty_audio_is_review():
    plan = fit_plan(_ready_plan(), {1: 0.0, 2: 2.0})
    assert plan.segments[0].needs_review is True
    assert "empty" in plan.segments[0].review_reason


def test_fit_invalid_window_is_review():
    plan = DubbingPlan(
        target_language="es",
        speakers=[SpeakerPlan(speaker_id="host", language="es", target_voice="es-HostNeural")],
        segments=[SegmentPlan(index=1, speaker_id="host", start=5.0, end=5.0, text="x")],
    )
    fit_plan(plan, {1: 1.0})
    assert plan.segments[0].needs_review is True
    assert "invalid timing window" in plan.segments[0].review_reason


def test_fit_requires_numeric_duration():
    with pytest.raises(PlanError, match="must be a number"):
        fit_plan(_ready_plan(), {1: "soon"})


# --- persistence ------------------------------------------------------------


def test_plan_round_trips_through_dict():
    plan = fit_plan(_ready_plan(), {1: 2.4, 2: 3.0})
    again = plan_from_dict(plan_to_dict(plan))
    assert again.status == plan.status
    assert again.speakers[0].target_voice == "es-HostNeural"
    assert again.segments[0].planned_rate == plan.segments[0].planned_rate
    assert again.review_count == plan.review_count


def test_plan_persists_and_is_workspace_scoped(db_session, workspace_with_user):
    from app.models.dubbing import DubbingPlanRow

    ws_id = workspace_with_user["workspace"]
    plan = fit_plan(_ready_plan(), {1: 2.0, 2: 4.0}, )
    row = save_plan(db_session, ws_id, plan, source_ref="clip-1.mp4")
    db_session.commit()

    stored = db_session.get(DubbingPlanRow, row.id)
    assert stored is not None
    assert stored.target_language == "es"
    assert stored.source_ref == "clip-1.mp4"
    assert stored.status == plan.status
    assert stored.plan_json["segments"][0]["index"] == 1
    assert stored.plan_json["id"] == row.id

    loaded = load_plan(db_session, ws_id, row.id)
    assert loaded is not None and loaded.id == row.id
    assert load_plan(db_session, "ws-other", row.id) is None  # cross-ws = None
    assert [r.id for r in list_plans(db_session, ws_id)] == [row.id]
    assert list_plans(db_session, "ws-other") == []


# --- API routes (POST/GET /dubbing/plans) -----------------------------------


def _register_plan_client():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post(
        "/api/v1/auth/register",
        json={"email": f"dp{os.urandom(4).hex()}@test.local",
              "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return client, {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def test_dubbing_plan_api_build_persist_and_list():
    client, headers, ws = _register_plan_client()

    r = client.post(
        f"/api/v1/workspaces/{ws}/dubbing/plans",
        headers=headers,
        json={
            "target_lang": "es",
            "cues": [
                {"start": 0.0, "end": 3.0, "text": "hola", "index": 1,
                 "speaker_id": "host"},
                {"start": 3.0, "end": 6.0, "text": "adios", "index": 2,
                 "speaker_id": "guest"},
            ],
            "voice_map": {"host": "es-HostNeural", "guest": "es-GuestNeural"},
            # 1.5s into a 3s window -> floor applied; 4.5s into a 3s window
            # needs 1.5x > 1.35x ceiling -> review
            "durations": {"1": 1.5, "2": 4.5},
            "source_ref": "clip-1.mp4",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "REVIEW" and body["needs_review"] is True
    assert body["summary"]["review_samples"]
    assert body["plan"]["speakers"][0]["target_voice"] == "es-HostNeural"

    r = client.get(f"/api/v1/workspaces/{ws}/dubbing/plans", headers=headers)
    assert r.status_code == 200, r.text
    listing = r.json()
    assert listing["total"] == 1
    assert listing["items"][0]["target_language"] == "es"
    assert listing["items"][0]["review_count"] == 1

    # broken cue timing fails closed with a 422 explanation
    r = client.post(
        f"/api/v1/workspaces/{ws}/dubbing/plans",
        headers=headers,
        json={"target_lang": "es",
              "cues": [{"start": 5.0, "end": 5.0, "text": "bad"}]},
    )
    assert r.status_code == 422
    assert "after start" in r.json()["detail"]


def test_dubbing_plan_api_workspace_isolation():
    client, headers_a, ws_a = _register_plan_client()
    _, headers_b, ws_b = _register_plan_client()

    r = client.post(
        f"/api/v1/workspaces/{ws_a}/dubbing/plans",
        headers=headers_a,
        json={"target_lang": "es",
              "cues": [{"start": 0.0, "end": 2.0, "text": "hola"}],
              "voice_map": {"speaker_1": "es-Solo"}},
    )
    assert r.status_code == 200, r.text

    # foreign workspace path: not a member -> 403, never data
    r = client.get(f"/api/v1/workspaces/{ws_a}/dubbing/plans", headers=headers_b)
    assert r.status_code == 403
    # the other workspace's own list stays empty
    r = client.get(f"/api/v1/workspaces/{ws_b}/dubbing/plans", headers=headers_b)
    assert r.status_code == 200 and r.json()["total"] == 0
