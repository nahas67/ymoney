"""community_bridge — Work 10 Lane E: CommunityInsight → GlobalMemory.

Promotes Work 09 community insights into provenance-tracked
``COMMUNITY_INSIGHT`` memories (``docs/work10_contracts.md``, "Lane E") so the
audience feedback loop reaches the creation agents through
``context_bridge`` — this is the MEMORY path; the Opportunity path
(``engine/community/opportunity.py``) is untouched.

Rules (all tested in ``tests/test_knowledge_integration.py``):

* Source: a direct ``CommunityInsight`` query for the workspace with
  ``state != "dismissed"`` (it carries every field needed —
  ``related_content_ids``, ``state`` — which ``community_content_feedback``
  does not return). Insights with ``evidence_count < min_count`` are SKIPPED
  entirely (a single comment never becomes memory) but still reported with
  ``promoted=False`` for honest accounting.
* ``content`` is DEDUPE-STABLE: deterministic text from ``topic`` +
  ``representative_text`` (sensitive traits/contacts redacted the same way
  ``opportunity.py`` redacts evidence) — NO volatile counts inside, so a
  growing ``evidence_count`` can never produce a second row under Lane A's
  (type, topic_key, content_hash) dedupe.
* ``confidence`` maps the insight label → float: low→0.3, medium→0.6,
  high→0.9 (unknown labels fall back to 0.3).
* ``evidence_ids``: one ``{kind: "interaction", ref_id}`` per
  ``source_interaction_ids`` entry PLUS one ``{kind: "publication", ref_id}``
  per ``related_content_ids`` entry (source publications = ContentItem ids).
* ``platform`` = first platform (or ``""``), ``scope`` = ",".join(platforms).
* ``origin="community_agent"``, ``type="COMMUNITY_INSIGHT"``.

Idempotency + enrichment: Lane A's dedupe returns the EXISTING row without
merging new evidence, so after ``store()`` any current interaction/publication
id missing from the memory gets one ``KnowledgeEvidence`` row (pre-checked
against the UNIQUE ``(memory_id, kind, ref_id)``) plus the denormalized
``evidence_ids`` entry, and ``confidence`` is bumped when the mapped value is
higher. ``content`` / ``content_hash`` / ``topic_key`` are NEVER touched and a
second row is NEVER created.

Session discipline: flush only — this module NEVER commits; the caller (API or
test) commits. Every query is workspace-scoped; foreign insights are never
read or written.
"""
from __future__ import annotations

from sqlalchemy import select

from app.engine.community.classify import redact_sensitive
from app.engine.knowledge.memory import GlobalMemory
from app.models.base import utcnow
from app.models.community import CommunityInsight
from app.models.knowledge import KnowledgeEvidence, KnowledgeMemory

# insight label → stored confidence (documented mapping)
CONFIDENCE_MAP: dict[str, float] = {"low": 0.3, "medium": 0.6, "high": 0.9}
DEFAULT_LABEL_CONFIDENCE = 0.3

# the content prefix; stable across runs so dedupe hashes match
CONTENT_PREFIX = "Audience asked: "


def _mapped_confidence(label) -> float:
    """low→0.3 · medium→0.6 · high→0.9 (unknown/absent label → 0.3)."""
    try:
        return CONFIDENCE_MAP.get(str(label or "").strip().lower(), DEFAULT_LABEL_CONFIDENCE)
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        return DEFAULT_LABEL_CONFIDENCE


def _content_for(row: CommunityInsight) -> str:
    """Deterministic, count-free content (dedupe-stable across promotes)."""
    topic = str(row.topic or "").strip()
    sample = redact_sensitive(str(row.representative_text or "")).strip()
    if sample:
        return f"{CONTENT_PREFIX}{topic} — {sample}"
    return f"{CONTENT_PREFIX}{topic}"


