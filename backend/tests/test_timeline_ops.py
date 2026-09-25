"""Typed timeline operations: split/trim/move/delete/duplicate/props."""
from __future__ import annotations


def _doc():
    from app.engine.timeline import add_clip, create_empty

    t = create_empty("ws", duration_seconds=20.0)
    add_clip(t, track="video", clip_id="v1", name="A", start=0.0, duration=10.0,
             source={"video_id": "x"})
    add_clip(t, track="video", clip_id="v2", name="B", start=10.0, duration=10.0)
    add_clip(t, track="voice", clip_id="n1", name="narr", start=0.0, duration=20.0)
    return t


def test_split_keeps_source_offsets():
    from app.engine.timeline_ops import apply_operations

    out = apply_operations(_doc(), [
        {"type": "split_item", "track": "video", "clip_id": "v1", "at": 4.0}])
    clips = next(tr for tr in out["tracks"] if tr["kind"] == "video")["clips"]
    assert [(c["id"], c["start"], c["duration"]) for c in clips] == [
        ("v1", 0.0, 4.0), ("v1__b", 4.0, 6.0), ("v2", 10.0, 10.0)]
    assert clips[1]["source_start"] == 4.0


def test_trim_start_shifts_source():
    from app.engine.timeline_ops import apply_operations

    out = apply_operations(_doc(), [
        {"type": "trim_item", "track": "video", "clip_id": "v1",
         "edge": "start", "start": 2.0}])
    v1 = next(tr for tr in out["tracks"] if tr["kind"] == "video")["clips"][0]
    assert (v1["start"], v1["duration"], v1["source_start"]) == (2.0, 8.0, 2.0)


def test_trim_end_and_move():
    from app.engine.timeline_ops import apply_operations

    out = apply_operations(_doc(), [
        {"type": "trim_item", "track": "video", "clip_id": "v2", "edge": "end", "end": 15.0},
        {"type": "move_item", "track": "voice", "clip_id": "n1", "start": 0.0},
    ])
    video = next(tr for tr in out["tracks"] if tr["kind"] == "video")["clips"]
    assert video[1]["duration"] == 5.0
    assert out["duration_seconds"] == 20.0


def test_move_between_tracks_enforces_compatibility():
    import pytest

    from app.engine.timeline_ops import TimelineOpError, apply_operations

    out = apply_operations(_doc(), [
        {"type": "move_to_track", "from_track": "voice", "to_track": "music",
         "clip_id": "n1"}])
    assert len(next(tr for tr in out["tracks"] if tr["kind"] == "music")["clips"]) == 1
    with pytest.raises(TimelineOpError, match="incompatible"):
        apply_operations(_doc(), [
            {"type": "move_to_track", "from_track": "voice", "to_track": "text",
             "clip_id": "n1"}])


def test_delete_and_duplicate():
    from app.engine.timeline_ops import apply_operations

    out = apply_operations(_doc(), [
        {"type": "delete_item", "track": "video", "clip_id": "v2"},
        {"type": "duplicate_item", "track": "video", "clip_id": "v1", "at": 10.0},
    ])
    clips = next(tr for tr in out["tracks"] if tr["kind"] == "video")["clips"]
    assert [c["id"] for c in clips] == ["v1", "v1__copy"]
    assert clips[1]["source"] == {"video_id": "x"}  # asset ref reused, bytes not copied


def test_transform_volume_speed_text_caption():
    from app.engine.timeline_ops import apply_operations

    out = apply_operations(_doc(), [
        {"type": "update_transform", "track": "video", "clip_id": "v1",
         "transform": {"scale": 1.2, "x": 10}},
        {"type": "update_volume", "track": "voice", "clip_id": "n1",
         "volume": 0.8, "fade_in": 0.5},
        {"type": "update_speed", "track": "video", "clip_id": "v2", "speed": 1.5},
        {"type": "update_text", "track": "video", "clip_id": "v1",
         "text": {"content": "Hello", "size": 64}},
    ])
    video = {c["id"]: c for c in next(tr for tr in out["tracks"]
                                      if tr["kind"] == "video")["clips"]}
    assert video["v1"]["transform"] == {"scale": 1.2, "x": 10}
    assert video["v1"]["text"]["content"] == "Hello"
    assert video["v2"]["speed"] == 1.5
    voice = next(tr for tr in out["tracks"] if tr["kind"] == "voice")["clips"][0]
    assert (voice["volume"], voice["fade_in"]) == (0.8, 0.5)


def test_batch_is_atomic_and_validated():
    import pytest

    from app.engine.timeline_ops import TimelineOpError, apply_operations

    with pytest.raises(TimelineOpError, match="op 1"):
        apply_operations(_doc(), [
            {"type": "move_item", "track": "video", "clip_id": "v1", "start": 0.0},
            {"type": "split_item", "track": "video", "clip_id": "v1", "at": 99.0},
        ])
    # original untouched (copy semantics)
    assert len(_doc()["tracks"][0]["clips"]) == 2


def test_unknown_op_rejected():
    import pytest

    from app.engine.timeline_ops import TimelineOpError, apply_operations

    with pytest.raises(TimelineOpError, match="unknown op type"):
        apply_operations(_doc(), [{"type": "teleport"}])
