"""ContextBudgetManager (Work 05, Lane B): raw → chunk → classify → keep/hide.

Pipeline stages, mirroring the evaluated OSS concepts (winnow's keep/hide +
recall stubs, fast-claude-compaction's verbatim-keep with pinning) but
implemented locally with deterministic heuristics — no network, no judge
model, no new dependencies:

- ``chunk`` splits oversized content into exact, recallable slices.
- ``classify`` assigns a :class:`Category` with keyword heuristics.
- ``budget`` keeps what fits in ``max_tokens`` and hides the rest behind
  :class:`ContextReference` stubs. Pinned categories (user instructions,
  current objectives, safety constraints, acceptance criteria) are NEVER
  filtered, even over budget.
- ``recall`` restores the exact original by reference id (reversible).

Token counting is a deterministic character estimator (no tokenizer, no
network). Metrics (raw/kept/filtered tokens, recalls, compression ratio,
latency, cost) are reported with every run.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Category(StrEnum):
    SYSTEM_CRITICAL = "SYSTEM_CRITICAL"
    TASK_REQUIRED = "TASK_REQUIRED"
    RECENT_WORKING = "RECENT_WORKING"
    LONG_TERM_MEMORY = "LONG_TERM_MEMORY"
    TOOL_RESULT = "TOOL_RESULT"
    RESEARCH_SOURCE = "RESEARCH_SOURCE"
    HISTORICAL = "HISTORICAL"
    LOW_RELEVANCE = "LOW_RELEVANCE"


# Categories that are hard-pinned: user instructions, the current objective,
# safety constraints, and acceptance criteria always survive filtering.
PREDEFINED_PINNED_CATEGORIES = frozenset({Category.SYSTEM_CRITICAL, Category.TASK_REQUIRED})

# Lower wins when competing for the token budget.
_PRIORITY = {
    Category.SYSTEM_CRITICAL: 0,
    Category.TASK_REQUIRED: 1,
    Category.RECENT_WORKING: 2,
    Category.LONG_TERM_MEMORY: 3,
    Category.TOOL_RESULT: 4,
    Category.RESEARCH_SOURCE: 5,
    Category.HISTORICAL: 6,
    Category.LOW_RELEVANCE: 7,
}

_CHARS_PER_TOKEN = 4
_SUMMARY_CHARS = 120


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate: ceil(chars / 4), minimum 1 for text."""
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


