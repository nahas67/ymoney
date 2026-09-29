"""Semantic timeline version diff + optimistic-concurrency editor save.

Pure-function cases use synthetic docs; the route cases go through HTTP on a
real workspace (mirrors tests/test_timelines_api.py fixtures).
"""
from __future__ import annotations

import uuid


def _register(client, email=None):
    email = email or f"td{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return (data["workspace"]["id"],
            {"Authorization": f"Bearer {data['access_token']}"},
            data["user"]["id"])


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _make_user(client, ws_id, role):
    """Register a fresh user, grant `role` on ws_id, return their headers."""
    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"{role.lower()}{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role))
    r = client.post("/api/v1/auth/login",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _base_doc(duration: float = 10.0) -> dict:
    """Empty 8-track doc with one video clip (synthetic, diff needs no validator)."""
    from app.engine.timeline import create_empty

    doc = create_empty("ws", duration_seconds=duration)
    video = next(t for t in doc["tracks"] if t["kind"] == "video")
    video["clips"].append(_clip("v1", start=0.0, duration=duration,
                                source={"asset_id": "a1"}))
    return doc


def _clip(clip_id: str, *, start: float = 0.0, duration: float = 5.0,
          source: dict | None = None, text: dict | None = None,
          source_start: float = 0.0, **extra) -> dict:
    clip = {"id": clip_id, "name": clip_id, "start": start,
            "duration": duration, "source": dict(source or {}),
            "effects": [], "source_start": source_start, "volume": 1.0,
            "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
            "text": dict(text or {}), "transition_in": "cut",
            "transition_out": "cut"}
    clip.update(extra)
    return clip


def _clips(doc: dict, kind: str) -> list:
    return next(t for t in doc["tracks"] if t["kind"] == kind)["clips"]


def _modified(diff: dict, clip_id: str) -> dict:
    for entry in diff["modified_clips"]:
        if entry["clip_id"] == clip_id:
            return entry
    raise AssertionError(f"no modified_clips entry for {clip_id}: {diff}")


# ---------------------------------------------------------------------------
# pure: semantic (not JSON-text) diff
# ---------------------------------------------------------------------------


def test_diff_added_and_removed_clips(tmp_path, monkeypatch):
    from app.engine.timeline_diff import diff_timeline_docs

    before = _base_doc(duration=10.0)
    after = _base_doc(duration=10.0)
    _clips(before, "broll").append(_clip("b1", duration=3.0))
    _clips(after, "video").append(_clip("v2", start=10.0, duration=5.0))

    diff = diff_timeline_docs(before, after)
    assert [c["clip_id"] for c in diff["added_clips"]] == ["v2"]
    assert [c["clip_id"] for c in diff["removed_clips"]] == ["b1"]
    assert diff["modified_clips"] == []
    assert diff["summary"]["added"] == 1
    assert diff["summary"]["removed"] == 1
    assert diff["summary"]["changed"] is True
    # a clip reference carries enough to render the notice
    ref = diff["added_clips"][0]
    assert ref["track_id"] == "t_video" and ref["track_kind"] == "video"
    assert ref["duration"] == 5.0


def test_diff_trim_and_timing_are_distinct(tmp_path, monkeypatch):
    from app.engine.timeline_diff import diff_timeline_docs

    # moved on the timeline: timing only, no trim
    before = _base_doc()
    after = _base_doc()
    _clips(after, "video")[0]["start"] = 2.0
    changes = _modified(diff_timeline_docs(before, after), "v1")["changes"]
    assert changes["timing"]["start"] == {"before": 0.0, "after": 2.0}
    assert changes["timing"]["end"] == {"before": 10.0, "after": 12.0}
    assert "trim" not in changes              # nothing moved inside the source

    # shortened in place: both the source out-point and the timeline end move
    before = _base_doc()
    after = _base_doc()
    _clips(after, "video")[0]["duration"] = 8.0
    changes = _modified(diff_timeline_docs(before, after), "v1")["changes"]
    assert changes["trim"]["end"] == {"before": 10.0, "after": 8.0}
    assert changes["timing"]["end"] == {"before": 10.0, "after": 8.0}
    assert "start" not in changes["timing"]   # the clip did not move

    # re-in-pointed with a compensating duration: trim.start only
    before = _base_doc()
    after = _base_doc()
    clip = _clips(after, "video")[0]
    clip["source_start"] = 2.0
    clip["duration"] = 8.0                     # 2 + 8 == 0 + 10: same out-point
    changes = _modified(diff_timeline_docs(before, after), "v1")["changes"]
    # the in-point moved while the out-point (source + timeline) stayed put
    assert changes["trim"] == {"start": {"before": 0.0, "after": 2.0}}
    assert changes["timing"] == {"end": {"before": 10.0, "after": 8.0}}


def test_diff_asset_text_voice_and_metadata(tmp_path, monkeypatch):
    from app.engine.timeline_diff import diff_timeline_docs

    before = _base_doc()
    after = _base_doc()
    # asset swap
    _clips(after, "video")[0]["source"] = {"asset_id": "a2"}
    # a voice clip whose TTS voice is swapped
    _clips(before, "voice").append(_clip("vo1", duration=5.0,
                                         source={"voice_id": "andrew"}))
    _clips(after, "voice").append(_clip("vo1", duration=5.0,
                                        source={"voice_id": "rachel"}))
    # a caption whose text changes
    _clips(before, "caption").append(_clip("cap1", duration=5.0,
                                           text={"content": "Old hook"}))
    _clips(after, "caption").append(_clip("cap1", duration=5.0,
                                          text={"content": "New hook"}))
    # a metadata-only edit (speed/volume)
    _clips(before, "music").append(_clip("mus1", duration=5.0))
    meta_clip = _clip("mus1", duration=5.0)
    meta_clip["speed"] = 1.5
    meta_clip["volume"] = 0.4
    _clips(after, "music").append(meta_clip)

    diff = diff_timeline_docs(before, after)
    assert diff["summary"]["modified"] == 4
    assert diff["summary"]["added"] == 0
    assert diff["summary"]["removed"] == 0

    asset = _modified(diff, "v1")["changes"]
    assert asset["asset"] == {"before": {"asset_id": "a1"},
                              "after": {"asset_id": "a2"}}
    assert "voice" not in asset and "text" not in asset

    voice = _modified(diff, "vo1")["changes"]
    assert voice["voice"] == {"before": {"voice_id": "andrew"},
                              "after": {"voice_id": "rachel"}}
    assert "asset" not in voice          # voice keys never read as an asset swap

    text = _modified(diff, "cap1")["changes"]
    assert text["text"] == {"before": {"text": {"content": "Old hook"}},
                            "after": {"text": {"content": "New hook"}}}

    meta = _modified(diff, "mus1")["changes"]
    assert set(meta) == {"metadata"}
    assert meta["metadata"]["before"] == {"speed": 1.0, "volume": 1.0}
    assert meta["metadata"]["after"] == {"speed": 1.5, "volume": 0.4}


def test_diff_tracks_and_doc_metadata(tmp_path, monkeypatch):
    from app.engine.timeline_diff import diff_timeline_docs

    before = _base_doc()
    before["name"] = "main"
    after = _base_doc()
    after["name"] = "renamed"
    after["tracks"].append({"id": "t_avatar", "kind": "avatar", "name": "Avatar",
                            "clips": [_clip("av1", duration=4.0)]})
    after["tracks"] = [t for t in after["tracks"] if t["kind"] != "music"]
    after["duration_seconds"] = 14.0

    diff = diff_timeline_docs(before, after)
    assert [t["track_id"] for t in diff["added_tracks"]] == ["t_avatar"]
    assert [t["kind"] for t in diff["removed_tracks"]] == ["music"]
    assert [c["clip_id"] for c in diff["added_clips"]] == ["av1"]
    assert diff["removed_clips"] == []        # the music track carried no clips
    assert diff["metadata_changes"]["duration_seconds"] == {"before": 10.0,
                                                            "after": 14.0}
    assert diff["metadata_changes"]["name"] == {"before": "main", "after": "renamed"}
    assert diff["summary"]["tracks_added"] == 1
    assert diff["summary"]["tracks_removed"] == 1
    assert diff["summary"]["metadata_changed"] == 2


def test_diff_identical_docs_is_empty(tmp_path, monkeypatch):
    from app.engine.timeline_diff import diff_timeline_docs

    before = _base_doc()
    after = _base_doc()
    diff = diff_timeline_docs(before, after)
    assert diff["added_clips"] == []
    assert diff["removed_clips"] == []
    assert diff["modified_clips"] == []
    assert diff["added_tracks"] == [] and diff["removed_tracks"] == []
    assert diff["metadata_changes"] == {}
    assert diff["summary"] == {"added": 0, "removed": 0, "modified": 0,
                               "tracks_added": 0, "tracks_removed": 0,
                               "metadata_changed": 0, "changed": False}


def test_brand_snapshot_diff(tmp_path, monkeypatch):
    from app.engine.timeline_diff import diff_brand_snapshots

    before = {"tone": "calm", "palette": {"primary": "#111", "accent": "#222"},
              "forbidden_phrases": ["buy now"], "legacy": "old"}
    after = {"tone": "bold", "palette": {"primary": "#0af", "accent": "#222"},
             "forbidden_phrases": ["buy now", "guaranteed"], "fresh": "new"}

    out = diff_brand_snapshots(before, after)
    assert out["changed"]["tone"] == {"before": "calm", "after": "bold"}
    # one level of nesting: the sub-key that moved is named
    palette = out["changed"]["palette"]
    assert palette["before"]["primary"] == "#111"
    assert palette["after"]["primary"] == "#0af"
    assert palette["changed_keys"] == ["primary"]
    assert out["changed"]["forbidden_phrases"]["after"] == ["buy now", "guaranteed"]
    assert out["added"] == {"fresh": {"after": "new"}}
    assert out["removed"] == {"legacy": {"before": "old"}}

    assert diff_brand_snapshots(before, dict(before)) == {
        "changed": {}, "added": {}, "removed": {}}
    assert diff_brand_snapshots(None, {}) == {"changed": {}, "added": {},
                                              "removed": {}}


# ---------------------------------------------------------------------------
# route: GET /timelines/{id}/diff
# ---------------------------------------------------------------------------


def _create(client, ws, headers, *, duration=10.0):
    from app.engine.timeline import TRACK_KINDS

    tracks = [{"id": f"t_{k}", "kind": k, "name": k.title(), "clips": []}
              for k in TRACK_KINDS]
    next(t for t in tracks if t["kind"] == "video")["clips"].append(
        _clip("v1", duration=duration, source={"asset_id": "a1"}))
    r = client.post(f"/api/v1/workspaces/{ws}/timelines", headers=headers,
                    json={"name": "main", "duration_seconds": duration,
                          "tracks": tracks})
    assert r.status_code == 200, r.text
    return r.json()


def test_diff_route_end_to_end(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl1 = _create(client, ws, headers)                      # version 1
    assert tl1["version"] == 1

    # copy-on-write so BOTH versions stay addressable
    r = client.post(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/versions",
                    headers=headers, json={"label": "v2"})
    assert r.status_code == 200, r.text
    child = r.json()
    assert child["version"] == 2

    # edit the copy through the guarded save (moves it to version 3)
    tracks = [dict(t) for t in child["tracks"]]
    video = next(t for t in tracks if t["kind"] == "video")
    video["clips"] = [_clip("v1", start=1.5, duration=5.0,
                            source={"asset_id": "a2"},
                            text={"content": "hi"})]
    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{child['id']}",
                   headers=headers,
                   json={"tracks": tracks, "duration_seconds": 11.5,
                         "base_version": child["version"]})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3

    r = client.get(
        f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/diff",
        headers=headers, params={"from_version": 1, "to_version": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from"] == {"version": 1,
                            "manifest_hash": body["from"]["manifest_hash"]}
    assert len(body["from"]["manifest_hash"]) == 32
    assert body["to"]["version"] == 3 and len(body["to"]["manifest_hash"]) == 32
    assert body["from"]["manifest_hash"] != body["to"]["manifest_hash"]

    changes = _modified(body["diff"], "v1")["changes"]
    assert changes["timing"]["start"] == {"before": 0.0, "after": 1.5}
    assert changes["asset"]["after"] == {"asset_id": "a2"}
    assert changes["text"]["after"]["text"]["content"] == "hi"
    assert body["diff"]["summary"]["modified"] == 1
    assert body["diff"]["summary"]["changed"] is True
    # no brand policy snapshot exists for this workspace → never fabricated
    assert body["brand_diff"]["available"] is False

    # from == to is a valid empty diff
    r = client.get(
        f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/diff",
        headers=headers, params={"from_version": 3, "to_version": 3})
    assert r.status_code == 200, r.text
    assert r.json()["diff"]["summary"]["changed"] is False
    assert r.json()["diff"]["modified_clips"] == []

    # version 2 no longer exists (in-place save superseded it) → 404
    r = client.get(
        f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/diff",
        headers=headers, params={"from_version": 2, "to_version": 3})
    assert r.status_code == 404, r.text

    # invalid / missing params → 422
    r = client.get(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/diff",
                   headers=headers, params={"from_version": 1})
    assert r.status_code == 422, r.text
    r = client.get(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/diff",
                   headers=headers,
                   params={"from_version": "x", "to_version": 3})
    assert r.status_code == 422, r.text


def test_diff_route_isolation_and_viewer_role(tmp_path, monkeypatch):
    from app.models import WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl = _create(client, ws, headers)
    url = f"/api/v1/workspaces/{ws}/timelines/{tl['id']}/diff"
    params = {"from_version": 1, "to_version": 1}

    viewer = _make_user(client, ws, WorkspaceMember.ROLE_VIEWER)
    r = client.get(url, headers=viewer, params=params)
    assert r.status_code == 200, r.text
    assert r.json()["diff"]["summary"]["changed"] is False

    # a viewer must not be able to SAVE
    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{tl['id']}", headers=viewer,
                   json={"base_version": 1})
    assert r.status_code == 403, r.text

    # foreign workspace → 404 (existence never hinted)
    ws2, headers2, _ = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws2}/timelines/{tl['id']}/diff",
                   headers=headers2, params=params)
    assert r.status_code == 404, r.text


