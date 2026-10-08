"""Fresh real-PostgreSQL startup/migration/schema/replay proof, never pytest SQLite.

Internal child of run_release_postgres.py. Refuses any database outside the
disposable release namespace. The existing runner bootstraps via ORM create_all;
this proves the shipped fresh-install path, not a historical SQL-only upgrade.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from release_app import configure


def schema_snapshot(engine) -> dict:
    from sqlalchemy import inspect
    inspector = inspect(engine)
    snapshot = {}
    for table in sorted(inspector.get_table_names()):
        snapshot[table] = {
            "columns": [{"name": c["name"], "type": str(c["type"]),
                         "nullable": c["nullable"], "default": c.get("default")}
                        for c in inspector.get_columns(table)],
            "indexes": sorted(inspector.get_indexes(table), key=lambda i: i["name"]),
            "unique": sorted(inspector.get_unique_constraints(table), key=lambda i: i["name"] or ""),
            "foreign_keys": sorted(inspector.get_foreign_keys(table), key=lambda i: i["name"] or ""),
            "primary_key": inspector.get_pk_constraint(table),
        }
    return snapshot


def main() -> int:
    from sqlalchemy.engine import make_url
    url = make_url(os.environ["DATABASE_URL"])
    if (url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1"
            or not re.fullmatch(r"ymoney_release_[a-f0-9]{16}", url.database or "")):
        raise RuntimeError("refusing non-disposable PostgreSQL database")
    configure()
    import app.models  # noqa: F401
    from app.db import Base, engine, session_scope
    from app.migrations.runner import applied_versions, load_migrations, run_migrations
    from sqlalchemy import inspect, text

    if inspect(engine).get_table_names():
        raise RuntimeError("release database was not empty")
    expected = [name for name, _ in load_migrations()]
    if not expected:
        raise RuntimeError("no migration modules shipped; refusing empty proof")
    with session_scope() as session:
        fresh = run_migrations(session)
        versions = sorted(applied_versions(session))
    if fresh != expected or versions != expected:
        raise RuntimeError("fresh migration ledger differs from shipped migration modules")
    before = schema_snapshot(engine)
    missing = []
    for table in Base.metadata.sorted_tables:
        actual = before.get(table.name)
        if actual is None:
            missing.append(table.name)
            continue
        columns = {c["name"] for c in actual["columns"]}
        missing.extend(f"{table.name}.{c.name}" for c in table.columns if c.name not in columns)
        indexes = {i["name"] for i in actual["indexes"]}
        missing.extend(f"index:{i.name}" for i in table.indexes if i.name not in indexes)
    if missing:
        raise RuntimeError(f"schema is missing ORM tables/columns/indexes: {missing}")
    with session_scope() as session:
        stamps = session.execute(text("SELECT version, applied_at FROM schema_migrations ORDER BY version")).all()
        replay = run_migrations(session)
        if replay or stamps != session.execute(text(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version")).all():
            raise RuntimeError("migration replay changed the ledger")

    # Actual FastAPI lifespan, workers, readiness request, and clean shutdown.
    from app.main import app
    from fastapi.testclient import TestClient
    for _ in range(2):
        with TestClient(app) as client:
            if client.get("/health").json() != {"status": "ok"}:
                raise RuntimeError("application health failed")
            readiness = client.get("/readyz")
            if readiness.status_code != 200:
                raise RuntimeError(f"application readiness failed: {readiness.text}")
    after = schema_snapshot(engine)
    if before != after:
        raise RuntimeError("schema changed on startup/replay")
    with session_scope() as session:
        if sorted(applied_versions(session)) != expected:
            raise RuntimeError("startup changed migration ledger")
    encoded = json.dumps(after, sort_keys=True, default=str).encode()
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    report = {"dialect": engine.dialect.name, "fresh_count": len(fresh), "replay_count": len(replay),
              "versions": versions, "tables": len(after), "startup_count": 2,
              "schema_sha256": hashlib.sha256(encoded).hexdigest(),
              "bootstrap": "ORM create_all + ordered migrations (shipped runner)"}
    (output / "schema.json").write_text(json.dumps(after, indent=2, default=str) + "\n")
    (output / "proof.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
