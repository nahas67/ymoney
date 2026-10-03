"""0003: video thumbnails + job progress columns."""

from app.migrations.ddl import add_column_if_missing


def upgrade(session) -> None:
    # See 0003_video_progress: the guard reads the catalog instead of
    # swallowing a duplicate-column error, which on PostgreSQL would abort
    # the migration's transaction for good.
    add_column_if_missing(session, "videos", "thumbnail_path", "TEXT DEFAULT ''")