def test_diff_route_brand_snapshots_when_recorded(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.db import session_scope
    from app.models import BrandEffectiveConfig, ContentTimeline

    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl1 = _create(client, ws, headers)
    r = client.post(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/versions",
                    headers=headers, json={"label": "v2"})
    child = r.json()

    now = datetime.now(UTC).replace(tzinfo=None)   # naive UTC, same as the ORM
    with session_scope() as s:
        first = s.get(ContentTimeline, tl1["id"])
        second = s.get(ContentTimeline, child["id"])
        first.created_at = now - timedelta(minutes=2)
        second.created_at = now
        snap_old = BrandEffectiveConfig(
            workspace_id=ws, subject_type="workspace", subject_id="",
            effective_json={"effective": {"tone": "calm",
                                          "palette": {"primary": "#111"},
                                          "legacy": "old"}},
            dna_version="sha256:aaa", note="t")
        snap_new = BrandEffectiveConfig(
            workspace_id=ws, subject_type="workspace", subject_id="",
            effective_json={"effective": {"tone": "bold",
                                          "palette": {"primary": "#0af"},
                                          "fresh": "new"}},
            dna_version="sha256:bbb", note="t")
        s.add_all([snap_old, snap_new])
        s.flush()
        snap_old.created_at = now - timedelta(minutes=3)
        snap_new.created_at = now - timedelta(minutes=1)

    r = client.get(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/diff",
                   headers=headers, params={"from_version": 1, "to_version": 2})
    assert r.status_code == 200, r.text
    brand = r.json()["brand_diff"]
    assert brand["available"] is True
    assert brand["from"]["dna_version"] == "sha256:aaa"
    assert brand["to"]["dna_version"] == "sha256:bbb"
    assert brand["diff"]["changed"]["tone"] == {"before": "calm", "after": "bold"}
    assert brand["diff"]["changed"]["palette"]["changed_keys"] == ["primary"]
    assert brand["diff"]["added"] == {"fresh": {"after": "new"}}
    assert brand["diff"]["removed"] == {"legacy": {"before": "old"}}
    # the timeline itself did not change between the two copy-on-write rows
    assert r.json()["diff"]["summary"]["changed"] is False


# ---------------------------------------------------------------------------
# PUT: optimistic concurrency (no silent last-write-wins)
# ---------------------------------------------------------------------------


def test_put_requires_base_version(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl = _create(client, ws, headers)

    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{tl['id']}",
                   headers=headers, json={"tracks": tl["tracks"]})
    assert r.status_code == 422, r.text


def test_put_stale_base_version_is_409(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl = _create(client, ws, headers)
    url = f"/api/v1/workspaces/{ws}/timelines/{tl['id']}"

    r = client.put(url, headers=headers,
                   json={"tracks": tl["tracks"], "base_version": 99})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "stale timeline version — reload latest"
    assert detail["expected_version"] == 1
    assert detail["actual_version"] == 99

    # nothing was written: a reload still sees version 1 with the same clips
    r = client.get(url, headers=headers)
    assert r.json()["version"] == 1
    assert _clips(r.json(), "video")[0]["id"] == "v1"


def test_put_correct_base_version_bumps_and_blocks_the_old_one(tmp_path,
                                                               monkeypatch):
    from app.db import session_scope
    from app.models import ContentTimeline

    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl = _create(client, ws, headers)
    url = f"/api/v1/workspaces/{ws}/timelines/{tl['id']}"

    tracks = [dict(t) for t in tl["tracks"]]
    next(t for t in tracks if t["kind"] == "video")["clips"] = [
        _clip("v1", duration=7.0, source={"asset_id": "a9"})]
    r = client.put(url, headers=headers,
                   json={"tracks": tracks, "duration_seconds": 7.0,
                         "base_version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["tracks"] == tracks

    # fresh session (never the handler's): the bump really landed
    with session_scope() as s:
        row = s.get(ContentTimeline, tl["id"])
        assert int(row.version) == 2
        assert row.tracks_json["tracks"][0]["clips"][0]["source"] == {
            "asset_id": "a9"}

    # the same (now stale) base_version must not overwrite the newer save
    r = client.put(url, headers=headers,
                   json={"tracks": tl["tracks"], "base_version": 1})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["expected_version"] == 2
    assert detail["actual_version"] == 1

    # and the second writer's tracks never landed
    r = client.get(url, headers=headers)
    assert r.json()["version"] == 2
    assert _clips(r.json(), "video")[0]["source"] == {"asset_id": "a9"}


def test_put_never_rewrites_a_superseded_history_row(tmp_path, monkeypatch):
    """The gate compares against the TIP, but only the tip row may be written."""
    from app.db import session_scope
    from app.models import ContentTimeline

    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl1 = _create(client, ws, headers)                      # version 1 (parent)
    r = client.post(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}/versions",
                    headers=headers, json={"label": "v2"})
    assert r.status_code == 200, r.text
    child = r.json()
    assert child["version"] == 2                            # tip

    # base_version matches the tip, but the save targets the superseded parent
    tracks = [dict(t) for t in tl1["tracks"]]
    next(t for t in tracks if t["kind"] == "video")["clips"] = [
        _clip("v1", duration=3.0, source={"asset_id": "hack"})]
    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{tl1['id']}",
                   headers=headers,
                   json={"tracks": tracks, "base_version": child["version"]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "stale timeline version — reload latest"
    assert detail["expected_version"] == 2

    # history stays append-only: neither row was touched
    with session_scope() as s:
        parent = s.get(ContentTimeline, tl1["id"])
        assert int(parent.version) == 1
        video = next(t for t in parent.tracks_json["tracks"]
                     if t["kind"] == "video")
        assert video["clips"][0]["source"] == {"asset_id": "a1"}
        tip = s.get(ContentTimeline, child["id"])
        assert int(tip.version) == 2


def test_operations_stale_gate_still_409(tmp_path, monkeypatch):
    """The Work 02 operations gate must keep its exact 409 contract."""
    client = _client(tmp_path, monkeypatch)
    ws, headers, _ = _register(client)
    tl = _create(client, ws, headers)
    url = f"/api/v1/workspaces/{ws}/timelines/{tl['id']}/operations"

    r = client.post(url, headers=headers,
                    json={"base_version": 1,
                          "operations": [{"type": "update_volume", "track": "video",
                                          "clip_id": "v1", "volume": 0.5}]})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    r = client.post(url, headers=headers,
                    json={"base_version": 1,
                          "operations": [{"type": "delete_item", "track": "video",
                                          "clip_id": "v1"}]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "stale timeline version — reload latest"
    assert detail["expected_version"] == 2
    # nothing applied
    r = client.get(f"/api/v1/workspaces/{ws}/timelines/{tl['id']}",
                   headers=headers)
    assert len(_clips(r.json(), "video")) == 1
