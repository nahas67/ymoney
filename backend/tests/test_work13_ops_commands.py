"""Work 13 — typed timeline ops + CreativeDirector motion commands.

Covers the parts of §17 that need the real operation layer and the real
CreativeDirector flow: every Work 13 op must persist, refuse invalid input, and
survive undo; every motion command must preview before it applies.
"""

from __future__ import annotations

import copy

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _doc():
    """A minimal, valid canonical document with a video + a caption track."""
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
                {"id": "c1", "name": "Save 500 dollars", "start": 0.0,
                 "duration": 2.0, "source": {}, "effects": [],
                 "source_start": 0.0, "volume": 1.0, "speed": 1.0,
                 "fade_in": 0.0, "fade_out": 0.0, "transform": {}, "text": {},
                 "transition_in": "cut", "transition_out": "cut"},
            ]},
            # The real create_empty pre-creates every track kind; motion
            # inserts (lower thirds, titles) land on the text track.
            {"id": "t_text", "kind": "text", "name": "Text", "clips": []},
        ],
    }


def _apply(doc, ops):
    from app.engine.timeline_ops import apply_operations

    return apply_operations(copy.deepcopy(doc), ops)


# ---------------------------------------------------------------------------
# typed ops: persist, validate, refuse
# ---------------------------------------------------------------------------


def test_op_types_include_the_work13_ops():
    from app.engine.timeline_ops import OP_TYPES

    for op in ("update_caption_style", "set_caption_words", "apply_effect",
               "remove_effect", "set_transition"):
        assert op in OP_TYPES


def test_update_caption_style_persists_a_typed_style():
    out = _apply(_doc(), [{
        "type": "update_caption_style", "track": "caption", "clip_id": "c1",
        "preset": "bold_shorts", "style": {"size": 90}}])
    text = out["tracks"][1]["clips"][0]["text"]
    assert text["preset"] == "bold_shorts"
    assert text["size"] == 90
    assert text["stroke_width"] >= 1  # the whole style is materialised


def test_update_caption_style_refuses_invalid_payloads():
    from app.engine.timeline_ops import TimelineOpError

    for op in (
        {"type": "update_caption_style", "track": "caption", "clip_id": "c1",
         "style": {"colour": "red"}},
        {"type": "update_caption_style", "track": "caption", "clip_id": "c1",
         "style": {"primary_color": "not-a-color"}},
        {"type": "update_caption_style", "track": "caption", "clip_id": "c1",
         "preset": "no_such_preset"},
        {"type": "update_caption_style", "track": "caption", "clip_id": "c1"},
    ):
        with pytest.raises(TimelineOpError):
            _apply(_doc(), [op])


def test_set_caption_words_requires_real_numeric_timing():
    out = _apply(_doc(), [{
        "type": "set_caption_words", "track": "caption", "clip_id": "c1",
        "words": [{"word": "Save", "start_s": 0.0, "end_s": 0.5},
                  {"word": "500", "start_s": 0.5, "end_s": 1.0}]}])
    clip = out["tracks"][1]["clips"][0]
    assert clip["word_level"] is True
    assert len(clip["words"]) == 2

    from app.engine.timeline_ops import TimelineOpError

    with pytest.raises(TimelineOpError):
        _apply(_doc(), [{"type": "set_caption_words", "track": "caption",
                         "clip_id": "c1",
                         "words": [{"word": "Save", "start_s": "soon",
                                    "end_s": 1.0}]}])
    with pytest.raises(TimelineOpError):
        _apply(_doc(), [{"type": "set_caption_words", "track": "caption",
                         "clip_id": "c1", "words": "nope"}])


