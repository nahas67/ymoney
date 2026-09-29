"""Work 10 Lane B: MemoryRetriever (retrieval.py).

Covers the exact contract from docs/work10_contracts.md: weight set/sum,
per-axis ranking with the full _score breakdown, freshness preference, hard
filters (time window, lifecycle, platform, type, brand, topic), workspace
isolation + limit truncation, and DISABLED/SHADOW/ASSISTED semantic gating
through the Work 05 DecisionEngine (deterministic provider only — no
network).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.engine.knowledge.normalize import topic_key_of
from app.engine.knowledge.retrieval import WEIGHTS, MemoryRetriever
from app.models.base import utcnow
from app.models.knowledge import KnowledgeMemory

SCORE_KEYS = {
    "total",
    "scope_match",
    "relevance",
    "evidence_quality",
    "freshness",
    "confidence",
    "usefulness",
}


def _seed(db, workspace_id, *, content="shared fact about budgeting", **kw) -> KnowledgeMemory:
    kw.setdefault("type", "RESEARCH_FACT")
    row = KnowledgeMemory(workspace_id=workspace_id, content=content, **kw)
    db.add(row)
    db.flush()
    # Commit each seed so the DecisionEngine's own session can write
    # its audit record without contending for the SQLite write lock.
    db.commit()
    return row


def _retrieve(db, workspace_id, **kw) -> dict:
    return MemoryRetriever().retrieve(db, workspace_id, **kw)


def _ids(result: dict) -> list[str]:
    return [item["id"] for item in result["items"]]


def _set_mode(db, workspace_id, mode: str) -> None:
    from app.models import Workspace

    row = db.get(Workspace, workspace_id)
    row.settings_json = {"intelligence": {"decision_mode": mode}}
    db.commit()


def _second_workspace(db) -> str:
    import os

    from app.models import Workspace

    ws = Workspace(name="WS B", slug=f"ws-b-{os.urandom(4).hex()}", niche="n")
    db.add(ws)
    db.flush()
    db.commit()
    return ws.id


# -- weights ----------------------------------------------------------------


def test_weights_have_exact_keys_and_sum_to_one(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    _seed(db_session, ws)
    result = _retrieve(db_session, ws)
    weights = result["ranking"]["weights"]
    assert set(weights) == {
        "scope_match", "relevance", "evidence_quality",
        "freshness", "confidence", "usefulness",
    }
    assert weights == WEIGHTS
    assert sum(weights.values()) == 1.0


# -- ranking axes ------------------------------------------------------------


def test_evidence_axis_ranks_and_score_breakdown(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    none = _seed(db_session, ws, content="identical content row")
    one = _seed(db_session, ws, content="identical content row", evidence_ids=["e1"])
    three = _seed(
        db_session, ws, content="identical content row",
        evidence_ids=["e1", "e2", "e3"],
    )
    result = _retrieve(db_session, ws)
    assert _ids(result) == [three.id, one.id, none.id]

    weights = result["ranking"]["weights"]
    for item in result["items"]:
        score = item["_score"]
        assert set(score) == SCORE_KEYS
        for key in SCORE_KEYS:
            assert 0.0 <= score[key] <= 1.0, key
        weighted = sum(weights[k] * score[k] for k in weights)
        assert abs(score["total"] - weighted) < 1e-9
    # the differing axis actually differs
    by_id = {i["id"]: i["_score"] for i in result["items"]}
    assert by_id[three.id]["evidence_quality"] == 1.0
    assert by_id[none.id]["evidence_quality"] == 0.0


def test_confidence_axis_ranks(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    low = _seed(db_session, ws, content="same content", confidence=0.1)
    high = _seed(db_session, ws, content="same content", confidence=0.9)
    result = _retrieve(db_session, ws)
    assert _ids(result) == [high.id, low.id]
    assert result["items"][1]["_score"]["confidence"] == pytest.approx(0.1)
    assert result["items"][0]["_score"]["confidence"] == pytest.approx(0.9)


# -- freshness ---------------------------------------------------------------


def test_fresh_ranks_before_stale(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    stale = _seed(db_session, ws, content="same content",
                  created_at=utcnow() - timedelta(days=45))
    fresh = _seed(db_session, ws, content="same content")
    result = _retrieve(db_session, ws)
    assert _ids(result) == [fresh.id, stale.id]
    by_id = {i["id"]: i["_score"]["freshness"] for i in result["items"]}
    assert by_id[fresh.id] == 1.0   # FRESH band
    assert by_id[stale.id] == 0.2   # STALE band


def test_conflicted_ranks_below_active(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    active = _seed(db_session, ws, content="same content", status="ACTIVE")
    conflicted = _seed(db_session, ws, content="same content", status="CONFLICTED")
    result = _retrieve(db_session, ws)
    assert _ids(result) == [active.id, conflicted.id]
    by_id = {i["id"]: i["_score"]["freshness"] for i in result["items"]}
    assert by_id[conflicted.id] == 0.15
    # both survive the hard filter (only SUPERSEDED/DISABLED die)
    assert result["metrics"]["ranked"] == 2


# -- hard filters -------------------------------------------------------------


def test_time_window_filter_reflects_in_metrics(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    old = _seed(db_session, ws, created_at=utcnow() - timedelta(days=40))
    new = _seed(db_session, ws)
    result = _retrieve(db_session, ws, time_window_days=30)
    assert _ids(result) == [new.id]
    assert result["metrics"]["considered"] == 2
    assert result["metrics"]["filtered_hard"] == 1
    assert result["metrics"]["ranked"] == 1
    assert result["metrics"]["returned"] == 1
    # None disables the time filter entirely
    wide = _retrieve(db_session, ws, time_window_days=None)
    assert wide["metrics"]["ranked"] == 2
    assert old.id in _ids(wide)


def test_superseded_and_disabled_never_returned(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    sup = _seed(db_session, ws, status="SUPERSEDED")
    dis = _seed(db_session, ws, status="DISABLED")
    act = _seed(db_session, ws, status="ACTIVE")
    unver = _seed(db_session, ws, status="UNVERIFIED")
    result = _retrieve(db_session, ws)
    assert set(_ids(result)) == {act.id, unver.id}
    assert sup.id not in _ids(result)
    assert dis.id not in _ids(result)
    assert result["metrics"]["considered"] == 4
    assert result["metrics"]["filtered_hard"] == 2


def test_platform_filter_keeps_unscoped_rows(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    yt = _seed(db_session, ws, platform="youtube")
    tk = _seed(db_session, ws, platform="tiktok")
    any_platform = _seed(db_session, ws, platform="")
    result = _retrieve(db_session, ws, platform="tiktok")
    assert set(_ids(result)) == {tk.id, any_platform.id}
    assert yt.id not in _ids(result)
    assert result["metrics"]["considered"] == 3
    assert result["metrics"]["filtered_hard"] == 1


def test_type_and_brand_filters(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    fact = _seed(db_session, ws, type="RESEARCH_FACT")  # brand-agnostic row
    src = _seed(db_session, ws, type="SOURCE", brand_id="brand-1")
    other = _seed(db_session, ws, type="CONTENT_RESULT", brand_id="brand-2")

    by_type = _retrieve(db_session, ws, types="RESEARCH_FACT")
    assert _ids(by_type) == [fact.id]

    # brand filter: unscoped (brand_id=None) rows are brand-agnostic and stay
    by_brand = _retrieve(db_session, ws, brand_id="brand-1")
    assert set(_ids(by_brand)) == {fact.id, src.id}
    assert other.id not in _ids(by_brand)

    miss = _retrieve(db_session, ws, brand_id="no-such-brand")
    assert _ids(miss) == [fact.id]
    assert src.id not in _ids(miss)
    assert miss["metrics"]["filtered_hard"] == 2


def test_topic_hard_filter_rule(db_session, workspace_with_user):
    """Exact topic_key hit OR token-overlap relevance > 0; nothing else."""
    ws = workspace_with_user["workspace"]
    exact = _seed(db_session, ws, content="unrelated text",
                  topic="budget tips", topic_key=topic_key_of("budget tips"))
    overlap = _seed(db_session, ws, content="budget tips for beginners")
    miss = _seed(db_session, ws, content="weather report")
    result = _retrieve(db_session, ws, topic="budget tips")
    assert set(_ids(result)) == {exact.id, overlap.id}
    assert miss.id not in _ids(result)
    assert result["metrics"]["filtered_hard"] == 1
    # the exact-key row wins the scope_match axis over the overlap row
    by_id = {i["id"]: i["_score"]["scope_match"] for i in result["items"]}
    assert by_id[exact.id] == 1.0
    assert by_id[overlap.id] == 0.0


# -- workspace isolation + truncation ----------------------------------------


def test_workspace_isolation_and_limit_truncation(db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _second_workspace(db_session)
    topic = "budget tips"
    for n in range(5):
        for ws in (ws_a, ws_b):
            _seed(
                db_session, ws, content=f"fact {n} about budget tips",
                topic=topic, topic_key=topic_key_of(topic),
            )
    kwargs = dict(task="budget tips", topic=topic, max_results=3)
    first = _retrieve(db_session, ws_a, **kwargs)
    second = _retrieve(db_session, ws_a, **kwargs)

    assert first["metrics"]["considered"] == 5  # only ws A rows are even considered
    assert first["metrics"]["returned"] == 3
    assert all(item["workspace_id"] == ws_a for item in first["items"])
    assert _ids(first) == _ids(second)  # deterministic across calls

    foreign = _retrieve(db_session, ws_b, **kwargs)
    assert all(item["workspace_id"] == ws_b for item in foreign["items"])
    assert set(_ids(foreign)).isdisjoint(_ids(first))


# -- semantic stage gating ----------------------------------------------------


def test_semantic_disabled_keeps_deterministic_order(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    low = _seed(db_session, ws, content="identical content row")
    high = _seed(db_session, ws, content="identical content row", evidence_ids=["a", "b", "c"])
    baseline = [high.id, low.id]  # evidence axis decides the deterministic order

    _set_mode(db_session, ws, "DISABLED")
    result = _retrieve(db_session, ws, task="anything at all")
    assert result["metrics"]["semantic"] == "disabled"
    assert _ids(result) == baseline
    # a second DISABLED call is byte-identical in order
    assert _ids(_retrieve(db_session, ws, task="anything at all")) == baseline


def test_semantic_shadow_calls_engine_but_keeps_order(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    low = _seed(db_session, ws, content="identical content row")
    high = _seed(db_session, ws, content="identical content row", evidence_ids=["a", "b", "c"])
    baseline = [high.id, low.id]

    _set_mode(db_session, ws, "SHADOW")
    result = _retrieve(db_session, ws, task="budget tips")
    assert result["metrics"]["semantic"] == "shadow"
    assert _ids(result) == baseline  # deterministic order KEPT

    # the engine was actually called: a rank decision record persisted
    from app.models.intelligence import DecisionRecordRow

    rows = db_session.scalars(
        select(DecisionRecordRow).where(
            DecisionRecordRow.workspace_id == ws,
            DecisionRecordRow.kind == "rank",
        )
    ).all()
    assert len(rows) >= 1
    assert rows[0].mode == "SHADOW"


def test_semantic_assisted_applies_without_crash(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    seeded = [
        _seed(db_session, ws, content="budget tips for creators"),
        _seed(db_session, ws, content="keto diet shortcuts"),
        _seed(db_session, ws, content="weather report today"),
    ]
    expected = {row.id for row in seeded}

    _set_mode(db_session, ws, "ASSISTED")
    result = _retrieve(db_session, ws, task="budget tips")
    assert result["metrics"]["semantic"] in ("applied", "unmappable_output")
    assert set(_ids(result)) == expected  # no crash, no lost/duplicated rows
    assert result["metrics"]["returned"] == 3


def test_retrieve_is_read_only(db_session, workspace_with_user):
    """No mark_used / mutation: use_count and rows stay untouched."""
    ws = workspace_with_user["workspace"]
    row = _seed(db_session, ws, use_count=4)
    before = (row.use_count, row.status, row.updated_at)
    _retrieve(db_session, ws, task="budget tips", max_results=5)
    db_session.expire(row)
    assert (row.use_count, row.status, row.updated_at) == before


def test_max_results_clamped(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    for n in range(60):
        _seed(db_session, ws, content=f"row {n} about budget tips")
    result = _retrieve(db_session, ws, max_results=10_000)
    assert result["metrics"]["returned"] == 50  # hard cap
    tiny = _retrieve(db_session, ws, max_results=0)
    assert tiny["metrics"]["returned"] >= 1  # floor of 1
