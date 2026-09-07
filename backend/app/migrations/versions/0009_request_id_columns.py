"""Upgrade 0009: add request_id columns for correlation tracing."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(session.bind)
    tables = set(inspector.get_table_names())

    for table in ("agent_runs", "event_logs"):
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "request_id" not in cols:
            session.execute(
                text(f'ALTER TABLE {table} ADD COLUMN request_id VARCHAR(32) DEFAULT ""')
            )
