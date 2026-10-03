"""Upgrade 0032: Work 15 autonomous planning (signals, opportunities, plans).

Adds the planning substrate WITHOUT duplicating existing state:

* ``trend_signals`` -- NEW. A signal is an observation with its evidence, not
  a score. Nothing here stores a "trend score" for data we do not have.
* ``opportunities`` -- EXTENDED (never replaced). The existing topic/source/
  score/skip columns stay; the new columns record WHERE the idea came from and
  how it was derived, so ``OBSERVED`` can never be confused with ``INFERRED``.
* ``editorial_plans`` + ``editorial_plan_items`` -- NEW. A plan is a horizon of
  work; an item REFERENCES the canonical ``opportunity_id`` and
  ``campaign_id``/``schedule_entry_id`` rather than copying their state.
* ``production_capacity` -- NEW per-workspace capacity, so the planner can
  refuse an impossible workload instead of scheduling it.

Every step is guarded by catalog reads, so a fresh install (create_all
first) and an already-migrated install both replay as no-ops. Nothing existing
is dropped or narrowed.
"""

from __future__ import annotations

from app.migrations.ddl import add_columns_if_missing, table_exists

#: (column, DDL type) pairs added to the EXISTING opportunities table.
#:
#: ``format`` and ``basis`` are SQL keywords-ish identifiers, so they are added
#: through ``add_columns_if_missing``, which quotes them. Unquoted they happen to
#: parse on both backends today, but a quoted identifier has no dialect
#: surprise left to give.
_OPPORTUNITY_COLUMNS = (
    ("angle", "VARCHAR(400)"),
    ("audience", "VARCHAR(200)"),
    ("platforms_json", "JSON"),
    ("format", "VARCHAR(40)"),
    ("evidence_json", "JSON"),
    ("basis", "VARCHAR(20)"),
    ("brand_fit", "FLOAT"),
    ("freshness", "VARCHAR(12)"),
    ("competition_json", "JSON"),
    ("estimated_effort_hours", "FLOAT"),
    ("estimated_cost_usd", "FLOAT"),
    ("priority_inputs_json", "JSON"),
    ("dedupe_verdict", "VARCHAR(20)"),
    ("dedupe_reason", "VARCHAR(400)"),
    ("plan_item_id", "VARCHAR(36)"),
)

#: Columns added to the EXISTING schedule_entries table.
#:
#: ``plan_item_id`` is the planner's idempotency key. Without it the only
#: identity available is (campaign_id, content_item_id, platform), which is
#: empty for a plan item that has produced neither yet -- so two unrelated
#: items on the same platform collapsed onto ONE row and re-planning one
#: silently moved the other's time. Nullable, and ignored by the canonical
#: Scheduler, so existing entries are unaffected.
_SCHEDULE_COLUMNS = (
    ("plan_item_id", "VARCHAR(36)"),
)

