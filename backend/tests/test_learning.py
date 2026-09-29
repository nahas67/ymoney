"""Work 06 Lane C: scoped lessons + creation influence.

Lessons are LearningPattern rows extended via evidence_json (no schema
change); writes go only through LearningAgent._upsert_pattern /
_remember_pattern. Scope matching forbids cross-platform/topic
generalization by construction; lessons never bypass QC/budgets/approvals.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.engine.performance import learning as lessons
from app.models import LearningPattern, Workspace
from app.models.base import utcnow


def _obs(metric="retention", scope=None, effect=None, evidence=None, sample=6, **kw):
    obs = {
        "metric": metric,
        "scope": scope or {"platform": "tiktok", "topic": "finance"},
        "effect": effect if effect is not None else {
            "direction": "positive", "improvement_pct": 18.0,
            "recommendation": "Open with the payoff number.",
            "kinds": ["strategy", "hooks"],
        },
        "evidence_ids": evidence if evidence is not None else ["vid-a", "vid-b"],
        "sample_size": sample,
    }
    obs.update(kw)
    return obs


def _enable(db_session, ws):
    row = db_session.get(Workspace, ws)
    row.settings_json = {**(row.settings_json or {}), "learning_assist": True}
    db_session.commit()


def _lesson_rows(db_session, ws):
    db_session.expire_all()
    return db_session.scalars(
        select(LearningPattern).where(LearningPattern.workspace_id == ws)
    ).all()


def _ctx(ws="ws-test"):
    from app.services.jobs import JobContext

    return JobContext(job_id="j1", type="test", workspace_id=ws,
                      cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)


# -- evidence bar -------------------------------------------------------------

class TestEvidenceBar:
    def test_rejects_small_sample(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        res = lessons.generate_lessons(db_session, ws, [_obs(sample=4)])
        assert res["stored"] == 0 and len(res["rejected"]) == 1
        assert _lesson_rows(db_session, ws) == []

    def test_rejects_single_evidence_id(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        res = lessons.generate_lessons(db_session, ws, [_obs(sample=8, evidence=["only-one"])])
        assert res["stored"] == 0 and len(res["rejected"]) == 1
        assert _lesson_rows(db_session, ws) == []

    def test_rejects_missing_metric(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        res = lessons.generate_lessons(db_session, ws, [_obs(metric="")])
        assert res["stored"] == 0 and len(res["rejected"]) == 1

    def test_accepts_boundary_sample(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        res = lessons.generate_lessons(db_session, ws, [_obs(sample=5)])
        assert res["stored"] == 1
        (row,) = _lesson_rows(db_session, ws)
        assert row.confidence == "low"
        assert lessons.is_lesson_row(row)

    def test_confidence_scales_with_sample(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        assert lessons.confidence_for(5) == "low"
        assert lessons.confidence_for(10) == "medium"
        assert lessons.confidence_for(30) == "high"
        assert lessons.confidence_for(30, consistency=0.2) == "medium"
        res = lessons.generate_lessons(db_session, ws, [_obs(sample=30)])
        assert res["stored"] == 1
        (row,) = _lesson_rows(db_session, ws)
        assert row.confidence == "high"


# -- scope isolation / no generalization --------------------------------------

class TestScope:
    def test_tiktok_lesson_never_matches_youtube(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        assert lessons.generate_lessons(db_session, ws, [_obs()])["stored"] == 1
        same = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"},
            {"topic": "x"}, "strategy")
        assert same["applied_lessons"] != []
        other = lessons.apply_lessons(
            db_session, ws, {"platform": "youtube", "topic": "finance"},
            {"topic": "x"}, "strategy")
        assert other["applied_lessons"] == []
        assert "lesson_recommendations" not in other

    def test_no_topic_generalization(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs()])
        other = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "fitness"},
            {"topic": "x"}, "strategy")
        assert other["applied_lessons"] == []

    def test_unset_lesson_dims_are_wildcards(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(
            db_session, ws, [_obs(scope={"platform": "tiktok"})])
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "anything"},
            {"topic": "x"}, "strategy")
        assert out["applied_lessons"] != []

    def test_scope_matches_unit(self):
        assert lessons.scope_matches({"platform": "tiktok"}, {"platform": "tiktok", "topic": "z"})
        assert not lessons.scope_matches({"platform": "tiktok"}, {"platform": "youtube"})
        assert not lessons.scope_matches({"platform": "tiktok"}, {"topic": "z"})
        assert not lessons.scope_matches(
            {"platform": "tiktok", "topic": "a"}, {"platform": "tiktok", "topic": "b"})


# -- freshness, conflict, supersede -------------------------------------------

class TestFreshness:
    def _backdate(self, db_session, ws, days):
        row = _lesson_rows(db_session, ws)[0]
        ev = dict(row.evidence_json or {})
        stamp = (utcnow() - timedelta(days=days)).isoformat() + "Z"
        ev["last_validated_at"] = stamp
        ev["created_at"] = stamp
        row.evidence_json = ev
        db_session.commit()

    def test_stale_lesson_not_applied(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs()])
        self._backdate(db_session, ws, 100)
        (row,) = _lesson_rows(db_session, ws)
        assert lessons.effective_status(row) == "stale"
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, {"t": 1}, "strategy")
        assert out["applied_lessons"] == []

    def test_aging_lesson_still_applies(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs()])
        self._backdate(db_session, ws, 45)
        (row,) = _lesson_rows(db_session, ws)
        assert lessons.effective_status(row) == "aging"
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, {"t": 1}, "strategy")
        assert out["applied_lessons"] != []
        assert out["lesson_recommendations"][0]["freshness"] == "aging"

    def test_revalidation_refreshes(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        lessons.generate_lessons(db_session, ws, [_obs()])
        self._backdate(db_session, ws, 45)
        res = lessons.generate_lessons(
            db_session, ws, [_obs(evidence=["vid-c", "vid-d"])])
        assert res["revalidated"] == 1
        rows = _lesson_rows(db_session, ws)
        assert len(rows) == 1
        assert lessons.effective_status(rows[0]) == "fresh"
        assert sorted(rows[0].evidence_json["evidence_ids"]) == ["vid-a", "vid-b", "vid-c", "vid-d"]

    def test_supersede_preserves_history(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs(sample=6)])
        bad = _obs(sample=12, effect={"direction": "negative", "improvement_pct": -9.0,
                                      "recommendation": "Avoid payoff-first opens.",
                                      "kinds": ["strategy"]})
        res = lessons.generate_lessons(db_session, ws, [bad])
        assert res["superseded"] == 1
        rows = _lesson_rows(db_session, ws)
        assert len(rows) == 2  # old row preserved, never deleted
        old = next(r for r in rows if r.evidence_json.get("generation", 1) == 1)
        new = next(r for r in rows if r.evidence_json.get("generation") == 2)
        assert old.active is False
        assert old.evidence_json["status"] == "superseded"
        assert old.evidence_json["superseded_by"] == new.pattern_key
        assert new.evidence_json["supersedes"] == old.pattern_key
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, {"t": 1}, "strategy")
        assert out["applied_lessons"] == [new.pattern_key]

    def test_conflict_weakening(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs(sample=20)])
        (before,) = _lesson_rows(db_session, ws)
        assert before.confidence == "medium"
        bad = _obs(sample=8, evidence=["vid-x", "vid-y"],
                   effect={"direction": "negative", "improvement_pct": -5.0,
                           "recommendation": "Doubt payoff-first.", "kinds": ["strategy"]})
        res = lessons.generate_lessons(db_session, ws, [bad])
        assert res["weakened"] == 1
        rows = _lesson_rows(db_session, ws)
        assert len(rows) == 1
        assert rows[0].confidence == "low"  # downgraded one level
        assert rows[0].evidence_json["status"] == "aging"
        assert len(rows[0].evidence_json["conflicts"]) == 1
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, {"t": 1}, "strategy")
        assert out["applied_lessons"] == [rows[0].pattern_key]


# -- influence -----------------------------------------------------------------

class TestInfluence:
    def test_apply_copies_and_audits(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs()])
        artifact = {"topic": "x", "angle": "keep me"}
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, artifact, "strategy")
        assert "applied_lessons" not in artifact  # input copy untouched
        assert len(out["applied_lessons"]) == 1
        assert out["lesson_recommendations"][0]["recommendation"] == "Open with the payoff number."
        assert out["angle"] == "keep me"

    def test_unknown_kind_raises(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        with pytest.raises(ValueError):
            lessons.apply_lessons(db_session, ws, {}, {}, "nope")

    def test_disabled_by_default(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        lessons.generate_lessons(db_session, ws, [_obs()])
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, {"t": 1}, "strategy")
        assert "applied_lessons" not in out  # byte-identical copy when flag is off

    def test_protected_keys_preserved(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs()])
        artifact = {"min_qc_score": 80, "max_videos_per_day": 3, "status": "APPROVED",
                    "duration_seconds": 32, "budget_usd": 5.0, "topic": "x"}
        out = lessons.apply_lessons(
            db_session, ws, {"platform": "tiktok", "topic": "finance"}, artifact, "strategy")
        for key, val in artifact.items():
            if key in lessons.PROTECTED_KEYS:
                assert out[key] == val
        assert out["applied_lessons"] != []

    def test_hook_rank_unchanged_without_scope(self):
        from app.engine.agents.creation import HookOptimizerAgent

        out = HookOptimizerAgent().rank_hooks(_ctx(), [{"script": "What if 3 banks fail this week?"}])
        assert out[0]["predicted_score"] == 86.0
        assert "applied_lessons" not in out[0]

    def test_hook_bonus_with_scope_and_flag(self, db_session, workspace_with_user):
        from app.engine.agents.creation import HookOptimizerAgent

        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs(
            scope={"platform": "tiktok"},
            effect={"direction": "positive", "improvement_pct": 20.0,
                    "recommendation": "Ask first.", "kinds": ["hooks"]})])
        ctx = _ctx(ws)
        variants = [{"script": "What if 3 banks fail this week?"}]
        plain = HookOptimizerAgent().rank_hooks(ctx, [dict(v) for v in variants])
        assert plain[0]["predicted_score"] == 86.0
        boosted = HookOptimizerAgent().rank_hooks(
            ctx, [dict(v) for v in variants], scope={"platform": "tiktok"})
        assert boosted[0]["predicted_score"] == 88.0  # deterministic +2.0 prior
        assert boosted[0]["applied_lessons"] != []

    def test_schedule_prior_keeps_same_hours(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        _enable(db_session, ws)
        lessons.generate_lessons(db_session, ws, [_obs(
            scope={"platform": "youtube"},
            effect={"direction": "positive", "improvement_pct": 12.0,
                    "recommendation": "Evenings win.", "kinds": ["schedule"],
                    "preferred_hours": [20, 18]})])
        ranked, keys, _recs = lessons.schedule_hour_prior(ws, [18, 12, 20], ["youtube"])
        assert sorted(ranked) == [12, 18, 20]  # re-rank only; caps/idempotency untouched
        assert ranked[0] == 20
        assert keys != []

    def test_script_guidance_disabled_identical(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        strategy = {"hook_type": "question", "duration_seconds": 32}
        out, keys = lessons.script_guidance(ws, "finance", strategy)
        assert out == strategy and keys == []

    def test_broll_boost_disabled_identical(self, db_session, workspace_with_user):
        ws = workspace_with_user["workspace"]
        kws, keys, _recs = lessons.broll_keyword_boost(ws, "finance", ["budget"])
        assert kws == ["budget"] and keys == []


# -- isolation ------------------------------------------------------------------

def test_workspace_isolation(db_session, workspace_with_user):
    from app.models import User, WorkspaceMember

    ws_a = workspace_with_user["workspace"]
    _enable(db_session, ws_a)
    lessons.generate_lessons(db_session, ws_a, [_obs()])
    user = User(email=f"iso-{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
    ws_b = Workspace(name="B", slug=f"ws-{uuid.uuid4().hex[:8]}")
    db_session.add_all([user, ws_b])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws_b.id, user_id=user.id, role="owner"))
    db_session.commit()
    _enable(db_session, ws_b.id)
    out = lessons.apply_lessons(
        db_session, ws_b.id, {"platform": "tiktok", "topic": "finance"}, {"t": 1}, "strategy")
    assert out["applied_lessons"] == []


# -- HTTP surface ----------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"lc{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return (data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"})


def _seed_lesson(ws_id):
    from app.db import session_scope

    with session_scope() as s:
        res = lessons.generate_lessons(s, ws_id, [_obs()])
        assert res["stored"] == 1


def test_lessons_routes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    _seed_lesson(ws_id)
    r = client.get(f"/api/v1/workspaces/{ws_id}/lessons", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["scope"]["platform"] == "tiktok"
    assert items[0]["status"] == "fresh"
    lesson_id = items[0]["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/lessons?platform=youtube", headers=headers)
    assert r.status_code == 200 and r.json()["items"] == []

    r = client.get(f"/api/v1/workspaces/{ws_id}/lessons/{lesson_id}/evidence", headers=headers)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["evidence_ids"]) == ["vid-a", "vid-b"]

    r = client.post(f"/api/v1/workspaces/{ws_id}/lessons/{lesson_id}/disable", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    r = client.get(f"/api/v1/workspaces/{ws_id}/lessons?status=fresh", headers=headers)
    assert r.json()["items"] == []


def test_lessons_routes_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    _ws_b, headers_b = _register(client)
    _seed_lesson(ws_a)
    (lesson_id,) = [i["id"] for i in
                    client.get(f"/api/v1/workspaces/{ws_a}/lessons", headers=headers_a).json()["items"]]
    # Cross-workspace access is denied by the standard workspace gate (403:
    # authenticated non-member), same as campaigns and other v1 routers.
    r = client.get(f"/api/v1/workspaces/{ws_a}/lessons", headers=headers_b)
    assert r.status_code == 403
    r = client.get(f"/api/v1/workspaces/{ws_a}/lessons/{lesson_id}/evidence", headers=headers_b)
    assert r.status_code in (403, 404)
    r = client.post(f"/api/v1/workspaces/{ws_a}/lessons/{lesson_id}/disable", headers=headers_b)
    assert r.status_code in (403, 404)
