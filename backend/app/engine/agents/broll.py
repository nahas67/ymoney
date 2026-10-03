"""B-roll Researcher agent (E4): per-scene visual sourcing.

Plans each scene's visual (stock query + AI prompt, alternating lanes for
variety), then fetches stock clips and generates AI clips on demand. Stock is
the CPU default; AI lanes are probe-gated and fail closed.
"""

from __future__ import annotations

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.broll import (
    BrollError,
    fetch_stock_clip,
    generate_clip,
    plan_scenes,
    search_stock,
)


def _maybe_sync_plan(ctx, plan: list) -> list[str]:
    """Adapter: when the job carries a timeline_id, persist the visual plan as
    Scene rows (even split across the timeline duration). Best-effort — the
    pipeline result never depends on it."""
    from loguru import logger

    from app.db import session_scope

    try:
        payload = getattr(ctx, "payload", None) or {}
        timeline_id = payload.get("timeline_id")
        if not timeline_id or not ctx.workspace_id:
            return []
        from app.engine import scene_sync as sync_mod
        from app.models import ContentTimeline

        with session_scope() as s:
            row = s.get(ContentTimeline, timeline_id)
            if row is None or row.workspace_id != ctx.workspace_id:
                return []
            rows = sync_mod.sync_from_broll_plan(
                s, workspace_id=ctx.workspace_id, timeline_id=timeline_id,
                content_item_id=row.content_item_id,
                plan=[{"prompt": p.prompt} for p in plan],
                total_duration=row.duration_seconds or 0.0)
            s.commit()
            return [r.id for r in rows]
    except Exception as exc:  # noqa: BLE001 — sync must never break planning
        logger.debug(f"[broll] scene sync skipped: {type(exc).__name__}")
        return []


class BrollResearcherAgent(BaseAgent):
    meta = AgentMeta(
        key="broll_researcher",
        title="B-roll Researcher",
        description="Sources per-scene visuals from stock and AI lanes.",
        skills=("broll_research",),
        tools=("fetch_broll",),
        permissions=("media:render",),
    )

    def research(self, ctx, *, topic: str, keywords: list[str] | None = None,
                 n_scenes: int = 4) -> dict:
        """Produce a per-scene visual plan (no bytes moved)."""

        def work():
            self.step("plan_scenes", f"{n_scenes} scene(s) for '{topic[:60]}'")
            # Performance lessons (Work 06 Lane C): scope-matched queries bias
            # a keyword copy when `learning_assist` is enabled (default off).
            lesson_keys: list[str] = []
            lesson_recs: list[dict] = []
            kw_list = list(keywords or [])
            try:
                from app.engine.performance import learning as _lessons

                kw_list, lesson_keys, lesson_recs = _lessons.broll_keyword_boost(
                    ctx.workspace_id or "", topic, kw_list)
            except Exception:
                kw_list, lesson_keys, lesson_recs = list(keywords or []), [], []
            plan = plan_scenes(topic, kw_list, n_scenes, ctx.workspace_id or "")
            self.step_done("ok", f"{len(plan)} scene(s)")
            scene_sync_ids = _maybe_sync_plan(ctx, plan)
            return {
                "summary": f"planned {len(plan)} scene visual(s) for '{topic[:60]}'",
                "scenes": [
                    {"index": s.index, "query": s.query, "prompt": s.prompt,
                     "source": s.source, "license": s.license}
                    for s in plan
                ],
                "synced_scene_ids": scene_sync_ids,
                "applied_lessons": lesson_keys,
                "lesson_recommendations": lesson_recs,
            }

        return self.execute(ctx, "plan_visuals", input_summary=topic[:200], fn=work)

    def fetch(self, ctx, *, query: str = "", video_id: str = "",
              prompt: str = "", seconds: float = 4.0,
              aspect: str = "9:16") -> dict:
        """Fetch one clip: stock by query/id, or AI-generate from a prompt."""

        def work():
            ws = ctx.workspace_id or ""
            if prompt.strip():
                self.step("generate_clip", prompt[:80])
                try:
                    path = generate_clip(prompt, ws, seconds, aspect)
                except BrollError as exc:
                    self.step_failed(str(exc)[:150])
                    raise
                self.step_done("ok", path)
                # Work 15.7: the AI-generate branch recorded NO cost at all. The
                # ``server`` backend is a billed GPU render, so its cost is now
                # booked by ``providers/broll.py`` as an estimate -- or as an
                # UNKNOWN-exposure event when the renderer never reports a
                # price. ``billable`` says which one happened so the caller does
                # not have to guess from a silent ledger.
                return {"summary": "generated AI B-roll clip", "path": path,
                        "source": "ai", "billable": True}
            vid = video_id.strip()
            if not vid:
                if not (query or "").strip():
                    raise BrollError("provide query, video_id, or prompt")
                self.step("search_stock", query[:80])
                found = search_stock(query, per_page=4)
                if not found:
                    raise BrollError(f"no stock results for '{query[:60]}'")
                vid = found[0].video_id
                self.step_done("ok", f"{len(found)} candidate(s)")
            self.step("fetch_stock", f"pexels:{vid}")
            try:
                path = fetch_stock_clip(vid, ws, aspect)
            except BrollError as exc:
                self.step_failed(str(exc)[:150])
                raise
            self.step_done("ok", path)
            return {"summary": f"fetched stock clip pexels:{vid}",
                    "path": path, "source": "stock"}

        return self.execute(ctx, "fetch_broll",
                            input_summary=(prompt or query or video_id)[:200], fn=work)
