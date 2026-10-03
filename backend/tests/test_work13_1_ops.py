"""Work 13.1 §2/§5/§6/§9/§10/§11 — canonical keyframe ops through the real layers.

These tests use the REAL operation layer, the REAL CreativeDirector (preview ->
apply -> version -> undo) and the REAL cache/QC helpers. Nothing is asserted
from schema alone: a keyframe is only "supported" once the typed op persists it,
the director emits it, the cache invalidates on it and QC reports on it.
"""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import select

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _doc():
    return {
        "workspace_id": "ws", "fps": 30, "aspect_ratio": "9:16",
        "duration_seconds": 6.0,
        "tracks": [
            {"id": "t_video", "kind": "video", "name": "Video", "clips": [
                {"id": "v1", "name": "a", "start": 0.0, "duration": 3.0,
                 "source": {}, "effects": [], "source_start": 0.0, "volume": 1.0,
                 "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
                 "text": {}, "transition_in": "cut", "transition_out": "cut"},
                {"id": "v2", "name": "b", "start": 3.0, "duration": 3.0,
                 "source": {}, "effects": [], "source_start": 0.0, "volume": 1.0,
                 "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
                 "text": {}, "transition_in": "cut", "transition_out": "cut"},
            ]},
            {"id": "t_caption", "kind": "caption", "name": "Captions", "clips": [
                {"id": "c1", "name": "Save 500", "start": 0.0, "duration": 2.0,
                 "source": {}, "effects": [], "source_start": 0.0, "volume": 1.0,
                 "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
                 "text": {}, "transition_in": "cut", "transition_out": "cut"},
            ]},
            {"id": "t_text", "kind": "text", "name": "Text", "clips": []},
        ],
    }


def _apply(doc, ops):
    from app.engine.timeline_ops import apply_operations

    return apply_operations(copy.deepcopy(doc), ops)


def _clip(doc, clip_id):
    for track in doc["tracks"]:
        for clip in track["clips"]:
            if clip["id"] == clip_id:
                return clip
    raise AssertionError(f"clip {clip_id} not found")


def _client(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client):
    import uuid

    from app.db import session_scope
    from app.models import ContentTimeline

    email = f"w131{uuid.uuid4().hex[:8]}@test.local"
    resp = client.post("/api/v1/auth/register",
                       json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ws_id = data["workspace"]["id"]
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    doc = _doc()
    doc["workspace_id"] = ws_id
    with session_scope() as s:
        row = ContentTimeline(workspace_id=ws_id, version=1, tracks_json=doc,
                              fps=30, duration_seconds=6.0)
        s.add(row)
        s.commit()
        timeline_id = row.id
    return ws_id, headers, timeline_id


# ---------------------------------------------------------------------------
# §2 canonical keyframe operations
# ---------------------------------------------------------------------------


def test_op_types_include_the_work13_1_keyframe_ops():
    from app.engine.timeline_ops import OP_TYPES

    for op in ("add_keyframe", "update_keyframe", "delete_keyframe",
               "move_keyframe", "set_keyframes"):
        assert op in OP_TYPES, f"{op} is not a registered typed operation"


def test_add_keyframe_persists_a_typed_keyframe():
    out = _apply(_doc(), [{
        "type": "add_keyframe", "track": "video", "clip_id": "v1",
        "keyframe": {"id": "k1", "t": 0.0, "easing": "LINEAR",
                     "props": {"x": 0.1, "opacity": 0.0}}}])
    frames = _clip(out, "v1")["keyframes"]
    assert len(frames) == 1
    assert frames[0]["id"] == "k1"
    assert frames[0]["props"]["x"] == 0.1


def test_keyframes_are_kept_in_deterministic_time_order():
    out = _apply(_doc(), [
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "late", "t": 2.0, "props": {"x": 0.9}}},
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "early", "t": 0.0, "props": {"x": 0.1}}},
    ])
    times = [f["t"] for f in _clip(out, "v1")["keyframes"]]
    assert times == sorted(times), f"keyframes stored out of order: {times}"


