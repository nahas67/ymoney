"""context_bridge — Work 10 Lane E: GlobalMemory → LLM prompt context.

Turns Lane A (``GlobalMemory``) + Lane B (``MemoryRetriever``) output into a
budgeted, provenance-tracked context block for the creation agents
(``docs/work10_contracts.md``, "Lane E — integration"):

1. **Retrieve** — ``MemoryRetriever().retrieve(...)`` (read-only ranked recall).
2. **Provenance/freshness pass** — SUPERSEDED/DISABLED candidates are dropped
   and counted into ``filtered`` (the retriever already excludes them, so this
   is a cheap re-check that normally drops nothing). UNVERIFIED is KEPT — it is
   already down-ranked by the retriever's freshness component, never removed.
   Every surviving item gains a computed ``effective_status``
   (``freshness.effective_status`` over a row-like view of the dict).
3. **Budget** — each candidate is added to a fresh Work 05
   ``ContextBudgetManager`` as ``Category.LONG_TERM_MEMORY`` and filtered via
   ``budget(items, max_tokens)``. The Work 05 caller pattern is mirrored
   exactly (``engine/community/draft.py:140-210``,
   ``api/v1/intelligence_routing.py:143-150``): ``budget()`` consumes the
   ``add()`` return list — budgeting the manager without its items would
   silently drop everything — and manager construction/add/budget errors are
   NOT swallowed here; isolation belongs to the agent caller
   (``creation._memory_block``).
4. **Record usage** — ``GlobalMemory.mark_used(db, workspace_id, kept_ids)``.

Token accounting: ``raw_tokens`` / ``kept_tokens`` / ``filtered_tokens`` /
``compression_ratio`` come verbatim from ``ContextBudgetManager.budget``'s own
metrics (``context_budget.py:272-282``, estimator ``estimate_tokens`` at :61)
— this module never re-counts characters. ``compression_ratio`` keeps the
manager's guard: ``kept / raw`` and ``1.0`` when ``raw_tokens == 0`` (an empty
store filtered nothing, so the ratio is vacuously 1.0), which also makes the
empty-store path return cleanly with zeroed counts.

Session discipline: when ``db`` is given it is used as-is (flush only — this
module NEVER calls ``commit()``; the caller commits). When ``db is None`` a
session is opened exactly the way ``app/services/memory.py`` does for its
no-session functions (``session_scope()``); writes inside are still flush-only
and the scope's own commit-on-exit is that helper's documented contract
(``app/db.py:50-60``).

The returned ``manager`` is NOT JSON-serializable for the API layer (contract
line 241) — tests and prompt builders consume ``items`` / ``metrics``.
"""
from __future__ import annotations

from datetime import datetime

from app.db import session_scope
from app.engine.intelligence.context_budget import Category, ContextBudgetManager
from app.engine.knowledge.freshness import (
    ACTIVE,
    DISABLED,
    SUPERSEDED,
    effective_status,
)
from app.engine.knowledge.memory import GlobalMemory
from app.engine.knowledge.retrieval import MemoryRetriever

# per-manager bridge state (recalls counter, metrics hook, memory ref index)
_STATE_ATTR = "_ym_context_bridge_state"

# lifecycle states the provenance pass drops (redundant with the retriever)
_DEAD_STATUSES = (SUPERSEDED, DISABLED)


