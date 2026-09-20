"""Agent registry — single source of truth for all YMONEY agents."""

from __future__ import annotations

from app.engine.agents.base import AgentMeta, BaseAgent
from app.engine.agents.creation import (
    HookOptimizerAgent,
    ResearchAgent,
    ScriptWriterAgent,
    StrategistAgent,
)
from app.engine.agents.discovery import TrendAnalystAgent, TrendHunterAgent
from app.engine.agents.distribution import PublisherAgent, SEOAgent
from app.engine.agents.intelligence import AnalyticsCollectorAgent, LearningAgent
from app.engine.agents.production import QualityAgent, VideoProducerAgent
from app.engine.agents.motion import MotionDesignerAgent
from app.engine.agents.repurpose import LinkMinerAgent, RepurposeEditorAgent

AGENTS: dict[str, type[BaseAgent]] = {
    "trend_hunter": TrendHunterAgent,
    "trend_analyst": TrendAnalystAgent,
    "research": ResearchAgent,
    "strategist": StrategistAgent,
    "script_writer": ScriptWriterAgent,
    "hook_optimizer": HookOptimizerAgent,
    "producer": VideoProducerAgent,
    "quality": QualityAgent,
    "seo": SEOAgent,
    "publisher": PublisherAgent,
    "analytics": AnalyticsCollectorAgent,
    "learning": LearningAgent,
    "link_miner": LinkMinerAgent,
    "repurpose_editor": RepurposeEditorAgent,
    "motion_designer": MotionDesignerAgent,
}

AGENT_META: dict[str, AgentMeta] = {k: v.meta for k, v in AGENTS.items()}

# Fail fast during startup/import if an agent references an unregistered
# capability. This prevents silently incomplete agent definitions.
for _meta in AGENT_META.values():
    _meta.validate_capabilities()


def get_agent(key: str) -> BaseAgent:
    cls = AGENTS.get(key)
    if not cls:
        raise KeyError(f"unknown agent: {key}")
    return cls()


def agent_catalog() -> list[dict]:
    return [
        {
            "key": m.key,
            "title": m.title,
            "description": m.description,
            **m.capability_catalog(),
        }
        for m in AGENT_META.values()
    ]
