"""Render cache keys for caption / motion work (Work 13 §15).

Motion effects are expensive, so a render must be able to skip work that
cannot have changed. A cache key is only useful if it covers EVERY input that
changes the output. The required inputs are:

* source checksum
* timeline version
* effect parameters
* renderer version (so a builder change invalidates old entries)
* font / template version

Two jobs sharing a key are byte-identical by construction, which is the only
property that makes reusing a cached chunk safe. Note this composes with the
EXISTING chunk cache in ``engine/longform/stages_finish.py``: that cache keys
on the sliced document, and this key is folded into the document-derived part
so changing a caption style re-renders exactly the chapters that contain it.
"""

from __future__ import annotations

import hashlib
import json

from app.engine.captions.ffmpeg_escape import FONT_CANDIDATES
from app.engine.captions.filters import effect_registry_version

__all__ = [
    "CACHE_VERSION",
    "caption_cache_key",
    "chunk_render_key",
    "font_template_version",
    "measure_caption_scaling",
]

#: Bump when the KEY STRUCTURE changes (not when the renderer changes - that is
#: covered by the renderer version folded in below).
CACHE_VERSION = "w13-cache-1"

#: Bumped whenever the template registry changes shape.
TEMPLATE_VERSION = "w13-templates-1"


def font_template_version() -> str:
    """The font/template token folded into every key.

    Includes the resolved font FAMILY candidates and the template version, so
    swapping the brand font or adding a template invalidates stale entries.
    """
    payload = json.dumps({
        "fonts": list(FONT_CANDIDATES),
        "templates": TEMPLATE_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def caption_cache_key(
    *,
    source_checksum: str,
    timeline_version: int,
    style: dict | None = None,
    words: list | None = None,
    effects: list | None = None,
    preset: str = "",
    template_version: str = "",
    extra: dict | None = None,
) -> str:
    """The cache key for one caption/motion render unit.

    Every argument is optional except the source + timeline identity, so a
    caller that genuinely has no style/word data still gets a stable key rather
    than accidentally omitting an input.
    """
    payload = {
        "v": CACHE_VERSION,
        "renderer": effect_registry_version(),
        "font_template": font_template_version(),
        "source": str(source_checksum or ""),
        "timeline_version": int(timeline_version or 0),
        "style": style or {},
        "preset": str(preset or ""),
        "words": words or [],
        "effects": effects or [],
        "template": str(template_version or TEMPLATE_VERSION),
        "extra": extra or {},
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()[:32]


def chunk_render_key(*, sub_doc: dict, chunk_index: int,
                     renderer_version: str = "") -> str:
    """Key for one chunk of a chapter-sliced render.

    ``sub_doc`` is the sliced timeline document (the existing longform chunk
    cache hashes this already); the renderer version is folded in so a builder
    change re-renders every chunk instead of silently reusing stale pixels.
    """
    payload = {
        "v": CACHE_VERSION,
        "renderer": renderer_version or effect_registry_version(),
        "font_template": font_template_version(),
        "chunk": int(chunk_index),
        "doc": sub_doc,
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()[:16]


def measure_caption_scaling(
    *, caption_counts: tuple[int, ...] = (10, 100, 500)
) -> list[dict]:
    """Measure filter-graph growth as caption count rises (§15 benchmark).

    Pure and deterministic: it BUILDS the graphs and counts filters, so the
    number reported is a real cost signal (ffmpeg filter count is the dominant
    per-caption cost) rather than a guess. No rendering happens.
    """
    from app.engine.captions.filters import build_caption_filters
    from app.engine.captions.presets import get_preset

    results: list[dict] = []
    for count in caption_counts:
        style = get_preset("minimal").style
        total_filters = 0
        total_chars = 0
        for i in range(count):
            caption = {"name": f"Caption number {i}", "start": 0.0,
                       "duration": 2.0}
            plan = build_caption_filters(
                label_in=f"v{i}", caption=caption, style=style,
                width=1080, height=1920)
            total_filters += plan.filter_count
            total_chars += len(caption["name"])
        results.append({
            "captions": count,
            "filters": total_filters,
            "filters_per_caption": round(total_filters / max(1, count), 3),
            "avg_chars": round(total_chars / max(1, count), 1),
        })
    return results