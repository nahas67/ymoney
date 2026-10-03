"""Backup, restore and -- the part that matters -- PROVE the restore (Work 16 §12).

**A backup that has never been restored is not a backup.** It is a file. This
module exists because that sentence is the whole job: it takes a backup, and it
provides the drill that destroys an environment and brings it back, so the claim
"we can recover" is an observation rather than an intention. ``drill()`` is not
a plan -- it performs ``backup -> DROP DATABASE -> restore -> boot -> verify``
against real PostgreSQL and writes every command it ran, with its real output,
into ``drill.json`` next to the backup.

Why it lives in ``app/scripts/`` rather than ``backend/scripts/``: it imports
``app.core.config`` and ``app.models``, it is exercised by
``tests/test_work16_backup_restore.py``, and a container image built from this
repository has to be able to take and restore its own backup without a source
checkout. A directory that is not on ``sys.path`` gives none of that.

Three things are backed up, and the third is the one people get wrong.

1. **PostgreSQL**, with ``pg_dump -Fc``. ``-Fc`` is not a stylistic choice: the
   custom format is the only one ``pg_restore`` can restore *selectively* and
   the only one that carries the object index, so a restore can be a table
   rather than a whole-database gamble.

2. **Object-storage METADATA and configuration** -- the ``storage_objects``
   rows (key, checksum, size, state, what object it backs) plus the storage
   backend settings. This is the inventory, and an inventory without bytes is
   not a media backup: a restored ``FINALIZED`` row whose bytes are gone would
   be a database that lies about what it has. Which is why
   :func:`verify` also reports how many canonical objects point at bytes that
   were NOT part of this backup, and the runbook says so in bold.

3. **Secret REFERENCES, never secret values.** The names of the credential
   settings, and whether each one is configured. A backup containing a live API
   key is a second copy of the thing the key was supposed to protect, and a
   restore that silently re-injects one from disk is a restore nobody can audit.
   The reference list is what an operator needs to know they must re-inject.

**Integrity is verified by DIGEST, not by "the restore exited 0".** A restore
that produces an empty database exits 0. So the manifest carries, per table, a
row count and a SHA-256 over a deterministic projection of its rows, and
:func:`verify` recomputes them. The projections are chosen to be the things that
must never silently change:

* ``cost_entries`` -- amount, ``is_estimate``, and the Work 15.9 authority
  fields inside ``detail_json`` (``spend_authority``, ``actor_authority``,
  ``budget_source``, ``owned_workspace_id``). A ledger that came back with the
  amounts but not the authority picture is a ledger that cannot answer "who
  paid for this", and that question is the first one of any spend investigation.
* ``schema_migrations`` -- every version, because a restored database whose
  migration ledger is short will accept writes it should refuse.
* ``content_items`` lineage -- ``parent_content_id`` / ``root_content_id``, and
  every non-null link must resolve. A republish chain whose root no longer
  points anywhere is content that has quietly become original work.
* the paid-execution columns from Work 15.8/15.9 -- ``videos.cost_outcome``,
  ``videos.submission_operation_id``, ``lipsync_jobs.execution_outcome`` and
  ``lipsync_jobs.cost_outcome`` -- by value histogram, because "may this have
  been billed" has to survive a disaster, not just a restart.

RPO/RTO, honestly: RPO is the interval between backups (whatever cron says --
this module cannot make that number better) plus the dump duration, because the
dump is a point-in-time snapshot. RTO is measured by :func:`drill`, which
records the wall clock of every step; the number in ``docs/BACKUP_RESTORE_RUNBOOK.md``
is the one the drill actually produced, not an estimate.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.db import normalize_database_url

#: Bumped whenever the manifest's shape changes. A restore must refuse a
#: manifest it does not understand rather than half-read it.
MANIFEST_FORMAT = "ymoney.backup/1"

#: Name inside the backup directory. Fixed, so the restore command never has to
#: guess.
MANIFEST_NAME = "manifest.json"
DRILL_NAME = "drill.json"
STORAGE_INVENTORY_NAME = "storage_inventory.json"
STORAGE_REPORT_NAME = "storage_inventory.md"
DUMP_NAME = "database.pgc"

#: The deterministic per-table projections. ``ORDER BY`` the first column so the
#: digest does not depend on the physical row order a restore happens to
#: produce -- two servers will not agree on that otherwise, and a digest that
#: depends on insertion order verifies nothing.
#: Every column named here must exist; :func:`verify` reports a MISSING column
#: as a failure rather than skipping it, because a table that lost a column is
#: exactly the corruption this is looking for.
TABLE_DIGESTS: dict[str, tuple[str, ...]] = {
    "cost_entries": ("id", "workspace_id", "category", "amount_usd", "provider",
                     "is_estimate", "created_at"),
    "content_items": ("id", "workspace_id", "status", "parent_content_id",
                      "root_content_id", "derivation_type", "created_at"),
    "videos": ("id", "workspace_id", "status", "cost_outcome",
               "submission_operation_id", "submission_state", "provider_task_id"),
    "lipsync_jobs": ("id", "execution_outcome", "cost_outcome"),
    "storage_objects": ("id", "workspace_id", "object_key", "checksum",
                        "size_bytes", "state", "kind", "backend", "ref_type",
                        "ref_id"),
    "budget_rollup_limits": ("id", "scope", "workspace_id", "daily_total_cap",
                             "monthly_total_cap", "enabled"),
    # Work 16 A2: a RUNNING job with a live lease is ownership. Restoring
    # ``claimed_by='worker-3'`` with no ``lease_expires_at`` turns a reclaimable
    # row into an immortal one, so both are in the digest.
    "jobs": ("id", "workspace_id", "type", "status", "claimed_by",
             "lease_expires_at", "created_at"),
    "workspaces": ("id", "slug", "created_at"),
}

#: ``detail_json`` keys that make a ledger row self-describing (Work 15.9). The
#: digest is taken over exactly these, sorted by KEY, so a JSONB re-ordering on
#: a restore cannot fake a match and a genuinely changed value cannot hide.
AUTHORITY_KEYS: tuple[str, ...] = (
    "actor_authority", "budget_source", "charged_workspace_id",
    "cost_outcome", "operation_id", "owned_workspace_id", "remote_id",
    "spend_authority",
)

#: A secret is anything the settings call a credential. Matched on the field
#: NAME because that is the only list that exists; the point is that the backup
#: names what has to be re-injected, and never carries the value.
SECRET_NAME_MARKERS: tuple[str, ...] = (
    "api_key", "secret", "token", "password", "credential", "access_key",
)

#: Settings that are storage CONFIGURATION rather than secrets, and therefore
#: belong in the backup (as values).
STORAGE_CONFIG_FIELDS: tuple[str, ...] = (
    "storage_backend", "s3_endpoint_url", "s3_bucket", "s3_region",
    "s3_public_base_url", "storage_staging_dir", "storage_pending_ttl_seconds",
    "storage_stream_chunk_bytes",
)


class BackupError(RuntimeError):
    """A backup/restore step failed. Carries the command that failed."""


# ---------------------------------------------------------------------------
# process plumbing
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _run(command: list[str], *, container: str | None = None,
         env: dict[str, str] | None = None,
         timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a command, in the container when one is named.

    In container mode the argument list is passed to ``docker exec`` UNCHANGED --
    one list element per argv element, no ``sh -lc``. That is not decoration in
    either direction:

    * going through a shell requires the command string not to begin with a
      flag, and a ``pg_*`` invocation begins with nothing but flags. Wrapping it
      produced ``sh: 0: Illegal option -U`` -- an error that names neither the
      tool, the container nor the database;
    * quoting through a shell means an SQL string with a quote in it has to be
      re-quoted by hand, and this module has to be able to ask about a database
      named ``o'brien``.

    Docker stops parsing its own options at the first non-option argument, which
    is why :func:`_pg_cmd` puts the TOOL first.

    A failed command is a :class:`BackupError` with its own stderr attached. A
    backup tool that reports "done" when ``pg_dump`` exited 1 is worse than no
    tool: the operator finds out during an incident instead of during a drill.
    """
    full = (["docker", "exec", container] + list(command)
            if container else list(command))
    printable = " ".join(full)
    try:
        finished = subprocess.run(full, capture_output=True, text=True,
                                  timeout=timeout, check=False,
                                  env=env or None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"{printable} could not run: {exc}") from exc
    if finished.returncode != 0:
        raise BackupError(
            f"{printable} exited {finished.returncode}\n"
            f"stdout: {(finished.stdout or '').strip()[:800]}\n"
            f"stderr: {(finished.stderr or '').strip()[:800]}")
    return finished