def test_update_keyframe_changes_props_and_easing_only():
    out = _apply(_doc(), [
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "k1", "t": 0.0, "props": {"x": 0.1}}}])
    out = _apply(out, [{
        "type": "update_keyframe", "track": "video", "clip_id": "v1",
        "keyframe_id": "k1",
        "keyframe": {"easing": "EASE_IN_OUT", "props": {"scale": 2.0}}}])
    frame = _clip(out, "v1")["keyframes"][0]
    assert frame["id"] == "k1"          # identity is stable
    assert frame["easing"] == "EASE_IN_OUT"
    assert frame["props"] == {"scale": 2.0}


def test_move_keyframe_re_times_and_reorders():
    out = _apply(_doc(), [
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "a", "t": 0.0, "props": {"x": 0.1}}},
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "b", "t": 2.0, "props": {"x": 0.9}}}])
    out = _apply(out, [{"type": "move_keyframe", "track": "video",
                        "clip_id": "v1", "keyframe_id": "b", "t": 1.0}])
    frames = _clip(out, "v1")["keyframes"]
    assert [f["id"] for f in frames] == ["a", "b"]
    assert [f["t"] for f in frames] == [0.0, 1.0]


def test_delete_keyframe_removes_exactly_one():
    out = _apply(_doc(), [
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "a", "t": 0.0, "props": {"x": 0.1}}},
        {"type": "add_keyframe", "track": "video", "clip_id": "v1",
         "keyframe": {"id": "b", "t": 1.0, "props": {"x": 0.5}}}])
    out = _apply(out, [{"type": "delete_keyframe", "track": "video",
                        "clip_id": "v1", "keyframe_id": "a"}])
    assert [f["id"] for f in _clip(out, "v1")["keyframes"]] == ["b"]


def test_set_keyframes_replaces_the_whole_chain():
    out = _apply(_doc(), [{
        "type": "set_keyframes", "track": "video", "clip_id": "v1",
        "keyframes": [
            {"id": "a", "t": 0.0, "easing": "LINEAR", "props": {"x": 0.1}},
            {"id": "b", "t": 2.5, "easing": "HOLD", "props": {"x": 0.8}},
        ]}])
    frames = _clip(out, "v1")["keyframes"]
    assert [f["id"] for f in frames] == ["a", "b"]
    assert frames[1]["easing"] == "HOLD"


@pytest.mark.parametrize("op,reason", [
    ({"type": "add_keyframe", "track": "video", "clip_id": "v1",
      "keyframe": {"id": "x", "t": 99.0, "props": {"x": 0.5}}}, "outside"),
    ({"type": "add_keyframe", "track": "video", "clip_id": "v1",
      "keyframe": {"id": "x", "t": 1.0, "easing": "BOUNCE",
                   "props": {"x": 0.5}}}, "easing"),
    ({"type": "add_keyframe", "track": "video", "clip_id": "v1",
      "keyframe": {"id": "x", "t": 1.0, "props": {"zdepth": 3.0}}}, "prop"),
    ({"type": "move_keyframe", "track": "video", "clip_id": "v1",
      "keyframe_id": "ghost", "t": 1.0}, "not found"),
    ({"type": "delete_keyframe", "track": "video", "clip_id": "v1",
      "keyframe_id": "ghost"}, "not found"),
    ({"type": "update_keyframe", "track": "video", "clip_id": "v1",
      "keyframe_id": "ghost", "keyframe": {"easing": "HOLD"}}, "not found"),
    ({"type": "add_keyframe", "track": "video", "clip_id": "ghost",
      "keyframe": {"id": "x", "t": 1.0, "props": {"x": 0.5}}}, "not found"),
])
def test_keyframe_ops_refuse_invalid_input(op, reason):
    from app.engine.timeline_ops import TimelineOpError

    with pytest.raises(TimelineOpError):
        _apply(_doc(), [op])
    assert reason  # the parametrisation documents WHY each case is refused


def test_duplicate_keyframe_ids_are_refused():
    from app.engine.timeline_ops import TimelineOpError

    with pytest.raises(TimelineOpError):
        _apply(_doc(), [
            {"type": "add_keyframe", "track": "video", "clip_id": "v1",
             "keyframe": {"id": "same", "t": 0.0, "props": {"x": 0.1}}},
            {"type": "add_keyframe", "track": "video", "clip_id": "v1",
             "keyframe": {"id": "same", "t": 1.0, "props": {"x": 0.9}}},
        ])


