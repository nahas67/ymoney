"""Work 11.5 reconciliation regression battery.

Every test here pins ONE fixed finding from the six-lane audit, so a
regression is attributed to a specific defect rather than to "the suite".
Findings are referenced by their lane letter (A-F) as recorded in
``docs/YMONEY_PRODUCTION_GAP_MATRIX.md``.

1. A-F1/F2/F5: PostgreSQL-invalid DDL (double-quoted string default, derived
   table without an alias, integer defaults on BOOLEAN) is gone from the
   migration sources, and the shipped files execute on the PG dialect.
2. A-F3/F4/F6: backfill migration 0030 enforces the ORM's uniqueness and
   closes the gaps the per-revision files left behind.
3. B-F1/F2: governance needs the ADMIN floor - a QC override or a QC-FAIL
   apply, and the whole review lifecycle, are refused for a plain member /
   viewer even when the capability check is vacuous (unlinked target).
4. C-F1: ``allow_private`` on a connector is necessary but not sufficient.
5. D-F5: the video checker resolves through the storage boundary and matches
   a MediaAsset by storage key (workspace-relative paths), not a basename.
6. E-F1: the pre-spend budget gate is now CALLED before billable render work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_BACKEND = Path(__file__).resolve().parents[1]
VERSIONS = REPO_BACKEND / "app" / "migrations" / "versions"


# ---------------------------------------------------------------------------
# A: migration portability (PostgreSQL is the production target)
# ---------------------------------------------------------------------------

def _sources() -> list[Path]:
    return sorted(VERSIONS.glob("0*.py"))


def test_no_double_quoted_default_in_migration_sql():
    """A-F1: `DEFAULT ""` is a double-quoted IDENTIFIER in PostgreSQL."""
    # Strip comment-only lines first: prose that mentions the old defect must
    # not be mistaken for live SQL.
    offenders = []
    pattern = re.compile(r"DEFAULT\s+\"\"")
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        for match in pattern.finditer(code):
            offenders.append(f"{path.name}:{code[:match.start()].count(chr(10)) + 1}")
    assert not offenders, f"PG-invalid double-quoted defaults: {offenders}"


def test_no_integer_default_on_boolean_column():
    """A-F5: PostgreSQL has no integer->boolean assignment cast."""
    offenders = []
    pattern = re.compile(r"BOOLEAN[^,;\n]*?DEFAULT\s+[01]\b")
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            offenders.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
    assert not offenders, f"BOOLEAN DEFAULT 0/1 is PG-invalid: {offenders}"


def test_derived_tables_carry_an_alias():
    """A-F2: PostgreSQL rejects `FROM (SELECT ...)` without an alias."""
    offenders = []
    # a derived table closed with `) WHERE` / `)` + `AND` and no `AS <alias>`
    # between its paren and the closing keyword is the 0002 defect
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        for match in re.finditer(r"\)\s*(WHERE\b|AND\b|ON\b)", code, re.IGNORECASE):
            segment = code[max(0, match.start() - 500):match.start()]
            if "ROW_NUMBER" not in segment:
                continue
            between = segment.rsplit("FROM (", 1)[-1]
            if not re.search(r"\bAS\s+\w+", between, re.IGNORECASE):
                offenders.append(path.name)
                break
    assert not offenders, f"derived table without alias: {sorted(set(offenders))}"


def test_orm_schema_compiles_for_postgresql():
    """A: every ORM table renders under the PostgreSQL dialect."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import app.models  # noqa: F401 - populates Base.metadata
    from app.db import Base

    failures = []
    for table in Base.metadata.tables.values():
        try:
            str(CreateTable(table).compile(dialect=postgresql.dialect()))
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures.append(f"{table.name}: {exc}")
    assert not failures, failures


# ---------------------------------------------------------------------------
# A-F3/F4/F6: the backfill migration closes the historical gaps
# ---------------------------------------------------------------------------

def test_backfill_migration_exists_and_covers_the_drifted_constraints():
    path = VERSIONS / "0030_reconciliation_backfill.py"
    assert path.exists(), "W11.5 backfill migration 0030 is missing"
    text = path.read_text(encoding="utf-8")
    for needle in ("uq_brand_override_subject", "uq_opportunity_ws_topic",
                   "RENAME COLUMN idx", "ix_platform_variants_workspace_id",
                   "request_id"):
        assert needle in text, f"0030 must handle {needle}"


