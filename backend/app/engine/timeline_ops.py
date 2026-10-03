"""Typed timeline mutations: the ONE operation layer shared by the browser
editor now and the AI Creative Director later.

Every op is absolute (no deltas) and JSON-serializable. The route applies a
batch transactionally against a base_version; stale bases get 409.
"""

from __future__ import annotations

import copy

from app.engine.timeline import (
    TRACK_KINDS,
    TimelineValidationError,
    _track,
    validate_timeline,
)

# track families that may exchange clips (visual / audio / overlay)
TRACK_FAMILIES: dict[str, set[str]] = {
    "visual": {"video", "broll", "avatar"},
    "audio": {"voice", "music", "sfx"},
    "overlay": {"text", "caption"},
}

OP_TYPES = ("add_item", "delete_item", "move_item", "trim_item", "split_item",
            "duplicate_item", "move_to_track", "update_transform",
            "update_volume", "update_speed", "update_text", "update_caption",
            # Work 13 typed caption / motion / effect ops
            "update_caption_style", "set_caption_words", "apply_effect",
            "remove_effect", "set_transition",
            # Work 13.1 canonical keyframe operations
            "add_keyframe", "update_keyframe", "delete_keyframe",
            "move_keyframe", "set_keyframes")


class TimelineOpError(ValueError):
    pass


def find_clip(doc: dict, track: str, clip_id: str) -> dict:
    tr = _track(doc, track)
    for c in tr.get("clips", []):
        if c.get("id") == clip_id:
            return c
    raise TimelineOpError(f"clip '{clip_id}' not found on track '{track}'")


def _family(kind: str) -> str:
    for fam, kinds in TRACK_FAMILIES.items():
        if kind in kinds:
            return fam
    raise TimelineOpError(f"unknown track kind '{kind}'")


def apply_operations(doc: dict, operations: list[dict]) -> dict:
    """Apply ops in order to a copy; validate at the end. Raises TimelineOpError."""
    if not isinstance(operations, list) or not operations:
        raise TimelineOpError("operations must be a non-empty list")
    if len(operations) > 200:
        raise TimelineOpError("too many operations in one batch (max 200)")
    work = copy.deepcopy(doc)
    for i, op in enumerate(operations):
        try:
            _apply_one(work, op)
        except (TimelineOpError, TimelineValidationError, KeyError,
                TypeError, ValueError) as exc:
            raise TimelineOpError(f"op {i} ({op.get('type')}): {exc}") from exc
    try:
        validate_timeline(work)
    except TimelineValidationError as exc:
        raise TimelineOpError(f"batch result invalid: {exc}") from exc
    # duration tracks content (grow; explicit trims below shrink via recompute)
    ends = [c["start"] + c["duration"] for tr in work.get("tracks", [])
            for c in tr.get("clips", [])]
    work["duration_seconds"] = max(ends) if ends else 0.0
    return work


def _apply_one(doc: dict, op: dict) -> None:
    typ = op.get("type")
    if typ not in OP_TYPES:
        raise TimelineOpError(f"unknown op type '{typ}'")
    globals()[f"_op_{typ}"](doc, op)


def _op_add_item(doc: dict, op: dict) -> None:
    from app.engine.timeline import add_clip

    for key in ("track", "clip"):
        if key not in op:
            raise TimelineOpError(f"add_item needs '{key}'")
    clip = dict(op["clip"])
    add_clip(doc, track=op["track"], clip_id=clip.pop("id"),
             name=clip.pop("name", "clip"),
             start=float(clip.pop("start", 0.0)),
             duration=float(clip.pop("duration", 0.0)),
             source=clip.pop("source", {}), effects=clip.pop("effects", []),
             source_start=float(clip.pop("source_start", 0.0)),
             volume=float(clip.pop("volume", 1.0)),
             speed=float(clip.pop("speed", 1.0)),
             fade_in=float(clip.pop("fade_in", 0.0)),
             fade_out=float(clip.pop("fade_out", 0.0)),
             transform=clip.pop("transform", {}), text=clip.pop("text", {}),
             transition_in=clip.pop("transition_in", "cut"),
             transition_out=clip.pop("transition_out", "cut"))


def _op_delete_item(doc: dict, op: dict) -> None:
    tr = _track(doc, op["track"])
    before = len(tr.get("clips", []))
    tr["clips"] = [c for c in tr.get("clips", []) if c.get("id") != op["clip_id"]]
    if len(tr["clips"]) == before:
        raise TimelineOpError(f"clip '{op.get('clip_id')}' not found")


