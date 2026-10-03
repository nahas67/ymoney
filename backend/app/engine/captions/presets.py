"""Caption presets (Work 13 §4).

A preset is *configuration only*: a :class:`~app.engine.captions.style.CaptionStyle`
plus its animation and emphasis defaults. There is exactly ONE rendering path
(``app.engine.captions.filters``); a preset never selects a different
renderer, a different ffmpeg mode, or a different pipeline. That is the
difference between a preset and a template.

BrandDNA can override a preset (fonts, colours, placement, animation,
forbidden presets, safe zones) through
:func:`effective_preset` -- brand always wins over the preset default.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from app.engine.captions.style import (
    CaptionStyle,
    CaptionStyleError,
)

__all__ = [
    "PRESET_NAMES",
    "CaptionPreset",
    "PRESETS",
    "UnknownPresetError",
    "effective_preset",
    "get_preset",
    "preset_names",
    "resolve_preset_chain",
]


class UnknownPresetError(ValueError):
    """Requested a caption preset that is not in the bounded registry."""


@dataclass(frozen=True)
class CaptionPreset:
    """One named, reusable caption look."""

    key: str
    label: str
    style: CaptionStyle
    #: Emphasis kinds this preset highlights by default (§3).
    default_emphasis: tuple[str, ...] = ("KEYWORD", "NUMBER")
    #: Max characters per line; drives caption grouping (§2).
    max_chars_per_line: int = 32
    #: Max lines; drives caption grouping and QC overflow checks (§16).
    max_lines: int = 2
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "style": self.style.to_dict(),
            "default_emphasis": list(self.default_emphasis),
            "max_chars_per_line": self.max_chars_per_line,
            "max_lines": self.max_lines,
            "description": self.description,
        }


def _preset(key: str, label: str, description: str, **style_kwargs) -> CaptionPreset:
    emphasis = style_kwargs.pop("default_emphasis", ("KEYWORD", "NUMBER"))
    max_chars = style_kwargs.pop("max_chars_per_line", 32)
    max_lines = style_kwargs.pop("max_lines", 2)
    # `style_kwargs` is now exactly a caption-style mapping; CaptionStyle
    # validates it (including the nested `animation` block) in one place.
    style = CaptionStyle.from_dict(style_kwargs)
    return CaptionPreset(
        key=key, label=label, style=style,
        default_emphasis=tuple(emphasis),
        max_chars_per_line=int(max_chars),
        max_lines=int(max_lines),
        description=description,
    )


#: The bounded registry. Adding a preset is a one-line change here; nothing
#: downstream enumerates alternatives.
PRESETS: dict[str, CaptionPreset] = {
    p.key: p for p in (
        _preset(
            "minimal", "Minimal",
            "Quiet, high-contrast captions that stay out of the way.",
            size=48, weight=500, stroke_width=1, shadow=False,
            animation={"entrance": "fade", "duration": 0.2},
            max_chars_per_line=36, max_lines=2,
        ),
        _preset(
            "bold_shorts", "Bold Shorts",
            "High-impact short-form captions with a punchy word pop.",
            size=76, weight=800, stroke_width=4, case="upper",
            animation={"entrance": "pop", "word_animation": "pop",
                       "duration": 0.22, "easing": "ease_out"},
            max_chars_per_line=20, max_lines=2,
        ),
        _preset(
            "karaoke", "Karaoke",
            "Per-word highlight driven by real word timestamps.",
            size=64, weight=700, stroke_width=3,
            animation={"entrance": "none", "word_animation": "karaoke",
                       "highlight_animation": "glow", "duration": 0.1},
            default_emphasis=("KEYWORD",),
            max_chars_per_line=24, max_lines=2,
        ),
        _preset(
            "podcast", "Podcast",
            "Two-line, speaker-friendly captions for talking-head audio.",
            size=44, weight=500, position="bottom", align="left",
            background="#000000", padding=16, corner_radius=10,
            animation={"entrance": "fade", "duration": 0.18},
            max_chars_per_line=44, max_lines=2,
        ),
        _preset(
            "documentary", "Documentary",
            "Restrained, letter-spaced captions for long-form.",
            size=42, weight=400, stroke_width=1, shadow=True,
            line_spacing=1.35,
            animation={"entrance": "fade", "easing": "ease_in_out",
                       "duration": 0.4},
            max_chars_per_line=48, max_lines=2,
        ),
        _preset(
            "educational", "Educational",
            "Calm captions that highlight the numbers being taught.",
            size=54, weight=600, position="bottom", line_spacing=1.3,
            animation={"entrance": "reveal", "duration": 0.3,
                       "word_animation": "highlight"},
            default_emphasis=("NUMBER", "KEYWORD"),
            max_chars_per_line=38, max_lines=3,
        ),
        _preset(
            "news", "News",
            "Dense lower-third style captions with strong legibility.",
            size=50, weight=700, case="upper", background="#0b0b0b",
            padding=14, corner_radius=6, stroke_width=2,
            animation={"entrance": "slide_left", "duration": 0.25},
            max_chars_per_line=34, max_lines=2,
        ),
        _preset(
            "ugc", "UGC",
            "Native-feeling captions with frequent word pops.",
            size=62, weight=800, stroke_width=3, primary_color="#ffe94a",
            animation={"entrance": "pop", "word_animation": "bounce",
                       "duration": 0.18, "easing": "ease_out"},
            default_emphasis=("EMOTION_CUE", "CTA", "NUMBER"),
            max_chars_per_line=22, max_lines=2,
        ),
        _preset(
            "brand_primary", "Brand Primary",
            "The brand's own caption look; BrandDNA is authoritative here.",
            size=58, weight=700, stroke_width=3,
            animation={"entrance": "fade", "duration": 0.25},
            max_chars_per_line=30, max_lines=2,
        ),
    )
}

#: Stable public ordering for UIs and API listings.
PRESET_NAMES: tuple[str, ...] = tuple(PRESETS)


def preset_names() -> list[str]:
    return list(PRESET_NAMES)


def get_preset(key: str) -> CaptionPreset:
    """Look up a preset. Unknown keys raise -- never silently fall back."""
    name = str(key or "").strip().lower()
    preset = PRESETS.get(name)
    if preset is None:
        raise UnknownPresetError(
            f"unknown caption preset {key!r}; known: {list(PRESET_NAMES)}"
        )
    return preset


def effective_preset(key: str, *, brand_patch: dict | None = None) -> CaptionPreset:
    """Resolve a preset with an optional BrandDNA override applied.

    ``brand_patch`` is the already-resolved ``caption_style`` mapping from
    ``EffectiveCreativePolicy``. Brand wins over the preset: the preset is the
    default, the brand is the rule. An invalid brand patch raises
    :class:`CaptionStyleError` so an off-brand value is never half-applied.
    """
    preset = get_preset(key)
    patch = dict(brand_patch or {})
    # A brand may pin the preset itself (that is what `preset` means in the
    # existing `_preset_from_style` helper); strip it, it is not a style key.
    patch.pop("preset", None)
    patch.pop("style", None)
    patch.pop("name", None)
    if not patch:
        return preset
    style = preset.style.patch(patch)
    return CaptionPreset(
        key=preset.key,
        label=preset.label,
        style=style,
        default_emphasis=preset.default_emphasis,
        max_chars_per_line=preset.max_chars_per_line,
        max_lines=preset.max_lines,
        description=preset.description,
    )


def resolve_preset_chain(
    preset_key: str,
    *,
    brand_patch: dict | None = None,
    clip_patch: dict | None = None,
) -> tuple[CaptionPreset, CaptionStyle]:
    """Resolve preset -> brand -> per-caption override.

    Returns ``(preset, final_style)`` so the caller can still report which
    preset produced the look (the editor shows it) while rendering the most
    specific style.
    """
    preset = effective_preset(preset_key, brand_patch=brand_patch)
    style = preset.style
    if clip_patch:
        # A malformed per-clip override must not destroy a valid preset; the
        # QC pass (§16) reports it as a style warning instead.
        with suppress(CaptionStyleError):
            style = style.patch(clip_patch)
    return preset, style