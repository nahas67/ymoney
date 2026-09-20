"""First-class capabilities used by YMONEY agents.

Skills describe reusable domain capabilities. Tools describe controlled
operations an agent may invoke. Both are declarative and registry-backed so
providers and agents can evolve without changing orchestration code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from app.db import session_scope
from app.models import CapabilityPermission, ToolCallAudit


@dataclass(frozen=True)
class SkillSpec:
    key: str
    title: str
    description: str
    version: str = "1.0.0"
    required_tools: tuple[str, ...] = ()
    provider: str = "local"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    provider: str = "local"
    timeout_seconds: int = 30
    retryable: bool = False
    estimated_cost_usd: float = 0.0
    handler: Callable[..., Any] | None = field(default=None, compare=False, repr=False)

    def invoke(
        self,
        *,
        granted_permissions: set[str] | frozenset[str],
        workspace_id: str | None = None,
        agent_key: str = "system",
        job_id: str | None = None,
        cycle_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Invoke only after declared and workspace permissions are granted."""
        missing = set(self.permissions) - set(granted_permissions)
        if workspace_id and missing:
            missing -= denied_workspace_permissions(workspace_id)
        if workspace_id and not workspace_allows(workspace_id, self.name):
            raise PermissionError(f"tool '{self.name}' is disabled for this workspace")
        audit_id = None
        started = __import__("time").monotonic()
        if workspace_id:
            audit_id = start_tool_audit(workspace_id, agent_key, self.name, job_id, cycle_id, self.estimated_cost_usd)
        if missing:
            if audit_id:
                finish_tool_audit(audit_id, "DENIED", error=f"missing permission(s): {', '.join(sorted(missing))}", duration_ms=int((__import__("time").monotonic() - started) * 1000))
            raise PermissionError(
                f"tool '{self.name}' requires permission(s): {', '.join(sorted(missing))}"
            )
        if self.handler is None:
            if audit_id:
                finish_tool_audit(audit_id, "FAILED", error="no executable handler", duration_ms=int((__import__("time").monotonic() - started) * 1000))
            raise RuntimeError(f"tool '{self.name}' has no executable handler")
        try:
            result = self.handler(**kwargs)
        except Exception as exc:
            if audit_id:
                finish_tool_audit(audit_id, "FAILED", error=str(exc), duration_ms=int((__import__("time").monotonic() - started) * 1000))
            raise
        if audit_id:
            finish_tool_audit(audit_id, "COMPLETED", output_summary=_summarize(result), duration_ms=int((__import__("time").monotonic() - started) * 1000))
        return result


SKILLS: dict[str, SkillSpec] = {}
TOOLS: dict[str, ToolSpec] = {}


def workspace_allows(workspace_id: str, tool_name: str) -> bool:
    with session_scope() as session:
        row = session.query(CapabilityPermission).filter_by(
            workspace_id=workspace_id, capability_type="tool", capability_key=tool_name
        ).first()
        return row is None or row.allowed


def denied_workspace_permissions(workspace_id: str) -> set[str]:
    with session_scope() as session:
        rows = session.query(CapabilityPermission).filter_by(
            workspace_id=workspace_id, capability_type="permission", allowed=False
        ).all()
        return {row.capability_key for row in rows}


def start_tool_audit(workspace_id, agent_key, tool_name, job_id, cycle_id, estimate):
    with session_scope() as session:
        row = ToolCallAudit(workspace_id=workspace_id, agent_key=agent_key, tool_name=tool_name,
                            job_id=job_id, cycle_id=cycle_id, estimated_cost_usd=estimate)
        session.add(row)
        session.flush()
        return row.id


def finish_tool_audit(audit_id, status, output_summary="", error="", duration_ms=None):
    with session_scope() as session:
        row = session.get(ToolCallAudit, audit_id)
        if row:
            row.status = status
            row.output_summary = output_summary[:4000]
            row.error = error[:2000]
            row.duration_ms = duration_ms


def _summarize(value):
    if isinstance(value, dict):
        return str(value.get("summary") or value)[:4000]
    return str(value)[:4000]


def register_skill(skill: SkillSpec) -> SkillSpec:
    if skill.key in SKILLS:
        raise ValueError(f"duplicate skill: {skill.key}")
    for tool_name in skill.required_tools:
        if tool_name not in TOOLS:
            raise ValueError(f"skill '{skill.key}' requires unknown tool '{tool_name}'")
    SKILLS[skill.key] = skill
    return skill


def register_tool(tool: ToolSpec) -> ToolSpec:
    if tool.name in TOOLS:
        raise ValueError(f"duplicate tool: {tool.name}")
    if tool.timeout_seconds <= 0:
        raise ValueError("tool timeout must be positive")
    if tool.estimated_cost_usd < 0:
        raise ValueError("tool estimated cost cannot be negative")
    TOOLS[tool.name] = tool
    return tool


