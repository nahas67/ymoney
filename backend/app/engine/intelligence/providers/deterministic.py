"""Deterministic provider: pure rule-based decisions, always available offline."""

from __future__ import annotations

import re

from app.engine.intelligence.providers.base import (
    ALL_KINDS,
    BaseDecisionProvider,
    ProviderHealth,
    ProviderResult,
)

_POSITIVE = frozenset({
    "yes", "true", "approve", "approved", "good", "great", "strong",
    "high", "viral", "emerging", "rising", "safe", "pass", "passed",
    "recommend", "best", "top", "excellent", "promising",
})
_NEGATIVE = frozenset({
    "no", "never", "bad", "weak", "low", "risk", "risky", "unsafe",
    "fail", "failed", "reject", "poor", "declining", "stale",
    "duplicate", "spam", "harmful",
})


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _sentiment(text: str) -> int:
    toks = _tokens(text)
    return len(toks & _POSITIVE) - len(toks & _NEGATIVE)


def _text(payload: dict, *keys: str) -> str:
    for key in keys:
        val = payload.get(key)
        if val:
            return str(val)
    return ""


class DeterministicProvider(BaseDecisionProvider):
    """Rule-based judgments with zero network and zero cost."""

    name = "deterministic"
    capabilities = ALL_KINDS

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="AVAILABLE", detail="pure rules, no credentials needed")

    def run(self, kind: str, payload: dict) -> ProviderResult:
        self._require(kind)
        fn = getattr(self, f"_do_{kind}")
        return ProviderResult(output=fn(dict(payload or {})), model="deterministic-v1")

    # -- primitives ------------------------------------------------------

    def _do_boolean(self, payload: dict) -> dict:
        text = _text(payload, "question", "criterion", "text", "context", "statement")
        context = _text(payload, "context", "evidence", "background")
        score = _sentiment(f"{text} {context}")
        return {"value": bool(score >= 0), "score": score,
                "reason": "rule: positive-minus-negative token sentiment >= 0"}

    def _do_choose(self, payload: dict) -> dict:
        options = list(payload.get("options") or [])
        criterion = _text(payload, "criterion", "question", "goal")
        if not options:
            return {"index": -1, "choice": None, "reason": "no options provided"}
        scored = [(_overlap(str(o), criterion), -i, o) for i, o in enumerate(options)]
        scored.sort(key=lambda t: (-t[0], t[1]))
        best = scored[0]
        return {"index": options.index(best[2]), "choice": best[2],
                "reason": f"rule: highest token overlap with criterion ({best[0]:.2f})"}

    def _do_rank(self, payload: dict) -> dict:
        return self._rank_items(payload, key="rank")

    def _do_rerank(self, payload: dict) -> dict:
        return self._rank_items(payload, key="rerank")

    def _rank_items(self, payload: dict, key: str) -> dict:
        items = list(payload.get("items") or [])
        criterion = _text(payload, "criterion", "query", "question", "goal")
        scored = sorted(
            [(round(_overlap(str(i), criterion), 4), idx) for idx, i in enumerate(items)],
            key=lambda t: (-t[0], t[1]),
        )
        return {"order": [idx for _, idx in scored],
                "scores": [s for s, _ in scored],
                "reason": f"rule: {key} by token overlap with criterion"}

    def _do_score(self, payload: dict) -> dict:
        text = _text(payload, "item", "text", "topic", "content")
        toks = _tokens(text)
        words = text.split()
        hook_hits = sum(1 for t in toks if t in _POSITIVE)
        numbers = sum(1 for w in words if any(c.isdigit() for c in w))
        value = 42.0 + min(28.0, hook_hits * 7.0) + min(16.0, numbers * 4.0)
        value += min(8.0, len(words) / 12.0)
        if text.rstrip().endswith(("?", "!")):
            value += 6.0
        return {"score": round(min(100.0, value), 1),
                "reason": "rule: hook density + numbers + length (offline heuristic)"}

    def _do_classify(self, payload: dict) -> dict:
        item = _text(payload, "item", "text", "topic", "content")
        labels = list(payload.get("labels") or [])
        if not labels:
            return {"label": None, "confidence": 0.0, "reason": "no labels provided"}
        scored = [(_overlap(item, str(label)), -i, label) for i, label in enumerate(labels)]
        scored.sort(key=lambda t: (-t[0], t[1]))
        best = scored[0]
        return {"label": best[2], "confidence": round(best[0], 3),
                "reason": f"rule: highest token overlap ({best[0]:.2f})"}

    def _do_compare(self, payload: dict) -> dict:
        a = str(payload.get("a", ""))
        b = str(payload.get("b", ""))
        criterion = _text(payload, "criterion", "question", "goal")
        sa, sb = _overlap(a, criterion), _overlap(b, criterion)
        winner = "a" if sa >= sb else "b"
        return {"winner": winner, "scores": {"a": round(sa, 3), "b": round(sb, 3)},
                "reason": "rule: higher criterion overlap wins, ties go to 'a'"}

    def _do_route(self, payload: dict) -> dict:
        task = _text(payload, "task", "text", "question")
        routes = list(payload.get("routes") or [])
        if not routes:
            return {"route": None, "reason": "no routes provided"}
        scored = [(_overlap(task, str(r)), -i, r) for i, r in enumerate(routes)]
        scored.sort(key=lambda t: (-t[0], t[1]))
        return {"route": scored[0][2], "confidence": round(scored[0][0], 3),
                "reason": "rule: highest token overlap with task"}

    def _do_verify(self, payload: dict) -> dict:
        claim = _text(payload, "claim", "statement", "text")
        evidence = _text(payload, "evidence", "context", "sources")
        overlap = _overlap(claim, evidence)
        if overlap >= 0.5:
            status = "SUPPORTED"
        elif overlap <= 0.05:
            status = "UNVERIFIED"
        else:
            status = "PARTIAL"
        return {"status": status, "overlap": round(overlap, 3),
                "reason": "rule: claim/evidence token overlap band"}