def database_of(dsn: str) -> str:
    """The database name out of a DSN. Never the password: it is not logged."""
    name = (urlsplit(dsn).path or "/").lstrip("/")
    if not name:
        raise BackupError(f"no database name in DSN: {redact(dsn)}")
    return name


def with_database(dsn: str, database: str) -> str:
    """The same DSN pointing at a different database.

    Used by the drill to boot and verify against the RESTORED database while
    ``pg_dump``/``pg_restore`` keep talking to the server. Written as a URL edit
    rather than ``str.replace``: a replace would happily rewrite a password that
    happened to contain the source database name, and would leave the DSN
    untouched if the path had a trailing slash or none at all.
    """
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}",
                       parts.query, parts.fragment))


def redact(dsn: str) -> str:
    """A DSN with the password removed, for manifests and log lines.

    A manifest is copied into tickets and chat. A password in one of those is a
    credential leak with a backup-shaped filename.
    """
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return urlunsplit((parts.scheme, f"{user}@{host}" if user else host,
                       parts.path, parts.query, parts.fragment))


def _pg_env(dsn: str, *, container: str | None = None) -> list[str]:
    """``-h/-p/-U`` flags derived from the DSN, for the pg_* binaries.

    **Host and port are omitted in container mode.** Inside the container
    PostgreSQL listens on its own loopback port, and the DSN's host/port describe
    the PUBLISHED port on the Docker host -- passing them asks the server for a
    port it is not listening on. The role name is still passed, because the
    container's default role is ``postgres`` and this database is owned by
    ``ymoney``.

    ``PGPASSWORD`` is passed through the ENVIRONMENT rather than the command line
    so the password never appears in ``ps`` output on the host.
    """
    parts = urlsplit(dsn)
    flags: list[str] = []
    if not container:
        if parts.hostname:
            flags += ["-h", parts.hostname]
        if parts.port:
            flags += ["-p", str(parts.port)]
    if parts.username:
        flags += ["-U", parts.username]
    return flags


def _pg_cmd(dsn: str, tool: str, *args: str,
            container: str | None = None) -> list[str]:
    """``[tool, *flags, *args]`` -- the TOOL FIRST, always.

    The order is load-bearing in container mode. ``docker exec CONTAINER -U
    ymoney psql ...`` does not run psql: the Docker CLI has not yet seen a
    non-option argument, so it reads ``-U`` as one of its own flags and then
    tries to exec a program named ``-U``::

        OCI runtime exec failed: exec failed: unable to start container
        process: exec: "-U": executable file not found in $PATH

    Putting the tool first ends Docker's option parsing before any ``-h``/``-U``
    flag exists, so no shell wrapper is needed and an SQL string containing a
    quote still arrives as ONE argument.
    """
    return [tool, *_pg_env(dsn, container=container), *args]


def _env_with_password(dsn: str) -> dict[str, str]:
    env = dict(os.environ)
    parts = urlsplit(dsn)
    if parts.password:
        env["PGPASSWORD"] = parts.password
    return env


def _pg_auth(dsn: str, container: str | None) -> dict[str, str] | None:
    """``PGPASSWORD`` for LOCAL runs, ``None`` for container runs.

    Inside the container the connection is local and authenticated by the
    container's own ``pg_hba.conf``; exporting the host DSN's password there would
    be noise at best.
    """
    return None if container else _env_with_password(dsn)


def create_database(dsn: str, name: str, *, container: str | None = None) -> str:
    """``CREATE DATABASE``. Idempotent: an existing database is left alone.

    Idempotent because a restore that must be preceded by "make sure this
    database does not exist yet" is a restore that needs a human in the loop at
    the worst possible moment.
    """
    if not database_exists(dsn, name, container=container):
        _run(_pg_cmd(dsn, "createdb", name, container=container),
             container=container, env=_pg_auth(dsn, container))
    return name


def database_exists(dsn: str, name: str, *,
                    container: str | None = None) -> bool:
    """Whether ``name`` exists, asked of the server rather than guessed."""
    finished = _run(_pg_cmd(dsn, "psql", "-d", "postgres", "-tAc",
                            f"SELECT 1 FROM pg_database WHERE datname = '{name}'",
                            container=container),
                    container=container, env=_pg_auth(dsn, container))
    return finished.stdout.strip() == "1"


def drop_database(dsn: str, name: str, *, container: str | None = None,
                  force: bool = True) -> str:
    """``DROP DATABASE``. ``force=True`` ends the sessions still using it.

    Used by the drill to DESTROY the environment it is about to rebuild. It is
    the only command in this module that deletes data, which is exactly why it
    is named for what it does and never called by ``backup``.
    """
    args = ["dropdb"] + _pg_env(dsn, container=container)
    if force:
        args.append("--force")
    args.append(name)
    _run(args, container=container, env=_pg_auth(dsn, container))
    return name


# ---------------------------------------------------------------------------
# the dump itself
# ---------------------------------------------------------------------------


@dataclass
class CommandLog:
    """Every command the tool ran, with its real output.

    The drill's evidence is this list. A runbook that quotes commands nobody
    captured is a wish list.
    """

    entries: list[dict] = field(default_factory=list)

    def record(self, step: str, command: list[str], *,
               container: str | None = None, stdout: str = "",
               stderr: str = "", seconds: float = 0.0) -> None:
        full = (["docker", "exec", container] + list(command)
                if container else list(command))
        self.entries.append({
            "step": step, "command": full, "at": _now(),
            "seconds": round(float(seconds), 3),
            "returncode": 0,
            "stdout": (stdout or "")[:4000], "stderr": (stderr or "")[:2000],
        })

    def failed(self, step: str, command: list[str], exc: BackupError, *,
               container: str | None = None) -> None:
        full = (["docker", "exec", container] + list(command)
                if container else list(command))
        self.entries.append({
            "step": step, "command": full, "at": _now(), "returncode": 1,
            "seconds": 0.0, "stdout": "", "stderr": str(exc)[:2000],
        })


