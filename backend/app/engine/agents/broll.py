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
            plan = plan_scenes(topic, keywords or [], n_scenes, ctx.workspace_id or "")
            self.step_done("ok", f"{len(plan)} scene(s)")
            return {
                "summary": f"planned {len(plan)} scene visual(s) for '{topic[:60]}'",
                "scenes": [
                    {"index": s.index, "query": s.query, "prompt": s.prompt,
                     "source": s.source, "license": s.license}
                    for s in plan
                ],
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
                return {"summary": "generated AI B-roll clip", "path": path, "source": "ai"}
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
