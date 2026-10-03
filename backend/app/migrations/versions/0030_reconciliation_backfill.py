"""Upgrade 0030: Work 11.5 reconciliation backfill (MEDIUM/HIGH fixes).

Re-applies, idempotently, every pre-0030 step that an install may have missed
because the runner keys applied-state on the filename and never re-runs a
marked-applied migration:

1. 0009 backfill (W11.5 A-F6): installs that applied 0009 before the
   event_logs->events rename are marked applied yet lack request_id columns.
2. 0002 backfill (W11.5 A-F2): dedupe + UNIQUE indexes with PostgreSQL-valid
   derived-table aliases (the shipped 0002 lacked them).
3. BrandOverride UNIQUE enforcement (W11.5 A-F3): dedupe + upgrade of the
   plain index to UNIQUE, matching the ORM's unique=True.
4. Opportunity workspace+topic UNIQUE (W11.5 A-F4): the ORM declares
   uq_opportunity_ws_topic but no migration ever created it.
5. scenes idx->index rename (W11.5 A-F4): legacy DDL created column `idx`,
   the ORM reads/writes column `index`. RENAME COLUMN works on SQLite
   (3.25+) and PostgreSQL.
6. platform_variants workspace index (W11.5 A-F7): the ORM declares
   index=True on workspace_id but the 0016 DDL never created it.

Every step is guarded by catalog reads, so fresh installs (create_all
first) and already-fixed installs replay as no-ops. No existing table is
dropped; the only destructive statements are dedupe DELETEs that keep the
newest row per group (same pattern as 0002).

Backend note
------------
The guards here read the catalog instead of catching errors, which is what makes
this migration safe on PostgreSQL. The old code wrapped the catalog lookups in
``except: return {}``; reflection for an existing table cannot fail, and on
PostgreSQL a swallowed error of any kind leaves the transaction ABORTED so every
statement after it dies with 25P02. A lookup that cannot fail cannot poison
anything.

Step 5 is the same class of drift as the ``longform_chapters.idx``/``index``
split fixed in 0015: SQLite short-circuits ``CREATE INDEX IF NOT EXISTS`` on the
index NAME and never validates the column list, so an index over ``idx`` looked
healthy for the life of the migration. PostgreSQL resolves columns first and
raises 42703. The rename below is what makes the two spellings converge.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.migrations.ddl import add_columns_if_missing, has_column, table_exists


def upgrade(session) -> None:
    from sqlalchemy import text

    def _unique_index_exists(table: str, index: str) -> bool:
        """Whether ``index`` is already present AND already UNIQUE.

        ``ddl.index_exists`` answers presence only, and presence alone is not
        enough here: a non-unique index of the right name is the exact drift
        steps 2-4 exist to repair, so the repair must still run. Reflection for
        an existing table cannot fail, so nothing is caught around it.
        """
        if not table_exists(session, table):
            return False
        found = inspect(session.get_bind()).get_indexes(table)
        entry = next((i for i in found if i.get("name") == index), None)
        return bool(entry) and bool(entry.get("unique"))

    # --- 1. request_id backfill (0009) -------------------------------------
    # ``create_all`` builds the current shape before migrations run, so on a
    # fresh install both columns are already there and this adds nothing; on an
    # install that marked 0009 applied before the events rename, they are not.
    for _table in ("agent_runs", "events"):
        add_columns_if_missing(
            session, _table, [("request_id", "VARCHAR(32) DEFAULT ''")]
        )

    # --- 2. publishing dedupe + UNIQUE (0002, PG-valid aliases) ------------
    if (table_exists(session, "publishing_jobs")
            and not _unique_index_exists("publishing_jobs", "uq_pubjob_video_platform")):
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
        session.execute(text("DROP INDEX IF EXISTS uq_pubjob_video_platform"))
        session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pubjob_video_platform "
            "ON publishing_jobs (video_id, platform)"
        ))
    if (table_exists(session, "published_posts")
            and not _unique_index_exists("published_posts", "uq_post_video_platform")):
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
        if table_exists(session, "post_metrics"):
            session.execute(text(
                "DELETE FROM post_metrics "
                "WHERE post_id NOT IN (SELECT id FROM published_posts)"
            ))
        session.execute(text("DROP INDEX IF EXISTS uq_post_video_platform"))
        session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_post_video_platform "
            "ON published_posts (video_id, platform)"
        ))

    # --- 3. brand_overrides UNIQUE (0024) ----------------------------------
    if (table_exists(session, "brand_overrides")
            and not _unique_index_exists("brand_overrides", "uq_brand_override_subject")):
        session.execute(text("""
            DELETE FROM brand_overrides
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY workspace_id, subject_type, subject_id
                        ORDER BY created_at DESC
                    ) AS rn
                    FROM brand_overrides
                ) AS keep_newest WHERE rn = 1
            )
        """))
        session.execute(text("DROP INDEX IF EXISTS uq_brand_override_subject"))
        session.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_brand_override_subject
            ON brand_overrides (workspace_id, subject_type, subject_id)
        """))

    # --- 4. opportunities workspace+topic UNIQUE ---------------------------
    if (table_exists(session, "opportunities")
            and not _unique_index_exists("opportunities", "uq_opportunity_ws_topic")):
        session.execute(text("""
            DELETE FROM opportunities
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY workspace_id, topic ORDER BY created_at DESC
                    ) AS rn
                    FROM opportunities
                ) AS keep_newest WHERE rn = 1
            )
        """))
        session.execute(text("DROP INDEX IF EXISTS uq_opportunity_ws_topic"))
        session.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_ws_topic
            ON opportunities (workspace_id, topic)
        """))

    # --- 5. scenes idx -> index rename --------------------------------------
    # Only when the legacy spelling is actually present AND the canonical one is
    # not: ``create_all`` gives ``index`` (the ORM attribute is named after the
    # SQL keyword), so on a fresh install this is a no-op. RENAME COLUMN keeps
    # the indexes that reference the old name consistent on both backends.
    if has_column(session, "scenes", "idx") and not has_column(session, "scenes", "index"):
        session.execute(text('ALTER TABLE scenes RENAME COLUMN idx TO "index"'))

    # --- 6. platform_variants workspace index -------------------------------
    if table_exists(session, "platform_variants"):
        session.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_platform_variants_workspace_id "
            "ON platform_variants (workspace_id)"
        ))