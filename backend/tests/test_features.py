"""Lane B feature extraction: hook/caption/shot math, null voice, no sensitive inference."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.engine.performance.features import extract_features
from app.models.assets import MediaAsset, Scene
from app.models.campaign import PlatformVariant
from app.models.content import ContentItem, ScheduleEntry, VideoVariant
from app.models.timeline import ContentTimeline

FORBIDDEN = ("gender", "age", "race", "ethnicity", "religion")


def _clip(cid, start, duration, source=None, text=None):
    return {"id": cid, "name": cid, "start": start, "duration": duration,
            "source": source or {}, "effects": [], "text": text or {}}


def _track(kind, clips):
    return {"id": f"t_{kind}", "kind": kind, "name": kind.title(), "clips": clips}


def _seed_full(db_session, ws_id):
    item = ContentItem(workspace_id=ws_id, topic="compound interest",
                       strategy_json={"duration_seconds": 32, "aspect_ratio": "9:16",
                                      "hook_type": "question", "cta": "follow for more"},
                       research_json={})
    db_session.add(item)
    db_session.flush()
    db_session.add(VideoVariant(content_item_id=item.id, label="v1",
                                hook="What if your money worked while you sleep?",
                                script="w" * 10, selected=True))
    tracks = [
        _track("video", [_clip("a", 0.0, 4.0), _clip("b", 4.0, 6.0)]),
        _track("broll", [_clip("br", 0.0, 5.0)]),
        _track("caption", [_clip("cc", 0.0, 10.0, {"preset": "bold-yellow"},
                                 {"content": "hi", "align": "bottom-center"})]),
        _track("voice", [_clip("vx", 0.0, 10.0,
                               {"voice_id": "voice-v1", "gender": "female"})]),
        _track("music", [_clip("mu", 0.0, 10.0, {"style": "lofi"})]),
        _track("avatar", []),
    ]
    db_session.add(ContentTimeline(
        workspace_id=ws_id, content_item_id=item.id, name="main",
        duration_seconds=10.0,
        tracks_json={"tracks": tracks, "aspect_ratio": "9:16", "duration_seconds": 10.0}))
    for i in range(3):
        db_session.add(Scene(workspace_id=ws_id, content_item_id=item.id, index=i,
                             title=f"s{i}", start_seconds=float(i * 3),
                             end_seconds=float(i * 3 + 3),
                             chapter_id="ch-1" if i == 0 else ""))
    asset = MediaAsset(workspace_id=ws_id, type="thumbnail", origin="upload",
                       storage_key="cover.png", mime_type="image/png")
    db_session.add(asset)
    db_session.flush()
    db_session.add(PlatformVariant(
        workspace_id=ws_id, campaign_id="camp-1", short_content_id=item.id,
        platform="youtube", aspect_ratio="9:16",
        metadata_json={"cta_kind": "FOLLOW", "title_style": "numbered"},
        cover_asset_id=asset.id))
    db_session.add(ScheduleEntry(workspace_id=ws_id, content_item_id=item.id,
                                 platform="youtube", run_at=datetime(2026, 9, 1, 12, 0)))
    db_session.commit()
    return item.id


def _assert_no_sensitive_keys(obj):
    import re

    pattern = re.compile(r"\b(gender|age|ages|race|ethnicity|religion)\b", re.IGNORECASE)
    if isinstance(obj, dict):
        for key, val in obj.items():
            assert not pattern.search(str(key)), f"sensitive key: {key}"
            _assert_no_sensitive_keys(val)
    elif isinstance(obj, list):
        for val in obj:
            _assert_no_sensitive_keys(val)


def test_hook_caption_shot_extraction(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    item_id = _seed_full(db_session, ws_id)

    feats = extract_features(db_session, ws_id, item_id)

    assert feats["hook_type"] == "QUESTION"
    assert feats["hook_duration"] == pytest.approx(8 / 2.6, abs=0.01)
    assert feats["first_cut_time"] == 4.0
    assert feats["average_shot_length"] == pytest.approx(5.0)
    assert feats["scene_count"] == 3
    assert feats["chapter"] == "ch-1"
    assert feats["caption_style"] == "bold-yellow"
    assert feats["caption_position"] == "bottom-center"
    assert feats["voice_id"] == "voice-v1"
    assert feats["voice_style"] is None
    assert feats["music_presence"] is True
    assert feats["music_style"] == "lofi"
    assert feats["broll_density"] == pytest.approx(0.5)
    assert feats["avatar_presence"] is False
    assert feats["cta_type"] == "FOLLOW"
    assert feats["video_duration"] == 10.0
    assert feats["aspect_ratio"] == "9:16"
    assert feats["posting_time"] == "2026-09-01T12:00:00Z"
    assert feats["title_style"] == "numbered"
    assert feats["thumbnail_style"] == "custom"
    assert feats["topic"] == "compound interest"
    assert feats["inferred"] == {}
    _assert_no_sensitive_keys(feats)


def test_null_voice_when_unconfigured(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    item = ContentItem(workspace_id=ws_id, topic="bare topic", strategy_json={},
                       research_json={})
    db_session.add(item)
    db_session.flush()
    db_session.add(ContentTimeline(
        workspace_id=ws_id, content_item_id=item.id, name="main",
        tracks_json={"tracks": [_track("voice", [_clip("vx", 0.0, 5.0)])]}))
    db_session.commit()

    feats = extract_features(db_session, ws_id, item.id)

    assert feats["voice_id"] is None
    assert feats["voice_style"] is None
    assert feats["hook_type"] is None
    assert feats["scene_count"] == 0
    assert feats["chapter"] is None
    assert feats["posting_time"] is None
    assert feats["inferred"] == {}
    _assert_no_sensitive_keys(feats)


def test_missing_or_foreign_content_raises(db_session, workspace_with_user):
    from app.models import Workspace

    ws_id = workspace_with_user["workspace"]
    with pytest.raises(LookupError):
        extract_features(db_session, ws_id, "no-such-id")
    other = Workspace(name="Other WS", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    db_session.flush()
    foreign = ContentItem(workspace_id=other.id, topic="x", strategy_json={},
                          research_json={})
    db_session.add(foreign)
    db_session.commit()
    with pytest.raises(LookupError):
        extract_features(db_session, ws_id, foreign.id)


def test_features_persisted_to_creative_features(db_session, workspace_with_user):
    import json

    from sqlalchemy import text

    ws_id = workspace_with_user["workspace"]
    item_id = _seed_full(db_session, ws_id)
    extract_features(db_session, ws_id, item_id)

    rows = db_session.execute(
        text("SELECT features_json FROM creative_features WHERE content_item_id = :c"),
        {"c": item_id},
    ).all()
    assert len(rows) == 1
    stored = json.loads(rows[0][0]) if isinstance(rows[0][0], str) else rows[0][0]
    assert stored["hook_type"] == "QUESTION"
    assert stored["topic"] == "compound interest"
    _assert_no_sensitive_keys(stored)
