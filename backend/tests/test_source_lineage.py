"""Work 10 Lane F: source lineage (engine/knowledge/lineage.py).

Covers the contract in docs/work10_contracts.md "Lane F": honest bundle
building (workspace-scoped, no silent drops, deterministic facts only),
ContentItem.research_json contract untouched + knowledge-graph edges through
KnowledgeGraphProvider, MediaAsset path-safety/dedupe/checksum, and the
wrapped timeline_from_video — with cross-workspace isolation everywhere.

Sessions: services flush only; tests that need a fresh-session re-read call
``db_session.commit()`` first (the fixture itself commits at teardown), so
assertions never read rows through the same identity map that wrote them.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.engine.knowledge.graph import KnowledgeGraphProvider
from app.engine.knowledge.lineage import (
    asset_from_document,
    build_research_bundle,
    create_content_from_bundle,
    timeline_from_asset,
)
from app.engine.knowledge.normalize import topic_key_of
from app.models import ContentItem, SourceConnector, SourceDocument, Workspace
from app.models.assets import MediaAsset
from app.models.knowledge import KnowledgeNode

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _other_workspace(db) -> str:
    ws = Workspace(name="WS B", slug=f"ws-b-{uuid.uuid4().hex[:8]}", niche="n")
    db.add(ws)
    db.flush()
    return ws.id


def _seed_doc(db, workspace_id, *, title="Doc", content="", state="active",
              asset_reference="", checksum="", mime_type="text/plain") -> str:
    connector = SourceConnector(
        workspace_id=workspace_id, kind="mock", name=f"c-{uuid.uuid4().hex[:10]}"
    )
    db.add(connector)
    db.flush()
    doc = SourceDocument(
        workspace_id=workspace_id,
        connector_id=connector.id,
        remote_id=f"r-{uuid.uuid4().hex[:10]}",
        title=title,
        content=content,
        state=state,
        asset_reference=asset_reference,
        checksum=checksum,
        mime_type=mime_type,
    )
    db.add(doc)
    db.flush()
    return doc.id


def _seed_asset(db, workspace_id, *, key, duration=None) -> str:
    row = MediaAsset(workspace_id=workspace_id, type="video", origin="upload",
                     storage_key=key, duration_seconds=duration)
    db.add(row)
    db.flush()
    return row.id


def _write_file(root: Path, workspace_id: str, key: str, data: bytes) -> Path:
    path = Path(root) / workspace_id / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    """Redirect storage into tmp; every path-safety helper reads it live."""
    import app.services.storage as storage

    root = tmp_path / "videos"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "STORAGE_ROOT", root)
    return root


def _count(db, model, workspace_id) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
        )
        or 0
    )


# ---------------------------------------------------------------------------
# build_research_bundle
# ---------------------------------------------------------------------------


def test_bundle_happy_path_shape_and_json_round_trip(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    with_text = _seed_doc(db_session, ws, title="Alpha guide", content="A" * 600,
                          mime_type="text/html")
    empty = _seed_doc(db_session, ws, title="Beta log", content="", mime_type="text/plain")

    bundle = build_research_bundle(
        db_session, ws, document_ids=[with_text, empty],
        topic="budgeting basics", title="Bundle title",
    )

    assert set(bundle) == {"topic", "title", "summary", "sources", "excerpts",
                           "from_documents", "factual_confidence", "provenance"}
    assert bundle["topic"] == "budgeting basics"
    assert bundle["title"] == "Bundle title"
    assert bundle["provenance"] == "source_documents"
    assert bundle["from_documents"] == [with_text, empty]  # input order preserved

    # deterministic summary built only from facts on hand
    assert bundle["summary"] == "2 source(s) on budgeting basics: Alpha guide; Beta log"

    assert len(bundle["sources"]) == 2
    for entry, doc_id in zip(bundle["sources"], [with_text, empty]):
        assert set(entry) == {"document_id", "title", "mime_type",
                              "retrieved_at", "checksum"}
        assert entry["document_id"] == doc_id
        assert isinstance(entry["retrieved_at"], str)  # created_at default exists

    # excerpts only for the document carrying text, first 500 chars + "..."
    assert len(bundle["excerpts"]) == 1
    assert bundle["excerpts"][0] == {"document_id": with_text, "text": "A" * 500 + "..."}

    # documented heuristic: min(1, 1/2) * min(1, 2/3)
    assert bundle["factual_confidence"] == min(1.0, 1 / 2) * min(1.0, 2 / 3)
    assert 0.0 <= bundle["factual_confidence"] <= 1.0
    assert json.loads(json.dumps(bundle)) == bundle


def test_bundle_all_empty_documents_give_zero_confidence(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    ids = [_seed_doc(db_session, ws, title=f"Empty {i}", content="") for i in range(3)]
    bundle = build_research_bundle(db_session, ws, document_ids=ids, topic="no text")

    assert bundle["excerpts"] == []
    assert bundle["factual_confidence"] == 0.0  # no document has content
    assert bundle["from_documents"] == ids
    assert bundle["summary"] == "3 source(s) on no text: Empty 0; Empty 1; Empty 2"


def test_bundle_excerpt_is_exact_at_500_chars(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    exact = _seed_doc(db_session, ws, title="Exact", content="x" * 500)
    over = _seed_doc(db_session, ws, title="Over", content="y" * 501)
    bundle = build_research_bundle(db_session, ws, document_ids=[exact, over], topic="t")

    texts = {e["document_id"]: e["text"] for e in bundle["excerpts"]}
    assert texts[exact] == "x" * 500  # exactly 500 -> no ellipsis
    assert texts[over] == "y" * 500 + "..."


def test_bundle_foreign_workspace_document_raises(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    other = _other_workspace(db_session)
    foreign = _seed_doc(db_session, other, title="Foreign")

    with pytest.raises(ValueError) as exc:
        build_research_bundle(db_session, ws, document_ids=[foreign], topic="t")
    assert foreign in str(exc.value)  # named, never a silent drop


def test_bundle_missing_document_raises(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    missing = str(uuid.uuid4())
    with pytest.raises(ValueError) as exc:
        build_research_bundle(db_session, ws, document_ids=[missing], topic="t")
    assert missing in str(exc.value)


def test_bundle_deleted_document_excluded(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    alive = _seed_doc(db_session, ws, title="Alive", content="text")
    gone = _seed_doc(db_session, ws, title="Gone", state="deleted", content="text")

    bundle = build_research_bundle(db_session, ws,
                                   document_ids=[alive, gone], topic="t")
    assert bundle["from_documents"] == [alive]
    assert [s["document_id"] for s in bundle["sources"]] == [alive]
    assert [e["document_id"] for e in bundle["excerpts"]] == [alive]


def test_bundle_only_deleted_document_raises(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    gone = _seed_doc(db_session, ws, state="deleted")
    with pytest.raises(ValueError):
        build_research_bundle(db_session, ws, document_ids=[gone], topic="t")


def test_bundle_empty_document_ids_raises(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    with pytest.raises(ValueError):
        build_research_bundle(db_session, ws, document_ids=[], topic="t")


def test_bundle_duplicate_ids_collapse_in_order(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    a = _seed_doc(db_session, ws, title="A", content="a")
    b = _seed_doc(db_session, ws, title="B", content="b")
    bundle = build_research_bundle(db_session, ws,
                                   document_ids=[a, b, a], topic="t")
    assert bundle["from_documents"] == [a, b]
    assert len(bundle["sources"]) == 2


# ---------------------------------------------------------------------------
# create_content_from_bundle
# ---------------------------------------------------------------------------


def test_content_item_research_json_contract_and_no_bypass(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    d1 = _seed_doc(db_session, ws, title="One", content="fact one")
    d2 = _seed_doc(db_session, ws, title="Two", content="fact two")
    bundle = build_research_bundle(db_session, ws, document_ids=[d1, d2],
                                   topic="budgeting basics", title="BB")

    content_id = create_content_from_bundle(db_session, ws, bundle=bundle,
                                            campaign_id="camp-77")
    db_session.commit()

    with session_scope() as s:
        row = s.get(ContentItem, content_id)
        assert row is not None
        assert row.workspace_id == ws
        assert row.topic == "budgeting basics"
        assert row.status == "IDEA"
        assert row.campaign_id == "camp-77"
        # exact bundle round-trip through the JSON column
        assert row.research_json == json.loads(json.dumps(bundle))
        # no-bypass: every bundle key survives for existing research readers
        # (engine/intelligence/verifier.py:276 reads research["sources"])
        assert set(row.research_json) == set(bundle)
        assert len(row.research_json["sources"]) == 2
        assert row.research_json["provenance"] == "source_documents"


def test_content_graph_nodes_and_edges(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    d1 = _seed_doc(db_session, ws, title="Source One")
    d2 = _seed_doc(db_session, ws, title="Source Two")
    topic = "compound interest"
    bundle = build_research_bundle(db_session, ws, document_ids=[d1, d2],
                                   topic=topic, title="CI")

    content_id = create_content_from_bundle(db_session, ws, bundle=bundle)
    db_session.commit()

    graph = KnowledgeGraphProvider()
    with session_scope() as s:
        content_node = graph.get_node(s, ws, "Content", content_id)
        assert content_node is not None
        assert content_node["ref_id"] == content_id
        assert content_node["label"] == "CI"

        sources = graph.neighbors(s, ws, node_type="Content", key=content_id,
                                  direction="out", relationship="SOURCE_REFERENCES")
        assert {n["ref_id"] for n in sources} == {d1, d2}
        assert all(n["node_type"] == "Source" for n in sources)
        assert {n["label"] for n in sources} == {"Source One", "Source Two"}

        topics = graph.neighbors(s, ws, node_type="Content", key=content_id,
                                 direction="out", relationship="CONTENT_ABOUT_TOPIC")
        assert len(topics) == 1
        assert topics[0]["node_type"] == "Topic"
        assert topics[0]["topic_key"] == topic_key_of(topic)

        # exactly Content + 2 Sources + Topic in this workspace
        assert _count(s, KnowledgeNode, ws) == 4


def test_content_invalid_bundle_shapes_raise(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    doc = _seed_doc(db_session, ws, title="Doc", content="text")
    good = build_research_bundle(db_session, ws, document_ids=[doc], topic="t")

    no_topic = dict(good)
    no_topic.pop("topic")
    with pytest.raises(ValueError, match="topic"):
        create_content_from_bundle(db_session, ws, bundle=no_topic)

    wrong_provenance = dict(good)
    wrong_provenance["provenance"] = "web_page"
    with pytest.raises(ValueError, match="provenance"):
        create_content_from_bundle(db_session, ws, bundle=wrong_provenance)

    empty_docs = dict(good)
    empty_docs["from_documents"] = []
    with pytest.raises(ValueError, match="from_documents"):
        create_content_from_bundle(db_session, ws, bundle=empty_docs)

    with pytest.raises(ValueError, match="dict"):
        create_content_from_bundle(db_session, ws, bundle=["not", "a", "dict"])

    # nothing was written by the rejected bundles
    assert _count(db_session, ContentItem, ws) == 0


def test_content_bundle_referencing_deleted_document_raises(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    doc = _seed_doc(db_session, ws, title="Doc", content="text")
    bundle = build_research_bundle(db_session, ws, document_ids=[doc], topic="t")

    row = db_session.get(SourceDocument, doc)
    row.state = "deleted"
    db_session.flush()

    with pytest.raises(ValueError) as exc:
        create_content_from_bundle(db_session, ws, bundle=bundle)
    assert doc in str(exc.value)
    assert _count(db_session, ContentItem, ws) == 0


def test_content_isolation_foreign_workspace_rejected(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    doc = _seed_doc(db_session, ws, title="Doc", content="text")
    bundle = build_research_bundle(db_session, ws, document_ids=[doc], topic="t")
    other = _other_workspace(db_session)

    with pytest.raises(ValueError) as exc:
        create_content_from_bundle(db_session, other, bundle=bundle)
    assert doc in str(exc.value)
    assert _count(db_session, ContentItem, other) == 0
    assert _count(db_session, KnowledgeNode, other) == 0


# ---------------------------------------------------------------------------
# asset_from_document
# ---------------------------------------------------------------------------


def test_asset_from_document_real_file_and_dedupe(
    db_session, workspace_with_user, storage_root
):
    ws = workspace_with_user["workspace"]
    _write_file(storage_root, ws, "sources/clip.mp4", b"\x00\x01fake-clip")
    doc = _seed_doc(db_session, ws, title="Clip", mime_type="video/mp4",
                    asset_reference="sources/clip.mp4", checksum="c" * 64)

    first = asset_from_document(db_session, ws, document_id=doc)
    assert isinstance(first, str) and first
    second = asset_from_document(db_session, ws, document_id=doc)
    assert second == first  # dedupe: same storage_key -> same row

    rows = db_session.scalars(
        select(MediaAsset).where(MediaAsset.workspace_id == ws)
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == first
    assert row.storage_key == "sources/clip.mp4"
    assert row.type == "video"
    assert row.mime_type == "video/mp4"
    assert row.origin == "import"
    assert row.checksum == "c" * 64  # document checksum lands as-is


def test_asset_checksum_computed_when_document_checksum_empty(
    db_session, workspace_with_user, storage_root
):
    ws = workspace_with_user["workspace"]
    data = b"report-bytes"
    _write_file(storage_root, ws, "docs/report.pdf", data)
    doc = _seed_doc(db_session, ws, title="Report", mime_type="application/pdf",
                    asset_reference="docs/report.pdf", checksum="")

    asset_id = asset_from_document(db_session, ws, document_id=doc)
    row = db_session.get(MediaAsset, asset_id)
    assert row.type == "other"  # unclassified mime maps to ASSET_TYPES "other"
    assert row.checksum == hashlib.sha256(data).hexdigest()


def test_asset_missing_file_returns_none(db_session, workspace_with_user, storage_root):
    ws = workspace_with_user["workspace"]
    doc = _seed_doc(db_session, ws, asset_reference="sources/absent.mp4")
    assert asset_from_document(db_session, ws, document_id=doc) is None
    assert _count(db_session, MediaAsset, ws) == 0  # never fabricated


def test_asset_traversal_reference_returns_none(
    db_session, workspace_with_user, storage_root
):
    ws = workspace_with_user["workspace"]
    # a real file outside the workspace proves traversal would "work" unsafely
    outside = Path(storage_root).parent / "outside.mp4"
    outside.write_bytes(b"secret")
    for bad in ("../outside.mp4", "sources/../../outside.mp4"):
        doc = _seed_doc(db_session, ws, title="Evil", asset_reference=bad)
        assert asset_from_document(db_session, ws, document_id=doc) is None
    assert _count(db_session, MediaAsset, ws) == 0


def test_asset_s3_reference_returns_none(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    doc = _seed_doc(db_session, ws, asset_reference="s3://bucket/key.mp4")
    assert asset_from_document(db_session, ws, document_id=doc) is None


def test_asset_empty_reference_returns_none(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    doc = _seed_doc(db_session, ws, asset_reference="")
    assert asset_from_document(db_session, ws, document_id=doc) is None


def test_asset_foreign_and_missing_document_return_none(
    db_session, workspace_with_user
):
    ws = workspace_with_user["workspace"]
    other = _other_workspace(db_session)
    foreign = _seed_doc(db_session, other, asset_reference="anything.mp4")

    assert asset_from_document(db_session, ws, document_id=foreign) is None
    assert asset_from_document(db_session, ws,
                               document_id=str(uuid.uuid4())) is None
    assert _count(db_session, MediaAsset, ws) == 0


# ---------------------------------------------------------------------------
# timeline_from_asset
# ---------------------------------------------------------------------------


def test_timeline_wraps_existing_builder_with_resolved_path(
    db_session, workspace_with_user, storage_root, monkeypatch
):
    ws = workspace_with_user["workspace"]
    path = _write_file(storage_root, ws, "clips/a.mp4", b"fakevideo")
    asset_id = _seed_asset(db_session, ws, key="clips/a.mp4", duration=7.5)

    calls: list[dict] = []

    def fake(workspace_id, *, video_id, duration_seconds=None, aspect="9:16",
             file_path=""):
        calls.append({"workspace_id": workspace_id, "video_id": video_id,
                      "duration_seconds": duration_seconds, "aspect": aspect,
                      "file_path": file_path})
        return {"tracks": [], "duration_seconds": float(duration_seconds or 0.0),
                "fps": 30.0, "aspect_ratio": aspect}

    monkeypatch.setattr("app.engine.knowledge.lineage.timeline_from_video", fake)

    out = timeline_from_asset(db_session, ws, asset_id=asset_id,
                              content_id="content-9")
    assert len(calls) == 1
    assert calls[0]["workspace_id"] == ws  # positional workspace
    assert calls[0]["video_id"] == asset_id
    assert calls[0]["duration_seconds"] == 7.5
    assert Path(calls[0]["file_path"]).resolve() == path.resolve()
    assert out["asset_id"] == asset_id
    assert out["content_id"] == "content-9"

    # content_id is optional passthrough
    out2 = timeline_from_asset(db_session, ws, asset_id=asset_id)
    assert out2["asset_id"] == asset_id
    assert "content_id" not in out2


def test_timeline_real_builder_returns_document_with_chain_ids(
    db_session, workspace_with_user, storage_root
):
    ws = workspace_with_user["workspace"]
    path = _write_file(storage_root, ws, "clips/b.mp4", b"fakevideo")
    asset_id = _seed_asset(db_session, ws, key="clips/b.mp4")  # unknown duration

    out = timeline_from_asset(db_session, ws, asset_id=asset_id,
                              content_id="content-1")
    assert "error" not in out
    assert out["asset_id"] == asset_id
    assert out["content_id"] == "content-1"
    assert out["duration_seconds"] == 5.0  # builder FALLBACK_DURATION_SECONDS
    video_track = next(t for t in out["tracks"] if t["kind"] == "video")
    clip = video_track["clips"][0]
    assert clip["source"]["video_id"] == asset_id
    assert Path(clip["source"]["file_path"]).resolve() == path.resolve()
    assert json.loads(json.dumps(out)) == out  # JSON-serializable


def test_timeline_missing_file_refuses_with_error(
    db_session, workspace_with_user, storage_root
):
    ws = workspace_with_user["workspace"]
    asset_id = _seed_asset(db_session, ws, key="clips/gone.mp4")  # no file

    out = timeline_from_asset(db_session, ws, asset_id=asset_id,
                              content_id="c-1")
    assert "error" in out
    assert "no existing file" in out["error"]
    assert out["asset_id"] == asset_id
    assert out["content_id"] == "c-1"


def test_timeline_unknown_asset_refuses(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    out = timeline_from_asset(db_session, ws, asset_id=str(uuid.uuid4()))
    assert "error" in out
    assert "not found in workspace" in out["error"]
    assert "tracks" not in out  # no invented timeline


def test_timeline_foreign_asset_refuses(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    other = _other_workspace(db_session)
    foreign = _seed_asset(db_session, other, key="clips/foreign.mp4")

    out = timeline_from_asset(db_session, ws, asset_id=foreign)
    assert "error" in out
    assert "not found in workspace" in out["error"]
    assert out["asset_id"] == foreign
