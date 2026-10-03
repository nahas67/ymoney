"""Bounded visual-effect registry (Work 13 §8).

An effect is a NAME in a closed registry plus TYPED parameters. There is no
escape hatch that accepts an ffmpeg fragment: :func:`build_effect_filter`
resolves a name through :data:`EFFECTS` and formats only the parameters that
effect declares. A caller who wants a new effect adds an entry here, in
reviewable code -- never a string from a user or a model.

Where the parameter is numeric it is clamped; where it is an enum it is
checked; where it is a colour it goes through the shared allowlist. Anything
unrecognised is refused by :func:`build_effect_filter`, never silently ignored,
so a typo surfaces as a QC finding instead of a silently missing effect.

Work 12 segmentation/face tracking is consumed here through the
``tracked_*`` parameters, which resolve a subject's box from stored face
tracks. When confidence is insufficient the effect is NOT emitted -- see
``app.engine.motion.tracking``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.engine.captions.ffmpeg_escape import safe_fontcolor

__all__ = [
    "EFFECT_NAMES",
    "EffectSpec",
    "EFFECTS",
    "EffectError",
    "build_effect_filter",
    "effect_dict",
    "validate_effect",
]


class EffectError(ValueError):
    """An effect name or parameter set was invalid."""


@dataclass(frozen=True)
class ParamSpec:
    """One typed effect parameter."""

    name: str
    kind: str  # "float" | "int" | "bool" | "enum" | "color"
    default: Any = None
    low: float = 0.0
    high: float = 1.0
    choices: tuple[str, ...] = ()
    #: When True a caller MUST supply it -- there is no sensible default.
    required: bool = False

    def coerce(self, value: Any, *, effect: str) -> Any:
        if value is None:
            if self.required:
                raise EffectError(f"{effect}: parameter {self.name!r} is required")
            return self.default
        if self.kind == "bool":
            return bool(value)
        if self.kind in ("float", "int"):
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise EffectError(
                    f"{effect}.{self.name}: {value!r} is not a number"
                ) from None
            num = max(self.low, min(self.high, num))
            return int(num) if self.kind == "int" else num
        if self.kind == "enum":
            token = str(value).strip().lower()
            if token not in self.choices:
                raise EffectError(
                    f"{effect}.{self.name}: {token!r} not in {list(self.choices)}"
                )
            return token
        if self.kind == "color":
            return safe_fontcolor(str(value), fallback="white")
        raise EffectError(f"{effect}.{self.name}: unsupported kind {self.kind!r}")


@dataclass(frozen=True)
class EffectSpec:
    """A registry entry: typed params + a builder that emits a filter."""

    key: str
    label: str
    params: tuple[ParamSpec, ...] = ()
    #: ``(effect_key, params, ctx) -> filter_string | None``. ``None`` means
    #: "declined to emit" (e.g. tracking confidence too low), which QC records.
    build: Callable[[str, dict, dict], str | None] | None = None
    description: str = ""
    #: True when the effect needs Work 12 face/subject evidence.
    requires_tracking: bool = False
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "requires_tracking": self.requires_tracking,
            "params": [
                {"name": p.name, "kind": p.kind, "default": p.default,
                 "low": p.low, "high": p.high, "choices": list(p.choices),
                 "required": p.required}
                for p in self.params
            ],
        }


def _p(name, kind, default=None, low=0.0, high=1.0, choices=(), required=False):
    return ParamSpec(name=name, kind=kind, default=default, low=low, high=high,
                     choices=choices, required=required)


# -- builders ---------------------------------------------------------------
# Every builder returns a filter string built from typed params only. None of
# them interpolate a caller string into the graph unescaped.


def _b_blur(key: str, p: dict, ctx: dict) -> str:
    return f"boxblur=luma_radius={p['radius']}:luma_power={p['power']}"


def _b_background_blur(key: str, p: dict, ctx: dict) -> str | None:
    # A real background blur needs the Work 12 segmentation mask. Without a
    # mask asset we decline rather than blur the whole frame and pretend.
    if not ctx.get("background_mask_key"):
        return None
    # The composite needs the foreground alpha to carry the mask; `alphamerge`
    # is applied by the caller-supplied graph, this branch only prepares the
    # blurred plate. Strength scales the merge weight downstream.
    return (
        "split=2[bg_blur_src][bg_keep];"
        f"[bg_blur_src]boxblur=luma_radius={p['radius']}:luma_power={p['power']}[bg_blur];"
        f"[bg_keep]alphamerge[mrg_bg];"
    )


def _b_vignette(key: str, p: dict, ctx: dict) -> str:
    return (f"vignette=angle={p['angle']}:mode=forward:"
            f"eval=frame:x0='iw/2':y0='ih/2':"
            f"d0={p['d0']}:x1='iw/2':y1='ih/2':d1={p['d1']}")


def _b_zoom(key: str, p: dict, ctx: dict) -> str:
    return (f"zoompan=z='min(zoom+{p['step']},{p['max_zoom']})'"
            f":d={p['frames']}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")


def _b_pan(key: str, p: dict, ctx: dict) -> str:
    return (f"crop=iw*{p['width_fraction']}:ih*{p['height_fraction']}:"
            f"x='(iw-ow)*{p['x_fraction']}':y='(ih-oh)*{p['y_fraction']}'")


def _b_crop(key: str, p: dict, ctx: dict) -> str:
    return (f"crop={max(16, int(p['width']))}:{max(16, int(p['height']))}:"
            f"{max(0, int(p['x']))}:{max(0, int(p['y']))}")


def _b_opacity(key: str, p: dict, ctx: dict) -> str:
    return f"colorchannelmixer=aa={p['value']}"


def _b_color_adjust(key: str, p: dict, ctx: dict) -> str:
    parts = [f"brightness={p['brightness']}", f"contrast={p['contrast']}",
             f"saturation={p['saturation']}"]
    if p["gamma"]:
        parts.append(f"gamma={p['gamma']}")
    return "eq=" + ":".join(parts)


def _b_sharpen(key: str, p: dict, ctx: dict) -> str:
    amount = max(0.0, min(3.0, float(p["amount"])))
    return f"unsharp=5:5:{amount}:5:5:0"


def _b_glow(key: str, p: dict, ctx: dict) -> str:
    return f"gblur=sigma={p['sigma']}:steps=1"


def _b_drop_shadow(key: str, p: dict, ctx: dict) -> str:
    return (f"drawbox=x={p['x']}:y={p['y']}:w=iw:h=ih:"
            f"color={safe_fontcolor(p['color'], fallback='black')}"
            f"@1:t={max(1, int(p['thickness']))}")


def _b_mask(key: str, p: dict, ctx: dict) -> str | None:
    """Composite a subject mask over a prepared plate.

    The mask is a Work 12 asset key. Without one the effect is declined, so
    an operator sees "no mask" instead of an untracked overlay.
    """
    mask_key = ctx.get("subject_mask_key")
    if not mask_key:
        return None
    return f"alphamerge[mrg_{p['layer']}]"


def _b_tracked_callout(key: str, p: dict, ctx: dict) -> str | None:
    """Draw a callout box that follows a tracked subject.

    Declines unless Work 12 gave us a box with sufficient confidence. The box
    is INTERPOLATED from stored samples -- it is never extrapolated.
    """
    box = ctx.get("subject_box")
    if not box:
        return None
    if float(box.get("confidence") or 0.0) < float(ctx.get("min_confidence", 0.55)):
        return None
    x = max(0, int(float(box["x"])))
    y = max(0, int(float(box["y"])))
    w = max(8, int(float(box["w"])))
    h = max(8, int(float(box["h"])))
    color = safe_fontcolor(p["color"], fallback="yellow")
    thickness = max(1, int(p["thickness"]))
    window = ctx.get("window_s") or (0.0, 0.0)
    return (f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}@1:t={thickness}"
            f":enable='between(t\\,{float(window[0]):.3f}\\,{float(window[1]):.3f})'")


#: The closed registry. Keys are the public contract.
EFFECTS: dict[str, EffectSpec] = {
    spec.key: spec for spec in (
        EffectSpec(
            "BLUR", "Blur",
            params=(_p("radius", "int", 4, 1, 40), _p("power", "int", 2, 1, 4)),
            build=_b_blur, description="Uniform frame blur.",
        ),
        EffectSpec(
            "BACKGROUND_BLUR", "Background Blur",
            params=(_p("radius", "int", 12, 1, 60),
                    _p("strength", "float", 1.0, 0.0, 1.0)),
            build=_b_background_blur,
            requires_tracking=True,
            description="Blur behind a segmented subject (needs a Work 12 mask).",
        ),
        EffectSpec(
            "VIGNETTE", "Vignette",
            params=(_p("angle", "float", 0.0, -1.0, 1.0),
                    _p("d0", "float", 0.7, 0.0, 1.5),
                    _p("d1", "float", 1.3, 0.0, 2.0)),
            build=_b_vignette, description="Darken the frame edges.",
        ),
        EffectSpec(
            "ZOOM", "Zoom",
            params=(_p("step", "float", 0.001, 0.0, 0.05),
                    _p("max_zoom", "float", 1.12, 1.0, 3.0),
                    _p("frames", "int", 25, 1, 600)),
            build=_b_zoom, description="Deterministic Ken Burns zoom.",
        ),
        EffectSpec(
            "PAN", "Pan",
            params=(_p("width_fraction", "float", 0.9, 0.1, 1.0),
                    _p("height_fraction", "float", 0.9, 0.1, 1.0),
                    _p("x_fraction", "float", 0.5, 0.0, 1.0),
                    _p("y_fraction", "float", 0.5, 0.0, 1.0)),
            build=_b_pan, description="Crop to a sub-window of the frame.",
        ),
        EffectSpec(
            "CROP", "Crop",
            params=(_p("width", "int", 1080, 16, 8192),
                    _p("height", "int", 1920, 16, 8192),
                    _p("x", "int", 0, 0, 8192), _p("y", "int", 0, 0, 8192)),
            build=_b_crop, description="Fixed crop rectangle in pixels.",
        ),
        EffectSpec(
            "OPACITY", "Opacity",
            params=(_p("value", "float", 1.0, 0.0, 1.0),),
            build=_b_opacity, description="Uniform frame opacity.",
        ),
        EffectSpec(
            "COLOR_ADJUST", "Colour Adjust",
            params=(_p("brightness", "float", 0.0, -1.0, 1.0),
                    _p("contrast", "float", 1.0, 0.0, 4.0),
                    _p("saturation", "float", 1.0, 0.0, 4.0),
                    _p("gamma", "float", 0.0, -1.0, 1.0)),
            build=_b_color_adjust, description="Brightness / contrast / saturation.",
        ),
        EffectSpec(
            "SHARPEN", "Sharpen",
            params=(_p("amount", "float", 1.0, 0.0, 3.0),),
            build=_b_sharpen, description="Unsharp mask.",
        ),
        EffectSpec(
            "GLOW", "Glow",
            params=(_p("sigma", "float", 6.0, 0.5, 40.0),
                    _p("color", "color", "", 0.0, 0.0)),
            build=_b_glow, description="Gaussian glow.",
        ),
        EffectSpec(
            "DROP_SHADOW", "Drop Shadow",
            params=(_p("x", "int", 6, 0, 512), _p("y", "int", 6, 0, 512),
                    _p("thickness", "int", 6, 1, 64),
                    _p("color", "color", "black", 0.0, 0.0)),
            build=_b_drop_shadow, description="Offset shadow box behind the frame.",
        ),
        EffectSpec(
            "MASK", "Subject Mask",
            params=(_p("layer", "enum", "a", choices=("a", "b")),),
            build=_b_mask, requires_tracking=True,
            description="Composite a Work 12 subject mask (needs a mask asset).",
        ),
        EffectSpec(
            "TRACKED_CALLOUT", "Tracked Callout",
            params=(_p("color", "color", "yellow", 0.0, 0.0),
                    _p("thickness", "int", 3, 1, 24)),
            build=_b_tracked_callout, requires_tracking=True,
            description="Outline box that follows a tracked subject.",
        ),
    )
}

EFFECT_NAMES: tuple[str, ...] = tuple(EFFECTS)


def effect_dict() -> list[dict]:
    """Full registry description for the API/UI."""
    return [spec.to_dict() for spec in EFFECTS.values()]


def _resolve_key(name: str) -> EffectSpec:
    token = str(name or "").strip().upper().replace("-", "_").replace(" ", "_")
    spec = EFFECTS.get(token)
    if spec is None:
        for candidate in EFFECTS.values():
            if token in candidate.aliases:
                return candidate
        raise EffectError(
            f"unknown effect {name!r}; allowed {list(EFFECT_NAMES)}"
        )
    return spec


def validate_effect(effect: dict) -> dict:
    """Validate + coerce one effect dict. Raises :class:`EffectError`.

    This is the ONLY writer path for effects, so an invalid effect can never
    be persisted.
    """
    if not isinstance(effect, dict):
        raise EffectError("effect must be a mapping")
    spec = _resolve_key(str(effect.get("type") or effect.get("key") or ""))
    raw = effect.get("params") or effect.get("parameters") or {}
    if not isinstance(raw, dict):
        raise EffectError(f"{spec.key}: params must be a mapping")
    unknown = set(raw) - {p.name for p in spec.params}
    if unknown:
        raise EffectError(
            f"{spec.key}: unknown parameter(s) {sorted(unknown)}; "
            f"allowed {[p.name for p in spec.params]}"
        )
    out: dict = {}
    for param in spec.params:
        out[param.name] = param.coerce(raw.get(param.name), effect=spec.key)
    enabled = effect.get("enabled", True)
    return {
        "type": spec.key,
        "params": out,
        "enabled": bool(enabled),
        "requires_tracking": spec.requires_tracking,
    }


def build_effect_filter(
    effect: dict,
    ctx: dict | None = None,
) -> str | None:
    """Build the filter for a validated effect.

    Returns ``None`` when the effect declines to emit (missing tracking
    evidence). The caller records that as a QC finding rather than dropping
    the effect silently.
    """
    validated = validate_effect(effect)
    spec = EFFECTS[validated["type"]]
    if not validated["enabled"] or spec.build is None:
        return None
    built = spec.build(spec.key, validated["params"], ctx or {})
    if built is None:
        return None
    # Builders compose only typed, clamped parameters into fixed templates, so
    # the result needs no further escaping -- and escaping it here would
    # corrupt the legitimate `[label]` graph syntax the composite filters use.
    return built