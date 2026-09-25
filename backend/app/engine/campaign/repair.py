"""Clip context repair: sentence-clean clip boundaries.

Transcript segments are speech — they are never rewritten. When a clip
boundary cuts mid-sentence the fragment is either extended to the sentence
edge (when most of the sentence is inside the range) or dropped (when only
a sliver is inside). Optional hook/CTA lines are generated wrappers: they
are tracked as ``{generated: True}`` entries and never mixed into quoted
speech.
"""

from __future__ import annotations

# A sentence boundary inside double quotes does not split: quoted speech is
# atomic and must never be altered or re-punctuated.
_QUOTE_CHARS = {'"': '"', "\u201c": "\u201d"}


def split_sentences(text: str) -> list[str]:
    """Split on sentence terminators outside quoted spans."""
    sentences: list[str] = []
    current: list[str] = []
    quote_open: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote_open is None and ch in _QUOTE_CHARS:
            quote_open = _QUOTE_CHARS[ch]
            current.append(ch)
        elif quote_open is not None and ch == quote_open:
            quote_open = None
            current.append(ch)
        elif quote_open is None and ch in ".!?…":
            current.append(ch)
            # consume closing quote + whitespace run
            j = i + 1
            while j < len(text) and text[j] in "'\"\u201d":
                current.append(text[j])
                j += 1
            sentences.append("".join(current).strip())
            current = []
            while j < len(text) and text[j].isspace():
                j += 1
            i = j
            continue
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
    """(char_start, char_end, sentence) offsets into text."""
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for sent in split_sentences(text):
        start = text.find(sent, cursor)
        if start < 0:
            start = cursor
        end = start + len(sent)
        spans.append((start, end, sent))
        cursor = end
    return spans


def _char_at_time(segments: list[dict], full_text: str, seg_offsets: list[int], moment: float) -> int:
    """Char offset in full_text best matching a timestamp (by segment time)."""
    for seg, offset in zip(segments, seg_offsets):
        try:
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", s))
        except (TypeError, ValueError):
            continue
        if s <= moment <= e:
            frac = 0.0 if e <= s else (moment - s) / (e - s)
            return offset + int(frac * len(str(seg.get("text") or "")))
    if segments:
        try:
            if moment <= float(segments[0].get("start", 0.0)):
                return 0
        except (TypeError, ValueError):
            pass
    return len(full_text)


class ClipContextRepair:
    """Trim/extend clip windows to clean sentence boundaries."""

    # A boundary fragment covering >= this share of its sentence is extended
    # to the sentence edge; below it the fragment sentence is dropped.
    EXTEND_THRESHOLD = 0.5

    def repair(
        self,
        *,
        start: float,
        end: float,
        segments: list[dict],
        hook_text: str = "",
        cta_text: str = "",
    ) -> dict:
        """Return repaired {start, end, text, segments, generated, ...}.

        segments: [{start, end, text}]. Quoted speech is never altered;
        hook/cta lines come back as labeled ``generated`` entries.
        """
        segs = [dict(s) for s in (segments or [])]
        start = float(start)
        end = float(end)
        if end <= start:
            raise ValueError(f"invalid clip range [{start}, {end}]")

        full_text = " ".join(str(s.get("text") or "").strip() for s in segs).strip()
        offsets: list[int] = []
        cursor = 0
        for s in segs:
            offsets.append(cursor)
            cursor += len(str(s.get("text") or "").strip()) + 1

        spans = _sentence_spans(full_text)
        if not spans:
            return self._result(start, end, "", segs, 0, 0, False, hook_text, cta_text)

        start_char = max(0, _char_at_time(segs, full_text, offsets, start))
        end_char = min(len(full_text), _char_at_time(segs, full_text, offsets, end))

        dropped_leading = 0
        dropped_trailing = 0
        extended = False
        new_start_char = start_char
        new_end_char = end_char

        # Leading fragment: inside sentence i but not at its start.
        for i, (s0, s1, _sent) in enumerate(spans):
            if s0 < start_char < s1:
                covered = (s1 - start_char) / max(1, s1 - s0)
                if covered >= self.EXTEND_THRESHOLD:
                    new_start_char = s0
                    extended = True
                else:
                    new_start_char = s1
                    dropped_leading = 1
                break
            if start_char <= s0:
                # In a gap between sentences: align to the next sentence start.
                new_start_char = s0
                break

        # Trailing fragment: inside sentence j but not at its end.
        for s0, s1, _sent in spans:
            if s0 < end_char < s1:
                covered = (end_char - s0) / max(1, s1 - s0)
                if covered >= self.EXTEND_THRESHOLD:
                    new_end_char = s1
                    extended = True
                else:
                    new_end_char = s0
                    dropped_trailing = 1
                break

        kept = full_text[new_start_char:new_end_char].strip()
        new_start = self._time_at_char(segs, offsets, full_text, new_start_char, start, before=True)
        new_end = self._time_at_char(segs, offsets, full_text, new_end_char, end, before=False)
        if new_end <= new_start:
            new_start, new_end = start, end
            kept = full_text[start_char:end_char].strip()
            dropped_leading = 0
            dropped_trailing = 0
            extended = False

        kept_segs = [
            s for s in segs
            if self._seg_overlaps(float(s.get("start", 0.0)), float(s.get("end", 0.0)), new_start, new_end)
        ]
        return self._result(
            new_start, new_end, kept, kept_segs,
            dropped_leading, dropped_trailing, extended,
            hook_text, cta_text,
        )

    @staticmethod
    def _seg_overlaps(s: float, e: float, start: float, end: float) -> bool:
        return e > start and s < end

    @staticmethod
    def _time_at_char(
        segs: list[dict],
        offsets: list[int],
        full_text: str,
        char: int,
        fallback: float,
        *,
        before: bool,
    ) -> float:
        for seg, offset in zip(segs, offsets):
            text = str(seg.get("text") or "").strip()
            if offset <= char <= offset + len(text):
                try:
                    s = float(seg.get("start", 0.0))
                    e = float(seg.get("end", s))
                except (TypeError, ValueError):
                    continue
                frac = 0.0 if not text else (char - offset) / max(1, len(text))
                t = s + frac * max(0.0, e - s)
                # Snap to the segment edge in the repair direction so the
                # repaired window never starts/ends mid-word of a neighbor.
                if before and frac < 0.05:
                    return s
                if not before and frac > 0.95:
                    return e
                return t
        return fallback

    @staticmethod
    def _result(
        start: float,
        end: float,
        text: str,
        segments: list[dict],
        dropped_leading: int,
        dropped_trailing: int,
        extended: bool,
        hook_text: str,
        cta_text: str,
    ) -> dict:
        generated: list[dict] = []
        if hook_text and hook_text.strip():
            generated.append({"kind": "hook", "text": hook_text.strip(), "generated": True})
        if cta_text and cta_text.strip():
            generated.append({"kind": "cta", "text": cta_text.strip(), "generated": True})
        return {
            "start": start,
            "end": end,
            "text": text,
            "segments": segments,
            "dropped_leading": dropped_leading,
            "dropped_trailing": dropped_trailing,
            "extended": extended,
            "generated": generated,
        }