def pg_dump_to_file(dsn: str, database: str, destination: Path, *,
                    container: str | None = None,
                    log: CommandLog | None = None,
                    step: str = "pg_dump") -> dict:
    """``pg_dump -Fc`` the database to ``destination``.

    In container mode the dump is written INSIDE the container and copied out
    with ``docker cp``. That is not incidental: piping a custom-format dump
    through a PowerShell stdout redirect corrupts it, because the redirect is a
    text pipeline, and a backup that is corrupt on the way to disk is worse than
    no backup because it looks like one.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    remote = f"/tmp/{DUMP_NAME}"
    env = None if container else _env_with_password(dsn)
    if not container:
        destination.unlink(missing_ok=True)
    command = _pg_cmd(dsn, "pg_dump", "-d", database, "-Fc", "-f", remote,
                     container=container)
    started = time.perf_counter()
    finished = _run(command, container=container, env=env)
    copied: subprocess.CompletedProcess | None = None
    if container:
        # In container mode pg_dump wrote INSIDE the container, so the bytes have
        # to be copied out before the remote file is removed. `docker cp` is
        # binary-safe; a shell redirect through a text pipeline is not.
        copied = _run(["docker", "cp", f"{container}:{remote}",
                       str(destination)])
        _run(["rm", "-f", remote], container=container)
    if log is not None:
        log.record(step, command, container=container,
                   stdout=finished.stdout, stderr=finished.stderr,
                   seconds=time.perf_counter() - started)
        if copied is not None:
            log.record(f"{step}:copy",
                       ["docker", "cp", f"{container}:{remote}",
                        str(destination)],
                       stdout=copied.stdout, stderr=copied.stderr)
    if not destination.exists():
        raise BackupError(
            f"pg_dump reported success but {destination} does not exist -- "
            f"the backup was not written")
    return {"file": destination.name, "bytes": destination.stat().st_size,
            "sha256": sha256_of(destination), "format": "pg_dump -Fc",
            "tool": _pg_tool_version(dsn, container=container)}


def pg_restore_from_file(dsn: str, dump: Path, database: str, *,
                         container: str | None = None,
                         log: CommandLog | None = None,
                         step: str = "pg_restore") -> str:
    """``pg_restore`` a custom dump into an existing, EMPTY database."""
    remote = f"/tmp/{DUMP_NAME}"
    env = None if container else _env_with_password(dsn)
    started = time.perf_counter()
    if container:
        _run(["docker", "cp", str(dump), f"{container}:{remote}"])
    command = _pg_cmd(dsn, "pg_restore", "-d", database, "--no-owner",
                     "--no-privileges",
                     str(dump) if not container else remote, container=container)
    finished = _run(command, container=container, env=env)
    if container:
        _run(["rm", "-f", remote], container=container)
    if log is not None:
        log.record(step, command, container=container,
                   stdout=finished.stdout, stderr=finished.stderr,
                   seconds=time.perf_counter() - started)
    return (finished.stdout or "").strip()


def _pg_tool_version(dsn: str, *, container: str | None = None) -> str:
    """``pg_dump --version``, recorded in the manifest.

    A dump that cannot say which tool wrote it is a dump whose restorability is a
    guess: a 17.x ``pg_dump`` refuses to restore into an older server, and an
    operator reading a manifest should not have to guess which side of that
    boundary they are on.
    """
    try:
        finished = _run(["pg_dump", "--version"], container=container)
    except BackupError:
        return "unknown"
    return (finished.stdout or "").strip() or "unknown"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# session plumbing
# ---------------------------------------------------------------------------


def session_for(dsn: str):
    """A SQLAlchemy session on ``dsn``, normalising the driver.

    ``normalize_database_url`` is used because a bare ``postgresql://`` URL
    makes SQLAlchemy reach for psycopg2, which this project does not ship -- the
    same reasoning ``app.db`` documents.
    """
    engine = create_engine(normalize_database_url(dsn), pool_pre_ping=True,
                           future=True)
    return engine, Session(engine)


def _scalar(session: Session, sql: str, **params):
    return session.execute(text(sql), params).scalar()


def _table_exists(session: Session, table: str) -> bool:
    """Whether ``table`` exists, asked of the CATALOG rather than guessed.

    Through SQLAlchemy's inspector, not ``to_regclass``: that function is
    PostgreSQL-only, and this repository's default deployment is SQLite, so a
    ``to_regclass`` probe made the entire SQLite backup path fail on the first
    table it looked at.
    """
    return bool(inspect(session.get_bind()).has_table(table))


def _digest_rows(session: Session, table: str, columns: tuple[str, ...]) -> str:
    """SHA-256 over the ordered rows of ``columns``.

    Deterministic for a given DATABASE and a given DIALECT: ordered by the key
    column, and every value rendered through :func:`_render` so a float, a bool,
    a timestamp and NULL each have exactly one spelling. A digest that renders
    ``True`` and ``'True'`` the same way on two servers is a digest that verifies
    nothing.

    Not portable across dialects, and it does not pretend to be: SQLite hands
    back ``1`` where PostgreSQL hands back ``true``, so a digest taken on one will
    not match the other. That is fine -- the manifest and the restore are on the
    same engine -- but it is the reason a cross-engine comparison has to compare
    row COUNTS, not digests.
    """
    projection = ", ".join(f'"{c}"' for c in columns)
    rows = session.execute(text(
        f'SELECT {projection} FROM "{table}" ORDER BY "{columns[0]}"')).all()
    digest = hashlib.sha256()
    for row in rows:
        digest.update("|".join(_render(value) for value in row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _render(value) -> str:
    """One canonical spelling per value type. See :func:`_digest_rows`."""
    if value is None:
        return "\x00NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


def _authority_digest(session: Session) -> dict:
    """Digest of the Work 15.9 authority picture carried in ``detail_json``.

    One sorted projection per ledger row, so key order in the JSON cannot affect
    the answer but a changed authority CAN. Rows with no authority at all (plain
    ``track_cost``) are counted, not mixed in: a ledger where every row claims to
    be system-owned is as broken as one where none do.
    """
    rows = session.execute(text(
        "SELECT id, detail_json FROM cost_entries ORDER BY id")).all()
    digest = hashlib.sha256()
    with_authority = 0
    for row_id, detail in rows:
        payload = detail if isinstance(detail, dict) else (
            json.loads(detail) if isinstance(detail, str) and detail.strip() else {})
        picture = tuple(f"{key}={payload.get(key, '')}" for key in AUTHORITY_KEYS)
        if any(p.split("=", 1)[1] for p in picture):
            with_authority += 1
        digest.update(f"{row_id}|".encode())
        digest.update("|".join(picture).encode())
        digest.update(b"\n")
    return {"rows": len(rows), "rows_with_authority": with_authority,
            "digest": digest.hexdigest()}


def _money_summary(session: Session) -> dict:
    """The ledger's own arithmetic, recomputed rather than trusted."""
    totals = session.execute(text(
        "SELECT COALESCE(SUM(amount_usd), 0), COUNT(*), "
        "COUNT(*) FILTER (WHERE is_estimate), "
        "COUNT(*) FILTER (WHERE NOT is_estimate), "
        "COUNT(*) FILTER (WHERE COALESCE(amount_usd,0) = 0) "
        "FROM cost_entries")).one()
    return {"total_usd": round(float(totals[0] or 0.0), 6),
            "rows": int(totals[1] or 0),
            "estimates": int(totals[2] or 0),
            "actuals": int(totals[3] or 0),
            "zero_amount_rows": int(totals[4] or 0)}


