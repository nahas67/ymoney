"""UGC voice stage: script segments → workspace narration assets.

Reuses `providers/tts.py` (real provider in production; the test suite
injected its mock at the boundary). Each segment becomes a `MediaAsset`
(type=voice) so voice and captions stay individually editable on the
timeline — audio is never flattened into the render.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
WORD_CHUNK = 14          # words per voiced/caption segment
DEFAULT_WORDS_PER_SECOND = 2.6   # matches ScriptAgent pacing + MockTTS


def split_segments(script: str, max_words: int = WORD_CHUNK) -> list[str]:
    """Script → voiced/caption segments (sentence-first, word-chunk fallback)."""
    out: list[str] = []
    for sentence in _SENTENCE_RE.split((script or "").strip()):
        sentence = sentence.strip()
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
    return [s for s in out if s]


def _estimate_duration(text: str) -> float:
    words = len((text or "").split())
    return max(0.6, round(words / DEFAULT_WORDS_PER_SECOND, 2))


def _provider(provider_factory: Callable[[], Any] | None):
    if provider_factory is not None:
        return provider_factory()
    from app.providers.tts import get_tts_provider

    return get_tts_provider()


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
    try:
        provider = _provider(provider_factory)
        result = provider.synthesize(text, voice=voice)
    except Exception:  # noqa: BLE001 — one failed segment never kills the run
        return None
    ext = "wav" if (result.format or "mp3") == "wav" else "mp3"
    key = get_storage().save_media(
        workspace_id, data=result.audio_bytes,
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
        duration = _estimate_duration(text)
    row = MediaAsset(
        workspace_id=workspace_id, type="voice", origin="generated",
        provider=getattr(provider, "name", "tts"), storage_key=key,
        mime_type="audio/wav" if ext == "wav" else "audio/mpeg",
        duration_seconds=duration,
        meta_json=dict(meta or {}) | {"chars": len(text), "words": len(text.split())})
    session.add(row)
    session.flush()
    return {"text": text, "asset_id": row.id, "duration": round(duration, 3),
            "words": len(text.split()), "storage_key": key}


def narrate_segments(session: Any, workspace_id: str, script: str, *,
                     provider_factory: Callable[[], Any] | None = None,
                     voice: str = "") -> list[dict]:
    """Narrate the whole script segment-by-segment (measured durations).

    Returns only the segments that produced real audio; an empty list means
    the voice stage produced nothing and the pipeline must fail loudly.
    """
    segments: list[dict] = []
    for i, text in enumerate(split_segments(script)):
        seg = narrate_text(session, workspace_id, text,
                           provider_factory=provider_factory, voice=voice,
                           meta={"segment": i})
        if seg is not None:
            segments.append(seg)
    return segments


__all__ = [
    "DEFAULT_WORDS_PER_SECOND",
    "narrate_segments",
    "narrate_text",
    "split_segments",
]
