"""Work 01: real OTIO roundtrip (opentimelineio==0.18.1, Apache-2.0)."""
from __future__ import annotations


def _doc():
    from app.engine.timeline import add_clip, create_empty

    t = create_empty("ws", duration_seconds=30.0, fps=30.0)
    add_clip(t, track="video", clip_id="c1", name="Hook", start=0.0, duration=10.0,
             source={"video_id": "v1"}, effects=["fade_in"])
    add_clip(t, track="video", clip_id="c2", name="Payoff", start=10.0, duration=20.0)
    add_clip(t, track="voice", clip_id="n1", name="say the hook", start=0.0, duration=10.0)
    return t


def test_otio_real_roundtrip_preserves_timing_order_media():
    from app.engine import otio_adapter as oa

    back = oa.roundtrip_serialized(_doc())
    video = next(tr for tr in back["tracks"] if tr["kind"] == "video")
    assert [c["id"] for c in video["clips"]] == ["c1", "c2"]
    assert [c["duration"] for c in video["clips"]] == [10.0, 20.0]
    assert video["clips"][0]["source"] == {"video_id": "v1"}
    assert video["clips"][0]["effects"] == ["fade_in"]
    voice = next(tr for tr in back["tracks"] if tr["kind"] == "voice")
    assert voice["clips"][0]["name"] == "say the hook"


def test_otio_export_is_valid_otio_json():
    import json

    import opentimelineio as otio

    from app.engine import otio_adapter as oa

    text = otio.adapters.write_to_string(oa.to_otio_timeline(_doc()), "otio_json")
    parsed = json.loads(text)
    assert parsed["OTIO_SCHEMA"] == "Timeline.1"


def test_manifest_to_render_request_adapter():
    from app.engine.timeline import (
        add_clip,
        create_empty,
        manifest_to_render_request,
        render_manifest,
    )

    t = create_empty("ws", duration_seconds=10.0)
    add_clip(t, track="voice", clip_id="n1", name="say the hook", start=0.0, duration=10.0)
    add_clip(t, track="broll", clip_id="b1", name="city night", start=0.0, duration=10.0)
    manifest = render_manifest(t)
    req = manifest_to_render_request(t, timeline_name="Night money", workspace_id="ws")
    assert req.subject == "Night money"
    assert "say the hook" in req.script
    assert "city night" in req.keywords
    assert req.aspect_ratio == "9:16" and req.workspace_id == "ws"
    assert manifest["clip_count"] == 2 and len(manifest["manifest_hash"]) == 32