def _migration_versions(session: Session) -> list[str]:
    if not _table_exists(session, "schema_migrations"):
        return []
    return sorted(str(row[0]) for row in session.execute(
        text("SELECT version FROM schema_migrations")).all())


def _lineage_summary(session: Session) -> dict:
    """Lineage that still points somewhere.

    ``content_items``'s value is that a republish chain is traceable, so the
    number that matters is not "how many rows have a parent" but "how many of
    those parents EXIST". A restore that loses half the rows would otherwise
    still report a healthy lineage count.
    """
    if not _table_exists(session, "content_items"):
        return {"present": False}
    totals = session.execute(text(
        "SELECT COUNT(*), "
        "COUNT(*) FILTER (WHERE parent_content_id IS NOT NULL), "
        "COUNT(*) FILTER (WHERE root_content_id IS NOT NULL), "
        "COUNT(*) FILTER (WHERE parent_content_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM content_items p "
        "                  WHERE p.id = content_items.parent_content_id)), "
        "COUNT(*) FILTER (WHERE root_content_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM content_items r "
        "                  WHERE r.id = content_items.root_content_id)) "
        "FROM content_items")).one()
    return {"present": True, "rows": int(totals[0] or 0),
            "with_parent": int(totals[1] or 0),
            "with_root": int(totals[2] or 0),
            "dangling_parent_links": int(totals[3] or 0),
            "dangling_root_links": int(totals[4] or 0)}


def _paid_execution_summary(session: Session) -> dict:
    """The Work 15.8/15.9 columns, by value histogram.

    Grouping by value rather than sampling rows is deliberate: the operator
    question is "how many renders may have been billed and are unreconciled",
    which is a COUNT over ``cost_outcome = 'UNKNOWN_EXPOSURE'`` -- and that count
    has to survive a disaster.
    """
    out: dict = {}
    for table, columns in (("videos",
                            ("cost_outcome", "submission_state",
                             "submission_operation_id")),
                           ("lipsync_jobs", ("execution_outcome", "cost_outcome"))):
        if not _table_exists(session, table):
            out[table] = {"present": False}
            continue
        entry: dict = {"present": True, "rows": 0, "histograms": {}}
        entry["rows"] = int(_scalar(session,
                                    f'SELECT COUNT(*) FROM "{table}"') or 0)
        for column in columns:
            counts = session.execute(text(
                f'SELECT COALESCE("{column}", \'\'), COUNT(*) FROM "{table}" '
                f'GROUP BY 1 ORDER BY 1')).all()
            entry["histograms"][column] = {str(value or ""): int(number)
                                           for value, number in counts}
        out[table] = entry
    return out


def _storage_inventory(session: Session) -> dict:
    """Every canonical stored object, as metadata. NEVER the bytes."""
    if not _table_exists(session, "storage_objects"):
        return {"present": False, "objects": []}
    rows = session.execute(text(
        "SELECT id, workspace_id, object_key, checksum, size_bytes, "
        "content_type, kind, state, backend, temp_path, ref_type, ref_id, "
        "finalized_at, expires_at, meta_json FROM storage_objects "
        "ORDER BY id")).all()
    objects = [dict(zip(
        ("id", "workspace_id", "object_key", "checksum", "size_bytes",
         "content_type", "kind", "state", "backend", "temp_path", "ref_type",
         "ref_id", "finalized_at", "expires_at", "meta_json"), row, strict=True))
        for row in rows]
    states: dict[str, int] = {}
    for entry in objects:
        states[str(entry["state"])] = states.get(str(entry["state"]), 0) + 1
    return {"present": True, "objects": objects, "count": len(objects),
            "by_state": states,
            "bytes_declared": sum(int(o["size_bytes"] or 0) for o in objects)}


def storage_inventory_markdown(dsn: str, *, limit: int = 500) -> str:
    """The inventory as MARKDOWN, with each object's BYTES resolved or not.

    This is the re-materialisation record. The backup deliberately does not carry
    media bytes, so the honest thing it can offer is: *key, sha256, size, and is
    the file actually on this volume right now*. Without the last column the
    inventory is a list of things that were once true, and a ``FINALIZED`` row
    whose bytes were lost reads identically to a healthy one.

    Verifying a checksum means reading the file, so this is bounded by ``limit``
    and SAYS how many it did not check. A report that quietly checked the first
    twenty of nine thousand files and printed no caveat is a report that will be
    believed.

    Only meaningful for the LOCAL backend. An S3 deployment resolves nothing
    here; those rows are reported as ``REMOTE`` rather than ``MISSING``, because
    "this process cannot see the bucket" is not "the object is gone".
    """
    from app.services import storage_objects as objects_service

    engine, session = session_for(dsn)
    try:
        inventory = _storage_inventory(session)
        rows: list[tuple[str, str, int, str, str]] = []
        checked = 0
        skipped = 0
        for entry in inventory.get("objects", []):
            state = str(entry.get("state") or "")
            if state != "FINALIZED":
                rows.append((str(entry.get("object_key") or ""), state, 0,
                             str(entry.get("checksum") or ""), "NOT_FINALIZED"))
                continue
            backend = str(entry.get("backend") or "local")
            if backend != "local":
                rows.append((str(entry.get("object_key") or ""), state,
                             int(entry.get("size_bytes") or 0),
                             str(entry.get("checksum") or ""), f"REMOTE:{backend}"))
                continue
            if checked >= limit:
                skipped += 1
                rows.append((str(entry.get("object_key") or ""), state,
                             int(entry.get("size_bytes") or 0),
                             str(entry.get("checksum") or ""), "NOT_CHECKED"))
                continue
            checked += 1
            rows.append((str(entry.get("object_key") or ""), state,
                         int(entry.get("size_bytes") or 0),
                         str(entry.get("checksum") or ""),
                         _object_bytes_status(entry, objects_service)))
    finally:
        session.close()
        engine.dispose()

    lines = [
        "# Object storage inventory (METADATA ONLY -- bytes are NOT in the backup)",
        "",
        f"- captured_at: {_now()}",
        f"- objects: {inventory.get('count', 0)}"
        f" ({json.dumps(inventory.get('by_state', {}), sort_keys=True)})",
        f"- declared bytes: {inventory.get('bytes_declared', 0)}",
        f"- checksums verified against the volume: {checked}",
        f"- NOT checked (over the limit of {limit}): {skipped}",
        "",
        "| object_key | state | size_bytes | sha256 | bytes on this volume |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {key} | {state} | {size} | {checksum or '-'} | {status} |"
              for key, state, size, checksum, status in rows]
    lines += ["",
              "`PRESENT` means the file exists and its sha256 matches the row. "
              "`CORRUPT` means it exists with different bytes -- that is a "
              "storage incident, not a backup incident. `MISSING` means a "
              "FINALIZED row with no bytes, which must be re-materialised from "
              "the renderer before the media is servable again."]
    return "\n".join(lines) + "\n"


