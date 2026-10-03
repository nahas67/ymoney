"""Work 13 caption + motion engine.

Everything here is an EXTENSION of the canonical timeline (Work 02) and the
existing renderer (Work 01). No second caption engine, no second render path.

Modules:
    style        typed caption style + animation (schema, validation)
    presets      bounded, reusable caption presets (configuration only)
    emphasis     deterministic semantic emphasis (§3)
    words        word-level alignment consumption + honest degradation (§2)
    filters      the single caption/motion -> ffmpeg filter graph builder
    qc           CaptionMotionQC (§16)
"""

from app.engine.captions.presets import (
    PRESETS,
    CaptionPreset,
    UnknownPresetError,
    effective_preset,
    get_preset,
    preset_names,
    resolve_preset_chain,
)
from app.engine.captions.style import (
    CaptionAnimation,
    CaptionStyle,
    CaptionStyleError,
    validate_style_patch,
)

__all__ = [
    "CaptionAnimation",
    "CaptionPreset",
    "CaptionStyle",
    "CaptionStyleError",
    "PRESETS",
    "UnknownPresetError",
    "effective_preset",
    "get_preset",
    "preset_names",
    "resolve_preset_chain",
    "validate_style_patch",
]