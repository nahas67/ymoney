"""Typed multi-label classification for community interactions (Work 09, Lane C).

Hard enforcement stays DETERMINISTIC. The DecisionEngine only *assists* with a
semantic label: its output is recorded with provider/model/confidence/evidence
and is accepted only above a confidence floor. AI/decision judgments never
override auth, RBAC, workspace isolation, budgets, publishing authorization,
compliance, idempotency or DB invariants.

Sensitive personal traits (religion, ethnicity, sexuality, health, politics,
disability, ...) are NEVER inferred; any evidence string that would carry one
is redacted before it reaches `classifications_json`.
"""

from __future__ import annotations

import re
from typing import Any

from app.models.community import CLASSIFICATION_LABELS

# Accepted label vocabulary (typed, multi-label).
LABELS: tuple[str, ...] = CLASSIFICATION_LABELS
LABEL_SET = frozenset(LABELS)

# Semantic (AI) labels below this confidence are recorded on the decision
# ledger only — never applied to the row.
SEMANTIC_MIN_CONFIDENCE = 0.25


def _rx(words: tuple[str, ...]) -> re.Pattern[str]:
    body = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


# -- deterministic signal tables ------------------------------------------

POSITIVE_WORDS = (
    "love", "loved", "great", "awesome", "amazing", "thank", "thanks",
    "thank you", "helpful", "excellent", "appreciate", "best", "nice",
    "brilliant", "fantastic", "perfect", "good", "impressed", "fantastic",
    "keep it up", "well done", "subscribe",  # positive endorsement context
)

NEGATIVE_WORDS = (
    "hate", "terrible", "worst", "bad", "useless", "disappointing",
    "disappointed", "scam", "garbage", "angry", "annoyed", "sucks", "poor",
    "awful", "frustrated", "waste of time", "overrated", "never again",
    "misleading",
)

SUPPORT_WORDS = (
    "help", "issue", "problem", "error", "bug", "broken", "not working",
    "can't", "cannot", "login", "log in", "password", "account", "billing",
    "charged", "charge", "refund", "support", "stuck", "failed", "failure",
    "crash", "crashing", "doesn't work", "did not work", "assistance",
    "technical", "reset", "verification code",
)

LEAD_WORDS = (
    "price", "pricing", "cost", "how much", "buy", "purchase", "subscribe",
    "sign up", "signup", "demo", "quote", "upgrade", "plan", "plans",
    "checkout", "order", "premium", "enterprise", "for my business",
)

FEEDBACK_WORDS = (
    "suggest", "suggestion", "feedback", "would be nice", "feature request",
    "improve", "improvement", "wish you", "could you add", "idea for",
    "consider adding", "it would help if", "add a feature",
)

COLLABORATION_WORDS = (
    "collab", "collaborate", "collaboration", "partnership", "partner",
    "sponsor", "sponsorship", "guest", "work with you", "brand deal",
    "cross-promote", "joint venture", "creator program",
)

CONTENT_REQUEST_WORDS = (
    "video on", "make a video", "can you make", "tutorial", "explainer",
    "cover this", "content about", "teach us", "how about a video",
    "do a video", "post more about", "series on", "walkthrough",
)

ABUSE_WORDS = (
    "kill yourself", "fuck off", "f off", "idiot", "idiots", "moron",
    "retard", "dumbass", "shut up", "you're stupid", "you are stupid",
    "pathetic loser", "absolute clown",
)

PRODUCT_QUESTION_WORDS = (
    "price", "pricing", "cost", "plan", "plans", "feature", "features",
    "integrate", "integration", "api", "works with", "compatible",
    "available on", "does it", "do you support", "roadmap",
)

_PURCHASE_INTENT_WORDS = (
    "buy", "purchase", "sign up", "subscribe", "checkout", "how much",
    "pricing", "price", "upgrade", "get started", "order",
)

_INTERROGATIVES = (
    "who", "what", "when", "where", "why", "how", "which", "is", "are",
    "was", "were", "do", "does", "did", "can", "could", "would", "will",
    "should", "shall", "may", "might", "am",
)

# Never inferred, never stored (evidence redaction only).
SENSITIVE_TRAIT_RE = re.compile(
    r"\b(muslim|islam|islamic|christian|catholic|protestant|judaism|jewish|"
    r"hindu|sikh|buddhist|atheist|religion|religious|gay|lesbian|bisexual|"
    r"transgender|homosexual|sexual orientation|black|white|hispanic|latino|"
    r"african|asian|ethnicity|ethnic|disabled|disability|handicapped|"
    r"pregnant|diabetic|cancer|depressed|mental illness|republican|democrat|"
    r"liberal|conservative|socialist|communist|political affiliation)\b",
    re.IGNORECASE,
)

