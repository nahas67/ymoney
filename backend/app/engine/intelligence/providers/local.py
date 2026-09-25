"""Local heuristic provider: statistical judgments, no network calls."""

from __future__ import annotations

import re

from app.engine.intelligence.providers.base import (
    ALL_KINDS,
    BaseDecisionProvider,
    ProviderHealth,
    ProviderResult,
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _text(payload: dict, *keys: str) -> str:
    for key in keys:
        val = payload.get(key)
        if val:
            return str(val)
    return ""


def _stat_score(text: str) -> float:
    """Length/numeral/punctuation statistical score (distinct from deterministic)."""
    words = _words(text)
    if not words:
        return 0.0
    unique_ratio = len(set(words)) / len(words)
    digits = sum(1 for w in words if any(c.isdigit() for c in w))
    punct = 4.0 if text.rstrip().endswith("?") else (2.0 if text.rstrip().endswith("!") else 0.0)
    length_term = min(30.0, len(words) * 1.5)
    return round(min(100.0, 30.0 + unique_ratio * 20.0 + digits * 5.0 + punct + length_term), 1)


class LocalHeuristicProvider(BaseDecisionProvider):
    """Offline statistical heuristics. Never touches the network."""

    name = "local"
    capabilities = ALL_KINDS

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="AVAILABLE", detail="offline statistics, no credentials needed")

    def run(self, kind: str, payload: dict) -> ProviderResult:
        self._require(kind)
        fn = getattr(self, f"_do_{kind}")
        return ProviderResult(output=fn(dict(payload or {})), model="local-heuristic-v1")

    def _do_boolean(self, payload: dict) -> dict:
        score = _stat_score(_text(payload, "question", "criterion", "text", "context"))
        return {"value": bool(score >= 50.0), "score": score,
                "reason": "rule: statistical score >= 50"}

    def _do_choose(self, payload: dict) -> dict:
        options = list(payload.get("options") or [])
        if not options:
            return {"index": -1, "choice": None, "reason": "no options provided"}
        scored = [(_stat_score(str(o)), -i, o) for i, o in enumerate(options)]
        scored.sort(key=lambda t: (-t[0], t[1]))
        best = scored[0]
        return {"index": options.index(best[2]), "choice": best[2],
                "reason": "rule: highest statistical score"}

    def _do_rank(self, payload: dict) -> dict:
        return self._rank_items(payload)

    def _do_rerank(self, payload: dict) -> dict:
        return self._rank_items(payload)

    def _rank_items(self, payload: dict) -> dict:
        items = list(payload.get("items") or [])
        scored = sorted(
            [(_stat_score(str(i)), idx) for idx, i in enumerate(items)],
            key=lambda t: (-t[0], t[1]),
        )
        return {"order": [idx for _, idx in scored],
                "scores": [s for s, _ in scored],
                "reason": "rule: rank by statistical score"}

    def _do_score(self, payload: dict) -> dict:
        text = _text(payload, "item", "text", "topic", "content")
        return {"score": _stat_score(text),
                "reason": "rule: uniqueness + numerals + punctuation + length"}

    def _do_classify(self, payload: dict) -> dict:
        labels = list(payload.get("labels") or [])
        item = _text(payload, "item", "text", "topic", "content")
        if not labels:
            return {"label": None, "confidence": 0.0, "reason": "no labels provided"}
        # Distinct tie-break from deterministic: longest label wins ties.
        item_set = set(_words(item))
        scored = []
        for i, label in enumerate(labels):
            label_set = set(_words(str(label)))
            overlap = len(item_set & label_set) / max(len(item_set | label_set), 1)
            scored.append((overlap, len(str(label)), -i, label))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        best = scored[0]
        return {"label": best[3], "confidence": round(best[0], 3),
                "reason": "rule: overlap, ties prefer the longest label"}

    def _do_compare(self, payload: dict) -> dict:
        sa = _stat_score(str(payload.get("a", "")))
        sb = _stat_score(str(payload.get("b", "")))
        return {"winner": "a" if sa >= sb else "b",
                "scores": {"a": sa, "b": sb},
                "reason": "rule: higher statistical score wins, ties go to 'a'"}

    def _do_route(self, payload: dict) -> dict:
        routes = list(payload.get("routes") or [])
        task = _text(payload, "task", "text", "question")
        if not routes:
            return {"route": None, "reason": "no routes provided"}
        task_set = set(_words(task))
        scored = []
        for i, route in enumerate(routes):
            route_set = set(_words(str(route)))
            overlap = len(task_set & route_set) / max(len(task_set | route_set), 1)
            scored.append((overlap, -i, route))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return {"route": scored[0][2], "confidence": round(scored[0][0], 3),
                "reason": "rule: highest Jaccard overlap with task"}

    def _do_verify(self, payload: dict) -> dict:
        claim = _text(payload, "claim", "statement", "text")
        evidence = _text(payload, "evidence", "context", "sources")
        claim_set, ev_set = set(_words(claim)), set(_words(evidence))
        if not claim_set:
            return {"status": "UNVERIFIED", "overlap": 0.0, "reason": "rule: empty claim"}
        coverage = len(claim_set & ev_set) / len(claim_set)
        if coverage >= 0.6:
            status = "SUPPORTED"
        elif coverage <= 0.1:
            status = "UNVERIFIED"
        else:
            status = "PARTIAL"
        return {"status": status, "overlap": round(coverage, 3),
                "reason": "rule: claim-term coverage band"}
