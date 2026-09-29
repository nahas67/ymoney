"""Scene-level performance mapping (Work 06, Lane A).

Resolves the full attribution chain Publication → Variant → Short → Master →
Campaign (reusing the campaign analytics resolver — one resolver, no second
system) plus Timeline → Scene → Chapter, and answers which-scene-lost-viewers,
strongest-shorts-chapter, hook-3s-retention, rewatch scenes and
visual-pattern-vs-completion.

All correlations are reported with ``n`` and labeled correlational — no
causal claims, ever.
"""

from __future__ import annotations

from app.engine.performance.retention import HOOK_WINDOW_SECONDS


class ScenePerformanceMapper:
    """Map metrics and retention signals onto timeline regions."""

    def __init__(self, session, workspace_id: str) -> None:
        self._s = session
        self._ws = workspace_id

    # -- chain resolution -----------------------------------------------------

    def resolve_chain(self, post_id: str) -> dict:
        """Publication → Variant → Short → Master → Campaign."""
        from app.engine.campaign.analytics import resolve_attribution

        return resolve_attribution(self._s, self._ws, post_id)

    def resolve_timeline_scene(
        self, short_content_id: str, t_seconds: float
    ) -> dict:
        """Map a timestamp to timeline item / scene / chapter / hook / captions."""
        from sqlalchemy import select

        from app.models import ContentTimeline
        from app.models.assets import Scene
        from app.models.longform import LongFormChapter

        try:
            t = float(t_seconds)
        except (TypeError, ValueError):
            t = 0.0
        tl = self._s.scalar(select(ContentTimeline).where(
            ContentTimeline.workspace_id == self._ws,
            ContentTimeline.content_item_id == short_content_id,
        ).order_by(ContentTimeline.version.desc()))
        scenes = list(self._s.scalars(select(Scene).where(
            Scene.workspace_id == self._ws,
            Scene.content_item_id == short_content_id,
        ).order_by(Scene.index.asc())).all())
        scene = _scene_at(scenes, t)
        chapter_title = ""
        chapter_id = getattr(scene, "chapter_id", None) if scene else None
        if chapter_id:
            chapter = self._s.get(LongFormChapter, chapter_id)
            chapter_title = (chapter.title if chapter and chapter.title
                             else getattr(scene, "title", "") or chapter_id)
        doc = (tl.tracks_json or {}) if tl else {}
        captions = _captions_at(doc, t)
        beats = _beats_at(scene, t)
        return {
            "timeline_id": tl.id if tl else None,
            "t_seconds": round(t, 2),
            "scene_id": scene.id if scene else None,
            "scene_index": scene.index if scene else None,
            "scene_title": getattr(scene, "title", "") if scene else "",
            "chapter_id": chapter_id,
            "chapter_title": chapter_title,
            "is_hook": t <= HOOK_WINDOW_SECONDS,
            "caption_state": captions,
            "visual_beats": beats,
        }

    def region_for_metric(
        self, checkpoint: str, *, post_id: str | None = None,
        short_content_id: str | None = None, duration_seconds: float | None = None,
    ) -> dict:
        """Timeline region answering one retention checkpoint."""
        from app.engine.performance.retention import _standard_seconds

        short_id = short_content_id
        if short_id is None and post_id is not None:
            short_id = self._short_for_post(post_id)
        duration = duration_seconds
        if duration is None and short_id is not None:
            duration = self._short_duration(short_id)
        t = _standard_seconds(checkpoint, duration)
        if t is None or short_id is None:
            return {"checkpoint": checkpoint, "mapped": False,
                    "reason": "timestamp underivable without duration"}
        region = self.resolve_timeline_scene(short_id, t)
        region["checkpoint"] = checkpoint
        region["mapped"] = region.get("scene_id") is not None
        return region

    # -- answers --------------------------------------------------------------

    def scene_dropoff(self, short_content_id: str) -> list[dict]:
        """Per-scene retention deltas, worst loss first (which-scene-lost-viewers)."""
        from app.engine.performance.retention import STANDARD_CHECKPOINTS

        scenes = self._scene_retention(short_content_id)
        if not scenes:
            return []
        ordered = [c for c in STANDARD_CHECKPOINTS if any(
            c in s["points"] for s in scenes)]
        out: list[dict] = []
        for scene in scenes:
            pts = scene["points"]
            first = next((pts[c] for c in ordered if c in pts), None)
            last = next((pts[c] for c in reversed(ordered) if c in pts), None)
            loss = (first - last) if first is not None and last is not None else 0.0
            out.append({
                "scene_id": scene["scene_id"],
                "scene_index": scene["scene_index"],
                "scene_title": scene["scene_title"],
                "chapter_id": scene["chapter_id"],
                "loss": round(loss, 4),
                "points": pts,
            })
        return sorted(out, key=lambda r: r["loss"], reverse=True)

    def strongest_shorts_chapter(self, campaign_id: str) -> dict | None:
        """Highest-views chapter across campaign shorts (None when unmeasured)."""
        from app.engine.campaign.analytics import chapter_performance

        chapters = chapter_performance(self._s, self._ws, campaign_id)
        measured = [c for c in chapters if (c.get("totals") or {}).get("views", 0) > 0]
        if not measured:
            return None
        top = max(measured, key=lambda c: c["totals"]["views"])
        return {
            "chapter": top.get("chapter"),
            "chapter_title": top.get("chapter_title"),
            "views": top["totals"]["views"],
            "short_ids": top.get("short_ids") or [],
            "n_shorts": len(top.get("short_ids") or []),
        }

    def hook_3s_retention(
        self, *, post_id: str | None = None, short_content_id: str | None = None
    ) -> dict:
        """Retention inside the 3s hook window, or honest UNAVAILABLE."""
        from app.engine.performance.retention import RetentionAnalyzer

        analysis = RetentionAnalyzer(self._s, self._ws).analyze(
            post_id=post_id, short_content_id=short_content_id)
        if analysis["status"] != "AVAILABLE":
            return {"status": "UNAVAILABLE",
                    "reason": analysis.get("reason", "no retention data")}
        curve = analysis["curve"]
        value = curve.get("3s", curve.get("1s"))
        if value is None:
            return {"status": "UNAVAILABLE", "reason": "hook checkpoints missing"}
        verdict = ("strong" if value >= 0.7 else
                   "moderate" if value >= 0.5 else "weak")
        return {
            "status": "AVAILABLE",
            "retention_3s": round(float(value), 4),
            "verdict": verdict,
            "short_content_id": analysis.get("short_content_id"),
        }

    def rewatch_scenes(self, short_content_id: str) -> list[dict]:
        """Scenes containing rewatch maxima (empty when no curve exists)."""
        from sqlalchemy import select

        from app.models.performance import RetentionPoint

        rows = list(self._s.scalars(select(RetentionPoint).where(
            RetentionPoint.workspace_id == self._ws,
            RetentionPoint.short_content_id == short_content_id,
        )).all())
        if not rows:
            return []
        from app.engine.performance.retention import RetentionAnalyzer

        duration = self._short_duration(short_content_id)
        points: list[tuple[float, float]] = []
        for r in rows:
            t = _checkpoint_seconds(str(r.checkpoint or ""), duration)
            if t is None:
                continue
            try:
                points.append((t, float(r.value or 0.0)))
            except (TypeError, ValueError):
                continue
        points.sort()
        hits = RetentionAnalyzer.detect_rewatches(points)
        out = []
        for hit in hits:
            region = self.resolve_timeline_scene(short_content_id, hit["t"])
            region["rewatch_t"] = hit["t"]
            region["rewatch_value"] = hit["value"]
            out.append(region)
        return out

    def visual_pattern_vs_completion(self, campaign_id: str) -> dict:
        """Correlate observable visual patterns with completion (n reported).

        Features come from timelines + scenes (caption density, hook caption,
        b-roll presence, duration, chapter size). Correlational only — the
        payload says so explicitly.
        """
        from sqlalchemy import select

        from app.engine.campaign.analytics import campaign_items, latest_metrics
        from app.models import PublishedPost

        items = [i for i in campaign_items(self._s, self._ws, campaign_id)
                 if i.parent_content_id or i.derivation_type]
        if not items:
            return {"campaign_id": campaign_id, "n": 0,
                    "correlations": [], "causal": False,
                    "note": "no shorts; nothing to correlate"}
        ids = [i.id for i in items]
        posts = list(self._s.scalars(select(PublishedPost).where(
            PublishedPost.workspace_id == self._ws,
            PublishedPost.content_item_id.in_(ids))).all())
        by_short: dict[str, list] = {}
        for p in posts:
            by_short.setdefault(p.content_item_id or "", []).append(p)
        feats: list[dict] = []
        for item in items:
            group = by_short.get(item.id, [])
            metrics = latest_metrics(self._s, [p.id for p in group])
            vals = [metrics[p.id] for p in group if p.id in metrics]
            if not vals:
                continue
            views = sum(m.views or 0 for m in vals)
            completion = (sum((m.completion_rate or 0.0) * (m.views or 0)
                              for m in vals) / views) if views else 0.0
            feats.append({
                **self._visual_features(item),
                "completion": completion,
                "views": views,
            })
        targets = [f.pop("completion") for f in feats]
        correlations = []
        for key in ("caption_density", "has_hook_caption", "has_broll",
                    "duration_seconds"):
            xs = [float(f.get(key) or 0.0) for f in feats]
            r = _pearson(xs, targets)
            correlations.append({"feature": key, "r": r, "n": len(feats)})
        return {
            "campaign_id": campaign_id,
            "n": len(feats),
            "correlations": correlations,
            "causal": False,
            "note": "correlational only; do not treat as causal evidence",
        }

    # -- helpers --------------------------------------------------------------

    def _short_for_post(self, post_id: str) -> str | None:
        from app.models import PublishedPost

        post = self._s.get(PublishedPost, post_id)
        if post is None or post.workspace_id != self._ws:
            return None
        return post.content_item_id

    def _short_duration(self, short_id: str) -> float | None:
        from sqlalchemy import select

        from app.models import ContentTimeline
        from app.models.assets import Scene

        tl = self._s.scalar(select(ContentTimeline).where(
            ContentTimeline.workspace_id == self._ws,
            ContentTimeline.content_item_id == short_id,
        ).order_by(ContentTimeline.version.desc()))
        if tl is not None and (tl.duration_seconds or 0) > 0:
            return float(tl.duration_seconds)
        ends = self._s.scalars(select(Scene.end_seconds).where(
            Scene.workspace_id == self._ws,
            Scene.content_item_id == short_id)).all()
        try:
            return max(float(e or 0.0) for e in ends) or None
        except ValueError:
            return None

    def _scene_retention(self, short_id: str) -> list[dict]:
        from sqlalchemy import select

        from app.models.assets import Scene

        scenes = list(self._s.scalars(select(Scene).where(
            Scene.workspace_id == self._ws,
            Scene.content_item_id == short_id,
        ).order_by(Scene.index.asc())).all())
        out = []
        for scene in scenes:
            pts = dict((scene.performance_json or {}).get("retention") or {})
            out.append({
                "scene_id": scene.id,
                "scene_index": scene.index,
                "scene_title": scene.title or "",
                "chapter_id": scene.chapter_id,
                "points": {k: float(v) for k, v in pts.items()},
            })
        return [s for s in out if s["points"]]

    def _visual_features(self, item) -> dict:
        from sqlalchemy import select

        from app.models import ContentTimeline

        tl = self._s.scalar(select(ContentTimeline).where(
            ContentTimeline.workspace_id == self._ws,
            ContentTimeline.content_item_id == item.id,
        ).order_by(ContentTimeline.version.desc()))
        doc = (tl.tracks_json or {}) if tl else {}
        duration = float((tl.duration_seconds if tl else 0.0) or 0.0)
        captions = _track_clips(doc, "caption")
        texts = _track_clips(doc, "text")
        broll = _track_clips(doc, "broll")
        hook_cap = any(float(c.get("start", 0.0) or 0.0) <= HOOK_WINDOW_SECONDS
                       for c in captions)
        return {
            "caption_density": (len(captions) / duration) if duration > 0 else 0.0,
            "has_hook_caption": 1.0 if hook_cap else 0.0,
            "has_broll": 1.0 if broll else 0.0,
            "duration_seconds": duration,
            "n_text_overlays": float(len(texts)),
        }


