"""Work 10 Lane E — ``SchedulerAgent.recommend_response_windows`` honesty.

Covers: recommendation-only default + explicit opt-in, measured-data honesty
(no invented best times), audience-activity hour bucketing, live autonomy caps,
campaign-schedule avoid hours, workspace isolation and deterministic output.
In-process DB only — no network, no new dependencies.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest

# ---------------------------------------------------------------------------
# seeding helpers (fixture style mirrors test_community_policy.py)
# ---------------------------------------------------------------------------


def _mk_workspace(db, settings_json=None) -> str:
    from app.models import Workspace

    ws = Workspace(name="Windows WS", slug=f"win-{uuid.uuid4().hex[:8]}",
                   niche="money", settings_json=dict(settings_json or {}))
    db.add(ws)
    db.flush()
    return ws.id


def _set_settings(db, ws_id: str, **top) -> str:
    """Merge top-level ``Workspace.settings_json`` keys (e.g. schedule_automation)."""
    from app.models import Workspace

    ws = db.get(Workspace, ws_id)
    settings = dict(ws.settings_json or {})
    settings.update(top)
    ws.settings_json = settings
    db.flush()
    return ws_id


def _set_community(db, ws_id: str, **community) -> str:
    """Merge ``settings_json["community"]`` (the autonomy settings surface)."""
    from app.models import Workspace

    ws = db.get(Workspace, ws_id)
    settings = dict(ws.settings_json or {})
    settings["community"] = community
    ws.settings_json = settings
    db.flush()
    return ws_id


def _mk_interaction(db, ws_id: str, *, created_at: datetime,
                    platform: str = "youtube") -> str:
    from app.models.community import SocialInteraction

    row = SocialInteraction(
        workspace_id=ws_id,
        platform=platform,
        account_id="acc-1",
        remote_id=f"r-{uuid.uuid4().hex[:12]}",
        text="nice video",
        status="unread",
        created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row.id


def _mk_entry(db, ws_id: str, *, hour: int, platform: str = "youtube",
              status: str = "PENDING") -> str:
    from app.models import ScheduleEntry

    entry = ScheduleEntry(
        workspace_id=ws_id,
        platform=platform,
        run_at=datetime(2026, 9, 10, hour, 0, 0),
        status=status,
    )
    db.add(entry)
    db.flush()
    return entry.id


def _mk_post(db, ws_id: str, *, hour: int, views: int) -> str:
    """One metric-backed published post at a controlled hour (measured data)."""
    from app.models import PostMetric, PublishedPost

    published_at = datetime(2026, 9, 1, hour, 5, 0)
    post = PublishedPost(
        workspace_id=ws_id,
        video_id=f"vid-{uuid.uuid4().hex[:10]}",
        platform="youtube",
        published_at=published_at,
        is_mock=True,
    )
    db.add(post)
    db.flush()
    db.add(PostMetric(post_id=post.id, views=views, likes=1, captured_at=published_at))
    db.flush()
    return post.id


def _recommend(ws: str, platform: str | None = None) -> dict:
    from app.engine.agents.scheduler import SchedulerAgent

    return SchedulerAgent.recommend_response_windows(ws, platform=platform)


# ---------------------------------------------------------------------------
# honesty: no measured data → measured=False, heuristic labels only
# ---------------------------------------------------------------------------


def test_fresh_workspace_never_claims_measured_data(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    out = _recommend(ws)

    assert out["measured"] is False
    assert out["audience_activity"] == {"available": False, "hours": []}
    assert out["activity"] is False
    assert out["notes"], "honest notes are required on the fallback path"
    assert any("heuristic fallback" in n for n in out["notes"])
    assert out["activity_policy"] == "recommendation_only"  # default, no opt-in
    assert 0 < len(out["items"]) <= 5
    for item in out["items"]:
        assert 0 <= item["hour"] <= 23
        assert item["reason"], item
        assert item["sources"], item
        assert not any("measured" in src for src in item["sources"]), item
    assert out["avoid_hours"] == []
    json.dumps(out)  # JSON-serializable


def test_missing_workspace_returns_honest_zeroed_answer(db_session):
    out = _recommend("no-such-workspace-id")

    assert out["items"] == []
    assert out["measured"] is False
    assert out["audience_activity"] == {"available": False, "hours": []}
    assert out["avoid_hours"] == []
    assert out["activity_policy"] == "recommendation_only"
    assert out["notes"] and "not found" in out["notes"][0]
    json.dumps(out)


def test_requires_a_workspace(db_session):
    with pytest.raises(ValueError):
        _recommend("")
    with pytest.raises(ValueError):
        _recommend("   ")


# ---------------------------------------------------------------------------
# opt-in: schedule_automation True (strictly) → action_allowed
# ---------------------------------------------------------------------------


def test_schedule_automation_opt_in_flips_activity_policy(db_session,
                                                          workspace_with_user):
    ws = workspace_with_user["workspace"]
    assert _recommend(ws)["activity_policy"] == "recommendation_only"

    _set_settings(db_session, ws, schedule_automation=True)
    db_session.commit()
    assert _recommend(ws)["activity_policy"] == "action_allowed"

    # non-boolean / falsy values never widen autonomy
    for value in (False, "true", 1, None):
        _set_settings(db_session, ws, schedule_automation=value)
        db_session.commit()
        assert _recommend(ws)["activity_policy"] == "recommendation_only", value


# ---------------------------------------------------------------------------
# caps: live autonomy config + safety upload cap
# ---------------------------------------------------------------------------


def test_caps_come_from_live_autonomy_and_safety(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    _set_community(db_session, ws, mode="LOW_RISK_AUTO", daily_cap=7,
                   rate_per_10min=3, cooldown_seconds=120)
    _set_settings(db_session, ws, safety={"max_uploads_per_hour": 2})
    db_session.commit()

    caps = _recommend(ws)["caps"]
    assert caps["daily_cap"] == 7
    assert caps["rate_per_10min"] == 3
    assert caps["cooldown_seconds"] == 120
    assert caps["max_uploads_per_hour"] == 2


# ---------------------------------------------------------------------------
# audience activity: >=10 interactions → hour buckets (UTC, platform-filtered)
# ---------------------------------------------------------------------------


def test_audience_activity_buckets_created_at_hours(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    for i in range(7):  # 7 @ 09:00 UTC
        _mk_interaction(db_session, ws, created_at=datetime(2026, 9, 1, 9, i, 0))
    for i in range(5):  # 5 @ 21:00 UTC
        _mk_interaction(db_session, ws, created_at=datetime(2026, 9, 1, 21, i, 0))
    for i in range(12):  # 12 @ 04:00 UTC on another platform
        _mk_interaction(db_session, ws, platform="tiktok",
                        created_at=datetime(2026, 9, 2, 4, i, 0))
    db_session.commit()

    out = _recommend(ws)
    assert out["audience_activity"]["available"] is True
    assert out["activity"] is True
    assert set(out["audience_activity"]["hours"]) == {4, 9, 21}

    # platform filter: only the 12 youtube rows count, hour 4 drops out
    out_yt = _recommend(ws, platform="youtube")
    assert out_yt["audience_activity"]["available"] is True
    assert set(out_yt["audience_activity"]["hours"]) == {9, 21}

    # audience hours surface as item sources when they make the top window list
    by_hour = {item["hour"]: item for item in out["items"]}
    assert "audience_activity" in by_hour[4]["sources"]
    assert "audience_activity" in by_hour[9]["sources"]


# ---------------------------------------------------------------------------
# measured=True when >=3 metric-backed posts exist (reuses _best_hours)
# ---------------------------------------------------------------------------


def test_measured_ranking_labels_measured_and_seeds(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    for views in (100, 200, 300):  # hour 10 wins on average views
        _mk_post(db_session, ws, hour=10, views=views)
    _mk_post(db_session, ws, hour=22, views=10)
    db_session.commit()

    out = _recommend(ws)
    assert out["measured"] is True
    assert not any("heuristic fallback" in n for n in out["notes"])
    assert out["items"][0]["hour"] == 10  # measured best hour ranks first
    by_hour = {item["hour"]: item for item in out["items"]}
    assert "measured_performance" in by_hour[10]["sources"]
    assert "measured_performance" in by_hour[22]["sources"]
    # _best_hours pads with generic seeds — those stay honestly labeled
    assert "platform_seed" in by_hour[18]["sources"]
    assert all(item["reason"] for item in out["items"])
    json.dumps(out)


# ---------------------------------------------------------------------------
# avoid hours: busy campaign hours sink below free ones + reason says why
# ---------------------------------------------------------------------------


def test_avoid_hours_sink_busy_windows_and_explain(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    _mk_entry(db_session, ws, hour=18)  # busy seed hour
    db_session.commit()

    out = _recommend(ws)
    assert out["avoid_hours"] == [18]
    assert [item["hour"] for item in out["items"]] == [12, 20, 18]
    busy = next(item for item in out["items"] if item["hour"] == 18)
    assert "prefer another hour" in busy["reason"]


# ---------------------------------------------------------------------------
# workspace isolation: foreign interactions/schedule never leak
# ---------------------------------------------------------------------------


def test_workspace_isolation_of_activity_and_avoid_hours(db_session,
                                                         workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)

    for i in range(12):  # ws_b activity @ 03:00 + busy hours 5 and 18
        _mk_interaction(db_session, ws_b, created_at=datetime(2026, 9, 1, 3, i, 0))
    _mk_entry(db_session, ws_b, hour=5)
    _mk_entry(db_session, ws_b, hour=18)
    for i in range(12):  # ws_a activity @ 14:00 only
        _mk_interaction(db_session, ws_a, created_at=datetime(2026, 9, 1, 14, i, 0))
    db_session.commit()

    out_a = _recommend(ws_a)
    out_b = _recommend(ws_b)

    assert out_a["audience_activity"]["hours"] == [14]
    assert out_b["audience_activity"]["hours"] == [3]
    assert out_a["avoid_hours"] == []
    assert out_b["avoid_hours"] == [5, 18]
    # hour 18 is busy ONLY in ws_b — it must not sink ws_a's windows
    assert [item["hour"] for item in out_a["items"]] == [18, 12, 20, 14]
    assert [item["hour"] for item in out_b["items"]] == [12, 20, 3, 18]
    assert 5 not in [item["hour"] for item in out_a["items"]]


# ---------------------------------------------------------------------------
# determinism: same DB state → identical, JSON-serializable output
# ---------------------------------------------------------------------------


def test_deterministic_output_across_calls(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    for i in range(12):
        _mk_interaction(db_session, ws, created_at=datetime(2026, 9, 1, 7, i, 0))
    _mk_entry(db_session, ws, hour=18)
    _mk_post(db_session, ws, hour=10, views=50)
    db_session.commit()

    first = _recommend(ws, platform="youtube")
    second = _recommend(ws, platform="youtube")

    assert first == second
    assert json.dumps(first) == json.dumps(second)
    assert [item["hour"] for item in first["items"]] == [
        item["hour"] for item in second["items"]
    ]
    assert first["avoid_hours"] == [18]