def test_apply_and_remove_effect_round_trip():
    out = _apply(_doc(), [{
        "type": "apply_effect", "track": "video", "clip_id": "v1",
        "effect": {"type": "vignette", "params": {"d0": 0.5}}}])
    effects = out["tracks"][0]["clips"][0]["effects"]
    assert effects and effects[0]["type"] == "VIGNETTE"
    assert effects[0]["params"]["d0"] == 0.5

    # re-applying the same effect replaces rather than duplicating
    twice = _apply(out, [{
        "type": "apply_effect", "track": "video", "clip_id": "v1",
        "effect": {"type": "VIGNETTE", "params": {"d0": 0.9}}}])
    assert len(twice["tracks"][0]["clips"][0]["effects"]) == 1

    removed = _apply(twice, [{"type": "remove_effect", "track": "video",
                              "clip_id": "v1", "effect": "vignette"}])
    assert removed["tracks"][0]["clips"][0]["effects"] == []


def test_apply_effect_refuses_an_unknown_effect():
    from app.engine.timeline_ops import TimelineOpError

    with pytest.raises(TimelineOpError):
        _apply(_doc(), [{"type": "apply_effect", "track": "video",
                         "clip_id": "v1", "effect": {"type": "NOPE"}}])
    with pytest.raises(TimelineOpError):
        _apply(_doc(), [{"type": "remove_effect", "track": "video",
                         "clip_id": "v1", "effect": "VIGNETTE"}])


def test_set_transition_validates_against_real_adjacency():
    from app.engine.timeline_ops import TimelineOpError

    out = _apply(_doc(), [{
        "type": "set_transition", "track": "video", "clip_id": "v1",
        "to_item": "v2",
        "transition": {"from_item": "v1", "to_item": "v2", "type": "dissolve",
                       "duration": 0.5}}])
    clip = out["tracks"][0]["clips"][0]
    assert clip["transition"]["type"] == "DISSOLVE"
    assert clip["transition_in"] == "crossfade"

    with pytest.raises(TimelineOpError):  # v1 and v2 are adjacent, v1/v1 are not
        _apply(_doc(), [{"type": "set_transition", "track": "video",
                         "clip_id": "v1", "to_item": "v1",
                         "transition": {"from_item": "v1", "to_item": "v1",
                                        "type": "fade", "duration": 0.5}}])
    with pytest.raises(TimelineOpError):  # longer than the shorter clip
        _apply(_doc(), [{"type": "set_transition", "track": "video",
                         "clip_id": "v1", "to_item": "v2",
                         "transition": {"from_item": "v1", "to_item": "v2",
                                        "type": "fade", "duration": 99.0}}])


def test_ops_are_deterministic_and_leave_the_input_untouched():
    doc = _doc()
    before = copy.deepcopy(doc)
    ops = [{"type": "update_caption_style", "track": "caption", "clip_id": "c1",
            "preset": "ugc"}]
    first = _apply(doc, ops)
    second = _apply(doc, ops)
    assert first == second, "same ops on same doc must give the same result"
    assert doc == before, "apply_operations must not mutate its input"


