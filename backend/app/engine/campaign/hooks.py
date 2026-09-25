"""Campaign hook shaping, reusing the Hook Optimizer's scoring signals.

Deterministic and LLM-free: classification + cleanup rules run inline while
strength scoring reuses ``HookOptimizerAgent.HOOK_SIGNALS`` /
``HOOK_MARKERS`` from ``engine/agents/creation.py``. Generated hooks never
carry deceptive claims (guarantees, false absolutes, bait superlatives).
"""

from __future__ import annotations

import re

from app.engine.agents.creation import HookOptimizerAgent

HOOK_TYPES = (
    "QUESTION",
    "SURPRISE",
    "CONTRARIAN",
    "NUMBER",
    "PROBLEM",
    "CURIOSITY",
    "DIRECT_CLAIM",
    "QUOTE",
)

# Absolute / bait phrasing replaced with measured language. Order matters:
# longer patterns first.
_DECEPTIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b100%\s+guaranteed\b", "designed"),
    (r"\bguaranteed\b", "proven"),
    (r"\byou won'?t believe\b", "here's"),
    (r"\bdoctors hate\b", "experts explain"),
    (r"\bnever been told\b", "rarely explained"),
    (r"\bshocking truth\b", "untold story"),
    (r"\bshocking\b", "surprising"),
    (r"\binsane\b", "remarkable"),
    (r"\bcrazy\b", "unexpected"),
    (r"\beveryone is wrong\b", "most people miss this"),
    (r"\bthey don'?t want you to know\b", "few people explain"),
)


def strip_deceptive_claims(text: str) -> str:
    """Replace bait/absolutist phrasing with measured language."""
    out = text or ""
    for pattern, replacement in _DECEPTIVE_PATTERNS:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def classify_hook(hook: str) -> str:
    """Rule-based hook type; mirrors the optimizer's marker doctrine."""
    text = (hook or "").lower()
    if '"' in text or "\u201c" in text or text.startswith(("i ", "my ")):
        return "QUOTE"
    if "?" in text:
        return "QUESTION"
    if any(c.isdigit() for c in text):
        return "NUMBER"
    if any(w in text for w in ("nobody", "everyone", "wrong", "myth", "lie", "actually")):
        return "CONTRARIAN"
    if any(w in text for w in ("mistake", "problem", "stop", "never", "don't", "avoid", "fail")):
        return "PROBLEM"
    if any(w in text for w in ("truth", "secret", "hidden", "nobody", "rarely", "untold")):
        return "CURIOSITY"
    if any(w in text for w in ("surprising", "unexpected", "unbelievable", "remarkable")):
        return "SURPRISE"
    return "DIRECT_CLAIM"


def hook_strength(hook: str, *, pattern_hook_bonus: float = 0.0) -> float:
    """Predicted hook strength, same signals as HookOptimizerAgent.rank_hooks."""
    base = 60.0
    opening = (hook or "").lower()[:120]
    for kind, score in HookOptimizerAgent.HOOK_SIGNALS:
        if any(m in opening for m in HookOptimizerAgent.HOOK_MARKERS[kind]):
            base = float(score)
            break
    if any(c.isdigit() for c in opening):
        base += HookOptimizerAgent.NUMBER_BONUS
    return round(min(100.0, base + pattern_hook_bonus), 1)


def optimize_hook(original_hook: str, *, topic: str = "") -> dict:
    """Clean + sharpen a clip hook without inventing claims.

    Returns {original_hook, optimized_hook, hook_type, predicted_score}.
    """
    original = (original_hook or "").strip()
    cleaned = strip_deceptive_claims(original)
    optimized = cleaned
    if topic and topic.lower() not in optimized.lower():
        optimized = f"{optimized} — {topic.strip()}" if optimized else topic.strip()
    # Front-load: hooks land in the first 3 seconds (~8 words spoken).
    words = optimized.split()
    if len(words) > 24:
        optimized = " ".join(words[:24]).rstrip(",;:")
    hook_type = classify_hook(optimized or original)
    return {
        "original_hook": original,
        "optimized_hook": optimized,
        "hook_type": hook_type,
        "predicted_score": hook_strength(optimized),
    }


def optimize_hooks(hooks: list[str], *, topic: str = "") -> list[dict]:
    """Batch version; rank strongest first (optimizer doctrine)."""
    results = [optimize_hook(h, topic=topic) for h in (hooks or [])]
    results.sort(key=lambda r: -r["predicted_score"])
    return results
