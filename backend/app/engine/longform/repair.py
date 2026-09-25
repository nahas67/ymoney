"""Bounded automatic repair for long-form timelines (Work 03).

One pass only, never a loop: unresolvable VISUAL clips get a deterministic
graphic fallback (recorded); unresolvable VOICE clips cannot be repaired
offline and fail loudly instead of receiving silent filler. After repair the
chunk hashes change, so the next render naturally re-renders only affected
chunks. Voice/music/sfx gaps are QC failures, never auto-patched.
"""

from __future__ import annotations


def repair_timeline_assets(session, project) -> dict:
    """Single repair pass. Returns {repaired, unrepairable, details}."""
    from app.models import ContentTimeline
    from app.providers.video_engine.timeline_render import resolve_clip_source

    row = session.get(ContentTimeline, project.timeline_id) if project.timeline_id else None
    if row is None:
        raise ValueError("no timeline — run TIMELINE first")
    import copy as _copy

    # deepcopy: SQLAlchemy does not reliably persist in-place mutations of
    # nested JSON structures (verified: shallow copies silently lose writes)
    doc = _copy.deepcopy(row.tracks_json or {})
    repaired, unrepairable, details = [], [], []
    for tr in doc.get("tracks", []):
        kind = tr.get("kind")
        for clip in tr.get("clips", []):
            source = clip.get("source") or {}
            if not source.get("asset_id") and not source.get("video_id"):
                continue  # captions/text/titles need no files
            if resolve_clip_source(project.workspace_id, session, source) is not None:
                continue
            if kind in ("video", "broll", "avatar"):
                asset_id = _graphic_fallback(session, project, clip)
                clip["source"] = {"asset_id": asset_id}
                repaired.append(clip["id"])
                details.append(f"{clip['id']}: replaced missing media with graphic {asset_id[:8]}")
            else:
                unrepairable.append(clip["id"])
                details.append(f"{clip['id']}: missing narration/audio cannot be auto-repaired")
    if repaired:
        from app.engine.timeline import validate_timeline

        validate_timeline(doc)
        row.tracks_json = doc
        row.version = int(row.version or 1) + 1
    session.flush()
    return {"repaired": repaired, "unrepairable": unrepairable, "details": details}


def _graphic_fallback(session, project, clip) -> str:
    from app.providers.longform_assets import GraphicProvider

    fake_scene = type("S", (), {"index": 0, "title": clip.get("name", "repair"),
                                "visual_intent": clip.get("name", "")})()
    asset = GraphicProvider().fetch(session, project, fake_scene, {"index": 0})
    return asset.id
