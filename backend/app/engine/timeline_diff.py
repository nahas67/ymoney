"""Semantic version diff for canonical timeline documents.

Pure functions over the tracks-JSON shape (see engine/timeline.py): no I/O,
no DB access, no text/JSON diffing. The output names *what* changed — clips
added, removed, trimmed, moved, re-sourced, re-worded, re-voiced — so the
editor can render a readable conflict/version notice instead of two blobs.

Diff bucket rules (only keys that actually changed are emitted):

* ``timing``  — clip position on the timeline: ``start`` and ``end``
  (end = start + duration).
* ``trim``    — the window taken from the source: ``start`` (= source_start)
  and ``end`` (= source_start + duration).
* ``asset``   — the media reference: ``source`` minus the voice/TTS keys.
* ``voice``   — voice/TTS keys (clip-level and inside ``source``).
* ``text``    — ``text`` / ``caption`` / ``content`` payloads (on-screen words).
* ``metadata``— every other changed clip field (name, speed, volume, fades,
  transform, effects, transitions, and unknown future fields).
"""

from __future__ import annotations

#: ``source`` keys that describe the SPOKEN voice, not the media asset
VOICE_SOURCE_KEYS = (
    "voice_id", "voice", "tts_voice", "narrator_voice",
    "voice_name", "voice_style", "speaker", "tts",
)
#: clip-level voice/TTS fields (producers that write them at the top level)
VOICE_CLIP_KEYS = (
    "voice_id", "voice", "tts_voice", "voice_name",
    "narrator", "narrator_voice", "tts",
)
#: clip keys that carry on-screen words
TEXT_KEYS = ("text", "caption", "content")

_TIMING_KEYS = ("start", "duration", "source_start")
#: doc-level keys that are structural (diffed through tracks) or fixed
_DOC_SKIP_KEYS = ("tracks", "workspace_id")
#: clip keys owned by timing/trim, source, text or voice — never "metadata"
_SKIP_CLIP_KEYS = (frozenset(_TIMING_KEYS) | frozenset(TEXT_KEYS)
                   | frozenset(VOICE_CLIP_KEYS) | {"source"})


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _voice_view(clip: dict) -> dict:
    """Only the voice/TTS part of a clip (top level + inside ``source``)."""
    out = {k: clip[k] for k in VOICE_CLIP_KEYS if k in clip}
    source = clip.get("source")
    if isinstance(source, dict):
        out.update({k: source[k] for k in VOICE_SOURCE_KEYS if k in source})
    return out


def _asset_view(clip: dict) -> dict:
    """The media part of a clip: ``source`` without the voice/TTS keys."""
    source = clip.get("source")
    if not isinstance(source, dict):
        return {}
    return {k: v for k, v in source.items() if k not in VOICE_SOURCE_KEYS}


def _clip_changes(before: dict, after: dict) -> dict:
    """Field-level changes for one clip pair (empty dict = no change)."""
    changes: dict = {}

    b_start, a_start = _num(before.get("start")), _num(after.get("start"))
    b_dur, a_dur = _num(before.get("duration")), _num(after.get("duration"))
    b_src, a_src = _num(before.get("source_start")), _num(after.get("source_start"))

    timing: dict = {}
    if b_start != a_start:
        timing["start"] = {"before": b_start, "after": a_start}
    if b_start + b_dur != a_start + a_dur:
        timing["end"] = {"before": b_start + b_dur, "after": a_start + a_dur}
    if timing:
        changes["timing"] = timing

    trim: dict = {}
    if b_src != a_src:
        trim["start"] = {"before": b_src, "after": a_src}
    if b_src + b_dur != a_src + a_dur:
        trim["end"] = {"before": b_src + b_dur, "after": a_src + a_dur}
    if trim:
        changes["trim"] = trim

    voice_b, voice_a = _voice_view(before), _voice_view(after)
    if voice_b != voice_a:
        changes["voice"] = {"before": voice_b, "after": voice_a}

    asset_b, asset_a = _asset_view(before), _asset_view(after)
    if asset_b != asset_a:
        changes["asset"] = {"before": asset_b, "after": asset_a}

    text_b: dict = {}
    text_a: dict = {}
    for key in TEXT_KEYS:
        if before.get(key) != after.get(key):
            text_b[key] = before.get(key)
            text_a[key] = after.get(key)
    if text_b or text_a:
        changes["text"] = {"before": text_b, "after": text_a}

    meta_b: dict = {}
    meta_a: dict = {}
    for key in sorted(set(before) | set(after)):
        if key in _SKIP_CLIP_KEYS:
            continue
        if before.get(key) != after.get(key) or (key in before) != (key in after):
            meta_b[key] = before.get(key)
            meta_a[key] = after.get(key)
    if meta_b or meta_a:
        changes["metadata"] = {"before": meta_b, "after": meta_a}

    return changes


