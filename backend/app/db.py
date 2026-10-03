"""Database engine and session management (SQLAlchemy 2.x)."""

from __future__ import annotations

import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def normalize_database_url(url: str) -> str:
    """Pin the Postgres driver instead of letting SQLAlchemy guess.

    SQLAlchemy maps a bare ``postgresql://`` to **psycopg2**, and a driver that
    is not installed turns a perfectly good Postgres DSN into
    ``ModuleNotFoundError: No module named 'psycopg2'`` at import time -- the
    failure surfaces while building the engine, long before anything explains
    itself. This project ships psycopg 3 (the ``psycopg`` package), so a bare
    scheme is rewritten to name it explicitly.

    An operator who genuinely wants psycopg2 can still ask for it with the
    explicit ``postgresql+psycopg2://`` scheme; only the *unqualified* form is
    redirected. Other dialects and an already-qualified URL are untouched.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine(url: str | None = None):
    url = normalize_database_url(url or settings.database_url)
    if url.startswith("sqlite"):
        # Ensure the sqlite file's parent directory exists (default lives in data/).
        db_path = url.split("///", 1)[-1]
        if db_path and not db_path.startswith(":"):
            from pathlib import Path

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False}
        # SQLite's file lock, not a pool, is the concurrency limit. Queueing is
        # better than failing fast, so a caller waits briefly rather than being
        # told the database is busy the instant it is.
        pool_kwargs: dict = {"pool_size": settings.db_pool_size,
                             "max_overflow": 0,
                             "pool_timeout": settings.db_pool_timeout}
    else:
        connect_args = {}
        # Postgres authority (Work 16 §1/§7): a real pool with real bounds.
        # `pool_pre_ping` already discards connections a proxy or failover has
        # killed underneath us; `pool_recycle` retires ones a load balancer has
        # already timed out, which pre_ping alone cannot see.
        pool_kwargs: dict = {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_timeout": settings.db_pool_timeout,
            "pool_recycle": settings.db_pool_recycle,
        }

    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True,
                           **pool_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Session:
    """Transactional scope; commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def escape_like(raw: str) -> str:
    """Escape LIKE wildcards so user search text matches literally.

    Without this, a search for "100%" matches anything containing "100"
    (% = any run) and "_" matches any single character. Read-only impact,
    but wrong results are wrong results.
    """
    return (
        (raw or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


class _Local(threading.local):
    session: Session | None = None


_request_local = _Local()


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
