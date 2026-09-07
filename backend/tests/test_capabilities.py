from __future__ import annotations

import pytest

from app.engine.agents.registry import AGENT_META, agent_catalog
from app.engine.capabilities import ToolSpec, get_skill, register_tool, skill_catalog


def test_core_capabilities_are_registered():
    assert get_skill("trend_research").version == "1.0.0"
    assert any(item["key"] == "scriptwriting" for item in skill_catalog())


def test_agent_catalog_exposes_capabilities():
    catalog = agent_catalog()
    producer = next(item for item in catalog if item["key"] == "producer")
    assert "skills" in producer
    assert "tools" in producer
    assert "execution_policy" in producer


def test_tool_requires_all_declared_permissions():
    tool = ToolSpec(
        name="test_permission_tool",
        description="test",
        permissions=("one", "two"),
        handler=lambda **kwargs: kwargs["value"],
    )
    register_tool(tool)
    with pytest.raises(PermissionError):
        tool.invoke(granted_permissions={"one"}, value=3)
    assert tool.invoke(granted_permissions={"one", "two"}, value=3) == 3


def test_agent_metadata_references_registered_capabilities():
    for meta in AGENT_META.values():
        meta.validate_capabilities()