def _object_bytes_status(entry: dict, objects_service) -> str:
    """``PRESENT`` / ``CORRUPT`` / ``MISSING`` / ``UNRESOLVED`` for one row."""
    try:
        path = objects_service.resolve(str(entry.get("workspace_id") or ""),
                                       str(entry.get("object_key") or ""))
    except Exception as exc:  # noqa: BLE001 - an unresolvable key is a finding
        return f"UNRESOLVED:{type(exc).__name__}"
    if not path.exists():
        return "MISSING"
    expected = str(entry.get("checksum") or "")
    if not expected:
        return "PRESENT_NO_CHECKSUM"
    return "PRESENT" if sha256_of(path) == expected else "CORRUPT"


def _secret_references() -> dict:
    """Which credentials must be RE-INJECTED after a restore. Never their values.

    The name and a boolean are the whole point: an operator reading a manifest
    needs to know "nine credentials are configured" and nothing else, because a
    manifest carrying the values is a credential store wearing a backup's
    filename.

    Only STRING settings are listed. The name markers also match
    ``access_token_expire_minutes`` and ``refresh_token_expire_days``, which are
    numbers, not credentials -- and a manifest that claims a number is a
    credential it does not have to re-inject is a manifest that trains the
    operator to trust a list that is padded with lies.
    """
    from app.core.config import settings

    references = []
    for name in sorted(type(settings).model_fields):
        lowered = name.lower()
        if not any(marker in lowered for marker in SECRET_NAME_MARKERS):
            continue
        try:
            value = getattr(settings, name)
        except Exception:  # noqa: BLE001 - a broken field is "not configured"
            continue
        if not isinstance(value, str):
            continue
        references.append({"setting": name, "configured": bool(value.strip())})
    return {"policy": "names-and-presence-only; secret VALUES are never backed up",
            "count": len(references),
            "configured": sum(1 for r in references if r["configured"]),
            "references": references}


def _storage_config() -> dict:
    from app.core.config import settings

    out = {}
    for name in STORAGE_CONFIG_FIELDS:
        try:
            out[name] = getattr(settings, name)
        except Exception:  # noqa: BLE001
            out[name] = None
    return out


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def _current_database(session: Session) -> str:
    """The database this session is actually connected to.

    Asked of the server, not of the DSN, because a DSN can name one database and
    the connection can be to another (``search_path``, a proxy, a ``replication``
    DSN). SQLite has no ``current_database()``, so the file is read out of
    ``PRAGMA database_list`` -- the same question, asked the dialect's way.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        rows = session.execute(text("PRAGMA database_list")).all()
        return str(rows[0][2]) if rows else ""
    return str(_scalar(session, "SELECT current_database()") or "")


def collect_canonical(session: Session) -> dict:
    """Everything :func:`verify` will later check, captured at backup time."""
    tables: dict[str, dict] = {}
    for table, columns in TABLE_DIGESTS.items():
        if not _table_exists(session, table):
            tables[table] = {"present": False}
            continue
        tables[table] = {"present": True, "digest": _digest_rows(session, table,
                                                                  columns),
                         "columns": list(columns),
                         "rows": int(_scalar(session,
                                             f'SELECT COUNT(*) FROM "{table}"') or 0)}
    versions = _migration_versions(session)
    has_ledger = _table_exists(session, "cost_entries")
    return {
        "tables": tables,
        "cost_entries": _money_summary(session) if has_ledger
        else {"present": False},
        "authority": _authority_digest(session) if has_ledger
        else {"present": False},
        "schema_migrations": {"count": len(versions), "versions": versions},
        "content_items": _lineage_summary(session),
        "paid_execution": _paid_execution_summary(session),
    }


def backup(dsn: str, out_dir: Path | str, *, container: str | None = None,
           log: CommandLog | None = None) -> dict:
    """Take a backup into ``out_dir`` and write ``manifest.json``.

    Returns the manifest. Raises :class:`BackupError` rather than returning a
    partial one: a manifest that describes a dump which is not there is how a
    restore discovers its backup is missing during an incident.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    journal = log if log is not None else CommandLog()
    database = database_of(dsn)

    dump_info = pg_dump_to_file(dsn, database, out_dir / DUMP_NAME,
                                container=container, log=journal)
    engine, session = session_for(dsn)
    try:
        canonical = collect_canonical(session)
        inventory = _storage_inventory(session)
        version = str(_scalar(session, "SELECT version()") or "")
    finally:
        session.close()
        engine.dispose()

    (out_dir / STORAGE_INVENTORY_NAME).write_text(
        json.dumps({"objects": inventory, "config": _storage_config(),
                    "captured_at": _now()}, indent=2, default=str),
        encoding="utf-8")
    # The operator-facing half of the same inventory: which bytes are ACTUALLY
    # on the volume. Written outside the session because it re-opens its own
    # connection to resolve paths.
    storage_report = storage_inventory_markdown(dsn)
    (out_dir / STORAGE_REPORT_NAME).write_text(storage_report, encoding="utf-8")
    byte_status: dict[str, int] = {}
    for line in storage_report.splitlines():
        if not line.startswith("| ") or line.startswith("| object_key"):
            continue
        for token in line.rsplit("|", 2)[-2:-1] or [""]:
            key = token.strip()
            if key in {"PRESENT", "PRESENT_NO_CHECKSUM", "CORRUPT", "MISSING",
                       "NOT_CHECKED", "NOT_FINALIZED"} or key.startswith(
                           ("REMOTE:", "UNRESOLVED:")):
                byte_status[key.split(":", 1)[0]] = (
                    byte_status.get(key.split(":", 1)[0], 0) + 1)
    manifest = {
        "format": MANIFEST_FORMAT,
        "created_at": _now(),
        "source": {"dsn": redact(dsn), "database": database,
                   "server_version": version},
        "dump": dump_info,
        "canonical": canonical,
        "storage_metadata": {
            "file": STORAGE_INVENTORY_NAME,
            "report": STORAGE_REPORT_NAME,
            "objects": inventory.get("count", 0),
            "by_state": inventory.get("by_state", {}),
            "bytes_by_status": byte_status,
            "note": ("METADATA ONLY. The bytes live in the storage volume/bucket "
                     "and are NOT in this backup. See "
                     f"{STORAGE_REPORT_NAME} for which of them are present now."),
        },
        "secret_references": _secret_references(),
        "commands": journal.entries,
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2,
                                                   default=str), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def read_manifest(backup_dir: Path | str) -> dict:
    manifest = json.loads((Path(backup_dir) / MANIFEST_NAME).read_text(
        encoding="utf-8"))
    if manifest.get("format") != MANIFEST_FORMAT:
        raise BackupError(
            f"manifest format {manifest.get('format')!r} is not "
            f"{MANIFEST_FORMAT!r}; refusing to half-restore it")
    return manifest


