"""Upgrade 0010: virality column on opportunities (breakout potential, informational)."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(session.bind)
    if "opportunities" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("opportunities")}
    if "virality" not in cols:
        session.execute(text("ALTER TABLE opportunities ADD COLUMN virality FLOAT DEFAULT 0.0"))
