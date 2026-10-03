"""Script pause tags, narration-length estimation, and voice-mode resolution.

Three small pure helpers that the narration lane needs and that used to live
only inside the text of a render script.

**Pause tags.** A script may carry bracketed pause markers such as
``[pause: 2s]``, ``[停顿：1.5秒]`` or ``[пауза: 0.5s]``. They are *not*
speech: a provider that receives the literal tag will either read it out loud
or drop it, and either way the timeline gets the wrong length. This module
parses a script into an ordered list of :class:`Segment` objects so the caller
can synthesize only the speech parts and insert exact silence for the rest.

The parser is deliberately strict about what it will *honour* and total about
what it will *remove*:

* a malformed tag (``[pause: -2s]``, ``[pause: nope]``, ``[pause: 0s]``) never
  becomes a pause — it is stripped from the speech text, so it can never be
  spoken and can never become a caption;
* a too-small pause is raised to :data:`MIN_PAUSE_DURATION_SECONDS` and a
  too-long one is cut to :data:`MAX_PAUSE_DURATION_SECONDS`;
* consecutive pauses merge into one segment, capped, so a script cannot
  produce a shard of silence files.

**Duration.** Word counting is an English-only instrument. A 40-character
Chinese sentence is one "word" and would be estimated at roughly a third of a
second; Russian, Arabic, Hindi and Japanese text break it just as badly. The
estimator below separates CJK characters, Latin/numeric words and other
scripts, and adds a little air between sentences so caption switching is not
unreadably tight.

**Voice mode.** An empty voice string means "the setting is missing", not
"render without narration". Treating ``""`` as silent would turn a config typo
— a lost WebUI state, a dropped API parameter — into a video that looks
successfully produced and is silent. Only an explicit sentinel means silent;
see :func:`is_no_voice` and :func:`assert_voice_mode_explicit`.

Pause-tag behaviour and the narration-length estimator are ported from
MoneyPrinterTurbo 1.3.7 (MIT), re-derived against YMONEY's own interfaces:

    Ported from MoneyPrinterTurbo 1.3.7
    Copyright (c) 2024 Harry — MIT License
    https://github.com/harry0703/MoneyPrinterTurbo
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "CJK_CHARS_PER_SECOND",
    "LATIN_WORDS_PER_SECOND",
    "MAX_PAUSE_DURATION_SECONDS",
    "MIN_PAUSE_DURATION_SECONDS",
    "NO_VOICE_ALIASES",
    "NO_VOICE_NAME",
    "OTHER_CHARS_PER_SECOND",
    "SENTENCE_PAUSE_SECONDS",
    "Segment",
    "VoiceModeError",
    "assert_voice_mode_explicit",
    "estimate_narration_seconds",
    "has_pause_tags",
    "is_no_voice",
    "parse_script_with_pauses",
    "remove_pause_tags",
    "resolve_voice_mode",
    "silent_duration_seconds",
    "total_pause_seconds",
]

# ---------------------------------------------------------------------------
# pause tags
# ---------------------------------------------------------------------------

#: Keywords that introduce a pause, across the locales YMONEY targets
#: (en/es/de/it, zh/ja/ko, ru, ar, hi, tr, id). Matching is case-insensitive.
#: Order is irrelevant because each alternative is anchored by the trailing
#: ``\b`` and the closing bracket.
PAUSE_TAG_KEYWORDS: tuple[str, ...] = (
    # en / es / de / it
    "pause", "pausa", "silence", "silencio", "silêncio", "silenzio", "stille",
    # ru / ar / tr
    "пауза", "тишина", "توقف", "صمت", "durak", "sessiz", "sessizlik",
    # hi
    "रुकें", "रुको", "ठहरें", "मौन",
    # id
    "jeda", "diam", "hening",
    # zh / ja / ko
    "停顿", "暫停", "暂停", "静音", "ポーズ", "一時停止", "無音", "일시중지", "정지",
)

#: Matches ANY bracketed tag whose first word is a pause keyword — valid
#: argument or not. Malformed tags must match too, otherwise they survive as
#: literal speech and get narrated.
#:
#: The trailing guard rejects a letter right after the keyword, so ``[paused]``
#: is speech and not a tag. A *digit* is allowed to follow, because
#: ``[停顿1.5秒]`` (no colon) is ordinary Chinese script and ``\b`` would not
#: match there.
#:
#: Ported from MoneyPrinterTurbo 1.3.7
#: Copyright (c) 2024 Harry — MIT License
#: https://github.com/harry0703/MoneyPrinterTurbo
PAUSE_TAG_PATTERN = re.compile(
    r"[\[\(]\s*(?:"
    + "|".join(re.escape(k) for k in PAUSE_TAG_KEYWORDS)
    + r")(?![^\W\d_])(?:\s*[:：]?\s*([^\]\)]*?))?\s*[\]\)]",
    re.IGNORECASE,
)

#: A pause shorter than this is raised to it: sub-100 ms gaps are inaudible
#: and, as a separate silence file, inaudibly short.
MIN_PAUSE_DURATION_SECONDS = 0.1
#: A pause longer than this is cut to it, both individually and when merging
#: consecutive pauses, so no single marker can blow out the timeline.
MAX_PAUSE_DURATION_SECONDS = 10.0
#: Used when a tag names a pause but gives no duration (``[pause]``).
DEFAULT_PAUSE_SECONDS = 1.0

#: ``1.5s`` / ``500ms`` / ``2秒`` / ``250 毫秒`` / a bare ``2``.
_PAUSE_ARGUMENT_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|ms|msec|msecs|秒|毫秒)?$",
    re.IGNORECASE,
)

SPEECH = "speech"
PAUSE = "pause"


@dataclass(frozen=True)
class Segment:
    """One ordered piece of a script: either words to speak, or a silence.

    ``kind`` is :data:`SPEECH` (read ``text``) or :data:`PAUSE` (honour
    ``seconds``). Exactly one of the two carries meaning, which is why this is
    a tagged union rather than two parallel lists that can drift apart.
    """

    kind: str
    text: str = ""
    seconds: float = 0.0

    @property
    def is_speech(self) -> bool:
        return self.kind == SPEECH

    @property
    def is_pause(self) -> bool:
        return self.kind == PAUSE


def has_pause_tags(text: str) -> bool:
    """True when the text contains a bracketed pause marker (valid or not)."""
    return bool(text) and bool(PAUSE_TAG_PATTERN.search(text))


def remove_pause_tags(text: str) -> str:
    """Strip every pause tag, including malformed ones.

    Use before passing text to a TTS provider, a caption builder or a keyword
    extractor: a tag is not a word, so leaving it in makes the provider read
    "pause colon two s" or the caption display it verbatim.
    """
    if not text:
        return ""
    return re.sub(r"[ \t]+", " ", PAUSE_TAG_PATTERN.sub(" ", text)).strip()


def _parse_pause_seconds(raw: str | None) -> float | None:
    """Seconds for a tag argument, or ``None`` when the tag is malformed.

    ``None`` means "drop this tag": it is not a pause, and it is not speech
    either. Units are ``s`` (default) and ``ms``.
    """
    if raw is None or not raw.strip():
        return DEFAULT_PAUSE_SECONDS
    match = _PAUSE_ARGUMENT_RE.match(raw.strip())
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return value / 1000.0 if unit.startswith("ms") or unit == "毫秒" else value


def parse_script_with_pauses(script: str) -> list[Segment]:
    """Split a script into ordered speech and pause segments.

    Guarantees:

    * speech segments never contain a pause tag (malformed ones included);
    * a malformed or non-positive pause yields no pause segment at all;
    * every pause lies within ``[MIN_PAUSE_DURATION_SECONDS,
      MAX_PAUSE_DURATION_SECONDS]``;
    * runs of consecutive pauses collapse into one capped pause;
    * an empty / whitespace-only script yields ``[]``.

    Ported from MoneyPrinterTurbo 1.3.7
    Copyright (c) 2024 Harry — MIT License
    https://github.com/harry0703/MoneyPrinterTurbo
    """
    if not script or not script.strip():
        return []

    segments: list[Segment] = []
    cursor = 0
    for match in PAUSE_TAG_PATTERN.finditer(script):
        start, end = match.span()
        if start > cursor:
            speech = script[cursor:start].strip()
            if speech:
                segments.append(Segment(SPEECH, text=speech))

        seconds = _parse_pause_seconds(match.group(1))
        cursor = end
        if seconds is None or seconds <= 0:
            continue  # malformed: removed, never spoken, never a pause

        seconds = max(seconds, MIN_PAUSE_DURATION_SECONDS)
        seconds = min(seconds, MAX_PAUSE_DURATION_SECONDS)
        if segments and segments[-1].is_pause:
            segments[-1] = Segment(
                PAUSE, seconds=min(segments[-1].seconds + seconds, MAX_PAUSE_DURATION_SECONDS))
        else:
            segments.append(Segment(PAUSE, seconds=seconds))

    if cursor < len(script):
        tail = script[cursor:].strip()
        if tail:
            segments.append(Segment(SPEECH, text=tail))
    return segments


def total_pause_seconds(segments: list[Segment]) -> float:
    """Sum of the pause segments — how much silence the caller must insert."""
    return round(sum(s.seconds for s in segments if s.is_pause), 6)


# ---------------------------------------------------------------------------
# narration length
# ---------------------------------------------------------------------------

#: Mandarin/Chinese reads at roughly this many characters per second.
CJK_CHARS_PER_SECOND = 4.2
#: English (and other Latin-script) speech at roughly this many words/second.
LATIN_WORDS_PER_SECOND = 2.7
#: Cyrillic, Arabic, Devanagari, Hangul, Kana: faster than Latin words, slower
#: than CJK characters.
OTHER_CHARS_PER_SECOND = 4.0
#: Breathing room added per sentence break, so consecutive captions are legible.
SENTENCE_PAUSE_SECONDS = 0.35
#: A very short script still yields a usable timeline rather than a 0.2 s clip.
MIN_NARRATION_SECONDS = 3.0

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+")
#: CJK plus the ideographic punctuation that ends a sentence in zh/ja/ko.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?。！？…]+[\s]*|\n+")


def _script_counts(text: str) -> tuple[int, int, int, int]:
    """(cjk chars, latin words, other-script chars, sentences)."""
    cjk = len(_CJK_RE.findall(text))
    latin_words = _LATIN_WORD_RE.findall(text)
    latin_chars = sum(len(w) for w in latin_words)
    # Any letter or digit that is neither CJK nor part of a Latin/numeric word.
    other = sum(1 for ch in text if unicodedata.category(ch).startswith(("L", "N")))
    other = max(other - cjk - latin_chars, 0)
    sentences = len([p for p in _SENTENCE_BOUNDARY_RE.split(text) if p.strip()])
    return cjk, len(latin_words), other, max(sentences, 1)


def estimate_narration_seconds(
    text: str,
    *,
    words_per_second: float = LATIN_WORDS_PER_SECOND,
    chars_per_second: float = OTHER_CHARS_PER_SECOND,
    min_seconds: float = MIN_NARRATION_SECONDS,
) -> float:
    """Estimated spoken length of ``text``, in seconds.

    Counts CJK characters, Latin/numeric words and remaining-script characters
    separately, then adds :data:`SENTENCE_PAUSE_SECONDS` per sentence break and
    floors the result at ``min_seconds``.

    Ported from MoneyPrinterTurbo 1.3.7
    Copyright (c) 2024 Harry — MIT License
    https://github.com/harry0703/MoneyPrinterTurbo
    """
    normalized = (text or "").strip()
    if not normalized:
        return float(min_seconds)
    cjk, words, other, sentences = _script_counts(normalized)
    seconds = (
        cjk / CJK_CHARS_PER_SECOND
        + words / max(float(words_per_second or LATIN_WORDS_PER_SECOND), 0.1)
        + other / max(float(chars_per_second or OTHER_CHARS_PER_SECOND), 0.1)
        + max(sentences - 1, 0) * SENTENCE_PAUSE_SECONDS
    )
    return max(float(min_seconds), seconds)


def silent_duration_seconds(text: str) -> float:
    """Timeline length for a no-voice render of ``text``.

    A silent track still has to be long enough to drive clip trimming, the
    caption timeline and the final mux, so this reuses the narration estimate
    rather than inventing a constant.
    """
    return estimate_narration_seconds(text, min_seconds=MIN_NARRATION_SECONDS)


# ---------------------------------------------------------------------------
# voice mode
# ---------------------------------------------------------------------------

#: The explicit "no narration" sentinel.
NO_VOICE_NAME = "no-voice"
#: ``none`` is accepted because an earlier API revision shipped it; new code
#: and the UI use :data:`NO_VOICE_NAME`.
NO_VOICE_ALIASES: frozenset[str] = frozenset({NO_VOICE_NAME, "none"})


class VoiceModeError(ValueError):
    """A voice setting was blank where an explicit value was required."""


def is_no_voice(voice_name: str | None) -> bool:
    """True ONLY for an explicit no-voice sentinel.

    ``""``, ``None`` and whitespace are **not** silent: a blank voice is a
    missing setting, and silently rendering without narration would convert a
    configuration bug into a video that looks fine and says nothing.

    Ported from MoneyPrinterTurbo 1.3.7
    Copyright (c) 2024 Harry — MIT License
    https://github.com/harry0703/MoneyPrinterTurbo
    """
    return str(voice_name or "").strip().lower() in NO_VOICE_ALIASES


def assert_voice_mode_explicit(voice_name: str | None) -> str:
    """Return the normalized voice name, refusing a blank one.

    The guard every no-voice code path runs *before* it produces silence, so
    the decision to skip narration always rests on a sentinel the caller
    actually wrote down. Raises :class:`VoiceModeError` on a blank value.
    """
    name = str(voice_name or "").strip()
    if not name:
        raise VoiceModeError(
            "voice is empty — choose a voice, or set the explicit "
            f"{NO_VOICE_NAME!r} sentinel to render without narration"
        )
    return name


def resolve_voice_mode(voice_name: str | None, *, default: str = "") -> str:
    """The voice to synthesize with.

    Returns :data:`NO_VOICE_NAME` for an explicit sentinel, otherwise the
    normalized voice, falling back to ``default`` when the setting is blank.
    A blank setting is a *fallback*, never a silent render.
    """
    name = str(voice_name or "").strip()
    if not name:
        return default
    return NO_VOICE_NAME if name.lower() in NO_VOICE_ALIASES else name
