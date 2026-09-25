"""Campaign analytics rollups + attribution (Work 04, Lane C).

Deterministic, no network. Seeds PublishedPost + PostMetric rows across
variants/platforms and asserts exact rollup math, the
publication → variant → short → master → campaign chain, per-platform and
per-chapter comparisons, learning observations via the existing Learning
Agent storage, and cross-workspace isolation.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.engine.campaign import analytics as ca
from app.models import (
    Campaign,
    ContentItem,
    LearningPattern,
    LongFormChapter,
    LongFormProject,
    MemoryRecord,
    PostMetric,
    PublishedPost,
    Scene,
    VideoVariant,
)
from app.models.base import utcnow


def _seed(db_session, ws_id, tag="a"):
    """Master + 3 shorts + platform posts. Returns id dict."""
    base = utcnow()
    uniq = os.urandom(4).hex()  # video_ids are globally unique per test
    campaign = Campaign(
        workspace_id=ws_id, name=f"Campaign {tag}", goal="test",
        platforms_json=["youtube", "tiktok"],
    )
    db_session.add(campaign)
    db_session.flush()

    master = ContentItem(
        workspace_id=ws_id, campaign_id=campaign.id,
        topic=f"master topic {tag}", status="PUBLISHED",
    )
    db_session.add(master)
    db_session.flush()

    shorts = []
    for i in range(3):
        short = ContentItem(
            workspace_id=ws_id, campaign_id=campaign.id,
            parent_content_id=master.id, root_content_id=master.id,
            derivation_type="short", topic=f"short {tag}-{i}",
            status="PUBLISHED",
            strategy_json={"hook": f"hook {tag}-{i}"},
        )
        db_session.add(short)
        db_session.flush()
        db_session.add(
            VideoVariant(
                content_item_id=short.id, label="v1",
                hook=f"hook {tag}-{i}", script="word " * 60, selected=True,
            )
        )
        shorts.append(short)
    db_session.flush()

    project = LongFormProject(workspace_id=ws_id, topic=f"master topic {tag}")
    db_session.add(project)
    db_session.flush()
    chapters = []
    for i, title in enumerate(("Cold Open", "Deep Dive")):
        chapters.append(
            LongFormChapter(project_id=project.id, index=i, title=title)
        )
    db_session.add_all(chapters)
    db_session.flush()
    # short0 -> Cold Open, short1 -> Deep Dive, short2 -> no scene (fallback)
    for short, chapter in zip(shorts[:2], chapters):
        db_session.add(
            Scene(
                workspace_id=ws_id, content_item_id=short.id,
                chapter_id=chapter.id, index=0, title=chapter.title,
                start_seconds=0.0, end_seconds=30.0,
            )
        )
    db_session.flush()

    def post(content_id, platform, suffix, **metric):
        row = PublishedPost(
            workspace_id=ws_id, content_item_id=content_id,
            video_id=f"vid-{tag}-{uniq}-{suffix}", platform=platform,
            title=f"post {tag} {suffix}", published_at=base,
        )
        db_session.add(row)
        db_session.flush()
        if metric:
            db_session.add(
                PostMetric(post_id=row.id, captured_at=base, **metric)
            )
            db_session.flush()
        return row

    # short0: youtube + tiktok; stale snapshot first to prove latest-wins.
    p_y0 = post(shorts[0].id, "youtube", "s0yt",
                views=10, likes=1, comments=0, shares=0, saves=0,
                watch_time_seconds=5.0, completion_rate=0.1)
    db_session.add(
        PostMetric(post_id=p_y0.id, views=1000, likes=100, comments=10,
                   shares=5, saves=2, watch_time_seconds=500.0,
                   completion_rate=0.5,
                   captured_at=base + timedelta(hours=1))
    )
    post(shorts[0].id, "tiktok", "s0tt",
         views=2000, likes=300, comments=20, shares=10, saves=5,
         watch_time_seconds=900.0, completion_rate=0.25)
    # short1: youtube only.
    post(shorts[1].id, "youtube", "s1yt",
         views=500, likes=25, comments=5, shares=0, saves=1,
         watch_time_seconds=100.0, completion_rate=0.8)
    # short2: published but no metrics yet.
    post(shorts[2].id, "youtube", "s2yt")
    # master direct publication.
    post(master.id, "youtube", "myt",
         views=4000, likes=200, comments=30, shares=20, saves=10,
         watch_time_seconds=2000.0, completion_rate=0.6)
    db_session.flush()
    return {
        "campaign": campaign.id, "master": master.id,
        "shorts": [s.id for s in shorts],
        "chapters": [c.id for c in chapters],
        "post_s0yt": p_y0.id,
    }


def test_rollup_short_adds_up_exactly(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    out = ca.rollup_short(db_session, ws, ids["shorts"][0])
    assert out["views"] == 3000  # latest snapshot wins, stale 10 ignored
    assert out["likes"] == 400
    assert out["comments"] == 30
    assert out["shares"] == 15
    assert out["saves"] == 7
    assert out["watch_time"] == pytest.approx(1400.0)
    assert out["engagement_rate"] == pytest.approx(round(452 / 3000, 4))
    assert out["completion"] == pytest.approx(round(1000 / 3000, 4))
    assert out["posts"] == 2
    assert set(out["variants"]) == {
        db_session.scalar(
            select(VideoVariant.id).where(
                VideoVariant.content_item_id == ids["shorts"][0])
        )
    }


def test_rollup_short_without_metrics_is_zero(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    out = ca.rollup_short(db_session, ws, ids["shorts"][2])
    assert out["views"] == 0
    assert out["engagement_rate"] == 0.0
    assert out["completion"] == 0.0
    assert out["posts"] == 1  # published post exists, just unmeasured


def test_rollup_variant_matches_short_slice(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    variant_id = db_session.scalar(
        select(VideoVariant.id).where(
            VideoVariant.content_item_id == ids["shorts"][0])
    )
    out = ca.rollup_variant(db_session, ws, variant_id)
    assert out["views"] == 3000
    assert out["posts"] == 2


def test_rollup_campaign_includes_shorts_and_master(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    out = ca.rollup_campaign(db_session, ws, ids["campaign"])
    assert out["master_content_id"] == ids["master"]
    assert out["master"]["views"] == 4000
    assert out["short_count"] == 3
    assert out["post_count"] == 5
    assert out["totals"]["views"] == 7500
    assert out["totals"]["likes"] == 625
    assert out["totals"]["comments"] == 65
    assert out["totals"]["shares"] == 35
    assert out["totals"]["saves"] == 18
    assert out["totals"]["watch_time"] == pytest.approx(3500.0)
    assert out["totals"]["engagement_rate"] == pytest.approx(round(743 / 7500, 4))
    assert out["totals"]["completion"] == pytest.approx(round(3800 / 7500, 4))
    assert out["platforms"] == ["tiktok", "youtube"]


def test_compare_platforms(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    out = ca.compare_platforms(db_session, ws, ids["campaign"])
    assert set(out) == {"youtube", "tiktok"}
    assert out["youtube"]["views"] == 5500
    assert out["youtube"]["posts"] == 4
    assert out["youtube"]["likes"] == 325
    assert out["youtube"]["watch_time"] == pytest.approx(2600.0)
    assert out["tiktok"]["views"] == 2000
    assert out["tiktok"]["posts"] == 1
    assert out["tiktok"]["engagement_rate"] == pytest.approx(round(335 / 2000, 4))


def test_chapter_performance_groups_shorts(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    out = ca.chapter_performance(db_session, ws, ids["campaign"])
    by_title = {g["chapter_title"]: g for g in out}
    assert by_title["Cold Open"]["totals"]["views"] == 3000
    assert by_title["Cold Open"]["short_ids"] == [ids["shorts"][0]]
    assert by_title["Cold Open"]["chapter"] == ids["chapters"][0]
    assert by_title["Deep Dive"]["totals"]["views"] == 500
    assert by_title["Deep Dive"]["short_ids"] == [ids["shorts"][1]]
    # short2 has no scene → graceful per-short fallback group
    fallback = [g for g in out if g["chapter"] is None]
    assert len(fallback) == 1
    assert fallback[0]["short_ids"] == [ids["shorts"][2]]
    assert fallback[0]["totals"]["views"] == 0


def test_attribution_chain_resolves(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    chain = ca.resolve_attribution(db_session, ws, ids["post_s0yt"])
    assert chain["platform"] == "youtube"
    assert chain["short_id"] == ids["shorts"][0]
    assert chain["master_id"] == ids["master"]
    assert chain["campaign_id"] == ids["campaign"]


def test_lane_a_variant_plan_and_post_columns(db_session, workspace_with_user):
    """Migration-0016 objects: platform_variants row, campaign_plans master,
    and published_posts lineage columns feed the same rollups/attribution."""
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    now = utcnow()
    variant_id = str(uuid.uuid4())
    db_session.execute(
        text(
            "INSERT INTO platform_variants "
            "(id, created_at, updated_at, workspace_id, campaign_id, "
            "short_content_id, platform, aspect_ratio, metadata_json, "
            "safe_zone_json, status) "
            "VALUES (:id, :now, :now, :ws, :cid, :short, 'youtube', '9:16', "
            "'{}', '{}', 'PUBLISHED')"
        ),
        {"id": variant_id, "now": now, "ws": ws,
         "cid": ids["campaign"], "short": ids["shorts"][0]},
    )
    db_session.execute(
        text(
            "INSERT INTO campaign_plans "
            "(id, created_at, updated_at, workspace_id, campaign_id, "
            "master_content_id, goal, target_platforms, desired_shorts, "
            "duration_min, duration_max, diversity_config, posting_window, "
            "frequency, status, progress_json, cost_usd, error) "
            "VALUES (:id, :now, :now, :ws, :cid, :master, '', '[]', 8, "
            "20.0, 55.0, '{}', '{}', 'daily', 'ACTIVE', '{}', 0.0, '')"
        ),
        {"id": str(uuid.uuid4()), "now": now, "ws": ws,
         "cid": ids["campaign"], "master": ids["master"]},
    )
    db_session.execute(
        text(
            "UPDATE published_posts SET platform_variant_id = :vid, "
            "campaign_id = :cid WHERE id = :pid"
        ),
        {"vid": variant_id, "cid": ids["campaign"], "pid": ids["post_s0yt"]},
    )
    # Campaign-direct publication with no content item at all.
    orphan = PublishedPost(
        workspace_id=ws, content_item_id=None,
        video_id=f"vid-orphan-{os.urandom(4).hex()}", platform="tiktok",
        title="orphan", published_at=now,
    )
    db_session.add(orphan)
    db_session.flush()
    db_session.execute(
        text("UPDATE published_posts SET campaign_id = :cid WHERE id = :pid"),
        {"cid": ids["campaign"], "pid": orphan.id},
    )
    db_session.add(
        PostMetric(post_id=orphan.id, views=700, likes=70, comments=7,
                   shares=7, saves=7, watch_time_seconds=70.0,
                   completion_rate=0.5, captured_at=now)
    )
    db_session.flush()

    variant = ca.rollup_variant(db_session, ws, variant_id)
    assert variant["views"] == 1000  # youtube slice only, not the tiktok post
    assert variant["posts"] == 1

    chain = ca.resolve_attribution(db_session, ws, ids["post_s0yt"])
    assert chain["variant_id"] == variant_id
    assert chain["short_id"] == ids["shorts"][0]
    assert chain["master_id"] == ids["master"]
    assert chain["campaign_id"] == ids["campaign"]

    camp = ca.rollup_campaign(db_session, ws, ids["campaign"])
    assert camp["master_content_id"] == ids["master"]
    assert camp["totals"]["views"] == 8200
    assert camp["post_count"] == 6


def test_record_learning_uses_existing_agent_storage(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    # Agent writes go through their own session_scope (existing Learning
    # Agent mechanism); commit seeds first so a second SQLite connection
    # is not blocked by this test's open write transaction.
    db_session.commit()
    result = ca.record_learning(db_session, ws, ids["campaign"])
    assert result["recorded"] == 2
    patterns = {
        p.pattern_key: p
        for p in db_session.scalars(
            select(LearningPattern).where(LearningPattern.workspace_id == ws)
        ).all()
    }
    assert "campaign_platform_youtube" in patterns  # 5500 views leads
    assert patterns["campaign_platform_youtube"].sample_size == 4
    assert "campaign_chapter_win" in patterns
    assert patterns["campaign_chapter_win"].evidence_json["chapter"] == "Cold Open"
    assert (
        patterns["campaign_chapter_win"].evidence_json["master_topic"]
        == "master topic a"
    )
    memories = db_session.scalars(
        select(MemoryRecord).where(
            MemoryRecord.workspace_id == ws,
            MemoryRecord.source == "learning",
            MemoryRecord.scope.like("campaign_%"),
        )
    ).all()
    assert len(memories) == 2


def test_record_learning_without_data_records_nothing(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    campaign = Campaign(workspace_id=ws, name="empty", goal="x")
    db_session.add(campaign)
    db_session.flush()
    result = ca.record_learning(db_session, ws, campaign.id)
    assert result == {"recorded": 0, "lessons": [], "reason": "no measured posts"}


def test_cross_workspace_isolation(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = _seed(db_session, ws)
    from app.models import User, Workspace, WorkspaceMember

    user2 = User(email="other@test.local", password_hash="x")
    ws2 = Workspace(name="Other WS", slug="ws-other-xyz", niche="money")
    db_session.add_all([user2, ws2])
    db_session.flush()
    db_session.add(
        WorkspaceMember(workspace_id=ws2.id, user_id=user2.id,
                        role=WorkspaceMember.ROLE_OWNER)
    )
    db_session.flush()
    _seed(db_session, ws2.id, tag="b")
    # Inflate ws2 metrics so any leak would be obvious.
    outsider = db_session.scalars(
        select(PublishedPost).where(PublishedPost.workspace_id == ws2.id)
    ).first()
    db_session.add(
        PostMetric(post_id=outsider.id, views=999999, likes=0, comments=0,
                   shares=0, saves=0, watch_time_seconds=0.0,
                   completion_rate=0.0, captured_at=utcnow())
    )
    db_session.flush()

    out = ca.rollup_campaign(db_session, ws, ids["campaign"])
    assert out["totals"]["views"] == 7500
    assert out["post_count"] == 5
    with pytest.raises(ValueError, match="campaign not found"):
        ca.rollup_campaign(db_session, ws2, ids["campaign"])