def _pair_tracks(before_tracks: list, after_tracks: list):
    """(matched pairs, added tracks, removed tracks): id first, then kind."""
    used_after: set[int] = set()
    matched_before: set[int] = set()
    pairs: list = []

    after_by_id: dict = {}
    for idx, track in enumerate(after_tracks):
        track_id = track.get("id")
        if track_id is not None and str(track_id) not in after_by_id:
            after_by_id[str(track_id)] = idx

    for idx, track in enumerate(before_tracks):
        track_id = track.get("id")
        other = after_by_id.get(str(track_id)) if track_id is not None else None
        if other is not None and other not in used_after:
            pairs.append((track, after_tracks[other]))
            matched_before.add(idx)
            used_after.add(other)

    for idx, track in enumerate(before_tracks):
        if idx in matched_before:
            continue
        kind = track.get("kind")
        if kind is None:
            continue
        for other, candidate in enumerate(after_tracks):
            if other in used_after or candidate.get("kind") != kind:
                continue
            pairs.append((track, candidate))
            matched_before.add(idx)
            used_after.add(other)
            break

    added = [t for i, t in enumerate(after_tracks) if i not in used_after]
    removed = [t for i, t in enumerate(before_tracks) if i not in matched_before]
    return pairs, added, removed


def _pair_clips(before_clips: list, after_clips: list):
    """Clip pairs inside one track: by clip id, else by position."""
    ids = [c.get("id") for c in before_clips] + [c.get("id") for c in after_clips]
    if ids and all(v not in (None, "") for v in ids):
        leftovers: dict = {}
        for clip in before_clips:
            leftovers.setdefault(str(clip["id"]), []).append(clip)
        pairs: list = []
        for clip in after_clips:
            queue = leftovers.get(str(clip["id"]))
            pairs.append((queue.pop(0) if queue else None, clip))
        for queue in leftovers.values():
            pairs.extend((clip, None) for clip in queue)
        return pairs
    # ids missing on either side → fall back to (track, position)
    pairs = []
    for i in range(max(len(before_clips), len(after_clips))):
        pairs.append((
            before_clips[i] if i < len(before_clips) else None,
            after_clips[i] if i < len(after_clips) else None,
        ))
    return pairs


def _clip_ref(track_id: str, track_kind: str, clip: dict) -> dict:
    return {
        "track_id": track_id,
        "track_kind": track_kind,
        "clip_id": str(clip.get("id") or ""),
        "name": clip.get("name", ""),
        "start": _num(clip.get("start")),
        "duration": _num(clip.get("duration")),
    }


def _track_ref(track: dict) -> dict:
    clips = track.get("clips") or []
    return {
        "track_id": str(track.get("id") or track.get("kind") or ""),
        "kind": str(track.get("kind") or ""),
        "name": str(track.get("name") or ""),
        "clip_count": len(clips),
    }


