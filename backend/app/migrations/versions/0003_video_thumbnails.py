"""0003: video thumbnails + job progress columns."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(session.bind)
    tables = set(inspector.get_table_names())

    if "videos" in tables:
        cols = {c["name"] for c in inspector.get_columns("videos")}
        if "thumbnail_path" not in cols:
            session.execute(text("ALTER TABLE videos ADD COLUMN thumbnail_path TEXT DEFAULT ''"))
