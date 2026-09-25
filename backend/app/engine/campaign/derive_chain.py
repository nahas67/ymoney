"""Real discovery/select/derive stages for the campaign.derive job.

Discovery reuses the repurpose ClipRepurposer (rank_moments with heuristic
fallback offline); selection uses the MMR diversity selector; derivation
uses derive_shorts with transcript segments + optimized hooks. No duplicate
clip detector, no duplicate timeline builder — one of each in the codebase.
"""

from __future__ import annotations


def _master_timeline(session, ws_id: str, master_content_id: str):
    from app.models import ContentTimeline

    return session.query(ContentTimeline).filter(
        ContentTimeline.content_item_id == master_content_id,
        ContentTimeline.workspace_id == ws_id,
    ).order_by(ContentTimeline.version.desc()).first()


def _master_segments(session, ws_id: str, master) -> tuple[list[dict], dict]:
    """Transcript segments + scene map from the master timeline + scenes.

    Captions carry the transcript; scenes carry chapter boundaries.
    Returns (segments, scenes_by_range) where each segment is
    {start, end, text, chapter, scene_ids}.
    """
    from app.models.assets import Scene

    doc = None
    if master is not None:
        tl = _master_timeline(session, ws_id, master.id)
        doc = (tl.tracks_json or {}) if tl else {}
    captions: list[dict] = []
    for track in (doc.get("tracks") or []):
        if track.get("kind") != "caption":
            continue
        for clip in track.get("clips", []):
            try:
                s, d = float(clip.get("start", 0.0)), float(clip.get("duration", 0.0))
            except (TypeError, ValueError):
                continue
            text = str(clip.get("name", "") or "")
            if d > 0 and text.strip():
                captions.append({"start": s, "end": s + d, "text": text})
    scenes = []
    if master is not None:
        scenes = session.query(Scene).filter(
            Scene.content_item_id == master.id,
            Scene.workspace_id == ws_id,
        ).order_by(Scene.index).all()
    scene_ranges = [
        {"id": sc.id, "chapter": sc.chapter_id or f"scene-{sc.index}",
         "start": float(sc.start_seconds or 0.0), "end": float(sc.end_seconds or 0.0)}
        for sc in scenes
    ]
    segments = []
    for cap in captions:
        mid = (cap["start"] + cap["end"]) / 2.0
        owner = next((r for r in scene_ranges if r["start"] <= mid < r["end"]), None)
        segments.append({
            "start": cap["start"], "end": cap["end"], "text": cap["text"],
            "chapter": (owner or {}).get("chapter", ""),
            "scene_ids": [owner["id"]] if owner else [],
        })
    return segments, {"scenes": scene_ranges}


def discover_moments(session, ws_id: str, master, max_moments: int) -> list[dict]:
    """Rank candidate clip moments from the master (repurposes rank_moments).

    Transcript comes from master caption clips; when the master has no
    captions, scene windows stand in (same fallback as LinkMiner.mine).
    Empty transcript AND no scenes means nothing to clip: returns [].
    """
    from app.providers.clips import get_repurposer

    segments, info = _master_segments(session, ws_id, master)
    if not segments:
        segments = [
            {"start": r["start"], "end": r["end"],
             "text": f"Segment {i + 1}",
             "chapter": r["chapter"], "scene_ids": [r["id"]]}
            for i, r in enumerate(info.get("scenes", []))
            if r["end"] > r["start"]
        ]
    if not segments:
        return []
    rep = get_repurposer()
    ranked = rep.rank_moments(segments, max_moments=max_moments, workspace_id=ws_id)
    out = []
    for m in ranked or []:
        mid = (float(m.start) + float(m.end)) / 2.0
        chapter = ""
        scene_ids: list[str] = []
        for seg in segments:
            if seg["start"] <= mid < seg["end"]:
                chapter = seg["chapter"]
                scene_ids = seg["scene_ids"]
                break
        out.append({
            "start": float(m.start), "end": float(m.end),
            "score": float(m.score), "hook": m.hook or "",
            "reason": m.reason or "", "text": m.text or "",
            "chapter": chapter, "scene_ids": scene_ids,
        })
    return out


def select_moments(session, campaign, moments: list[dict], n: int) -> list[dict]:
    """Diverse top-n via the MMR selector (never plain top-n).

    Already-derived ranges and topics are excluded so generate-more and
    resume never duplicate existing shorts.
    """
    from app.engine.campaign.diversity import select_diverse
    from app.models import ContentItem

    exclude: set[str] = set()
    if campaign is not None:
        existing = session.query(ContentItem).filter(
            ContentItem.workspace_id == campaign.workspace_id,
            ContentItem.campaign_id == campaign.id,
            ContentItem.derivation_type == "short",
        ).all()
        for row in existing:
            exclude.add(row.id)
            if row.topic:
                exclude.add(row.topic)
    selected = select_diverse(moments or [], n, exclude)
    # Intelligence advisory (Work 05, Lane A): shadow-only; MMR guards stay authoritative.
    try:
        from app.engine.intelligence.integrations import advise_diversity

        advise_diversity(list(moments or []), list(selected),
                         workspace_id=getattr(campaign, "workspace_id", ""))
    except Exception:
        pass
    return selected


def _covered_ranges(session, shorts: list) -> list[tuple[float, float]]:
    """Existing shorts' source ranges (from their Scene rows)."""
    from app.models.assets import Scene

    ranges: list[tuple[float, float]] = []
    for short in shorts:
        rows = session.query(Scene).filter(
            Scene.content_item_id == short.id).all()
        for sc in rows:
            try:
                s, e = float(sc.start_seconds or 0.0), float(sc.end_seconds or 0.0)
            except (TypeError, ValueError):
                continue
            if e > s:
                ranges.append((s, e))
    return ranges


def range_covered(moment: dict, covered: list[tuple[float, float]]) -> bool:
    """True when >50% of the moment overlaps already-derived ranges."""
    try:
        s, e = float(moment["start"]), float(moment["end"])
    except (TypeError, ValueError, KeyError):
        return True
    total = max(e - s, 0.0)
    if total <= 0:
        return True
    overlap = sum(max(0.0, min(e, cs_e) - max(s, cs_s)) for cs_s, cs_e in covered)
    return overlap / total > 0.5


def derive_moment(session, campaign, master, moment: dict, plan: dict):
    """Derive ONE short from one moment (hooks optimized, transcript attached)."""
    from app.engine.campaign.hooks import optimize_hook
    from app.engine.campaign.shorts import derive_shorts

    ws_id = campaign.workspace_id
    segments, _ = _master_segments(session, ws_id, master)
    in_range = [sg for sg in segments
                if sg["end"] > float(moment["start"]) and sg["start"] < float(moment["end"])]
    hooked = optimize_hook(str(moment.get("hook") or moment.get("text") or ""),
                           topic=master.topic if master else campaign.name)
    enriched = dict(moment)
    enriched["hook_text"] = hooked["optimized_hook"]
    enriched["hook_type"] = hooked["hook_type"]
    enriched["transcript"] = " ".join(sg["text"] for sg in in_range)[:2000]
    enriched["segments"] = in_range
    enriched["min_duration"] = 20.0
    enriched["topic"] = str(moment.get("topic") or moment.get("title")
                            or hooked["optimized_hook"] or f"short from {campaign.name}")[:400]
    (child,) = derive_shorts(session, ws_id, campaign.id,
                             master.id if master else "",
                             [enriched], plan)
    return (child,)
