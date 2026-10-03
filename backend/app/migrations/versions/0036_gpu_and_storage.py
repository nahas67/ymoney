"""Upgrade 0036: GPU devices, GPU reservations, canonical storage objects.

Work 16 §4 and §5. Three new tables, no altered table, no backfill.

``gpu_devices``
    A device and its VRAM. Capacity lives in a ROW rather than in a config
    value because capacity is per-machine: the same deployment has a 4 GB
    laptop card and a 80 GB datacenter card, and a config constant cannot be
    right for both. ``reserved_mb`` is deliberately denormalised -- deriving it
    with ``SUM(...) WHERE status IN (...)`` turns admission into check-then-act
    and two processes both admit the last 4 GB. See ``services/gpu_scheduler``.

``gpu_reservations``
    One job's claim on part of a device, with a LEASE. Without it a worker that
    dies mid-render holds VRAM until somebody notices by watching the card; with
    it, the slot's expiry is proof of absence, exactly as in ``jobs`` (0035).
    ``job_id`` is what lets recovery ask the lease model instead of guessing:
    a live job keeps its slot, a dead one loses it on the spot.

``storage_objects``
    The canonical record of one stored object. ``state`` is the invariant:
    ``PENDING`` is a promise (an upload is in flight) and can never be read as
    content; only ``FINALIZED`` names bytes that exist. ``temp_path`` exists so
    a temporary file can be TRACKED without being canonical -- it is never
    served, and ``services.storage_objects`` refuses to finalize from inside
    the staging root.

Nothing here guesses at existing data. There is no prior notion of a device or
of a canonical object in the schema, so there is nothing to migrate -- a
backfill inventing a synthetic ``cpu`` device with a made-up capacity would be
a number no operator chose and every later admission decision would rest on.

Reversible: the downgrade drops every index before its table. There is no
``DROP COLUMN`` here because there are no new columns on existing tables, but
the index-before-table order is kept anyway -- it is the order SQLite and
PostgreSQL both require when the reverse would leave a broken object behind,
and a downgrade that only works on one backend is a downgrade nobody has run.
"""

from __future__ import annotations

from app.migrations.ddl import create_index_if_missing, index_exists, table_exists

