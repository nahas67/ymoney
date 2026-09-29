"""Moderation pipeline: deterministic rules first, semantic assist second.

Verdicts (``MODERATION_VERDICTS``): ALLOW, REVIEW, HIDE_IF_SUPPORTED,
BLOCK_ACTION. Every verdict stores ``{verdict, rule, reason, evidence,
provider}`` entries in ``SocialInteraction.moderation_json``.

Hard rules:
  * an uncertain semantic judgment NEVER produces a destructive verdict —
    it can only downgrade ALLOW → REVIEW;
  * HIDE/BLOCK additionally require the platform capability (Lane A
    platform registry, imported lazily). Registry missing or silent about
    the capability degrades to REVIEW with reason "capability unavailable".
"""

from __future__ import annotations

from typing import Any

from app.engine.community.classify import labels_of, redact_sensitive
from app.models.community import MODERATION_VERDICTS

ALLOW, REVIEW, HIDE, BLOCK = MODERATION_VERDICTS

CAPABILITY_UNAVAILABLE = "capability unavailable"
SEMANTIC_MIN_CONFIDENCE = 0.4

# deterministic destructive signals (high precision, never semantic-only)
_THREAT_RX = r"\b(kill|murder|shoot|beat you|hurt you|bomb threat|swat|doxx)\b"
_DOXX_RX = (r"\b(home address|phone number|social security|credit card number|"
            r"bank account number|private photos|nudes of|leaked)\b")


def _pattern_search(text: str, pattern: str) -> str:
    import re

    match = re.search(pattern, text or "", re.IGNORECASE)
    return match.group(0) if match else ""


def supports_capability(platform: str, capability: str = "DELETE_COMMENT") -> tuple[bool, str]:
    """Ask Lane A's platform registry; degrade to REVIEW when unavailable."""
    try:
        from app.engine.platform_registry import Capability, get_registry
    except Exception:  # noqa: BLE001 - sibling lane not landed yet
        return False, CAPABILITY_UNAVAILABLE
    try:
        registry = get_registry()
        cap = getattr(Capability, capability, None)
        if cap is None:
            return False, f"{CAPABILITY_UNAVAILABLE}: unknown capability {capability}"
        supported = bool(registry.supports(platform, cap))
        return supported, "" if supported else f"platform {platform} does not support {capability}"
    except Exception as exc:  # noqa: BLE001 - registry must never crash callers
        return False, f"{CAPABILITY_UNAVAILABLE}: {type(exc).__name__}"


def _semantic_hint(text: str, engine: Any = None) -> dict:
    """Advisory semantic review. Can only ask for REVIEW, never HIDE/BLOCK."""
    if engine is None:
        from app.engine.community.classify import _default_engine

        engine = _default_engine()
    try:
        out, record = engine.classify({
            "item": str(text or "")[:1200],
            "labels": ["ALLOW", "REMOVE"],
            "task": "community moderation",
        })
    except Exception as exc:  # noqa: BLE001 - advisory only
        return {"label": None, "confidence": 0.0, "provider": "deterministic",
                "reason": f"decision unavailable: {type(exc).__name__}"}
    out = out if isinstance(out, dict) else {}
    return {
        "label": out.get("label"),
        "confidence": float(out.get("confidence") or 0.0),
        "provider": getattr(record, "actual_provider", "deterministic") or "deterministic",
        "reason": str(out.get("reason") or ""),
    }


def _entry(verdict: str, rule: str, reason: str, evidence: str,
           provider: str = "deterministic") -> dict:
    return {
        "verdict": verdict,
        "rule": rule,
        "reason": reason,
        "evidence": redact_sensitive(evidence)[:400],
        "provider": provider,
    }