# Heuristic classification rules, first match wins. Documented limitation:
# these are cheap keyword signals, not semantic judgments; callers with
# ground truth should pass explicit categories to ``add``.
_CLASSIFY_RULES: list[tuple[Category, re.Pattern[str]]] = [
    (
        Category.SYSTEM_CRITICAL,
        re.compile(
            r"\b(acceptance criteria|must never|never do|safety|policy|"
            r"constraint|forbidden|required approval)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.TASK_REQUIRED,
        re.compile(
            r"\b(objective|goal|instruction|requirement|must |required|"
            r"deliverable|user asks|user wants)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.TOOL_RESULT,
        re.compile(
            r"\b(traceback|test (output|result|failure)|tool (output|result)|"
            r"exit code|stderr|assertionerror)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.RESEARCH_SOURCE,
        re.compile(
            r"\b(according to|source:|citation|research|study shows|"
            r"report finds|statistics?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.LONG_TERM_MEMORY,
        re.compile(
            r"\b(remember|learned|pattern|preference|always |usually|"
            r"historical fact|known that)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.HISTORICAL,
        re.compile(
            r"\b(yesterday|last week|previous|earlier|archived|old version)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Category.LOW_RELEVANCE,
        re.compile(r"\b(lorem ipsum|filler|placeholder|duplicate|ignore this)\b", re.IGNORECASE),
    ),
]


def classify(text: str) -> Category:
    """Heuristic category for free text. Defaults to RECENT_WORKING."""
    for category, pattern in _CLASSIFY_RULES:
        if pattern.search(text or ""):
            return category
    return Category.RECENT_WORKING


def _summarize(content: str) -> str:
    flat = " ".join((content or "").split())
    if len(flat) <= _SUMMARY_CHARS:
        return flat
    return flat[:_SUMMARY_CHARS].rstrip() + "…"


@dataclass
class ContextItem:
    id: str
    content: str
    category: Category
    pinned: bool
    tokens: int
    seq: int

    def to_dict(self) -> dict:
        data = asdict(self)
        data["category"] = self.category.value
        return data


@dataclass
class ContextReference:
    """Stub for hidden content: what was hidden and how to get it back."""

    ref_id: str
    category: str
    summary: str
    tokens: int  # original token count of the hidden content

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BudgetResult:
    kept: list[dict] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kept": self.kept, "references": self.references, "metrics": self.metrics}


class ContextBudgetManager:
    """In-memory reversible context filtering with metrics."""

    def __init__(self):
        self._store: dict[str, ContextItem] = {}
        self._seq = 0
        self._recalls = 0

    # -- ingest ----------------------------------------------------------
    def add(
        self,
        content: str,
        category: Category | str | None = None,
        pinned: bool | None = None,
    ) -> ContextItem:
        cat = self._coerce_category(category, content)
        is_pinned = cat in PREDEFINED_PINNED_CATEGORIES if pinned is None else bool(pinned)
        # Pinned categories cannot be unpinned: safety constraints and user
        # instructions survive even when a caller passes pinned=False.
        if cat in PREDEFINED_PINNED_CATEGORIES:
            is_pinned = True
        self._seq += 1
        item = ContextItem(
            id=uuid.uuid4().hex[:16],
            content=content or "",
            category=cat,
            pinned=is_pinned,
            tokens=estimate_tokens(content or ""),
            seq=self._seq,
        )
        self._store[item.id] = item
        return item

    def chunk(
        self,
        text: str,
        max_chars: int = 2000,
        category: Category | str | None = None,
        pinned: bool | None = None,
    ) -> list[ContextItem]:
        """Split oversized text into exact slices; each slice is recallable."""
        text = text or ""
        parts = [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        cat = self._coerce_category(category, text)
        return [self.add(part, category=cat, pinned=pinned) for part in parts]

    @staticmethod
    def _coerce_category(category: Category | str | None, content: str) -> Category:
        if isinstance(category, Category):
            return category
        if isinstance(category, str):
            try:
                return Category(category.upper())
            except ValueError:
                pass
        return classify(content)

    # -- filter ----------------------------------------------------------
    def budget(
        self,
        items: list[ContextItem | dict] | None = None,
        max_tokens: int = 4000,
    ) -> BudgetResult:
        """Keep what fits; hide the rest behind references. Pinned always kept."""
        started = time.perf_counter()
        resolved = [self._resolve(item) for item in (items or [])]
        resolved = [item for item in resolved if item is not None]

        raw_tokens = sum(item.tokens for item in resolved)
        ordered = sorted(resolved, key=lambda it: (_PRIORITY[it.category], it.seq))

        kept: list[ContextItem] = []
        hidden: list[ContextItem] = []
        used = 0
        for item in ordered:
            if item.pinned or used + item.tokens <= max_tokens:
                kept.append(item)
                used += item.tokens
            else:
                hidden.append(item)

        references = [
            ContextReference(
                ref_id=item.id,
                category=item.category.value,
                summary=_summarize(item.content),
                tokens=item.tokens,
            ).to_dict()
            for item in hidden
        ]
        kept_tokens = sum(item.tokens for item in kept)
        filtered_tokens = sum(item.tokens for item in hidden)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        metrics = {
            "raw_tokens": raw_tokens,
            "kept_tokens": kept_tokens,
            "filtered_tokens": filtered_tokens,
            "recalls": self._recalls,
            "compression_ratio": round(kept_tokens / raw_tokens, 4) if raw_tokens else 1.0,
            "latency_ms": latency_ms,
            "cost_usd": self._estimate_cost(kept_tokens),
            "max_tokens": max_tokens,
            "over_budget": used > max_tokens,  # pinned content can force overage
        }
        return BudgetResult(
            kept=[item.to_dict() for item in kept],
            references=references,
            metrics=metrics,
        )

    def run_batch(
        self,
        batches: list[list[ContextItem | dict]],
        max_tokens: int = 4000,
    ) -> list[BudgetResult]:
        """Filter several independent batches; every hidden item stays recallable."""
        return [self.budget(batch, max_tokens=max_tokens) for batch in batches]

    # -- recall ----------------------------------------------------------
    def recall(self, ref_id: str) -> ContextItem:
        """Restore the exact original content for a reference id."""
        item = self._store.get(ref_id)
        if item is None:
            raise KeyError(f"unknown context reference {ref_id!r}")
        self._recalls += 1
        return item

    @property
    def recalls(self) -> int:
        return self._recalls

    def _resolve(self, item: ContextItem | dict) -> ContextItem | None:
        if isinstance(item, ContextItem):
            self._store[item.id] = item
            return item
        if isinstance(item, dict):
            content = item.get("content", "")
            category = item.get("category")
            pinned = item.get("pinned")
            ref_id = item.get("id") or item.get("ref_id")
            if ref_id and ref_id in self._store:
                return self._store[ref_id]
            return self.add(content, category=category, pinned=pinned)
        return None

    @staticmethod
    def _estimate_cost(kept_tokens: int) -> float:
        try:
            from app.services import cost as cost_service

            return round(cost_service.estimate_llm_cost("context-budget", kept_tokens, 0), 6)
        except Exception:
            return 0.0
