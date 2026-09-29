"""Audience-retention analysis (Work 06, Lane A).

Providers ship no granular retention data today, so :class:`RetentionAnalyzer`
reports ``UNAVAILABLE`` honestly whenever no retention points exist — a coarse
proxy from :class:`PostMetric` averages is offered instead, clearly labeled,
never presented as a real curve.

Inputs accepted by :meth:`RetentionAnalyzer.ingest`:

- ``points``: ``{checkpoint: value}`` where a checkpoint is ``1s``/``3s``/
  ``25%``/``50%``/``75%``/``100%`` or raw seconds (``12.5``).
- ``curve``: full curves ``[{t, v}]`` (or ``(t, v)`` tuples); standard
  checkpoints are interpolated where the curve covers them.

Every analyzed point is mapped to its timeline item / scene / chapter /
hook-window / caption-state / visual-beat via Scene rows + ContentTimeline,
and that mapping is persisted (Scene ``performance_json`` + a
``performance_observations`` evidence row).
"""

from __future__ import annotations

HOOK_WINDOW_SECONDS = 3.0
DROP_THRESHOLD = 0.15  # absolute fraction lost between consecutive checkpoints
SLOPE_THRESHOLD = 0.03  # fraction lost per second on raw curves
REWATCH_EPS = 0.02  # local-maximum prominence for rewatch detection

STANDARD_CHECKPOINTS = ("1s", "3s", "25%", "50%", "75%", "100%")


