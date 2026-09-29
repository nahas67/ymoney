"""Work 10 Section 0 — guarded job bootstrap in ``api/v1/inbox.py``.

The inbox module registers the community job handlers at import time inside
a per-target guard (``_bootstrap_community_jobs``): a missing or half-landed
sibling module must never break the API import, the healthy sibling still
registers, and repeat/reload calls are idempotent (the register functions
themselves skip duplicates).
"""
from __future__ import annotations

import importlib

from app.api.v1 import inbox as inbox_mod
from app.services import jobs as jobs_service

HANDLERS = (
    "COMMUNITY_SYNC",
    "INTERACTION_CLASSIFY",
    "COMMUNITY_DRAFT",
    "COMMUNITY_ACTION",
)


def test_bootstrap_registers_every_community_handler():
    # the app factory path imports the same module chain as the server
    import app.main  # noqa: F401

    missing = [key for key in HANDLERS if key not in jobs_service._handlers]
    assert not missing, f"community handlers not registered: {missing}"


def test_bootstrap_is_idempotent_on_repeat_calls():
    before = {key: jobs_service._handlers.get(key) for key in HANDLERS}
    # re-running the guarded registration must not raise on duplicate keys
    inbox_mod._bootstrap_community_jobs()
    inbox_mod._bootstrap_community_jobs()
    for key in HANDLERS:
        assert key in jobs_service._handlers, key
        assert jobs_service._handlers[key] is before[key], \
            f"{key} re-registered a different handler object"


def test_bootstrap_guard_survives_a_failing_sibling(monkeypatch):
    attempted: list[str] = []
    real_import_module = importlib.import_module

    class _FlakyImportlib:
        @staticmethod
        def import_module(name: str):
            attempted.append(name)
            if name == "app.engine.community.sync":
                raise RuntimeError("sibling lane mid-landing")
            return real_import_module(name)

    # module-global rebinding only — the real importlib stays intact
    monkeypatch.setattr(inbox_mod, "importlib", _FlakyImportlib)
    inbox_mod._bootstrap_community_jobs()  # must NOT raise

    assert "app.engine.community.sync" in attempted
    assert "app.engine.community.jobs" in attempted
    # the healthy sibling's handlers remain registered
    assert "COMMUNITY_DRAFT" in jobs_service._handlers
    assert "COMMUNITY_ACTION" in jobs_service._handlers
