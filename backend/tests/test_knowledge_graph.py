"""Work 10 Lane B: knowledge-graph provider (graph.py).

Covers the contract in docs/work10_contracts.md: vocabulary validation,
node/edge idempotency, endpoint workspace checks, direction/relationship
filters in ``neighbors``, BFS depth/limit/determinism in ``subgraph``,
``edges_for`` by domain ref, and hard workspace isolation everywhere.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

from app.engine.knowledge.graph import (
    NODE_TYPES,
    RELATIONSHIPS,
    KnowledgeGraphProvider,
    node_key_of,
)
from app.engine.knowledge.normalize import topic_key_of
from app.models.knowledge import KnowledgeEdge, KnowledgeNode


@pytest.fixture()
def graph():
    return KnowledgeGraphProvider()


def _other_workspace(db_session) -> str:
    from app.models import Workspace

    ws = Workspace(name="WS B", slug=f"ws-b-{os.urandom(4).hex()}", niche="n")
    db_session.add(ws)
    db_session.flush()
    return ws.id


def _count(db, model, workspace_id) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.workspace_id == workspace_id)
        )
        or 0
    )


def _chain(db, ws, graph):
    """Content -about-> Topic -learned_from-> CreativeLesson ; Content -source-> Source."""
    content = graph.upsert_node(db, ws, node_type="Content", ref_id="c1", label="Video One")
    topic = graph.upsert_node(
        db, ws, node_type="Topic", label="Keto Diet", topic_key=topic_key_of("Keto Diet")
    )
    source = graph.upsert_node(db, ws, node_type="Source", ref_id="s1", label="Diet Blog")
    lesson = graph.upsert_node(
        db, ws, node_type="CreativeLesson", ref_id="l1", label="Hook first"
    )
    graph.link(db, ws, from_node=content, to_node=topic, relationship="CONTENT_ABOUT_TOPIC")
    graph.link(db, ws, from_node=content, to_node=source, relationship="SOURCE_REFERENCES")
    graph.link(db, ws, from_node=topic, to_node=lesson, relationship="LEARNED_FROM")
    return {"content": content, "topic": topic, "source": source, "lesson": lesson}


# -- vocabulary ------------------------------------------------------------


def test_unknown_node_type_raises(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    with pytest.raises(ValueError):
        graph.upsert_node(db_session, ws, node_type="Idea", label="x")
    with pytest.raises(ValueError):
        graph.get_node(db_session, ws, "Idea", "x")
    assert set(NODE_TYPES) >= {"Content", "Topic", "Source", "Publication"}


def test_unknown_relationship_raises(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    a = graph.upsert_node(db_session, ws, node_type="Content", ref_id="c1")
    b = graph.upsert_node(db_session, ws, node_type="Topic", label="t")
    with pytest.raises(ValueError):
        graph.link(
            db_session, ws, from_node=a, to_node=b, relationship="KNOWS"
        )
    with pytest.raises(ValueError):
        graph.neighbors(db_session, ws, node_id=a["id"], relationship="KNOWS")
    assert "CONTENT_ABOUT_TOPIC" in RELATIONSHIPS


def test_node_key_is_deterministic_and_bounded():
    long_a = "a" * 200
    assert node_key_of("", long_a) == node_key_of("", long_a)
    assert node_key_of("", long_a) != node_key_of("", "a" * 199 + "b")
    assert len(node_key_of("", long_a)) <= 120
    assert node_key_of("ref-1", "ignored") == "ref-1"
    # normalize_text: case + whitespace collapse share one key
    assert node_key_of("", "  Hello   WORLD ") == node_key_of("", "hello world")


# -- upsert / link idempotency --------------------------------------------


def test_upsert_idempotent_updates_in_place(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    first = graph.upsert_node(db_session, ws, node_type="Content", ref_id="c9", label="Old")
    second = graph.upsert_node(
        db_session, ws, node_type="Content", ref_id="c9", label="New", meta={"k": "v"}
    )
    assert first["id"] == second["id"]
    assert second["label"] == "New"
    assert second["meta"] == {"k": "v"}
    assert _count(db_session, KnowledgeNode, ws) == 1

    # label-only node (no ref_id): same normalized label -> same row, meta merges
    t1 = graph.upsert_node(db_session, ws, node_type="Topic", label="Keto Diet", meta={"a": 1})
    t2 = graph.upsert_node(db_session, ws, node_type="Topic", label="  keto   diet ", meta={"b": 2})
    assert t1["id"] == t2["id"]
    assert t2["meta"] == {"a": 1, "b": 2}
    assert _count(db_session, KnowledgeNode, ws) == 2  # c9 + keto diet only


def test_link_idempotent_one_edge(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    content = graph.upsert_node(db_session, ws, node_type="Content", ref_id="c1")
    topic = graph.upsert_node(db_session, ws, node_type="Topic", label="t1")
    e1 = graph.link(
        db_session, ws, from_node=content, to_node=topic,
        relationship="CONTENT_ABOUT_TOPIC", weight=2.0,
    )
    e2 = graph.link(
        db_session, ws, from_node=content, to_node=topic,
        relationship="CONTENT_ABOUT_TOPIC", weight=3.0, evidence_ids=["ev1"],
    )
    assert e1["id"] == e2["id"]
    assert e2["weight"] == 3.0
    assert e2["evidence_ids"] == ["ev1"]
    assert _count(db_session, KnowledgeEdge, ws) == 1

    # re-link with overlapping evidence: union, still one row
    e3 = graph.link(
        db_session, ws, from_node=content, to_node=topic,
        relationship="CONTENT_ABOUT_TOPIC", evidence_ids=["ev1", "ev2"],
    )
    assert e3["id"] == e1["id"]
    assert e3["evidence_ids"] == ["ev1", "ev2"]
    assert _count(db_session, KnowledgeEdge, ws) == 1


def test_link_missing_node_raises(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    topic = graph.upsert_node(db_session, ws, node_type="Topic", label="t")
    ghost = {"id": "00000000-0000-0000-0000-000000000000", "workspace_id": ws}
    with pytest.raises(ValueError):
        graph.link(
            db_session, ws, from_node=ghost, to_node=topic,
            relationship="CONTENT_ABOUT_TOPIC",
        )
    with pytest.raises(ValueError):
        graph.link(
            db_session, ws, from_node=topic, to_node="not-a-node",
            relationship="CONTENT_ABOUT_TOPIC",
        )


def test_link_cross_workspace_node_raises(graph, db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _other_workspace(db_session)
    node_a = graph.upsert_node(db_session, ws_a, node_type="Content", ref_id="c1")
    node_b = graph.upsert_node(db_session, ws_b, node_type="Topic", label="shared topic")
    # dict carries its home workspace: passing it under ws_b must fail
    with pytest.raises(ValueError):
        graph.link(
            db_session, ws_b, from_node=node_a, to_node=node_b,
            relationship="CONTENT_ABOUT_TOPIC",
        )
    # ...and a B dict under A likewise
    with pytest.raises(ValueError):
        graph.link(
            db_session, ws_a, from_node=node_a, to_node=node_b,
            relationship="CONTENT_ABOUT_TOPIC",
        )
    assert _count(db_session, KnowledgeEdge, ws_a) == 0


# -- neighbors --------------------------------------------------------------


def test_neighbors_directions_and_relationship_filter(
    graph, db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    nodes = _chain(db_session, ws, graph)
    content, topic, source, lesson = (
        nodes["content"], nodes["topic"], nodes["source"], nodes["lesson"]
    )

    out = graph.neighbors(
        db_session, ws, node_id=content["id"], direction="out", relationship=None
    )
    assert {e["id"] for e in out} == {topic["id"], source["id"]}
    assert all(e["relationship"] in RELATIONSHIPS for e in out)

    inn = graph.neighbors(db_session, ws, node_id=topic["id"], direction="in")
    assert [e["id"] for e in inn] == [content["id"]]
    assert inn[0]["relationship"] == "CONTENT_ABOUT_TOPIC"

    both = graph.neighbors(db_session, ws, node_id=content["id"], direction="both")
    assert {e["id"] for e in both} == {topic["id"], source["id"]}

    # topic has one outgoing edge only
    topic_out = graph.neighbors(db_session, ws, node_id=topic["id"], direction="out")
    assert [e["id"] for e in topic_out] == [lesson["id"]]

    # relationship filter narrows correctly
    only_topic = graph.neighbors(
        db_session, ws, node_id=content["id"], relationship="CONTENT_ABOUT_TOPIC"
    )
    assert [e["id"] for e in only_topic] == [topic["id"]]

    # target via (node_type, key): Topic node_key == normalized label
    via_key = graph.neighbors(
        db_session, ws, node_type="Topic", key="keto diet", direction="in"
    )
    assert [e["id"] for e in via_key] == [content["id"]]


def test_neighbors_unknown_target_is_empty(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ws_b = _other_workspace(db_session)
    nodes = _chain(db_session, ws, graph)
    # foreign node id under another workspace
    assert graph.neighbors(db_session, ws_b, node_id=nodes["content"]["id"]) == []
    # no target at all
    assert graph.neighbors(db_session, ws) == []
    assert graph.neighbors(db_session, ws, node_type="Content", key="nope") == []


# -- subgraph ---------------------------------------------------------------


def test_subgraph_depth_respected(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    nodes = _chain(db_session, ws, graph)
    content, topic, source, lesson = (
        nodes["content"], nodes["topic"], nodes["source"], nodes["lesson"]
    )

    d0 = graph.subgraph(db_session, ws, seed=content, depth=0)
    assert [n["id"] for n in d0["nodes"]] == [content["id"]]
    assert d0["edges"] == []

    d1 = graph.subgraph(db_session, ws, seed=content, depth=1)
    ids1 = {n["id"] for n in d1["nodes"]}
    assert ids1 == {content["id"], topic["id"], source["id"]}
    assert lesson["id"] not in ids1  # 2-hop node NOT included at depth 1

    d2 = graph.subgraph(db_session, ws, seed=content, depth=2)
    ids2 = {n["id"] for n in d2["nodes"]}
    assert lesson["id"] in ids2  # ...IS included at depth 2
    assert ids2 == {content["id"], topic["id"], source["id"], lesson["id"]}
    # depth-1 edges present, 2-hop edge only at depth 2
    edge_pairs1 = {(e["from_node_id"], e["to_node_id"]) for e in d1["edges"]}
    edge_pairs2 = {(e["from_node_id"], e["to_node_id"]) for e in d2["edges"]}
    assert (topic["id"], lesson["id"]) not in edge_pairs1
    assert (topic["id"], lesson["id"]) in edge_pairs2


def test_subgraph_limit_and_determinism(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    nodes = _chain(db_session, ws, graph)
    content = nodes["content"]

    limited = graph.subgraph(db_session, ws, seed=content, depth=2, limit=2)
    assert len(limited["nodes"]) == 2
    assert content["id"] in {n["id"] for n in limited["nodes"]}

    a = graph.subgraph(db_session, ws, seed=content, depth=2)
    b = graph.subgraph(db_session, ws, seed=content, depth=2)
    assert [n["id"] for n in a["nodes"]] == [n["id"] for n in b["nodes"]]
    assert [e["id"] for e in a["edges"]] == [e["id"] for e in b["edges"]]
    # nodes sorted by id ascending
    node_ids = [n["id"] for n in a["nodes"]]
    assert node_ids == sorted(node_ids)
    # every edge touches only collected nodes (no dangling endpoints)
    collected = set(node_ids)
    for edge in a["edges"]:
        assert edge["from_node_id"] in collected
        assert edge["to_node_id"] in collected


# -- edges_for --------------------------------------------------------------


def test_edges_for_ref_id(graph, db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    nodes = _chain(db_session, ws, graph)
    content = nodes["content"]
    edges = graph.edges_for(db_session, ws, ref_id="c1")
    assert len(edges) == 2
    assert all(
        e["from_node_id"] == content["id"]
        for e in edges
    )
    assert {e["relationship"] for e in edges} == {
        "CONTENT_ABOUT_TOPIC", "SOURCE_REFERENCES"
    }
    assert graph.edges_for(db_session, ws, ref_id="") == []
    assert graph.edges_for(db_session, ws, ref_id="does-not-exist") == []


def test_edges_for_excludes_foreign_workspace(graph, db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _other_workspace(db_session)
    _chain(db_session, ws_a, graph)
    # same ref_id exists in B with its own edge
    b_content = graph.upsert_node(db_session, ws_b, node_type="Content", ref_id="c1")
    b_topic = graph.upsert_node(db_session, ws_b, node_type="Topic", label="other topic")
    b_edge = graph.link(
        db_session, ws_b, from_node=b_content, to_node=b_topic,
        relationship="CONTENT_ABOUT_TOPIC",
    )
    edges_a = graph.edges_for(db_session, ws_a, ref_id="c1")
    edges_b = graph.edges_for(db_session, ws_b, ref_id="c1")
    assert len(edges_a) == 2 and len(edges_b) == 1
    assert b_edge["id"] not in {e["id"] for e in edges_a}
    assert {e["id"] for e in edges_b} == {b_edge["id"]}


# -- workspace isolation -----------------------------------------------------


def test_workspace_isolation(graph, db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _other_workspace(db_session)
    nodes = _chain(db_session, ws_a, graph)
    content = nodes["content"]

    # same logical node created independently in B gets its own id
    b_dup = graph.upsert_node(db_session, ws_b, node_type="Content", ref_id="c1")
    b_topic = graph.upsert_node(db_session, ws_b, node_type="Topic", label="other topic")
    assert b_dup["id"] != content["id"]

    # B sees its own node, never A's (distinct ids, distinct edges)
    assert graph.get_node(db_session, ws_b, "Content", "c1")["id"] == b_dup["id"]
    assert graph.get_node(db_session, ws_b, "Topic", "keto diet") is None

    # neighbors of A's node from B -> empty (id does not resolve under B);
    # B's own c1 node has no edges yet -> also empty, but via B's rows only
    assert graph.neighbors(db_session, ws_b, node_id=content["id"]) == []
    assert graph.neighbors(db_session, ws_b, node_type="Content", key="c1") == []
    assert len(graph.neighbors(db_session, ws_a, node_id=content["id"])) == 2

    # A's edges are not reachable from B
    assert graph.edges_for(db_session, ws_b, ref_id="s1") == []

    # subgraph with an A seed under B raises (consistent with link)
    with pytest.raises(ValueError):
        graph.subgraph(db_session, ws_b, seed=content, depth=2)

    # subgraph from B's own node sees only B's nodes
    graph.link(
        db_session, ws_b, from_node=b_dup, to_node=b_topic,
        relationship="CONTENT_ABOUT_TOPIC",
    )
    sub = graph.subgraph(db_session, ws_b, seed=b_dup, depth=2)
    assert {n["workspace_id"] for n in sub["nodes"]} == {ws_b}
