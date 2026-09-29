"""GlobalMemory — the Work 10 knowledge store (Lane A).

Typed, provenance-tracked, workspace-isolated memories with a service-level
lifecycle (``ACTIVE`` | ``UNVERIFIED`` | ``CONFLICTED`` | ``SUPERSEDED`` |
``DISABLED``) and a freshness band recomputed on read (``FRESH`` | ``AGING``
| ``STALE`` — see ``freshness.py``).

Contract highlights (``docs/work10_contracts.md``, "Lane A"):

* Dedupe is a SERVICE concern: a non-SUPERSEDED row with the same
  (workspace_id, type, topic_key, content_hash) is returned as-is — never a
  second row, never a second batch of evidence rows.
* Missing provenance (empty ``evidence_ids`` AND empty ``source_ids``) stores
  the row as ``UNVERIFIED``; at insert ``CONFLICTED`` beats ``UNVERIFIED``
  beats ``ACTIVE`` (``freshness.STATUS_PRECEDENCE``).
* Conflicting facts are preserved side by side: both rows go ``CONFLICTED``
  under one ``conflict_group`` — nothing is ever deleted or overwritten.
* Reads recompute the freshness band / effective status from
  ``last_verified_at or created_at`` and persist the band when it changed
  (flush only — this service NEVER commits; the API/tests commit).
* ``related_json`` is service-owned: ``store()`` does not accept it (rows
  default to ``{}``) and no credential/OAuth/PII field exists in any
  signature here.
* Every query hard-filters ``workspace_id``. Foreign ids behave as
  not-found: ``get`` returns ``None``; ``verify`` / ``disable`` /
  ``supersede`` raise ``ValueError``.

Session discipline mirrors ``app.services.memory`` (flush inside, commit
outside). Returned dicts are JSON-serializable — datetimes become ISO-8601
strings with a ``"Z"`` suffix (``None`` when unset), matching the dto style
used across ``api/v1`` and ``services``.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select

from app.db import escape_like
from app.engine.knowledge.freshness import (
    ACTIVE,
    AGING,
    AGING_DAYS,
    BANDS,
    CONFLICTED,
    DISABLED,
    FRESH,
    FRESH_DAYS,
    LIFECYCLE_STATUSES,
    STATUS_PRECEDENCE,
    SUPERSEDED,
    UNVERIFIED,
    effective_status,
    freshness_band,
)
from app.engine.knowledge.normalize import content_hash_of, topic_key_of
from app.models import KnowledgeEvidence, KnowledgeMemory
from app.models.base import utcnow

TYPES: tuple[str, ...] = (
    "RESEARCH_FACT",
    "SOURCE",
    "CONTENT_RESULT",
    "AUDIENCE_INSIGHT",
    "COMMUNITY_INSIGHT",
    "BRAND_KNOWLEDGE",
    "CREATIVE_LESSON",
    "EXPERIMENT_RESULT",
    "PLATFORM_LEARNING",
    "ENTITY",
    "RELATIONSHIP",
    "USER_APPROVED_KNOWLEDGE",
)
# auto-conflict types: same (workspace, type, topic_key) + different hash
FACT_CONFLICT_TYPES: tuple[str, ...] = ("RESEARCH_FACT",)
EVIDENCE_KINDS: tuple[str, ...] = (
    "evidence_record",
    "publication",
    "interaction",
    "source_document",
    "agent_run",
    "user",
    "metric",
    "research_claim",
)

MAX_LIMIT = 200


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 + Z — the dto convention used across api/v1 and services."""
    return value.isoformat() + "Z" if value else None


def _confidence(value, default: float = 0.5) -> float:
    """Clamp to [0, 1]; unparseable input falls back to ``default``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _as_source_list(value) -> list[str]:
    """Provenance list input: non-list (or empty) → ``[]``, never raises."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip() if item is not None else ""
        if text:
            out.append(text)
    return out


def _jsonable(mapping: dict) -> dict:
    """Shallow-coerce a dict so the row is always JSON-serializable."""
    safe = {}
    for key, val in mapping.items():
        if isinstance(val, (str, int, float, bool)) or val is None:
            safe[str(key)] = val
        else:
            safe[str(key)] = str(val)
    return safe


