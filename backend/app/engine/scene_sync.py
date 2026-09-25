"""Scene ↔ timeline synchronization (Work 02).

Deterministic adapters that map existing pipeline outputs (B-roll plans,
repurpose segments, caption clips) onto Scene rows — without rewriting those
pipelines. Sync is replace-based per timeline: rerunning with the same inputs
reproduces the same rows.
"""

from __future__ import annotations


def _clear(session, workspace_id: str, timeline_id: str) -> None:
    from app.models.assets import Scene

    for row in session.query(Scene).filter(
            Scene.workspace_id == workspace_id,
            Scene.timeline_id == timeline_id).all():
        session.delete(row)
    session.flush()


def _insert(session, *, workspace_id: str, timeline_id: str,
            content_item_id: str | None, index: int, title: str,
            script_segment: str = "", narration: str = "",
            visual_intent: str = "", start: float = 0.0, end: float = 0.0,
            parent_scene_id: str | None = None) -> object:
    from app.models.assets import Scene

    row = Scene(workspace_id=workspace_id, content_item_id=content_item_id,
                timeline_id=timeline_id, index=index, title=title[:200],
                script_segment=script_segment, narration=narration,
                visual_intent=visual_intent,
                start_seconds=float(start), end_seconds=float(end),
                parent_scene_id=parent_scene_id)
    session.add(row)
    return row


def sync_from_broll_plan(session, *, workspace_id: str, timeline_id: str,
                         content_item_id: str | None, plan: list,
                         total_duration: float) -> list:
    """Map a B-roll visual plan (SceneVisual dicts) onto evenly split scenes."""
    _clear(session, workspace_id, timeline_id)
    total = max(float(total_duration or 0.0), 0.0)
    items = list(plan or [])
    n = max(len(items), 1)
    rows = []
    for i, entry in enumerate(items):
        get = (lambda k, d="": entry.get(k, d) if isinstance(entry, dict) else getattr(entry, k, d))
        rows.append(_insert(
            session, workspace_id=workspace_id, timeline_id=timeline_id,
            content_item_id=content_item_id, index=i,
            title=f"Scene {i + 1}",
            visual_intent=str(get("prompt", "")),
            start=total * i / n, end=total * (i + 1) / n))
    session.flush()
    return rows


def sync_from_segments(session, *, workspace_id: str, timeline_id: str,
                       content_item_id: str | None,
                       segments: list[tuple[float, float] | dict],
                       source: str = "repurpose") -> list:
    """Map detected/repurpose (start, end[, text]) segments onto scenes."""
    _clear(session, workspace_id, timeline_id)
    rows = []
    for i, seg in enumerate(segments or []):
        if isinstance(seg, dict):
            start, end, text = float(seg.get("start", 0.0)), float(seg.get("end", 0.0)), \
                str(seg.get("text", seg.get("hook", "")))
        else:
            (start, end), text = (float(seg[0]), float(seg[1])), ""
        if not end > start:
            continue
        rows.append(_insert(
            session, workspace_id=workspace_id, timeline_id=timeline_id,
            content_item_id=content_item_id, index=len(rows),
            title=f"Scene {len(rows) + 1} ({source})",
            narration=text[:2000], visual_intent=text[:500],
            start=start, end=end))
    session.flush()
    return rows


def attach_captions_to_scenes(session, *, workspace_id: str, timeline_id: str,
                              caption_clips: list[dict]) -> int:
    """Attach caption cues overlapping each scene into captions_json. Returns scenes touched."""
    from app.models.assets import Scene

    scenes = session.query(Scene).filter(
        Scene.workspace_id == workspace_id, Scene.timeline_id == timeline_id).all()
    touched = 0
    for sc in scenes:
        cues = [{"text": str(c.get("name", "")),
                 "start": float(c.get("start", 0.0)),
                 "end": float(c.get("start", 0.0)) + float(c.get("duration", 0.0))}
                for c in (caption_clips or [])
                if float(c.get("start", 0.0)) < sc.end_seconds
                and float(c.get("start", 0.0)) + float(c.get("duration", 0.0)) > sc.start_seconds]
        if cues:
            sc.captions_json = cues
            touched += 1
    session.flush()
    return touched


def scene_clip_map(session, *, workspace_id: str, timeline_id: str,
                   tracks_doc: dict) -> list[dict]:
    """Each scene with the ids of clips overlapping its range (per track kind)."""
    from app.models.assets import Scene

    scenes = session.query(Scene).filter(
        Scene.workspace_id == workspace_id, Scene.timeline_id == timeline_id
    ).order_by(Scene.index).all()
    out = []
    for sc in scenes:
        per_track: dict[str, list[str]] = {}
        for tr in (tracks_doc or {}).get("tracks", []):
            hits = [c.get("id") for c in tr.get("clips", [])
                    if float(c.get("start", 0.0)) < sc.end_seconds
                    and float(c.get("start", 0.0)) + float(c.get("duration", 0.0)) > sc.start_seconds]
            if hits:
                per_track[tr.get("kind")] = hits
        out.append({"scene_id": sc.id, "index": sc.index,
                    "start": sc.start_seconds, "end": sc.end_seconds,
                    "clips": per_track})
    return out


def resync_scene_ranges(session, *, workspace_id: str, timeline_id: str,
                        tracks_doc: dict) -> int:
    """After timeline edits, shrink/expand each scene to the union of its
    overlapping video/voice clips. Scenes with no overlapping clips keep
    their ranges. Returns scenes updated."""
    from app.models.assets import Scene

    scenes = session.query(Scene).filter(
        Scene.workspace_id == workspace_id, Scene.timeline_id == timeline_id).all()
    updated = 0
    for sc in scenes:
        bounds = [(float(c.get("start", 0.0)),
                   float(c.get("start", 0.0)) + float(c.get("duration", 0.0)))
                  for tr in (tracks_doc or {}).get("tracks", [])
                  if tr.get("kind") in ("video", "voice")
                  for c in tr.get("clips", [])
                  if float(c.get("start", 0.0)) < sc.end_seconds
                  and float(c.get("start", 0.0)) + float(c.get("duration", 0.0)) > sc.start_seconds]
        if not bounds:
            continue
        new_start, new_end = min(b[0] for b in bounds), max(b[1] for b in bounds)
        if (new_start, new_end) != (sc.start_seconds, sc.end_seconds):
            sc.start_seconds, sc.end_seconds = new_start, new_end
            updated += 1
    session.flush()
    return updated
