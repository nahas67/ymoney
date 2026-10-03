"""Semantic caption emphasis (Work 13 §3).

Deterministic metadata is the PRIMARY signal. The DecisionEngine may assist,
but only to choose among the bounded kinds below, and its choice is recorded
as a *proposal* that the caller stores explicitly -- so the emphasis a caption
renders with is always readable and editable, never an opaque model output.

Privacy rule (non-negotiable): this engine classifies TEXT for typography. It
never infers or records anything about a person. There is deliberately no
"speaker trait", "demographic", "sentiment-about-a-person" or health/religion/
ethnicity/politics kind, and :func:`assert_no_sensitive_kinds` refuses a kind
list that smuggles one in. Gender/age inference from a face or a name is out
of scope and must be added upstream of the timeline, never here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "EMPHASIS_KINDS",
    "FORBIDDEN_SENSITIVE_KINDS",
    "EmphasisError",
    "EmphasisWord",
    "CaptionEmphasisEngine",
    "assert_no_sensitive_kinds",
    "classify_text",
]


class EmphasisError(ValueError):
    """An emphasis request was invalid (unknown kind, sensitive kind, ...)."""


#: The bounded, documented vocabulary. ``NONE`` means "explicitly not
#: emphasised", which is different from "not classified".
EMPHASIS_KINDS: tuple[str, ...] = (
    "KEYWORD", "NUMBER", "ENTITY", "CTA", "QUESTION", "EMOTION_CUE", "NONE",
)

#: Kinds this engine must never produce or accept. Present as a tripwire, not
#: as documentation: :func:`assert_no_sensitive_kinds` raises on them.
FORBIDDEN_SENSITIVE_KINDS: frozenset[str] = frozenset({
    "age", "gender", "sex", "race", "ethnicity", "religion", "politics",
    "political", "health", "medical", "disability", "income", "wealth",
    "sexual_orientation", "orientation", "nationality", "immigration",
    "demographic", "personality", "mental_health",
})

# -- deterministic detectors ------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?:[$€£¥]\s?\d[\d,]*(?:\.\d+)?)"
    r"|(?:\b\d[\d,]*(?:\.\d+)?\s?%?)"
    r"|(?:\b\d+(?:\.\d+)?\s?(?:k|m|b|x|bn|mn)\b)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"\?\s*$|^\s*(?:who|what|when|where|why|how|which|is|are|do|does|did|can|could|should|would|will)\b", re.IGNORECASE)

#: Call-to-action phrasing. Matched on the caption text, not on intent.
_CTA_RE = re.compile(
    r"\b(?:subscribe|follow|comment|share|save|download|get|grab|join|sign\s*up"
    r"|link\s+in\s+bio|swipe|check\s+out|don'?t\s+forget|tap)\b",
    re.IGNORECASE,
)
_EMOTION_RE = re.compile(
    r"\b(?:amazing|incredible|insane|wild|shocking|unbelievable|love|hate"
    r"|finally|secret|mind[- ]blow|stunning|ridiculous|heartbreaking"
    r"|game[- ]changer|crazy|epic|beautiful|terrible|perfect)\b",
    re.IGNORECASE,
)
#: Entity heuristic: capitalised tokens that are not sentence-initial-only.
_ENTITY_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-zA-Z0-9&.\-]{2,}\b")

#: Words that are capitalised only because they start a sentence/line. They
#: must not be treated as entities -- this is what keeps "Save $500" from
#: reading "Save" as an organisation.
_SENTENCE_LEAD_STOPWORDS = frozenset({
    "the", "this", "that", "these", "those", "a", "an", "and", "but", "or",
    "if", "so", "then", "now", "here", "there", "when", "while", "after",
    "before", "because", "it", "we", "you", "i", "they", "he", "she", "my",
    "your", "our", "their", "its", "do", "does", "did", "is", "are", "was",
    "were", "will", "would", "can", "could", "should", "let", "save",
})


def assert_no_sensitive_kinds(kinds) -> None:
    """Raise if any kind looks like a sensitive personal characteristic."""
    for kind in kinds or ():
        token = str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")
        if token in FORBIDDEN_SENSITIVE_KINDS:
            raise EmphasisError(
                f"emphasis kind {kind!r} is a protected personal characteristic "
                f"and must never be inferred for captions"
            )


def _tokenize(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping word spans as ``(start, end, word)``."""
    return [(m.start(), m.end(), m.group(0))
            for m in re.finditer(r"\S+", text or "")]


def classify_text(text: str, *, word: str | None = None) -> str:
    """Classify one caption segment into a single emphasis kind.

    ``word`` narrows the decision to a single token (used when the caller
    already knows which word it is asking about). Returns ``NONE`` when
    nothing matches -- an honest "not emphasised", never a guess.
    """
    probe = (word if word is not None else (text or "")).strip()
    if not probe:
        return "NONE"
    if _NUMBER_RE.search(probe):
        return "NUMBER"
    if _CTA_RE.search(probe):
        return "CTA"
    if word is None and _QUESTION_RE.search(probe):
        return "QUESTION"
    if _EMOTION_RE.search(probe):
        return "EMOTION_CUE"
    if word is not None:
        # A single token: only an entity or a number can apply.
        bare = probe.strip(".,!?;:")
        if (_ENTITY_RE.fullmatch(bare) or re.fullmatch(r"[A-Z][\w&\-]{2,}", bare)) \
                and bare.lower() not in _SENTENCE_LEAD_STOPWORDS:
            return "ENTITY"
        return "NONE"
    for _s, _e, token in _tokenize(probe):
        bare = token.strip(".,!?;:")
        if bare.lower() in _SENTENCE_LEAD_STOPWORDS:
            continue
        if re.fullmatch(r"[A-Z][\w&\-]{2,}", bare):
            return "ENTITY"
    if probe.strip():
        return "KEYWORD"
    return "NONE"