_CONTACT_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s().-]{7,}\d", re.IGNORECASE
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CHAR_RUN_RE = re.compile(r"(.)\1{4,}")
_SPAM_PHRASES = (
    "free money", "click here", "make $", "guaranteed income",
    "crypto giveaway", "dm me", "follow me", "check my bio", "limited offer",
    "earn $", "work from home", "double your money",
)


def redact_sensitive(text: str, *, keep_contacts: bool = False) -> str:
    """Strip sensitive personal traits (and contact details) from evidence."""
    out = SENSITIVE_TRAIT_RE.sub("[redacted]", str(text or ""))
    if not keep_contacts:
        out = _CONTACT_RE.sub("[redacted-contact]", out)
    return out


def _hits(pattern: re.Pattern[str], text: str) -> list[str]:
    seen: list[str] = []
    for m in pattern.finditer(text):
        token = m.group(0).lower()
        if token not in seen:
            seen.append(token)
    return seen


# -- deterministic detectors ------------------------------------------------


def detect_question(text: str, *, engine: Any = None) -> dict:
    """Question detection: deterministic first, DecisionEngine assists.

    Returns {is_question, confidence, provider, model, evidence, signals}.
    """
    raw = str(text or "").strip()
    low = raw.lower()
    signals: list[str] = []
    confident = False
    if "?" in raw:
        signals.append("contains '?'")
        confident = True
    stripped = low.rstrip("!.")
    first = stripped.split(" ")[0].strip("?,.!") if stripped else ""
    if first in _INTERROGATIVES:
        signals.append(f"starts with interrogative '{first}'")
        confident = True
    if any(low.startswith(f"{w} ") for w in _INTERROGATIVES) and \
            f"starts with interrogative '{first}'" not in signals:
        signals.append("interrogative opening")
        confident = True

    result = {
        "is_question": confident,
        "confidence": 1.0 if confident else 0.0,
        "provider": "deterministic",
        "model": "",
        "evidence": redact_sensitive("; ".join(signals)) if signals else "",
        "signals": signals,
    }
    if confident:
        return result

    # Inconclusive deterministically → advisory semantic assist (never a
    # destructive input; only flips is_question above the confidence floor).
    out, record = _classify_semantic(
        engine, raw, ["QUESTION", "STATEMENT"], kind_hint="question"
    )
    label = str(out.get("label") or "") if isinstance(out, dict) else ""
    conf = float(out.get("confidence") or 0.0) if isinstance(out, dict) else 0.0
    if label == "QUESTION" and conf >= SEMANTIC_MIN_CONFIDENCE:
        result.update({
            "is_question": True,
            "confidence": round(conf, 3),
            "provider": getattr(record, "actual_provider", "deterministic"),
            "model": getattr(record, "model", ""),
            "evidence": redact_sensitive(str(out.get("reason") or "semantic assist")),
            "signals": ["semantic assist"],
        })
    return result


_SPAM_RX = _rx(_SPAM_PHRASES)


def detect_spam(text: str, *, engine: Any = None) -> dict:
    """Spam detection: deterministic patterns first (links/caps/repetition).

    Semantic assist may ADD a suspicion signal but can never be the sole
    basis for a destructive moderation action (see moderation.py).
    """
    raw = str(text or "")
    low = raw.lower()
    reasons: list[str] = []
    score = 0.0

    links = len(_URL_RE.findall(raw))
    if links >= 3:
        reasons.append(f"{links} links")
        score += 0.6
    elif links == 1:
        reasons.append("contains a link")
        score += 0.2

    letters = [c for c in raw if c.isalpha()]
    if len(letters) >= 10:
        upper = sum(1 for c in letters if c.isupper())
        ratio = upper / len(letters)
        if ratio >= 0.7:
            reasons.append(f"shouting caps ({ratio:.0%})")
            score += 0.4

    runs = _CHAR_RUN_RE.findall(raw)
    if runs:
        reasons.append("repeated character run")
        score += 0.3

    words = [w for w in re.findall(r"[a-z']+", low) if len(w) > 2]
    if words:
        top = max(words.count(w) for w in set(words))
        if top >= 5:
            reasons.append(f"word repeated {top}x")
            score += 0.4

    phrases = _hits(_SPAM_RX, low)
    if phrases:
        reasons.append("spam phrases: " + ", ".join(phrases[:3]))
        score += 0.3 * min(3, len(phrases))

    is_spam = score >= 0.6
    provider = "deterministic"
    model = ""
    evidence = "; ".join(reasons)

    if not is_spam and engine is not None and 0 < score < 0.6:
        # Advisory only: semantic suspicion never promotes alone to SPAM.
        out, record = _classify_semantic(engine, raw, ["SPAM", "GENUINE"])
        label = str(out.get("label") or "") if isinstance(out, dict) else ""
        if label == "SPAM":
            provider = getattr(record, "actual_provider", "deterministic")
            model = getattr(record, "model", "")
            evidence = (evidence + "; " if evidence else "") + "semantic suspicion (advisory)"

    return {
        "is_spam": is_spam,
        "confidence": round(min(1.0, score), 3),
        "reasons": reasons,
        "provider": provider,
        "model": model,
        "evidence": redact_sensitive(evidence),
        "links": links,
    }


