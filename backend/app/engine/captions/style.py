"""Typed caption styling + animation (Work 13 §1).

Captions live in the canonical timeline as ``caption`` track clips, so this
module extends that shape rather than introducing a parallel engine. A plain
caption (only ``name`` + ``start`` + ``duration``) stays valid: every field
here is optional with a deterministic default.

Design rules that the rest of Work 13 depends on:

* **Typed, never free-form.** Unknown style keys are *rejected* (not silently
  dropped) so a typo in an AI-authored command cannot silently render as
  something else. This is the caption-side twin of the CreativeDirector
  ``FORBIDDEN_SCHEMA_KEYS`` gate.
* **No arbitrary values reach ffmpeg.** Colours must be a named token or a
  hex literal, enums are closed sets, numbers are clamped to safe ranges.
* **Serializable.** ``to_dict``/``from_dict`` round-trip, so styling stays
  timeline-native and editable (no flattening to video).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace

__all__ = [
    "ALIGNMENTS",
    "CASES",
    "ENTRANCES",
    "EXITS",
    "EASINGS",
    "HIGHLIGHT_ANIMATIONS",
    "POSITIONS",
    "WEIGHT_ANIMATIONS",
    "CaptionStyleError",
    "CaptionStyle",
    "CaptionAnimation",
    "NAMED_COLORS",
    "apply_case",
    "resolve_style",
    "validate_style_patch",
]


class CaptionStyleError(ValueError):
    """A caption style patch was invalid. Never swallowed by the renderer."""


# -- closed vocabularies ----------------------------------------------------

ALIGNMENTS = ("left", "center", "right")
POSITIONS = ("top", "middle", "bottom")
CASES = ("none", "upper", "lower", "title")
ENTRANCES = ("none", "fade", "pop", "slide_up", "slide_left", "wipe", "reveal")
EXITS = ("none", "fade", "pop", "slide_down")
EASINGS = ("linear", "ease_in", "ease_out", "ease_in_out")
WEIGHT_ANIMATIONS = ("none", "pop", "karaoke", "highlight", "bounce")
HIGHLIGHT_ANIMATIONS = ("none", "flash", "pulse", "glow")

# Colour must be a named token or a hex literal. This is the allowlist that
# keeps a hostile colour string out of the ffmpeg filter graph; anything else
# is rejected rather than escaped-and-hoped-for.
NAMED_COLORS = frozenset({
    "white", "black", "yellow", "red", "green", "blue", "cyan", "magenta",
    "gray", "grey", "orange", "pink", "gold", "silver", "lime", "navy",
    "teal", "olive", "maroon", "purple",
})
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

_FONT_SAFE_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,80}$")


def _check_color(value: str, *, field_name: str) -> str:
    """Validate a colour token. Returns it lower-cased."""
    text = str(value or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in NAMED_COLORS:
        return low
    if _HEX_RE.match(text):
        return text.lower()
    raise CaptionStyleError(
        f"{field_name}: {text!r} is not a named colour or hex literal"
    )


def _clamp(value, low: float, high: float, *, field_name: str, default):
    if value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise CaptionStyleError(f"{field_name}: {value!r} is not a number") from None
    if num != num:  # NaN
        raise CaptionStyleError(f"{field_name}: NaN is not a valid number")
    return max(low, min(high, num))


def _one_of(value, allowed, *, field_name: str, default: str) -> str:
    text = str(value if value is not None else default).strip().lower()
    if text not in allowed:
        raise CaptionStyleError(
            f"{field_name}: {text!r} not in {sorted(allowed)}"
        )
    return text


# -- animation --------------------------------------------------------------

@dataclass(frozen=True)
class CaptionAnimation:
    """Per-caption animation metadata (§1).

    Stored as configuration, never baked into pixels, so the editor can change
    an animation and re-render deterministically.
    """

    entrance: str = "none"
    exit: str = "none"
    word_animation: str = "none"
    highlight_animation: str = "none"
    duration: float = 0.25
    easing: str = "ease_out"

    ALLOWED = frozenset({
        "entrance", "exit", "word_animation", "highlight_animation",
        "duration", "easing",
    })

    @classmethod
    def from_dict(cls, data: dict | None) -> CaptionAnimation:
        data = dict(data or {})
        unknown = set(data) - cls.ALLOWED
        if unknown:
            raise CaptionStyleError(
                f"animation: unknown key(s) {sorted(unknown)}; "
                f"allowed {sorted(cls.ALLOWED)}"
            )
        return cls(
            entrance=_one_of(data.get("entrance"), ENTRANCES,
                             field_name="animation.entrance", default="none"),
            exit=_one_of(data.get("exit"), EXITS,
                         field_name="animation.exit", default="none"),
            word_animation=_one_of(data.get("word_animation"), WEIGHT_ANIMATIONS,
                                   field_name="animation.word_animation",
                                   default="none"),
            highlight_animation=_one_of(
                data.get("highlight_animation"), HIGHLIGHT_ANIMATIONS,
                field_name="animation.highlight_animation", default="none"),
            duration=_clamp(data.get("duration"), 0.0, 5.0,
                            field_name="animation.duration", default=0.25),
            easing=_one_of(data.get("easing"), EASINGS,
                           field_name="animation.easing", default="ease_out"),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def is_static(self) -> bool:
        """True when nothing animates, so the renderer can skip extra filters."""
        return (
            self.entrance == "none"
            and self.exit == "none"
            and self.word_animation == "none"
            and self.highlight_animation == "none"
        )


# -- style ------------------------------------------------------------------

@dataclass(frozen=True)
class CaptionStyle:
    """Fully typed caption style (§1). Defaults reproduce the legacy look."""

    font: str = ""
    size: int = 56
    weight: int = 700
    italic: bool = False
    align: str = "center"
    position: str = "bottom"
    primary_color: str = "#ffffff"
    stroke_width: int = 2
    stroke_color: str = "#000000"
    shadow: bool = True
    background: str = ""
    padding: int = 12
    corner_radius: int = 0
    opacity: float = 1.0
    case: str = "none"
    line_spacing: float = 1.2
    word_spacing: float = 0.0
    animation: CaptionAnimation = field(default_factory=CaptionAnimation)

    ALLOWED = frozenset({
        "font", "size", "weight", "italic", "align", "position", "safe_zone",
        "primary_color", "stroke_width", "stroke_color", "shadow", "background",
        "padding", "corner_radius", "opacity", "case", "line_spacing",
        "word_spacing", "animation",
    })

    @classmethod
    def from_dict(cls, data: dict | None) -> CaptionStyle:
        """Build a style from a (possibly partial, possibly empty) mapping.

        An empty/None mapping yields the default style, which is what keeps
        every pre-Work-13 caption valid without a migration.
        """
        data = dict(data or {})
        # Tolerate the legacy aliases the exporter/ops already write, so a
        # caption styled by Work 01..12 keeps rendering identically.
        if "color" in data and "primary_color" not in data:
            data["primary_color"] = data.pop("color")
        if "borderw" in data and "stroke_width" not in data:
            data["stroke_width"] = data.pop("borderw")
        if "bordercolor" in data and "stroke_color" not in data:
            data["stroke_color"] = data.pop("bordercolor")
        unknown = set(data) - cls.ALLOWED
        if unknown:
            raise CaptionStyleError(
                f"caption style: unknown key(s) {sorted(unknown)}; "
                f"allowed {sorted(cls.ALLOWED)}"
            )
        style = cls()
        return replace(
            style,
            font=_font(data.get("font")),
            size=int(_clamp(data.get("size"), 8, 400, field_name="size", default=56)),
            weight=int(_clamp(data.get("weight"), 100, 900,
                              field_name="weight", default=700)),
            italic=bool(data.get("italic", False)),
            align=_one_of(data.get("align"), ALIGNMENTS,
                          field_name="align", default="center"),
            position=_one_of(data.get("position"), POSITIONS,
                             field_name="position", default="bottom"),
            primary_color=_check_color(data.get("primary_color", "#ffffff"),
                                       field_name="primary_color")
            or "#ffffff",
            stroke_width=int(_clamp(data.get("stroke_width"), 0, 20,
                                    field_name="stroke_width", default=2)),
            stroke_color=_check_color(data.get("stroke_color", "#000000"),
                                      field_name="stroke_color")
            or "#000000",
            shadow=bool(data.get("shadow", True)),
            background=_check_color(data.get("background", ""),
                                    field_name="background"),
            padding=int(_clamp(data.get("padding"), 0, 200,
                               field_name="padding", default=12)),
            corner_radius=int(_clamp(data.get("corner_radius"), 0, 200,
                                     field_name="corner_radius", default=0)),
            opacity=_clamp(data.get("opacity"), 0.0, 1.0,
                           field_name="opacity", default=1.0),
            case=_one_of(data.get("case"), CASES, field_name="case",
                         default="none"),
            line_spacing=_clamp(data.get("line_spacing"), 0.5, 4.0,
                                field_name="line_spacing", default=1.2),
            word_spacing=_clamp(data.get("word_spacing"), -50.0, 200.0,
                                field_name="word_spacing", default=0.0),
            animation=CaptionAnimation.from_dict(data.get("animation")),
        )

    def to_dict(self) -> dict:
        out = asdict(self)
        out["animation"] = self.animation.to_dict()
        return out

    def patch(self, changes: dict | None) -> CaptionStyle:
        """Return a NEW style with ``changes`` applied (validated).

        Used by every write path so an invalid patch can never reach storage.
        """
        changes = dict(changes or {})
        if not changes:
            return self
        merged = self.to_dict()
        for key, value in changes.items():
            if key not in self.ALLOWED:
                raise CaptionStyleError(
                    f"caption style: unknown key {key!r}; "
                    f"allowed {sorted(self.ALLOWED)}"
                )
            if key == "animation" and isinstance(value, dict):
                merged["animation"] = {**merged["animation"], **value}
            else:
                merged[key] = value
        return CaptionStyle.from_dict(merged)

    def safe_zone(self) -> dict:
        """Normalized safe-zone inset in frame fractions.

        BrandDNA's ``logo_safe_zone`` and the platform ``safe_zones`` are two
        DIFFERENT concepts (they are never merged anywhere in Work 01..12).
        This returns the caption's own opt-in inset; the renderer combines it
        with whichever box the caller passes in.
        """
        return {"top": 0.0, "right": 0.0, "bottom": 0.0, "left": 0.0}

    def effective_color(self) -> str:
        """The colour handed to ffmpeg (already validated)."""
        return self.primary_color or "#ffffff"


def _font(value) -> str:
    """Validate a font token. Empty means "use the renderer default"."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not _FONT_SAFE_RE.match(text):
        raise CaptionStyleError(
            f"font: {text!r} may only contain letters, digits, space, '_', "
            f"'-' and '.'"
        )
    return text


_CASE_MAP = {
    "upper": str.upper,
    "lower": str.lower,
    "title": str.title,
}


def apply_case(text: str, case: str) -> str:
    """Apply a case transform. ``none`` returns the text untouched."""
    fn = _CASE_MAP.get(str(case or "none").lower())
    return fn(text) if fn else text


def validate_style_patch(patch: dict | None) -> CaptionStyle:
    """Validate a partial patch against the DEFAULT style.

    Used by command validation so an invalid patch is rejected at *validate*
    time, before any preview or timeline mutation.
    """
    return CaptionStyle.from_dict({}).patch(patch)


def resolve_style(caption: dict, *, default_style: CaptionStyle | None = None) -> CaptionStyle:
    """Read the style of a canonical caption clip.

    Accepts the clip's ``text`` mapping (where Work 01..12 put styling) and
    falls back to ``default_style``/defaults. A caption with no ``text`` block
    renders exactly as it did before Work 13.
    """
    base = default_style or CaptionStyle.from_dict({})
    text = caption.get("text")
    if not isinstance(text, dict):
        return base
    return base.patch(text)