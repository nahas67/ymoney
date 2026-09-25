"""Master -> shorts timeline derivation (Work 04 Lane A).

Slices the master canonical timeline into vertical short timelines without
mutating the master doc, then persists ContentItem + ContentTimeline + Scene
rows with full lineage (parent=root=master, campaign_id).
"""

from __future__ import annotations

import copy

from app.engine.content_graph import derive_content
from app.engine.timeline import create_empty, validate_timeline

SHORT_ASPECT = "9:16"
HOOK_WINDOW_SECONDS = 3.0


def _slice_clips(master_doc: dict, kind: str, start: float, end: float) -> list[dict]:
    """Master clips of one track kind intersecting [start, end], rebased to 0."""
    out: list[dict] = []
    for track in (master_doc.get("tracks") or []):
        if track.get("kind") != kind:
            continue
        for clip in track.get("clips", []):
            try:
                s = float(clip.get("start", 0.0))
                d = float(clip.get("duration", 0.0))
            except (TypeError, ValueError):
                continue
            if d <= 0 or s + d <= start or s >= end:
                continue
            offset = max(s, start)
            duration = min(s + d, end) - offset
            if duration <= 0:
                continue
            sliced = copy.deepcopy(clip)
            sliced["id"] = f"{clip.get('id', 'clip')}_short"
            sliced["start"] = offset - start
            sliced["duration"] = duration
            try:
                sliced["source_start"] = float(clip.get("source_start", 0.0)) + (offset - s)
            except (TypeError, ValueError):
                sliced["source_start"] = offset - s
            source = dict(sliced.get("source") or {})
            source["master_range"] = {"start": start, "end": end}
            sliced["source"] = source
            out.append(sliced)
    out.sort(key=lambda c: c["start"])
    return out


def build_short_timeline(
    session,
    ws,
    master_timeline_doc: dict,
    start: float,
    end: float,
    hook_text: str = "",
    cta_text: str = "",
    caption_style: str = "karaoke",
) -> dict:
    """Build a validated 9:16 short doc covering master range [start, end].

    Video track: sliced master video clips (or a single master-range clip
    when the master has no video coverage there). Caption clips: sliced
    master captions, falling back to voice-track lines, then to a hook
    caption. Text track: hook opener + CTA closer. Full-precision times —
    never rounded.
    """
    _ = session  # signature reserves the session for future asset lookups
    if master_timeline_doc is None:
        raise ValueError("master_timeline_doc is required")
    start = float(start)
    end = float(end)
    if end <= start:
        raise ValueError(f"invalid short range [{start}, {end}]")
    ws_id = getattr(ws, "id", ws)
    master = copy.deepcopy(master_timeline_doc)  # never mutate the master doc

    duration = end - start
    fps = master.get("fps", 30.0)
    try:
        fps = float(fps or 30.0)
    except (TypeError, ValueError):
        fps = 30.0
    doc = create_empty(ws_id, duration_seconds=duration, aspect=SHORT_ASPECT)
    doc["fps"] = fps
    by_kind = {t["kind"]: t for t in doc.get("tracks", [])}

    video_clips = _slice_clips(master, "video", start, end)
    if not video_clips:
        video_clips = [{
            "id": "src_master_short", "name": "master range",
            "start": 0.0, "duration": duration,
            "source": {"master_range": {"start": start, "end": end}},
            "effects": [], "source_start": start,
            "volume": 1.0, "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0,
            "transform": {}, "text": {},
            "transition_in": "cut", "transition_out": "cut",
        }]
    by_kind["video"]["clips"] = video_clips

    caption_clips = _slice_clips(master, "caption", start, end)
    if not caption_clips:
        caption_clips = _slice_clips(master, "voice", start, end)
        for clip in caption_clips:
            clip["id"] = clip["id"].replace("_short", "_cap_short")
    if not caption_clips and (hook_text or "").strip():
        caption_clips = [{
            "id": "cap_hook_fallback", "name": (hook_text or "").strip()[:200],
            "start": 0.0, "duration": min(HOOK_WINDOW_SECONDS, duration),
            "source": {"master_range": {"start": start, "end": end}, "synthetic": True},
            "effects": [caption_style], "source_start": 0.0,
            "volume": 1.0, "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0,
            "transform": {}, "text": {"content": (hook_text or "").strip()[:200]},
            "transition_in": "cut", "transition_out": "cut",
        }]
    by_kind["caption"]["clips"] = caption_clips

    # Text track: hook opener + CTA closer, non-overlapping by construction.
    text_clips: list[dict] = []
    hook = (hook_text or "").strip()
    cta = (cta_text or "").strip()
    if hook and cta and duration < 2 * HOOK_WINDOW_SECONDS:
        hook_dur = duration / 2.0
        cta_start = duration / 2.0
        cta_dur = duration - cta_start
    else:
        hook_dur = min(HOOK_WINDOW_SECONDS, duration)
        cta_dur = min(HOOK_WINDOW_SECONDS, duration)
        cta_start = max(0.0, duration - cta_dur)
    if hook:
        text_clips.append({
            "id": "txt_hook", "name": f"hook: {hook[:120]}",
            "start": 0.0, "duration": hook_dur,
            "source": {"role": "hook", "master_range": {"start": start, "end": end}},
            "effects": [], "source_start": 0.0,
            "volume": 1.0, "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0,
            "transform": {}, "text": {"content": hook[:300]},
            "transition_in": "cut", "transition_out": "cut",
        })
    if cta and cta_start + cta_dur > (hook_dur if hook else 0.0):
        cta_start = max(cta_start, hook_dur if hook else 0.0)
        cta_dur = max(0.5, duration - cta_start) if duration > cta_start else 0.0
        if cta_dur > 0:
            text_clips.append({
                "id": "txt_cta", "name": f"cta: {cta[:120]}",
                "start": cta_start, "duration": cta_dur,
                "source": {"role": "cta", "master_range": {"start": start, "end": end}},
                "effects": [], "source_start": 0.0,
                "volume": 1.0, "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                "transform": {}, "text": {"content": cta[:300]},
                "transition_in": "cut", "transition_out": "cut",
            })
    by_kind["text"]["clips"] = text_clips

    doc["duration_seconds"] = duration
    doc["aspect_ratio"] = SHORT_ASPECT
    doc["campaign"] = {
        "master_range": {"start": start, "end": end},
        "hook_text": hook[:300],
        "cta_text": cta[:300],
        "caption_style": caption_style,
    }
    return validate_timeline(doc)


