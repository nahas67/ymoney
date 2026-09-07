"""Reusable v2 agent runtime contracts.

This module intentionally does not replace the existing job queue. It provides
an incremental boundary that agents and future workers can adopt while the
legacy pipeline remains compatible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.engine.capabilities import get_skill, get_tool


@dataclass(frozen=True)
class AgentExecutionPolicy:
    timeout_seconds: int = 300
    max_retries: int = 2
    risk_level: str = "low"
    requires_human_review: bool = False


@dataclass
class AgentExecutionResult:
    status: str
    output: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    error: str = ""


class AgentRuntime:
    """Validate and execute a declarative agent definition.

    The runtime is deliberately provider-neutral. Concrete agents supply the
    callable; the runtime owns capability validation and structured results.
    """

    def __init__(self, meta, *, policy: AgentExecutionPolicy | None = None):
        self.meta = meta
        self.policy = policy or AgentExecutionPolicy()
        self.validate()

    def validate(self) -> None:
        self.meta.validate_capabilities()
        for skill_key in self.meta.skills:
            skill = get_skill(skill_key)
            missing = set(skill.required_tools) - set(self.meta.tools)
            if missing:
                raise ValueError(
                    f"agent '{self.meta.key}' is missing required tool(s): {', '.join(sorted(missing))}"
                )
        if self.policy.timeout_seconds <= 0:
            raise ValueError("agent timeout must be positive")
        if self.policy.max_retries < 0:
            raise ValueError("agent max_retries cannot be negative")

    def execute(self, work: Callable[[], Any]) -> AgentExecutionResult:
        try:
            output = work()
            return AgentExecutionResult(status="COMPLETED", output=output)
        except Exception as exc:
            return AgentExecutionResult(status="FAILED", error=str(exc))

    def tool_specs(self):
        return [get_tool(name) for name in self.meta.tools]
