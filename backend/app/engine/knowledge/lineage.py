"""Source lineage (Work 10, Lane F): SourceDocument -> research bundle ->
ContentItem -> MediaAsset -> timeline.

Four functions, one honest chain (docs/work10_contracts.md "Lane F"):

* ``build_research_bundle`` — facts only: the titles/mime types/checksums/
  timestamps held on the SourceDocument rows plus the first 500 chars of
  stored text. A missing or foreign document id raises ``ValueError`` naming
  the id (never a silent drop); ``state == "deleted"`` documents are
  excluded; a request left with no usable document raises as well.
* ``create_content_from_bundle`` — re-resolves the bundle's documents inside
  the caller's workspace (a bundle can never conjure rows), writes the bundle
  into ``ContentItem.research_json`` exactly the way existing Research code
  does (app/engine/autopilot.py:555-558 sets ``content.research_json`` on an
  IDEA item), and records the lineage via ``KnowledgeGraphProvider`` using
  content_graph conventions: Content node keyed by the ContentItem id, one
  Source node keyed by each SourceDocument id, Topic node keyed by
  ``topic_key_of(topic)``; edges SOURCE_REFERENCES + CONTENT_ABOUT_TOPIC.
* ``asset_from_document`` — local files only. ``validate_storage_key`` runs
  FIRST (absolute / ``..``-escaping keys are refused), then ``managed_path``
  plus an on-disk existence check — an asset row is never fabricated.
  ``s3://`` references return ``None`` because materializing remote objects
  needs network + credentials this lane never has; the sync/download path
  owns that. Dedupe is per ``(workspace_id, storage_key)``, mirroring
  providers/longform_assets.py:45-49; checksum lands on the row as the
  MediaAsset column (hex digest): the document's own checksum when set,
  otherwise sha256 of the file bytes.
* ``timeline_from_asset`` — wraps the existing pure builder
  ``engine.timeline.timeline_from_video(workspace_id, video_id=asset.id,
  duration_seconds=..., file_path=resolved path)``. Refusals (unknown/foreign
  asset, missing storage key, file missing or outside the workspace storage
  root) come back as an honest ``{"error": ..., "asset_id": ...}`` dict
  (``content_id`` added when the caller passed one) — this function's return
  type is always ``dict`` and callers branch on the ``error`` key. Whatever
  the existing builder returns (or raises, e.g. TimelineValidationError) is
  surfaced untouched: this lane never invents timeline entries.

Every query is workspace-scoped. Sessions are flushed, never committed —
commit ownership stays with ``session_scope`` / the API dependency.
No network, no secrets, no new dependencies.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.knowledge.graph import KnowledgeGraphProvider
from app.engine.knowledge.normalize import topic_key_of
from app.engine.timeline import timeline_from_video
from app.models import ContentItem, SourceDocument
from app.models.assets import MediaAsset
from app.models.base import ContentStatus
from app.services import storage as storage_mod

#: literal provenance marker required by create_content_from_bundle
PROVENANCE_SOURCE_DOCUMENTS = "source_documents"
#: excerpt window: first 500 chars of stored text, "..." when truncated
EXCERPT_CHARS = 500
#: document count at which the size term of factual_confidence saturates
CONFIDENCE_SATURATION_DOCS = 3


def _iso(value) -> str | None:
    """ISO-8601 string for datetimes, ``None`` for missing values."""
    return value.isoformat() if value is not None else None


def _retrieved_at(doc: SourceDocument):
    """When this workspace last held the document: last_seen -> first_seen ->
    row creation (all recorded ingestion facts, never invented)."""
    return doc.last_seen_at or doc.first_seen_at or doc.created_at


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_file(workspace_id: str, key: str) -> Path | None:
    """Existing regular file for a workspace-relative storage key.

    The candidate is joined under ``STORAGE_ROOT/<workspace_id>/`` and pushed
    through ``managed_path`` (fails closed when it escapes the workspace
    directory — ``..``, absolute paths, foreign workspaces), then must exist
    on disk. ``STORAGE_ROOT`` is read from the storage module at call time so
    tests can redirect it.
    """
    if not workspace_id or not key:
        return None
    candidate = Path(storage_mod.STORAGE_ROOT) / workspace_id / key
    managed = storage_mod.managed_path(workspace_id, str(candidate))
    if managed is None:
        return None
    return managed if managed.is_file() else None


def _asset_file(workspace_id: str, storage_key: str) -> Path | None:
    """MediaAsset.storage_key -> existing local file (or ``None``).

    Accepts both storage-key conventions present in the codebase: a bare
    workspace-relative key (``clips/a.mp4``, resolved under
    ``STORAGE_ROOT/<ws>/``) and a stored path already relative to the repo
    root (``data/videos/<ws>/x``, resolved by ``managed_path`` directly).
    """
    key = str(storage_key or "").strip()
    if not key or key.startswith("s3://"):
        return None
    direct = storage_mod.managed_path(workspace_id, key)
    if direct is not None and direct.is_file():
        return direct
    return _workspace_file(workspace_id, key)


def _load_document(db: Session, workspace_id: str, document_id: str) -> SourceDocument | None:
    return db.scalars(
        select(SourceDocument).where(
            SourceDocument.workspace_id == workspace_id,
            SourceDocument.id == document_id,
        )
    ).first()


def _asset_type_for(mime_type: str) -> str:
    """MediaAsset.type from the document mime (``ASSET_TYPES`` vocabulary)."""
    mime = str(mime_type or "").lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    return "other"


def build_research_bundle(db: Session, workspace_id: str, *, document_ids,
                          topic: str, title: str = "") -> dict:
    """SourceDocument rows -> a JSON-serializable research bundle.

    Honesty rules (tested in tests/test_source_lineage.py):

    * A missing or foreign document id raises ``ValueError`` naming it —
      inputs are never silently dropped, and rows from another workspace
      behave exactly like missing rows.
    * ``state == "deleted"`` documents are excluded from the bundle; if that
      leaves nothing usable, ``ValueError``.
    * Duplicate ids are collapsed (first occurrence wins) so ``sources``,
      ``excerpts`` and ``from_documents`` stay aligned.
    * ``sources`` entries carry exactly
      ``{document_id, title, mime_type, retrieved_at, checksum}`` with
      datetimes as ISO strings (``retrieved_at`` = last_seen_at falling back
      to first_seen_at, then created_at).
    * ``excerpts`` are the deterministic first 500 characters of each
      non-empty ``content`` (``"..."`` appended when truncated) — only for
      documents that actually carry text.
    * ``summary`` is assembled from facts on hand (count + topic + source
      titles, falling back to remote_id, then the document id) — never
      invented prose.
    * ``factual_confidence`` in [0.0, 1.0] is a documented deterministic
      heuristic (the Research agent's aggregate at engine/agents/creation.py
      :79-83 is claims-based and unreachable without LLM claims, so it is
      NOT reused here):

          min(1.0, docs_with_text / max(1, len(docs)))
          * min(1.0, len(docs) / 3)

      i.e. full credit only when every document carries text AND there are
      at least 3 documents; 0.0 when no document has content.
    """
    if isinstance(document_ids, str) or not document_ids:
        raise ValueError("document_ids must be a non-empty list of document ids")
    ordered_ids = list(dict.fromkeys(str(i) for i in document_ids))

    docs: list[SourceDocument] = []
    for doc_id in ordered_ids:
        doc = _load_document(db, workspace_id, doc_id)
        if doc is None:
            raise ValueError(f"document '{doc_id}' not found in workspace {workspace_id}")
        if doc.state == "deleted":
            continue  # tombstones never enter research
        docs.append(doc)
    if not docs:
        raise ValueError("no usable source documents left (all missing or deleted)")

    topic_text = str(topic or "")
    sources = [
        {
            "document_id": doc.id,
            "title": doc.title,
            "mime_type": doc.mime_type,
            "retrieved_at": _iso(_retrieved_at(doc)),
            "checksum": doc.checksum,
        }
        for doc in docs
    ]
    excerpts = [
        {"document_id": doc.id, "text": _excerpt(doc.content or "")}
        for doc in docs
        if doc.content
    ]
    titles = "; ".join(doc.title or doc.remote_id or doc.id for doc in docs)
    with_text = sum(1 for doc in docs if doc.content)
    confidence = min(1.0, with_text / max(1, len(docs))) * min(
        1.0, len(docs) / CONFIDENCE_SATURATION_DOCS
    )
    return {
        "topic": topic_text,
        "title": str(title or ""),
        "summary": f"{len(docs)} source(s) on {topic_text}: {titles}",
        "sources": sources,
        "excerpts": excerpts,
        "from_documents": [doc.id for doc in docs],
        "factual_confidence": confidence,
        "provenance": PROVENANCE_SOURCE_DOCUMENTS,
    }


def _excerpt(content: str) -> str:
    if len(content) <= EXCERPT_CHARS:
        return content
    return content[:EXCERPT_CHARS] + "..."


def create_content_from_bundle(db: Session, workspace_id: str, *, bundle,
                               campaign_id: str | None = None) -> str:
    """Bundle -> ContentItem (research_json contract) + knowledge-graph edges.

    Validation is minimal and honest: ``bundle`` must be a dict with a
    non-empty ``topic``, ``provenance == "source_documents"`` and a non-empty
    ``from_documents`` list, or ``ValueError``. The documents are then
    resolved AGAIN, workspace-scoped: a vanished, foreign or now-deleted row
    raises ``ValueError`` naming it — a bundle cannot conjure rows.

    The ContentItem is created exactly the way existing code creates root
    content (workspace_id + topic + status IDEA + research_json, flushed,
    never committed here — see engine/autopilot.py:308-317 and
    models/content.py:96). Graph lineage uses KnowledgeGraphProvider with
    content_graph node-key conventions: Content keyed by the ContentItem id,
    Source nodes keyed by SourceDocument ids, Topic keyed by its normalized
    topic_key; SOURCE_REFERENCES (Content -> each Source) and
    CONTENT_ABOUT_TOPIC (Content -> Topic) edges.

    Returns the new content id.
    """
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be a dict")
    topic = str(bundle.get("topic") or "").strip()
    if not topic:
        raise ValueError("bundle is missing a topic")
    if bundle.get("provenance") != PROVENANCE_SOURCE_DOCUMENTS:
        raise ValueError("bundle provenance must be 'source_documents'")
    from_documents = bundle.get("from_documents")
    if not isinstance(from_documents, list) or not from_documents:
        raise ValueError("bundle from_documents must be a non-empty list")

    docs: list[SourceDocument] = []
    for raw_id in from_documents:
        doc_id = str(raw_id)
        doc = _load_document(db, workspace_id, doc_id)
        if doc is None:
            raise ValueError(f"document '{doc_id}' not found in workspace {workspace_id}")
        if doc.state == "deleted":
            raise ValueError(f"document '{doc_id}' is deleted; a bundle cannot conjure rows")
        docs.append(doc)

    item = ContentItem(
        workspace_id=workspace_id,
        topic=topic[:400],
        status=ContentStatus.IDEA.value,
        research_json=dict(bundle),
        campaign_id=campaign_id,
    )
    db.add(item)
    db.flush()

    graph = KnowledgeGraphProvider()
    content_node = graph.upsert_node(
        db, workspace_id, node_type="Content", ref_id=item.id,
        label=str(bundle.get("title") or topic),
    )
    source_nodes = [
        graph.upsert_node(db, workspace_id, node_type="Source",
                          ref_id=doc.id, label=doc.title)
        for doc in docs
    ]
    topic_node = graph.upsert_node(
        db, workspace_id, node_type="Topic", label=topic,
        topic_key=topic_key_of(topic),
    )
    for source_node in source_nodes:
        graph.link(db, workspace_id, from_node=content_node, to_node=source_node,
                   relationship="SOURCE_REFERENCES")
    graph.link(db, workspace_id, from_node=content_node, to_node=topic_node,
               relationship="CONTENT_ABOUT_TOPIC")
    return item.id


def asset_from_document(db: Session, workspace_id: str, *, document_id) -> str | None:
    """SourceDocument -> MediaAsset id (``None`` when there is no local file).

    Honest refusals (all tested): unknown/foreign ``document_id``; empty
    ``asset_reference``; ``s3://`` references (remote materialization belongs
    to the sync/download path — this lane has no network/credentials); keys
    failing ``validate_storage_key`` (absolute or ``..``-escaping — path
    safety runs BEFORE any filesystem access); a reference that resolves
    outside the workspace storage root or does not exist on disk (assets are
    never fabricated).

    Dedupe: an existing MediaAsset with the same ``(workspace_id,
    storage_key)`` is returned as-is — no second row. Otherwise a row is
    created with type/mime derived from the document's mime_type, origin
    "import", and checksum = the document's checksum when non-empty, else
    sha256 of the file bytes (MediaAsset.checksum is a free-form hex digest
    column, String(128), so both land identically).
    """
    doc = _load_document(db, workspace_id, str(document_id))
    if doc is None:
        return None
    reference = str(doc.asset_reference or "").strip()
    if not reference:
        return None
    if reference.startswith("s3://"):
        return None  # materialize in the sync/download path, never here
    key = storage_mod.validate_storage_key(workspace_id, reference)
    if not key:
        return None  # path-safety refusal: absolute or escaping the root
    path = _workspace_file(workspace_id, key)
    if path is None:
        return None  # missing on disk or outside the workspace directory

    existing = db.scalars(
        select(MediaAsset).where(
            MediaAsset.workspace_id == workspace_id,
            MediaAsset.storage_key == key,
        )
    ).first()
    if existing is not None:
        return existing.id

    checksum = str(doc.checksum or "") or _sha256_file(path)
    row = MediaAsset(
        workspace_id=workspace_id,
        type=_asset_type_for(doc.mime_type),
        origin="import",
        storage_key=key,
        mime_type=doc.mime_type,
        checksum=checksum,
    )
    db.add(row)
    db.flush()
    return row.id


def timeline_from_asset(db: Session, workspace_id: str, *, asset_id,
                        content_id: str | None = None) -> dict:
    """MediaAsset -> the existing timeline builder, plus chain ids.

    Wraps ``engine.timeline.timeline_from_video(workspace_id,
    video_id=asset.id, duration_seconds=asset.duration_seconds,
    file_path=<resolved path>)``. The MediaAsset is loaded workspace-scoped;
    an unknown/foreign asset, an empty storage key, or a file that is missing
    or resolves outside the workspace storage root yields an honest refusal
    dict ``{"error": <reason>, "asset_id": ..., "content_id": ...}``
    (``content_id`` only when the caller passed one) — callers branch on the
    ``error`` key; this function never raises for those environmental
    failures.

    On success the builder's dict is returned verbatim plus ``asset_id``
    (and ``content_id`` when given) so callers can record the
    Asset -> Timeline -> Content chain. The builder is pure — no rows are
    written here — and any error it itself raises (TimelineValidationError
    etc.) propagates untouched. Timeline entries are never invented.
    """

    def refusal(reason: str) -> dict[str, Any]:
        out: dict[str, Any] = {"error": reason, "asset_id": asset_id}
        if content_id is not None:
            out["content_id"] = content_id
        return out

    asset = db.scalars(
        select(MediaAsset).where(
            MediaAsset.workspace_id == workspace_id,
            MediaAsset.id == str(asset_id),
        )
    ).first()
    if asset is None:
        return refusal(f"asset '{asset_id}' not found in workspace {workspace_id}")
    path = _asset_file(workspace_id, asset.storage_key)
    if path is None:
        return refusal(
            f"asset '{asset_id}' has no existing file inside workspace storage"
        )

    doc = timeline_from_video(
        workspace_id,
        video_id=asset.id,
        duration_seconds=asset.duration_seconds,
        file_path=str(path),
    )
    doc["asset_id"] = asset_id
    if content_id is not None:
        doc["content_id"] = content_id
    return doc


__all__ = [
    "asset_from_document",
    "build_research_bundle",
    "create_content_from_bundle",
    "timeline_from_asset",
]
