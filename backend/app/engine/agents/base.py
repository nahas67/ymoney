"""Agent base class: identity, run recording, cost attribution."""

from __future__ import annotations

import time
from abc import ABC
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.engine.capabilities import get_skill, get_tool
from app.models import AgentConfig, Workspace
from app.services import cost as cost_service
from app.services import jobs as jobs_service
from app.services import provider_settings
from app.services.events import record_event


@dataclass
class AgentMeta:
    key: str
    title: str
    description: str
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    model_policy: str = "configured"
    execution_policy: str = "durable"

    def validate_capabilities(self) -> None:
        for skill_key in self.skills:
            get_skill(skill_key)
        for tool_name in self.tools:
            get_tool(tool_name)

    def capability_catalog(self) -> dict:
        return {
            "skills": list(self.skills),
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "model_policy": self.model_policy,
            "execution_policy": self.execution_policy,
        }


class BaseAgent(ABC):
    meta: AgentMeta

    def __init__(self):
        self._started = 0.0
        self._steps: list[dict] = []

    # -- step tracing (subagent decomposition visibility) ---------------------

    def step(self, name: str, detail: str = "") -> None:
        """Record the start of a named sub-step. Close it with step_done().
        Steps appear in the Agent Center run inspector in execution order."""
        self._steps.append({
            "step": name,
            "detail": detail[:300],
            "started_ms": int((time.monotonic() - self._started) * 1000)
            if self._started else 0,
            "duration_ms": None,
            "status": "running",
        })

    def step_done(self, status: str = "ok", detail: str = "") -> None:
        """Close the most recent running step with an outcome."""
        for rec in reversed(self._steps):
            if rec["status"] == "running":
                rec["status"] = status
                rec["duration_ms"] = max(
                    0, int((time.monotonic() - self._started) * 1000) - rec["started_ms"]
                )
                if detail:
                    rec["detail"] = detail[:300]
                return

    def step_failed(self, detail: str) -> None:
        self.step_done(status="failed", detail=detail)

    # -- configuration ------------------------------------------------------

    def config(self, workspace_id: str) -> AgentConfig | None:
        with session_scope() as s:
            cfg = s.scalar(
                select(AgentConfig).where(
                    AgentConfig.workspace_id == workspace_id,
                    AgentConfig.agent_key == self.meta.key,
                )
            )
            if cfg:
                s.expunge(cfg)
            return cfg

    def is_enabled(self, workspace_id: str) -> bool:
        cfg = self.config(workspace_id)
        return True if cfg is None else cfg.enabled

    def model_for(self, workspace_id: str) -> str | None:
        cfg = self.config(workspace_id)
        return (cfg.model or "") or None if cfg else None

    # -- execution wrapper ---------------------------------------------------

    def execute(
        self,
        ctx: jobs_service.JobContext,
        task_type: str,
        input_summary: str = "",
        fn=None,
    ) -> Any:
        """Run agent work with run-tracking + events. `fn` does the actual work."""
        ws = ctx.workspace_id
        run_id = jobs_service.start_agent_run(
            ws,
            self.meta.key,
            task_type,
            job_id=ctx.job_id,
            cycle_id=ctx.cycle_id,
            input_summary=input_summary,
        )
        self._started = time.monotonic()
        self._steps = []
        try:
            # All provider access performed by an agent inherits the authorized
            # workspace, including calls made several layers below the agent.
            # This prevents encrypted credentials from leaking across tenants.
            with provider_settings.workspace_scope(ws):
                result = fn() if fn else None
            duration_ms = int((time.monotonic() - self._started) * 1000)
            output_summary = _summarize(result)
            for rec in self._steps:
                if rec["status"] == "running":
                    rec["status"] = "interrupted"
            jobs_service.finish_agent_run(
                run_id,
                status="COMPLETED",
                output_summary=output_summary,
                cost_usd=float(ctx.artifacts.get("cost_usd", 0.0)),
                steps=list(self._steps),
            )
            record_event(
                ws,
                kind=f"agent.{self.meta.key}",
                message=f"{self.meta.title}: {output_summary}",
                level="info",
                source=self.meta.key,
                data={"cycle_id": ctx.cycle_id, "run_id": run_id},
            )
            return result
        except Exception as exc:
            for rec in self._steps:
                if rec["status"] == "running":
                    rec["status"] = "failed"
            jobs_service.finish_agent_run(
                run_id, status="FAILED", error=str(exc), cost_usd=float(ctx.artifacts.get("cost_usd", 0.0)),
                steps=list(self._steps),
            )
            record_event(
                ws,
                kind=f"agent.{self.meta.key}.error",
                message=f"{self.meta.title} failed: {exc}",
                level="error",
                source=self.meta.key,
                data={"cycle_id": ctx.cycle_id},
            )
            raise

    def track_cost(self, ctx, category: str, amount: float, provider: str = "", detail: dict | None = None):
        if amount > 0:
            cost_service.track_cost(
                ctx.workspace_id or "", category, amount, provider=provider, cycle_id=ctx.cycle_id, detail=detail
            )
            ctx.artifacts["cost_usd"] = ctx.artifacts.get("cost_usd", 0.0) + amount

    def announce(self, workspace_id: str, message: str, level: str = "info", **data):
        record_event(workspace_id, kind=f"agent.{self.meta.key}", message=message, level=level, source=self.meta.key, data=data)


def _summarize(result) -> str:
    if result is None:
        return "completed"
    if isinstance(result, dict):
        for k in ("summary", "message", "topic"):
            if k in result and isinstance(result[k], str):
                return result[k][:300]
        return f"{len(result)} item(s)"
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    return str(result)[:300]


def get_workspace(workspace_id: str) -> Workspace | None:
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        if ws:
            s.expunge(ws)
        return ws


def all_agents() -> list[BaseAgent]:
    from app.engine.agents.registry import AGENTS

    return [agent_cls() for agent_cls in AGENTS.values()]