# ---------------------------------------------------------------------------
# CreativeDirector: validate -> preview -> apply -> undo
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client):
    import uuid

    from app.db import session_scope
    from app.models import ContentTimeline

    email = f"w13{uuid.uuid4().hex[:8]}@test.local"
    resp = client.post("/api/v1/auth/register",
                       json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ws_id = data["workspace"]["id"]
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    doc = _doc()
    doc["workspace_id"] = ws_id
    with session_scope() as s:
        row = ContentTimeline(workspace_id=ws_id, version=1,
                              tracks_json=doc, fps=30,
                              duration_seconds=6.0)
        s.add(row)
        s.commit()
        timeline_id = row.id
    return ws_id, headers, timeline_id


def _patch(client, ws_id, headers, timeline_id, doc):
    from app.db import session_scope
    from app.models import ContentTimeline

    with session_scope() as s:
        row = s.get(ContentTimeline, timeline_id)
        row.tracks_json = doc
        s.commit()
    _ = client


def _load_doc(timeline_id):
    from app.db import session_scope
    from app.models import ContentTimeline

    with session_scope() as s:
        return copy.deepcopy(dict(s.get(ContentTimeline, timeline_id).tracks_json))


def test_change_caption_style_previews_then_applies(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/creative"

    payload = {"type": "ChangeCaptionStyle", "preset": "bold_shorts",
               "style": {"size": 88},
               "target": {"timeline_id": timeline_id}}

    preview = client.post(f"{base}/preview", headers=headers,
                          json={"commands": [payload]})
    assert preview.status_code == 200, preview.text
    change_set = preview.json()
    assert change_set["changes"][0]["type"] == "ChangeCaptionStyle"
    # preview must NOT mutate the timeline
    assert _load_doc(timeline_id)["tracks"][1]["clips"][0].get("text", {}).get(
        "preset") is None

    applied = client.post(f"{base}/apply", headers=headers, json={
        "commands": [payload], "base_version": 1, "approve": True,
        "preview_id": change_set["preview_id"]})
    assert applied.status_code == 200, applied.text
    new_id = applied.json()["timeline_id"]
    text = _load_doc(new_id)["tracks"][1]["clips"][0]["text"]
    assert text["preset"] == "bold_shorts" and text["size"] == 88


def test_creative_change_caption_style_is_undone(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/creative"
    payload = {"type": "ChangeCaptionStyle", "preset": "news",
               "target": {"timeline_id": timeline_id}}

    preview = client.post(f"{base}/preview", headers=headers,
                          json={"commands": [payload]}).json()
    applied = client.post(f"{base}/apply", headers=headers, json={
        "commands": [payload], "base_version": 1, "approve": True,
        "preview_id": preview["preview_id"]})
    assert applied.status_code == 200, applied.text

    undone = client.post(f"{base}/undo/{applied.json()['timeline_id']}",
                         headers=headers)
    assert undone.status_code == 200, undone.text
    restored = _load_doc(undone.json()["timeline_id"])
    assert restored["tracks"][1]["clips"][0].get("text", {}).get("preset") is None


def test_add_lower_third_refuses_invented_metadata(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    payload = {"type": "AddLowerThird", "kind": "person", "name": "Someone",
               "role": "CEO", "target": {"timeline_id": timeline_id}}
    resp = client.post(f"/api/v1/workspaces/{ws_id}/creative/preview",
                       headers=headers, json={"commands": [payload]})
    assert resp.status_code == 200, resp.text
    entry = resp.json()["changes"][0]
    # No `known` declaration means the role cannot be vouched for.
    assert entry["status"] == "rejected"
    assert "unvouched" in " ".join(entry["reasons"]).lower()


def test_add_lower_third_applies_with_declared_metadata(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/creative"
    payload = {"type": "AddLowerThird", "kind": "person", "name": "Ada",
               "role": "Engineer", "known": ["name", "role"], "start": 1.0,
               "duration": 3.0, "target": {"timeline_id": timeline_id}}

    preview = client.post(f"{base}/preview", headers=headers,
                          json={"commands": [payload]}).json()
    assert preview["changes"][0]["status"] == "ok", preview["changes"][0]
    applied = client.post(f"{base}/apply", headers=headers, json={
        "commands": [payload], "base_version": 1, "approve": True,
        "preview_id": preview["preview_id"]})
    assert applied.status_code == 200, applied.text
    doc = _load_doc(applied.json()["timeline_id"])
    text_clips = [c for tr in doc["tracks"] if tr["kind"] == "text"
                  for c in tr["clips"]]
    assert text_clips and "Ada" in text_clips[0]["text"]["content"]


def test_apply_effect_command_is_rejected_for_an_unknown_effect(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    payload = {"type": "ApplyEffect",
               "effect": {"type": "totally_made_up"},
               "target": {"timeline_id": timeline_id}}
    resp = client.post(f"/api/v1/workspaces/{ws_id}/creative/preview",
                       headers=headers, json={"commands": [payload]})
    entry = resp.json()["changes"][0]
    assert entry["status"] == "rejected"
    assert any("unknown effect" in r.lower() for r in entry["reasons"])


def test_apply_effect_command_applies_a_valid_effect(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/creative"
    payload = {"type": "ApplyEffect",
               "effect": {"type": "COLOR_ADJUST",
                          "params": {"brightness": 0.1}},
               "clip_ids": ["v1"], "target": {"timeline_id": timeline_id}}
    preview = client.post(f"{base}/preview", headers=headers,
                          json={"commands": [payload]}).json()
    assert preview["changes"][0]["status"] == "ok", preview["changes"][0]
    applied = client.post(f"{base}/apply", headers=headers, json={
        "commands": [payload], "base_version": 1, "approve": True,
        "preview_id": preview["preview_id"]})
    assert applied.status_code == 200, applied.text
    doc = _load_doc(applied.json()["timeline_id"])
    effects = doc["tracks"][0]["clips"][0]["effects"]
    assert effects and effects[0]["type"] == "COLOR_ADJUST"


def test_add_transition_command_validates_against_the_document(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/creative"
    good = {"type": "AddTransition", "transition_type": "dissolve",
            "from_item": "v1", "to_item": "v2", "duration": 0.5,
            "target": {"timeline_id": timeline_id}}
    entry = client.post(f"{base}/preview", headers=headers,
                        json={"commands": [good]}).json()["changes"][0]
    assert entry["status"] == "ok", entry

    bad = {"type": "AddTransition", "transition_type": "dissolve",
           "from_item": "v1", "to_item": "v1", "duration": 0.5,
           "target": {"timeline_id": timeline_id}}
    entry2 = client.post(f"{base}/preview", headers=headers,
                         json={"commands": [bad]}).json()["changes"][0]
    assert entry2["status"] == "rejected"


def test_highlight_keyword_refuses_sensitive_kinds(tmp_path, monkeypatch):
    """A protected personal characteristic is never an emphasis kind.

    Two independent guards fire: the kind is not in the closed vocabulary, and
    ``assert_no_sensitive_kinds`` is a tripwire in case anyone ever adds it.
    Either rejection is correct; what matters is that it never succeeds.
    """
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    for kind in ("gender", "AGE", "ethnicity", "religion", "income"):
        payload = {"type": "HighlightKeyword", "kinds": [kind],
                   "target": {"timeline_id": timeline_id}}
        entry = client.post(f"/api/v1/workspaces/{ws_id}/creative/preview",
                            headers=headers, json={
                                "commands": [payload]}
                            ).json()["changes"][0]
        assert entry["status"] == "rejected", f"{kind} must be rejected"
        assert any(kind.upper() in r.upper() for r in entry["reasons"]), entry


def test_highlight_keyword_refuses_without_word_timing(tmp_path, monkeypatch):
    """No stored words -> the command is refused rather than guessing offsets."""
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    payload = {"type": "HighlightKeyword", "kinds": ["NUMBER"],
               "target": {"timeline_id": timeline_id}}
    entry = client.post(f"/api/v1/workspaces/{ws_id}/creative/preview",
                        headers=headers,
                        json={"commands": [payload]}).json()["changes"][0]
    # rejected at validate, or accepted at validate but refused at apply; either
    # way it must never invent word timing.
    if entry["status"] == "ok":
        apply = client.post(f"/api/v1/workspaces/{ws_id}/creative/apply",
                            headers=headers,
                            json={"commands": [payload], "base_version": 1, "approve": True,
                                  "preview_id": entry and
                                  client.post(
                                      f"/api/v1/workspaces/{ws_id}/creative/preview",
                                      headers=headers,
                                      json={"commands": [payload]}
                                  ).json()["preview_id"]})
        assert apply.status_code == 422
        assert "word timing" in apply.text.lower()


def test_motion_commands_are_isolated_by_workspace(tmp_path, monkeypatch):
    """A foreign timeline must 404, never be editable."""
    from starlette.testclient import TestClient

    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(), raise_server_exceptions=False)
    _ws_a, headers_a, timeline_a = _register(client)

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    client_b = TestClient(create_app(), raise_server_exceptions=False)
    import uuid

    email = f"other{uuid.uuid4().hex[:8]}@test.local"
    resp = client_b.post("/api/v1/auth/register",
                         json={"email": email, "password": "supersecret123"})
    headers_b = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws_b = resp.json()["workspace"]["id"]

    # workspace B's member cannot preview an edit to workspace A's timeline
    payload = {"type": "ChangeCaptionStyle", "preset": "ugc",
               "target": {"timeline_id": timeline_a}}
    out = client_b.post(f"/api/v1/workspaces/{ws_b}/creative/preview",
                        headers=headers_b, json={"commands": [payload]})
    assert out.status_code in (200, 404), out.status_code
    if out.status_code == 200:
        entry = out.json()["changes"][0]
        assert entry["status"] == "rejected"
    _ = headers_a

# ---------------------------------------------------------------------------
# HTTP surface: registries, QC and the evidence report
# ---------------------------------------------------------------------------


def test_presets_endpoint_returns_brand_resolved_presets(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, _tid = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/captions/presets", headers=headers)
    assert r.status_code == 200, r.text
    keys = {item["key"] for item in r.json()["items"]}
    assert {"minimal", "bold_shorts", "karaoke", "podcast", "documentary",
            "educational", "news", "ugc", "brand_primary"} <= keys
    for item in r.json()["items"]:
        assert "style" in item and "size" in item["style"]


def test_templates_effects_transitions_endpoints(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, _tid = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/captions"

    templates = client.get(f"{base}/templates", headers=headers)
    assert templates.status_code == 200
    types = {t["type"] for t in templates.json()["items"]}
    assert {"TITLE", "LOWER_THIRD", "CALLOUT", "STAT", "CTA"} <= types

    effects = client.get(f"{base}/effects", headers=headers)
    assert effects.status_code == 200
    names = {e["key"] for e in effects.json()["items"]}
    assert {"BLUR", "VIGNETTE", "ZOOM", "COLOR_ADJUST"} <= names
    for effect in effects.json()["items"]:
        assert "params" in effect

    transitions = client.get(f"{base}/transitions", headers=headers)
    assert transitions.status_code == 200
    assert {"CUT", "FADE", "DISSOLVE", "SLIDE", "WIPE", "ZOOM"} <= {
        t["key"] for t in transitions.json()["items"]}


def test_emphasis_endpoint_never_offers_protected_kinds(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, _tid = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/captions/emphasis",
                   headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "KEYWORD" in data["emphasis_kinds"]
    assert not (set(data["emphasis_kinds"]) & set(
        data["protected_kinds_never_offered"]))


def test_qc_endpoint_runs_and_reports(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, timeline_id = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/captions/qc", headers=headers,
                    json={"timeline_id": timeline_id})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["status"] in ("PASS", "PASS_WITH_WARNINGS", "REVIEW_REQUIRED",
                                "FAIL")
    assert report["checks"]


def test_qc_endpoint_404s_for_a_foreign_timeline(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _ws, headers, _tid = _register(client)
    r = client.post("/api/v1/workspaces/"
                    f"{_ws}/captions/qc", headers=headers,
                    json={"timeline_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 404, r.status_code


def test_evidence_endpoint_explains_why_automation_is_blocked(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers, _tid = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/captions/evidence",
                   headers=headers, params={"asset_id": "not-an-asset"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["allows"]["word_level_captions"] is False
    assert data["word_alignment"]["reason"]
    assert data["blocked_reasons"], "an operator must see WHY it is blocked"


def test_registries_require_a_workspace_role(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/captions/presets")
    assert r.status_code in (401, 403), r.status_code