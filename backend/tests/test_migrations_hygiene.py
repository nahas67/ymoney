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
