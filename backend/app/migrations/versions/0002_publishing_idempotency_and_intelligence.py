"""Upgrade 0002: publishing idempotency + opportunity intelligence fields."""


def upgrade(session) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(session.bind)
    tables = set(inspector.get_table_names())

    # --- publishing_jobs: one row per (video_id, platform) -------------------
    if "publishing_jobs" in tables:
        cols = {c["name"] for c in inspector.get_columns("publishing_jobs")}
        has_unique = any(
            idx.get("name") == "uq_pubjob_video_platform"
            for idx in inspector.get_indexes("publishing_jobs")
        )
        if not has_unique:
            # collapse historical duplicates first: keep newest row per pair
            session.execute(text("""
                DELETE FROM publishing_jobs
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY video_id, platform ORDER BY created_at DESC
                        ) AS rn
                        FROM publishing_jobs
                    ) WHERE rn = 1
                )
            """))
            session.execute(text(
                "CREATE UNIQUE INDEX uq_pubjob_video_platform "
                "ON publishing_jobs (video_id, platform)"
            ))
        del cols

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
                    ) WHERE rn = 1
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
        cols = {c["name"] for c in inspector.get_columns("opportunities")}
        if "lifecycle" not in cols:
            session.execute(text(
                "ALTER TABLE opportunities ADD COLUMN lifecycle VARCHAR(15) DEFAULT 'UNKNOWN'"
            ))
        if "confidence" not in cols:
            session.execute(text(
                "ALTER TABLE opportunities ADD COLUMN confidence FLOAT DEFAULT 0.5"
            ))
