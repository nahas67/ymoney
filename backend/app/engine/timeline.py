"""Canonical timeline engine: pure functions over the tracks-JSON shape.

No I/O, no new dependencies. The OTIO functions speak OTIO-compatible plain
JSON (a future task binds the real `opentimelineio` library behind these same
signatures). The render engine is untouched — `render_manifest` describes what
to build, including a stable hash so the engine/UI can detect changes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import ContentTimeline

TRACK_KINDS = ("video", "broll", "avatar", "text", "caption", "voice", "music", "sfx")

# aspect ratios the repurpose/render path can actually produce
SUPPORTED_ASPECTS = ("9:16", "16:9", "1:1", "4:5")

#: fallback when a source video reports no usable duration (keeps editor load alive)
FALLBACK_DURATION_SECONDS = 5.0


class TimelineValidationError(ValueError):
    """Raised with the offending track kind + clip id in the message."""


def create_empty(workspace_id: str, *, duration_seconds: float = 0.0,
                 fps: float = 30.0, aspect: str = "9:16") -> dict:
    """One empty track per visual/audio family so editors can assume tracks exist."""
    tracks = [
        {"id": f"t_{kind}", "kind": kind, "name": kind.title(), "clips": []}
        for kind in TRACK_KINDS
    ]
    return {"workspace_id": workspace_id, "tracks": tracks,
            "duration_seconds": float(duration_seconds or 0.0),
            "fps": float(fps or 30.0), "aspect_ratio": aspect}


def _track(doc: dict, kind: str) -> dict:
    for tr in doc.get("tracks", []):
        if tr.get("kind") == kind:
            return tr
    raise TimelineValidationError(f"unknown track kind '{kind}'")


def add_clip(doc: dict, *, track: str, clip_id: str, name: str,
             start: float, duration: float, source: dict | None = None,
             effects: list | None = None, source_start: float = 0.0,
             volume: float = 1.0, speed: float = 1.0,
             fade_in: float = 0.0, fade_out: float = 0.0,
             transform: dict | None = None, text: dict | None = None,
             transition_in: str = "cut", transition_out: str = "cut") -> dict:
    """Append a clip. Extended fields (all optional, all preserved by OTIO):

    source_start — offset into the source asset; volume 0..4; speed 0.25..4;
    fade_in/out seconds; transform {x,y,scale,rotation,opacity,crop};
    text {content,font,size,weight,color,align}; transitions cut|fade|crossfade.
    """
    if not 0.25 <= float(speed) <= 4.0:
        raise TimelineValidationError(f"clip '{clip_id}' speed out of range 0.25..4")
    if not 0.0 <= float(volume) <= 4.0:
        raise TimelineValidationError(f"clip '{clip_id}' volume out of range 0..4")
    if transition_in not in ("cut", "fade", "crossfade") or transition_out not in ("cut", "fade", "crossfade"):
        raise TimelineValidationError(f"clip '{clip_id}' has unsupported transition")
    tr = _track(doc, track)
    tr["clips"].append({"id": clip_id, "name": name,
                        "start": float(start), "duration": float(duration),
                        "source": dict(source or {}), "effects": list(effects or []),
                        "source_start": float(source_start), "volume": float(volume),
                        "speed": float(speed), "fade_in": float(fade_in),
                        "fade_out": float(fade_out),
                        "transform": dict(transform or {}),
                        "text": dict(text or {}),
                        "transition_in": transition_in, "transition_out": transition_out})
    tr["clips"].sort(key=lambda c: c["start"])
    doc["duration_seconds"] = max(float(doc.get("duration_seconds") or 0.0),
                                  float(start) + float(duration))
    return doc


def validate_timeline(doc: dict) -> dict:
    """Overlap/negative/unknown-kind guard. Returns doc when valid."""
    seen_kinds = set()
    for tr in doc.get("tracks", []):
        kind = tr.get("kind")
        if kind not in TRACK_KINDS:
            raise TimelineValidationError(f"unknown track kind '{kind}'")
        seen_kinds.add(kind)
        ordered = sorted(tr.get("clips", []), key=lambda c: c.get("start", 0.0))
        cursor = 0.0
        for clip in ordered:
            start = float(clip.get("start", 0.0))
            duration = float(clip.get("duration", 0.0))
            if duration <= 0:
                raise TimelineValidationError(
                    f"track '{kind}' clip '{clip.get('id')}' has non-positive duration")
            if start < cursor - 1e-6:
                # 1e-6 tolerance: algebraically tiled boundaries
                # (a+s*k vs (a+s2*k)+d2*k) can differ by 1 ulp of float
                # arithmetic — thousands of times below one video frame
                raise TimelineValidationError(
                    f"track '{kind}' clip '{clip.get('id')}' overlaps previous clip")
            speed = float(clip.get("speed", 1.0))
            if not 0.25 <= speed <= 4.0:
                raise TimelineValidationError(
                    f"track '{kind}' clip '{clip.get('id')}' speed out of range 0.25..4")
            volume = float(clip.get("volume", 1.0))
            if not 0.0 <= volume <= 4.0:
                raise TimelineValidationError(
                    f"track '{kind}' clip '{clip.get('id')}' volume out of range 0..4")
            for key in ("transition_in", "transition_out"):
                if clip.get(key, "cut") not in ("cut", "fade", "crossfade"):
                    raise TimelineValidationError(
                        f"track '{kind}' clip '{clip.get('id')}' has unsupported transition")
            cursor = start + duration
    return doc


def to_otio_dict(doc: dict) -> dict:
    """Export to an OTIO-compatible Timeline dict (plain JSON, no lib needed)."""
    validate_timeline(doc)
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": doc.get("name", "main"),
        "global_start_time": {"rate": doc.get("fps", 30.0), "value": 0.0},
        "tracks": [
            {"OTIO_SCHEMA": "Track.1", "kind": tr["kind"], "name": tr.get("name", tr["kind"]),
             "clips": [
                 {"OTIO_SCHEMA": "Clip.1", "name": c.get("name", c["id"]),
                  "source_range": {"rate": doc.get("fps", 30.0),
                                   "start_time": c["start"], "duration": c["duration"]},
                  "metadata": {"ymoney_clip_id": c["id"], "source": c.get("source", {}),
                               "effects": c.get("effects", []),
                               "ymoney_clip": {k: v for k, v in c.items() if k not in (
                                   "id", "name", "start", "duration", "source", "effects")}}}
                 for c in tr.get("clips", [])
             ]}
            for tr in doc.get("tracks", [])
        ],
        "metadata": {"ymoney": {"aspect_ratio": doc.get("aspect_ratio", "9:16"),
                                "duration_seconds": doc.get("duration_seconds", 0.0),
                                "fps": doc.get("fps", 30.0)}},
    }


def from_otio_dict(otio: dict) -> dict:
    """Inverse of to_otio_dict; raises TimelineValidationError on shape mismatch."""
    try:
        tracks = otio["tracks"]
        meta = otio.get("metadata", {}).get("ymoney", {})
    except (KeyError, AttributeError, TypeError) as exc:
        raise TimelineValidationError(f"not an OTIO timeline dict: {exc}") from exc
    doc: dict = {"tracks": [], "duration_seconds": float(meta.get("duration_seconds", 0.0)),
                 "fps": float(meta.get("fps", 30.0)),
                 "aspect_ratio": meta.get("aspect_ratio", "9:16")}
    for i, tr in enumerate(tracks):
        kind = tr.get("kind")
        if kind not in TRACK_KINDS:
            raise TimelineValidationError(f"unknown track kind '{kind}'")
        clips = []
        for c in tr.get("clips", []):
            rng = c.get("source_range", {})
            md = c.get("metadata", {})
            clip = {"id": md.get("ymoney_clip_id", f"c{i}"),
                    "name": c.get("name", ""),
                    "start": float(rng.get("start_time", 0.0)),
                    "duration": float(rng.get("duration", 0.0)),
                    "source": dict(md.get("source", {})),
                    "effects": list(md.get("effects", []))}
            # full payload (transforms, volume/speed, text, scene linkage)
            # survives our own exports; foreign OTIO keeps the basics above
            extra = md.get("ymoney_clip")
            if isinstance(extra, dict):
                clip.update({k: v for k, v in extra.items() if k not in clip})
            clips.append(clip)
        doc["tracks"].append({"id": f"t_{kind}", "kind": kind,
                              "name": tr.get("name", kind), "clips": clips})
    return validate_timeline(doc)


def timeline_from_video(workspace_id: str, *, video_id: str,
                        duration_seconds: float | None, aspect: str = "9:16",
                        file_path: str = "") -> dict:
    """Import an existing render as a single-clip timeline (shorts included)."""
    duration = float(duration_seconds) if duration_seconds else FALLBACK_DURATION_SECONDS
    doc = create_empty(workspace_id, duration_seconds=duration, aspect=aspect)
    add_clip(doc, track="video", clip_id=f"src_{video_id}", name="source",
             start=0.0, duration=duration,
             source={"video_id": video_id, "file_path": file_path, "aspect": aspect})
    doc["aspect_ratio"] = aspect
    return doc


def shorts_representation(doc: dict, *, aspect: str) -> dict:
    """Can this timeline be framed for the requested vertical/square ratio?"""
    if aspect not in SUPPORTED_ASPECTS:
        return {"ok": False, "aspect": aspect,
                "reason": f"unsupported aspect '{aspect}'; supported: {list(SUPPORTED_ASPECTS)}"}
    return {"ok": True, "aspect": aspect,
            "duration_seconds": doc.get("duration_seconds", 0.0),
            "clip_count": sum(len(tr.get("clips", [])) for tr in doc.get("tracks", []))}


def render_manifest(doc: dict) -> dict:
    """Flattened build description + stable hash for change detection."""
    validate_timeline(doc)
    clips = [{"track": tr["kind"], **c} for tr in doc.get("tracks", [])
             for c in tr.get("clips", [])]
    canonical = json.dumps({"fps": doc.get("fps", 30.0),
                            "duration": doc.get("duration_seconds", 0.0),
                            "aspect": doc.get("aspect_ratio", "9:16"),
                            "clips": clips}, sort_keys=True, default=str)
    return {"fps": doc.get("fps", 30.0),
            "total_duration": doc.get("duration_seconds", 0.0),
            "aspect_ratio": doc.get("aspect_ratio", "9:16"),
            "clip_count": len(clips), "clips": clips,
            "manifest_hash": hashlib.sha256(canonical.encode()).hexdigest()[:32]}


def save_version(session, timeline_id: str, *, label: str = "") -> str:
    """Copy-on-write version: new row, version+1, parent link. Returns new id."""
    from app.models import ContentTimeline

    row = session.get(ContentTimeline, timeline_id)
    if row is None:
        raise TimelineValidationError(f"timeline '{timeline_id}' not found")
    child = ContentTimeline(
        workspace_id=row.workspace_id, content_item_id=row.content_item_id,
        video_id=row.video_id, name=row.name, fps=row.fps,
        duration_seconds=row.duration_seconds,
        tracks_json=copy.deepcopy(row.tracks_json or {}),
        version=(row.version or 1) + 1, parent_timeline_id=row.id,
    )
    session.add(child)
    session.flush()
    _ = label  # label is recorded by the caller's audit event, not the row
    return child.id


def version_family(session, timeline_id: str) -> list:
    """Root → … → tip chain plus all branches: every row in the family."""
    from app.models import ContentTimeline

    tip = session.get(ContentTimeline, timeline_id)
    if tip is None:
        raise TimelineValidationError(f"timeline '{timeline_id}' not found")
    root = tip
    seen = {tip.id}
    while root.parent_timeline_id and root.parent_timeline_id not in seen:
        seen.add(root.parent_timeline_id)
        parent = session.get(ContentTimeline, root.parent_timeline_id)
        if parent is None:
            break
        root = parent
    # BFS down from root
    family = [root]
    queue = [root.id]
    known = {root.id}
    while queue:
        pid = queue.pop(0)
        children = session.query(ContentTimeline).filter(
            ContentTimeline.parent_timeline_id == pid).all()
        for child in children:
            if child.id not in known:
                known.add(child.id)
                family.append(child)
                queue.append(child.id)
    family.sort(key=lambda r: (r.version or 0, str(r.created_at)))
    return root, family


def list_versions(session, timeline_id: str) -> tuple[object, list]:
    """(root, family rows) for the versions panel."""
    return version_family(session, timeline_id)


def tip_version(session, timeline_id: str) -> ContentTimeline | None:
    """Canonical "current version": the highest-`version` row of the family.

    One resolver for every caller that needs an authoritative answer instead
    of re-deriving it (the API list sorts `created_at` desc, engine code sorts
    `version` desc — both can disagree once branches exist). Returns None when
    the timeline does not exist; raises nothing.
    """
    from app.models import ContentTimeline

    if session.get(ContentTimeline, timeline_id) is None:
        return None
    try:
        _, family = version_family(session, timeline_id)
    except TimelineValidationError:
        return None
    if not family:
        return None
    # version first, newest row first on a tie (branches can share a number)
    return max(family, key=lambda row: (int(row.version or 0), str(row.created_at)))


def manifest_hash_of(doc: dict) -> str:
    """Stable `manifest_hash` for a timeline doc (see render_manifest)."""
    return render_manifest(doc)["manifest_hash"]


def restore_version(session, timeline_id: str, version_id: str) -> str:
    """Copy version_id's content onto a NEW tip child of timeline_id.

    History stays append-only: restore never rewrites rows.
    """
    from app.models import ContentTimeline

    tip = session.get(ContentTimeline, timeline_id)
    if tip is None:
        raise TimelineValidationError(f"timeline '{timeline_id}' not found")
    _, family = version_family(session, timeline_id)
    target = next((r for r in family if r.id == version_id), None)
    if target is None:
        raise TimelineValidationError(
            f"version '{version_id}' is not in timeline '{timeline_id}' family")
    child = ContentTimeline(
        workspace_id=tip.workspace_id, content_item_id=tip.content_item_id,
        video_id=tip.video_id, name=tip.name, fps=target.fps,
        duration_seconds=target.duration_seconds,
        tracks_json=copy.deepcopy(target.tracks_json or {}),
        version=(tip.version or 1) + 1, parent_timeline_id=tip.id,
    )
    session.add(child)
    session.flush()
    return child.id


def manifest_to_render_request(doc: dict, *, timeline_name: str = "main",
                               workspace_id: str = "") -> object:
    """Best-effort adapter: canonical timeline → VideoEngine RenderRequest.

    The renderer never parses AI prompts: subject comes from the timeline name,
    the script from voice/caption/text clip names in play order, keywords from
    broll clip names. Returns an un-submitted RenderRequest (caller submits).
    """
    from app.providers.video_engine.base import RenderRequest

    validate_timeline(doc)
    by_kind: dict[str, list] = {}
    for tr in doc.get("tracks", []):
        by_kind.setdefault(tr["kind"], []).extend(
            sorted(tr.get("clips", []), key=lambda c: c.get("start", 0.0)))
    script_lines = [c.get("name", "") for k in ("voice", "caption", "text")
                    for c in by_kind.get(k, []) if c.get("name")]
    keywords = [c.get("name", "") for c in by_kind.get("broll", []) if c.get("name")]
    aspect = doc.get("aspect_ratio", "9:16")
    if aspect not in SUPPORTED_ASPECTS:
        aspect = "9:16"
    return RenderRequest(
        subject=timeline_name,
        script="\n".join(script_lines) or timeline_name,
        keywords=keywords[:12],
        aspect_ratio=aspect,
        workspace_id=workspace_id,
    )