@dataclass(frozen=True)
class EmphasisWord:
    """One emphasised token, with its provenance.

    ``source`` is ``"deterministic"`` for the metadata path and
    ``"decision_engine"`` when the assistant chose it; ``confidence`` is 1.0
    for deterministic classification because there is no model uncertainty to
    report.
    """

    word: str
    start: int
    end: int
    kind: str
    source: str = "deterministic"
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class CaptionEmphasisEngine:
    """Assigns emphasis to caption words, deterministically by default.

    ``enabled_kinds`` bounds what the engine may emit. ``max_per_caption``
    keeps a caption readable -- emphasising every word is the same as
    emphasising none.
    """

    enabled_kinds: tuple[str, ...] = ("KEYWORD", "NUMBER", "CTA", "QUESTION",
                                      "EMOTION_CUE")
    max_per_caption: int = 3
    assist_provider: str = ""
    assist_threshold: float = 0.6
    #: Deterministic kinds are ranked ahead of assistant-proposed ones.
    _RANK: dict[str, int] = field(default_factory=lambda: {
        "NUMBER": 0, "CTA": 1, "QUESTION": 2, "EMOTION_CUE": 3,
        "ENTITY": 4, "KEYWORD": 5, "NONE": 9,
    })

    def __post_init__(self) -> None:
        unknown = [k for k in self.enabled_kinds if k not in EMPHASIS_KINDS]
        if unknown:
            raise EmphasisError(
                f"unknown emphasis kind(s) {unknown}; allowed {list(EMPHASIS_KINDS)}"
            )
        assert_no_sensitive_kinds(self.enabled_kinds)
        if self.max_per_caption < 0:
            raise EmphasisError("max_per_caption must be >= 0")

    # -- public API ---------------------------------------------------------

    def emphasize(self, text: str) -> list[EmphasisWord]:
        """Return the emphasis for a caption's text.

        Deterministic metadata drives this. The DecisionEngine is consulted
        ONLY when ``assist_provider`` is set AND deterministic classification
        found nothing, and its answer is a proposal stored with its own
        provenance so a human can see and change it.
        """
        if not (text or "").strip():
            return []
        found: list[EmphasisWord] = []
        seen_spans: set[tuple[int, int]] = set()
        for start, end, token in _tokenize(text):
            bare = token.strip(".,!?;:\"'()")
            if not bare:
                continue
            kind = classify_text(text, word=bare)
            if kind == "NONE" or kind not in self.enabled_kinds:
                continue
            offset = token.find(bare)
            span = (start + offset, start + offset + len(bare))
            if span in seen_spans:
                continue
            seen_spans.add(span)
            found.append(EmphasisWord(word=bare, start=span[0], end=span[1],
                                      kind=kind))
        if not found and self.assist_provider:
            found = self._assist(text)
        return self._rank_and_limit(found)

    def kinds_in(self, text: str) -> list[str]:
        """The distinct kinds present -- what the UI shows as chips."""
        seen: list[str] = []
        for item in self.emphasize(text):
            if item.kind not in seen:
                seen.append(item.kind)
        return seen

    # -- internals ----------------------------------------------------------

    def _assist(self, text: str) -> list[EmphasisWord]:
        """Ask the DecisionEngine to classify, defensively.

        Never raises, never emits a kind outside ``enabled_kinds``, and always
        labels the result ``decision_engine`` with a real confidence. A failure
        degrades to "no emphasis" rather than blocking a caption.
        """
        try:
            from app.engine.intelligence.decision import decide
        except Exception:
            return []
        try:
            out, record = decide(
                "classify",
                {"text": text[:500], "task": "caption_keyword_emphasis",
                 "allowed_kinds": list(self.enabled_kinds)},
                provider=self.assist_provider,
            )
        except Exception:
            return []
        confidence = float(getattr(record, "cost_usd", 0.0) and 0.0 or 0.0)
        # Confidence is read from the record when present; otherwise the
        # assistant's label is treated as a weak proposal.
        confidence = float((out or {}).get("confidence", 0.0)
                           if isinstance(out, dict) else 0.0)
        kind = (out or {}).get("kind") if isinstance(out, dict) else None
        if kind not in self.enabled_kinds or confidence < self.assist_threshold:
            return []
        start = (text or "").find((out or {}).get("word", ""))
        if start < 0:
            return []
        word = (out or {})["word"]
        return [EmphasisWord(
            word=word, start=start, end=start + len(word), kind=kind,
            source="decision_engine", confidence=confidence,
        )]

    def _rank_and_limit(self, items: list[EmphasisWord]) -> list[EmphasisWord]:
        ordered = sorted(
            items,
            key=lambda i: (0 if i.source == "deterministic" else 1,
                           self._RANK.get(i.kind, 8), i.start),
        )[:self.max_per_caption]
        return sorted(ordered, key=lambda i: i.start)