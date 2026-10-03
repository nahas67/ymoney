"""Migrations are append-only; the 0003 pair is the single allowlisted exception."""
from __future__ import annotations


def test_no_new_colliding_sequence_prefixes():
    from app.migrations.runner import load_migrations

    mods = load_migrations()
    seen: dict[str, list[str]] = {}
    for name, _ in mods:
        prefix = name.split("_", 1)[0]
        if prefix.isdigit():
            seen.setdefault(prefix, []).append(name)
    collisions = {p: n for p, n in seen.items() if len(n) > 1}
    assert collisions == {"0003": ["0003_video_progress", "0003_video_thumbnails"]}, collisions


def test_migrations_apply_twice_without_error():
    from app.db import session_scope
    from app.migrations.runner import run_migrations

    with session_scope() as s:
        assert run_migrations(s) == []  # replay is a no-op


def test_0032_round_trips_upgrade_and_downgrade(tmp_path):
    """Work 15's migration must be reversible, not just applicable.

    The downgrade dropped ``schedule_entries.plan_item_id`` while an index still
    referenced it, and SQLite raises ``error in index ... after drop column``.
    The index count is not predictable (the ORM's ``index=True`` adds one name
    and the explicit DDL another), so the downgrade has to discover them.

    Runs against its OWN throwaway database on purpose: the test suite shares one
    session-scoped SQLite file, and a downgrade on that connection strips the
    schema out from under every test that runs afterwards.
    """

    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401  (registers every table)
    from app.db import Base

    engine = create_engine(f"sqlite:///{(tmp_path / 'mig.db').as_posix()}")
    session = sessionmaker(bind=engine)()
    try:
        Base.metadata.create_all(bind=engine)
        module = _load_0032()
        module.upgrade(session)
        session.commit()

        inspector = inspect(engine)
        assert "plan_item_id" in {c["name"] for c in
                                  inspector.get_columns("schedule_entries")}
        for table in ("trend_signals", "editorial_plans",
                      "editorial_plan_items", "production_capacity"):
            assert table in inspector.get_table_names()

        # replay safety: a second upgrade is a no-op
        module.upgrade(session)
        session.commit()

        # every pre-Work-15 opportunity is labelled, not left NULL
        unlabelled = session.execute(text(
            "SELECT COUNT(*) FROM opportunities "
            "WHERE basis IS NULL OR basis=''")).scalar()
        assert unlabelled == 0

        module.downgrade(session)
        session.commit()
        inspector = inspect(engine)
        assert "plan_item_id" not in {
            c["name"] for c in inspector.get_columns("schedule_entries")}
        assert "editorial_plans" not in inspector.get_table_names()
    finally:
        session.close()
        engine.dispose()


def _load_0032():
    """Import migration 0032 by path (the runner keys on filename)."""
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent
            / "app" / "migrations" / "versions"
            / "0032_autonomous_planning.py")
    spec = importlib.util.spec_from_file_location("m0032_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
