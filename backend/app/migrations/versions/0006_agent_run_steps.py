"""Upgrade 0006: agent-run step tracing (subagent decomposition visibility)."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(session.bind)
    if "agent_runs" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("agent_runs")}
    if "steps_json" not in cols:
        session.execute(text("ALTER TABLE agent_runs ADD COLUMN steps_json JSON"))
