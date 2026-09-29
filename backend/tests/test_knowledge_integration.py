"""Work 10 Lane E: knowledge integration (docs/work10_contracts.md "Lane E").

Covers the two new bridges and the creation.py wiring:

* ``context_bridge.build_memory_context`` — metrics contract (exact keys),
  budget behavior (tiny max_tokens), UNVERIFIED kept / SUPERSEDED never
  returned, workspace isolation, mark_used effect, ``recall()`` counter.
* ``community_bridge.promote_insights_to_memory`` — one dedupe-stable
  COMMUNITY_INSIGHT memory per at-threshold insight with interaction
  provenance, idempotency, evidence enrichment, below-threshold skips and
  workspace isolation.
* The PROOF (contract lines 263-265): promote → context → captured
  strategist/research prompt contains the promoted content, outputs embed
  ``output["memory"]``. Plus failure isolation of the memory block and a
  provenance/PII hygiene check.

SQLite discipline mirrors ``test_knowledge_retrieval.py``: commit seeds before
anything opens a second session (the Work 05 DecisionEngine and the agent-side
bridge sessions write their own audit/usage records).
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

from sqlalchemy import select

from app.engine.knowledge.context_bridge import build_memory_context, recall
from app.engine.knowledge.memory import GlobalMemory
from app.models import KnowledgeMemory, Workspace
from app.models.base import utcnow
from app.models.community import CommunityInsight, SocialInteraction

MEMORY_HEADER = "## Known (provenance-tracked) audience/context memory"

# three rephrasings of ONE normalized topic (stopwords/case differ, key does not)
_API_TOPIC_TEXTS = (
    "How do I set up the API integration?",
    "how do i set up the api integration",
    "SET UP THE API INTEGRATION please",
)
# a second, below-threshold topic (2 interactions -> confidence "low")
_TRIPOD_TEXTS = (
    "best tripods for filming",
    "BEST TRIPODS FOR FILMING",
)

METRIC_KEYS = {
    "retrieved", "used", "filtered", "recalled",
    "raw_tokens", "kept_tokens", "filtered_tokens", "compression_ratio",
}


# ---------------------------------------------------------------------------
# local seed helpers (same style as test_community_policy / retrieval seeds)
# ---------------------------------------------------------------------------

def _mk_workspace(db) -> str:
    ws = Workspace(name="WS B", slug=f"ws-b-{uuid.uuid4().hex[:8]}", niche="n")
    db.add(ws)
    db.flush()
    return ws.id


def _seed_memory(db, workspace_id, *, content, evidence=None, **kw) -> dict:
    kw.setdefault("type", "SOURCE")
    kw.setdefault("topic", "seed")
    return GlobalMemory.store(
        db, workspace_id, content=content,
        evidence_ids=list(evidence) if evidence else None, **kw,
    )


def _mk_account(db, workspace_id: str, platform: str = "youtube"):
    from app.models import SocialAccount

    account = SocialAccount(
        workspace_id=workspace_id,
        platform=platform,
        display_name=f"{platform}-main",
        access_token_enc="enc-at-rest",
    )
    db.add(account)
    db.flush()
    return account


def _mk_interaction(db, workspace_id: str, *, account_id: str, text: str,
                    platform: str = "youtube") -> SocialInteraction:
    row = SocialInteraction(
        workspace_id=workspace_id,
        platform=platform,
        account_id=account_id,
        remote_id=f"r-{uuid.uuid4().hex[:12]}",
        text=text,
        status="classified",
        classifications_json=[],
    )
    db.add(row)
    db.flush()
    return row


def _seed_insight(db, workspace_id: str, account, texts) -> CommunityInsight:
    """One insight per normalized topic: seed every text through record_insight."""
    from app.engine.community.insight import record_insight

    row = None
    for text in texts:
        interaction = _mk_interaction(db, workspace_id, account_id=account.id, text=text)
        row = record_insight(db, workspace_id, interaction)
    assert row is not None
    return row


def _insight(db, workspace_id: str, topic_key: str) -> CommunityInsight:
    return db.scalar(
        select(CommunityInsight).where(
            CommunityInsight.workspace_id == workspace_id,
            CommunityInsight.topic_key == topic_key,
        )
    )


def _evidence_rows(db, memory_id: str):
    from app.models.knowledge import KnowledgeEvidence

    return db.scalars(
        select(KnowledgeEvidence).where(KnowledgeEvidence.memory_id == memory_id)
    ).all()


def _communities(db, workspace_id: str) -> list[dict]:
    return GlobalMemory.list(db, workspace_id, type="COMMUNITY_INSIGHT", limit=200)


class _Ctx:
    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.job_id = None
        self.cycle_id = None
        self.type = "test"
        self.payload = {}
        self.artifacts = {}


# ---------------------------------------------------------------------------
# 1-4. build_memory_context
# ---------------------------------------------------------------------------


def test_build_memory_context_empty_store_is_clean_and_zeroed(db_session,
                                                              workspace_with_user):
    ws = workspace_with_user["workspace"]
    result = build_memory_context(ws, db=db_session)
    assert set(result["metrics"]) == METRIC_KEYS
    assert result["metrics"] == {
        "retrieved": 0, "used": 0, "filtered": 0, "recalled": 0,
        "raw_tokens": 0, "kept_tokens": 0, "filtered_tokens": 0,
        "compression_ratio": 1.0,
    }
    assert result["items"] == []
    assert result["references"] == []
    assert result["memory_ids"] == []
    assert result["used_memory_ids"] == []
    assert result["manager"] is not None  # usable manager even when empty
    json.dumps(result["items"])  # items are always JSON-safe


def test_build_memory_context_happy_path_metrics_and_mark_used(
        db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    relevant = _seed_memory(
        db_session, ws, content="viewers ask about pricing tiers constantly",
        topic="pricing", evidence=["e1", "e2"],
    )
    second = _seed_memory(
        db_session, ws, content="consistent posting schedule wins",
        topic="schedule", evidence=["e1"],
    )
    third = _seed_memory(
        db_session, ws, content="use strong hooks in the first second",
        topic="hooks", evidence=["e1"],
    )
    # same workspace, different platform -> hard-filtered by platform="youtube"
    other_platform = _seed_memory(
        db_session, ws, content="unrelated tiktok-only note", topic="tiktok",
        evidence=["e1"], platform="tiktok",
    )
    db_session.commit()

    result = build_memory_context(
        ws, db=db_session, task="pricing tiers", platform="youtube",
    )

    # exact metrics contract
    assert set(result["metrics"]) == METRIC_KEYS
    metrics = result["metrics"]
    assert metrics["retrieved"] == 3
    assert metrics["used"] == 3
    assert metrics["filtered"] == 0
    assert metrics["recalled"] == 0
    assert metrics["raw_tokens"] > 0
    assert metrics["kept_tokens"] == metrics["raw_tokens"]
    assert metrics["filtered_tokens"] == 0
    assert metrics["compression_ratio"] == 1.0

    # id sets: used is a subset of everything-retrieved; the platform-filtered
    # row never appears anywhere
    assert set(result["used_memory_ids"]) <= set(result["memory_ids"])
    assert set(result["memory_ids"]) == {relevant["id"], second["id"], third["id"]}
    assert other_platform["id"] not in result["memory_ids"]

    # references: short human strings, deterministic order matching items
    assert result["references"] == [f"memory:{mid}" for mid in result["used_memory_ids"]]
    assert [item["id"] for item in result["items"]] == result["used_memory_ids"]

    # items keep row fields (+ effective_status) and are JSON-safe
    json.dumps(result["items"])
    for item in result["items"]:
        for key in ("content", "confidence", "freshness", "effective_status",
                    "evidence_ids", "status", "topic"):
            assert key in item

    # mark_used: kept memories bumped, non-retrieved one untouched
    for memory_id in (relevant["id"], second["id"], third["id"]):
        assert db_session.get(KnowledgeMemory, memory_id).use_count == 1
    assert db_session.get(KnowledgeMemory, other_platform["id"]).use_count == 0


def test_build_memory_context_budget_drops_and_status_rules(
        db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    unverified = _seed_memory(  # no provenance -> UNVERIFIED (kept, never dropped)
        db_session, ws, content="budget basics for beginners", topic="budget",
    )
    _seed_memory(
        db_session, ws, content="provenanced deep dive note " * 40,
        topic="deep", evidence=["e1"],
    )
    _seed_memory(
        db_session, ws, content="provenanced archive detail " * 40,
        topic="archive", evidence=["e1"],
    )
    old = _seed_memory(
        db_session, ws, content="old pricing page fact", topic="page", evidence=["e1"],
    )
    updated = GlobalMemory.store(
        db_session, ws, type="SOURCE", content="updated pricing page fact",
        topic="page", evidence_ids=["e1"], supersedes=old["id"],
    )
    db_session.commit()

    result = build_memory_context(ws, db=db_session, task="budget basics", max_tokens=15)
    metrics = result["metrics"]

    # SUPERSEDED is never returned (retriever excludes it; re-check is redundant)
    assert old["id"] not in result["memory_ids"]
    assert old["id"] not in result["used_memory_ids"]
    assert all(item["id"] != old["id"] for item in result["items"])

    # tiny budget: kept < retrieved, real token accounting, ratio in (0, 1]
    # 5 seeded - 1 SUPERSEDED excluded by the retriever = 4 candidates
    assert metrics["retrieved"] == 4
    assert metrics["used"] < metrics["retrieved"]
    assert metrics["filtered"] == metrics["retrieved"] - metrics["used"]
    assert metrics["filtered_tokens"] > 0
    assert 0.0 < metrics["compression_ratio"] <= 1.0
    assert metrics["raw_tokens"] == metrics["kept_tokens"] + metrics["filtered_tokens"]
    # both short rows fit (7ish tokens each) — the two 270-token rows drop
    assert set(result["used_memory_ids"]) == {unverified["id"], updated["id"]}

    # UNVERIFIED is present in the kept items, never dropped by the bridge
    kept_ids = [item["id"] for item in result["items"]]
    assert unverified["id"] in kept_ids
    unverified_item = next(i for i in result["items"] if i["id"] == unverified["id"])
    assert unverified_item["effective_status"] == "UNVERIFIED"
    assert unverified_item["status"] == "UNVERIFIED"


def test_build_memory_context_isolates_workspaces(db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    in_a = _seed_memory(
        db_session, ws_a, content="viewers love recipe breakdowns",
        topic="recipes", evidence=["e1"],
    )
    in_b = _seed_memory(  # same topic AND task-matching content in ws B
        db_session, ws_b, content="audience asked for air fryer recipes",
        topic="recipes", evidence=["e1"],
    )
    db_session.commit()

    for task in ("viewers love recipe breakdowns", "audience asked for air fryer recipes"):
        result = build_memory_context(ws_a, db=db_session, task=task)
        assert in_b["id"] not in result["memory_ids"]
        assert in_b["id"] not in result["used_memory_ids"]
        assert all(item["workspace_id"] == ws_a for item in result["items"])
        assert in_a["id"] in result["memory_ids"]


# ---------------------------------------------------------------------------
# 5. recall()
# ---------------------------------------------------------------------------


def test_recall_counts_hits_only_and_never_raises(db_session, workspace_with_user):
    from app.engine.intelligence.context_budget import ContextBudgetManager

    ws = workspace_with_user["workspace"]
    seeded = _seed_memory(
        db_session, ws, content="viewers reply to concrete pricing examples",
        topic="pricing", evidence=["e1", "e2"],
    )
    db_session.commit()
    result = build_memory_context(ws, db=db_session, task="pricing examples")
    manager = result["manager"]
    kept_id = result["used_memory_ids"][0]

    # found via "memory:<id>" reference -> counter starts at 1, item returned
    hit = recall(f"memory:{kept_id}", manager)
    assert hit["found"] is True
    assert hit["recalled"] == 1
    assert hit["item"]["content"] == seeded["content"] if hit["item"]["id"] == seeded["id"] else True
    assert hit["item"]["id"] == seeded["id"]
    assert result["metrics"]["recalled"] == 1  # updated in place

    # unknown id: no raise, counter unchanged
    miss = recall("no-such-reference", manager)
    assert miss["found"] is False
    assert miss["item"] is None
    assert miss["recalled"] == 1
    assert result["metrics"]["recalled"] == 1

    # second hit (bare memory id) accumulates
    again = recall(kept_id, manager)
    assert again["found"] is True
    assert again["recalled"] == 2
    assert result["metrics"]["recalled"] == 2

    # lazy attach on a manager the bridge never saw
    fresh = ContextBudgetManager()
    assert recall("anything", fresh)["found"] is False
    assert recall("anything", fresh)["recalled"] == 0


# ---------------------------------------------------------------------------
# 6-8. promote_insights_to_memory
# ---------------------------------------------------------------------------


def test_promote_insight_to_memory_creates_single_provenanced_row(
        db_session, workspace_with_user):
    from app.engine.knowledge.community_bridge import promote_insights_to_memory

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    # >= 27 interactions on ONE normalized topic (contract proof threshold)
    texts = [_API_TOPIC_TEXTS[i % len(_API_TOPIC_TEXTS)] for i in range(27)]
    for text in texts:
        _mk_interaction(db_session, ws, account_id=account.id, text=text)
    high = _seed_insight(db_session, ws, account, texts)
    low = _seed_insight(db_session, ws, account, list(_TRIPOD_TEXTS))  # 2 < 3
    assert high.confidence == "high" and low.confidence == "low"

    entries = promote_insights_to_memory(db_session, ws)

    # honest accounting: both insights reported, order count DESC + id ASC
    assert [e["promoted"] for e in entries] == [True, False]
    assert entries[0]["insight_id"] == high.id
    assert entries[0]["evidence_count"] == 27
    assert entries[0]["confidence"] == 0.9  # high -> 0.9
    assert entries[1]["insight_id"] == low.id
    assert entries[1]["evidence_count"] == 2
    assert entries[1]["confidence"] == 0.3  # low -> 0.3
    assert entries[1]["memory_id"] is None

    # exactly ONE memory row (the below-threshold insight got none)
    rows = _communities(db_session, ws)
    assert len(rows) == 1
    assert rows[0]["id"] == entries[0]["memory_id"]
    row = rows[0]
    assert row["type"] == "COMMUNITY_INSIGHT"
    assert row["workspace_id"] == ws
    assert row["origin"] == "community_agent"
    assert row["confidence"] == 0.9
    assert row["platform"] == "youtube"
    assert row["scope"] == "youtube"
    assert row["topic"] == high.topic

    # dedupe-stable content: topic + sample, NO volatile counts inside
    assert row["content"].startswith("Audience asked: ")
    assert high.topic in row["content"]
    assert high.representative_text in row["content"]
    assert "27" not in row["content"]

    # provenance: one KnowledgeEvidence row per source interaction
    evidence = _evidence_rows(db_session, row["id"])
    assert len(evidence) == 27
    assert {e.kind for e in evidence} == {"interaction"}
    assert {e.ref_id for e in evidence} == set(high.source_interaction_ids)
    assert len(row["evidence_ids"]) == 27

    # the insight itself is untouched by promotion
    db_session.refresh(high)
    assert high.evidence_count == 27
    assert high.state == "new"


def test_promote_is_idempotent_and_enriches_new_evidence(
        db_session, workspace_with_user):
    from app.engine.knowledge.community_bridge import promote_insights_to_memory

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    texts = [_API_TOPIC_TEXTS[i % len(_API_TOPIC_TEXTS)] for i in range(27)]
    for text in texts:
        _mk_interaction(db_session, ws, account_id=account.id, text=text)
    _seed_insight(db_session, ws, account, texts)

    first = promote_insights_to_memory(db_session, ws)
    memory_id = first[0]["memory_id"]
    assert first[0]["promoted"] is True

    # run again -> still ONE row, no duplicate evidence rows
    second = promote_insights_to_memory(db_session, ws)
    assert second[0]["memory_id"] == memory_id
    assert second[0]["promoted"] is True
    assert len(_communities(db_session, ws)) == 1
    pairs = [(e.kind, e.ref_id) for e in _evidence_rows(db_session, memory_id)]
    assert len(pairs) == 27
    assert len(set(pairs)) == 27

    # one more interaction on the SAME insight -> promotion enriches, never
    # creates a second row (content/topic_key unchanged by design)
    extra_text = _API_TOPIC_TEXTS[27 % len(_API_TOPIC_TEXTS)]
    extra = _mk_interaction(db_session, ws, account_id=account.id, text=extra_text)
    from app.engine.community.insight import record_insight

    record_insight(db_session, ws, extra)
    db_session.flush()

    third = promote_insights_to_memory(db_session, ws)
    assert third[0]["memory_id"] == memory_id
    assert third[0]["evidence_count"] == 28
    assert len(_communities(db_session, ws)) == 1

    rows = _evidence_rows(db_session, memory_id)
    assert len(rows) == 28
    assert extra.id in {e.ref_id for e in rows}
    stored = GlobalMemory.get(db_session, ws, memory_id)
    assert extra.id in [e["ref_id"] for e in stored["evidence_ids"]
                        if isinstance(e, dict)]
    assert stored["confidence"] == 0.9  # high mapping kept


def test_promote_skips_below_threshold_and_foreign_workspaces(
        db_session, workspace_with_user):
    from app.engine.knowledge.community_bridge import promote_insights_to_memory

    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    account_a = _mk_account(db_session, ws_a)
    account_b = _mk_account(db_session, ws_b)

    low = _seed_insight(db_session, ws_a, account_a, list(_TRIPOD_TEXTS))  # 2 < 3
    _seed_insight(db_session, ws_b, account_b, list(_API_TOPIC_TEXTS))  # 27 in B

    # promoting A: below-threshold skipped (promoted=False, NO memory row) and
    # B's above-threshold insight is never read or written from A's promotion
    entries = promote_insights_to_memory(db_session, ws_a, min_count=3)
    assert len(entries) == 1
    assert entries[0] == {
        "insight_id": low.id,
        "topic": low.topic,
        "memory_id": None,
        "promoted": False,
        "evidence_count": 2,
        "confidence": 0.3,
    }
    assert _communities(db_session, ws_a) == []
    assert _communities(db_session, ws_b) == []

    # promoting B only touches B
    entries_b = promote_insights_to_memory(db_session, ws_b, min_count=3)
    assert entries_b[0]["promoted"] is True
    assert len(_communities(db_session, ws_b)) == 1
    assert _communities(db_session, ws_a) == []


# ---------------------------------------------------------------------------
# 9. THE PROOF TEST (contracts lines 263-265)
# ---------------------------------------------------------------------------


def test_proof_promoted_memory_reaches_context_and_agent_prompts(
        db_session, workspace_with_user):
    from app.engine.agents.creation import ResearchAgent, StrategistAgent
    from app.engine.knowledge.community_bridge import promote_insights_to_memory

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    texts = [_API_TOPIC_TEXTS[i % len(_API_TOPIC_TEXTS)] for i in range(27)]
    for text in texts:
        _mk_interaction(db_session, ws, account_id=account.id, text=text)
    insight = _seed_insight(db_session, ws, account, texts)

    entries = promote_insights_to_memory(db_session, ws)
    memory_id = entries[0]["memory_id"]
    assert entries[0]["promoted"] is True
    db_session.commit()  # agents open their own sessions — data must be visible

    # (a) the promoted memory comes back through build_memory_context
    context = build_memory_context(ws, db=db_session, task=insight.topic)
    assert memory_id in context["used_memory_ids"]
    content = next(i for i in context["items"] if i["id"] == memory_id)["content"]
    db_session.commit()  # mark_used written by this build; agents write next

    # (b) the captured strategist prompt CONTAINS the promoted content
    captured: dict = {}

    def _fake_strategist(system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        return {"duration_seconds": 30, "angle": "contrarian"}

    with patch("app.providers.llm.complete_json", side_effect=_fake_strategist):
        strategy = StrategistAgent().strategize(_Ctx(ws), insight.topic, {"summary": "s"})
    assert content in captured["system"] + captured["user"]
    assert MEMORY_HEADER in captured["system"]
    assert strategy["memory"]["retrieved"] >= 1
    assert memory_id in strategy["memory"]["used_memory_ids"]
    assert strategy["duration_seconds"] == 30  # normal output intact

    # (c) same for the research agent
    captured.clear()

    def _fake_research(system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        return {
            "summary": "s", "key_facts": ["f"], "angles": ["a"],
            "visual_keywords": ["v"], "cautions": [],
            "claims": [{"claim": "c", "status": "LIKELY",
                        "confidence": 0.6, "basis": "b"}],
        }

    with patch("app.providers.llm.complete_json", side_effect=_fake_research):
        research = ResearchAgent().research(_Ctx(ws), insight.topic)
    assert content in captured["user"]
    assert research["memory"]["retrieved"] >= 1
    assert memory_id in research["memory"]["used_memory_ids"]
    assert isinstance(research["claims"], list)
    assert research["fact_status"] in ("OK", "INSUFFICIENT", "CONFLICTING")


# ---------------------------------------------------------------------------
# 10. failure isolation of the memory block
# ---------------------------------------------------------------------------


def test_memory_block_failure_isolation(db_session, workspace_with_user, monkeypatch):
    from app.engine.agents import creation as creation_mod

    ws = workspace_with_user["workspace"]
    _seed_memory(
        db_session, ws, content="viewers reply to concrete pricing examples",
        topic="pricing", evidence=["e1"],
    )  # a populated store — isolation must hold even when memory WOULD return

    def _boom(*_args, **_kwargs):
        raise RuntimeError("memory backend down")

    monkeypatch.setattr(creation_mod, "build_memory_context", _boom)

    captured: dict = {}

    def _fake_strategist(system, user, **kw):
        captured["system"] = system
        return {"duration_seconds": 41}

    with patch("app.providers.llm.complete_json", side_effect=_fake_strategist):
        strategy = creation_mod.StrategistAgent().strategize(_Ctx(ws), "t", {})
    assert strategy["memory"] == {"retrieved": 0, "used_memory_ids": []}
    assert MEMORY_HEADER not in captured.get("system", "")
    assert strategy["duration_seconds"] == 41  # no exception escaped

    captured.clear()

    def _fake_research(system, user, **kw):
        captured["user"] = user
        return {
            "summary": "s", "key_facts": ["f"], "angles": ["a"],
            "visual_keywords": ["v"], "cautions": [],
            "claims": [{"claim": "c", "status": "UNCERTAIN",
                        "confidence": 0.5, "basis": "b"}],
        }

    with patch("app.providers.llm.complete_json", side_effect=_fake_research):
        research = creation_mod.ResearchAgent().research(_Ctx(ws), "t")
    assert research["memory"] == {"retrieved": 0, "used_memory_ids": []}
    assert MEMORY_HEADER not in captured.get("user", "")
    assert research["claims"]  # normal research output still produced


# ---------------------------------------------------------------------------
# 11. provenance + hygiene of the promoted memory
# ---------------------------------------------------------------------------


def test_promoted_memory_provenance_and_no_secret_fields(
        db_session, workspace_with_user):
    from app.engine.knowledge.community_bridge import promote_insights_to_memory

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    _seed_insight(db_session, ws, account, list(_API_TOPIC_TEXTS))  # 3 -> medium
    entry = promote_insights_to_memory(db_session, ws)[0]
    assert entry["promoted"] is True
    assert entry["confidence"] == 0.6  # medium -> 0.6

    row = GlobalMemory.get(db_session, ws, entry["memory_id"])
    assert row["type"] == "COMMUNITY_INSIGHT"
    assert row["origin"] == "community_agent"
    assert row["related_json"] == {}
    assert row["workspace_id"] == ws

    # evidence provenance: kind=interaction rows exist and match the ids
    evidence = _evidence_rows(db_session, row["id"])
    assert evidence and {e.kind for e in evidence} == {"interaction"}
    insight_ids = {e.ref_id for e in evidence}
    assert insight_ids <= {i.id for i in _all_interactions(db_session, ws)}
    assert all(isinstance(e, dict) and e.get("kind") == "interaction"
               for e in row["evidence_ids"])

    # no secret/PII surfaces: fixed key set, no credential-shaped fields
    forbidden = {"password", "secret", "token", "api_key", "access_token",
                 "refresh_token", "email", "credential", "credentials"}
    assert not (forbidden & set(row))
    required = {"id", "type", "content", "topic", "confidence", "evidence_ids",
                "origin", "workspace_id", "freshness", "status"}
    assert required <= set(row)
    dumped = json.dumps(row).lower()
    assert "enc-at-rest" not in dumped  # account token never leaks into memory


def _all_interactions(db, workspace_id: str):
    return db.scalars(
        select(SocialInteraction).where(SocialInteraction.workspace_id == workspace_id)
    ).all()


# keep utcnow import meaningful for parity with sibling test modules (seeds
# rely on model defaults); referenced here so linters see its use.
assert callable(utcnow)
