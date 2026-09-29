"""Work 10 Lane A — GlobalMemory service: provenance, dedupe, conflicts,
supersession, freshness, workspace isolation and JSON-safe reads.

Every test runs against the shared in-test DB through the conftest fixtures
(``db_session`` / ``workspace_with_user``) and hard-scopes queries to its own
workspace, so row counts stay deterministic across the suite. No commits are
expected inside the service: the session fixture commits at teardown.
"""
from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.engine.knowledge.freshness import (
    ACTIVE,
    AGING,
    CONFLICTED,
    DISABLED,
    FRESH,
    STALE,
    SUPERSEDED,
    UNVERIFIED,
)
from app.engine.knowledge.memory import TYPES, GlobalMemory
from app.engine.knowledge.normalize import content_hash_of, topic_key_of
from app.models import KnowledgeEvidence, KnowledgeMemory, Workspace
from app.models.base import utcnow

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk_workspace(db) -> str:
    ws = Workspace(name="Memory WS", slug=f"mem-{uuid.uuid4().hex[:8]}", niche="money")
    db.add(ws)
    db.flush()
    return ws.id


def _count(db, workspace_id) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(KnowledgeMemory)
            .where(KnowledgeMemory.workspace_id == workspace_id)
        )
        or 0
    )


def _evidence(db, memory_id):
    return db.scalars(
        select(KnowledgeEvidence)
        .where(KnowledgeEvidence.memory_id == memory_id)
        .order_by(KnowledgeEvidence.kind, KnowledgeEvidence.ref_id)
    ).all()