def moderate_interaction(db, workspace_id: str, interaction_id: str,
                         *, engine: Any = None) -> dict:
    """Run the moderation pipeline for one interaction and persist the verdict."""
    from app.models.community import SocialInteraction

    row = db.get(SocialInteraction, interaction_id)
    if row is None or row.workspace_id != workspace_id:
        return {"found": False, "interaction_id": interaction_id,
                "verdict": REVIEW, "entries": []}

    text = str(row.text or "")
    labels = labels_of(row)
    entries: list[dict] = []
    verdict = ALLOW
    rule = "clean"
    reason = "no moderation signal"
    provider = "deterministic"

    # 1. deterministic destructive signals (highest precision first)
    threat = _pattern_search(text, _THREAT_RX)
    doxx = _pattern_search(text, _DOXX_RX)
    if threat:
        verdict, rule = BLOCK, "threat"
        reason = f"explicit threat: {threat[:60]}"
        entries.append(_entry(BLOCK, rule, reason, threat))
    elif doxx:
        verdict, rule = BLOCK, "pii_exposure"
        reason = f"sensitive data exposure: {doxx[:60]}"
        entries.append(_entry(BLOCK, rule, reason, doxx))
    elif "ABUSE" in labels:
        verdict, rule = HIDE, "abuse_label"
        reason = "deterministic ABUSE classification"
        entries.append(_entry(HIDE, rule, reason, text[:200]))
    elif "SPAM" in labels:
        verdict, rule = HIDE, "spam_label"
        reason = "deterministic SPAM classification"
        entries.append(_entry(HIDE, rule, reason, text[:200]))

    # 2. destructive verdicts need the platform capability
    if verdict in (HIDE, BLOCK):
        capability = "DELETE_COMMENT"
        supported, detail = supports_capability(row.platform or "", capability)
        if not supported:
            downgrade_reason = detail if CAPABILITY_UNAVAILABLE in detail else detail
            entries.append(_entry(REVIEW, "capability_gate",
                                  downgrade_reason or CAPABILITY_UNAVAILABLE, "",
                                  provider="registry"))
            verdict, rule = REVIEW, "capability_gate"
            reason = downgrade_reason or CAPABILITY_UNAVAILABLE

    # 3. semantic assist: may only downgrade ALLOW → REVIEW (never destructive)
    hint = _semantic_hint(text, engine)
    if verdict == ALLOW and hint["label"] == "REMOVE" and \
            hint["confidence"] >= SEMANTIC_MIN_CONFIDENCE:
        verdict, rule = REVIEW, "semantic_uncertainty"
        provider = hint["provider"]
        reason = f"semantic review suggests removal (confidence {hint['confidence']:.2f})"
        entries.append(_entry(REVIEW, rule, reason, hint["reason"], provider))
    elif verdict == ALLOW:
        entries.append(_entry(ALLOW, "clean", "deterministic rules found no signal",
                              f"labels={','.join(labels) or 'OTHER'}"))

    if verdict not in MODERATION_VERDICTS:  # defensive: typed vocabulary only
        verdict, rule, reason = REVIEW, "invalid_verdict", "unexpected verdict value"

    row.moderation_json = list(entries)
    row.moderation_state = {
        ALLOW: "allowed",
        REVIEW: "review",
        HIDE: "hidden",
        BLOCK: "blocked",
    }[verdict]
    if verdict == REVIEW and rule == "capability_gate" and not reason:
        reason = CAPABILITY_UNAVAILABLE
    db.flush()
    return {
        "found": True,
        "interaction_id": row.id,
        "verdict": verdict,
        "rule": rule,
        "reason": reason,
        "entries": entries,
        "moderation_state": row.moderation_state,
        "platform": row.platform,
        "labels": labels,
        "semantic": hint,
    }


def verdict_rank(verdict: str) -> int:
    """Severity ordering (ALLOW < REVIEW < HIDE < BLOCK)."""
    return {ALLOW: 0, REVIEW: 1, HIDE: 2, BLOCK: 3}.get(verdict, 1)


__all__ = [
    "ALLOW",
    "BLOCK",
    "CAPABILITY_UNAVAILABLE",
    "HIDE",
    "REVIEW",
    "moderate_interaction",
    "supports_capability",
    "verdict_rank",
]
