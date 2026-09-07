"""Minimal, reliable, ordered migrations runner.

Migrations are plain Python modules in app/migrations/versions with an
`upgrade(session)` function. The runner tracks applied versions in the
schema_migrations table and applies pending ones inside transactions.
This avoids heavyweight tooling while keeping upgrades deterministic,
testable and safe across SQLite/Postgres.
"""

from __future__ import annotations

import importlib
import pkgutil

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.db import Base


def _ensure_meta(session: Session) -> None:
    session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version VARCHAR(64) PRIMARY KEY,"
            "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    session.commit()


def applied_versions(session: Session) -> set[str]:
    rows = session.execute(text("SELECT version FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def load_migrations() -> list[tuple[str, object]]:
    from app.migrations import versions as pkg

    mods = []
    for m in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"{pkg.__name__}.{m.name}")
        if hasattr(mod, "upgrade"):
            # module name convention: NNNN_description.py
            mods.append((m.name, mod))
    mods.sort(key=lambda t: t[0])
    return mods


def run_migrations(session: Session, *, create_missing_tables: bool = True) -> list[str]:
    """Apply all pending migrations. Returns list of applied versions."""
    _ensure_meta(session)
    applied = applied_versions(session)
    done: list[str] = []

    inspector = inspect(session.bind)

    if create_missing_tables:
        # Base tables first; migrations handle incremental changes after v1.
        Base.metadata.create_all(session.bind)

    for name, mod in load_migrations():
        if name in applied:
            continue
        logger.info(f"applying migration {name}")
        try:
            mod.upgrade(session)
            session.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": name}
            )
            session.commit()
            done.append(name)
        except Exception:
            session.rollback()
            logger.exception(f"migration {name} failed")
            raise
    if not inspector:
        pass
    return done
