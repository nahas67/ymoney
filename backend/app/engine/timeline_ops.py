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
            "update_volume", "update_speed", "update_text", "update_caption")


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