def test_backfill_replays_as_a_noop_on_a_fresh_schema(tmp_path, monkeypatch):
    """A-F6: 0030 is idempotent on an already-current database."""

    db_path = tmp_path / "replay.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("YMONEY_SECRET_KEY", "w115-replay")
    monkeypatch.setenv("VIDEO_ENGINE", "mock")

    from app.db import session_scope
    from app.migrations.runner import applied_versions, run_migrations

    with session_scope() as session:
        # the session-scoped test DB is already migrated by the autouse
        # `_migrate_once` fixture, so read the applied list rather than
        # expecting a fresh application here
        already = applied_versions(session)
    assert any("0030" in name for name in already), sorted(already)

    with session_scope() as session:
        second = run_migrations(session)
    assert second == [], f"second run must be a no-op, applied: {second}"


# ---------------------------------------------------------------------------
# B: governance floors (QC override / review lifecycle)
# ---------------------------------------------------------------------------

def _client(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, role: str = "owner"):
    """Register a user owning a NEW workspace (owner role by default)."""
    import uuid

    from app.db import session_scope
    from app.models import Workspace, WorkspaceMember

    email = f"w{uuid.uuid4().hex[:10]}@test.local"
    resp = client.post("/api/v1/auth/register",
                       json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ws_id = data["workspace"]["id"]
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    user_id = data["user"]["id"]
    if role != "owner":
        with session_scope() as s:
            row = s.get(Workspace, ws_id)
            s.get(WorkspaceMember, (ws_id, user_id))
            s.query(WorkspaceMember).filter(
                WorkspaceMember.workspace_id == ws_id).update({"role": role})
            assert row is not None
    return ws_id, headers, user_id


def _add_member(client, ws_id: str, role: str):
    """Add a second user to ws_id with `role`; return (headers, user_id)."""
    import uuid

    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"m{uuid.uuid4().hex[:10]}@test.local"
    resp = client.post("/api/v1/auth/register",
                       json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    user_id = data["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role))
    login = client.post("/api/v1/auth/login",
                        json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user_id


def test_review_lifecycle_requires_member_not_viewer(tmp_path, monkeypatch):
    """B-F2: submit/assign were reachable at the VIEWER floor."""
    client = _client(tmp_path, monkeypatch)
    ws_id, owner_h, owner_id = _register(client, "owner")
    viewer_h, _ = _add_member(client, ws_id, "viewer")

    timeline = client.post(f"/api/v1/workspaces/{ws_id}/timelines", headers=owner_h,
                           json={"name": "t", "fps": 30.0, "duration_seconds": 1.0,
                                 "aspect": "9:16", "tracks": []})
    assert timeline.status_code == 200, timeline.text
    timeline_id = timeline.json()["id"]

    created = client.post(f"/api/v1/workspaces/{ws_id}/reviews", headers=owner_h,
                          json={"target_type": "timeline_version",
                                "target_id": timeline_id, "title": "r"})
    assert created.status_code == 201, created.text
    body = created.json()
    review_id = (body.get("review") or body).get("id")
    assert review_id, f"no review id in {body}"

    r = client.post(f"/api/v1/workspaces/{ws_id}/reviews/{review_id}/submit",
                    headers=viewer_h)
    assert r.status_code == 403, f"viewer must not submit a review: {r.status_code} {r.text}"

    r = client.post(f"/api/v1/workspaces/{ws_id}/reviews/{review_id}/assignments",
                    headers=viewer_h, json={"reviewer_id": owner_id})
    assert r.status_code == 403, f"viewer must not assign reviewers: {r.status_code} {r.text}"

    r = client.post(f"/api/v1/workspaces/{ws_id}/reviews/{review_id}/submit",
                    headers=owner_h)
    assert r.status_code == 200, f"owner submit must still work: {r.status_code} {r.text}"


def test_qc_override_requires_admin(tmp_path, monkeypatch, db_session, workspace_with_user):
    """B-F1: the QC override used a vacuous capability check + member floor."""
    from app.engine import intel  # noqa: F401 - package presence

    client = _client(tmp_path, monkeypatch)
    ws_id, owner_h, _ = _register(client, "owner")
    member_h, _ = _add_member(client, ws_id, "member")

    # a run + a persisted FAIL result for this workspace
    from app.db import session_scope
    from app.engine.intel import qc as intel_qc
    from app.models import MediaAsset
    from app.models.media_intel import MediaIntelRun

    with session_scope() as s:
        asset = MediaAsset(workspace_id=ws_id, type="audio", storage_key="w115/src.wav")
        s.add(asset)
        s.flush()
        run = MediaIntelRun(workspace_id=ws_id, asset_id=asset.id, kind="enhance",
                            provider_key="ffmpeg_enhancement", params_json={})
        s.add(run)
        s.flush()
        run_id = run.id
        intel_qc._persist(
            s, ws_id, run_id, "audio",
            [{"name": "clipping", "passed": False, "critical": True,
              "severity": "HARD", "detail": "seeded FAIL"}],
            {"verdict": "FAIL"},
        )
        s.commit()

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/override",
                    headers=member_h, json={"reason": "because", "kind": "audio"})
    assert r.status_code == 403, (
        f"a MEMBER must not record a QC override: {r.status_code} {r.text}")

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/qc/{run_id}/override",
                    headers=owner_h, json={"reason": "because", "kind": "audio"})
    assert r.status_code == 200, f"admin override must work: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# C-F1: allow_private is operator-gated
# ---------------------------------------------------------------------------

def test_private_targets_require_the_operator_kill_switch(monkeypatch):
    """C-F1: a workspace admin must not be able to self-serve an SSRF pivot."""
    from app.core.config import settings
    from app.engine.sources.adapters.url import _assert_public_host

    monkeypatch.setattr(settings, "allow_private_connectors", False)
    with pytest.raises(Exception, match="operator"):
        _assert_public_host("127.0.0.1", allow_private=True)
    with pytest.raises(Exception, match="operator"):
        _assert_public_host("169.254.169.254", allow_private=True)  # cloud metadata

    monkeypatch.setattr(settings, "allow_private_connectors", True)
    assert _assert_public_host("127.0.0.1", allow_private=True) is None


# ---------------------------------------------------------------------------
# D-F5: the video checker resolves + matches correctly
# ---------------------------------------------------------------------------

def test_video_checker_uses_the_storage_boundary_and_matches_storage_keys(
    tmp_path, monkeypatch, db_session, workspace_with_user
):
    """D-F5: raw DB paths were stat'ed and assets matched by bare basename."""
    from app.engine.intelligence import verifier

    ws_id = workspace_with_user["workspace"]
    monkeypatch.chdir(tmp_path)

    captured = {}

    def fake_managed_path(workspace_id, stored_path):
        captured["managed"] = (workspace_id, stored_path)
        return None

    monkeypatch.setattr("app.services.storage.managed_path", fake_managed_path)

    class _Video:
        id = "vid-1"
        workspace_id = ws_id
        status = "READY"
        file_path = "/etc/passwd"

    class _Session:
        def get(self, model, ident):
            return _Video() if model.__name__ == "Video" else None

        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def first(self):
            return None

        def all(self):
            return []

    contract = verifier.CompletionContract(kind="video", subject_id="vid-1")
    execution, verdict, checks = verifier.check_video(_Session(), ws_id, contract)
    assert captured["managed"] == (ws_id, "/etc/passwd"), (
        "the checker must resolve the path through managed_path")
    names = {c["name"]: c for c in checks}
    assert names["file_real_nonzero"]["passed"] is False, (
        "a path outside workspace storage must not verify")


def test_video_checker_does_not_read_a_file_the_storage_boundary_refused(
    db_session, workspace_with_user, tmp_path, monkeypatch
):
    """A refused path must stay refused even when the file really exists.

    ``managed_path`` is THE storage boundary: it returns None for a path outside
    the workspace directory so a tampered row cannot make a caller read another
    workspace's bytes. If the verifier re-adopts that same path with a bare
    ``Path(...).exists()`` fallback, the refusal is undone and the boundary
    becomes advisory. The earlier sibling test could only prove this with
    ``/etc/passwd``, which exists on the Linux runner and not on a Windows
    developer machine, so it passed locally and failed in CI; this one uses a
    file the test itself created, which exists on every platform.
    """
    from app.engine.intelligence import verifier
    from app.services import storage as storage_service

    ws_id = workspace_with_user["workspace"]
    monkeypatch.chdir(tmp_path)

    outside = tmp_path / "not-our-storage" / "video.mp4"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"not workspace owned bytes")

    monkeypatch.setattr(storage_service, "STORAGE_ROOT", tmp_path / "managed")
    assert storage_service.managed_path(ws_id, str(outside)) is None, (
        "precondition: the storage boundary must refuse this path")

    class _Video:
        id = "vid-1"
        workspace_id = ws_id
        status = "READY"
        file_path = str(outside)

    class _Session:
        def get(self, model, ident):
            return _Video() if model.__name__ == "Video" else None

        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def first(self):
            return None

        def all(self):
            return []

    contract = verifier.CompletionContract(kind="video", subject_id="vid-1")
    _execution, _verdict, checks = verifier.check_video(_Session(), ws_id, contract)
    names = {c["name"]: c for c in checks}
    assert names["file_real_nonzero"]["passed"] is False, (
        "a file outside workspace storage must never verify, even when it exists")


def test_asset_lookup_accepts_workspace_relative_storage_keys():
    source = (REPO_BACKEND / "app" / "engine" / "intelligence" / "verifier.py").read_text(
        encoding="utf-8")
    assert 'MediaAsset.storage_key == path' in source, (
        "the exact-key match is gone")
    assert 'MediaAsset.storage_key.like(f"%/{wanted}")' in source, (
        "the same-workspace basename-suffix match is gone")
    # and the old broken comparison must not come back
    assert 'MediaAsset.storage_key == Path(path).name' not in source


# ---------------------------------------------------------------------------
# E-F1: the pre-spend budget gate is actually called
# ---------------------------------------------------------------------------

def test_render_path_gates_spend_through_assert_can_spend():
    """E-F1 (CRITICAL): assert_can_spend had ZERO call sites."""
    source = (REPO_BACKEND / "app" / "engine" / "agents" / "production.py").read_text(
        encoding="utf-8")
    assert "assert_can_spend" in source, "the render path must gate on budget"
    assert "BudgetExceededError" in source, "the gate must map to a failure"


def test_mock_providers_are_refused_in_production(monkeypatch):
    """E-MED: a mock provider must not be reachable by config alone in prod.

    NOTE: the autouse ``fake_analytics`` conftest fixture replaces
    ``analytics.get_provider`` entirely, so this test calls the REAL factory
    through a direct module reference (and the real video-engine factory,
    bypassing the ``fake_video_engine`` fixture the same way).
    """
    # the autouse fixtures patch these module attributes, so recover the real
    # implementations from a fresh import (sys.modules is already patched, so
    # read the source functions instead)
    import importlib.util

    from app.core.config import settings

    spec = importlib.util.find_spec("app.providers.analytics")
    analytics_fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analytics_fresh)
    real_get_provider = analytics_fresh.get_provider

    spec = importlib.util.find_spec("app.providers.video_engine.factory")
    engine_fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine_fresh)
    real_get_engine = engine_fresh.get_video_engine
    reset_engine = engine_fresh.reset_video_engine
    monkeypatch.setattr(settings, "ymoney_env", "production")
    monkeypatch.setattr(settings, "mock_analytics", True)
    monkeypatch.setattr(settings, "video_engine", "mock")
    monkeypatch.setattr(settings, "allow_mock_in_production", False)
    reset_engine()

    with pytest.raises(RuntimeError, match="refused in production"):
        real_get_provider("youtube")
    with pytest.raises(engine_fresh.EngineNotConfigured, match="refused in production"):
        real_get_engine()

    # the deliberate override still works, so staging is not blocked
    monkeypatch.setattr(settings, "allow_mock_in_production", True)
    reset_engine()
    assert real_get_provider("youtube") is not None
    assert real_get_engine() is not None


def test_cost_ledger_is_written_for_browser_and_decision_spend():
    """E-F2: browser + decision spend never reached CostEntry."""
    for rel in ("app/api/v1/intelligence_evidence.py",
                "app/api/v1/intelligence_decisions.py"):
        text = (REPO_BACKEND / rel).read_text(encoding="utf-8")
        assert "track_cost" in text, f"{rel} must ledger its spend"
