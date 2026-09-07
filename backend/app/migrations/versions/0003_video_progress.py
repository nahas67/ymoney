"""Upgrade 0003: video render progress column."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(session.bind)
    if "videos" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("videos")}
    if "progress" not in cols:
        session.execute(text("ALTER TABLE videos ADD COLUMN progress INTEGER DEFAULT 0"))