def _clamp01(value: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _parse_checkpoint(key: str) -> tuple[str, float] | None:
    """Parse a checkpoint key → ("seconds", t) | ("percent", p) | None."""
    text = str(key or "").strip().lower().replace(" ", "")
    if not text:
        return None
    try:
        if text.endswith("%"):
            return ("percent", float(text[:-1]))
        if text.endswith("s"):
            return ("seconds", float(text[:-1]))
        return ("seconds", float(text))
    except ValueError:
        return None


def _standard_seconds(checkpoint: str, duration: float | None) -> float | None:
    """Convert a standard checkpoint to seconds; None when underivable."""
    if checkpoint == "1s":
        return 1.0
    if checkpoint == "3s":
        return 3.0
    if duration and duration > 0 and checkpoint.endswith("%"):
        try:
            return float(checkpoint[:-1]) / 100.0 * duration
        except ValueError:
            return None
    return None


class RetentionAnalyzer:
    """Ingest, normalize, map and analyze audience retention."""

    def __init__(self, session, workspace_id: str) -> None:
        self._s = session
        self._ws = workspace_id

    # -- normalization (pure) -------------------------------------------------

    @staticmethod
    def normalize(
        points: dict | None = None,
        curve: list | None = None,
        duration_seconds: float | None = None,
    ) -> dict:
        """Normalize inputs to ``{checkpoint: value}`` over standard checkpoints.

        Returns ``{"curve": {...}, "missing": [...], "duration": d}``. Only
        checkpoints derivable from the inputs appear in ``curve``; the rest
        are listed in ``missing`` (never interpolated from nothing).
        """
        by_seconds: dict[float, float] = {}
        by_percent: dict[float, float] = {}
        for key, val in (points or {}).items():
            parsed = _parse_checkpoint(key)
            if parsed is None:
                continue
            kind, num = parsed
            if kind == "seconds":
                by_seconds[num] = _clamp01(val)
            else:
                by_percent[num] = _clamp01(val)
        raw = _coerce_curve(curve)
        for t, v in raw:
            by_seconds[t] = _clamp01(v)
        duration = None
        try:
            duration = float(duration_seconds) if duration_seconds else None
        except (TypeError, ValueError):
            duration = None
        if (duration is None or duration <= 0) and raw:
            duration = max(t for t, _ in raw) or None
        if duration is not None and duration > 0:
            for t, v in by_seconds.items():
                pct = t / duration * 100.0
                by_percent.setdefault(pct, v)

        out: dict[str, float] = {}
        missing: list[str] = []
        for cp in STANDARD_CHECKPOINTS:
            resolved = _resolve_checkpoint(cp, by_seconds, by_percent, duration)
            if resolved is None:
                missing.append(cp)
            else:
                out[cp] = round(resolved, 4)
        return {"curve": out, "missing": missing, "duration": duration}

    # -- ingestion ------------------------------------------------------------

    def ingest(
        self,
        *,
        post_id: str | None = None,
        short_content_id: str | None = None,
        campaign_id: str | None = None,
        points: dict | None = None,
        curve: list | None = None,
        source: str = "provider",
        duration_seconds: float | None = None,
    ) -> dict:
        """Persist retention points + their timeline mapping. Returns analysis."""
        from app.models.base import utcnow
        from app.models.performance import RetentionPoint

        short_id = short_content_id or self._short_for_post(post_id)
        duration = duration_seconds or self._short_duration(short_id)
        norm = self.normalize(points, curve, duration)
        now = utcnow()
        for checkpoint, value in norm["curve"].items():
            self._s.add(RetentionPoint(
                workspace_id=self._ws, post_id=post_id,
                campaign_id=campaign_id, short_content_id=short_id,
                checkpoint=checkpoint, value=value, source=source,
                captured_at=now,
            ))
        self._s.flush()
        mapping = self._map_curve(short_id, norm["curve"], duration)
        self._persist_mapping(short_id, post_id, norm, mapping, source)
        return {
            "status": "AVAILABLE",
            "curve": norm["curve"],
            "missing": norm["missing"],
            "mapping": mapping,
            "drops": self.detect_drops(norm["curve"], _coerce_curve(curve)),
            "rewatches": self.detect_rewatches(_coerce_curve(curve)),
            "source": source,
        }

    # -- analysis -------------------------------------------------------------

    def analyze(
        self, *, post_id: str | None = None, short_content_id: str | None = None
    ) -> dict:
        """Curve + mapping + drops for a post/short, or honest UNAVAILABLE."""
        from sqlalchemy import select

        from app.models.performance import RetentionPoint

        short_id = short_content_id or self._short_for_post(post_id)
        post_id = post_id or self._post_for_short(short_id)
        rows: list = []
        if post_id or short_id:
            q = select(RetentionPoint).where(
                RetentionPoint.workspace_id == self._ws)
            if post_id:
                q = q.where(RetentionPoint.post_id == post_id)
            elif short_id:
                q = q.where(RetentionPoint.short_content_id == short_id)
            rows = list(self._s.scalars(
                q.order_by(RetentionPoint.captured_at.desc())).all())
        latest: dict[str, float] = {}
        for row in rows:  # newest first → first sample per checkpoint wins
            latest.setdefault(row.checkpoint, float(row.value or 0.0))
        if not latest:
            return {
                "status": "UNAVAILABLE",
                "reason": "no granular retention data for this platform",
                "curve": {},
                "mapping": {},
                "drops": [],
                "rewatches": [],
                "coarse_proxy": self._coarse_proxy(post_id),
                "post_id": post_id,
                "short_content_id": short_id,
            }
        duration = self._short_duration(short_id)
        mapping = self._map_curve(short_id, latest, duration)
        return {
            "status": "AVAILABLE",
            "curve": latest,
            "missing": [c for c in STANDARD_CHECKPOINTS if c not in latest],
            "mapping": mapping,
            "drops": self.detect_drops(latest, None),
            "rewatches": [],
            "source": (rows[0].source if rows else ""),
            "post_id": post_id,
            "short_content_id": short_id,
        }

    # -- detection (pure) -----------------------------------------------------

    @staticmethod
    def detect_drops(
        curve: dict, raw_curve: list[tuple[float, float]] | None
    ) -> list[dict]:
        """Flag audience losses: threshold drops + steep slopes on raw curves."""
        drops: list[dict] = []
        ordered = [c for c in STANDARD_CHECKPOINTS if c in (curve or {})]
        for prev, cur in zip(ordered, ordered[1:]):
            loss = float(curve[prev]) - float(curve[cur])
            if loss >= DROP_THRESHOLD:
                drops.append({
                    "type": "threshold_drop",
                    "from": prev, "to": cur,
                    "loss": round(loss, 4),
                })
        for (t0, v0), (t1, v1) in zip(raw_curve or [], (raw_curve or [])[1:]):
            dt = t1 - t0
            if dt > 0 and (v0 - v1) / dt >= SLOPE_THRESHOLD:
                drops.append({
                    "type": "steep_slope",
                    "from_t": round(t0, 2), "to_t": round(t1, 2),
                    "loss": round(v0 - v1, 4),
                })
                break  # first steep segment is the story; avoid noise spam
        return drops

    @staticmethod
    def detect_rewatches(raw_curve: list[tuple[float, float]] | None) -> list[dict]:
        """Local maxima on raw curves (viewers scrubbing back)."""
        out: list[dict] = []
        pts = list(raw_curve or [])
        for i in range(1, len(pts) - 1):
            _, prev = pts[i - 1]
            t, v = pts[i]
            _, nxt = pts[i + 1]
            if v - prev >= REWATCH_EPS and v - nxt >= REWATCH_EPS:
                out.append({"t": round(t, 2), "value": round(v, 4)})
        return out

    # -- internals ------------------------------------------------------------

    def _short_for_post(self, post_id: str | None) -> str | None:
        if not post_id:
            return None
        from app.models import PublishedPost

        post = self._s.get(PublishedPost, post_id)
        if post is None or post.workspace_id != self._ws:
            return None
        return post.content_item_id

    def _post_for_short(self, short_id: str | None) -> str | None:
        if not short_id:
            return None
        from sqlalchemy import select

        from app.models import PublishedPost

        return self._s.scalar(select(PublishedPost.id).where(
            PublishedPost.workspace_id == self._ws,
            PublishedPost.content_item_id == short_id,
        ).order_by(PublishedPost.created_at.desc()))

    def _short_duration(self, short_id: str | None) -> float | None:
        if not short_id:
            return None
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

    def _map_curve(
        self, short_id: str | None, curve: dict, duration: float | None
    ) -> dict:
        """Map each checkpoint → scene/chapter/hook/caption/visual-beat."""
        from app.engine.performance.mapper import ScenePerformanceMapper

        mapper = ScenePerformanceMapper(self._s, self._ws)
        mapping: dict[str, dict] = {}
        for checkpoint, value in (curve or {}).items():
            t = _standard_seconds(checkpoint, duration)
            if t is None or short_id is None:
                mapping[checkpoint] = {"value": value, "mapped": False}
                continue
            region = mapper.resolve_timeline_scene(short_id, t)
            region["value"] = value
            region["mapped"] = region.get("scene_id") is not None
            mapping[checkpoint] = region
        return mapping

    def _persist_mapping(
        self, short_id: str | None, post_id: str | None,
        norm: dict, mapping: dict, source: str,
    ) -> None:

        from app.models.assets import Scene
        from app.models.base import utcnow
        from app.models.performance import PerformanceObservation

        if short_id:
            per_scene: dict[str, dict] = {}
            for checkpoint, region in mapping.items():
                sid = region.get("scene_id")
                if sid:
                    per_scene.setdefault(sid, {})[checkpoint] = region.get("value")
            for sid, points in per_scene.items():
                scene = self._s.get(Scene, sid)
                if scene is None or scene.workspace_id != self._ws:
                    continue
                perf = dict(scene.performance_json or {})
                merged = dict(perf.get("retention") or {})
                merged.update(points)
                perf["retention"] = merged
                scene.performance_json = perf
            completion = norm["curve"].get("100%")
            self._s.add(PerformanceObservation(
                workspace_id=self._ws, subject_type="short",
                subject_id=short_id, metric="retention_curve",
                value=float(completion or 0.0),
                scope_json={"checkpoints": sorted(norm["curve"]),
                            "missing": norm["missing"]},
                evidence_json={"curve": norm["curve"], "mapping": mapping,
                               "source": source, "post_id": post_id or ""},
                observed_at=utcnow(),
            ))
        self._s.flush()

    def _coarse_proxy(self, post_id: str | None) -> dict:
        """Clearly-labeled coarse proxy from averages — never a real curve."""
        if not post_id:
            return {"label": "coarse_proxy", "available": False}
        from app.models import PostMetric

        rows = self._s.query(PostMetric).filter(
            PostMetric.post_id == post_id).order_by(
            PostMetric.captured_at.desc()).all()
        metric = next((m for m in rows if m is not None), None)
        if metric is None:
            return {"label": "coarse_proxy", "available": False}
        return {
            "label": "coarse_proxy",
            "available": True,
            "avg_view_duration_seconds": float(
                metric.avg_view_duration_seconds or 0.0),
            "completion_rate": float(metric.completion_rate or 0.0),
            "note": "derived from averages only; no per-second retention exists",
        }


def _coerce_curve(curve: list | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for item in curve or []:
        try:
            if isinstance(item, dict):
                t, v = item.get("t"), item.get("v")
            else:
                t, v = item[0], item[1]
            out.append((float(t), _clamp01(v)))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return sorted(out)


def _resolve_checkpoint(
    checkpoint: str,
    by_seconds: dict[float, float],
    by_percent: dict[float, float],
    duration: float | None,
) -> float | None:
    """Exact sample → bracketing interpolation → None (never invented)."""
    target_pct = {"25%": 25.0, "50%": 50.0, "75%": 75.0, "100%": 100.0}.get(checkpoint)
    if target_pct is not None and target_pct in by_percent:
        return by_percent[target_pct]
    target_t = _standard_seconds(checkpoint, duration)
    if target_t is not None and target_t in by_seconds:
        return by_seconds[target_t]
    pool = (by_percent if target_pct is not None else by_seconds,
            target_pct if target_pct is not None else target_t)
    return _interpolate(pool[0], pool[1])


def _interpolate(samples: dict[float, float], target: float | None) -> float | None:
    if target is None or not samples:
        return None
    keys = sorted(samples)
    if target <= keys[0] and abs(target - keys[0]) < 1e-9:
        return samples[keys[0]]
    if target >= keys[-1] and abs(target - keys[-1]) < 1e-9:
        return samples[keys[-1]]
    for lo, hi in zip(keys, keys[1:]):
        if lo <= target <= hi:
            if hi == lo:
                return samples[lo]
            frac = (target - lo) / (hi - lo)
            return samples[lo] + frac * (samples[hi] - samples[lo])
    return None


__all__ = [
    "DROP_THRESHOLD",
    "HOOK_WINDOW_SECONDS",
    "REWATCH_EPS",
    "SLOPE_THRESHOLD",
    "STANDARD_CHECKPOINTS",
    "RetentionAnalyzer",
]
