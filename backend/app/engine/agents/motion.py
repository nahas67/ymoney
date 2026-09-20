"""Motion Designer agent (E1b: HyperFrames motion-graphics lane).

Renders designed motion cards — hook titles, stat hits, CTAs, lower-thirds —
through the HyperFrames provider. Fully optional: when no working browser/CLI
exists the provider fails closed with remediation and the agent surfaces it
instead of faking output.
"""

from __future__ import annotations

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.motion import CARD_KINDS, render_card


class MotionDesignerAgent(BaseAgent):
    meta = AgentMeta(
        key="motion_designer",
        title="Motion Designer",
        description="Renders kinetic motion-graphics cards (hooks, stats, CTAs).",
        skills=("motion_graphics",),
        tools=("render_motion",),
        permissions=("media:render",),
    )

    def design(self, ctx, *, kind: str, title: str, subtitle: str = "",
               accent: str = "#22c55e", duration: float = 3.0) -> dict:
        def work():
            kind_norm = (kind or "hook").lower()
            if kind_norm not in CARD_KINDS:
                raise ValueError(f"unknown card kind '{kind}' ({'/'.join(CARD_KINDS)})")
            self.step("render_card", f"{kind_norm} card: '{title[:60]}'")
            card = render_card(kind_norm, title, ctx.workspace_id or "",
                               subtitle=subtitle, accent=accent, duration=duration)
            self.step_done("ok", card.path)
            self.track_cost(ctx, "video", 0.0, provider="hyperframes")
            return {
                "summary": f"rendered {kind_norm} motion card",
                "path": card.path,
                "kind": card.kind,
                "duration": card.duration,
            }

        return self.execute(ctx, "render_motion", input_summary=f"{kind}: {title[:80]}", fn=work)
