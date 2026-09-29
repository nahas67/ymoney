"""E6 brains tests: new trend sources, competitor scan, retention, scheduler."""
from __future__ import annotations

from datetime import timedelta

import pytest

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
<author><name>Money Channel</name></author>
<entry><title>Save $500 fast with this trick?</title><yt:videoId>aaa111</yt:videoId>
<published>2026-09-18T10:00:00+00:00</published></entry>
<entry><title>Weekly market recap number 42</title><yt:videoId>bbb222</yt:videoId>
<published>2026-08-01T10:00:00+00:00</published></entry>
</feed>"""


class _Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code != 200:
            raise ValueError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def test_youtube_trending_source_mocked(monkeypatch):
    from app.providers.trends import YouTubeTrendingSource

    payload = {"items": [
        {"id": "v1", "snippet": {"title": "Big money move", "channelTitle": "C"},
         "statistics": {"viewCount": "4000000"}},
        {"id": "v2", "snippet": {"title": "Quiet update", "channelTitle": "C"},
         "statistics": {"viewCount": "1000"}},
    ]}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(json_data=payload))
    out = YouTubeTrendingSource(api_key="k").fetch(niche="money", limit=5)
    assert len(out) == 2
    assert out[0].topic == "Big money move"
    assert (out[0].velocity_hint or 0) > (out[1].velocity_hint or 0)
    assert out[0].volume_hint == pytest.approx(0.8)
    assert out[0].external_ref.endswith("v1")


def test_youtube_trending_needs_key():
    from app.providers.trends import TrendSourceError, YouTubeTrendingSource

    with pytest.raises(TrendSourceError, match="API key"):
        YouTubeTrendingSource(api_key="").fetch(niche="x", limit=3)


def test_youtube_channel_rss_parse(monkeypatch):
    from app.providers.trends import YouTubeChannelSource

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(text=RSS_FIXTURE))
    out = YouTubeChannelSource(channel_id="UC123", days=60).fetch(niche="money", limit=5)
    assert len(out) == 2
    assert out[0].topic.startswith("Save $500")
    assert out[0].source == "youtube_channel"
    assert (out[0].velocity_hint or 0) > (out[1].velocity_hint or 0)


def test_youtube_channel_needs_config():
    from app.providers.trends import TrendSourceError, YouTubeChannelSource

    with pytest.raises(TrendSourceError, match="channel_id"):
        YouTubeChannelSource(channel_id="").fetch(niche="x", limit=3)


def test_factory_builds_new_kinds(monkeypatch):
    from app.core import config as config_mod
    from app.providers.trends import create_source

    monkeypatch.setattr(config_mod.settings, "youtube_api_key", "k")
    assert create_source("youtube_trending", {}).kind == "youtube_trending"
    assert create_source("youtube_channel", {"channel_id": "UC1"}).kind == "youtube_channel"


def test_topic_hot_relevance():
    from types import SimpleNamespace

    from app.engine.decision import _pattern_relevant

    assert _pattern_relevant(SimpleNamespace(pattern_key="topic_hot_quantum"), "quantum breakthrough") is True
    assert _pattern_relevant(SimpleNamespace(pattern_key="topic_hot_quantum"), "money saving tips") is False


def test_hot_topic_words_unit():
    from types import SimpleNamespace

    from app.engine.agents.intelligence import _hot_topic_words

    perf = [
        (SimpleNamespace(title="quantum leap in chips?"), SimpleNamespace(views=5000)),
        (SimpleNamespace(title="quantum stocks rally"), SimpleNamespace(views=4000)),
        (SimpleNamespace(title="morning coffee routine"), SimpleNamespace(views=500)),
    ]
    assert "quantum" in _hot_topic_words(perf, 1000)


def _mk_ws(s, name="e6"):
    from app.models import Workspace

    ws = Workspace(name=name, slug=f"{name}-slug", niche="money")
    s.add(ws)
    s.flush()
    return ws.id


def test_competitor_scan_alerts_uncovered(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.agents import competitor as comp_mod
    from app.models import TrendSource
    from app.providers.trends import TrendCandidate
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        ws_id = _mk_ws(s)
        s.add(TrendSource(workspace_id=ws_id, kind="youtube_channel",
                          name="rival", enabled=True,
                          config_json={"channel_id": "UC1"}))
        s.flush()

    cands = [TrendCandidate(topic="Secret fee banks hide?", source="youtube_channel",
                            external_ref="https://youtube.com/watch?v=x",
                            raw={"channel": "Rival"}, velocity_hint=0.9)]
    monkeypatch.setattr(comp_mod, "create_source", lambda kind, cfg: type("S", (), {
        "fetch": lambda self, niche="", limit=10: cands})())
    ctx = JobContext(job_id="j1", type="test", workspace_id=ws_id,
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = comp_mod.CompetitorAnalystAgent().scan(ctx)
    assert out["new"] == 1 and len(out["alerts"]) == 1
    assert out["alerts"][0]["topic"].startswith("Secret fee")


def test_fatigue_decays_stale_pattern(tmp_path, monkeypatch):

    from app.db import session_scope
    from app.engine.agents.intelligence import LearningAgent
    from app.models import LearningPattern, PostMetric, PublishedPost
    from app.models.base import utcnow

    monkeypatch.chdir(tmp_path)
    base = utcnow()
    with session_scope() as s:
        ws_id = _mk_ws(s, "fatigue")
        for i in range(6):  # old winners, mixed hooks
            p = PublishedPost(workspace_id=ws_id, video_id=f"v{i}", platform="youtube",
                              title=("Win? " if i % 2 == 0 else "Win ") + f"post {i}",
                              published_at=base - timedelta(days=30),
                              is_mock=True)
            s.add(p)
            s.flush()
            s.add(PostMetric(post_id=p.id, views=2000, likes=200, comments=20,
                             captured_at=base - timedelta(days=29)))
        for i in range(6, 12):  # recent flops, half question-titled
            q = "Why flop? " if i % 2 == 0 else "Flop update "
            p = PublishedPost(workspace_id=ws_id, video_id=f"v{i}", platform="youtube",
                              title=q + f"{i}", published_at=base - timedelta(hours=i),
                              is_mock=True)
            s.add(p)
            s.flush()
            s.add(PostMetric(post_id=p.id, views=100, likes=5, comments=1,
                             captured_at=base - timedelta(hours=i - 1 if i > 6 else 1)))
        s.add(LearningPattern(workspace_id=ws_id, pattern_key="hook_style_question",
                              description="question-style titles",
                              observed_improvement_pct=8.0, confidence="low",
                              sample_size=5, evidence_json={}))
        s.flush()
    assert LearningAgent()._apply_fatigue(ws_id) == 1
    with session_scope() as s:
        from sqlalchemy import select

        pat = s.scalar(select(LearningPattern).where(
            LearningPattern.workspace_id == ws_id,
            LearningPattern.pattern_key == "hook_style_question"))
        assert pat.observed_improvement_pct == pytest.approx(5.6)
        assert pat.evidence_json["fatigue"]["window"] == 10


def _mk_ready_content(s, ws_id, topic="plan me", status="APPROVED"):
    from app.models import ContentItem, Video, VideoVariant

    item = ContentItem(workspace_id=ws_id, topic=topic, status=status)
    s.add(item)
    s.flush()
    variant = VideoVariant(content_item_id=item.id, label="v1", script="word " * 60, selected=True)
    s.add(variant)
    s.flush()
    video = Video(variant_id=variant.id, workspace_id=ws_id, engine="ffmpeg_avatar",
                  status="READY", file_path="data/videos/x.mp4")
    s.add(video)
    s.flush()
    return item.id


def test_scheduler_plans_and_stays_idempotent(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.engine.agents.scheduler import SchedulerAgent
    from app.models import ScheduleEntry
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    with session_scope() as s:
        ws_id = _mk_ws(s, "sched")
        _mk_ready_content(s, ws_id, "topic one")
        _mk_ready_content(s, ws_id, "topic two")
        s.flush()
    ctx = JobContext(job_id="j1", type="test", workspace_id=ws_id,
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    out = SchedulerAgent().plan(ctx, days=2, platforms=["youtube"])
    assert len(out["created"]) == 2
    again = SchedulerAgent().plan(ctx, days=2, platforms=["youtube"])
    assert again["created"] == []
    with session_scope() as s:
        rows = s.scalars(select(ScheduleEntry).where(ScheduleEntry.workspace_id == ws_id)).all()
        assert len(rows) == 2
        assert {e.status for e in rows} == {"PENDING"}
        assert all(e.run_at > e.created_at for e in rows)


def test_brain_agents_registered():
    from app.engine.agents.registry import AGENTS
    from app.engine.capabilities import get_skill, get_tool

    assert AGENTS["competitor_analyst"].meta.title == "Competitor Analyst"
    assert AGENTS["scheduler"].meta.title == "Scheduler"
    assert get_skill("schedule_planning").required_tools == ("plan_schedule",)
    assert "schedule:write" in get_tool("plan_schedule").permissions
    assert len(AGENTS) == 23