def _as_evidence_list(value) -> list:
    """Evidence list input: non-list → ``[]``; dicts JSON-coerced, never raises."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list = []
    for item in value:
        if isinstance(item, dict):
            out.append(_jsonable(item))
        elif item is None:
            continue
        else:
            text = str(item)
            if text.strip():
                out.append(text)
    return out


def _evidence_entries(sources: list[str], evidence: list) -> list[dict]:
    """Normalize store inputs into KnowledgeEvidence row payloads.

    * every source id → ``kind="source_document"`` (ref_id = source_id = id)
    * a plain string → ``kind="evidence_record"`` (ref_id = the string)
    * a dict ``{kind, ref_id, source_id, detail, confidence}`` is honored,
      unknown ``kind`` falls back to ``"evidence_record"``.
    """
    entries: list[dict] = []
    for sid in sources:
        entries.append(
            {
                "kind": "source_document",
                "ref_id": sid[:250],
                "source_id": sid[:36],
                "detail": "",
                "confidence": 1.0,
            }
        )
    for item in evidence:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "evidence_record")
            if kind not in EVIDENCE_KINDS:
                kind = "evidence_record"
            entries.append(
                {
                    "kind": kind[:40],
                    "ref_id": str(item.get("ref_id") or "")[:250],
                    "source_id": str(item.get("source_id") or "")[:36],
                    "detail": str(item.get("detail") or ""),
                    "confidence": _confidence(item.get("confidence"), 1.0),
                }
            )
        else:
            entries.append(
                {
                    "kind": "evidence_record",
                    "ref_id": str(item)[:250],
                    "source_id": "",
                    "detail": "",
                    "confidence": 1.0,
                }
            )
    return entries


def _load(db, workspace_id, memory_id) -> KnowledgeMemory | None:
    """Workspace-scoped fetch: foreign/missing ids read as not-found."""
    if not memory_id:
        return None
    return db.scalars(
        select(KnowledgeMemory).where(
            KnowledgeMemory.workspace_id == workspace_id,
            KnowledgeMemory.id == str(memory_id),
        )
    ).first()


def _to_dict(db, row: KnowledgeMemory, *, now: datetime | None = None) -> dict:
    """JSON-safe read model; recomputes freshness (persisting it on change)."""
    band = freshness_band(row, now)
    if row.freshness != band:
        # recomputed-on-read: persist the band (flush only, never commit)
        row.freshness = band
        db.flush()
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
        "freshness": band,
        "status": row.status,
        "effective_status": effective_status(row, now),
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


def _insert_evidence(db, workspace_id, memory_id, entries: list[dict]) -> None:
    """Append evidence rows, pre-checking the UNIQUE (memory, kind, ref) key.

    Duplicates (already present, or repeated inside one call) are skipped —
    we never rely on IntegrityError for control flow.
    """
    if not entries:
        return
    seen = {
        (kind, ref)
        for kind, ref in db.execute(
            select(KnowledgeEvidence.kind, KnowledgeEvidence.ref_id).where(
                KnowledgeEvidence.workspace_id == workspace_id,
                KnowledgeEvidence.memory_id == memory_id,
            )
        ).all()
    }
    for entry in entries:
        key = (entry["kind"], entry["ref_id"])
        if key in seen:
            continue
        seen.add(key)
        db.add(
            KnowledgeEvidence(
                workspace_id=workspace_id,
                memory_id=memory_id,
                kind=entry["kind"],
                ref_id=entry["ref_id"],
                source_id=entry["source_id"],
                detail=entry["detail"],
                confidence=entry["confidence"],
                captured_at=utcnow(),
            )
        )
    db.flush()


def _mark_superseded(target: KnowledgeMemory, replacement_id: str) -> None:
    """Supersede in place: status + pointer; content/history stay untouched."""
    target.status = SUPERSEDED
    target.superseded_by = replacement_id


class GlobalMemory:
    """Workspace-scoped knowledge store (all methods flush, never commit)."""

    @staticmethod
    def store(
        db,
        workspace_id,
        *,
        type,
        content,
        confidence=0.5,
        scope="",
        brand_id=None,
        topic="",
        platform="",
        source_ids=None,
        evidence_ids=None,
        origin="",
        supersedes=None,
        conflicts_with=None,
    ) -> dict:
        """Insert (or idempotently return) one memory row.

        Order of operations: validate → normalize keys → validate supersede
        target → dedupe → resolve conflicts (explicit then auto) → status by
        ``STATUS_PRECEDENCE`` → insert → evidence rows → supersession.

        Raises ``ValueError`` for an unknown ``type`` (message lists
        ``TYPES``), empty ``content``, an unknown/foreign ``conflicts_with``
        or ``supersedes`` target, or a SUPERSEDED conflict target.
        """
        if type not in TYPES:
            raise ValueError(
                f"unknown memory type {type!r}; expected one of TYPES: {', '.join(TYPES)}"
            )
        text = str(content) if content is not None else ""
        if not text.strip():
            raise ValueError("content must not be empty")

        content_hash = content_hash_of(text)
        topic_text = str(topic or "")[:200]
        topic_key = topic_key_of(topic_text)
        sources = _as_source_list(source_ids)
        evidence = _as_evidence_list(evidence_ids)
        provenanced = bool(sources or evidence)

        # supersede target: validate BEFORE any insert so bad input never
        # leaves a half-written row behind.
        sup_target = None
        if supersedes:
            sup_target = _load(db, workspace_id, supersedes)
            if sup_target is None:
                raise ValueError(f"supersede target not found in workspace: {supersedes}")
            if sup_target.status == SUPERSEDED:
                raise ValueError(f"memory already superseded: {supersedes}")

        # idempotent store: same (ws, type, topic_key, content_hash) and not
        # SUPERSEDED → return the existing row, insert nothing (no evidence).
        existing = db.scalars(
            select(KnowledgeMemory)
            .where(
                KnowledgeMemory.workspace_id == workspace_id,
                KnowledgeMemory.type == type,
                KnowledgeMemory.topic_key == topic_key,
                KnowledgeMemory.content_hash == content_hash,
                KnowledgeMemory.status != SUPERSEDED,
            )
            .order_by(KnowledgeMemory.created_at.asc(), KnowledgeMemory.id.asc())
        ).first()
        if existing is not None:
            if sup_target is not None:
                if sup_target.id == existing.id:
                    raise ValueError("cannot supersede a memory with itself")
                _mark_superseded(sup_target, existing.id)
                db.flush()
            return _to_dict(db, existing)

        conflict_group = ""
        conflict_found = False
        if conflicts_with:
            # explicit conflict: both rows keep their content under one group
            target = _load(db, workspace_id, conflicts_with)
            if target is None:
                raise ValueError(f"conflict target not found in workspace: {conflicts_with}")
            if target.status == SUPERSEDED:
                raise ValueError(f"cannot conflict with a superseded memory: {conflicts_with}")
            conflict_group = target.conflict_group or f"cg_{target.id}"
            target.status = CONFLICTED
            target.conflict_group = conflict_group
            conflict_found = True
        elif type in FACT_CONFLICT_TYPES and topic_key:
            # auto-conflict: competing facts on the same topic. Same-hash rows
            # never conflict (dedupe caught them above); an empty topic_key
            # never auto-conflicts (unrelated statements, not rivals).
            rivals_stmt = select(KnowledgeMemory).where(
                KnowledgeMemory.workspace_id == workspace_id,
                KnowledgeMemory.type == type,
                KnowledgeMemory.topic_key == topic_key,
                KnowledgeMemory.content_hash != content_hash,
                KnowledgeMemory.status.in_((ACTIVE, UNVERIFIED, CONFLICTED)),
            )
            if sup_target is not None:
                # a row this store supersedes is replaced, not contested —
                # it is about to become SUPERSEDED (outside the rival pool).
                rivals_stmt = rivals_stmt.where(KnowledgeMemory.id != sup_target.id)
            rivals = db.scalars(
                rivals_stmt.order_by(
                    KnowledgeMemory.created_at.asc(), KnowledgeMemory.id.asc()
                )
            ).all()
            if rivals:
                conflict_group = next(
                    (r.conflict_group for r in rivals if r.conflict_group), ""
                ) or f"cg_{rivals[0].id}"
                for rival in rivals:
                    rival.status = CONFLICTED
                    rival.conflict_group = conflict_group
                conflict_found = True

        # STATUS_PRECEDENCE: CONFLICTED beats UNVERIFIED beats ACTIVE.
        flags: set[str] = set()
        if conflict_found:
            flags.add(CONFLICTED)
        if not provenanced:
            flags.add(UNVERIFIED)
        if not flags:
            flags.add(ACTIVE)
        status = next(state for state in STATUS_PRECEDENCE if state in flags)

        row = KnowledgeMemory(
            workspace_id=workspace_id,
            brand_id=brand_id or None,
            type=type,
            content=text,
            topic=topic_text,
            topic_key=topic_key,
            scope=str(scope or "")[:120],
            platform=str(platform or "")[:40],
            source_ids=list(sources),
            evidence_ids=list(evidence),
            confidence=_confidence(confidence, 0.5),
            freshness=FRESH,
            status=status,
            origin=str(origin or "")[:80],
            content_hash=content_hash,
            conflict_group=conflict_group,
            last_verified_at=None,
            superseded_by=None,
            use_count=0,
            last_used_at=None,
            related_json={},
        )
        db.add(row)
        db.flush()

        _insert_evidence(db, workspace_id, row.id, _evidence_entries(sources, evidence))
        if sup_target is not None:
            _mark_superseded(sup_target, row.id)
        db.flush()
        return _to_dict(db, row)

    @staticmethod
    def get(db, workspace_id, memory_id) -> dict | None:
        """One memory as a JSON-safe dict, or ``None`` when foreign/missing."""
        row = _load(db, workspace_id, memory_id)
        return _to_dict(db, row) if row is not None else None

    @staticmethod
    def list(
        db,
        workspace_id,
        *,
        type=None,
        status=None,
        scope=None,
        topic=None,
        q=None,
        limit=50,
    ) -> list[dict]:
        """Filtered, deterministic listing: ``created_at DESC, id ASC``.

        * ``topic`` matches on the normalized ``topic_key`` (case/space
          insensitive, same key dedupe uses); ``q`` is a case-insensitive
          substring match over content + topic (LIKE wildcards escaped via
          ``app.db.escape_like``).
        * ``status`` filter choice (documented): a LIFECYCLE value
          (ACTIVE/UNVERIFIED/CONFLICTED/SUPERSEDED/DISABLED) matches the
          ``status`` column — which is also the effective status for every
          non-ACTIVE value (for ACTIVE the effective status is the freshness
          band, so a lifecycle match is the meaningful one). A BAND value
          (FRESH/AGING/STALE) matches the *computed* effective status of
          ACTIVE rows (SQL anchor = COALESCE(last_verified_at, created_at),
          re-checked per row at read time). Unknown values → no rows.
        * ``limit`` is clamped to 1..200.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        now = utcnow()
        stmt = select(KnowledgeMemory).where(KnowledgeMemory.workspace_id == workspace_id)
        if type:
            stmt = stmt.where(KnowledgeMemory.type == type)
        if scope:
            stmt = stmt.where(KnowledgeMemory.scope == scope)
        if topic:
            stmt = stmt.where(KnowledgeMemory.topic_key == topic_key_of(topic))
        if q:
            needle = f"%{escape_like(q.lower())}%"
            stmt = stmt.where(
                or_(
                    KnowledgeMemory.content.ilike(needle, escape="\\"),
                    KnowledgeMemory.topic.ilike(needle, escape="\\"),
                )
            )

        want_band: str | None = None
        if status:
            if status in LIFECYCLE_STATUSES:
                stmt = stmt.where(KnowledgeMemory.status == status)
            elif status in BANDS:
                want_band = status
                anchor = func.coalesce(
                    KnowledgeMemory.last_verified_at, KnowledgeMemory.created_at
                )
                fresh_cut = now - timedelta(days=FRESH_DAYS)
                aging_cut = now - timedelta(days=AGING_DAYS)
                stmt = stmt.where(KnowledgeMemory.status == ACTIVE)
                if status == FRESH:
                    stmt = stmt.where(anchor > fresh_cut)
                elif status == AGING:
                    stmt = stmt.where(anchor <= fresh_cut, anchor > aging_cut)
                else:  # STALE
                    stmt = stmt.where(anchor <= aging_cut)
            else:
                return []

        stmt = stmt.order_by(KnowledgeMemory.created_at.desc(), KnowledgeMemory.id.asc())
        rows = db.scalars(stmt.limit(limit)).all()
        out: list[dict] = []
        for row in rows:
            item = _to_dict(db, row, now=now)
            if want_band is not None and item["effective_status"] != want_band:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def verify(db, workspace_id, memory_id, *, user_id="") -> dict:
        """Record a human verification: refresh age anchor + user evidence.

        ``UNVERIFIED → ACTIVE``; ``CONFLICTED`` / ``SUPERSEDED`` / ``DISABLED``
        stay unchanged (verification never silently erases a conflict,
        supersession or disable) and ``ACTIVE`` stays ``ACTIVE``. Appends one
        ``kind="user"`` evidence row (skipped when already present).
        Foreign/missing id → ``ValueError``.
        """
        row = _load(db, workspace_id, memory_id)
        if row is None:
            raise ValueError(f"memory not found in workspace: {memory_id}")
        row.last_verified_at = utcnow()
        if row.status == UNVERIFIED:
            row.status = ACTIVE
        _insert_evidence(
            db,
            workspace_id,
            row.id,
            [
                {
                    "kind": "user",
                    "ref_id": str(user_id or "")[:250],
                    "source_id": "",
                    "detail": "",
                    "confidence": 1.0,
                }
            ],
        )
        db.flush()
        return _to_dict(db, row)

    @staticmethod
    def disable(db, workspace_id, memory_id) -> dict:
        """Set ``DISABLED`` (row is never deleted).

        Idempotent when already ``DISABLED``; a ``SUPERSEDED`` row is left
        unchanged. Foreign/missing id → ``ValueError``.
        """
        row = _load(db, workspace_id, memory_id)
        if row is None:
            raise ValueError(f"memory not found in workspace: {memory_id}")
        if row.status not in (SUPERSEDED, DISABLED):
            row.status = DISABLED
            db.flush()
        return _to_dict(db, row)

    @staticmethod
    def supersede(db, workspace_id, target_id, *, replacement_id) -> dict:
        """Point ``target`` at ``replacement``: status ``SUPERSEDED``.

        Target must exist in this workspace and not already be SUPERSEDED;
        replacement must exist here and be a different row. Content and
        history are preserved. Foreign/missing/duplicate id → ``ValueError``.
        """
        target = _load(db, workspace_id, target_id)
        if target is None:
            raise ValueError(f"memory not found in workspace: {target_id}")
        if target.status == SUPERSEDED:
            raise ValueError(f"memory already superseded: {target_id}")
        replacement = _load(db, workspace_id, replacement_id)
        if replacement is None:
            raise ValueError(f"replacement memory not found in workspace: {replacement_id}")
        if replacement.id == target.id:
            raise ValueError("target and replacement must be different rows")
        _mark_superseded(target, replacement.id)
        db.flush()
        return _to_dict(db, target)

    @staticmethod
    def mark_used(db, workspace_id, memory_ids) -> None:
        """``use_count += 1`` + ``last_used_at`` for ids owned by this workspace.

        Foreign / missing / non-string ids are silently skipped — this never
        raises and never touches another workspace's rows.
        """
        if isinstance(memory_ids, str):
            memory_ids = [memory_ids]
        if not isinstance(memory_ids, (list, tuple, set)):
            return
        now = utcnow()
        changed = False
        for memory_id in memory_ids:
            if not isinstance(memory_id, str) or not memory_id:
                continue
            row = db.get(KnowledgeMemory, memory_id)
            if row is None or row.workspace_id != workspace_id:
                continue
            row.use_count = (row.use_count or 0) + 1
            row.last_used_at = now
            changed = True
        if changed:
            db.flush()


__all__ = [
    "EVIDENCE_KINDS",
    "FACT_CONFLICT_TYPES",
    "GlobalMemory",
    "TYPES",
]