def sentiment_of(text: str, labels: list[str] | None = None) -> str:
    """Deterministic sentiment: positive | negative | mixed | neutral."""
    low = str(text or "").lower()
    pos = bool(_hits(_rx(POSITIVE_WORDS), low))
    neg = bool(_hits(_rx(NEGATIVE_WORDS), low))
    labels = labels or []
    if "POSITIVE" in labels:
        pos = True
    if "NEGATIVE" in labels:
        neg = True
    if pos and neg:
        return "mixed"
    if pos:
        return "positive"
    if neg:
        return "negative"
    return "neutral"


_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


def priority_for(labels: list[str]) -> str:
    """LEAD/QUESTION → high; ABUSE → urgent; SPAM → low; else normal."""
    have = set(labels or [])
    if "ABUSE" in have:
        return "urgent"
    if "LEAD" in have or "QUESTION" in have:
        return "high"
    if "SUPPORT" in have:
        return "high"
    if have and have <= {"SPAM", "OTHER"}:
        return "low"
    return "normal"


_PRIORITY_ORDER = ("ABUSE", "LEAD", "SUPPORT", "QUESTION", "NEGATIVE",
                   "FEEDBACK", "CONTENT_REQUEST", "COLLABORATION", "POSITIVE",
                   "SPAM", "OTHER")


def intent_for(labels: list[str]) -> str:
    """Primary intent label (stable ordering, first match wins)."""
    have = set(labels or [])
    for label in _PRIORITY_ORDER:
        if label in have:
            return label
    return "OTHER"


# -- semantic assist ---------------------------------------------------------


def _default_engine(workspace_id: str = ""):
    from app.engine.intelligence.decision import DecisionEngine

    return DecisionEngine(workspace_id, persist=False)


def _classify_semantic(engine: Any, text: str, labels: list[str],
                       kind_hint: str = "") -> tuple[dict, Any]:
    """One advisory DecisionEngine.classify call. Never raises."""
    if engine is None:
        engine = _default_engine()
    try:
        out, record = engine.classify({
            "item": str(text or "")[:1200],
            "labels": list(labels),
            "task": kind_hint or "community classification",
        })
    except Exception as exc:  # noqa: BLE001 — advisory path must not break
        return {"label": None, "confidence": 0.0,
                "reason": f"decision unavailable: {type(exc).__name__}"}, None
    return (out if isinstance(out, dict) else {"label": None}), record


_PATTERN_LABELS: dict[str, re.Pattern[str]] = {
    "POSITIVE": _rx(POSITIVE_WORDS),
    "NEGATIVE": _rx(NEGATIVE_WORDS),
    "SUPPORT": _rx(SUPPORT_WORDS),
    "LEAD": _rx(LEAD_WORDS),
    "FEEDBACK": _rx(FEEDBACK_WORDS),
    "COLLABORATION": _rx(COLLABORATION_WORDS),
    "CONTENT_REQUEST": _rx(CONTENT_REQUEST_WORDS),
    "ABUSE": _rx(ABUSE_WORDS),
}


