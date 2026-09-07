from app.engine.capabilities import ToolSpec
from app.models import CapabilityPermission, ToolCallAudit


def test_workspace_tool_denial_is_persisted(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    db_session.add(CapabilityPermission(
        workspace_id=ws_id, capability_type="tool", capability_key="test_tool", allowed=False
    ))
    db_session.commit()

    tool = ToolSpec("test_tool", "test", handler=lambda: "ok")
    try:
        tool.invoke(granted_permissions=set(), workspace_id=ws_id, agent_key="tester")
    except PermissionError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("disabled workspace tool was invoked")


def test_tool_call_is_audited(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    tool = ToolSpec("audited_tool", "test", handler=lambda value: value)
    assert tool.invoke(
        granted_permissions=set(), workspace_id=ws_id, agent_key="tester", value="done"
    ) == "done"

    row = db_session.query(ToolCallAudit).filter_by(
        workspace_id=ws_id, tool_name="audited_tool"
    ).one()
    assert row.status == "COMPLETED"
    assert row.output_summary == "done"