#: (table, column, DDL type) for every column this migration adds. The empty
#: string is the convention throughout the schema for "nobody has set this
#: yet", which is an honest reading and beats a NULL that every reader must
#: COALESCE. ``checksum`` is the exception that matters: it is EMPTY while a row
#: is PENDING, because there are no bytes to hash yet, and that emptiness is
#: exactly what distinguishes a promise from content.
_NEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("gpu_devices", "device_key", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("gpu_devices", "name", "VARCHAR(80) NOT NULL DEFAULT ''"),
    ("gpu_devices", "backend", "VARCHAR(20) NOT NULL DEFAULT 'cpu'"),
    ("gpu_devices", "total_mb", "INTEGER NOT NULL DEFAULT 0"),
    ("gpu_devices", "reserved_mb", "INTEGER NOT NULL DEFAULT 0"),
    ("gpu_devices", "enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("gpu_devices", "meta_json", "JSON NOT NULL DEFAULT '{}'"),
    ("gpu_reservations", "device_id", "VARCHAR(36) NOT NULL DEFAULT ''"),
    ("gpu_reservations", "workspace_id", "VARCHAR(36) NULL"),
    ("gpu_reservations", "job_id", "VARCHAR(36) NULL"),
    ("gpu_reservations", "kind", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("gpu_reservations", "job_type", "VARCHAR(60) NOT NULL DEFAULT ''"),
    ("gpu_reservations", "vram_mb", "INTEGER NOT NULL DEFAULT 0"),
    ("gpu_reservations", "priority", "INTEGER NOT NULL DEFAULT 100"),
    ("gpu_reservations", "status", "VARCHAR(15) NOT NULL DEFAULT 'RESERVED'"),
    ("gpu_reservations", "cpu_fallback", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("gpu_reservations", "acquired_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ("gpu_reservations", "lease_expires_at", "TIMESTAMP NULL"),
    ("gpu_reservations", "heartbeat_at", "TIMESTAMP NULL"),
    ("gpu_reservations", "released_at", "TIMESTAMP NULL"),
    ("gpu_reservations", "release_reason", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("storage_objects", "workspace_id", "VARCHAR(36) NOT NULL DEFAULT ''"),
    ("storage_objects", "object_key", "VARCHAR(300) NOT NULL DEFAULT ''"),
    ("storage_objects", "checksum", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("storage_objects", "size_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("storage_objects", "content_type", "VARCHAR(120) NOT NULL DEFAULT ''"),
    ("storage_objects", "kind", "VARCHAR(20) NOT NULL DEFAULT 'source'"),
    ("storage_objects", "state", "VARCHAR(15) NOT NULL DEFAULT 'PENDING'"),
    ("storage_objects", "backend", "VARCHAR(20) NOT NULL DEFAULT 'local'"),
    ("storage_objects", "temp_path", "VARCHAR(400) NOT NULL DEFAULT ''"),
    ("storage_objects", "ref_type", "VARCHAR(30) NOT NULL DEFAULT ''"),
    ("storage_objects", "ref_id", "VARCHAR(36) NOT NULL DEFAULT ''"),
    ("storage_objects", "finalized_at", "TIMESTAMP NULL"),
    ("storage_objects", "expires_at", "TIMESTAMP NULL"),
    ("storage_objects", "last_accessed_at", "TIMESTAMP NULL"),
    ("storage_objects", "meta_json", "JSON NOT NULL DEFAULT '{}'"),
)

#: Explicit composite indexes for the three questions these tables are read by.
#: The single-column ORM ``index=True`` names are created separately by
#: ``create_all`` and are DISCOVERED on downgrade -- dropping only the known
#: ones leaves a broken object behind on SQLite.
_INDEXES: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    ("ix_gpu_device_key", "gpu_devices", ("device_key",), True),
    ("ix_gpu_device_enabled", "gpu_devices", ("enabled",), False),
    ("ix_gpu_res_device_state", "gpu_reservations", ("device_id", "status"), False),
    ("ix_gpu_res_lease", "gpu_reservations", ("status", "lease_expires_at"), False),
    ("ix_gpu_res_ws", "gpu_reservations", ("workspace_id", "status"), False),
    # The uniqueness that makes a retried upload idempotent rather than a
    # second copy of the same logical object.
    ("ix_storage_object_key", "storage_objects",
     ("workspace_id", "object_key"), True),
    ("ix_storage_object_state", "storage_objects", ("state", "workspace_id"), False),
    ("ix_storage_object_expiry", "storage_objects", ("state", "expires_at"), False),
)

#: Tables this migration owns. ``gpu_reservations.device_id`` and
#: ``storage_objects.workspace_id`` are declared WITHOUT foreign keys on
#: purpose: a reservation must survive the job row it referenced being purged
#: (it is audit evidence of where VRAM went), and a storage object must survive
#: its workspace row in a restore-from-backup that ran out of order. The ORM
#: declares the FK for new databases; this DDL does not, and the mismatch is
#: deliberate rather than an oversight.
_TABLES: tuple[str, ...] = ("gpu_devices", "gpu_reservations", "storage_objects")


def _create_table(session, table: str) -> None:
    """``CREATE TABLE IF NOT EXISTS`` for one of this migration's tables.

    ``create_all`` builds the CURRENT schema before migrations run, so on a
    fresh install these already exist and this is a no-op. The catalog read is
    what makes it one, and it is correct on both backends -- catching the
    duplicate error instead would ABORT the transaction on PostgreSQL and every
    later statement would die with 25P02 (see ``app/migrations/ddl.py``).
    """
    from sqlalchemy import text

    if not table_exists(session, "gpu_devices") and table == "gpu_devices":
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS gpu_devices ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY,"
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "device_key VARCHAR(40) NOT NULL DEFAULT '',"
            "name VARCHAR(80) NOT NULL DEFAULT '',"
            "backend VARCHAR(20) NOT NULL DEFAULT 'cpu',"
            "total_mb INTEGER NOT NULL DEFAULT 0,"
            "reserved_mb INTEGER NOT NULL DEFAULT 0,"
            "enabled BOOLEAN NOT NULL DEFAULT TRUE,"
            "meta_json JSON NOT NULL DEFAULT '{}')"))
    if table_exists(session, "gpu_devices") and table == "gpu_devices":
        return

    if table == "gpu_reservations":
        if not table_exists(session, "gpu_devices"):
            return  # a trimmed deployment: nothing to reserve against
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS gpu_reservations ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY,"
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "device_id VARCHAR(36) NOT NULL, "
            "workspace_id VARCHAR(36), "
            "job_id VARCHAR(36), "
            "kind VARCHAR(40) NOT NULL DEFAULT '', "
            "job_type VARCHAR(60) NOT NULL DEFAULT '', "
            "vram_mb INTEGER NOT NULL DEFAULT 0, "
            "priority INTEGER NOT NULL DEFAULT 100, "
            "status VARCHAR(15) NOT NULL DEFAULT 'RESERVED', "
            "cpu_fallback BOOLEAN NOT NULL DEFAULT FALSE, "
            "acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "lease_expires_at TIMESTAMP, "
            "heartbeat_at TIMESTAMP, "
            "released_at TIMESTAMP, "
            "release_reason VARCHAR(40) NOT NULL DEFAULT '')"))
    elif table == "storage_objects":
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS storage_objects ("
            "id VARCHAR(36) NOT NULL PRIMARY KEY,"
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            "workspace_id VARCHAR(36) NOT NULL DEFAULT '', "
            "object_key VARCHAR(300) NOT NULL DEFAULT '', "
            "checksum VARCHAR(64) NOT NULL DEFAULT '', "
            "size_bytes INTEGER NOT NULL DEFAULT 0, "
            "content_type VARCHAR(120) NOT NULL DEFAULT '', "
            "kind VARCHAR(20) NOT NULL DEFAULT 'source', "
            "state VARCHAR(15) NOT NULL DEFAULT 'PENDING', "
            "backend VARCHAR(20) NOT NULL DEFAULT 'local', "
            "temp_path VARCHAR(400) NOT NULL DEFAULT '', "
            "ref_type VARCHAR(30) NOT NULL DEFAULT '', "
            "ref_id VARCHAR(36) NOT NULL DEFAULT '', "
            "finalized_at TIMESTAMP, "
            "expires_at TIMESTAMP, "
            "last_accessed_at TIMESTAMP, "
            "meta_json JSON NOT NULL DEFAULT '{}')"))


def upgrade(session) -> None:
    from app.migrations.ddl import add_columns_if_missing

    for table in _TABLES:
        _create_table(session, table)
    # Belt and braces: a deployment whose ``gpu_devices`` predates this
    # migration (there is none today, but the shape of the check is the point)
    # still gets every column.
    for table, column, ddl_type in _NEW_COLUMNS:
        if table_exists(session, table):
            add_columns_if_missing(session, table, [(column, ddl_type)])
    for name, table, cols, unique in _INDEXES:
        if not table_exists(session, table):
            continue  # trimmed deployment; nothing to index
        create_index_if_missing(session, name, table, ", ".join(cols), unique=unique)


def _indexes_on(session, table: str, columns: tuple[str, ...]) -> list[str]:
    """Every index on ``table`` naming one of ``columns``.

    Discovered, not hardcoded: the ORM's ``index=True`` creates one family of
    names while the explicit DDL above creates another, and dropping only the
    known ones makes the downgrade fail outright on SQLite. Read up front,
    before any DDL, because every catalog read after the first DROP is another
    round trip inside a transaction.
    """
    from sqlalchemy import inspect

    if not table_exists(session, table):
        return []
    found: list[str] = []
    for index in inspect(session.get_bind()).get_indexes(table):
        if any(col in (index.get("column_names") or ()) for col in columns):
            found.append(str(index["name"]))
    return found


def downgrade(session) -> None:
    """Indexes FIRST, then tables. Never the other way round.

    SQLite refuses ``DROP TABLE``'s dependents in an unspecified order and
    PostgreSQL silently keeps an index whose table is gone; dropping indexes
    explicitly is the one order that works on both.
    """
    from sqlalchemy import text

    for name, table, cols, _unique in _INDEXES:
        for discovered in {name, *_indexes_on(session, table, cols)}:
            if index_exists(session, table, discovered):
                session.execute(text(f'DROP INDEX IF EXISTS "{discovered}"'))
    for table in reversed(_TABLES):
        if table_exists(session, table):
            session.execute(text(f'DROP TABLE IF EXISTS "{table}"'))