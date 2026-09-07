"""Upgrade 0007: telegram remote-control links."""

from sqlalchemy import text


def upgrade(session) -> None:
    # Table is created by Base.metadata.create_all (models are imported by
    # app.models before migrations run); this migration exists to give the
    # change a deterministic version marker and add helpful indexes for
    # older installs where the table may already exist without them.
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_telegram_links_workspace_id "
            "ON telegram_links (workspace_id)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_telegram_links_chat_id "
            "ON telegram_links (chat_id)"
        )
    )