def restore(backup_dir: Path | str, dsn: str, target_database: str, *,
            container: str | None = None, log: CommandLog | None = None,
            replace: bool = False) -> dict:
    """Restore a backup into ``target_database``.

    ``target_database`` must NOT be the source: this tool will ``DROP`` it when
    ``replace=True``, and a restore that can quietly overwrite the database it is
    recovering from is a restore that can destroy the last good copy. The check
    is a refusal, not a warning.
    """
    backup_dir = Path(backup_dir)
    manifest = read_manifest(backup_dir)
    source_database = manifest["source"]["database"]
    if target_database == source_database:
        raise BackupError(
            f"refusing to restore into the source database {source_database!r}; "
            f"a restore that can overwrite the database it recovers from can "
            f"destroy the last good copy. Pass a different --target-db.")
    journal = log if log is not None else CommandLog()
    dump = backup_dir / str(manifest["dump"]["file"])
    if not dump.exists():
        raise BackupError(f"the dump named by the manifest is missing: {dump}")
    actual = sha256_of(dump)
    if actual != manifest["dump"]["sha256"]:
        raise BackupError(
            f"dump checksum mismatch for {dump.name}: manifest says "
            f"{manifest['dump']['sha256'][:16]}..., file is {actual[:16]}... "
            f"-- the backup is corrupt or truncated")

    if database_exists(dsn, target_database, container=container):
        if not replace:
            raise BackupError(
                f"database {target_database!r} already exists; pass replace=True "
                f"to drop and rebuild it")
        drop_database(dsn, target_database, container=container)
    create_database(dsn, target_database, container=container)
    pg_restore_from_file(dsn, dump, target_database, container=container,
                         log=journal)
    return manifest


def boot(dsn: str, *, timeout: int = 300) -> str:
    """Boot the application against ``dsn`` in a fresh process.

    A restored database that the app cannot open is not a restore, it is a file
    with tables in it. Three things are checked, because "the import worked" is
    the weakest of them:

    1. ``create_app()`` builds the application against the RESTORED database;
    2. ``run_migrations`` finds **nothing pending** -- a restored database whose
       ledger the runner considers incomplete is a database that would migrate on
       first boot, i.e. a database that is already changed from what was backed
       up;
    3. the ORM reads canonical rows through the application's own session
       factory, which is the connection the running server would use.

    Run in a SUBPROCESS so this is a real process start with a real
    ``DATABASE_URL`` and no engine inherited from the tool's own run.

    ``len(app.routes)`` is NOT the route count on this FastAPI version -- the
    included routers are one lazy entry each -- so the number reported is the
    resolved OpenAPI path count, which cannot flatter itself.
    """
    snippet = (
        "from app.main import create_app\n"
        "from app.db import session_scope\n"
        "from app.migrations.runner import run_migrations\n"
        "from sqlalchemy import text\n"
        "app = create_app()\n"
        "paths = len(app.openapi().get('paths') or {})\n"
        "with session_scope() as s:\n"
        "    pending = run_migrations(s)\n"
        "    ws = s.execute(text('SELECT COUNT(*) FROM workspaces')).scalar()\n"
        "    mig = s.execute(text('SELECT COUNT(*) FROM schema_migrations')).scalar()\n"
        "    led = s.execute(text('SELECT COUNT(*) FROM cost_entries')).scalar()\n"
        "print('BOOT OK paths=%d workspaces=%s migrations=%s ledger_rows=%s "
        "pending=%s' % (paths, ws, mig, led, pending))\n")
    env = dict(os.environ)
    env["DATABASE_URL"] = dsn
    finished = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                              text=True, env=env, timeout=timeout, check=False,
                              cwd=str(Path(__file__).resolve().parents[2]))
    if finished.returncode != 0:
        raise BackupError(
            f"the application did not boot against {redact(dsn)} "
            f"(exit {finished.returncode})\n"
            f"{(finished.stderr or '').strip()[-1500:]}")
    output = (finished.stdout or "").strip()
    if "pending=[]" not in output:
        raise BackupError(
            f"the restored database has MIGRATIONS PENDING, so booting it would "
            f"change it from what was backed up: {output}")
    return output


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    expected: object
    observed: object
    detail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.name, "ok": bool(self.ok),
                "expected": self.expected, "observed": self.observed,
                "detail": self.detail}


@dataclass
class IntegrityReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def add(self, name: str, ok: bool, expected: object, observed: object,
            detail: str = "") -> None:
        self.checks.append(Check(name=name, ok=ok, expected=expected,
                                observed=observed, detail=detail))

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks],
                "failed": len(self.failures)}


def verify(dsn: str, manifest: dict, *, database: str | None = None) -> IntegrityReport:
    """Recompute every digest in ``manifest`` against ``dsn``.

    Compares against the MANIFEST, not against "a non-empty table": a restore
    that produced the wrong rows passes a sanity check and fails this one, which
    is the difference between a restore that ran and a restore that worked.
    """
    report = IntegrityReport()
    expected_tables = manifest["canonical"]["tables"]
    engine, session = session_for(dsn)
    try:
        if database:
            observed = _current_database(session)
            report.add("connected_to_expected_database", observed == database,
                       database, observed)
        for table, wanted in expected_tables.items():
            if not wanted.get("present"):
                report.add(f"table:{table}", True, "absent at backup time",
                           "absent")
                continue
            if not _table_exists(session, table):
                report.add(f"table:{table}", False, "present",
                           "MISSING after restore")
                continue
            rows = int(_scalar(session, f'SELECT COUNT(*) FROM "{table}"') or 0)
            report.add(f"rows:{table}", rows == wanted["rows"], wanted["rows"],
                       rows, f"{table} row count")
            digest = _digest_rows(session, table, tuple(wanted["columns"]))
            report.add(f"digest:{table}", digest == wanted["digest"],
                       wanted["digest"][:16], digest[:16],
                       f"{table} row digest over {', '.join(wanted['columns'])}")

        money_wanted = manifest["canonical"]["cost_entries"]
        if money_wanted.get("present") is False:
            report.add("cost_entries", True, "absent at backup time", "absent")
        else:
            money = _money_summary(session)
            for field_name in ("total_usd", "rows", "estimates", "actuals",
                               "zero_amount_rows"):
                report.add(f"cost_entries.{field_name}",
                           money[field_name] == money_wanted[field_name],
                           money_wanted[field_name], money[field_name],
                           "the money ledger, recomputed")

            authority_wanted = manifest["canonical"]["authority"]
            authority = _authority_digest(session)
            report.add("cost_entries.authority_digest",
                       authority["digest"] == authority_wanted["digest"],
                       authority_wanted["digest"][:16], authority["digest"][:16],
                       "Work 15.9 spend/actor authority inside detail_json")
            report.add("cost_entries.rows_with_authority",
                       authority["rows_with_authority"]
                       == authority_wanted["rows_with_authority"],
                       authority_wanted["rows_with_authority"],
                       authority["rows_with_authority"])

        wanted_versions = set(manifest["canonical"]["schema_migrations"]["versions"])
        observed_versions = set(_migration_versions(session))
        missing = sorted(wanted_versions - observed_versions)
        report.add("schema_migrations.all_versions_present", not missing,
                   sorted(wanted_versions), sorted(observed_versions),
                   f"missing after restore: {missing}")
        # Count as well as membership. A database with EXTRA versions is a
        # database migrated by something other than this codebase, and the
        # membership check above is silent about that; it is exactly the case
        # where "every version is present" and "the schema is the schema we
        # backed up" disagree.
        report.add("schema_migrations.version_count",
                   len(observed_versions)
                   == manifest["canonical"]["schema_migrations"]["count"],
                   manifest["canonical"]["schema_migrations"]["count"],
                   len(observed_versions),
                   f"extra versions after restore: "
                   f"{sorted(observed_versions - wanted_versions)}")

        lineage_wanted = manifest["canonical"]["content_items"]
        lineage = _lineage_summary(session)
        if lineage_wanted.get("present"):
            report.add("content_items.lineage_links",
                       lineage["with_parent"] == lineage_wanted["with_parent"]
                       and lineage["with_root"] == lineage_wanted["with_root"],
                       {"with_parent": lineage_wanted["with_parent"],
                        "with_root": lineage_wanted["with_root"]},
                       {"with_parent": lineage["with_parent"],
                        "with_root": lineage["with_root"]},
                       "parent_content_id / root_content_id counts")
            report.add("content_items.no_dangling_lineage",
                       lineage["dangling_parent_links"] == 0
                       and lineage["dangling_root_links"] == 0,
                       0, {"parent": lineage["dangling_parent_links"],
                           "root": lineage["dangling_root_links"]},
                       "every lineage link still resolves to a row")

        for table, wanted in manifest["canonical"]["paid_execution"].items():
            observed = _paid_execution_summary(session).get(table, {})
            if not wanted.get("present"):
                continue
            report.add(f"paid_execution.{table}.histograms",
                       observed.get("histograms") == wanted["histograms"],
                       wanted["histograms"], observed.get("histograms"),
                       "cost_outcome / execution_outcome / submission_state")
    finally:
        session.close()
        engine.dispose()
    return report