# ---------------------------------------------------------------------------
# §3 interpolation: deterministic, distinct per easing
# ---------------------------------------------------------------------------


def test_interpolation_is_deterministic_for_identical_input():
    from app.engine.motion.graph import keyframe_expressions, validate_keyframes

    raw = [{"id": "a", "t": 0.0, "easing": "EASE_IN_OUT", "props": {"x": 0.1}},
           {"id": "b", "t": 2.0, "easing": "EASE_IN_OUT", "props": {"x": 0.9}}]
    first, _ = validate_keyframes(raw, clip_duration=3.0)
    second, _ = validate_keyframes(raw, clip_duration=3.0)
    assert (keyframe_expressions(first, width=1080, height=1920)
            == keyframe_expressions(second, width=1080, height=1920))


@pytest.mark.parametrize("easing", ["LINEAR", "EASE_IN", "EASE_OUT",
                                    "EASE_IN_OUT", "HOLD"])
def test_every_documented_easing_compiles_to_an_expression(easing):
    from app.engine.motion.graph import evaluate_keyframes, validate_keyframes

    frames, problems = validate_keyframes(
        [{"id": "a", "t": 0.0, "easing": easing, "props": {"x": 0.0}},
         {"id": "b", "t": 1.0, "easing": easing, "props": {"x": 1.0}}],
        clip_duration=2.0)
    assert not problems
    expression, _constant = evaluate_keyframes(frames, "x")
    assert expression, f"{easing} produced no expression"
    # only `t` and numbers may appear: no caller-supplied syntax
    assert "if(lt(t," in expression


