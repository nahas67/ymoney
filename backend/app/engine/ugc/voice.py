"""UGC voice stage: script segments → workspace narration assets.

Reuses `providers/tts.py` (real provider in production; the test suite
injected its mock at the boundary). Each segment becomes a `MediaAsset`
(type=voice) so voice and captions stay individually editable on the
timeline — audio is never flattened into the render.

Three things this stage refuses to get wrong:

* **Script length is not English.** Segmentation splits on CJK sentence
  terminators and on characters when a sentence has no spaces, and the
  duration fallback counts CJK characters, Latin words and other scripts
  separately (see :mod:`app.services.pause_tags`). Word counting alone
  estimates a Chinese paragraph as a fraction of a second.
* **A pause tag is not a word.** Tags are parsed out before synthesis, so a
  provider never narrates ``[pause: 2s]`` and a caption never displays it.
* **Blank is not silent.** Only an explicit ``none`` / ``no-voice`` produces a
  silent track; an empty voice falls through to the provider default, because
  a missing setting must not masquerade as a successful silent render.
"""

from __future__ import annotations

import io
import re
import time
import wave
from collections.abc import Callable
from typing import Any

from app.services.audio_concat import SAMPLE_RATE, SAMPLE_WIDTH, silence_pcm
from app.services.pause_tags import (
    NO_VOICE_NAME,
    assert_voice_mode_explicit,
    estimate_narration_seconds,
    has_pause_tags,
    is_no_voice,
    parse_script_with_pauses,
    remove_pause_tags,
    silent_duration_seconds,
)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
#: CJK sentences end at 。！？… — splitting only on ASCII full stops turns a
#: whole Chinese paragraph into one unspeakably long segment.
_CJK_SENTENCE_RE = re.compile(r"(?<=[。！？…])\s*")
WORD_CHUNK = 14          # words per voiced/caption segment
DEFAULT_WORDS_PER_SECOND = 2.6   # matches ScriptAgent pacing + MockTTS


def _sentences(script: str) -> list[str]:
    """Split on ASCII and CJK sentence terminators, order preserved."""
    out: list[str] = []
    for chunk in _CJK_SENTENCE_RE.split(script):
        out.extend(_SENTENCE_RE.split(chunk))
    return [s.strip() for s in out if s.strip()]


def split_segments(script: str, max_words: int = WORD_CHUNK) -> list[str]:
    """Script → voiced/caption segments (sentence-first, word-chunk fallback).

    A script with no whitespace (Chinese, Japanese, Thai) is chunked by
    character count instead, because ``str.split()`` sees one enormous "word"
    and would hand the whole paragraph to a single synthesis call.
    """
    out: list[str] = []
    for sentence in _sentences(remove_pause_tags(script)):
        if len(sentence.split()) >= 2 or " " in sentence:
            while sentence:
                words = sentence.split()
                if len(words) <= max_words:
                    out.append(sentence)
                    break
                cut = sentence.rfind(" ", 0, max_words * 6)
                chunk = sentence[:cut].strip() if cut > 0 else " ".join(words[:max_words])
                if not chunk:
                    chunk = " ".join(words[:max_words])
                out.append(chunk)
                sentence = sentence[len(chunk):].strip()
        else:
            # No spaces at all: split on characters so each piece is speakable.
            for i in range(0, len(sentence), max_words * 2):
                piece = sentence[i:i + max_words * 2].strip()
                if piece:
                    out.append(piece)
    return [s for s in out if s]


def _estimate_duration(text: str) -> float:
    """Fallback length when ffprobe cannot measure the produced audio.

    Counts CJK characters, Latin words and other scripts separately — plain
    word counting estimates a 40-character Chinese sentence at well under a
    second and the whole timeline collapses around it.
    """
    return max(0.6, round(estimate_narration_seconds(
        text, words_per_second=DEFAULT_WORDS_PER_SECOND, min_seconds=0.6), 2))


def _provider(provider_factory: Callable[[], Any] | None):
    if provider_factory is not None:
        return provider_factory()
    from app.providers.tts import get_tts_provider

    return get_tts_provider()


