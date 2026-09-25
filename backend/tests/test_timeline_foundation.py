"""Phase 1: canonical editorial timeline — model, OTIO interchange, manifest."""
from __future__ import annotations


def _track(t, kind="video"):
    return next(tr for tr in t["tracks"] if tr["kind"] == kind)


def test_otio_roundtrip_preserves_order_and_duration():
    from app.engine.timeline import add_clip, create_empty, from_otio_dict, to_otio_dict

    t = create_empty("ws", duration_seconds=30.0)
    add_clip(t, track="video", clip_id="c1", name="A", start=0.0, duration=10.0)
    add_clip(t, track="video", clip_id="c2", name="B", start=10.0, duration=20.0)
    back = from_otio_dict(to_otio_dict(t))
    clips = _track(back)["clips"]
    assert [c["id"] for c in clips] == ["c1", "c2"]
    assert [c["duration"] for c in clips] == [10.0, 20.0]


def test_validation_rejects_overlap_with_named_error():
    import pytest

    from app.engine.timeline import (
        TimelineValidationError,
        add_clip,
        create_empty,
        validate_timeline,
    )

    t = create_empty("ws")
    add_clip(t, track="video", clip_id="c1", name="A", start=0.0, duration=10.0)
    add_clip(t, track="video", clip_id="c2", name="B", start=5.0, duration=5.0)
    with pytest.raises(TimelineValidationError, match="video.*c2"):
        validate_timeline(t)


def test_validation_rejects_unknown_track_kind():
    import pytest

    from app.engine.timeline import TimelineValidationError, validate_timeline

    bad = {"tracks": [{"id": "t1", "kind": "smell", "name": "x", "clips": []}],
           "duration_seconds": 0.0, "fps": 30.0}
    with pytest.raises(TimelineValidationError, match="smell"):
        validate_timeline(bad)


def test_from_video_without_duration_uses_fallback():
    from app.engine.timeline import timeline_from_video

    t = timeline_from_video("ws", video_id="v1", duration_seconds=None, aspect="9:16")
    assert t["duration_seconds"] == 5.0
    assert _track(t)["clips"][0]["source"]["video_id"] == "v1"
    assert t["aspect_ratio"] == "9:16"


def test_shorts_representation_accepts_standard_ratios():
    from app.engine.timeline import shorts_representation, timeline_from_video

    t = timeline_from_video("ws", video_id="v1", duration_seconds=12.0, aspect="9:16")
    assert shorts_representation(t, aspect="9:16")["ok"] is True
    assert shorts_representation(t, aspect="16:9")["ok"] is True
    assert shorts_representation(t, aspect="3:7")["ok"] is False


def test_render_manifest_hash_stable():
    from app.engine.timeline import add_clip, create_empty, render_manifest

    t = create_empty("ws", duration_seconds=10.0)
    add_clip(t, track="voice", clip_id="n1", name="narr", start=0.0, duration=10.0)
    first = render_manifest(t)
    assert first["manifest_hash"] == render_manifest(t)["manifest_hash"]
    assert first["total_duration"] == 10.0
    assert first["fps"] == 30.0


def test_persistence_and_version_history():
    import uuid

    from app.db import session_scope
    from app.engine.timeline import create_empty, save_version
    from app.models import ContentTimeline, Workspace

    slug = f"tl-{uuid.uuid4().hex[:8]}"
    with session_scope() as s:
        ws = Workspace(name="Timeline WS", slug=slug, niche="test")
        s.add(ws)
        s.flush()
        ws_id = ws.id
    with session_scope() as s:
        row = ContentTimeline(workspace_id=ws_id, name="main",
                              duration_seconds=8.0, tracks_json=create_empty(ws_id))
        s.add(row)
        s.flush()
        tid = row.id
        v2_id = save_version(s, tid, label="hook swap")
    with session_scope() as s:
        row2 = s.get(ContentTimeline, v2_id)
        assert row2 is not None
        assert row2.version == 2 and row2.parent_timeline_id == tid
        assert row2.workspace_id == ws_id


def test_validation_tolerates_float_tiling_ulp():
    # algebraically tiled boundaries (a+s*k vs (a+s2*k)+d2*k) differ by 1 ulp;
    # that must not read as an overlap (real long-form beat rescaling hit this)
    from app.engine.timeline import add_clip, create_empty, validate_timeline

    t = create_empty("ws")
    scale = 23.846125999999998 / 24.8
    cursor = 0.0
    for i, (s, d) in enumerate([(0.0, 3.5), (3.5, 6.0), (9.5, 6.0), (15.5, 3.5)]):
        add_clip(t, track="video", clip_id=f"c{i}", name=f"c{i}",
                 start=s * scale, duration=d * scale)
    validate_timeline(t)