def _op_move_item(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    new_start = float(op["start"])
    if new_start < 0:
        raise TimelineOpError("start must be >= 0")
    clip["start"] = new_start
    _track(doc, op["track"])["clips"].sort(key=lambda c: c["start"])


def _op_trim_item(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    edge = op.get("edge", "end")
    if edge == "start":
        new_start = float(op["start"])
        old_start = float(clip["start"])
        if new_start < 0 or new_start >= old_start + float(clip["duration"]):
            raise TimelineOpError("trim start out of range")
        # keep source offsets correct: content under the playhead stays put
        clip["source_start"] = float(clip.get("source_start", 0.0)) + (new_start - old_start)
        clip["duration"] = old_start + float(clip["duration"]) - new_start
        clip["start"] = new_start
    elif edge == "end":
        new_end = float(op["end"])
        if new_end <= float(clip["start"]):
            raise TimelineOpError("trim end must be after start")
        clip["duration"] = new_end - float(clip["start"])
    else:
        raise TimelineOpError("edge must be 'start' or 'end'")


def _op_split_item(doc: dict, op: dict) -> None:
    tr = _track(doc, op["track"])
    clip = find_clip(doc, op["track"], op["clip_id"])
    at = float(op["at"])
    start, duration = float(clip["start"]), float(clip["duration"])
    if not start < at < start + duration:
        raise TimelineOpError("split point must be strictly inside the clip")
    first = copy.deepcopy(clip)
    second = copy.deepcopy(clip)
    first["duration"] = at - start
    second["id"] = f"{clip['id']}__b"
    second["name"] = f"{clip.get('name', '')} (2)"
    second["start"] = at
    second["duration"] = start + duration - at
    second["source_start"] = float(clip.get("source_start", 0.0)) + (at - start)
    tr["clips"] = [c for c in tr.get("clips", []) if c.get("id") != clip["id"]]
    tr["clips"].extend([first, second])
    tr["clips"].sort(key=lambda c: c["start"])


def _op_duplicate_item(doc: dict, op: dict) -> None:
    tr = _track(doc, op["track"])
    clip = find_clip(doc, op["track"], op["clip_id"])
    gap_at = float(op.get("at", float(clip["start"]) + float(clip["duration"])))
    if gap_at < 0:
        raise TimelineOpError("duplicate position must be >= 0")
    dup = copy.deepcopy(clip)
    dup["id"] = op.get("new_id") or f"{clip['id']}__copy"
    dup["start"] = gap_at
    tr["clips"].append(dup)
    tr["clips"].sort(key=lambda c: c["start"])


def _op_move_to_track(doc: dict, op: dict) -> None:
    src, dst = op["from_track"], op["to_track"]
    if dst not in TRACK_KINDS:
        raise TimelineOpError(f"unknown destination track '{dst}'")
    if _family(src) != _family(dst):
        raise TimelineOpError(
            f"incompatible move {_family(src)} → {_family(dst)} ({src} → {dst})")
    src_tr = _track(doc, src)
    clip = find_clip(doc, src, op["clip_id"])
    src_tr["clips"] = [c for c in src_tr.get("clips", []) if c.get("id") != clip["id"]]
    moved = copy.deepcopy(clip)
    if "start" in op:
        moved["start"] = float(op["start"])
    _track(doc, dst)["clips"].append(moved)
    _track(doc, dst)["clips"].sort(key=lambda c: c["start"])


def _op_update_transform(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    allowed = {"x", "y", "scale", "rotation", "opacity", "crop", "z_index"}
    patch = op.get("transform", {})
    if not isinstance(patch, dict) or not patch:
        raise TimelineOpError("transform patch must be a non-empty object")
    for key in patch:
        if key not in allowed:
            raise TimelineOpError(f"unknown transform key '{key}'")
    merged = dict(clip.get("transform") or {})
    merged.update({k: patch[k] for k in allowed if k in patch})
    clip["transform"] = merged


def _op_update_volume(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    volume = float(op["volume"])
    fade_in = float(op.get("fade_in", clip.get("fade_in", 0.0)))
    fade_out = float(op.get("fade_out", clip.get("fade_out", 0.0)))
    if not 0.0 <= volume <= 4.0:
        raise TimelineOpError("volume out of range 0..4")
    clip["volume"] = volume
    clip["fade_in"] = fade_in
    clip["fade_out"] = fade_out


def _op_update_speed(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    speed = float(op["speed"])
    if not 0.25 <= speed <= 4.0:
        raise TimelineOpError("speed out of range 0.25..4")
    clip["speed"] = speed


def _op_update_text(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    patch = op.get("text", {})
    if not isinstance(patch, dict):
        raise TimelineOpError("text patch must be an object")
    allowed = {"content", "font", "size", "weight", "color", "align", "opacity"}
    merged = dict(clip.get("text") or {})
    for key in patch:
        if key not in allowed:
            raise TimelineOpError(f"unknown text key '{key}'")
    merged.update({k: patch[k] for k in allowed if k in patch})
    clip["text"] = merged
    if "start" in op:
        clip["start"] = float(op["start"])
    if "duration" in op:
        duration = float(op["duration"])
        if duration <= 0:
            raise TimelineOpError("duration must be > 0")
        clip["duration"] = duration


def _op_set_keyframes(doc: dict, op: dict) -> None:
    """Replace a clip's whole keyframe chain with a validated one.

    Used by the CreativeDirector animation commands: one command owns the
    properties it animates and must not silently erase the clip's OTHER
    keyframed properties, so the caller sends the merged chain explicitly.
    """
    from app.engine.motion.graph import validate_keyframes

    clip = find_clip(doc, op["track"], op["clip_id"])
    raw = op.get("keyframes")
    if not isinstance(raw, list):
        raise TimelineOpError("keyframes must be a list")
    merged, problems = validate_keyframes(raw,
                                          clip_duration=_clip_duration(clip))
    if problems:
        raise TimelineOpError(str(problems[0]))
    clip["keyframes"] = merged


def _op_add_keyframe(doc: dict, op: dict) -> None:
    """Add ONE canonical keyframe, validating through the graph schema."""
    from app.engine.motion.graph import validate_keyframes

    clip = find_clip(doc, op["track"], op["clip_id"])
    frame = op.get("keyframe")
    if not isinstance(frame, dict):
        raise TimelineOpError("keyframe must be an object")
    existing = list(clip.get("keyframes") or [])
    # Validate the incoming frame in isolation first, then against the clip.
    _probe, problems = validate_keyframes([frame], clip_duration=_clip_duration(clip))
    if problems:
        raise TimelineOpError(str(problems[0]))
    merged, problems = validate_keyframes(existing + [frame],
                                          clip_duration=_clip_duration(clip))
    if problems:
        raise TimelineOpError(str(problems[0]))
    clip["keyframes"] = merged


def _op_update_keyframe(doc: dict, op: dict) -> None:
    """Update an existing keyframe's props/easing (never its identity)."""
    from app.engine.motion.graph import validate_keyframes

    clip = find_clip(doc, op["track"], op["clip_id"])
    target = str(op.get("keyframe_id") or "")
    existing = [dict(f) for f in (clip.get("keyframes") or [])]
    found = False
    for frame in existing:
        if str(frame.get("id")) == target:
            frame.update({k: v for k, v in (op.get("keyframe") or {}).items()
                          if k in ("t", "easing", "props")})
            found = True
    if not found:
        raise TimelineOpError(f"keyframe {target!r} not found on this clip")
    merged, problems = validate_keyframes(existing,
                                          clip_duration=_clip_duration(clip))
    if problems:
        raise TimelineOpError(str(problems[0]))
    clip["keyframes"] = merged


def _op_delete_keyframe(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    target = str(op.get("keyframe_id") or "")
    existing = [f for f in (clip.get("keyframes") or [])
                if str(f.get("id")) != target]
    if len(existing) == len(clip.get("keyframes") or []):
        raise TimelineOpError(f"keyframe {target!r} not found on this clip")
    clip["keyframes"] = existing


def _op_move_keyframe(doc: dict, op: dict) -> None:
    """Re-time a keyframe, keeping deterministic ordering by (t, id)."""
    from app.engine.motion.graph import validate_keyframes

    clip = find_clip(doc, op["track"], op["clip_id"])
    target = str(op.get("keyframe_id") or "")
    try:
        at = float(op.get("t"))
    except (TypeError, ValueError):
        raise TimelineOpError("t must be a number") from None
    existing = [dict(f) for f in (clip.get("keyframes") or [])]
    found = False
    for frame in existing:
        if str(frame.get("id")) == target:
            frame["t"] = at
            found = True
    if not found:
        raise TimelineOpError(f"keyframe {target!r} not found on this clip")
    merged, problems = validate_keyframes(existing,
                                          clip_duration=_clip_duration(clip))
    if problems:
        raise TimelineOpError(str(problems[0]))
    clip["keyframes"] = merged


def _clip_duration(clip: dict) -> float:
    try:
        return float(clip.get("duration", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _op_update_caption(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    if "text" in op:
        clip["name"] = str(op["text"])[:500]
    if "start" in op:
        clip["start"] = float(op["start"])
    if "duration" in op:
        duration = float(op["duration"])
        if duration <= 0:
            raise TimelineOpError("duration must be > 0")
        clip["duration"] = duration
    if "style" in op:
        clip.setdefault("text", {})["preset"] = str(op["style"])[:40]


# -- Work 13 typed ops -------------------------------------------------------
# Every one of these validates through the Work 13 schema BEFORE touching the
# document, so an invalid style/effect/transition can never be persisted, and
# each produces a plain dict patch the frontend `inverseOps` can mirror for
# undo.


def _op_update_caption_style(doc: dict, op: dict) -> None:
    """Apply a typed caption style (and/or preset) to a caption/text clip."""
    from app.engine.captions.style import CaptionStyle, CaptionStyleError

    clip = find_clip(doc, op["track"], op["clip_id"])
    preset = op.get("preset")
    if preset:
        from app.engine.captions.presets import UnknownPresetError, get_preset

        try:
            base = get_preset(str(preset)).style
        except UnknownPresetError as exc:
            raise TimelineOpError(str(exc)) from exc
        clip.setdefault("text", {})["preset"] = str(preset)[:40]
    else:
        base = CaptionStyle.from_dict(clip.get("text") or {})
    patch = op.get("style")
    try:
        if patch:
            resolved = base.patch(patch)
        elif preset:
            resolved = base
        else:
            raise TimelineOpError(
                "update_caption_style needs either 'preset' or 'style'")
    except CaptionStyleError as exc:
        raise TimelineOpError(str(exc)) from exc
    clip["text"] = {**(clip.get("text") or {}), **resolved.to_dict(),
                    "preset": str(preset)[:40] if preset
                    else (clip.get("text") or {}).get("preset", "minimal")}


def _op_set_caption_words(doc: dict, op: dict) -> None:
    """Attach real word timings + emphasis to a caption clip.

    Word timings are only ever STORED, never synthesised: an empty list is
    accepted (it clears the words) but a word without numeric timing is not.
    """
    clip = find_clip(doc, op["track"], op["clip_id"])
    raw = op.get("words")
    if not isinstance(raw, list):
        raise TimelineOpError("words must be a list")
    words: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TimelineOpError(f"words[{index}] must be an object")
        try:
            start = float(item.get("start_s"))
            end = float(item.get("end_s"))
        except (TypeError, ValueError):
            raise TimelineOpError(
                f"words[{index}] needs numeric start_s/end_s") from None
        if end < start:
            start, end = end, start
        words.append({
            "word": str(item.get("word") or "")[:200],
            "start_s": round(start, 4), "end_s": round(end, 4),
            "speaker_id": str(item.get("speaker_id") or "") or None,
            "confidence": item.get("confidence"),
        })
    clip["words"] = words
    clip["word_level"] = bool(words)


def _op_apply_effect(doc: dict, op: dict) -> None:
    """Append a validated effect to a clip (typed registry, §8)."""
    from app.engine.motion.effects import EffectError, validate_effect

    clip = find_clip(doc, op["track"], op["clip_id"])
    try:
        validated = validate_effect(op.get("effect") or {})
    except EffectError as exc:
        raise TimelineOpError(str(exc)) from exc
    effects = list(clip.get("effects") or [])
    for index, existing in enumerate(effects):
        if isinstance(existing, dict) and str(existing.get("type")) == validated["type"]:
            effects[index] = {"type": validated["type"],
                              "params": validated["params"],
                              "enabled": validated["enabled"]}
            clip["effects"] = effects
            return
    effects.append({"type": validated["type"], "params": validated["params"],
                    "enabled": validated["enabled"]})
    clip["effects"] = effects


def _op_remove_effect(doc: dict, op: dict) -> None:
    clip = find_clip(doc, op["track"], op["clip_id"])
    target = str(op.get("effect") or op.get("effect_type") or "").upper()
    effects = list(clip.get("effects") or [])
    kept = [e for e in effects
            if not (isinstance(e, dict) and str(e.get("type", "")).upper() == target)]
    if len(kept) == len(effects):
        raise TimelineOpError(f"clip has no {target!r} effect to remove")
    clip["effects"] = kept


def _op_set_transition(doc: dict, op: dict) -> None:
    """Validate a transition against the real document (§9)."""
    from app.engine.motion.transitions import TransitionError, validate_transition

    clip = find_clip(doc, op["track"], op["clip_id"])
    spec = dict(op.get("transition") or {})
    spec.setdefault("from_item", str(clip.get("id")))
    if not spec.get("to_item"):
        spec["to_item"] = str(op.get("to_item") or clip.get("next_item") or "")
    try:
        validated = validate_transition(spec, doc)
    except TransitionError as exc:
        raise TimelineOpError(str(exc)) from exc
    clip["transition"] = validated
    clip["transition_in"] = "crossfade" if validated["duration"] > 0 else "cut"