def silence_wav_bytes(duration_seconds: float) -> bytes:
    """A real 24 kHz mono WAV of silence, in memory.

    No encoder, no ffmpeg, no temporary file: the sample count is exact, so a
    caption timeline derived from it matches the track sample for sample.
    """
    pcm = silence_pcm(duration_seconds)
    if not pcm:
        raise ValueError(
            f"silent narration would be zero-length ({duration_seconds!r}s) — "
            "refusing to write an empty placeholder"
        )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def narrate_text(session: Any, workspace_id: str, text: str, *,
                 provider_factory: Callable[[], Any] | None = None,
                 voice: str = "", meta: dict | None = None) -> dict | None:
    """Synthesize ONE segment → workspace storage + MediaAsset row.

    Returns {text, asset_id, duration, words, storage_key} or None when the
    voice provider is unavailable (caller degrades honestly — never a
    silent fake narration).
    """
    from app.models.assets import MediaAsset
    from app.services.storage import get_storage, managed_path, probe_metadata

    text = (text or "").strip()
    if not text:
        return None
    # A pause tag is not speech. It is removed here so it can never be
    # narrated and never becomes a caption word.
    text = remove_pause_tags(text) or text

    silent = False
    provider: Any = None
    audio_bytes = b""
    ext = "wav"
    estimated = silent_duration_seconds(text)

    if is_no_voice(voice):
        # The sentinel must be explicit: a blank voice is a missing setting,
        # not a request for silence, and falls through to the provider below.
        assert_voice_mode_explicit(voice)
        audio_bytes = silence_wav_bytes(estimated)
        ext = "wav"
        silent = True
    else:
        try:
            provider = _provider(provider_factory)
            result = provider.synthesize(text, voice=voice)
        except Exception:  # noqa: BLE001 — one failed segment never kills the run
            return None
        audio_bytes = result.audio_bytes
        ext = "wav" if (result.format or "mp3") == "wav" else "mp3"

    key = get_storage().save_media(
        workspace_id, data=audio_bytes,
        filename=f"ugc_voice_{int(time.time() * 1000)}_{abs(hash(text)) % 100000}.{ext}")
    duration = 0.0
    path = managed_path(workspace_id, key)
    if path is not None:
        meta_probe = probe_metadata(path)
        try:
            duration = float(meta_probe.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
    if duration <= 0:
        duration = estimated if silent else _estimate_duration(text)
    row = MediaAsset(
        workspace_id=workspace_id, type="voice", origin="generated",
        provider="silent" if silent else getattr(provider, "name", "tts"),
        storage_key=key,
        mime_type="audio/wav" if ext == "wav" else "audio/mpeg",
        duration_seconds=duration,
        meta_json=dict(meta or {}) | {"chars": len(text), "words": len(text.split()),
                                      "silent": silent})
    session.add(row)
    session.flush()
    out = {"text": text, "asset_id": row.id, "duration": round(duration, 3),
           "words": len(text.split()), "storage_key": key, "silent": silent,
           # Provenance: which engine actually spoke this line. Without it a
           # caption/UX layer cannot tell a real voice from a silent track, and
           # a provider swap is invisible after the fact.
           "provider": (NO_VOICE_NAME if silent
                        else str(getattr(provider, "name", "") or
                                 type(provider).__name__))}
    if silent:
        out["estimated"] = True
    return out


def narrate_segments(session: Any, workspace_id: str, script: str, *,
                     provider_factory: Callable[[], Any] | None = None,
                     voice: str = "") -> list[dict]:
    """Narrate the whole script segment-by-segment (measured durations).

    A script carrying pause tags is narrated run-by-run with real silence
    synthesized between the runs, so the segment durations reflect the pauses
    the author asked for instead of the pauses being dropped.

    Returns only the segments that produced real audio; an empty list means
    the voice stage produced nothing and the pipeline must fail loudly.
    """
    if has_pause_tags(script):
        return _narrate_pauses(session, workspace_id, script,
                               provider_factory=provider_factory, voice=voice)
    segments: list[dict] = []
    for i, text in enumerate(split_segments(script)):
        seg = narrate_text(session, workspace_id, text,
                           provider_factory=provider_factory, voice=voice,
                           meta={"segment": i})
        if seg is not None:
            segments.append(seg)
    return segments


def _store_silence(session: Any, workspace_id: str, seconds: float, meta: dict) -> dict:
    """Persist one silent MediaAsset of exactly ``seconds``.

    A pause still has to occupy the timeline, so it becomes a real (silent)
    asset rather than a hole the renderer would have to guess about.
    """
    from app.models.assets import MediaAsset
    from app.services.storage import get_storage, managed_path, probe_metadata

    key = get_storage().save_media(
        workspace_id, data=silence_wav_bytes(seconds),
        filename=f"ugc_pause_{int(time.time() * 1000)}_{abs(hash(seconds)) % 100000}.wav")
    duration = 0.0
    path = managed_path(workspace_id, key)
    if path is not None:
        try:
            duration = float(probe_metadata(path).get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
    if duration <= 0:
        duration = seconds
    row = MediaAsset(
        workspace_id=workspace_id, type="voice", origin="generated", provider="silent",
        storage_key=key, mime_type="audio/wav", duration_seconds=duration,
        meta_json=dict(meta) | {"silent": True, "chars": 0, "words": 0,
                                "pause_seconds": round(seconds, 3)})
    session.add(row)
    session.flush()
    return {"text": "", "asset_id": row.id, "duration": round(duration, 3),
            "words": 0, "storage_key": key, "silent": True, "pause": True}


def _narrate_pauses(session: Any, workspace_id: str, script: str, *,
                    provider_factory: Callable[[], Any] | None,
                    voice: str) -> list[dict]:
    """Speech runs separated by exact-length silent segments."""
    out: list[dict] = []
    for i, segment in enumerate(parse_script_with_pauses(script)):
        if segment.is_pause:
            out.append(_store_silence(session, workspace_id, segment.seconds,
                                      {"segment": i}))
            continue
        seg = narrate_text(session, workspace_id, segment.text,
                           provider_factory=provider_factory, voice=voice,
                           meta={"segment": i})
        if seg is not None:
            out.append(seg)
    return out


__all__ = [
    "DEFAULT_WORDS_PER_SECOND",
    "WORD_CHUNK",
    "narrate_segments",
    "narrate_text",
    "silence_wav_bytes",
    "split_segments",
]