# ---------------------------------------------------------------------------
# SQLite (the docker-compose deployment really does run this)
# ---------------------------------------------------------------------------


def backup_sqlite(database_path: Path | str, out_dir: Path | str) -> dict:
    """A consistent SQLite backup via the online backup API.

    Not a file copy: copying a live database file while a writer holds it gives
    you a torn snapshot, and ``PRAGMA integrity_check`` is run afterwards so a
    torn snapshot cannot be filed as a backup.

    Every connection is opened with ``closing()``. ``with sqlite3.connect(...)``
    is a TRANSACTION context manager, not a closer -- it commits on exit and
    leaves the handle open, which on Windows makes the next ``unlink`` of that
    file fail with a sharing violation. That is how a working backup tool becomes
    a backup tool whose own restore step cannot replace the file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = Path(database_path)
    if not source.exists():
        raise BackupError(f"no SQLite database at {source}")
    destination = out_dir / "database.sqlite3"
    with (contextlib.closing(sqlite3.connect(str(source))) as reader,
          contextlib.closing(sqlite3.connect(str(destination))) as writer):
        reader.backup(writer)
    with contextlib.closing(sqlite3.connect(str(destination))) as check:
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity.lower() != "ok":
        raise BackupError(f"the SQLite backup failed its own integrity check: {integrity}")
    engine, session = session_for(f"sqlite:///{destination.as_posix()}")
    try:
        canonical = collect_canonical(session)
        inventory = _storage_inventory(session)
    finally:
        session.close()
        engine.dispose()
    (out_dir / STORAGE_INVENTORY_NAME).write_text(
        json.dumps({"objects": inventory, "config": _storage_config(),
                    "captured_at": _now()}, indent=2, default=str),
        encoding="utf-8")
    storage_report = storage_inventory_markdown(f"sqlite:///{destination.as_posix()}")
    (out_dir / STORAGE_REPORT_NAME).write_text(storage_report, encoding="utf-8")
    manifest = {
        "format": MANIFEST_FORMAT, "created_at": _now(),
        "source": {"dsn": f"sqlite:///{source.as_posix()}",
                   "database": source.name, "server_version": "sqlite"},
        "dump": {"file": destination.name, "bytes": destination.stat().st_size,
                 "sha256": sha256_of(destination), "format": "sqlite backup API",
                 "tool": "sqlite3.Connection.backup"},
        "canonical": canonical,
        "storage_metadata": {"file": STORAGE_INVENTORY_NAME,
                             "report": STORAGE_REPORT_NAME,
                             "objects": inventory.get("count", 0),
                             "by_state": inventory.get("by_state", {}),
                             "note": ("METADATA ONLY; the bytes are not "
                                      "included. See "
                                      f"{STORAGE_REPORT_NAME}.")},
        "secret_references": _secret_references(),
        "commands": [],
    }
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2,
                                                    default=str), encoding="utf-8")
    return manifest


def restore_sqlite(backup_dir: Path | str, target_path: Path | str) -> Path:
    """Replace ``target_path`` with the backup, atomically where possible."""
    backup_dir = Path(backup_dir)
    manifest = read_manifest(backup_dir)
    dump = backup_dir / str(manifest["dump"]["file"])
    if sha256_of(dump) != manifest["dump"]["sha256"]:
        raise BackupError("the SQLite backup's checksum does not match its manifest")
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    staged = target_path.with_suffix(target_path.suffix + ".restoring")
    shutil.copyfile(dump, staged)
    target_path.unlink(missing_ok=True)
    staged.replace(target_path)
    return target_path


# ---------------------------------------------------------------------------
# the drill
# ---------------------------------------------------------------------------


@dataclass
class DrillResult:
    ok: bool
    steps: list[dict]
    report: dict
    manifest: dict
    path: Path

    def to_dict(self) -> dict:
        return {"ok": self.ok, "steps": self.steps, "verification": self.report,
                "backup_dir": str(self.path)}


def drill(dsn: str, out_dir: Path | str, *, container: str | None = None,
          restored_database: str = "",
          mutation: dict | None = None) -> DrillResult:
    """backup -> DESTROY -> restore -> boot -> verify, for real.

    ``mutation`` names a canonical row to corrupt between the backup and the
    destroy (``{"table": "cost_entries", "set": {"amount_usd": 999.0},
    "where": {"id": "..."}}``). That is the step that makes this a test rather
    than a demo: without it, a restore that silently restored nothing would look
    identical to one that worked, because both leave an empty table and an empty
    table raises no error.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    journal = CommandLog()
    source = database_of(dsn)
    target = restored_database or f"{source}_restored_{uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    steps: list[dict] = []

    def step(name: str, detail: str = "") -> None:
        steps.append({"step": name, "at": _now(), "detail": detail,
                      "elapsed_s": round(time.perf_counter() - started, 3)})

    manifest = backup(dsn, out_dir, container=container, log=journal)
    step("backup", f"{manifest['dump']['bytes']} bytes, "
                   f"sha256 {manifest['dump']['sha256'][:16]}")

    mutation_result = None
    if mutation:
        mutation_result = apply_mutation(dsn, **mutation)
        journal.record("mutate", ["sql"] + [f"{k}={v}" for k, v in
                                            (mutation.get("set") or {}).items()],
                       stdout=json.dumps(mutation_result, default=str))
        step("mutate", json.dumps(mutation_result, default=str))

    drop_database(dsn, source, container=container)
    journal.record("drop_source",
                   _pg_cmd(dsn, "dropdb", "--force", source, container=container),
                   container=container)
    gone = not database_exists(dsn, source, container=container)
    step("destroy", f"source database {source} exists afterwards: {gone}")
    if not gone:
        raise BackupError(f"the drill could not destroy {source}; refusing to "
                          f"claim a restore was proven")

    restore(out_dir, dsn, target, container=container, log=journal, replace=True)
    step("restore", f"restored into {target}")

    # Boot and verify against the TARGET database, not ``dsn``. The source was
    # dropped two steps ago, so a verify that kept the source DSN would either
    # connect to nothing (and report a driver error, not an integrity answer)
    # or -- worse -- pass against the wrong database.
    target_dsn = with_database(dsn, target)
    booted = boot(target_dsn)
    journal.record("boot", [sys.executable, "-c", "create_app()"], stdout=booted)
    step("boot", booted)

    report = verify(target_dsn, manifest, database=target)
    step("verify", f"{len(report.checks)} checks, "
                   f"{len(report.failures)} failed")
    result = DrillResult(ok=report.ok, steps=steps, report=report.to_dict(),
                         manifest=manifest, path=out_dir)
    (out_dir / DRILL_NAME).write_text(
        json.dumps({**result.to_dict(), "commands": journal.entries,
                    "mutation": mutation_result,
                    "finished_at": _now(),
                    "elapsed_s": round(time.perf_counter() - started, 3)},
                   indent=2, default=str), encoding="utf-8")
    return result


