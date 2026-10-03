"""The single caption + motion -> ffmpeg filter-graph builder (Work 13 §13).

One rendering path. There is no second renderer and no hard-coded ffmpeg
string anywhere else: every caption style, animation, effect and transition
is expressed as a *typed* parameter that this module turns into a filter.

Determinism: the output depends only on (style, timing, geometry). Two runs
with the same inputs emit byte-identical filter graphs, which is what makes
chunk caches and manifest hashes meaningful.

Safety: nothing caller-supplied becomes an ffmpeg expression. Text is escaped
via :mod:`app.engine.captions.ffmpeg_escape`, colours are allowlisted,
positions are computed, and animations are resolved through closed maps in
:data:`ENTRANCE_EXPR` / :data:`EXIT_EXPR`.

Word timing: a word-level caption emits one ``drawtext`` per word gated by
``enable='between(t,start,end)'`` using STORED timings. If the caption is not
word-level, exactly one filter is emitted and no per-word animation is
attempted -- see ``app.engine.captions.words``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.captions.ffmpeg_escape import (
    escape_drawtext,
    escape_filter_value,
    escape_filterfile,
    resolve_font,
    safe_fontcolor,
    wrap_text,
)
from app.engine.captions.style import CaptionStyle, apply_case
from app.engine.motion.graph import keyframe_expressions

__all__ = [
    "ENTRANCE_EXPR",
    "EXIT_EXPR",
    "CaptionFilterPlan",
    "build_caption_filters",
    "build_text_filters",
    "caption_y_expression",
    "effect_registry_version",
]

#: Closed map: entrance name -> x/y expression fragment. A caller cannot add
#: an entry at runtime through any API path.
ENTRANCE_EXPR: dict[str, str] = {
    "none": "",
    "fade": "alpha='min(1,max(0,(t-{t0})/{dur}))'",
    "pop": "alpha='if(lt(t,{t_end}-{dur}),0,min(1,max(0,(t-{t0})/{dur})))'",
    "slide_up": "y='{y}-(1-min(1,max(0,(t-{t0})/{dur})))*{rise}'",
    "slide_left": "x='{x}-(1-min(1,max(0,(t-{t0})/{dur})))*{run}'",
    "wipe": "alpha='if(lt((t-{t0})/{dur},1),0,1)'",
    "reveal": "alpha='min(1,max(0,(t-{t0})/{dur}))'",
}

#: Closed map: exit name -> x/y/alpha expression fragment.
EXIT_EXPR: dict[str, str] = {
    "none": "",
    "fade": "alpha='if(gt(t,{t_fade}),max(0,({t_end}-t)/{dur}),1)'",
    "pop": "alpha='if(gt(t,{t_end}-{dur}),max(0,({t_end}-t)/{dur}),1)'",
    "slide_down": "y='{y}+min(1,max(0,(t-{t_fade})/{dur}))*{rise}'",
}

#: Bumped whenever the emitted filter graph changes, so an existing cache
#: entry from an older builder can never be mistaken for a current one.
RENDERER_FILTER_VERSION = "w13-caption-filters-1"


def effect_registry_version() -> str:
    """The renderer-version token used in cache keys (§15)."""
    return RENDERER_FILTER_VERSION


@dataclass
class CaptionFilterPlan:
    """The filter graph produced for one caption clip."""

    filters: list[str] = field(default_factory=list)
    #: ``[in][out]`` label pairs to splice after the source label.
    chains: list[tuple[str, str]] = field(default_factory=list)
    word_level: bool = False
    font_used: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def filter_count(self) -> int:
        return len(self.filters)


def caption_y_expression(
    position: str,
    *,
    width: int,
    height: int,
    safe_box: dict | None,
    text_height: int,
) -> str:
    """Compute the caption baseline ``y`` for a position + safe zone.

    Arithmetic only. A brand/platform safe box (fractions of the frame) is
    honoured, so captions cannot sit under platform chrome or a logo.
    """
    top = float((safe_box or {}).get("top", 0.0))
    bottom = float((safe_box or {}).get("bottom", 0.0))
    safe_top = int(height * top)
    safe_bottom = int(height * (1.0 - bottom))
    if position == "top":
        return f"{safe_top}"
    if position == "middle":
        return "(h-text_h)/2"
    # bottom: sit just above the safe bottom edge
    return f"{max(0, safe_bottom - text_height - int(height * 0.02))}"


def _x_expression(align: str, *, width: int, safe_box: dict | None) -> str:
    left = float((safe_box or {}).get("left", 0.0))
    right = float((safe_box or {}).get("right", 0.0))
    safe_left = int(width * left)
    safe_right = int(width * (1.0 - right))
    if align == "left":
        return f"{safe_left}"
    if align == "right":
        return f"{safe_right}-text_w"
    return "(w-text_w)/2"


def _enable_window(t0: float, t1: float) -> str:
    return f"between(t\\,{t0:.3f}\\,{t1:.3f})"


def _emit(
    *,
    label_in: str,
    label_out: str,
    text: str,
    fontfile: str,
    style: CaptionStyle,
    width: int,
    height: int,
    safe_box: dict | None,
    t0: float,
    t1: float,
    entrance: str,
    exit_name: str,
    anim_dur: float,
    enable: str | None = None,
    emphasis_color: str = "",
    keyframes: list | None = None,
) -> str:
    """Assemble ONE drawtext filter. Returns the filter string.

    ``keyframes`` (Work 13.1 §3) compile into time-varying x / y / font-size /
    alpha expressions, which is what makes a keyframe change rendered pixels
    instead of only being stored.
    """
    size = int(style.size)
    y = caption_y_expression(style.position, width=width, height=height,
                             safe_box=safe_box, text_height=size)
    x = _x_expression(style.align, width=width, safe_box=safe_box)
    color = safe_fontcolor(emphasis_color or style.primary_color)

    kf = keyframe_expressions(keyframes or [], width=width, height=height) \
        if keyframes else {}

    font_size_expr = f"fontsize={size}"
    # A constant scale compiles to a bare number, so only substitute when the
    # chain is actually time-varying.
    if kf.get("scale") and any(op in kf["scale"] for op in ("if(", "t")):
        font_size_expr = f"fontsize='max(1,{kf['scale']}*{size})'"
    if kf.get("x"):
        x = kf["x"]
    if kf.get("y"):
        y = kf["y"]

    parts = [
        f"fontfile='{escape_filterfile(fontfile)}'",
        f"text='{escape_drawtext(text)}'",
        font_size_expr,
        f"fontcolor={color}",
    ]
    if style.weight >= 700:
        # drawtext has no weight knob; emulate emphasis via a heavier stroke.
        parts.append(f"borderw={max(1, style.stroke_width + 1)}")
    else:
        parts.append(f"borderw={style.stroke_width}")
    parts.append(f"bordercolor={safe_fontcolor(style.stroke_color, fallback='black')}")
    if style.shadow:
        parts.append("shadowx=2:shadowy=2:shadowcolor=black@0.6")
    parts.append(f"x={escape_filter_value(x)}")
    parts.append(f"y={escape_filter_value(y)}")
    parts.append(f"line_spacing={int(style.line_spacing * 10)}")

    # animation fragments, resolved ONLY through the closed maps. When the
    # keyframe chain drives alpha it is the authority (a filter may carry only
    # one `alpha` option), so the entrance/exit alpha is suppressed rather than
    # emitted twice and silently ignored by ffmpeg.
    t_end = max(t0 + 0.001, t1)
    t_fade = max(t0, t_end - anim_dur)
    subs = {
        "t0": f"{t0:.3f}", "t_end": f"{t_end:.3f}", "t_fade": f"{t_fade:.3f}",
        "dur": f"{max(0.04, anim_dur):.3f}",
        "x": x, "y": y,
        "rise": str(int(height * 0.04)), "run": str(int(width * 0.06)),
    }
    keyframe_alpha = bool(kf.get("alpha"))
    for name in (entrance, exit_name):
        frag = (ENTRANCE_EXPR if name in ENTRANCE_EXPR else EXIT_EXPR).get(name, "")
        if not frag:
            continue
        rendered = frag.format(**subs)
        # split the rendered `key='expr'` into two filter options
        key, _, expr = rendered.partition("=")
        if key == "alpha" and keyframe_alpha:
            continue
        parts.append(f"{key}={escape_filter_value(expr)}")

    if kf.get("alpha"):
        # Unquoted option value -> the same escaping as x/y. Unescaped commas
        # here split the filter into bogus extra options and ffmpeg rejects the
        # whole graph.
        parts.append(f"alpha={escape_filter_value(kf['alpha'])}")

    window = _enable_window(t0, t1)
    parts.append(f"enable='{window}'" if enable is None else f"enable='{enable}'")
    return f"[{label_in}]drawtext={':'.join(parts)}[{label_out}]"


def build_caption_filters(
    *,
    label_in: str,
    caption: dict,
    style: CaptionStyle,
    width: int,
    height: int,
    safe_box: dict | None = None,
    max_chars_per_line: int = 32,
    max_lines: int = 2,
    words: list | None = None,
    word_level: bool = False,
    emphasis: list | None = None,
    keyframes: list | None = None,
    font_family: str = "",
    label_prefix: str = "cap",
) -> CaptionFilterPlan:
    """Build the filter chain for ONE canonical caption clip.

    ``words`` are :class:`~app.engine.captions.words.WordTiming` objects. When
    ``word_level`` is False they are ignored for timing purposes and a single
    static filter is emitted -- the honest degradation path.
    """
    plan = CaptionFilterPlan()
    text = str(caption.get("name") or caption.get("text", {}).get("content") or "").strip()
    if not text:
        return plan
    try:
        t0 = float(caption.get("start", 0.0))
        t1 = t0 + float(caption.get("duration", 0.0))
    except (TypeError, ValueError):
        plan.warnings.append("caption has non-numeric timing; skipped")
        return plan
    if t1 <= t0:
        plan.warnings.append("caption has non-positive duration; skipped")
        return plan

    font = resolve_font(font_family or style.font)
    if not font:
        plan.warnings.append("no render font found - caption skipped")
        return plan
    plan.font_used = font

    body = apply_case(text, style.case)
    wrapped = wrap_text(body, max_chars_per_line)
    anim = style.animation
    anim_dur = float(anim.duration or 0.25)

    if word_level and words:
        plan.word_level = True
        emphasis_by_word: dict[str, str] = {}
        for item in (emphasis or []):
            w = str(getattr(item, "word", "") or "")
            if w and w not in emphasis_by_word:
                emphasis_by_word[w] = str(getattr(item, "kind", "") or "")
        usable = [w for w in words if str(getattr(w, "word", "")).strip()]
        cursor = label_in
        for i, word in enumerate(usable):
            is_last = i == len(usable) - 1
            out = f"{label_in}_capdone" if is_last else f"{label_prefix}w{i}"
            wtext = apply_case(str(word.word), style.case)
            kind = emphasis_by_word.get(str(word.word), "")
            hl_color = ""
            if kind in ("KEYWORD", "NUMBER", "CTA"):
                # The highlight colour comes from the preset's own stroke so
                # brand styling stays authoritative.
                hl_color = safe_fontcolor(style.stroke_color, fallback="white")
            plan.filters.append(_emit(
                label_in=cursor, label_out=out, text=wtext, fontfile=font,
                style=style, width=width, height=height, safe_box=safe_box,
                t0=max(t0, float(word.start_s)), t1=min(t1, float(word.end_s)),
                entrance=anim.entrance if i == 0 else "none",
                exit_name=anim.exit if is_last else "none",
                anim_dur=anim_dur, emphasis_color=hl_color,
                keyframes=keyframes,
            ))
            cursor = out
        plan.chains.append((label_in, cursor))
        return plan

    # segment/static path
    out = f"{label_in}_capdone"
    plan.filters.append(_emit(
        label_in=label_in, label_out=out, text=wrapped, fontfile=font,
        style=style, width=width, height=height, safe_box=safe_box,
        t0=t0, t1=t1, entrance=anim.entrance, exit_name=anim.exit,
        anim_dur=anim_dur, keyframes=keyframes,
    ))
    plan.chains.append((label_in, out))
    return plan


def build_text_filters(
    *,
    label_in: str,
    clip: dict,
    style: CaptionStyle,
    width: int,
    height: int,
    safe_box: dict | None = None,
    label_prefix: str = "txt",
) -> CaptionFilterPlan:
    """Build filters for a ``text`` track clip (titles, callouts, stats).

    Shares the caption path so a title and a caption cannot diverge in how
    they escape or position themselves.
    """
    plan = CaptionFilterPlan()
    text_obj = clip.get("text") or {}
    content = str(text_obj.get("content") or clip.get("name") or "").strip()
    if not content:
        return plan
    try:
        t0 = float(clip.get("start", 0.0))
        t1 = t0 + float(clip.get("duration", 0.0))
    except (TypeError, ValueError):
        return plan
    if t1 <= t0:
        return plan
    font = resolve_font(style.font)
    if not font:
        plan.warnings.append("no render font found - text overlay skipped")
        return plan
    plan.font_used = font

    # an explicit transform overrides the computed position (legacy behavior)
    transform = clip.get("transform") or {}
    x_expr = _x_expression(style.align, width=width, safe_box=safe_box)
    y_expr = caption_y_expression(style.position, width=width, height=height,
                                  safe_box=safe_box, text_height=style.size)
    if transform.get("x") is not None:
        x_expr = str(int(float(transform["x"]) * width))
    if transform.get("y") is not None:
        y_expr = str(int(float(transform["y"]) * height))

    out = f"{label_prefix}0"
    parts = [
        f"fontfile='{escape_filterfile(font)}'",
        f"text='{escape_drawtext(apply_case(content, style.case))}'",
        f"fontsize={int(style.size)}",
        f"fontcolor={safe_fontcolor(style.primary_color)}",
        f"borderw={style.stroke_width}",
        f"bordercolor={safe_fontcolor(style.stroke_color, fallback='black')}",
        f"x={escape_filter_value(x_expr)}",
        f"y={escape_filter_value(y_expr)}",
        f"enable='{_enable_window(t0, t1)}'",
    ]
    if style.opacity < 1.0:
        parts.append(f"alpha={round(float(style.opacity), 3)}")
    plan.filters.append(f"[{label_in}]drawtext={':'.join(parts)}[{out}]")
    plan.chains.append((label_in, out))
    return plan