def derive_shorts(
    session,
    ws,
    campaign_id: str,
    master_content_id: str,
    moments: list[dict],
    plan=None,
) -> list:
    """Persist one short per moment. Returns the new ContentItem rows.

    moments: [{start, end, topic|title, hook_text|hook, cta_text|cta,
    transcript|text, segments? (repaired), caption_style?}].
    """
    from app.engine.campaign.repair import ClipContextRepair
    from app.models import ContentItem, ContentTimeline, Scene

    ws_id = getattr(ws, "id", ws)
    master = session.get(ContentItem, master_content_id)
    if master is None or master.workspace_id != ws_id:
        raise ValueError(f"master content '{master_content_id}' not found")
    if not moments:
        raise ValueError("at least one moment is required")

    master_timeline = session.query(ContentTimeline).filter(
        ContentTimeline.content_item_id == master_content_id,
        ContentTimeline.workspace_id == ws_id,
    ).order_by(ContentTimeline.version.desc()).first()
    if master_timeline is None:
        raise ValueError(f"master content '{master_content_id}' has no timeline")
    master_doc = master_timeline.tracks_json or {}
    default_style = "karaoke"
    if plan is not None and getattr(plan, "diversity_config", None):
        default_style = (plan.diversity_config or {}).get("caption_style", "karaoke")

    master_scenes = session.query(Scene).filter(
        Scene.content_item_id == master_content_id,
        Scene.workspace_id == ws_id,
    ).all()

    repairer = ClipContextRepair()
    created: list = []
    for i, moment in enumerate(moments):
        start = float(moment["start"])
        end = float(moment["end"])
        hook_text = str(moment.get("hook_text") or moment.get("hook") or "")
        cta_text = str(moment.get("cta_text") or moment.get("cta") or "")
        topic = str(
            moment.get("topic") or moment.get("title") or moment.get("hook") or f"short {i + 1}"
        )[:400]
        repaired_text = str(moment.get("transcript") or moment.get("text") or "")
        segments = moment.get("segments")
        if segments:
            repaired = repairer.repair(
                start=start, end=end, segments=segments,
                hook_text=hook_text, cta_text=cta_text,
            )
            start, end = float(repaired["start"]), float(repaired["end"])
            if repaired["text"]:
                repaired_text = repaired["text"]
        try:
            minimum = float(moment.get("min_duration") or 0.0)
        except (TypeError, ValueError):
            minimum = 0.0
        if minimum > 0 and end - start < minimum:
            # campaign floor: extend the ending within master bounds
            # (context repair, never fabricated footage)
            try:
                cap = float((master_doc or {}).get("duration_seconds") or end)
            except (TypeError, ValueError):
                cap = end
            end = max(end, min(start + minimum, cap))

        doc = build_short_timeline(
            session, ws_id, master_doc, start, end,
            hook_text=hook_text, cta_text=cta_text,
            caption_style=str(moment.get("caption_style") or default_style),
        )
        child = derive_content(
            session, parent_id=master.id, workspace_id=ws_id,
            derivation_type="short", topic=topic, campaign_id=campaign_id,
        )
        timeline_row = ContentTimeline(
            workspace_id=ws_id, content_item_id=child.id,
            name=str(moment.get("title") or f"short-{i + 1}")[:200],
            fps=float(doc.get("fps", 30.0)),
            duration_seconds=float(doc.get("duration_seconds", 0.0)),
            tracks_json=doc, version=1,
        )
        session.add(timeline_row)
        session.flush()

        parent_scene = _best_parent_scene(master_scenes, start, end)
        scene = Scene(
            workspace_id=ws_id, content_item_id=child.id,
            timeline_id=timeline_row.id, index=i, title=topic[:200],
            chapter_id=getattr(parent_scene, "chapter_id", None),
            script_segment=repaired_text[:2000],
            start_seconds=start, end_seconds=end,
            captions_json=[c.get("name", "") for c in _track_clips(doc, "caption")][:20],
            parent_scene_id=parent_scene.id if parent_scene is not None else None,
        )
        session.add(scene)
        session.flush()
        created.append(child)

    if plan is not None and hasattr(plan, "progress_json"):
        progress = dict(plan.progress_json or {})
        progress["shorts_derived"] = len(created)
        plan.progress_json = progress
        session.flush()
    return created


def _track_clips(doc: dict, kind: str) -> list[dict]:
    for track in doc.get("tracks", []):
        if track.get("kind") == kind:
            return list(track.get("clips", []))
    return []


def _best_parent_scene(master_scenes: list, start: float, end: float):
    """Master scene with the largest overlap of [start, end], if any."""
    best = None
    best_overlap = 0.0
    for scene in master_scenes:
        try:
            s = float(scene.start_seconds)
            e = float(scene.end_seconds)
        except (TypeError, ValueError):
            continue
        overlap = max(0.0, min(e, end) - max(s, start))
        if overlap > best_overlap:
            best_overlap = overlap
            best = scene
    return best