def _craft(
    db,
    workspace_id,
    *,
    days_ago,
    content="crafted fact",
    topic="crafted topic",
    type="RESEARCH_FACT",
    status=ACTIVE,
) -> str:
    """Seed a row with a backdated created_at (direct attribute set + flush)."""
    row = KnowledgeMemory(
        workspace_id=workspace_id,
        type=type,
        content=content,
        topic=topic,
        topic_key=topic_key_of(topic),
        content_hash=content_hash_of(content),
        status=status,
        freshness=FRESH,
        created_at=utcnow() - timedelta(days=days_ago),
        source_ids=["src"],
        evidence_ids=["ev"],
    )
    db.add(row)
    db.flush()
    return row.id


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_store_rejects_unknown_type_and_empty_content(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    with pytest.raises(ValueError) as exc:
        GlobalMemory.store(db_session, ws, type="NOT_A_TYPE", content="x")
    message = str(exc.value)
    assert all(known in message for known in TYPES)  # message lists TYPES
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            GlobalMemory.store(db_session, ws, type="SOURCE", content=bad)
    assert _count(db_session, ws) == 0


def test_store_signature_has_no_secrets_surface():
    params = inspect.signature(GlobalMemory.store).parameters
    assert "related_json" not in params  # service-owned, never accepted
    forbidden = ("token", "secret", "password", "access_token", "credential", "oauth")
    assert not [name for name in params if any(f in name for f in forbidden)]


# ---------------------------------------------------------------------------
# provenance gate + evidence rows
# ---------------------------------------------------------------------------


def test_store_without_provenance_is_unverified(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d = GlobalMemory.store(
        db_session, ws, type="AUDIENCE_INSIGHT", content="Teens watch at night",
        topic="audience",
    )
    assert d["status"] == UNVERIFIED
    assert d["effective_status"] == UNVERIFIED
    assert d["topic_key"] == topic_key_of("audience")
    assert d["content_hash"] == content_hash_of("Teens watch at night")
    assert d["related_json"] == {}


def test_store_with_evidence_is_active_and_fresh(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d = GlobalMemory.store(
        db_session, ws, type="COMMUNITY_INSIGHT", content="Users want uploads",
        topic="uploads", evidence_ids=["ev-1"],
    )
    assert d["status"] == ACTIVE
    assert d["freshness"] == FRESH
    assert d["effective_status"] == FRESH


def test_store_source_and_evidence_rows(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="Shorts outperform longs",
        topic="format", source_ids=["s1", "s2"],
        evidence_ids=[
            "plain-1",
            {"kind": "metric", "ref_id": "m1", "detail": "ctr 5%", "confidence": 0.9},
            {"kind": "not_a_kind", "ref_id": "b1"},  # unknown kind → evidence_record
            "plain-1",  # duplicate inside one call → skipped
        ],
    )
    rows = _evidence(db_session, d["id"])
    pairs = {(r.kind, r.ref_id) for r in rows}
    assert ("source_document", "s1") in pairs
    assert ("source_document", "s2") in pairs
    assert ("evidence_record", "plain-1") in pairs
    assert ("metric", "m1") in pairs
    assert ("evidence_record", "b1") in pairs  # unknown kind fell back
    assert len(rows) == 5  # duplicates skipped, no IntegrityError reliance
    src = next(r for r in rows if r.ref_id == "s1")
    assert src.source_id == "s1" and src.captured_at is not None
    # denormalized mirror on the memory row
    assert d["source_ids"] == ["s1", "s2"]
    assert "plain-1" in d["evidence_ids"]
    # provenance present → ACTIVE (not UNVERIFIED)
    assert d["status"] == ACTIVE


def test_non_list_provenance_inputs_treated_as_empty(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d = GlobalMemory.store(
        db_session, ws, type="SOURCE", content="odd inputs",
        source_ids="not-a-list", evidence_ids={"kind": "metric"},
    )
    assert d["source_ids"] == [] and d["evidence_ids"] == []
    assert d["status"] == UNVERIFIED
    assert _evidence(db_session, d["id"]) == []


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


def test_verify_promotes_unverified_and_appends_user_evidence(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    user_id = workspace_with_user["user"]
    d = GlobalMemory.store(db_session, ws, type="ENTITY", content="Acme is a client")
    assert d["status"] == UNVERIFIED

    v = GlobalMemory.verify(db_session, ws, d["id"], user_id=user_id)
    assert v["status"] == ACTIVE
    assert v["effective_status"] == FRESH
    assert v["last_verified_at"] is not None

    users = [r for r in _evidence(db_session, d["id"]) if r.kind == "user"]
    assert len(users) == 1 and users[0].ref_id == user_id

    # repeat verify: anchor refreshes, no duplicate user evidence
    again = GlobalMemory.verify(db_session, ws, d["id"], user_id=user_id)
    assert again["status"] == ACTIVE
    users = [r for r in _evidence(db_session, d["id"]) if r.kind == "user"]
    assert len(users) == 1


def test_verify_keeps_conflicted_superseded_disabled(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    target = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="old claim", topic="claim",
        evidence_ids=["e1"],
    )
    GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="rival claim", topic="other",
        conflicts_with=target["id"],
    )
    superseded = GlobalMemory.store(
        db_session, ws, type="BRAND_KNOWLEDGE", content="stale brand rule",
        topic="brand", evidence_ids=["e1"],
    )
    replacement = GlobalMemory.store(
        db_session, ws, type="BRAND_KNOWLEDGE", content="fresh brand rule",
        topic="brand", evidence_ids=["e1"], supersedes=superseded["id"],
    )
    assert replacement["id"] != superseded["id"]
    disabled = GlobalMemory.store(
        db_session, ws, type="PLATFORM_LEARNING", content="tiktok quirk",
        topic="tiktok", evidence_ids=["e1"],
    )
    GlobalMemory.disable(db_session, ws, disabled["id"])

    checks = ((target, CONFLICTED), (superseded, SUPERSEDED), (disabled, DISABLED))
    for memory, expected in checks:
        v = GlobalMemory.verify(db_session, ws, memory["id"], user_id="u1")
        assert v["status"] == expected, f"{expected} must survive verify()"
        assert v["last_verified_at"] is not None


# ---------------------------------------------------------------------------
# dedupe / idempotency
# ---------------------------------------------------------------------------


def test_store_is_idempotent_same_content(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    first = GlobalMemory.store(
        db_session, ws, type="PLATFORM_LEARNING", content="TikTok favors 9:16",
        topic="tiktok", evidence_ids=["e1"],
    )
    second = GlobalMemory.store(
        db_session, ws, type="PLATFORM_LEARNING", content="TikTok favors 9:16",
        topic="tiktok", evidence_ids=["e1"],
    )
    assert first["id"] == second["id"]
    assert _count(db_session, ws) == 1
    assert len(_evidence(db_session, first["id"])) == 1  # no duplicate evidence


def test_store_different_content_new_row(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    a = GlobalMemory.store(
        db_session, ws, type="CREATIVE_LESSON", content="Open on motion",
        topic="hooks", evidence_ids=["e1"],
    )
    b = GlobalMemory.store(
        db_session, ws, type="CREATIVE_LESSON", content="Open on a question",
        topic="hooks", evidence_ids=["e1"],
    )
    assert a["id"] != b["id"]
    assert _count(db_session, ws) == 2


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


def test_explicit_conflict_marks_both_rows_and_preserves_content(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    original = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="The sky is blue",
        topic="sky", evidence_ids=["e1"],
    )
    rival = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="The sky is green",
        topic="haze", conflicts_with=original["id"],
    )  # no provenance: CONFLICTED must still beat UNVERIFIED
    assert rival["status"] == CONFLICTED

    left = GlobalMemory.get(db_session, ws, original["id"])
    assert left is not None
    assert left["status"] == CONFLICTED
    assert left["conflict_group"] == rival["conflict_group"] == f"cg_{original['id']}"
    assert left["content"] == "The sky is blue"
    assert rival["content"] == "The sky is green"
    assert _count(db_session, ws) == 2  # neither row deleted/overwritten


def test_explicit_conflict_rejects_missing_and_superseded_targets(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    with pytest.raises(ValueError):
        GlobalMemory.store(
            db_session, ws, type="RESEARCH_FACT", content="rival",
            conflicts_with="does-not-exist",
        )
    old = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="superseded fact",
        topic="t", evidence_ids=["e1"],
    )
    replacement = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="replacement fact",
        topic="t", evidence_ids=["e1"], supersedes=old["id"],
    )
    # supersession is replacement, not a rivalry: the successor stays clean
    assert replacement["status"] == ACTIVE
    with pytest.raises(ValueError):
        GlobalMemory.store(
            db_session, ws, type="RESEARCH_FACT", content="rival of dead fact",
            conflicts_with=old["id"],
        )
    assert GlobalMemory.get(db_session, ws, old["id"])["status"] == SUPERSEDED


def test_auto_conflict_same_topic_different_content(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    x = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="Fact one", topic="gravity",
        evidence_ids=["e1"],
    )
    y = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="Fact two", topic="gravity",
        evidence_ids=["e1"],
    )
    x2 = GlobalMemory.get(db_session, ws, x["id"])
    y2 = GlobalMemory.get(db_session, ws, y["id"])
    assert x2["status"] == y2["status"] == CONFLICTED
    assert x2["conflict_group"] and x2["conflict_group"] == y2["conflict_group"]
    assert x2["content"] == "Fact one" and y2["content"] == "Fact two"
    assert _count(db_session, ws) == 2

    # same content again → dedupe returns the first row, still one conflict pair
    again = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="Fact one", topic="gravity",
        evidence_ids=["e1"],
    )
    assert again["id"] == x["id"]
    assert _count(db_session, ws) == 2


def test_auto_conflict_not_triggered_for_other_cases(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    # different topics never rival each other
    a = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="alpha", topic="one",
        evidence_ids=["e1"],
    )
    b = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="beta", topic="two",
        evidence_ids=["e1"],
    )
    # non-FACT types never auto-conflict, even on the same topic
    c = GlobalMemory.store(
        db_session, ws, type="CREATIVE_LESSON", content="lesson one",
        topic="shared", evidence_ids=["e1"],
    )
    d = GlobalMemory.store(
        db_session, ws, type="CREATIVE_LESSON", content="lesson two",
        topic="shared", evidence_ids=["e1"],
    )
    # empty topic_key never auto-conflicts
    e = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="topic-less one",
        evidence_ids=["e1"],
    )
    f = GlobalMemory.store(
        db_session, ws, type="RESEARCH_FACT", content="topic-less two",
        evidence_ids=["e1"],
    )
    for memory in (a, b, c, d, e, f):
        row = GlobalMemory.get(db_session, ws, memory["id"])
        assert row["status"] == ACTIVE, f"{memory['content']} must not conflict"
    assert _count(db_session, ws) == 6


