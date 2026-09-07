from app.engine.agents.registry import AGENT_META
from app.engine.runtime import AgentExecutionPolicy, AgentRuntime


def test_registered_agents_are_runtime_valid():
    for meta in AGENT_META.values():
        runtime = AgentRuntime(meta)
        assert runtime.tool_specs() or not meta.skills


def test_runtime_returns_structured_success():
    runtime = AgentRuntime(AGENT_META["research"])
    result = runtime.execute(lambda: {"summary": "ok"})
    assert result.status == "COMPLETED"
    assert result.output == {"summary": "ok"}


def test_runtime_returns_structured_failure():
    runtime = AgentRuntime(AGENT_META["research"], policy=AgentExecutionPolicy(max_retries=1))
    result = runtime.execute(lambda: (_ for _ in ()).throw(RuntimeError("provider down")))
    assert result.status == "FAILED"
    assert result.error == "provider down"
