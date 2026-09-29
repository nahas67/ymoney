"""OTIO adapter: the ONLY place that imports opentimelineio.

YMONEY's canonical Timeline (engine/timeline.py dict shape) stays independent;
this module translates to/from real OTIO objects for interchange and NLE
exports. Pinned: opentimelineio==0.18.1 (Apache-2.0, see docs/oss/OSS_COMPONENTS.md).
Lossy notes: track display names survive in metadata; clip effects/source dicts
round-trip via OTIO metadata (OTIO-native consumers ignore them, YMONEY keeps them).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import opentimelineio as otio

from app.engine import timeline as tl
from app.engine.timeline import TimelineValidationError


def _plain(value):
    """Deep-copy an OTIO metadata value into JSON-safe builtins.

    OTIO hands back ``AnyDictionary`` for nested metadata objects. It is a
    ``MutableMapping`` but deliberately NOT a ``dict`` subclass, so an
    ``isinstance(x, dict)`` guard rejects every nested value -- and the
    surviving references are C++-backed, so they dangle (``ValueError:
    Underlying C++ AnyDictionary has been destroyed``) once the Timeline they
    came from is garbage collected, and they are not JSON serializable.
    Convert eagerly while the owning timeline is still alive.
    """
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    # OTIO returns AnyVector for nested arrays: a Sequence, but not list/tuple.
    # isinstance(str) must be excluded or strings split into characters.
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

TRACK_KIND_TO_OTIO = {"video": otio.schema.TrackKind.Video,
                      "broll": otio.schema.TrackKind.Video,
                      "avatar": otio.schema.TrackKind.Video,
                      "text": otio.schema.TrackKind.Video,
                      "caption": otio.schema.TrackKind.Video,
                      "voice": otio.schema.TrackKind.Audio,
                      "music": otio.schema.TrackKind.Audio,
                      "sfx": otio.schema.TrackKind.Audio}


def to_otio_timeline(doc: dict) -> otio.schema.Timeline:
    """Canonical doc → real OTIO Timeline (raises TimelineValidationError when invalid)."""
    tl.validate_timeline(doc)
    fps = float(doc.get("fps", 30.0))
    timeline = otio.schema.Timeline(name=doc.get("name", "main"))
    for tr in doc.get("tracks", []):
        track = otio.schema.Track(name=tr.get("name", tr["kind"]),
                                  kind=TRACK_KIND_TO_OTIO[tr["kind"]])
        for c in tr.get("clips", []):
            rng = otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(float(c["start"]), fps),
                duration=otio.opentime.RationalTime(float(c["duration"]), fps),
            )
            clip = otio.schema.Clip(
                name=c.get("name", c["id"]),
                source_range=rng,
                metadata={"ymoney_clip_id": c["id"],
                          "ymoney_track": tr["kind"],
                          "source": dict(c.get("source", {})),
                          "effects": list(c.get("effects", [])),
                          "ymoney_clip": {k: v for k, v in c.items()
                                          if k not in ("id", "name", "start", "duration",
                                                       "source", "effects")}},
            )
            track.append(clip)
        timeline.tracks.append(track)
    return timeline


def from_otio_timeline(timeline: otio.schema.Timeline, *, aspect: str = "9:16") -> dict:
    """Real OTIO Timeline → canonical doc (raises TimelineValidationError when invalid)."""
    fps = 30.0
    doc: dict = {"tracks": [], "duration_seconds": 0.0, "fps": fps, "aspect_ratio": aspect,
                 "name": timeline.name or "main"}
    # YMONEY track per OTIO track, kind recovered from clip metadata (default video/audio)
    for track in timeline.tracks:
        is_audio = track.kind == otio.schema.TrackKind.Audio
        clips = []
        for clip in track:
            if not isinstance(clip, otio.schema.Clip):
                continue
            md = _plain(clip.metadata or {})
            kind = md.get("ymoney_track", "voice" if is_audio else "video")
            rng = clip.source_range
            start = rng.start_time.value if rng else 0.0
            duration = rng.duration.value if rng else 0.0
            entry = {"id": md.get("ymoney_clip_id", clip.name or "clip"),
                     "name": clip.name or "",
                     "start": float(start), "duration": float(duration),
                     "source": md.get("source") or {},
                     "effects": list(md.get("effects") or []),
                     "_kind": kind}
            extra = md.get("ymoney_clip")
            # AnyDictionary is a MutableMapping, never a dict -- hence Mapping.
            if isinstance(extra, Mapping):
                entry.update({k: v for k, v in extra.items() if k not in entry})
            clips.append(entry)
            if rng:
                fps = float(rng.start_time.rate or fps)
        # group clips by their recorded kind (OTIO tracks are single-kind; YMONEY keeps kinds)
        by_kind: dict[str, list] = {}
        for c in clips:
            by_kind.setdefault(c.pop("_kind"), []).append(c)
        for kind, group in by_kind.items():
            doc["tracks"].append({"id": f"t_{kind}_{track.name or kind}", "kind": kind,
                                  "name": f"{track.name or kind}", "clips": group})
        if not clips:
            kind = "voice" if is_audio else "video"
            doc["tracks"].append({"id": f"t_{kind}_{track.name or kind}", "kind": kind,
                                  "name": f"{track.name or kind}", "clips": []})
    doc["fps"] = fps
    # duration = max clip end across tracks
    ends = [c["start"] + c["duration"] for tr in doc["tracks"] for c in tr["clips"]]
    doc["duration_seconds"] = max(ends) if ends else 0.0
    return tl.validate_timeline(doc)


def roundtrip_serialized(doc: dict) -> dict:
    """OTIO JSON serialize → deserialize → canonical doc. Proves interchange fidelity."""
    timeline = to_otio_timeline(doc)
    text = otio.adapters.write_to_string(timeline, "otio_json")
    back = otio.adapters.read_from_string(text, "otio_json")
    return from_otio_timeline(back, aspect=doc.get("aspect_ratio", "9:16"))


# -- user-facing export (TimelineExporter concept) -----------------------------

def export_formats() -> dict:
    """Availability per format. Only verified formats are exposed; everything
    else reports NOT_AVAILABLE with a reason instead of fake files."""
    try:
        available = set(otio.adapters.available_adapter_names())
    except Exception:
        available = {"otio_json"}
    out = {"otio": {"status": "AVAILABLE", "adapter": "otio_json",
                    "suffix": ".otio", "media_type": "application/json"}}
    if "fcpxml" in available:
        out["fcpxml"] = {"status": "AVAILABLE", "adapter": "fcpxml",
                         "suffix": ".fcpxml", "media_type": "application/xml"}
    else:
        out["fcpxml"] = {"status": "NOT_AVAILABLE",
                         "reason": "fcpxml adapter not in this OTIO build"}
    out["premiere"] = {"status": "NOT_AVAILABLE",
                       "reason": "no verified Premiere interchange adapter in OTIO"}
    out["resolve"] = {"status": "NOT_AVAILABLE",
                      "reason": "no verified DaVinci Resolve adapter in OTIO"}
    return out


def export_timeline(doc: dict, fmt: str) -> tuple[str, str, str]:
    """(filename, media_type, content) for a verified format.

    Raises TimelineValidationError for unknown/unsupported formats.
    """
    info = export_formats().get(fmt)
    if info is None or info["status"] != "AVAILABLE":
        reason = (info or {}).get("reason", f"unknown format '{fmt}'")
        raise TimelineValidationError(f"export {fmt}: {reason}")
    timeline = to_otio_timeline(doc)
    try:
        content = otio.adapters.write_to_string(timeline, info["adapter"])
    except Exception as exc:
        raise TimelineValidationError(f"export {fmt} failed: {exc}") from exc
    # prove the artifact parses before serving it (never ship fake files)
    try:
        otio.adapters.read_from_string(content, info["adapter"])
    except Exception as exc:
        raise TimelineValidationError(f"export {fmt} produced invalid output: {exc}") from exc
    name = (doc.get("name") or "timeline").strip().replace(" ", "_")[:60] or "timeline"
    return f"{name}{info['suffix']}", info["media_type"], content