# ``DATETIME`` was spelled literally in the four new tables below. That is a
# SQLite-only type name: SQLite accepts any unknown type and gives it NUMERIC
# affinity, but PostgreSQL has no ``datetime`` type and raises 42704 ("type
# datetime does not exist"). It stayed hidden because ``create_all`` runs first
# and already builds these tables, so ``CREATE TABLE IF NOT EXISTS`` never
# executed the DDL on a fresh install -- it only bites the trimmed-deployment
# path where the table genuinely does not exist yet. ``TIMESTAMP`` is what
# ``create_all`` builds on both backends and carries the same NUMERIC affinity
# in SQLite, so the resulting schema is identical on either backend.
#
# ``REAL`` below is LEFT ALONE deliberately. Unlike ``DATETIME`` it is valid on
# both backends, so nothing breaks; but it is not equivalent -- PostgreSQL reads
# it as float4 (numeric_precision 24) where ``create_all`` / the ORM's ``Float``
# builds float8 (53), while SQLite reads it as REAL affinity over 8-byte storage.
# On any run through the runner the ORM wins and this DDL never executes, so the
# two agree. It would only diverge if these tables were ever created by the
# migration path alone, and these are money-adjacent columns (budget_usd,
# spent_usd, estimated_cost_usd, priority): changing the declared width of a
# spend field is a decision for whoever owns the money semantics, not a side
# effect of a database-port fix. Flagged here rather than done silently.
_NEW_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS trend_signals (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL,
        source VARCHAR(60) NOT NULL,
        topic VARCHAR(400) NOT NULL,
        topic_key VARCHAR(400) NOT NULL,
        external_ref VARCHAR(500) NOT NULL DEFAULT '',
        evidence_ids_json JSON NOT NULL DEFAULT '[]',
        observed_at TIMESTAMP NOT NULL,
        freshness VARCHAR(12) NOT NULL DEFAULT 'FRESH',
        scope VARCHAR(40) NOT NULL DEFAULT 'workspace',
        velocity_json JSON,
        confidence REAL NOT NULL DEFAULT 0.0,
        status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
        payload_json JSON NOT NULL DEFAULT '{}',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        UNIQUE (workspace_id, source, topic_key, external_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS editorial_plans (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL,
        horizon_days INTEGER NOT NULL DEFAULT 30,
        goals_json JSON NOT NULL DEFAULT '[]',
        platforms_json JSON NOT NULL DEFAULT '[]',
        budget_usd REAL NOT NULL DEFAULT 0.0,
        spent_usd REAL NOT NULL DEFAULT 0.0,
        autonomy VARCHAR(20) NOT NULL DEFAULT 'RECOMMEND',
        constraints_json JSON NOT NULL DEFAULT '{}',
        status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS editorial_plan_items (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL,
        plan_id VARCHAR(36) NOT NULL,
        opportunity_id VARCHAR(36),
        campaign_id VARCHAR(36),
        schedule_entry_id VARCHAR(36),
        content_format VARCHAR(60) NOT NULL DEFAULT 'SHORT',
        angle VARCHAR(400) NOT NULL DEFAULT '',
        platforms_json JSON NOT NULL DEFAULT '[]',
        priority REAL NOT NULL DEFAULT 0.0,
        target_date TIMESTAMP,
        estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
        dependencies_json JSON NOT NULL DEFAULT '[]',
        status VARCHAR(20) NOT NULL DEFAULT 'IDEA',
        blocked_reason VARCHAR(400) NOT NULL DEFAULT '',
        why_json JSON NOT NULL DEFAULT '{}',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS production_capacity (
        id VARCHAR(36) PRIMARY KEY,
        workspace_id VARCHAR(36) NOT NULL,
        longform_per_week REAL NOT NULL DEFAULT 0.0,
        shorts_per_day REAL NOT NULL DEFAULT 0.0,
        ugc_per_day REAL NOT NULL DEFAULT 0.0,
        localization_per_day REAL NOT NULL DEFAULT 0.0,
        render_hours_per_day REAL NOT NULL DEFAULT 0.0,
        review_slots_per_day REAL NOT NULL DEFAULT 0.0,
        locale VARCHAR(40) NOT NULL DEFAULT '',
        notes VARCHAR(400) NOT NULL DEFAULT '',
        created_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        UNIQUE (workspace_id, locale)
    )
    """,
)

#: (index name, table, columns). Split out so every index is created against a
#: table that is actually present -- ``CREATE INDEX`` on a missing table is an
#: error on PostgreSQL (42704/42P01), and unlike ``ALTER TABLE`` it cannot be
#: skipped silently on SQLite either.
_INDEXES = (
    ("ix_trend_signal_ws_topic", "trend_signals", "workspace_id, topic_key"),
    ("ix_trend_signal_ws_observed", "trend_signals", "workspace_id, observed_at"),
    ("ix_editorial_plan_ws", "editorial_plans", "workspace_id"),
    ("ix_plan_item_plan", "editorial_plan_items", "plan_id"),
    ("ix_plan_item_ws_status", "editorial_plan_items", "workspace_id, status"),
    ("ix_plan_item_target", "editorial_plan_items", "target_date"),
    ("ix_opportunity_basis", "opportunities", "workspace_id, basis"),
    ("ix_capacity_ws", "production_capacity", "workspace_id"),
    ("ix_schedule_plan_item", "schedule_entries", "workspace_id, plan_item_id"),
)


def _tables(session) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(session.get_bind()).get_table_names())


def _columns(session, table: str) -> set[str]:
    from app.migrations.ddl import columns_of

    return columns_of(session, table)


def upgrade(session) -> None:
    from sqlalchemy import text

    for ddl in _NEW_TABLES:
        session.execute(text(ddl))
    # SQLite lacks ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so each column is
    # guarded by a catalog read instead of by catching the duplicate error.
    add_columns_if_missing(session, "opportunities", list(_OPPORTUNITY_COLUMNS))
    add_columns_if_missing(session, "schedule_entries", list(_SCHEDULE_COLUMNS))
    for name, table, columns in _INDEXES:
        if not table_exists(session, table):
            continue
        session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({columns})'))
    # Backfill: pre-Work-15 opportunities were produced by the cycle's scoring
    # over ingested evidence, which is an INFERENCE, not a direct observation.
    # Marking them so is more honest than leaving basis NULL and reading it
    # as OBSERVED.
    if table_exists(session, "opportunities"):
        session.execute(text(
            "UPDATE opportunities SET basis = 'INFERRED' "
            "WHERE basis IS NULL OR basis = ''"))


def _drop_indexes_on(session, table: str, column: str) -> None:
    """Drop every index referencing ``table.column``.

    SQLite refuses ``ALTER TABLE ... DROP COLUMN`` while any index still
    references the column, and the count is not predictable: the ORM's
    ``index=True`` creates one name and the explicit DDL another. Discovering
    them beats hardcoding, because a missing one makes the downgrade fail
    outright.
    """
    from sqlalchemy import text

    if table not in _tables(session):
        return
    for index in _indexes(session, table):
        if column in (index.get("column_names") or ()):
            session.execute(text(f'DROP INDEX IF EXISTS "{index["name"]}"'))


def _indexes(session, table: str) -> list[dict]:
    from sqlalchemy import inspect

    return inspect(session.get_bind()).get_indexes(table)


def downgrade(session) -> None:
    from sqlalchemy import text

    # Indexes FIRST: SQLite raises "error in index ... after drop column".
    for name, _ddl_type in _SCHEDULE_COLUMNS:
        _drop_indexes_on(session, "schedule_entries", name)
    for name, _ddl_type in _SCHEDULE_COLUMNS:
        if name in _columns(session, "schedule_entries"):
            session.execute(text(
                f'ALTER TABLE schedule_entries DROP COLUMN "{name}"'))
    for name, _ddl_type in _OPPORTUNITY_COLUMNS:
        _drop_indexes_on(session, "opportunities", name)
    for name, _ddl_type in _OPPORTUNITY_COLUMNS:
        if name in _columns(session, "opportunities"):
            session.execute(text(f'ALTER TABLE opportunities DROP COLUMN "{name}"'))
    for table in ("trend_signals", "editorial_plan_items", "editorial_plans",
                  "production_capacity"):
        if table in _tables(session):
            session.execute(text(f'DROP TABLE IF EXISTS "{table}"'))