# ---------------------------------------------------------------------------
# supersession + disable
# ---------------------------------------------------------------------------


def test_supersede_marks_target_and_allows_reinsert(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    old = GlobalMemory.store(
        db_session, ws, type="BRAND_KNOWLEDGE", content="Old brand claim",
        topic="brand", evidence_ids=["e1"],
    )
    new = GlobalMemory.store(
        db_session, ws, type="BRAND_KNOWLEDGE", content="New brand claim",
        topic="brand", evidence_ids=["e1"], supersedes=old["id"],
    )
    target = GlobalMemory.get(db_session, ws, old["id"])
    assert target["status"] == SUPERSEDED
    assert target["superseded_by"] == new["id"]
    assert target["content"] == "Old brand claim"  # content intact
    assert target["effective_status"] == SUPERSEDED
    assert new["status"] == ACTIVE

    # list still shows the superseded row
    listed = GlobalMemory.list(db_session, ws, type="BRAND_KNOWLEDGE")
    assert [r["id"] for r in listed].count(old["id"]) == 1

    # dedupe skips SUPERSEDED rows → re-storing old content creates a NEW row
    again = GlobalMemory.store(
        db_session, ws, type="BRAND_KNOWLEDGE", content="Old brand claim",
        topic="brand", evidence_ids=["e1"],
    )
    assert again["id"] not in (old["id"], new["id"])
    assert _count(db_session, ws) == 3


def test_supersede_validates_targets(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    a = GlobalMemory.store(
        db_session, ws, type="ENTITY", content="entity a", evidence_ids=["e1"]
    )
    b = GlobalMemory.store(
        db_session, ws, type="ENTITY", content="entity b", evidence_ids=["e1"]
    )
    with pytest.raises(ValueError):
        GlobalMemory.supersede(db_session, ws, "missing", replacement_id=b["id"])
    with pytest.raises(ValueError):
        GlobalMemory.supersede(db_session, ws, a["id"], replacement_id="missing")
    with pytest.raises(ValueError):
        GlobalMemory.supersede(db_session, ws, a["id"], replacement_id=a["id"])
    GlobalMemory.supersede(db_session, ws, a["id"], replacement_id=b["id"])
    with pytest.raises(ValueError):
        GlobalMemory.supersede(db_session, ws, a["id"], replacement_id=b["id"])


def test_disable_is_idempotent_and_never_deletes(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d = GlobalMemory.store(
        db_session, ws, type="CONTENT_RESULT", content="works well", evidence_ids=["e1"]
    )
    out = GlobalMemory.disable(db_session, ws, d["id"])
    assert out["status"] == DISABLED and out["effective_status"] == DISABLED
    again = GlobalMemory.disable(db_session, ws, d["id"])
    assert again["status"] == DISABLED  # idempotent
    assert GlobalMemory.get(db_session, ws, d["id"]) is not None  # never deleted
    assert _count(db_session, ws) == 1

    # SUPERSEDED rows stay SUPERSEDED
    old = GlobalMemory.store(
        db_session, ws, type="ENTITY", content="old entity", evidence_ids=["e1"]
    )
    GlobalMemory.store(
        db_session, ws, type="ENTITY", content="new entity", evidence_ids=["e1"],
        supersedes=old["id"],
    )
    assert GlobalMemory.disable(db_session, ws, old["id"])["status"] == SUPERSEDED


# ---------------------------------------------------------------------------
# workspace isolation + mark_used
# ---------------------------------------------------------------------------


def test_workspace_isolation_reads_and_mutations(db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    d = GlobalMemory.store(
        db_session, ws_a, type="AUDIENCE_INSIGHT", content="Gen Z prefers shorts",
        topic="audience", evidence_ids=["e1"],
    )

    assert GlobalMemory.get(db_session, ws_b, d["id"]) is None
    assert GlobalMemory.list(db_session, ws_b) == []
    assert (
        GlobalMemory.list(db_session, ws_b, type="AUDIENCE_INSIGHT", topic="audience")
        == []
    )

    GlobalMemory.mark_used(db_session, ws_b, [d["id"]])
    assert GlobalMemory.get(db_session, ws_a, d["id"])["use_count"] == 0

    with pytest.raises(ValueError):
        GlobalMemory.verify(db_session, ws_b, d["id"], user_id="u")
    with pytest.raises(ValueError):
        GlobalMemory.disable(db_session, ws_b, d["id"])
    with pytest.raises(ValueError):
        GlobalMemory.supersede(db_session, ws_b, d["id"], replacement_id=d["id"])
    # the foreign attempts left workspace A untouched
    assert GlobalMemory.get(db_session, ws_a, d["id"])["status"] == ACTIVE


def test_mark_used_only_touches_own_workspace(db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    mine = GlobalMemory.store(
        db_session, ws_a, type="EXPERIMENT_RESULT", content="A/B won",
        topic="ab", evidence_ids=["e1"],
    )
    foreign = GlobalMemory.store(
        db_session, ws_b, type="EXPERIMENT_RESULT", content="other ws result",
        topic="ab", evidence_ids=["e1"],
    )

    GlobalMemory.mark_used(db_session, ws_a, [mine["id"], foreign["id"], "missing-id"])
    assert GlobalMemory.get(db_session, ws_a, mine["id"])["use_count"] == 1
    assert GlobalMemory.get(db_session, ws_a, mine["id"])["last_used_at"] is not None
    assert GlobalMemory.get(db_session, ws_b, foreign["id"])["use_count"] == 0

    # foreign workspace cannot bump our counter
    GlobalMemory.mark_used(db_session, ws_b, [mine["id"]])
    assert GlobalMemory.get(db_session, ws_a, mine["id"])["use_count"] == 1


# ---------------------------------------------------------------------------
# freshness / effective status
# ---------------------------------------------------------------------------


def test_freshness_bands_from_created_at(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    for days, expected in ((1, FRESH), (10, AGING), (40, STALE)):
        memory_id = _craft(db_session, ws, days_ago=days, content=f"aged {days}")
        row = GlobalMemory.get(db_session, ws, memory_id)
        assert row["freshness"] == expected
        assert row["effective_status"] == expected


def test_verify_resets_age_anchor(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    memory_id = _craft(db_session, ws, days_ago=40, content="stale until verified")
    assert GlobalMemory.get(db_session, ws, memory_id)["effective_status"] == STALE
    verified = GlobalMemory.verify(db_session, ws, memory_id, user_id="u1")
    assert verified["freshness"] == FRESH  # age anchors on last_verified_at
    assert verified["effective_status"] == FRESH


def test_effective_status_lifecycle_beats_age(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    conflicted = _craft(db_session, ws, days_ago=40, content="old conflict",
                        status=CONFLICTED)
    unverified = _craft(db_session, ws, days_ago=40, content="old unverified",
                        status=UNVERIFIED)
    disabled = _craft(db_session, ws, days_ago=40, content="old disabled",
                      status=DISABLED)
    assert GlobalMemory.get(db_session, ws, conflicted)["effective_status"] == CONFLICTED
    assert GlobalMemory.get(db_session, ws, unverified)["effective_status"] == UNVERIFIED
    assert GlobalMemory.get(db_session, ws, disabled)["effective_status"] == DISABLED
    # and verify() on a conflicted row never erases the conflict
    GlobalMemory.verify(db_session, ws, conflicted, user_id="u1")
    assert GlobalMemory.get(db_session, ws, conflicted)["effective_status"] == CONFLICTED


# ---------------------------------------------------------------------------
# list(): filters, ordering, limit clamp
# ---------------------------------------------------------------------------


def test_list_filters_order_and_limit_clamp(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    unverified = GlobalMemory.store(
        db_session, ws, type="SOURCE", content="Alpha webhook pricing guide",
        topic="pricing",
    )
    active = GlobalMemory.store(
        db_session, ws, type="SOURCE", content="Beta pricing report",
        topic="pricing", evidence_ids=["e1"],
    )
    GlobalMemory.store(
        db_session, ws, type="SOURCE", content="conversion 1000 views",
        topic="stats", evidence_ids=["e1"],
    )
    GlobalMemory.store(
        db_session, ws, type="SOURCE", content="conversion 100% up",
        topic="stats", evidence_ids=["e1"],
    )

    # case-insensitive substring over content + topic
    assert [r["id"] for r in GlobalMemory.list(db_session, ws, q="ALPHA")] == [
        unverified["id"]
    ]
    assert [r["id"] for r in GlobalMemory.list(db_session, ws, q="pricing")] == [
        active["id"],
        unverified["id"],
    ]
    # LIKE wildcards are escaped: "100%" must not match "1000"
    hits = GlobalMemory.list(db_session, ws, q="100%")
    assert len(hits) == 1 and "100%" in hits[0]["content"]

    # status: lifecycle column for lifecycle values, computed band for bands
    assert [r["id"] for r in GlobalMemory.list(db_session, ws, status=UNVERIFIED)] == [
        unverified["id"]
    ]
    assert active["id"] in [r["id"] for r in GlobalMemory.list(db_session, ws, status=FRESH)]
    assert unverified["id"] not in [
        r["id"] for r in GlobalMemory.list(db_session, ws, status=FRESH)
    ]
    assert GlobalMemory.list(db_session, ws, status="NOPE") == []

    # type / topic (normalized) / unknown-type filters
    assert len(GlobalMemory.list(db_session, ws, type="SOURCE")) == 4
    assert GlobalMemory.list(db_session, ws, type="ENTITY") == []
    assert len(GlobalMemory.list(db_session, ws, topic="Pricing")) == 2

    # deterministic order: created_at DESC (id ASC tie-break)
    rows = GlobalMemory.list(db_session, ws, limit=50)
    times = [datetime.fromisoformat(r["created_at"].rstrip("Z")) for r in rows]
    assert times == sorted(times, reverse=True)

    # limit clamp: 1..200
    for i in range(205):
        _craft(db_session, ws, days_ago=0, content=f"bulk fact {i}",
               topic=f"bulk {i % 3}")
    assert len(GlobalMemory.list(db_session, ws, limit=0)) == 1
    assert len(GlobalMemory.list(db_session, ws, limit=10_000)) == 200


# ---------------------------------------------------------------------------
# JSON safety
# ---------------------------------------------------------------------------


def test_all_rows_are_json_serializable(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d = GlobalMemory.store(
        db_session, ws, type="USER_APPROVED_KNOWLEDGE", content="Approved claim",
        topic="approval", source_ids=["s1"],
        evidence_ids=[{"kind": "research_claim", "ref_id": "c1", "detail": "ok"}],
    )
    fetched = GlobalMemory.get(db_session, ws, d["id"])
    json.dumps(fetched)
    rows = GlobalMemory.list(db_session, ws)
    json.dumps(rows)
    for row in rows:
        assert set(("freshness", "effective_status", "status")) <= set(row)
        json.dumps(row)