def _evidence_for(row: CommunityInsight) -> list[dict]:
    """One dict per source interaction + one per source publication."""
    entries = [
        {"kind": "interaction", "ref_id": str(ref)}
        for ref in (row.source_interaction_ids or [])
        if str(ref or "").strip()
    ]
    entries += [
        {"kind": "publication", "ref_id": str(ref)}
        for ref in (row.related_content_ids or [])
        if str(ref or "").strip()
    ]
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for entry in entries:
        key = (entry["kind"], entry["ref_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _enrich(db, workspace_id, memory: dict, desired: list[dict], confidence: float) -> bool:
    """Merge evidence the dedupe path cannot: missing rows + denormalized ids.

    Pre-checks the UNIQUE ``(memory_id, kind, ref_id)`` key (never relies on
    IntegrityError) and bumps ``confidence`` upward only. Returns True when
    anything changed. Never touches content/content_hash/topic_key.
    """
    row = db.get(KnowledgeMemory, memory["id"])
    if row is None or row.workspace_id != workspace_id:
        return False
    existing = {
        (kind, ref)
        for kind, ref in db.execute(
            select(KnowledgeEvidence.kind, KnowledgeEvidence.ref_id).where(
                KnowledgeEvidence.workspace_id == workspace_id,
                KnowledgeEvidence.memory_id == row.id,
            )
        ).all()
    }
    denorm = list(row.evidence_ids or [])
    denorm_keys = {
        (str(e.get("kind") or ""), str(e.get("ref_id") or ""))
        for e in denorm
        if isinstance(e, dict)
    }
    changed = False
    for entry in desired:
        key = (entry["kind"], entry["ref_id"])
        if key not in existing:
            existing.add(key)
            db.add(
                KnowledgeEvidence(
                    workspace_id=workspace_id,
                    memory_id=row.id,
                    kind=entry["kind"],
                    ref_id=entry["ref_id"],
                    source_id="",
                    detail="",
                    confidence=1.0,
                    captured_at=utcnow(),
                )
            )
            changed = True
        if key not in denorm_keys:
            denorm_keys.add(key)
            denorm.append(
                {
                    "kind": entry["kind"],
                    "ref_id": entry["ref_id"],
                    "source_id": "",
                    "detail": "",
                    "confidence": 1.0,
                }
            )
            changed = True
    if changed:
        row.evidence_ids = denorm
    try:
        current = float(row.confidence or 0.0)
    except (TypeError, ValueError):
        current = 0.0
    if confidence > current:
        row.confidence = confidence
        changed = True
    if changed:
        db.flush()
    return changed


def promote_insights_to_memory(db, workspace_id, *, min_count=3) -> list[dict]:
    """Promote workspace insights at/above ``min_count`` into memory rows.

    Returns one dict per NON-DISMISSED insight, ordered by
    ``evidence_count DESC, insight_id ASC``::

        {"insight_id", "topic", "memory_id", "promoted": bool,
         "evidence_count", "confidence"}

    * ``promoted=True`` — a COMMUNITY_INSIGHT memory exists (created or the
      deduped existing row, enriched with any new evidence);
      ``memory_id`` is its id; ``confidence`` is the mapped value (low→0.3,
      medium→0.6, high→0.9); ``evidence_count`` is the insight's source
      interaction count.
    * ``promoted=False`` — insight below ``min_count`` (SKIPPED entirely:
      no memory row is ever created for it), ``memory_id`` is ``None``.

    Flush only, never commits; workspace isolation on every read/write.
    """
    min_count = int(min_count)
    rows = db.scalars(
        select(CommunityInsight)
        .where(
            CommunityInsight.workspace_id == workspace_id,
            CommunityInsight.state != "dismissed",
        )
        .order_by(CommunityInsight.evidence_count.desc(), CommunityInsight.id.asc())
    ).all()

    entries: list[dict] = []
    for row in rows:
        count = int(row.evidence_count or 0)
        confidence = _mapped_confidence(row.confidence)
        if count < min_count:
            entries.append(
                {
                    "insight_id": row.id,
                    "topic": row.topic,
                    "memory_id": None,
                    "promoted": False,
                    "evidence_count": count,
                    "confidence": confidence,
                }
            )
            continue

        platforms = [str(p) for p in (row.platforms or []) if str(p or "").strip()]
        desired = _evidence_for(row)
        memory = GlobalMemory.store(
            db,
            workspace_id,
            type="COMMUNITY_INSIGHT",
            content=_content_for(row),
            confidence=confidence,
            scope=",".join(platforms),
            topic=str(row.topic or ""),
            platform=platforms[0] if platforms else "",
            evidence_ids=desired,
            origin="community_agent",
        )
        _enrich(db, workspace_id, memory, desired, confidence)
        entries.append(
            {
                "insight_id": row.id,
                "topic": row.topic,
                "memory_id": memory["id"],
                "promoted": True,
                "evidence_count": count,
                "confidence": confidence,
            }
        )

    # rows arrive in (count DESC, id ASC) order and entries preserve it; sort
    # explicitly so the contract holds even if the source ordering changes.
    entries.sort(key=lambda e: (-int(e["evidence_count"]), str(e["insight_id"])))
    return entries


__all__ = ["CONFIDENCE_MAP", "promote_insights_to_memory"]