def _scene_at(scenes: list, t: float):
    for scene in scenes:
        try:
            start, end = float(scene.start_seconds or 0.0), float(scene.end_seconds or 0.0)
        except (TypeError, ValueError):
            continue
        if start <= t < end:
            return scene
    # Boundary fallback: a timestamp exactly on a shared edge belongs to the
    # scene that starts there; only when none does, take the ending scene.
    for scene in scenes:
        try:
            start = float(scene.start_seconds or 0.0)
        except (TypeError, ValueError):
            continue
        if t == start:
            return scene
    for scene in scenes:
        try:
            end = float(scene.end_seconds or 0.0)
        except (TypeError, ValueError):
            continue
        if t == end:
            return scene
    if scenes:
        try:
            return min(scenes,
                       key=lambda s: abs((float(s.start_seconds or 0.0)
                                          + float(s.end_seconds or 0.0)) / 2.0 - t))
        except (TypeError, ValueError):
            return scenes[0]
    return None


def _track_clips(doc: dict, kind: str) -> list[dict]:
    for track in doc.get("tracks") or []:
        if track.get("kind") == kind:
            return list(track.get("clips") or [])
    return []


def _captions_at(doc: dict, t: float) -> dict:
    active = []
    for clip in _track_clips(doc, "caption"):
        try:
            start = float(clip.get("start", 0.0))
            dur = float(clip.get("duration", 0.0))
        except (TypeError, ValueError):
            continue
        if start <= t < start + dur:
            active.append(str(clip.get("name", "") or "")[:120])
    return {"active": bool(active), "count": len(active), "text": active[:3]}


def _beats_at(scene, t: float) -> list:
    beats = list(getattr(scene, "beats_json", None) or []) if scene else []
    hits = []
    for beat in beats[:20]:
        if not isinstance(beat, dict):
            continue
        try:
            start = float(beat.get("start", beat.get("t", 0.0)))
            end = float(beat.get("end", start))
        except (TypeError, ValueError):
            continue
        if start <= t <= max(end, start):
            hits.append({k: beat.get(k) for k in list(beat)[:6]})
        if len(hits) >= 3:
            break
    return hits


def _checkpoint_seconds(checkpoint: str, duration: float | None) -> float | None:
    """One stored checkpoint → seconds (None when underivable)."""
    text = (checkpoint or "").strip().lower()
    try:
        if text.endswith("%") and duration:
            return float(text[:-1]) / 100.0 * float(duration)
        if text.endswith("s"):
            return float(text[:-1])
        return float(text)
    except (TypeError, ValueError):
        return None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    if den == 0:
        return None
    return round(num / den, 4)


__all__ = ["ScenePerformanceMapper"]
