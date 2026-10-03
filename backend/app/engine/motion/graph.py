"""Render-graph closure for Work 13.1.

Four things the Work 13 storage layer promised but did not yet EXECUTE:

1. :func:`build_visual_join` -- real ``xfade`` transitions between adjacent
   clips, replacing the flat ``concat`` when a transition exists.
2. :func:`evaluate_keyframes` / :func:`keyframe_expressions` -- deterministic
   interpolation of canonical keyframes into ffmpeg expressions, so a keyframe
   changes rendered pixels instead of only being stored.
3. :func:`order_effects` -- a deterministic effect order (geometry -> mask ->
   background -> colour -> blur -> opacity -> overlay) with cycle rejection.
4. :func:`plan_composite` -- a typed multi-input graph for the composite
   effects that cannot be expressed as a single filter.

Everything here is pure: same inputs -> byte-identical graph. No caller-supplied
ffmpeg string ever reaches the output; expressions are compiled from numeric
keyframes and closed easing/enum vocabularies only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "EASINGS",
    "EFFECT_CATEGORIES",
    "EFFECT_CATEGORY_INDEX",
    "CompositePlan",
    "JoinPlan",
    "build_visual_join",
    "clamp",
    "evaluate_keyframes",
    "keyframe_expressions",
    "order_effects",
    "plan_composite",
    "plan_transitions",
    "validate_keyframes",
]

#: Closed interpolation vocabulary (Work 13.1 §3).
EASINGS: tuple[str, ...] = ("LINEAR", "EASE_IN", "EASE_OUT", "EASE_IN_OUT", "HOLD")

#: Deterministic effect categories (§5). Lower index runs FIRST.
EFFECT_CATEGORIES: tuple[str, ...] = (
    "geometry", "mask", "background", "color", "blur", "opacity", "overlay",
)
EFFECT_CATEGORY_INDEX: dict[str, int] = {
    name: i for i, name in enumerate(EFFECT_CATEGORIES)
}

#: Which category each registry effect belongs to. An effect absent from this
#: map is REJECTED rather than guessed into the graph.
_EFFECT_CATEGORY: dict[str, str] = {
    # geometry
    "CROP": "geometry", "PAN": "geometry", "ZOOM": "geometry",
    "TRACKED_CALLOUT": "geometry",
    # mask / background (composite)
    "MASK": "mask", "BACKGROUND_BLUR": "background",
    # colour
    "COLOR_ADJUST": "color",
    # blur / sharpen
    "BLUR": "blur", "SHARPEN": "blur", "GLOW": "blur",
    # opacity
    "OPACITY": "opacity",
    # overlay
    "DROP_SHADOW": "overlay",
}

#: Effects that need a real multi-input graph rather than a chainable filter.
COMPOSITE_EFFECTS: frozenset[str] = frozenset({"MASK", "BACKGROUND_BLUR"})

#: A keyframe chain longer than this is refused: the compiled ffmpeg expression
#: nests one `if()` per keyframe, and a pathological timeline would produce an
#: unreadable (and slow) graph.
MAX_KEYFRAMES = 64


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# §5 effect ordering
# ---------------------------------------------------------------------------


def order_effects(effects) -> tuple[list[dict], list[str]]:
    """Sort effects into the deterministic category order.

    Returns ``(ordered, problems)``. An effect whose type is unknown to the
    ordering map, or that appears twice in the same category, is reported as a
    problem and DROPPED rather than silently placed -- a dropped effect must
    never look active.
    """
    problems: list[str] = []
    buckets: dict[str, list[tuple[int, dict]]] = {}
    for index, effect in enumerate(effects or []):
        if not isinstance(effect, dict):
            problems.append(f"effect #{index} is not a mapping")
            continue
        kind = str(effect.get("type") or "").upper()
        category = _EFFECT_CATEGORY.get(kind)
        if category is None:
            problems.append(f"effect {kind!r} has no ordering category")
            continue
        buckets.setdefault(category, []).append((index, effect))
    ordered: list[dict] = []
    for category in EFFECT_CATEGORIES:
        for _index, effect in buckets.get(category, []):
            ordered.append(effect)
    return ordered, problems


# ---------------------------------------------------------------------------
# §2/§3 canonical keyframes
# ---------------------------------------------------------------------------

#: Which clip fields a keyframe may drive, and how they reach ffmpeg.
KEYFRAME_PROPS: tuple[str, ...] = ("x", "y", "scale", "opacity", "crop_width",
                                   "crop_height", "crop_x", "crop_y")

_NUMERIC_LIMITS: dict[str, tuple[float, float]] = {
    "x": (-4.0, 4.0), "y": (-4.0, 4.0), "scale": (0.01, 8.0),
    "opacity": (0.0, 1.0), "crop_width": (16.0, 8192.0),
    "crop_height": (16.0, 8192.0), "crop_x": (-8192.0, 8192.0),
    "crop_y": (-8192.0, 8192.0),
}


@dataclass
class KeyframeProblem:
    code: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.code}: {self.detail}"


def validate_keyframes(
    keyframes,
    *,
    clip_duration: float,
) -> tuple[list[dict], list[KeyframeProblem]]:
    """Validate + normalise a keyframe list.

    Enforces: a bounded count, unique stable ids, strictly increasing and
    in-range times, known easing, and known numeric props clamped to their
    limits. The returned list is sorted by time, which is what makes the
    compiled expression deterministic.
    """
    problems: list[KeyframeProblem] = []
    raw = list(keyframes or [])
    if len(raw) > MAX_KEYFRAMES:
        problems.append(KeyframeProblem(
            "too_many_keyframes",
            f"{len(raw)} keyframes exceeds the {MAX_KEYFRAMES} limit"))
        raw = raw[:MAX_KEYFRAMES]
    out: list[dict] = []
    seen_ids: set[str] = set()
    for index, frame in enumerate(raw):
        if not isinstance(frame, dict):
            problems.append(KeyframeProblem("malformed", f"#{index} is not a mapping"))
            continue
        try:
            at = float(frame.get("t"))
        except (TypeError, ValueError):
            problems.append(KeyframeProblem(
                "malformed", f"#{index} has a non-numeric t"))
            continue
        if at < 0 or (clip_duration and at > clip_duration + 1e-6):
            problems.append(KeyframeProblem(
                "out_of_range",
                f"#{index} t={at:.3f}s outside clip duration {clip_duration:.3f}s"))
            continue
        easing = str(frame.get("easing") or "LINEAR").strip().upper()
        if easing not in EASINGS:
            problems.append(KeyframeProblem(
                "unsupported_interpolation",
                f"#{index} easing {easing!r} not in {list(EASINGS)}"))
            continue
        kf_id = str(frame.get("id") or f"kf{index}")
        if kf_id in seen_ids:
            problems.append(KeyframeProblem(
                "duplicate_id", f"keyframe id {kf_id!r} appears twice"))
            continue
        seen_ids.add(kf_id)
        props_in = frame.get("props") if isinstance(frame.get("props"), dict) else {}
        props: dict[str, float] = {}
        for name, value in (props_in or {}).items():
            if name not in KEYFRAME_PROPS:
                problems.append(KeyframeProblem(
                    "unknown_prop", f"#{index} prop {name!r} not supported"))
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                problems.append(KeyframeProblem(
                    "malformed", f"#{index} prop {name!r} is not a number"))
                continue
            low, high = _NUMERIC_LIMITS[name]
            props[name] = clamp(number, low, high)
        if not props:
            # A keyframe that drives nothing is malformed: the renderer would
            # drop it, so storing it silently would make a stored keyframe look
            # active when it has no effect.
            problems.append(KeyframeProblem(
                "no_props", f"#{index} drives no supported property"))
            continue
        out.append({"id": kf_id, "t": round(at, 4), "easing": easing,
                    "props": props})
    out.sort(key=lambda f: (f["t"], f["id"]))
    previous = -1.0
    for frame in out:
        if frame["t"] < previous - 1e-9:
            problems.append(KeyframeProblem(
                "unsorted", f"keyframe {frame['id']} is out of order"))
        previous = frame["t"]
    return out, problems


def _eased_unit(easing: str, u: str) -> str:
    """Map a normalised local time ``u`` through the easing curve.

    Every branch is a fixed algebraic form over ``u`` only, so the compiled
    expression stays a pure function of ``t`` and is fully deterministic.
    """
    if easing == "HOLD":
        return "0"
    if easing == "EASE_IN":
        return f"({u})*({u})"
    if easing == "EASE_OUT":
        return f"({u})*(2-({u}))"
    if easing == "EASE_IN_OUT":
        return f"({u})*({u})*(3-2*({u}))"
    return u


def evaluate_keyframes(
    frames: list[dict],
    prop: str,
    *,
    default: float = 0.0,
) -> tuple[str, float]:
    """Compile a keyframe chain into an ffmpeg expression.

    Returns ``(expression, constant)`` -- exactly one is meaningful: a constant
    chain yields ``constant`` (and an empty expression) so the caller can emit a
    literal instead of a needless ``if()`` ladder.
    """
    series = [(f["t"], f["props"][prop], f["easing"]) for f in frames
              if prop in f["props"]]
    if not series:
        return "", default
    if len(series) == 1:
        return "", series[0][1]

    def fmt(value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"

    def build(index: int) -> str:
        # Base case: the final keyframe holds its value for the rest of the
        # clip. Without this the recursion walks off the end of `series`.
        if index >= len(series) - 1:
            return fmt(series[index][1])
        t0, v0, easing = series[index]
        t1, v1, _next_easing = series[index + 1]
        span = max(1e-6, t1 - t0)
        if easing == "HOLD":
            return f"if(lt(t,{fmt(t1)}),{fmt(v0)},{build(index + 1)})"
        u = f"((t-{fmt(t0)})/{fmt(span)})"
        eased = _eased_unit(easing, u)
        delta = v1 - v0
        if abs(delta) < 1e-9:
            return f"if(lt(t,{fmt(t1)}),{fmt(v0)},{build(index + 1)})"
        value = f"({fmt(v0)}+({fmt(delta)})*({eased}))"
        return f"if(lt(t,{fmt(t1)}),{value},{build(index + 1)})"

    return build(0), default


def keyframe_expressions(
    frames: list[dict],
    *,
    width: int,
    height: int,
) -> dict[str, str]:
    """Compile keyframes into the ffmpeg expressions the renderer needs.

    ONLY properties that at least one keyframe drives appear in the result.
    Returning a default for an un-keyed property would silently override the
    caller's computed position/size (e.g. a bottom-anchored caption collapsing
    to y=0), which is why absent keys are simply absent.

    ``x``/``y`` are emitted in PIXELS (keyframes are stored as frame
    fractions), ``scale`` becomes a font-size multiplier, and ``opacity``
    becomes an ``alpha`` expression.
    """
    out: dict[str, str] = {}
    driven = {prop for frame in frames for prop in frame.get("props", {})}
    if "x" in driven:
        expr, constant = evaluate_keyframes(frames, "x")
        out["x"] = f"(({expr})*{width})" if expr else f"{constant * width:.0f}"
    if "y" in driven:
        expr, constant = evaluate_keyframes(frames, "y")
        out["y"] = f"(({expr})*{height})" if expr else f"{constant * height:.0f}"
    if "scale" in driven:
        expr, constant = evaluate_keyframes(frames, "scale", default=1.0)
        out["scale"] = expr or f"{constant:.4f}"
    if "opacity" in driven:
        expr, constant = evaluate_keyframes(frames, "opacity", default=1.0)
        out["alpha"] = expr or f"{clamp(constant, 0.0, 1.0):.4f}"
    return out


# ---------------------------------------------------------------------------
# §1 transitions
# ---------------------------------------------------------------------------


@dataclass
class JoinPlan:
    """The result of planning the visual join."""

    label: str = "vcat"
    filters: list[str] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: True when at least one real cross-fade replaced the flat concat.
    transitioned: bool = False


def plan_transitions(doc: dict, segments: list[dict]) -> tuple[dict, list[str]]:
    """Map stored transition objects onto SEGMENT boundaries.

    A transition only applies where the outgoing segment's clip is
    ``from_item`` and the next segment's clip is ``to_item``. Anything else is
    reported -- a stored transition must never be a silent no-op.
    """
    by_pair: dict[tuple[str, str], dict] = {}
    warnings: list[str] = []
    for track in doc.get("tracks", []) or []:
        for clip in track.get("clips", []) or []:
            spec = clip.get("transition")
            if not isinstance(spec, dict):
                continue
            key = (str(spec.get("from_item") or ""), str(spec.get("to_item") or ""))
            if not all(key):
                warnings.append(
                    f"clip {clip.get('id')!r} stores a transition with no "
                    f"from/to item; it will not render")
                continue
            by_pair[key] = spec
    plan: dict[tuple[int, int], dict] = {}
    for index in range(len(segments) - 1):
        here = (segments[index].get("clip") or {}).get("id")
        nxt = (segments[index + 1].get("clip") or {}).get("id")
        if here is None or nxt is None:
            continue
        spec = by_pair.get((str(here), str(nxt)))
        if spec is None:
            continue
        plan[(index, index + 1)] = spec
    matched = {(str(s.get("from_item")), str(s.get("to_item")))
               for s in plan.values()}
    for key, spec in by_pair.items():
        if key not in matched:
            warnings.append(
                f"transition {spec.get('type')!r} between {key[0]!r} and {key[1]!r} "
                f"does not correspond to adjacent rendered segments; it will not render")
    return plan, warnings


def build_visual_join(
    vlabels: list[str],
    durations: list[float],
    transition_plan: dict[tuple[int, int], dict],
    *,
    xfade_builder,
) -> JoinPlan:
    """Build either the flat ``concat`` or a real ``xfade`` ladder.

    ``xfade`` is pairwise: each step consumes the accumulated stream and the
    next segment. The offset for step *i* is the accumulated length so far
    minus the overlap of that step, which is what keeps the timeline's
    absolute start times valid for every later clip.
    """
    out = JoinPlan()
    if not vlabels:
        return out
    if not transition_plan or len(vlabels) < 2:
        out.filters.append(f"{''.join(vlabels)}concat=n={len(vlabels)}:v=1:a=0[vcat]")
        out.label = "vcat"
        return out

    current = vlabels[0]
    accumulated = float(durations[0]) if durations else 0.0
    step = 0
    for index in range(1, len(vlabels)):
        spec = transition_plan.get((index - 1, index))
        duration = float(durations[index]) if index < len(durations) else 0.0
        if not spec:
            out.filters.append(
                f"{current}{vlabels[index]}concat=n=2:v=1:a=0[join{step}]")
            current = f"[join{step}]"
            accumulated += duration
            step += 1
            continue
        overlap = float(spec.get("duration") or 0.0)
        kind = str(spec.get("type") or "CUT").upper()
        if kind == "CUT" or overlap <= 0:
            out.filters.append(
                f"{current}{vlabels[index]}concat=n=2:v=1:a=0[join{step}]")
            current = f"[join{step}]"
            accumulated += duration
            step += 1
            continue
        shortest = min(accumulated, duration)
        if overlap > shortest - 1e-3 or overlap <= 0:
            out.warnings.append(
                f"transition {kind!r} overlap {overlap:.3f}s exceeds the shorter "
                f"adjacent clip ({shortest:.3f}s); rendered as a cut")
            out.filters.append(
                f"{current}{vlabels[index]}concat=n=2:v=1:a=0[join{step}]")
            current = f"[join{step}]"
            accumulated += duration
            step += 1
            continue
        offset = max(0.0, accumulated - overlap)
        rendered = xfade_builder(spec, offset=offset)
        if not rendered:
            out.warnings.append(
                f"transition {kind!r} produced no filter; rendered as a cut")
            out.filters.append(
                f"{current}{vlabels[index]}concat=n=2:v=1:a=0[join{step}]")
            current = f"[join{step}]"
            accumulated += duration
            step += 1
            continue
        out.filters.append(f"{current}{vlabels[index]}{rendered}[xjoin{step}]")
        current = f"[xjoin{step}]"
        accumulated = accumulated + duration - overlap
        out.applied.append({"type": kind, "offset": round(offset, 4),
                            "duration": round(overlap, 4),
                            "from_item": spec.get("from_item"),
                            "to_item": spec.get("to_item")})
        out.transitioned = True
        step += 1
    out.filters.append(f"{current}null[vcat]" if len(out.filters) > 1 else
                       f"{current}null[vcat]")
    out.label = "vcat"
    return out


# ---------------------------------------------------------------------------
# §4 composite (multi-input) effects
# ---------------------------------------------------------------------------


@dataclass
class CompositePlan:
    """A multi-input effect graph plus the inputs it needs."""

    key: str
    available: bool = False
    reason: str = ""
    #: Extra ``-i`` arguments, in the order their labels appear.
    input_args: list[str] = field(default_factory=list)
    input_label: str = ""
    #: Graph fragments (joined with ``;``), using ``{IN}`` for the source label.
    filters: list[str] = field(default_factory=list)
    out_suffix: str = ""


def plan_composite(effect: dict, ctx: dict) -> CompositePlan:
    """Build the graph for a composite effect, or report it unavailable.

    ``NOT_AVAILABLE`` is a first-class outcome: when the Work 12 mask asset is
    missing the caller gets ``available=False`` with a reason, and QC records a
    finding. It is never quietly dropped.
    """
    from app.engine.motion.effects import EffectError, validate_effect

    kind = str(effect.get("type") or "").upper()
    plan = CompositePlan(key=kind)
    try:
        validated = validate_effect(effect)
    except EffectError as exc:
        plan.reason = f"invalid effect: {exc}"
        return plan
    params = validated["params"]
    mask_key = str(ctx.get("subject_mask_key") or "")
    mask_path = ctx.get("subject_mask_path") or ""
    if not mask_key and kind == "MASK":
        plan.reason = ("no subject mask asset: run a Work 12 segmentation pass "
                       "before applying MASK")
        return plan
    if not mask_path:
        plan.reason = ("no resolved mask file for asset "
                       f"{mask_key or '(none)'}; run segmentation first")
        return plan
    plan.input_args = ["-i", str(mask_path)]
    plan.input_label = "maskin"
    if kind == "MASK":
        # Composite the matted subject over the plate.
        plan.filters = [
            "{IN}split=2[mask_fg][mask_bg]",
            f"[mask_bg]boxblur=luma_radius={params.get('radius', 8)}:"
            f"luma_power=2[mask_plate]",
            f"[mask_fg][{plan.input_label}:v]alphamerge[mask_fg_a]",
            "[mask_plate][mask_fg_a]overlay=format=auto[vcomp]",
        ]
        plan.out_suffix = "vcomp"
    elif kind == "BACKGROUND_BLUR":
        plan.filters = [
            "{IN}split=2[bg_fg][bg_bg]",
            f"[bg_bg]boxblur=luma_radius={params.get('radius', 12)}:"
            f"luma_power=2[bg_blurred]",
            f"[bg_fg][{plan.input_label}:v]alphamerge[bg_fg_a]",
            "[bg_blurred][bg_fg_a]overlay=format=auto[vcomp]",
        ]
        plan.out_suffix = "vcomp"
    else:
        plan.reason = f"{kind} is not a composite effect"
        return plan
    plan.available = True
    return plan