def diff_timeline_docs(before: dict, after: dict) -> dict:
    """Structured, semantic diff of two timeline documents.

    Clips are matched by clip id inside a matched track (track id first, then
    track kind); when either side has clips without ids the match falls back to
    position inside the track. Identical docs → empty lists + zero summary.
    """
    before = before or {}
    after = after or {}
    pairs, added_tracks, removed_tracks = _pair_tracks(
        list(before.get("tracks") or []), list(after.get("tracks") or []))

    added_clips: list = []
    removed_clips: list = []
    modified_clips: list = []

    for track_b, track_a in pairs:
        ref = track_a or track_b or {}
        track_id = str(ref.get("id") or ref.get("kind") or "")
        track_kind = str(ref.get("kind") or "")
        for clip_b, clip_a in _pair_clips(
            list((track_b or {}).get("clips") or []),
            list((track_a or {}).get("clips") or []),
        ):
            if clip_b is None and clip_a is not None:
                added_clips.append(_clip_ref(track_id, track_kind, clip_a))
            elif clip_a is None and clip_b is not None:
                removed_clips.append(_clip_ref(track_id, track_kind, clip_b))
            else:
                changes = _clip_changes(clip_b or {}, clip_a or {})
                if changes:
                    modified_clips.append({
                        "track_id": track_id,
                        "track_kind": track_kind,
                        "clip_id": str((clip_a or {}).get("id")
                                       or (clip_b or {}).get("id") or ""),
                        "changes": changes,
                    })

    # clips riding a wholly new / gone track still count as added / removed
    for track in added_tracks:
        for clip in (track.get("clips") or []):
            added_clips.append(_clip_ref(
                str(track.get("id") or track.get("kind") or ""),
                str(track.get("kind") or ""), clip))
    for track in removed_tracks:
        for clip in (track.get("clips") or []):
            removed_clips.append(_clip_ref(
                str(track.get("id") or track.get("kind") or ""),
                str(track.get("kind") or ""), clip))

    metadata_changes: dict = {}
    for key in sorted((set(before) | set(after)) - set(_DOC_SKIP_KEYS)):
        if before.get(key) != after.get(key) or (key in before) != (key in after):
            metadata_changes[key] = {"before": before.get(key),
                                     "after": after.get(key)}

    return {
        "added_clips": added_clips,
        "removed_clips": removed_clips,
        "modified_clips": modified_clips,
        "added_tracks": [_track_ref(t) for t in added_tracks],
        "removed_tracks": [_track_ref(t) for t in removed_tracks],
        "metadata_changes": metadata_changes,
        "summary": {
            "added": len(added_clips),
            "removed": len(removed_clips),
            "modified": len(modified_clips),
            "tracks_added": len(added_tracks),
            "tracks_removed": len(removed_tracks),
            "metadata_changed": len(metadata_changes),
            "changed": bool(added_clips or removed_clips or modified_clips
                            or added_tracks or removed_tracks or metadata_changes),
        },
    }


def diff_brand_snapshots(before_dna: dict, after_dna: dict) -> dict:
    """Key-level BrandDNA diff: changed / added / removed keys.

    Nested dicts are compared one level deep (``changed[key]`` then carries the
    full before/after values plus the sub-keys that moved); anything deeper is
    compared as a whole value. Total: unknown shapes never raise.
    """
    before = dict(before_dna or {})
    after = dict(after_dna or {})
    changed: dict = {}
    added: dict = {}
    removed: dict = {}

    for key in sorted(set(before) | set(after)):
        if key not in before:
            added[key] = {"after": after[key]}
        elif key not in after:
            removed[key] = {"before": before[key]}
        elif before[key] != after[key]:
            entry: dict = {"before": before[key], "after": after[key]}
            if isinstance(before[key], dict) and isinstance(after[key], dict):
                entry["changed_keys"] = sorted(
                    k for k in set(before[key]) | set(after[key])
                    if before[key].get(k) != after[key].get(k)
                    or (k in before[key]) != (k in after[key]))
            changed[key] = entry

    return {"changed": changed, "added": added, "removed": removed}
