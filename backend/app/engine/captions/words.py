"""Word-level caption timing (Work 13 §2).

Consumes the Work 12 alignment rows (``media_intel_words``) and turns them
into caption groups with per-word timing.

THE CONTRACT: word timing is never invented.

If an asset has no stored word rows, this module reports
``available=False`` with a machine reason and the caller degrades to
segment-level captions. It does not interpolate, distribute evenly, or
"estimate" per-word boundaries. Fabricated word timing is the single easiest
way to ship a caption feature that looks right and is wrong, so the
availability signal is a first-class return value rather than an exception.

The read path mirrors the existing honest pattern in
``app.engine.intel.silence_fillers.resolve_units`` and always scopes by
workspace, so a foreign asset simply looks absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CaptionGroup",
    "WordTiming",
    "WordTimingSource",
    "UNAVAILABLE_NO_WORDS",
    "UNAVAILABLE_NO_ASSET",
    "group_words_into_captions",
    "load_word_timings",
    "segments_to_groups",
]

UNAVAILABLE_NO_ASSET = "no media asset linked to this timeline"
UNAVAILABLE_NO_WORDS = "no media_intel_words rows for this asset (run an alignment first)"
UNAVAILABLE_FOREIGN = "asset not visible in this workspace"


@dataclass(frozen=True)
class WordTiming:
    """One aligned word. ``start_s``/``end_s`` come from stored rows only."""

    word: str
    start_s: float
    end_s: float
    speaker_id: str | None = None
    confidence: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def to_dict(self) -> dict:
        return {
            "word": self.word, "start_s": self.start_s, "end_s": self.end_s,
            "speaker_id": self.speaker_id, "confidence": self.confidence,
        }


@dataclass
class CaptionGroup:
    """A caption built from words: text plus the timing of each word."""

    text: str
    start_s: float
    end_s: float
    words: list[WordTiming] = field(default_factory=list)
    speaker_id: str | None = None
    #: True when these are real word timings; False means segment-level
    #: degradation and the renderer must not draw per-word animation.
    word_level: bool = True

    @property
    def duration(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def char_spans(self) -> list[tuple[int, int]]:
        """``(start, end)`` char offsets of each word inside ``self.text``."""
        spans: list[tuple[int, int]] = []
        cursor = 0
        for word in self.words:
            idx = self.text.find(word.word, cursor)
            if idx < 0:  # defensive: never emit a bogus offset
                continue
            spans.append((idx, idx + len(word.word)))
            cursor = idx + len(word.word)
        return spans

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "speaker_id": self.speaker_id,
            "word_level": self.word_level,
            "words": [w.to_dict() for w in self.words],
        }


@dataclass
class WordTimingSource:
    """Honest availability report.

    ``available`` is the ONLY thing a caller should branch on. When it is
    False, ``reason`` says why and ``words`` is empty -- there is no partial
    or guessed mode.
    """

    available: bool
    words: list[WordTiming] = field(default_factory=list)
    reason: str = ""
    run_id: str | None = None
    asset_id: str | None = None

    @property
    def word_level(self) -> bool:
        return self.available and bool(self.words)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "run_id": self.run_id,
            "asset_id": self.asset_id,
            "word_count": len(self.words),
            "word_level": self.word_level,
        }


def load_word_timings(
    db,
    workspace_id: str,
    *,
    asset_id: str | None,
    run_id: str | None = None,
    limit: int = 20_000,
) -> WordTimingSource:
    """Read stored word rows for an asset, honestly.

    Delegates to the Work 12 alignment engine so this stays a *consumer* of
    that subsystem rather than a second query path. Returns
    ``available=False`` (never raises) when the asset has no alignment.
    """
    if not asset_id:
        return WordTimingSource(available=False, reason=UNAVAILABLE_NO_ASSET)
    try:
        from app.engine.intel import alignment
    except Exception:  # pragma: no cover - import guard
        return WordTimingSource(available=False,
                                reason="alignment engine unavailable")
    try:
        rows = alignment.list_words(
            db, workspace_id, run_id=run_id, asset_id=asset_id, limit=limit,
        )
    except Exception:
        # A read failure must not be reported as "no words" silently in a way
        # that looks like a deliberate degrade; say so in the reason.
        return WordTimingSource(available=False, asset_id=asset_id,
                                reason="word rows could not be read")
    if not rows:
        return WordTimingSource(available=False, asset_id=asset_id,
                                reason=UNAVAILABLE_NO_WORDS)
    words: list[WordTiming] = []
    for row in rows:
        text = str(row.get("word") or "").strip()
        if not text:
            continue
        try:
            start = float(row.get("start_s"))
            end = float(row.get("end_s"))
        except (TypeError, ValueError):
            # A row without usable timing is dropped, never guessed at.
            continue
        if end < start:
            start, end = end, start
        words.append(WordTiming(
            word=text, start_s=start, end_s=end,
            speaker_id=row.get("speaker_id") or None,
            confidence=(None if row.get("confidence") is None
                        else float(row["confidence"])),
        ))
    if not words:
        return WordTimingSource(available=False, asset_id=asset_id,
                                reason="word rows present but none carried usable timing")
    words.sort(key=lambda w: (w.start_s, w.end_s))
    resolved_run = run_id or str(rows[0].get("run_id") or "") or None
    return WordTimingSource(available=True, words=words, asset_id=asset_id,
                            run_id=resolved_run)


def group_words_into_captions(
    words: list[WordTiming],
    *,
    max_chars_per_line: int = 32,
    max_lines: int = 2,
    max_words: int = 8,
    max_gap_s: float = 0.6,
    break_on_speaker: bool = True,
) -> list[CaptionGroup]:
    """Group aligned words into caption-sized groups.

    Grouping uses ONLY real timings: a group spans from its first word's
    ``start_s`` to its last word's ``end_s``. A pause longer than ``max_gap_s``
    always starts a new group, and a speaker change does too when
    ``break_on_speaker`` is set (a caption spanning two speakers is a
    readability bug, not a compact one).
    """
    if not words:
        return []
    max_chars = max(8, int(max_chars_per_line)) * max(1, int(max_lines))
    groups: list[CaptionGroup] = []
    current: list[WordTiming] = []
    for word in words:
        if current:
            previous = current[-1]
            gap = word.start_s - previous.end_s
            speaker_change = (
                break_on_speaker
                and previous.speaker_id is not None
                and word.speaker_id is not None
                and previous.speaker_id != word.speaker_id
            )
            projected = len(" ".join(w.word for w in current)) + 1 + len(word.word)
            if (gap > max_gap_s or speaker_change
                    or len(current) >= max_words or projected > max_chars):
                groups.append(_make_group(current))
                current = []
        current.append(word)
    if current:
        groups.append(_make_group(current))
    return groups


def _make_group(words: list[WordTiming]) -> CaptionGroup:
    speakers = {w.speaker_id for w in words if w.speaker_id}
    return CaptionGroup(
        text=" ".join(w.word for w in words),
        start_s=min(w.start_s for w in words),
        end_s=max(w.end_s for w in words),
        words=list(words),
        speaker_id=(next(iter(speakers)) if len(speakers) == 1 else None),
        word_level=True,
    )


def segments_to_groups(
    segments: list[dict],
    *,
    max_chars_per_line: int = 32,
    max_lines: int = 2,
) -> list[CaptionGroup]:
    """Segment-level captions: the HONEST degradation path.

    Used when no word alignment exists. Every group is explicitly marked
    ``word_level=False`` so the renderer draws a static caption and never
    invents per-word animation.
    """
    max_chars = max(8, int(max_chars_per_line)) * max(1, int(max_lines))
    # A single line never exceeds the per-line budget either, so the effective
    # wrap width is the smaller of the two constraints.
    line_width = max(8, min(int(max_chars_per_line),
                            max_chars // max(1, int(max_lines))))
    groups: list[CaptionGroup] = []
    for seg in segments or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg.get("start_s"))
            end = float(seg.get("end_s"))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        # Wrap on a word boundary so the renderer never has to break mid-word.
        words = text.split()
        lines: list[list[str]] = [[]]
        length = 0
        for word in words:
            add = len(word) + (1 if length else 0)
            if length + add > line_width and lines[-1]:
                lines.append([])
                length = 0
                add = len(word)
            lines[-1].append(word)
            length += add
        # A line budget is a HARD readability constraint, so overflow becomes
        # MORE captions (splitting the segment's own time range evenly across
        # the pieces) rather than a caption that overflows the frame.
        chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        if len(chunks) > 1:
            step = max(0.05, (end - start) / len(chunks))
            for n, chunk in enumerate(chunks):
                chunk_start = start + n * step
                chunk_end = end if n == len(chunks) - 1 else start + (n + 1) * step
                groups.append(CaptionGroup(
                    text="\n".join(" ".join(line) for line in chunk if line),
                    start_s=chunk_start, end_s=chunk_end, words=[],
                    speaker_id=seg.get("speaker_id") or None, word_level=False,
                ))
            continue
        body = "\n".join(" ".join(line) for line in lines if line)
        groups.append(CaptionGroup(
            text=body, start_s=start, end_s=end, words=[],
            speaker_id=seg.get("speaker_id") or None, word_level=False,
        ))
    return groups