def get_skill(key: str) -> SkillSpec:
    try:
        return SKILLS[key]
    except KeyError as exc:
        raise KeyError(f"unknown skill: {key}") from exc


def get_tool(name: str) -> ToolSpec:
    try:
        return TOOLS[name]
    except KeyError as exc:
        raise KeyError(f"unknown tool: {name}") from exc


def skill_catalog() -> list[dict[str, Any]]:
    return [
        {
            "key": skill.key,
            "title": skill.title,
            "description": skill.description,
            "version": skill.version,
            "required_tools": list(skill.required_tools),
            "provider": skill.provider,
        }
        for skill in SKILLS.values()
    ]


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "permissions": list(tool.permissions),
            "provider": tool.provider,
            "timeout_seconds": tool.timeout_seconds,
            "retryable": tool.retryable,
            "estimated_cost_usd": tool.estimated_cost_usd,
        }
        for tool in TOOLS.values()
    ]


def register_core_capabilities() -> None:
    """Register the safe, provider-neutral capability vocabulary."""
    core_tools = [
        ToolSpec("search_trends", "Retrieve trend candidates from enabled sources", {"type": "object"}, ("trend:read",), "trend", 30, True),
        ToolSpec("fetch_url", "Fetch a permitted public URL", {"type": "object", "required": ["url"]}, ("research:read",), "http", 30, True),
        ToolSpec("generate_script", "Generate a structured script through the configured LLM", {"type": "object"}, ("llm:generate",), "llm", 120, True),
        ToolSpec("render_video", "Render a storyboard through the selected video engine", {"type": "object"}, ("media:render",), "video", 900, True),
        ToolSpec("publish_post", "Publish through a configured official platform adapter", {"type": "object"}, ("publish:write",), "publisher", 300, True),
        ToolSpec("fetch_metrics", "Read metrics through a configured analytics adapter", {"type": "object"}, ("analytics:read",), "analytics", 60, True),
        ToolSpec("mine_moments", "Mine ranked viral moments from a long-form source transcript", {"type": "object"}, ("llm:generate",), "clips", 180, True),
        ToolSpec("assemble_clips", "Cut ranked moments into captioned vertical shorts", {"type": "object"}, ("media:render",), "clips", 900, True),
        ToolSpec("render_motion", "Render a HyperFrames motion-graphics card (title/stat/CTA/lower-third)", {"type": "object"}, ("media:render",), "motion", 600, True),
        ToolSpec("dub_video", "Translate, voice and reassemble a video in another language", {"type": "object"}, ("media:render",), "dubbing", 1200, True),
        ToolSpec("synthesize_speech", "Narrate text with the workspace voice stack (clone/emotion aware)", {"type": "object"}, ("tts:synthesize",), "tts", 180, True),
        ToolSpec("store_memory", "Persist a scoped workflow or learning memory", {"type": "object"}, ("memory:write",), "memory", 30, False),        ToolSpec("retrieve_memory", "Retrieve targeted memories for a workflow", {"type": "object"}, ("memory:read",), "memory", 30, True),
    ]
    for tool in core_tools:
        if tool.name not in TOOLS:
            register_tool(tool)

    core_skills = [
        SkillSpec("trend_research", "Trend Research", "Discover and normalize opportunity candidates", required_tools=("search_trends",)),
        SkillSpec("research", "Research", "Collect source-backed research for a topic", required_tools=("fetch_url",)),
        SkillSpec("scriptwriting", "Script Writing", "Create platform-aware short-form scripts", required_tools=("generate_script",)),
        SkillSpec("video_production", "Video Production", "Turn storyboards into rendered media", required_tools=("render_video",)),
        SkillSpec("publishing", "Platform Publishing", "Deliver platform-specific packages idempotently", required_tools=("publish_post",)),
        SkillSpec("analytics_learning", "Analytics and Learning", "Measure performance and update memory", required_tools=("fetch_metrics", "store_memory", "retrieve_memory")),
        SkillSpec("clip_mining", "Clip Mining", "Mine ranked viral moments from long-form sources", required_tools=("mine_moments",)),
        SkillSpec("clip_assembly", "Clip Assembly", "Assemble ranked moments into captioned vertical shorts", required_tools=("assemble_clips",)),
        SkillSpec("motion_graphics", "Motion Graphics", "Render designed motion cards via HyperFrames", required_tools=("render_motion",)),
        SkillSpec("dubbing_localization", "Dubbing & Localization", "Translate and re-voice videos into other languages", required_tools=("dub_video",)),
        SkillSpec("voice_design", "Voice Design", "Cast, clone and direct narration voices per scene", required_tools=("synthesize_speech",)),
    ]
    for skill in core_skills:
        if skill.key not in SKILLS:
            register_skill(skill)


register_core_capabilities()
