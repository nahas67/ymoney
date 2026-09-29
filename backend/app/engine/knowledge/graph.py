"""Provider-independent relational knowledge graph (Work 10, Lane B).

SQLite/SQLAlchemy implementation of the knowledge-graph interface over the
``knowledge_nodes`` / ``knowledge_edges`` tables — no graph DB required, so a
future provider can swap the persistence layer behind the same method
signatures.

Invariants (all tested in ``tests/test_knowledge_graph.py``):

* Vocabulary: ``node_type`` / ``relationship`` outside ``NODE_TYPES`` /
  ``RELATIONSHIPS`` raise ``ValueError`` before touching the session.
* Identity: ``node_key`` = ``ref_id`` when non-empty, else the normalized
  label truncated/hashed to 120 chars (``node_key_of`` — byte-for-byte
  deterministic: same input → same key → same row, never a duplicate).
  Nodes are unique per ``(workspace_id, node_type, node_key)``; re-upserting
  updates label/topic/meta in place and keeps the same id.
* ``link`` is idempotent on ``(workspace_id, from, to, relationship)``: the
  second call updates ``weight``/``evidence_ids``/``meta`` on the existing
  edge — never inserts a second row. Both endpoints must exist *in the
  caller's workspace*: a node dict carrying another ``workspace_id`` (or a
  row that does not resolve under the requested workspace) → ``ValueError``.
* Workspace isolation is always a WHERE clause, never a score.
* Every returned dict is JSON-serializable (datetimes as ISO strings).

The session ``db`` is flushed but never committed here — commit ownership
stays with the caller (``session_scope`` / API dependency).
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.engine.knowledge.normalize import normalize_text
from app.models.knowledge import KnowledgeEdge, KnowledgeNode

NODE_TYPES: tuple[str, ...] = (
    "Brand",
    "Campaign",
    "Content",
    "Scene",
    "Topic",
    "Entity",
    "Source",
    "Claim",
    "AudienceInsight",
    "CommunityInsight",
    "Experiment",
    "CreativeLesson",
    "Publication",
)

RELATIONSHIPS: tuple[str, ...] = (
    "CONTENT_ABOUT_TOPIC",
    "CLAIM_SUPPORTED_BY",
    "DERIVED_FROM",
    "MENTS_ENTITY",
    "PERFORMED_ON",
    "LEARNED_FROM",
    "COMMUNITY_REQUESTED",
    "EXPERIMENT_TESTED",
    "BRAND_USES",
    "SOURCE_REFERENCES",
)

_NODE_KEY_MAX = 120
_HASH_LEN = 16
_LABEL_PREFIX_LEN = _NODE_KEY_MAX - _HASH_LEN - 1  # 103 + "~" + 16 hex = 120

DIRECTIONS: tuple[str, ...] = ("out", "in", "both")


def node_key_of(ref_id: str = "", label: str = "") -> str:
    """Deterministic node identity (max 120 chars — the column width).

    ``ref_id`` wins when non-empty (domain rows are already unique per
    workspace+type). Otherwise the normalized label is used as-is when it
    fits; longer labels keep a 103-char prefix plus a sha256 fragment so two
    long labels sharing a prefix never collapse onto one node.
    """
    ref = str(ref_id or "")
    if ref:
        return ref[:_NODE_KEY_MAX]
    normalized = normalize_text(label)
    if len(normalized) <= _NODE_KEY_MAX:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{normalized[:_LABEL_PREFIX_LEN]}~{digest}"


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _node_dict(row: KnowledgeNode) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "node_type": row.node_type,
        "ref_id": row.ref_id,
        "node_key": row.node_key,
        "label": row.label,
        "topic_key": row.topic_key,
        "meta": dict(row.meta_json or {}),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _edge_dict(row: KnowledgeEdge) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "from_node_id": row.from_node_id,
        "to_node_id": row.to_node_id,
        "relationship": row.relationship,
        "weight": float(row.weight),
        "evidence_ids": list(row.evidence_ids or []),
        "meta": dict(row.meta_json or {}),
        "created_at": _iso(row.created_at),
    }


class KnowledgeGraphProvider:
    """Relational implementation (SQLite/SQLAlchemy first — no graph DB)."""

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def _check_node_type(node_type: str) -> None:
        if node_type not in NODE_TYPES:
            raise ValueError(f"unknown node_type: {node_type!r}")

    @staticmethod
    def _check_relationship(relationship: str) -> None:
        if relationship not in RELATIONSHIPS:
            raise ValueError(f"unknown relationship: {relationship!r}")

    @staticmethod
    def _check_direction(direction: str) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(f"unknown direction: {direction!r}")

    # -- nodes --------------------------------------------------------------

    def upsert_node(
        self,
        db: Session,
        workspace_id: str,
        *,
        node_type: str,
        ref_id: str = "",
        label: str = "",
        topic_key: str = "",
        meta: dict | None = None,
    ) -> dict:
        """Create or refresh one node; identical input always yields one row.

        ``node_key`` derives from ``ref_id`` (preferred) or ``normalize_text
        (label)`` — see :func:`node_key_of`. On re-create: a non-empty
        ``label`` replaces the stored label, ``meta`` keys merge over the
        stored dict (later wins), a non-empty ``topic_key`` replaces the old
        one, and a missing ``ref_id`` is backfilled. Empty arguments never
        wipe existing values.
        """
        self._check_node_type(node_type)
        ref = str(ref_id or "")
        key = node_key_of(ref, label)
        row = db.scalars(
            select(KnowledgeNode).where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.node_type == node_type,
                KnowledgeNode.node_key == key,
            )
        ).first()
        if row is None:
            row = KnowledgeNode(
                workspace_id=workspace_id,
                node_type=node_type,
                ref_id=ref[:36],
                node_key=key,
                label=str(label or "")[:200],
                topic_key=str(topic_key or "")[:64],
                meta_json=dict(meta or {}),
            )
            db.add(row)
        else:
            if label:
                row.label = str(label)[:200]
            if ref and not row.ref_id:
                row.ref_id = ref[:36]
            if topic_key:
                row.topic_key = str(topic_key)[:64]
            if meta is not None:
                merged = dict(row.meta_json or {})
                merged.update(meta)
                row.meta_json = merged
        db.flush()
        return _node_dict(row)

    def get_node(
        self, db: Session, workspace_id: str, node_type: str, key: str
    ) -> dict | None:
        """Look up a node by ``node_key`` (falling back to ``ref_id``).

        Returns ``None`` when nothing matches inside ``workspace_id``.
        """
        self._check_node_type(node_type)
        if key is None:
            return None
        try:
            key_str = str(key)
        except Exception:
            return None
        condition = KnowledgeNode.node_key == key_str
        if key_str:
            condition = or_(
                KnowledgeNode.node_key == key_str, KnowledgeNode.ref_id == key_str
            )
        row = db.scalars(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.node_type == node_type,
                condition,
            )
            .order_by(KnowledgeNode.node_key)
        ).first()
        return _node_dict(row) if row is not None else None

    # -- edges --------------------------------------------------------------

    def _resolve_endpoint(
        self, db: Session, workspace_id: str, node: Any, which: str
    ) -> KnowledgeNode:
        """Validate a node reference (dict or ``(node_type, key)`` tuple).

        Every failure mode is ``ValueError``: foreign-workspace dict, dict
        without ``id``/``workspace_id``, unresolvable id, unknown tuple key.
        """
        if isinstance(node, dict):
            if str(node.get("workspace_id") or "") != str(workspace_id):
                raise ValueError(
                    f"{which} node belongs to a different workspace "
                    f"({node.get('workspace_id')!r} != {workspace_id!r})"
                )
            node_id = node.get("id")
            if not node_id:
                raise ValueError(f"{which} node dict is missing an id")
        elif isinstance(node, (tuple, list)) and len(node) == 2:
            found = self.get_node(db, workspace_id, node[0], node[1])
            if found is None:
                raise ValueError(f"{which} node not found in workspace: {node!r}")
            node_id = found["id"]
        else:
            raise ValueError(
                f"{which} node must be a node dict or (node_type, key), "
                f"got {type(node).__name__}"
            )
        row = db.scalars(
            select(KnowledgeNode).where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.id == node_id,
            )
        ).first()
        if row is None:
            raise ValueError(f"{which} node does not exist in this workspace: {node_id!r}")
        return row

    def link(
        self,
        db: Session,
        workspace_id: str,
        *,
        from_node: Any,
        to_node: Any,
        relationship: str,
        weight: float = 1.0,
        evidence_ids: list | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Create or refresh one edge; idempotent on the unique tuple.

        The newest call's ``weight`` wins and ``evidence_ids`` merge as an
        order-preserving union (existing first). ``meta`` keys merge over the
        stored dict when given. Both endpoints must resolve inside
        ``workspace_id`` — a node dict from another workspace raises
        ``ValueError``.
        """
        self._check_relationship(relationship)
        src = self._resolve_endpoint(db, workspace_id, from_node, "from")
        dst = self._resolve_endpoint(db, workspace_id, to_node, "to")
        edge = db.scalars(
            select(KnowledgeEdge).where(
                KnowledgeEdge.workspace_id == workspace_id,
                KnowledgeEdge.from_node_id == src.id,
                KnowledgeEdge.to_node_id == dst.id,
                KnowledgeEdge.relationship == relationship,
            )
        ).first()
        if edge is None:
            edge = KnowledgeEdge(
                workspace_id=workspace_id,
                from_node_id=src.id,
                to_node_id=dst.id,
                relationship=relationship,
                weight=float(weight),
                evidence_ids=list(evidence_ids or []),
                meta_json=dict(meta or {}),
            )
            db.add(edge)
        else:
            edge.weight = float(weight)
            if evidence_ids:
                merged = list(edge.evidence_ids or [])
                for item in evidence_ids:
                    if item not in merged:
                        merged.append(item)
                edge.evidence_ids = merged
            if meta is not None:
                merged_meta = dict(edge.meta_json or {})
                merged_meta.update(meta)
                edge.meta_json = merged_meta
        db.flush()
        return _edge_dict(edge)

    # -- queries ------------------------------------------------------------

    def neighbors(
        self,
        db: Session,
        workspace_id: str,
        *,
        node_id: str | None = None,
        node_type: str | None = None,
        key: str | None = None,
        relationship: str | None = None,
        direction: str = "both",
        limit: int = 50,
    ) -> list[dict]:
        """Adjacent nodes across outgoing/incoming/both edges.

        A target is ``node_id`` or ``node_type``+``key``; without one the
        result is ``[]``. Each entry is the neighbor's node dict plus the
        traversed edge context (``edge_id``, ``relationship``, ``weight``),
        ordered by (neighbor id, relationship, edge id) — deterministic.
        Returns ``[]`` for an unknown/foreign node. ``limit <= 0`` → ``[]``.
        """
        self._check_direction(direction)
        if relationship is not None:
            self._check_relationship(relationship)
        limit = int(limit)
        if limit <= 0:
            return []
        target_id: str | None = None
        if node_id:
            row = db.scalars(
                select(KnowledgeNode).where(
                    KnowledgeNode.workspace_id == workspace_id,
                    KnowledgeNode.id == node_id,
                )
            ).first()
            target_id = row.id if row is not None else None
        elif node_type is not None and key is not None:
            found = self.get_node(db, workspace_id, node_type, key)
            target_id = found["id"] if found is not None else None
        if target_id is None:
            return []

        conditions = []
        if direction in ("out", "both"):
            conditions.append(KnowledgeEdge.from_node_id == target_id)
        if direction in ("in", "both"):
            conditions.append(KnowledgeEdge.to_node_id == target_id)
        query = select(KnowledgeEdge).where(
            KnowledgeEdge.workspace_id == workspace_id, or_(*conditions)
        )
        if relationship is not None:
            query = query.where(KnowledgeEdge.relationship == relationship)
        edges = db.scalars(query.order_by(KnowledgeEdge.id)).all()
        if not edges:
            return []
        other_ids = {
            (e.to_node_id if e.from_node_id == target_id else e.from_node_id)
            for e in edges
        }
        rows = db.scalars(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.id.in_(other_ids),
            )
            .order_by(KnowledgeNode.id)
        ).all()
        by_id = {r.id: r for r in rows}
        entries: list[dict] = []
        for edge in edges:
            other = edge.to_node_id if edge.from_node_id == target_id else edge.from_node_id
            neighbor = by_id.get(other)
            if neighbor is None:
                continue  # foreign-workspace drift can never leak through
            entry = _node_dict(neighbor)
            entry["edge_id"] = edge.id
            entry["relationship"] = edge.relationship
            entry["weight"] = float(edge.weight)
            entries.append(entry)
        entries.sort(key=lambda e: (e["id"], e["relationship"], e["edge_id"]))
        return entries[:limit]

    def subgraph(
        self,
        db: Session,
        workspace_id: str,
        *,
        seed: dict,
        depth: int = 2,
        limit: int = 200,
    ) -> dict:
        """BFS neighborhood of ``seed`` up to ``depth`` hops, capped at ``limit`` nodes.

        ``depth=0`` returns the seed alone; ``depth=1`` adds its direct
        neighbors only. Expansion is deterministic (candidate ids sorted
        ascending before the cap applies), the returned ``nodes`` are sorted
        by id ascending, and ``edges`` contain only edges whose BOTH
        endpoints are inside the collected set — no dangling references.
        An unresolvable seed (missing id or foreign workspace) raises
        ``ValueError``.
        """
        seed_row = self._resolve_endpoint(db, workspace_id, seed, "seed")
        depth = max(0, int(depth))
        limit = max(1, int(limit))
        collected: dict[str, None] = {seed_row.id: None}
        frontier: list[str] = [seed_row.id]
        for _ in range(depth):
            if not frontier or len(collected) >= limit:
                break
            hop_edges = db.scalars(
                select(KnowledgeEdge)
                .where(
                    KnowledgeEdge.workspace_id == workspace_id,
                    or_(
                        KnowledgeEdge.from_node_id.in_(frontier),
                        KnowledgeEdge.to_node_id.in_(frontier),
                    ),
                )
                .order_by(KnowledgeEdge.id)
            ).all()
            candidates: set[str] = set()
            for edge in hop_edges:
                candidates.update((edge.from_node_id, edge.to_node_id))
            next_frontier: list[str] = []
            for candidate in sorted(candidates):
                if candidate in collected:
                    continue
                if len(collected) >= limit:
                    break
                collected[candidate] = None
                next_frontier.append(candidate)
            frontier = next_frontier
        node_ids = list(collected)
        nodes = db.scalars(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.id.in_(node_ids),
            )
            .order_by(KnowledgeNode.id)
        ).all()
        edge_rows = db.scalars(
            select(KnowledgeEdge)
            .where(
                KnowledgeEdge.workspace_id == workspace_id,
                KnowledgeEdge.from_node_id.in_(node_ids),
                KnowledgeEdge.to_node_id.in_(node_ids),
            )
            .order_by(KnowledgeEdge.id)
        ).all()
        return {
            "nodes": [_node_dict(n) for n in nodes],
            "edges": [_edge_dict(e) for e in edge_rows],
        }

    def list_nodes(
        self,
        db: Session,
        workspace_id: str,
        *,
        node_type: str = "",
        limit: int = 200,
    ) -> dict:
        """Every node in this workspace (optionally of one type) + its edges.

        The flat listing behind ``GET /knowledge/graph``: nodes are hard
        filtered to ``workspace_id`` (plus ``node_type`` when given),
        ordered by id ascending and capped at ``clamp(1..500)`` so the
        payload is deterministic and bounded. ``edges`` contain only edges
        whose BOTH endpoints are inside the returned node set — no dangling
        references (the ``subgraph`` rule) — sorted by id ascending.
        ``node_type`` outside ``NODE_TYPES`` raises ``ValueError``.
        """
        if node_type:
            self._check_node_type(node_type)
        limit = max(1, min(int(limit), 500))
        stmt = select(KnowledgeNode).where(KnowledgeNode.workspace_id == workspace_id)
        if node_type:
            stmt = stmt.where(KnowledgeNode.node_type == node_type)
        nodes = db.scalars(stmt.order_by(KnowledgeNode.id).limit(limit)).all()
        node_ids = [n.id for n in nodes]
        if not node_ids:
            return {"nodes": [], "edges": []}
        edges = db.scalars(
            select(KnowledgeEdge)
            .where(
                KnowledgeEdge.workspace_id == workspace_id,
                KnowledgeEdge.from_node_id.in_(node_ids),
                KnowledgeEdge.to_node_id.in_(node_ids),
            )
            .order_by(KnowledgeEdge.id)
        ).all()
        return {
            "nodes": [_node_dict(n) for n in nodes],
            "edges": [_edge_dict(e) for e in edges],
        }

    def edges_for(self, db: Session, workspace_id: str, *, ref_id: str) -> list[dict]:
        """Every edge touching a node that carries this domain ``ref_id``.

        Empty ``ref_id`` returns ``[]`` (it would otherwise match every
        keyless Topic/Entity node). Edges of other workspaces never appear.
        """
        ref = str(ref_id or "")
        if not ref:
            return []
        node_ids = [
            n.id
            for n in db.scalars(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.workspace_id == workspace_id,
                    KnowledgeNode.ref_id == ref,
                )
                .order_by(KnowledgeNode.id)
            ).all()
        ]
        if not node_ids:
            return []
        edges = db.scalars(
            select(KnowledgeEdge)
            .where(
                KnowledgeEdge.workspace_id == workspace_id,
                or_(
                    KnowledgeEdge.from_node_id.in_(node_ids),
                    KnowledgeEdge.to_node_id.in_(node_ids),
                ),
            )
            .order_by(KnowledgeEdge.id)
        ).all()
        return [_edge_dict(e) for e in edges]


__all__ = ["NODE_TYPES", "RELATIONSHIPS", "KnowledgeGraphProvider", "node_key_of"]