def test_distinct_easings_produce_distinct_expressions():
    from app.engine.motion.graph import evaluate_keyframes, validate_keyframes

    seen = {}
    for easing in ("LINEAR", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"):
        frames, _ = validate_keyframes(
            [{"id": "a", "t": 0.0, "easing": easing, "props": {"x": 0.0}},
             {"id": "b", "t": 1.0, "easing": easing, "props": {"x": 1.0}}],
            clip_duration=2.0)
        seen[easing] = evaluate_keyframes(frames, "x")[0]
    assert len(set(seen.values())) == len(seen), (
        f"two easings compiled to the same expression: {seen}")


def test_hold_does_not_interpolate():
    from app.engine.motion.graph import evaluate_keyframes, validate_keyframes

    frames, _ = validate_keyframes(
        [{"id": "a", "t": 0.0, "easing": "HOLD", "props": {"x": 0.25}},
         {"id": "b", "t": 1.0, "easing": "HOLD", "props": {"x": 0.9}}],
        clip_duration=2.0)
    expression, _ = evaluate_keyframes(frames, "x")
    assert "0.25" in expression
    # HOLD must not contain a normalised (t-a)/(b-a) term
    assert "(t-0)/1" not in expression


def test_a_single_keyframe_compiles_to_a_constant_not_a_ladder():
    from app.engine.motion.graph import evaluate_keyframes, validate_keyframes

    frames, _ = validate_keyframes(
        [{"id": "a", "t": 0.0, "props": {"scale": 1.75}}], clip_duration=2.0)
    expression, constant = evaluate_keyframes(frames, "scale")
    assert expression == ""
    assert constant == 1.75


# ---------------------------------------------------------------------------
# §5 deterministic effect ordering
# ---------------------------------------------------------------------------


def test_effect_order_is_independent_of_input_order():
    from app.engine.motion.graph import order_effects

    a = order_effects([{"type": "OPACITY"}, {"type": "CROP"},
                       {"type": "BLUR"}, {"type": "COLOR_ADJUST"}])[0]
    b = order_effects([{"type": "BLUR"}, {"type": "COLOR_ADJUST"},
                       {"type": "OPACITY"}, {"type": "CROP"}])[0]
    assert [e["type"] for e in a] == [e["type"] for e in b] == [
        "CROP", "COLOR_ADJUST", "BLUR", "OPACITY"]


def test_effect_order_follows_the_documented_categories():
    from app.engine.motion.graph import EFFECT_CATEGORIES, order_effects

    ordered, problems = order_effects([
        {"type": "DROP_SHADOW"},      # overlay
        {"type": "OPACITY"},          # opacity
        {"type": "BACKGROUND_BLUR"},  # background
        {"type": "MASK"},             # mask
        {"type": "ZOOM"},             # geometry
    ])
    assert not problems
    assert [e["type"] for e in ordered] == [
        "ZOOM", "MASK", "BACKGROUND_BLUR", "OPACITY", "DROP_SHADOW"]
    assert EFFECT_CATEGORIES[0] == "geometry"
    assert EFFECT_CATEGORIES[-1] == "overlay"


def test_unknown_effect_type_is_reported_and_dropped():
    from app.engine.motion.graph import order_effects

    ordered, problems = order_effects([{"type": "CROP"},
                                       {"type": "NOT_A_REAL_EFFECT"}])
    assert [e["type"] for e in ordered] == ["CROP"]
    assert problems and "NOT_A_REAL_EFFECT" in problems[0]


def test_effect_order_is_stable_across_repeated_calls():
    from app.engine.motion.graph import order_effects

    effects = [{"type": "BLUR"}, {"type": "CROP"}, {"type": "OPACITY"}]
    first = [e["type"] for e in order_effects(effects)[0]]
    second = [e["type"] for e in order_effects(effects)[0]]
    assert first == second


# ---------------------------------------------------------------------------
# §4 composite effects: unavailable is explicit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["MASK", "BACKGROUND_BLUR"])
def test_composite_without_a_mask_is_not_available(kind):
    from app.engine.motion.graph import plan_composite

    plan = plan_composite({"type": kind, "params": {}}, {})
    assert plan.available is False
    assert "mask" in plan.reason.lower()


def test_composite_with_a_mask_builds_a_multi_input_graph():
    from app.engine.motion.graph import plan_composite

    plan = plan_composite({"type": "BACKGROUND_BLUR", "params": {"radius": 20}},
                          {"subject_mask_key": "m1",
                           "subject_mask_path": "C:/tmp/mask.png"})
    assert plan.available is True
    assert plan.input_args == ["-i", "C:/tmp/mask.png"]
    assert plan.out_suffix
    graph = ";".join(plan.filters)
    for required in ("split", "boxblur", "alphamerge", "overlay"):
        assert required in graph, f"composite graph lacks {required}"


def test_composite_never_emits_caller_supplied_filter_text():
    from app.engine.motion.graph import plan_composite

    plan = plan_composite(
        {"type": "BACKGROUND_BLUR", "params": {"radius": 12}},
        {"subject_mask_key": "m1", "subject_mask_path": "C:/tmp/mask.png"})
    graph = ";".join(plan.filters)
    for suspicious in ("eval", "$", "`", ";system", "movie="):
        assert suspicious not in graph, (
            f"composite graph contains caller-influenced text: {suspicious}")


def test_invalid_composite_params_are_refused_not_rendered():
    from app.engine.motion.graph import plan_composite

    plan = plan_composite({"type": "BACKGROUND_BLUR",
                           "params": {"radius": "not-a-number"}},
                          {"subject_mask_key": "m",
                           "subject_mask_path": "C:/tmp/mask.png"})
    assert plan.available is False
    assert plan.reason


# ---------------------------------------------------------------------------
# §6 CreativeDirector: typed motion commands only
# ---------------------------------------------------------------------------


def _plan(command, doc=None):
    """Call the REAL director plan function (session + workspace + policy)."""
    from app.db import session_scope
    from app.engine.creative import director as director_module
    from app.models import Workspace

    document = doc or _doc()
    with session_scope() as session:
        workspace = session.scalars(
            select(Workspace).limit(1)).first()
        return director_module._plan(session, workspace, command,
                                     doc=document, policy={})


def test_creative_command_types_include_the_motion_commands():
    from app.engine.creative.commands import COMMAND_TYPES

    for name in ("AnimateElement", "MoveElement", "AnimateOpacity",
                 "AnimateScale", "ChangeTransition"):
        assert name in COMMAND_TYPES, f"{name} is not a typed creative command"


def test_animate_commands_emit_canonical_keyframe_ops():
    from app.engine.creative.commands import COMMAND_TYPES

    doc = _doc()
    for command, expected_prop in (
        (COMMAND_TYPES["AnimateOpacity"](clip_ids=["c1"], from_opacity=0.0,
                                         to_opacity=1.0), "opacity"),
        (COMMAND_TYPES["AnimateScale"](clip_ids=["c1"], from_scale=1.0,
                                       to_scale=1.5), "scale"),
        (COMMAND_TYPES["MoveElement"](clip_ids=["c1"], from_x=0.1, to_x=0.9,
                                      from_y=0.5, to_y=0.5), "x"),
        (COMMAND_TYPES["AnimateElement"](clip_ids=["c1"], from_x=0.0,
                                         to_x=0.5, easing="EASE_OUT"), "x"),
    ):
        ops, _artifacts, summary = _plan(command, doc)
        keyframe_ops = [o for o in ops if o["type"] == "set_keyframes"]
        assert keyframe_ops, f"{command.type} emitted no keyframe op: {ops}"
        chain = keyframe_ops[0]["keyframes"]
        driven = {p for frame in chain for p in frame.get("props", {})}
        assert expected_prop in driven, (
            f"{command.type} did not keyframe {expected_prop}: {driven}")
        assert summary["op"] == command.type


def test_animate_command_rejects_an_unsupported_easing():
    """The validator, not just the planner, must refuse a bad easing."""
    from app.engine.creative.commands import COMMAND_TYPES, validate_command

    command = COMMAND_TYPES["AnimateOpacity"](clip_ids=["c1"], easing="WOBBLE")
    reasons = validate_command(command, session=None, workspace_id="ws",
                               timeline=None, doc=_doc(), policy={})
    assert any("WOBBLE" in r for r in reasons), reasons


def test_animate_command_rejects_out_of_range_values():
    from app.engine.creative.commands import COMMAND_TYPES, validate_command

    for command in (
        COMMAND_TYPES["AnimateOpacity"](clip_ids=["c1"], to_opacity=4.0),
        COMMAND_TYPES["AnimateScale"](clip_ids=["c1"], to_scale=99.0),
        COMMAND_TYPES["MoveElement"](clip_ids=["c1"], to_x=50.0),
    ):
        reasons = validate_command(command, session=None, workspace_id="ws",
                                   timeline=None, doc=_doc(), policy={})
        assert reasons, f"{command.type} accepted an out-of-range value"
        assert any("outside" in r for r in reasons), reasons


def test_animate_command_preserves_other_keyframed_properties():
    """One animation must not silently erase an unrelated animation."""
    from app.engine.creative.commands import COMMAND_TYPES

    doc = _apply(_doc(), [{"type": "set_keyframes", "track": "caption",
                           "clip_id": "c1", "keyframes": [
                               {"id": "pos0", "t": 0.0, "props": {"x": 0.1}},
                               {"id": "pos1", "t": 1.5, "props": {"x": 0.4}}]}])
    command = COMMAND_TYPES["AnimateOpacity"](clip_ids=["c1"],
                                              from_opacity=0.0, to_opacity=1.0)
    ops, _artifacts, _summary = _plan(command, doc)
    chain = next(o for o in ops if o["type"] == "set_keyframes")["keyframes"]
    driven = {p for frame in chain for p in frame.get("props", {})}
    assert "opacity" in driven
    assert "x" in driven, "the existing position animation was erased"


def test_change_transition_emits_a_set_transition_op():
    from app.engine.creative.commands import COMMAND_TYPES

    command = COMMAND_TYPES["ChangeTransition"](
        from_item="v1", to_item="v2", transition_type="DISSOLVE",
        duration=0.5)
    ops, _artifacts, summary = _plan(command)
    assert [o["type"] for o in ops] == ["set_transition"]
    assert ops[0]["transition"]["type"] == "DISSOLVE"
    assert summary["op"] == "change_transition"


def test_director_never_emits_ffmpeg_syntax():
    """No motion command may put filter text into the operation stream."""
    from app.engine.creative.commands import COMMAND_TYPES

    doc = _doc()
    commands = [
        COMMAND_TYPES["AnimateElement"](clip_ids=["c1"]),
        COMMAND_TYPES["AnimateOpacity"](clip_ids=["c1"]),
        COMMAND_TYPES["AnimateScale"](clip_ids=["c1"]),
        COMMAND_TYPES["MoveElement"](clip_ids=["c1"]),
    ]
    for command in commands:
        ops, _a, _s = _plan(command, doc)
        blob = repr(ops)
        for suspicious in ("drawtext", "xfade", "boxblur", "overlay=",
                           "filter_complex", "alphamerge", "-i "):
            assert suspicious not in blob, (
                f"{command.type} leaked ffmpeg syntax: {suspicious}")


# ---------------------------------------------------------------------------
# §11 preview -> apply -> version -> undo -> redo, through the real API
# ---------------------------------------------------------------------------


def test_keyframe_command_previews_before_it_applies(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    payload = {"type": "AnimateOpacity", "clip_ids": ["c1"],
               "from_opacity": 0.0, "to_opacity": 1.0,
               "target": {"timeline_id": timeline_id}}
    r = client.post(f"/api/v1/workspaces/{ws_id}/creative/preview",
                    headers=headers, json={"commands": [payload]})
    assert r.status_code == 200, r.text
    entry = r.json()["changes"][0]
    assert entry["status"] in ("previewed", "ok", "valid"), entry


def test_keyframe_apply_is_versioned_and_undo_restores_the_prior_timeline(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/creative"
    payload = {"type": "AnimateOpacity", "clip_ids": ["c1"],
               "from_opacity": 0.0, "to_opacity": 1.0,
               "target": {"timeline_id": timeline_id}}
    # the real flow: preview first, then apply against the preview + version
    preview = client.post(f"{base}/preview", headers=headers,
                          json={"commands": [payload]})
    assert preview.status_code == 200, preview.text
    preview_id = preview.json()["preview_id"]
    applied = client.post(f"{base}/apply", headers=headers,
                          json={"commands": [payload], "base_version": 1,
                                "preview_id": preview_id, "approve": True})
    assert applied.status_code == 200, applied.text
    new_tid = applied.json()["timeline_id"]
    assert new_tid != timeline_id

    from app.db import session_scope
    from app.models import ContentTimeline

    with session_scope() as s:
        row = s.get(ContentTimeline, new_tid)
        frames = row.tracks_json["tracks"][1]["clips"][0].get("keyframes")
    assert frames, "the applied timeline has no keyframes"
    assert any("opacity" in f["props"] for f in frames)

    undone = client.post(f"/api/v1/workspaces/{ws_id}/creative/undo/{new_tid}",
                         headers=headers)
    assert undone.status_code == 200, undone.text
    assert undone.json()["status"] == "undone"

    with session_scope() as s:
        restored = s.get(ContentTimeline, undone.json()["timeline_id"])
        prior = restored.tracks_json["tracks"][1]["clips"][0].get("keyframes")
    assert not prior, "undo did not restore the keyframe-free timeline"


def test_motion_commands_are_workspace_isolated(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, _headers_a, timeline_a = _register(client)

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    client_b = _client(other, monkeypatch)
    import uuid

    email = f"w131b{uuid.uuid4().hex[:8]}@test.local"
    resp = client_b.post("/api/v1/auth/register",
                         json={"email": email, "password": "supersecret123"})
    headers_b = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws_b = resp.json()["workspace"]["id"]

    payload = {"type": "AnimateOpacity", "clip_ids": ["c1"],
               "from_opacity": 0.0, "to_opacity": 1.0,
               "target": {"timeline_id": timeline_a}}
    out = client_b.post(f"/api/v1/workspaces/{ws_b}/creative/preview",
                        headers=headers_b, json={"commands": [payload]})
    assert out.status_code in (200, 404), out.status_code
    if out.status_code == 200:
        assert out.json()["changes"][0]["status"] == "rejected"
    assert ws_b != ws_a


# ---------------------------------------------------------------------------
# §9 cache invalidation
# ---------------------------------------------------------------------------


def test_keyframe_change_invalidates_only_the_affected_cache_key():
    from app.engine.captions.cache import caption_cache_key

    base = {"source_checksum": "src-1", "timeline_version": 7,
            "style": {"size": 60}, "preset": "minimal"}
    before = caption_cache_key(**base)
    after_keyframe = caption_cache_key(
        **base, extra={"keyframes": [{"id": "k1", "t": 0.0, "props": {"x": 0.1}}]})
    after_other_keyframe = caption_cache_key(
        **base, extra={"keyframes": [{"id": "k1", "t": 0.0, "props": {"x": 0.9}}]})
    assert before != after_keyframe, "a keyframe change reused the cache"
    assert after_keyframe != after_other_keyframe, (
        "a different keyframe value reused the cache")


def test_transition_change_invalidates_the_chunk_key():
    from app.engine.captions.cache import chunk_render_key

    doc = _doc()
    changed = copy.deepcopy(doc)
    changed["tracks"][0]["clips"][1]["transition"] = {
        "from_item": "v1", "to_item": "v2", "type": "DISSOLVE", "duration": 0.5}
    assert (chunk_render_key(sub_doc=doc, chunk_index=0)
            != chunk_render_key(sub_doc=changed, chunk_index=0))


def test_effect_parameter_change_invalidates_the_chunk_key():
    from app.engine.captions.cache import chunk_render_key

    doc = _doc()
    changed = copy.deepcopy(doc)
    changed["tracks"][0]["clips"][0]["effects"] = [
        {"type": "BLUR", "params": {"radius": 4}}]
    other = copy.deepcopy(doc)
    other["tracks"][0]["clips"][0]["effects"] = [
        {"type": "BLUR", "params": {"radius": 9}}]
    assert (chunk_render_key(sub_doc=doc, chunk_index=1)
            != chunk_render_key(sub_doc=changed, chunk_index=1))
    assert (chunk_render_key(sub_doc=changed, chunk_index=1)
            != chunk_render_key(sub_doc=other, chunk_index=1))


def test_mask_change_invalidates_the_chunk_key():
    from app.engine.captions.cache import chunk_render_key

    doc = _doc()
    changed = copy.deepcopy(doc)
    changed["tracks"][0]["clips"][0]["subject_mask_asset_id"] = "mask-2"
    assert (chunk_render_key(sub_doc=doc, chunk_index=0)
            != chunk_render_key(sub_doc=changed, chunk_index=0))


def test_unchanged_timeline_keeps_the_same_chunk_key():
    from app.engine.captions.cache import chunk_render_key

    doc = _doc()
    assert (chunk_render_key(sub_doc=copy.deepcopy(doc), chunk_index=0)
            == chunk_render_key(sub_doc=doc, chunk_index=0))


def test_chunk_keys_differ_per_chunk_index():
    from app.engine.captions.cache import chunk_render_key

    doc = _doc()
    assert (chunk_render_key(sub_doc=doc, chunk_index=0)
            != chunk_render_key(sub_doc=doc, chunk_index=1))


# ---------------------------------------------------------------------------
# §10 QC: nothing stored may look active when it will not render
# ---------------------------------------------------------------------------


def _qc(checks=None):
    from app.engine.motion.qc import CaptionMotionQC

    return CaptionMotionQC(checks).run()


def _names(report):
    return [c.name for c in report.checks]


def _by_name(report, name):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"QC has no check named {name}: {_names(report)}")


def test_qc_reports_a_transition_that_consumes_its_neighbour():
    doc = _doc()
    doc["tracks"][0]["clips"][0]["duration"] = 1.0
    doc["tracks"][0]["clips"][1]["start"] = 1.0
    doc["tracks"][0]["clips"][1]["duration"] = 1.0
    doc["tracks"][0]["clips"][1]["transition"] = {
        "from_item": "v1", "to_item": "v2", "type": "FADE", "duration": 1.0}
    check = _by_name(_qc(doc), "transition_duration_feasible")
    assert not check.passed
    assert "consumes the shorter clip" in check.detail


def test_qc_reports_a_transition_with_no_adjacent_neighbour():
    doc = _doc()
    doc["tracks"][0]["clips"][1]["transition"] = {
        "from_item": "v1", "to_item": "c1", "type": "FADE", "duration": 0.5}
    check = _by_name(_qc(doc), "transition_neighbour_exists")
    assert not check.passed
    assert "not adjacent" in check.detail or "names no neighbour" in check.detail


def test_qc_reports_a_transition_with_no_neighbour_named():
    doc = _doc()
    doc["tracks"][0]["clips"][1]["transition"] = {"type": "FADE",
                                                  "duration": 0.5}
    assert not _by_name(_qc(doc), "transition_neighbour_exists").passed


def test_qc_reports_malformed_keyframes():
    doc = _doc()
    doc["tracks"][1]["clips"][0]["keyframes"] = [
        {"id": "k1", "t": 99.0, "props": {"x": 0.5}}]
    assert not _by_name(_qc(doc), "keyframes_valid").passed


def test_qc_reports_out_of_range_keyframes():
    doc = _doc()
    doc["tracks"][1]["clips"][0]["keyframes"] = [
        {"id": "k1", "t": 0.0, "props": {"x": 0.1}},
        {"id": "k2", "t": 50.0, "props": {"x": 0.9}}]
    check = _by_name(_qc(doc), "keyframes_valid")
    assert not check.passed
    assert "outside clip duration" in check.detail


def test_qc_reports_unsupported_interpolation():
    doc = _doc()
    doc["tracks"][1]["clips"][0]["keyframes"] = [
        {"id": "k1", "t": 0.0, "easing": "ELASTIC", "props": {"x": 0.1}}]
    assert not _by_name(_qc(doc), "keyframes_valid").passed


def test_qc_reports_a_composite_effect_with_no_mask():
    doc = _doc()
    doc["tracks"][0]["clips"][0]["effects"] = [
        {"type": "BACKGROUND_BLUR", "params": {"radius": 10}}]
    check = _by_name(_qc(doc), "composite_effects_available")
    assert not check.passed
    assert "NOT_AVAILABLE" in check.detail


def test_qc_reports_effects_stored_out_of_render_order():
    doc = _doc()
    doc["tracks"][0]["clips"][0]["effects"] = [
        {"type": "OPACITY", "params": {"alpha": 0.5}},
        {"type": "CROP", "params": {}},
    ]
    check = _by_name(_qc(doc), "effect_graph_order")
    assert not check.passed
    assert "out of render order" in check.detail


def test_qc_passes_a_well_formed_timeline():
    report = _qc(_doc())
    assert report.status == "PASS", [c.detail for c in report.checks
                                     if not c.passed]
    for name in ("transition_neighbour_exists", "effect_graph_order",
                 "composite_effects_available", "transition_duration_feasible",
                 "keyframes_valid"):
        _by_name(report, name)


# ---------------------------------------------------------------------------
# §9 chunk rendering stays compatible
# ---------------------------------------------------------------------------


def test_chunked_sub_document_keeps_its_transitions():
    """Slicing a timeline for chunk render must not drop a transition."""
    from app.engine.motion.graph import plan_transitions

    doc = _doc()
    doc["tracks"][0]["clips"][1]["transition"] = {
        "from_item": "v1", "to_item": "v2", "type": "DISSOLVE", "duration": 0.5}
    segments = [{"clip": {"id": "v1"}, "duration": 3.0},
                {"clip": {"id": "v2"}, "duration": 3.0}]
    plan, warnings = plan_transitions(doc, segments)
    assert plan and not warnings

    # a chunk that contains only the second half has no adjacent pair to
    # transition into, and must SAY so rather than silently rendering a cut
    partial = [{"clip": {"id": "v2"}, "duration": 3.0}]
    partial_plan, partial_warnings = plan_transitions(doc, partial)
    assert not partial_plan
    assert partial_warnings, "an unrenderable transition was silent"


def test_transition_plan_only_matches_adjacent_segments():
    from app.engine.motion.graph import plan_transitions

    doc = _doc()
    doc["tracks"][0]["clips"][1]["transition"] = {
        "from_item": "v1", "to_item": "v2", "type": "FADE", "duration": 0.5}
    # the same clips, but separated by another segment: not adjacent
    segments = [{"clip": {"id": "v1"}, "duration": 1.0},
                {"clip": {"id": "v2"}, "duration": 1.0},
                {"clip": {"id": "v3"}, "duration": 1.0}]
    plan, warnings = plan_transitions(doc, segments)
    assert (0, 1) in plan
    assert (1, 2) not in plan
    assert (2, 3) not in plan