def classify_text(text: str, *, engine: Any = None) -> dict:
    """Pure multi-label classification of one interaction body.

    Returns a JSON-safe dict: labels (entries with provenance), sentiment,
    is_question, intent, priority, is_spam. No DB access.
    """
    raw = str(text or "")
    low = raw.lower()

    entries: dict[str, dict] = {}

    def add(label: str, *, provider: str, model: str, confidence: float,
            evidence: str, source: str) -> None:
        if label not in LABEL_SET:  # typed vocabulary only
            return
        prev = entries.get(label)
        if prev is not None and prev.get("source") == "ai":
            return
        entries[label] = {
            "label": label,
            "provider": provider,
            "model": model,
            "confidence": round(float(confidence), 3),
            "evidence": redact_sensitive(evidence)[:400],
            "source": source,
        }

    # 1. deterministic patterns (the enforcement layer)
    question = detect_question(raw, engine=engine)
    if question["is_question"]:
        add("QUESTION", provider="deterministic", model="",
            confidence=question["confidence"],
            evidence=question["evidence"] or "interrogative text", source="rule")

    spam = detect_spam(raw, engine=engine)
    if spam["is_spam"]:
        add("SPAM", provider="deterministic", model="",
            confidence=spam["confidence"], evidence=spam["evidence"], source="rule")

    for label, pattern in _PATTERN_LABELS.items():
        found = _hits(pattern, low)
        if found:
            add(label, provider="deterministic", model="", confidence=1.0,
                evidence="matched signals: " + ", ".join(found[:4]), source="rule")

    # 2. semantic assist (advisory, confidence-floored, vocabulary-typed)
    out, record = _classify_semantic(engine, raw, list(LABELS))
    label = str(out.get("label") or "") if isinstance(out, dict) else ""
    conf = float(out.get("confidence") or 0.0) if isinstance(out, dict) else 0.0
    if label in LABEL_SET and conf >= SEMANTIC_MIN_CONFIDENCE and label not in entries:
        add(label,
            provider=getattr(record, "actual_provider", "deterministic") or "deterministic",
            model=getattr(record, "model", "") if record is not None else "",
            confidence=conf,
            evidence=str(out.get("reason") or "semantic classification"),
            source="ai")

    if not entries:
        add("OTHER", provider="deterministic", model="", confidence=0.5,
            evidence="no known signal matched", source="rule")

    labels = [e["label"] for e in entries.values()]
    if spam["is_spam"] and "SPAM" not in labels:
        labels.append("SPAM")
    labels.sort(key=lambda lb: _PRIORITY_ORDER.index(lb) if lb in _PRIORITY_ORDER else 99)

    return {
        "labels": [entries[k] for k in entries],
        "label_values": labels,
        "sentiment": sentiment_of(raw, labels),
        "is_question": bool(question["is_question"]),
        "question": question,
        "is_spam": bool(spam["is_spam"]),
        "spam": spam,
        "intent": intent_for(labels),
        "priority": priority_for(labels),
        "product_question": bool(
            question["is_question"] and _hits(_rx(PRODUCT_QUESTION_WORDS), low)),
        "purchase_intent": bool(_hits(_rx(_PURCHASE_INTENT_WORDS), low)),
    }


def classify_interaction(db, workspace_id: str, interaction_id: str,
                         *, engine: Any = None) -> dict:
    """Classify one SocialInteraction row and persist the typed outcome.

    Workspace isolation is enforced: an interaction of another workspace is
    reported as {"found": False} and never read or written.
    """
    from app.models.community import SocialInteraction

    row = db.get(SocialInteraction, interaction_id)
    if row is None or row.workspace_id != workspace_id:
        return {"found": False, "interaction_id": interaction_id}

    result = classify_text(row.text or "", engine=engine)
    row.classifications_json = list(result["labels"])
    row.is_question = bool(result["is_question"])
    row.sentiment = str(result["sentiment"])
    row.intent = str(result["intent"])
    row.priority = str(result["priority"])
    row.status = "spam" if result["is_spam"] and not result["is_question"] else "classified"
    db.flush()
    return {
        "found": True,
        "interaction_id": row.id,
        "labels": result["label_values"],
        "classifications": result["labels"],
        "sentiment": row.sentiment,
        "intent": row.intent,
        "priority": row.priority,
        "is_question": row.is_question,
        "is_spam": result["is_spam"],
        "question": result["question"],
        "spam": result["spam"],
        "product_question": result["product_question"],
        "purchase_intent": result["purchase_intent"],
    }


def labels_of(interaction) -> list[str]:
    """Label values stored on a SocialInteraction row (safe for missing data)."""
    entries = getattr(interaction, "classifications_json", None) or []
    out: list[str] = []
    for entry in entries:
        label = (str(entry.get("label") or "") if isinstance(entry, dict)
                 else str(entry))
        if label in LABEL_SET and label not in out:
            out.append(label)
    return out


__all__ = [
    "LABELS",
    "LABEL_SET",
    "SEMANTIC_MIN_CONFIDENCE",
    "classify_interaction",
    "classify_text",
    "detect_question",
    "detect_spam",
    "intent_for",
    "labels_of",
    "priority_for",
    "redact_sensitive",
    "sentiment_of",
]
