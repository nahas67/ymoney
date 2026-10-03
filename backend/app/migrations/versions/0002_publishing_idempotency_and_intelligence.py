"""Upgrade 0002: publishing idempotency + opportunity intelligence fields."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    from app.migrations.ddl import add_columns_if_missing

    inspector = inspect(session.bind)
    tables = set(inspector.get_table_names())

    # --- publishing_jobs: one row per (video_id, platform) -------------------
    if "publishing_jobs" in tables:
        has_unique = any(
            idx.get("name") == "uq_pubjob_video_platform"
            for idx in inspector.get_indexes("publishing_jobs")
        )
        if not has_unique:
            # collapse historical duplicates first: keep newest row per pair
            # (derived tables carry an alias: PostgreSQL rejects FROM
            # (SELECT ...) without one -- W11.5 F2)
            session.execute(text("""
                DELETE FROM publishing_jobs
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY video_id, platform ORDER BY created_at DESC
                        ) AS rn
                        FROM publishing_jobs
                    ) AS keep_newest WHERE rn = 1
                )
            """))
            session.execute(text(
                "CREATE UNIQUE INDEX uq_pubjob_video_platform "
                "ON publishing_jobs (video_id, platform)"
            ))

    # --- published_posts: unique per (video_id, platform) --------------------
    if "published_posts" in tables:
        has_unique = any(
            idx.get("name") == "uq_post_video_platform"
            for idx in inspector.get_indexes("published_posts")
        )
        if not has_unique:
            session.execute(text("""
                DELETE FROM published_posts
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY video_id, platform ORDER BY created_at DESC
                        ) AS rn
                        FROM published_posts
                    ) AS keep_newest WHERE rn = 1
                )
            """))
            # metrics referencing deleted posts must go too
            session.execute(text(
                "DELETE FROM post_metrics WHERE post_id NOT IN (SELECT id FROM published_posts)"
            ))
            session.execute(text(
                "CREATE UNIQUE INDEX uq_post_video_platform ON published_posts (video_id, platform)"
            ))

    # --- opportunities: lifecycle + confidence columns ------------------------
    if "opportunities" in tables:
        # Catalog-guarded rather than try/except, for the same reason as the
        # rest of the corpus: a caught duplicate-column error leaves the
        # PostgreSQL transaction ABORTED (25P02) and the real fault never shows.
        add_columns_if_missing(
            session,
            "opportunities",
            [
                ("lifecycle", "VARCHAR(15) DEFAULT 'UNKNOWN'"),
                ("confidence", "FLOAT DEFAULT 0.5"),
            ],
        )
