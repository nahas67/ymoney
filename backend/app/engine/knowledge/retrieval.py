"""MemoryRetriever — Work 10 Lane B ranked recall over GlobalMemory.

Deterministic-first pipeline (exact order, all tested in
``tests/test_knowledge_retrieval.py``):

1. **Hard filters** — workspace scope, lifecycle, time window, type,
   platform, brand, topic. Pure predicates over columns, run before any
   scoring; ``metrics["filtered_hard"]`` counts what they removed.
2. **Weighted scoring** — six components in [0, 1] with EXACTLY these
   weights (``ranking.weights``, they sum to 1.0)::

       scope_match 0.25 · relevance 0.25 · evidence_quality 0.15
       freshness 0.15 · confidence 0.10 · usefulness 0.10
3. **Deterministic sort** — total DESC, then created_at DESC, then id ASC.
4. **Semantic stage** — the top ``max(3 * max_results, max_results)``
   candidates are re-ranked through the Work 05 ``DecisionEngine`` with the
   deterministic provider, gated by ``decision_mode_for(workspace_id)``
   (same structure as ``integrations.advise_*``):

   * ``DISABLED`` → engine NOT called, deterministic order kept,
     ``metrics["semantic"] = "disabled"``.
   * ``SHADOW`` → engine called (decision record persists) but the
     deterministic order is KEPT, ``metrics["semantic"] = "shadow"``.
   * ``ASSISTED``/``PRIMARY`` → apply the engine order only when its
     output maps cleanly onto the candidate indices (a full permutation);
     otherwise keep the deterministic order with
     ``"unmappable_output"``. Success → ``"applied"``.
   * Any provider/engine exception is swallowed → deterministic order,
     ``metrics["semantic"] = "error"`` (never propagates).
5. **Truncate** to ``max_results`` (clamped 1..50). Items keep ALL original
   row fields plus ``_score`` (the full breakdown).

The retriever NEVER mutates rows, never writes (no ``mark_used`` — the
context bridge does that later), never commits its session, and never goes
to the network (deterministic provider only).

Exact scoring/filter rules (normative, deterministic):

* ``topic`` hard filter — a row survives iff
  ``row.topic_key == topic_key_of(topic)`` **OR**
  ``semantic_score(topic, row.content, row.scope)[0] > 0``
  (deterministic token-overlap from ``app.services.memory``; an empty topic
  disables the filter).
* ``platform`` — row survives iff ``row.platform in ("", platform)``
  (unscoped = platform-agnostic). ``brand_id`` — survives iff
  ``row.brand_id in (None, "", brand_id)``. Falsy ``platform``/``brand_id``
  means "not requested".
* ``scope_match`` — three dimensions (platform, brand, topic), each counted
  only when its filter was requested. A dimension is *satisfied* iff the
  row matches the request (platform: ``("", filter)``; brand:
  ``(None, "", filter)``; topic: exact ``topic_key`` equality — no partial
  credit). Score = 1.0 when nothing was requested or all requested
  dimensions are satisfied, 0.5 when some are, 0.0 when none are.
* ``relevance`` = ``semantic_score(task or topic, content, scope)[0]``;
  0.0 when neither task nor topic is given.
* ``evidence_quality`` = ``min(1.0, (len(evidence_ids) + len(source_ids))
  / 3)``; 0.0 when both are empty (UNVERIFIED rows rank low here).
* ``freshness`` from ``effective_status(row)``: FRESH 1.0, AGING 0.6,
  STALE 0.2, UNVERIFIED 0.4, CONFLICTED 0.15, anything else 0.3.
* ``confidence`` = row confidence clamped to [0, 1];
  ``usefulness`` = ``min(1.0, use_count / 10)``.
* ``content_format`` — accepted for interface compatibility; the schema has
  no format column, so it intentionally participates in neither filtering
  nor scoring (documented no-op).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.intelligence.decision import DecisionEngine
from app.engine.intelligence.integrations import decision_mode_for
from app.engine.knowledge.freshness import (
    AGING,
    CONFLICTED,
    DISABLED,
    FRESH,
    STALE,
    SUPERSEDED,
    UNVERIFIED,
    effective_status,
)
from app.engine.knowledge.normalize import topic_key_of
from app.models.base import utcnow
from app.models.knowledge import KnowledgeMemory
from app.services.memory import semantic_score

# EXACT ranking weights — tests assert the six keys and sum == 1.0.
WEIGHTS: dict[str, float] = {
    "scope_match": 0.25,
    "relevance": 0.25,
    "evidence_quality": 0.15,
    "freshness": 0.15,
    "confidence": 0.10,
    "usefulness": 0.10,
}

FRESHNESS_POINTS: dict[str, float] = {
    FRESH: 1.0,
    AGING: 0.6,
    STALE: 0.2,
    UNVERIFIED: 0.4,
    CONFLICTED: 0.15,
}
FRESHNESS_FALLBACK = 0.3  # any unexpected effective_status value

MAX_RESULTS_CAP = 50

# lifecycle states that never come back from retrieve()
_DEAD_STATUSES = (SUPERSEDED, DISABLED)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _memory_dict(row: KnowledgeMemory) -> dict:
    """Full row as a fresh JSON-serializable dict (never aliases the row)."""
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "brand_id": row.brand_id,
        "type": row.type,
        "content": row.content,
        "topic": row.topic,
        "topic_key": row.topic_key,
        "scope": row.scope,
        "platform": row.platform,
        "source_ids": list(row.source_ids or []),
        "evidence_ids": list(row.evidence_ids or []),
        "confidence": row.confidence,
        "freshness": row.freshness,
        "status": row.status,
        "origin": row.origin,
        "content_hash": row.content_hash,
        "conflict_group": row.conflict_group,
        "last_verified_at": _iso(row.last_verified_at),
        "superseded_by": row.superseded_by,
        "use_count": row.use_count,
        "last_used_at": _iso(row.last_used_at),
        "related_json": dict(row.related_json or {}),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _map_order(order, size: int) -> list[int] | None:
    """Validate an engine order as a full permutation of range(size)."""
    if not isinstance(order, (list, tuple)) or len(order) != size:
        return None
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in order):
        return None
    if sorted(order) != list(range(size)):
        return None
    return list(order)


class MemoryRetriever:
    """Ranked recall over GlobalMemory (see module docstring for the rules)."""

    # -- stage 1: hard filters ---------------------------------------------

    @staticmethod
    def _hard_filter(
        rows: list[KnowledgeMemory],
        *,
        now,
        time_window_days,
        types: set[str] | None,
        platform: str,
        brand_id: str,
        topic: str,
        topic_key: str,
    ) -> list[KnowledgeMemory]:
        cutoff = None
        if time_window_days is not None:
            cutoff = now - timedelta(days=float(time_window_days))
        survivors: list[KnowledgeMemory] = []
        for row in rows:
            if row.status in _DEAD_STATUSES:
                continue
            if cutoff is not None and row.created_at is not None and row.created_at < cutoff:
                continue
            if types is not None and row.type not in types:
                continue
            if platform and row.platform not in ("", platform):
                continue
            if brand_id and row.brand_id not in (None, "", brand_id):
                continue
            if topic and row.topic_key != topic_key:
                overlap, _ = semantic_score(topic, row.content or "", row.scope or "")
                if overlap <= 0:
                    continue
            survivors.append(row)
        return survivors

    # -- stage 2: scoring ----------------------------------------------------

    @staticmethod
    def _scope_match(row: KnowledgeMemory, *, platform: str, brand_id: str, topic: str,
                     topic_key: str) -> float:
        requested = 0
        satisfied = 0
        if platform:
            requested += 1
            if row.platform in ("", platform):
                satisfied += 1
        if brand_id:
            requested += 1
            if row.brand_id in (None, "", brand_id):
                satisfied += 1
        if topic:
            requested += 1
            if row.topic_key == topic_key:
                satisfied += 1
        if requested == 0 or satisfied == requested:
            return 1.0
        if satisfied > 0:
            return 0.5
        return 0.0

    @staticmethod
    def _relevance(row: KnowledgeMemory, query: str) -> float:
        if not query:
            return 0.0
        score, _ = semantic_score(query, row.content or "", row.scope or "")
        return _clamp01(float(score))

    @staticmethod
    def _evidence_quality(row: KnowledgeMemory) -> float:
        evidence = len(row.evidence_ids or [])
        sources = len(row.source_ids or [])
        if evidence == 0 and sources == 0:
            return 0.0
        return min(1.0, (evidence + sources) / 3.0)

    @staticmethod
    def _freshness(row: KnowledgeMemory) -> float:
        return FRESHNESS_POINTS.get(effective_status(row), FRESHNESS_FALLBACK)

    @staticmethod
    def _confidence(row: KnowledgeMemory) -> float:
        try:
            value = float(row.confidence)
        except (TypeError, ValueError):
            return 0.0
        return _clamp01(value)

    @staticmethod
    def _usefulness(row: KnowledgeMemory) -> float:
        try:
            used = float(row.use_count)
        except (TypeError, ValueError):
            return 0.0
        return _clamp01(used / 10.0)

    def _score_row(
        self, row: KnowledgeMemory, *, relevance_query: str, platform: str, brand_id: str,
        topic: str, topic_key: str,
    ) -> dict:
        components = {
            "scope_match": self._scope_match(
                row, platform=platform, brand_id=brand_id, topic=topic, topic_key=topic_key
            ),
            "relevance": self._relevance(row, relevance_query),
            "evidence_quality": self._evidence_quality(row),
            "freshness": self._freshness(row),
            "confidence": self._confidence(row),
            "usefulness": self._usefulness(row),
        }
        total = 0.0
        for name, weight in WEIGHTS.items():
            total += weight * components[name]
        item = _memory_dict(row)
        item["_score"] = {"total": total, **components}
        return item

    # -- stage 4: semantic re-rank (mode-gated, mirrors advise_*) -------------

    @staticmethod
    def _semantic_rank(
        workspace_id: str, items: list[dict], task: str
    ) -> tuple[list[dict], str]:
        """Mode-gated DecisionEngine.rank re-rank; never raises.

        Returns (ordered items, semantic flag). DISABLED never constructs
        the engine; SHADOW calls it (record persists) but keeps the
        deterministic order; ASSISTED/PRIMARY apply the order only when it
        maps cleanly onto the candidate indices.
        """
        mode = decision_mode_for(workspace_id)
        if mode == "DISABLED":
            return items, "disabled"
        if not items:
            return items, "shadow" if mode == "SHADOW" else "applied"
        try:
            engine = DecisionEngine(workspace_id, mode=mode, persist=True)
            texts = [str(i.get("content", "") or i.get("topic", "")) for i in items]
            out, _ = engine.rank({"items": texts, "criterion": str(task or "")}, save=True)
            advisory = list((out or {}).get("order", [])) if isinstance(out, dict) else []
            if mode == "SHADOW":
                return items, "shadow"
            order = _map_order(advisory, len(items))
            if order is None:
                return items, "unmappable_output"
            return [items[i] for i in order], "applied"
        except Exception:  # noqa: BLE001 - belt-and-braces; engine already swallows
            return items, "error"

    # -- pipeline -------------------------------------------------------------

    def retrieve(
        self,
        db: Session,
        workspace_id: str,
        *,
        task: str = "",
        brand_id: str | None = None,
        platform: str | None = None,
        topic: str = "",
        content_format: str | None = None,
        time_window_days: int | None = 90,
        max_results: int = 10,
        types: list[str] | str | tuple[str, ...] | None = None,
    ) -> dict:
        """Rank this workspace's memories for one task (pipeline in docstring).

        Returns ``{"items": [row dict + "_score" breakdown], "metrics":
        {"considered", "filtered_hard", "ranked", "returned", "semantic"},
        "ranking": {"weights": {...}}}``. Read-only: no row mutation, no
        ``mark_used``, no commit — workspace isolation is a WHERE clause,
        never a score component.
        """
        # content_format: documented no-op (schema has no format column).
        max_results = max(1, min(int(max_results), MAX_RESULTS_CAP))
        platform = str(platform or "")
        brand_id = str(brand_id or "")
        topic = str(topic or "")
        topic_key = topic_key_of(topic) if topic else ""
        relevance_query = str(task or topic or "")
        type_set: set[str] | None = None
        if isinstance(types, str):
            type_set = {types}
        elif types is not None:
            type_set = {str(t) for t in types}

        now = utcnow()
        rows = list(
            db.scalars(
                select(KnowledgeMemory).where(KnowledgeMemory.workspace_id == workspace_id)
            ).all()
        )
        considered = len(rows)
        survivors = self._hard_filter(
            rows,
            now=now,
            time_window_days=time_window_days,
            types=type_set,
            platform=platform,
            brand_id=brand_id,
            topic=topic,
            topic_key=topic_key,
        )
        scored = [
            self._score_row(
                row,
                relevance_query=relevance_query,
                platform=platform,
                brand_id=brand_id,
                topic=topic,
                topic_key=topic_key,
            )
            for row in survivors
        ]
        # total DESC, then created_at DESC, then id ASC — fully deterministic.
        scored.sort(
            key=lambda item: (
                -item["_score"]["total"],
                -(_ts(item.get("created_at"))),
                str(item.get("id") or ""),
            )
        )
        ranked = len(scored)

        # Semantic stage over the top 3x candidates (mode-gated, never raises).
        candidate_count = max(3 * max_results, max_results)
        candidates = scored[: min(candidate_count, len(scored))]
        ordered, semantic_flag = self._semantic_rank(workspace_id, candidates, str(task or ""))
        items = ordered[:max_results]
        metrics = {
            "considered": considered,
            "filtered_hard": considered - ranked,
            "ranked": ranked,
            "returned": len(items),
            "semantic": semantic_flag,
        }
        return {"items": items, "metrics": metrics, "ranking": {"weights": dict(WEIGHTS)}}


def _ts(iso_value) -> float:
    """ISO timestamp → sortable float (missing/invalid timestamps sort last)."""
    if not iso_value:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso_value)).timestamp()
    except ValueError:
        return 0.0


__all__ = ["WEIGHTS", "MemoryRetriever"]