def apply_mutation(dsn: str, *, table: str, set: dict, where: dict) -> dict:  # noqa: A002
    """Corrupt one canonical row. Used only to prove the restore brought it back.

    ``set`` shadows the builtin on purpose only in the keyword name, because the
    caller reads it as data, not as Python. A restricted table allow-list is not
    decoration: this function exists to destroy a value, and a tool that can
    destroy an arbitrary table is a tool that will.
    """
    if table not in {"cost_entries", "content_items", "videos", "lipsync_jobs"}:
        raise BackupError(f"refusing to mutate {table!r}: not a canonical table")
    assignments = ", ".join(f'"{k}" = :{k}' for k in set)
    # ONE condition string, quoted, used by all three statements. Built once so
    # the "before" read can never disagree with the UPDATE it is meant to
    # describe -- the previous version re-derived it by stripping quotes, which
    # is the kind of difference that makes a mutation report a "before" that no
    # row ever had.
    conditions = " AND ".join(f'"{k}" = :w_{k}' for k in where)
    where_params = {f"w_{k}": v for k, v in where.items()}
    sql = (f'UPDATE "{table}" SET {assignments} WHERE {conditions} RETURNING id'
           if _table_has_returning(dsn, table)
           else f'UPDATE "{table}" SET {assignments} WHERE {conditions}')
    engine, session = session_for(dsn)
    try:
        before = session.execute(
            text(f'SELECT * FROM "{table}" WHERE {conditions}'), where_params
        ).mappings().first()
        params = {**set, **where_params}
        result = session.execute(text(sql), params)
        session.commit()
        after = session.execute(
            text(f'SELECT * FROM "{table}" WHERE {conditions}'), where_params
        ).mappings().first()
        return {"table": table, "where": where, "set": set,
                "before": dict(before) if before else None,
                "after": dict(after) if after else None,
                "rows_updated": int(result.rowcount or 0)}
    finally:
        session.close()
        engine.dispose()


def _table_has_returning(dsn: str, table: str) -> bool:
    """Whether ``UPDATE ... RETURNING`` is available (PostgreSQL yes, SQLite no)."""
    return urlsplit(dsn).scheme.startswith("postgres")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from app.core.config import settings

    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.backup_restore",
        description="Backup, restore, verify and DRILL the YMONEY backup.")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""),
                        help="PostgreSQL DSN, e.g. postgresql://user:pw@host/db")
    parser.add_argument(
        "--container",
        default=(os.environ.get("YMONEY_PG_CONTAINER", "")
                 or settings.backup_pg_container),
        help="run pg_dump/pg_restore inside this docker container")
    parser.add_argument("--out", default="", help="backup directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="write a backup into --out")

    restore_parser = sub.add_parser("restore", help="restore into --target-db")
    restore_parser.add_argument("--target-db", required=True)
    restore_parser.add_argument("--replace", action="store_true",
                                help="drop the target database first")
    restore_parser.add_argument("--from", dest="source_dir", default="")

    verify_parser = sub.add_parser("verify", help="verify a restored database")
    verify_parser.add_argument("--from", dest="source_dir", default="")
    verify_parser.add_argument("--database", default="")

    drill_parser = sub.add_parser(
        "drill", help="backup -> destroy -> restore -> boot -> verify")
    drill_parser.add_argument("--target-db", default="")
    drill_parser.add_argument("--mutate-table", default="")
    drill_parser.add_argument("--mutate-column", default="")
    drill_parser.add_argument("--mutate-value", default="")
    drill_parser.add_argument("--mutate-id", default="")
    return parser


def settings_default_backup_dir() -> str:
    """``settings.backup_dir``, isolated from import time.

    Imported lazily so the module stays importable in a process whose settings
    cannot be constructed (a restore run from a bare container is exactly that
    process, and it is the one that must not fail on an unrelated setting).
    """
    try:
        from app.core.config import settings

        return settings.backup_dir
    except Exception:  # noqa: BLE001 - a fallback path must not raise
        return "./backup"


def is_sqlite(dsn: str) -> bool:
    """Whether ``dsn`` names a SQLite file rather than a server.

    The CLI dispatches on this because a SQLite deployment is the DEFAULT here
    (``Settings.database_url`` ships SQLite), and a ``backup`` subcommand that
    shells out to ``pg_dump`` against a file path fails with a message about a
    missing executable rather than about the database.
    """
    return urlsplit(str(dsn or "")).scheme.startswith("sqlite")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out or settings_default_backup_dir())
    source_dir = Path(getattr(args, "source_dir", "") or out_dir)
    try:
        if args.command == "backup":
            if is_sqlite(args.dsn):
                manifest = backup_sqlite(urlsplit(args.dsn).path.lstrip("/")
                                         or args.dsn, out_dir)
            else:
                manifest = backup(args.dsn, out_dir, container=args.container or None)
            print(f"backup written to {out_dir}: {manifest['dump']['bytes']} bytes, "
                  f"sha256 {manifest['dump']['sha256']}")
            return 0
        if args.command == "restore":
            if is_sqlite(args.dsn) and not args.target_db.endswith(".sqlite3"):
                # A file path as the target: the SQLite restore, not a database.
                target = restore_sqlite(source_dir, args.target_db)
                print(f"restored {source_dir} -> {target}")
                return 0
            manifest = restore(source_dir, args.dsn, args.target_db,
                               container=args.container or None,
                               replace=bool(args.replace))
            print(f"restored {manifest['source']['database']} -> "
                  f"{args.target_db}")
            return 0
        if args.command == "verify":
            manifest = read_manifest(source_dir)
            report = verify(args.dsn, manifest,
                            database=args.database or None)
            print(json.dumps(report.to_dict(), indent=2, default=str))
            return 0 if report.ok else 1
        if args.command == "drill":
            if is_sqlite(args.dsn):
                raise BackupError(
                    "drill is not available for a SQLite DSN: the drill proves a "
                    "restore by DROPPING a database and re-creating it, which "
                    "SQLite has no equivalent of. Use test_sqlite_backup_"
                    "round_trip_replaces_the_file for the SQLite round trip.")
            mutation = None
            if args.mutate_table and args.mutate_column and args.mutate_id:
                raw = args.mutate_value
                mutation = {"table": args.mutate_table,
                            "set": {args.mutate_column:
                                    float(raw) if _looks_numeric(raw) else raw},
                            "where": {"id": args.mutate_id}}
            result = drill(args.dsn, out_dir, container=args.container or None,
                           restored_database=args.target_db, mutation=mutation)
            print(json.dumps(result.to_dict(), indent=2, default=str))
            return 0 if result.ok else 1
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 2
    return 2


def _looks_numeric(raw: str) -> bool:
    try:
        float(raw)
    except (TypeError, ValueError):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