def _ts(value) -> datetime | None:
    """ISO string / datetime → naive UTC datetime (``None`` when unparseable)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


class _RowView:
    """Row-like view over a retriever dict for ``freshness.effective_status``.

    The retriever returns plain dicts (no attributes), and the dict's ISO
    timestamps are strings — ``effective_status`` needs attribute access and
    real datetimes, so bridge them here instead of re-querying rows.
    """

    __slots__ = ("status", "last_verified_at", "created_at")

    def __init__(self, item: dict):
        self.status = str(item.get("status") or ACTIVE)
        self.last_verified_at = _ts(item.get("last_verified_at"))
        self.created_at = _ts(item.get("created_at"))


def _state(manager: ContextBudgetManager) -> dict:
    """Per-manager bridge state, attached lazily on first use."""
    state = getattr(manager, _STATE_ATTR, None)
    if state is None:
        state = {"recalls": 0, "metrics": None, "mem_index": {}}
        setattr(manager, _STATE_ATTR, state)
    return state


def build_memory_context(workspace_id, *, db=None, task="", brand_id=None, platform=None,
                         topic="", content_format=None, time_window_days=90,
                         max_results=10, max_tokens=2000) -> dict:
    """Budgeted, provenance-tracked memory context for one task.

    Returns exactly::

        {"metrics": {"retrieved", "used", "filtered", "recalled",
                     "raw_tokens", "kept_tokens", "filtered_tokens",
                     "compression_ratio"},
         "items": [kept memory dicts (all row fields + effective_status + _score)],
         "references": ["memory:<id>", ...],   # 1:1 with items, same order
         "memory_ids": [...all ids retrieved...],
         "used_memory_ids": [...kept ids...],
         "manager": ContextBudgetManager}

    * ``retrieved`` — candidates returned by ``retrieve()`` (before drops);
      ``used`` — kept after the budget; ``filtered`` — provenance re-check
      drops + budget drops (so ``retrieved == used + filtered``);
      ``recalled`` — starts at 0 and is updated IN PLACE when :func:`recall`
      finds a reference on this result's ``manager``.
    * ``compression_ratio`` — ``ContextBudgetManager.budget``'s guarded value
      (``kept/raw``; ``1.0`` when ``raw_tokens == 0``).
    * Empty store → all counts 0, empty lists, ``compression_ratio == 1.0``,
      a usable (empty) ``manager`` — returned cleanly, no exception.
    * Workspace isolation is the retriever's WHERE clause; foreign ids can
      never appear. Read-only except ``mark_used`` on the KEPT ids (flush
      only; the caller — or the ``session_scope`` opened here for
      ``db=None`` — owns the commit).
    """
    if db is not None:
        return _build(
            db, workspace_id, task=task, brand_id=brand_id, platform=platform,
            topic=topic, content_format=content_format,
            time_window_days=time_window_days, max_results=max_results,
            max_tokens=max_tokens,
        )
    with session_scope() as session:
        return _build(
            session, workspace_id, task=task, brand_id=brand_id, platform=platform,
            topic=topic, content_format=content_format,
            time_window_days=time_window_days, max_results=max_results,
            max_tokens=max_tokens,
        )


def _build(db, workspace_id, *, task, brand_id, platform, topic, content_format,
           time_window_days, max_results, max_tokens) -> dict:
    result = MemoryRetriever().retrieve(
        db,
        workspace_id,
        task=task,
        brand_id=brand_id,
        platform=platform,
        topic=topic,
        content_format=content_format,
        time_window_days=time_window_days,
        max_results=max_results,
    )
    candidates = list(result.get("items") or [])
    retrieved = len(candidates)

    # provenance/freshness pass: dead states dropped (counted into filtered),
    # UNVERIFIED kept (already down-ranked); every survivor gains
    # effective_status so prompt builders can show it.
    survivors: list[dict] = []
    recheck_drops = 0
    for item in candidates:
        status = effective_status(_RowView(item))
        item["effective_status"] = status
        if status in _DEAD_STATUSES:
            recheck_drops += 1
            continue
        survivors.append(item)

    # Work 05 caller pattern: keep the add() return values and hand them to
    # budget() explicitly — errors here propagate (failure isolation lives in
    # the agent caller, never silently swallowed in the bridge).
    manager = ContextBudgetManager()
    added = []
    by_ctx_id: dict[str, dict] = {}
    for item in survivors:
        ctx_item = manager.add(str(item.get("content") or ""), Category.LONG_TERM_MEMORY)
        added.append(ctx_item)
        by_ctx_id[ctx_item.id] = item
    budget = manager.budget(added, max_tokens=int(max_tokens))

    items: list[dict] = []
    used_memory_ids: list[str] = []
    references: list[str] = []
    for kept in budget.kept:
        item = by_ctx_id.get(str(kept.get("id") or ""))
        if item is None:  # not ours — defensive; never fabricate entries
            continue
        items.append(item)
        memory_id = str(item.get("id") or "")
        used_memory_ids.append(memory_id)
        references.append(f"memory:{memory_id}")

    filtered = recheck_drops + len(budget.references)
    memory_ids = [str(item.get("id") or "") for item in candidates]

    # flush only — this module never commits (see module docstring).
    GlobalMemory.mark_used(db, workspace_id, used_memory_ids)

    bmetrics = budget.metrics
    metrics = {
        "retrieved": retrieved,
        "used": len(used_memory_ids),
        "filtered": filtered,
        "recalled": 0,
        "raw_tokens": bmetrics["raw_tokens"],
        "kept_tokens": bmetrics["kept_tokens"],
        "filtered_tokens": bmetrics["filtered_tokens"],
        "compression_ratio": bmetrics["compression_ratio"],
    }

    state = _state(manager)
    state["metrics"] = metrics
    # index every held memory (kept AND budget-hidden — the manager holds them
    # all), so "memory:<id>" / bare-id recalls work for hidden items too.
    mem_index: dict[str, dict] = {}
    for item in survivors:
        memory_id = str(item.get("id") or "")
        if memory_id:
            mem_index[memory_id] = item
            mem_index[f"memory:{memory_id}"] = item
    state["mem_index"] = mem_index

    return {
        "metrics": metrics,
        "items": items,
        "references": references,
        "memory_ids": memory_ids,
        "used_memory_ids": used_memory_ids,
        "manager": manager,
    }


def recall(ref_id, manager) -> dict:
    """Recall one reference from a bridge manager's held items.

    Lookup order (never raises, even for unknown/``None`` ids):

    1. ``ref_id`` as a raw ``ContextItem`` id — restored through the manager's
       public ``manager.recall()``; ``item`` is that ContextItem as a dict
       (``{id, content, category, pinned, tokens, seq}``).
    2. ``ref_id`` as a memory reference — ``"memory:<memory_id>"`` or the bare
       ``memory_id`` — resolved through the index attached by
       :func:`build_memory_context`; ``item`` is the full memory dict (all row
       fields).

    Returns ``{"ref_id", "found": bool, "recalled": int, "item": dict | None}``
    where ``recalled`` is the PER-MANAGER running counter (lazily attached,
    starts at 0): it increments ONLY when the reference is found, so a miss
    returns the previous count unchanged. A hit also updates
    ``metrics["recalled"]`` in place on the dict returned by the build that
    attached this manager — that is how ``metrics["recalled"]`` consumers see
    the count.
    """
    state = _state(manager)
    item = None
    try:
        item = manager.recall(ref_id).to_dict()
    except KeyError:
        item = None
    if item is None:
        item = state["mem_index"].get(ref_id)
    if item is None:
        return {"ref_id": ref_id, "found": False, "recalled": state["recalls"], "item": None}
    state["recalls"] += 1
    metrics = state.get("metrics")
    if isinstance(metrics, dict):
        metrics["recalled"] = state["recalls"]
    return {"ref_id": ref_id, "found": True, "recalled": state["recalls"], "item": item}


__all__ = ["build_memory_context", "recall"]
