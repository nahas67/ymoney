"""Registry-backed transitions (Work 13 §9).

A transition is a stored object -- ``from_item``, ``to_item``, ``type``,
``duration``, ``parameters`` -- validated against the ACTUAL adjacent clips
before it is accepted. Nothing executes arbitrary code or an arbitrary filter
string: :data:`TRANSITIONS` maps a name to a fixed template.

Failure is safe by construction. :func:`validate_transition` raises
:class:`TransitionError` for an unknown type, a negative duration, a duration
longer than either adjacent clip, or a pair that is not actually adjacent in
time. A caller that ignores the error still renders correctly, because the
renderer only applies a transition whose object validated.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "TRANSITION_NAMES",
    "TransitionSpec",
    "TRANSITIONS",
    "TransitionError",
    "build_transition_filter",
    "find_adjacent_pair",
    "transition_dict",
    "validate_transition",
]


class TransitionError(ValueError):
    """A transition object was invalid against the timeline it claims."""


@dataclass(frozen=True)
class TransitionSpec:
    key: str
    label: str
    #: The ffmpeg ``xfade`` transition name (or "" for a plain cut).
    xfade: str
    #: ``None`` means "no cross-dissolve; this is an instant cut".
    cross: bool = True
    description: str = ""
    #: Extra typed params beyond ``duration``.
    params: tuple[tuple[str, str, object], ...] = ()

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "xfade": self.xfade,
            "cross": self.cross, "description": self.description,
            "params": [{"name": n, "kind": k, "default": d}
                       for n, k, d in self.params],
        }


TRANSITIONS: dict[str, TransitionSpec] = {
    spec.key: spec for spec in (
        TransitionSpec("CUT", "Cut", xfade="", cross=False,
                       description="Hard cut; no overlap."),
        TransitionSpec("FADE", "Fade to black", xfade="fade", cross=True,
                       description="Fade through black."),
        TransitionSpec("DISSOLVE", "Dissolve", xfade="dissolve", cross=True,
                       description="Cross-dissolve between clips."),
        TransitionSpec("SLIDE", "Slide", xfade="slideleft", cross=True,
                       description="Slide the outgoing clip away."),
        TransitionSpec("WIPE", "Wipe", xfade="wipeleft", cross=True,
                       description="Wipe across the frame."),
        TransitionSpec("ZOOM", "Zoom", xfade="zoomin", cross=True,
                       description="Zoom through the cut."),
    )
}

TRANSITION_NAMES: tuple[str, ...] = tuple(TRANSITIONS)

#: Longest transition we will accept, in seconds. A transition longer than the
#: clip it joins would silently eat content.
MAX_TRANSITION_SECONDS = 5.0


def transition_dict() -> list[dict]:
    return [spec.to_dict() for spec in TRANSITIONS.values()]


def find_adjacent_pair(doc: dict, from_id: str, to_id: str) -> tuple[dict, dict] | None:
    """Locate two clips on the SAME track that are adjacent in time.

    "Adjacent" means one ends where (or within a small epsilon before) the
    other starts. Anything else is refused, because a transition between
    non-adjacent clips would visually reorder the timeline. Either order is
    accepted and normalized to (earlier, later).
    """
    epsilon = 0.05
    for track in doc.get("tracks", []) or []:
        clips = list(track.get("clips", []) or [])
        ordered = sorted(clips, key=lambda c: float(c.get("start", 0.0)))
        for a, b in zip(ordered, ordered[1:]):
            a_end = float(a.get("start", 0.0)) + float(a.get("duration", 0.0))
            if float(b.get("start", 0.0)) - a_end > epsilon:
                continue  # a gap (or an overlap): not adjacent
            pair = {str(a.get("id")), str(b.get("id"))}
            if pair != {str(from_id), str(to_id)}:
                continue
            return (a, b) if str(a.get("id")) == str(from_id) else (b, a)
    return None


def validate_transition(transition: dict, doc: dict | None = None) -> dict:
    """Validate a transition object, optionally against a timeline document.

    With ``doc`` supplied the pair must exist and be adjacent; without it only
    the intrinsic shape is checked (used by command validation before a
    timeline has been loaded).
    """
    if not isinstance(transition, dict):
        raise TransitionError("transition must be a mapping")
    key = str(transition.get("type") or "").strip().upper().replace("-", "_")
    spec = TRANSITIONS.get(key)
    if spec is None:
        raise TransitionError(
            f"unknown transition {transition.get('type')!r}; "
            f"allowed {list(TRANSITION_NAMES)}"
        )
    try:
        duration = float(transition.get("duration", 0.0))
    except (TypeError, ValueError):
        raise TransitionError(
            f"{key}: duration must be a number, got {transition.get('duration')!r}"
        ) from None
    if duration < 0:
        raise TransitionError(f"{key}: duration must be >= 0, got {duration}")
    if duration > MAX_TRANSITION_SECONDS:
        raise TransitionError(
            f"{key}: duration {duration}s exceeds the {MAX_TRANSITION_SECONDS}s maximum"
        )

    params = transition.get("parameters") or transition.get("params") or {}
    if not isinstance(params, dict):
        raise TransitionError(f"{key}: parameters must be a mapping")
    known = {n for n, _k, _d in spec.params}
    unknown = set(params) - known
    if unknown:
        raise TransitionError(
            f"{key}: unknown parameter(s) {sorted(unknown)}; allowed {sorted(known)}"
        )

    from_id = str(transition.get("from_item") or transition.get("from") or "")
    to_id = str(transition.get("to_item") or transition.get("to") or "")
    if not from_id or not to_id:
        raise TransitionError(f"{key}: from_item and to_item are both required")
    if from_id == to_id:
        raise TransitionError(f"{key}: a transition needs two DIFFERENT items")

    out = {
        "from_item": from_id,
        "to_item": to_id,
        "type": spec.key,
        "duration": duration,
        "parameters": dict(params),
    }

    if doc is not None:
        if duration > 0 and not spec.cross:
            raise TransitionError(
                f"{spec.key} has no overlap; duration must be 0"
            )
        pair = find_adjacent_pair(doc, from_id, to_id)
        if pair is None:
            raise TransitionError(
                f"{spec.key}: {from_id!r} and {to_id!r} are not adjacent clips "
                f"on the same track"
            )
        a, b = pair
        shortest = min(float(a.get("duration", 0.0)), float(b.get("duration", 0.0)))
        if duration > shortest:
            raise TransitionError(
                f"{spec.key}: duration {duration}s exceeds the shorter adjacent "
                f"clip ({shortest:.3f}s)"
            )
    elif duration > 0 and not spec.cross:
        raise TransitionError(f"{spec.key} has no overlap; duration must be 0")
    return out


def build_transition_filter(transition: dict, *, offset_s: float) -> str | None:
    """The ``xfade`` filter for a validated transition.

    ``offset_s`` is the time in the *concatenated* stream at which the
    outgoing clip ends and the cross begins. Returns ``None`` for a CUT (and
    for any zero-duration transition), which is the correct behaviour: a cut
    needs no filter at all.

    Scope note: this is a VIDEO cross-dissolve only. It never concatenates
    audio, so the cumulative-MP3-encoder-delay drift that affects
    ``ffmpeg -c:a copy`` joins does not arise here. Audio in the timeline
    renderer is decoded, mixed with ``amix`` and encoded once
    (``timeline_render.py``); there is no per-part stream copy to splice.
    """
    validated = validate_transition(transition)
    spec = TRANSITIONS[validated["type"]]
    if not spec.cross or validated["duration"] <= 0:
        return None
    return (f"xfade=transition={spec.xfade}"
            f":duration={validated['duration']:.3f}"
            f":offset={max(0.0, float(offset_s)):.3f}")