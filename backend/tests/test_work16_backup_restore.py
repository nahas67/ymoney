"""Work 16 §12: backup, restore, and -- the point -- the DRILL that proves it.

**A backup that has never been restored is not a backup.** Every test in this
file exists to rule out one specific way "we can recover" can be a fiction:

* a backup that quietly omits half the product (a);
* a backup that carries the SECRET VALUES it was supposed to name (b);
* a backup that claims to cover media but only records that media was once
  mentioned, with no checksum to re-materialise or refuse it from (c);
* a restore that produces the wrong rows and says "ok" because the command
  exited 0 -- or, worse, a restore that restores nothing and looks identical to
  one that worked (d);
* a restore tool that can overwrite the database it is recovering from.

Tests (a)-(d) are MUTATION TESTS. Each names a guard, and the guard is
deliberately broken in the test body to show the check actually fails -- an
assertion nobody has seen fail is a claim, not evidence.

PostgreSQL is required for the round-trip tests, so they are gated on
``YMONEY_BACKUP_TEST_DSN`` with ``skipif``. The default SQLite suite is NOT made
to require a database: the SQLite round-trip runs everywhere, because
``docker-compose.yml`` really does deploy SQLite and a backup path that only
works against a server this deployment does not run is not a backup path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from app.scripts import backup_restore as bk

#: Declarative gate. Empty means "no PostgreSQL configured", and every test that
#: needs a real server skips with a reason instead of failing -- so the default
#: suite stays SQLite-only, as the rest of this repository's suite is.
PG_DSN = os.environ.get("YMONEY_BACKUP_TEST_DSN", "").strip()
PG_CONTAINER = os.environ.get("YMONEY_BACKUP_PG_CONTAINER", "").strip()

requires_pg = pytest.mark.skipif(
    not PG_DSN,
    reason="needs a live PostgreSQL; set YMONEY_BACKUP_TEST_DSN to run it",
)


# ---------------------------------------------------------------------------
# seeding a source database with data that MEANS something
# ---------------------------------------------------------------------------


def _source_dsn(name: str) -> str:
    return bk.with_database(PG_DSN, name)


def seed_source_database(dsn: str, storage_root: Path | None = None) -> dict:
    """Build a source database with one of everything the DoD names.

    Rows are written through a session bound to ``dsn``, not through
    ``app.services.cost``: ``reserve_spend``/``settle_reservation`` go through
    the process-global ``SessionLocal``, which in this suite is bound to the
    temp SQLite file, so calling them here would quietly seed the WRONG database
    and every assertion after it would be about SQLite. The ``detail_json``
    authority shape written below is byte-for-byte the one
    ``services/paid_provider.py`` writes, which is the part that matters.

    The ``storage_objects`` checksums are computed from REAL bytes written to
    ``storage_root``. A checksum copied from another row would make the
    "inventory has checksums" assertion true for the wrong reason.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401 - registers every table for create_all
    from app.db import normalize_database_url
    from app.migrations.runner import run_migrations
    from app.models import (
        ContentItem,
        CostEntry,
        Job,
        LipSyncJob,
        StorageObject,
        Video,
        VideoVariant,
        Workspace,
    )
    from app.models.base import utcnow

    database = bk.database_of(dsn)
    bk.create_database(dsn, database,
                       container=PG_CONTAINER or None)
    engine = create_engine(normalize_database_url(dsn), future=True)
    session = Session(engine)
    try:
        run_migrations(session)
        session.commit()

        ws = Workspace(name="Backup Drill WS", slug=f"bkp-{uuid.uuid4().hex[:8]}",
                       niche="AI money")
        session.add(ws)
        session.flush()

        # -- content lineage: root -> variant -> translation -> platform cut ---
        root = ContentItem(workspace_id=ws.id, topic="Backup drills are not optional",
                           status="PUBLISHED")
        session.add(root)
        session.flush()
        child = ContentItem(workspace_id=ws.id, topic="Drill -> root",
                            status="PUBLISHED", parent_content_id=root.id,
                            root_content_id=root.id, derivation_type="variant")
        session.add(child)
        session.flush()
        grandchild = ContentItem(workspace_id=ws.id, topic="Drill -> child",
                                 status="PUBLISHED", parent_content_id=child.id,
                                 root_content_id=root.id,
                                 derivation_type="platform_cut")
        session.add(grandchild)
        session.flush()

        variant = VideoVariant(content_item_id=child.id, label="v1",
                               hook="A backup you never restored is a rumour")
        session.add(variant)
        session.flush()
        video = Video(variant_id=variant.id, workspace_id=ws.id, engine="mock",
                      status="READY", cost_outcome="ACTUAL",
                      submission_operation_id="op-paid-7f3a",
                      submission_state="SUBMITTED",
                      provider_task_id="task-abc-123")
        session.add(video)

        lipsync = LipSyncJob(workspace_id=ws.id, provider="mock", status="FAILED",
                             execution_outcome="SUBMISSION_UNKNOWN",
                             cost_outcome="UNKNOWN_EXPOSURE")
        session.add(lipsync)

        # -- Work 16 §2: a CLAIMED job carrying a LIVE lease -------------------
        lease_until = utcnow() + timedelta(minutes=5)
        job = Job(workspace_id=ws.id, type="render", status="RUNNING",
                  claimed_by="worker-backup-3", claimed_at=utcnow(),
                  lease_expires_at=lease_until, started_at=utcnow())
        session.add(job)

        # -- cost ledger: an estimate, its settlement, and an unknown exposure --
        entries = [
            CostEntry(
                workspace_id=ws.id, category="render", amount_usd=0.42,
                provider="mock", is_estimate=False,
                detail_json={"spend_authority": "WORKSPACE_OWNED",
                             "actor_authority": "WORKSPACE_OWNER",
                             "budget_source": "WORKSPACE_BUDGET",
                             "owned_workspace_id": ws.id,
                             "charged_workspace_id": ws.id,
                             "cost_outcome": "ACTUAL", "operation_id": "op-paid-7f3a"}),
            CostEntry(
                workspace_id=ws.id, category="llm", amount_usd=0.0175,
                provider="mock", is_estimate=True,
                detail_json={"spend_authority": "SYSTEM_OWNED",
                             "actor_authority": "SYSTEM",
                             "budget_source": "SYSTEM_BUDGET",
                             "owned_workspace_id": ws.id,
                             "charged_workspace_id": "__system__",
                             "cost_outcome": "UNKNOWN_EXPOSURE",
                             "exposure_unknown": True, "reservation": True}),
            CostEntry(
                workspace_id=ws.id, category="llm", amount_usd=0.09,
                provider="mock", is_estimate=False,
                detail_json={"spend_authority": "WORKSPACE_OWNED",
                             "actor_authority": "WORKSPACE_OWNER",
                             "budget_source": "WORKSPACE_BUDGET",
                             "owned_workspace_id": ws.id,
                             "charged_workspace_id": ws.id,
                             "cost_outcome": "ACTUAL",
                             "estimated_usd": 0.12}),
        ]
        session.add_all(entries)

        # -- storage inventory with REAL checksums, at REAL canonical keys -----
        # The bytes are written through the canonical key helper into the real
        # STORAGE_ROOT, because a row whose key does not resolve is reported
        # UNRESOLVED by ``storage_inventory_markdown`` and would make the
        # "inventory has checksums AND the bytes are really there" assertion
        # vacuous.
        from app.services import storage as storage_service
        from app.services.storage_objects import object_key as canonical_key

        storage_base = Path(storage_root) if storage_root else storage_service.STORAGE_ROOT
        workspace_root = storage_base / ws.id
        workspace_root.mkdir(parents=True, exist_ok=True)
        objects = []
        for kind, logical, payload in (
                ("render", "hero.mp4", b"w16-backup-bytes-a" * 512),
                ("subtitle", "caption.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n"),
                ("thumbnail", "thumb.jpg", b"\xff\xd8\xff\xe0-w16-backup-thumb")):
            key = canonical_key(ws.id, kind, logical)
            relative = key.split("/", 1)[1]
            target = workspace_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            obj = StorageObject(
                workspace_id=ws.id, object_key=key,
                checksum=digest, size_bytes=len(payload),
                content_type="application/octet-stream", kind=kind,
                state=StorageObject.FINALIZED, backend="local",
                ref_type="content_item", ref_id=child.id, finalized_at=utcnow())
            session.add(obj)
            objects.append({"object_key": key, "checksum": digest,
                            "size_bytes": len(payload), "path": str(target)})

        session.commit()
        return {
            "database": database,
            "workspace_id": ws.id,
            "root_id": root.id,
            "child_id": child.id,
            "grandchild_id": grandchild.id,
            "video_id": video.id,
            "lipsync_id": lipsync.id,
            "job_id": job.id,
            "job_claimed_by": job.claimed_by,
            "lease_expires_at": lease_until.isoformat(),
            "cost_ids": [e.id for e in entries],
            "actual_cost_id": entries[0].id,
            "actual_amount": entries[0].amount_usd,
            "objects": objects,
        }
    finally:
        session.close()
        engine.dispose()


def _corrupt(path: Path) -> None:
    """Flip one byte in the MIDDLE of a dump.

    Not the tail: a SQLite page and a ``pg_dump -Fc`` archive both pad their
    last bytes with zeros, so overwriting the tail with zeros can leave the file
    byte-identical -- a corruption test that silently corrupts nothing.
    """
    payload = bytearray(path.read_bytes())
    middle = len(payload) // 2
    payload[middle] ^= 0xFF
    path.write_bytes(bytes(payload))
    assert path.stat().st_size == len(payload)


def _drop_quietly(dsn: str, database: str) -> None:
    with contextlib.suppress(bk.BackupError):  # teardown is best effort
        bk.drop_database(dsn, database, container=PG_CONTAINER or None)


@pytest.fixture()
def pg_source(tmp_path):
    """A freshly created, freshly migrated, freshly seeded source database."""
    if not PG_DSN:
        pytest.skip("needs a live PostgreSQL; set YMONEY_BACKUP_TEST_DSN")
    name = f"w16_src_{uuid.uuid4().hex[:8]}"
    dsn = _source_dsn(name)
    seeded = seed_source_database(dsn, storage_root=None)
    yield {"dsn": dsn, "name": name, **seeded}
    _drop_quietly(PG_DSN, name)
    # The seeded media lives in the real STORAGE_ROOT so the canonical keys
    # resolve; leave the repo the way it was found.
    workspace_root = Path("data/videos") / seeded["workspace_id"]
    if workspace_root.is_dir():
        shutil.rmtree(workspace_root, ignore_errors=True)


@pytest.fixture()
def pg_target(request):
    """The restore target's database NAME; dropped before and after the test."""
    if not PG_DSN:
        pytest.skip("needs a live PostgreSQL; set YMONEY_BACKUP_TEST_DSN")
    name = f"w16_restore_{request.node.name[:24].replace('_', '')}_{uuid.uuid4().hex[:6]}"
    _drop_quietly(PG_DSN, name)
    yield name
    _drop_quietly(PG_DSN, name)


# ---------------------------------------------------------------------------
# (b) the backup never carries a secret VALUE -- runs everywhere
# ---------------------------------------------------------------------------


def test_manifest_names_secret_references_without_their_values(monkeypatch, tmp_path):
    """A manifest must say WHICH credentials to re-inject and never what they are.

    An operator's manifest gets pasted into a ticket. A live API key in one of
    those is a credential leak wearing a backup's filename.
    """
    from app.core.config import settings

    sentinel = "sk-live-SENTINEL-MUST-NOT-APPEAR-9f3c2a"
    monkeypatch.setattr(settings, "openai_api_key", sentinel, raising=False)

    database = tmp_path / "scratch.sqlite3"
    import sqlite3

    sqlite3.connect(database).close()
    manifest = bk.backup_sqlite(database, tmp_path / "bk")

    references = manifest["secret_references"]
    names = [ref["setting"] for ref in references["references"]]
    assert "openai_api_key" in names, references
    assert references["configured"] >= 1

    # Only names + a boolean. No field of the reference entry may carry the value.
    for ref in references["references"]:
        assert set(ref) == {"setting", "configured"}, ref

    on_disk = (tmp_path / "bk" / bk.MANIFEST_NAME).read_text(encoding="utf-8")
    assert sentinel not in on_disk, "the manifest carries the secret VALUE"
    assert sentinel not in json.dumps(manifest)

    # And the whole backup DIRECTORY, not just the manifest.
    for path in (tmp_path / "bk").iterdir():
        if path.is_file():
            assert sentinel not in path.read_bytes().decode("utf-8", "replace"), path


def test_secret_reference_list_excludes_non_string_settings(monkeypatch):
    """``access_token_expire_minutes`` is a number, not a credential.

    Listing it would inflate "12 credentials must be re-injected" with a lie,
    and a manifest padded with lies is a manifest nobody reads carefully.
    """
    references = bk._secret_references()
    listed = {ref["setting"] for ref in references["references"]}
    assert "access_token_expire_minutes" not in listed
    assert "refresh_token_expire_days" not in listed
    assert "openai_api_key" in listed
    assert references["count"] == len(listed)


def test_redact_removes_the_password_from_a_dsn():
    assert "hunter2" not in bk.redact("postgresql://ymoney:hunter2@127.0.0.1:55432/db")
    assert bk.redact("postgresql://ymoney:hunter2@127.0.0.1:55432/db") == (
        "postgresql://ymoney@127.0.0.1:55432/db")


def test_with_database_rewrites_only_the_path():
    """The DSN swap must rewrite the PATH and nothing else.

    A ``str.replace`` would happily rewrite a password that happened to contain
    the source database name, leaving the tool pointed at a database nobody can
    log into.
    """
    dsn = "postgresql://ymoney:db_pass_w16@127.0.0.1:55432/source_db"
    swapped = bk.with_database(dsn, "target_db")
    assert swapped == "postgresql://ymoney:db_pass_w16@127.0.0.1:55432/target_db"
    # A password that CONTAINS the old database name survives intact.
    tricky = bk.with_database("postgresql://u:src@host:5432/src", "dst")
    assert tricky == "postgresql://u:src@host:5432/dst", tricky


# ---------------------------------------------------------------------------
# SQLite round trip -- runs everywhere, because this deployment runs SQLite
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_source(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    from app.db import normalize_database_url
    from app.migrations.runner import run_migrations
    from app.models import CostEntry, StorageObject, Workspace

    database = tmp_path / "source.sqlite3"
    engine = create_engine(normalize_database_url(f"sqlite:///{database.as_posix()}"),
                           future=True)
    session = Session(engine)
    try:
        run_migrations(session)
        session.commit()
        ws = Workspace(name="SQLite Drill", slug="sqlite-drill")
        session.add(ws)
        session.flush()
        session.add(CostEntry(
            workspace_id=ws.id, category="render", amount_usd=1.25,
            provider="mock", is_estimate=False,
            detail_json={"spend_authority": "WORKSPACE_OWNED",
                         "actor_authority": "WORKSPACE_OWNER",
                         "budget_source": "WORKSPACE_BUDGET",
                         "owned_workspace_id": ws.id,
                         "charged_workspace_id": ws.id,
                         "cost_outcome": "ACTUAL"}))
        session.add(StorageObject(
            workspace_id=ws.id,
            object_key=f"{ws.id}/render/one.mp4", checksum="a" * 64,
            size_bytes=11, kind="render", state=StorageObject.FINALIZED,
            backend="local", ref_type="content_item", ref_id=ws.id))
        session.commit()
    finally:
        session.close()
        engine.dispose()
    sqlite3.connect(database).close()
    return {"path": database, "dsn": f"sqlite:///{database.as_posix()}"}


def test_sqlite_backup_round_trip_replaces_the_file(sqlite_source, tmp_path):
    """backup -> mutate the original -> restore -> the ORIGINAL value is back.

    The mutation is the whole test, and it happens AFTER the backup: the amount
    is changed to $999, and only a restore that actually replaced the file can
    bring $1.25 back. Mutate-then-backup would prove nothing, because the backup
    would simply contain the mutation.
    """
    backup_dir = tmp_path / "bk"
    manifest = bk.backup_sqlite(sqlite_source["path"], backup_dir)
    assert manifest["dump"]["bytes"] > 0
    assert manifest["canonical"]["schema_migrations"]["count"] > 30

    engine, session = bk.session_for(sqlite_source["dsn"])
    try:
        session.execute(text("UPDATE cost_entries SET amount_usd = 999.0"))
        session.commit()
        observed = session.execute(text("SELECT amount_usd FROM cost_entries")).scalar()
        assert float(observed) == 999.0, "the mutation did not take"
    finally:
        session.close()
        engine.dispose()

    restored = bk.restore_sqlite(backup_dir, sqlite_source["path"])
    engine, session = bk.session_for(f"sqlite:///{restored.as_posix()}")
    try:
        amount = float(session.execute(text("SELECT amount_usd FROM cost_entries")).scalar())
        assert amount == 1.25, "the restore did not replace the mutated file"
        report = bk.verify(f"sqlite:///{restored.as_posix()}", manifest)
        assert report.ok, [c.to_dict() for c in report.failures]
    finally:
        session.close()
        engine.dispose()


def test_verify_detects_a_row_the_restore_lost(sqlite_source, tmp_path):
    """(d) A PARTIAL restore must be detected, not silently accepted."""
    backup_dir = tmp_path / "bk"
    manifest = bk.backup_sqlite(sqlite_source["path"], backup_dir)
    target = tmp_path / "restored.sqlite3"
    bk.restore_sqlite(backup_dir, target)
    dsn = f"sqlite:///{target.as_posix()}"

    report = bk.verify(dsn, manifest)
    assert report.ok, [c.to_dict() for c in report.failures]

    # Simulate the partial restore: the ledger rows never made it.
    engine, session = bk.session_for(dsn)
    try:
        session.execute(text("DELETE FROM cost_entries"))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    report = bk.verify(dsn, manifest)
    assert not report.ok, "a restore that lost every ledger row passed verification"
    failed = {check.name for check in report.failures}
    assert "rows:cost_entries" in failed, failed
    assert "digest:cost_entries" in failed, failed
    assert "cost_entries.authority_digest" in failed, failed


def test_verify_detects_a_removed_migration_version(sqlite_source, tmp_path):
    """A restored database whose migration ledger is short accepts writes it
    should refuse, so the ledger is verified by membership AND by count."""
    backup_dir = tmp_path / "bk"
    manifest = bk.backup_sqlite(sqlite_source["path"], backup_dir)
    target = tmp_path / "restored.sqlite3"
    bk.restore_sqlite(backup_dir, target)
    dsn = f"sqlite:///{target.as_posix()}"

    engine, session = bk.session_for(dsn)
    try:
        session.execute(text(
            "DELETE FROM schema_migrations WHERE version = "
            "(SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1)"))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    report = bk.verify(dsn, manifest)
    assert not report.ok
    failed = {check.name for check in report.failures}
    assert "schema_migrations.all_versions_present" in failed, failed
    assert "schema_migrations.version_count" in failed, failed


def test_backup_refuses_to_claim_a_dump_it_did_not_write(sqlite_source, tmp_path):
    """A corrupt/truncated dump is refused on the checksum, before any restore."""
    backup_dir = tmp_path / "bk"
    manifest = bk.backup_sqlite(sqlite_source["path"], backup_dir)
    dump = backup_dir / manifest["dump"]["file"]
    _corrupt(dump)

    with pytest.raises(bk.BackupError) as caught:
        bk.restore_sqlite(backup_dir, tmp_path / "restored.sqlite3")
    assert "checksum does not match" in str(caught.value)


def test_read_manifest_refuses_a_format_it_does_not_understand(sqlite_source, tmp_path):
    """Half-reading a manifest is worse than refusing it."""
    backup_dir = tmp_path / "bk"
    bk.backup_sqlite(sqlite_source["path"], backup_dir)
    manifest = json.loads((backup_dir / bk.MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["format"] = "ymoney.backup/999"
    (backup_dir / bk.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(bk.BackupError) as caught:
        bk.read_manifest(backup_dir)
    assert "refusing to half-restore" in str(caught.value)


def test_storage_inventory_records_checksums_for_every_canonical_object(
        sqlite_source, tmp_path):
    """(c) The object-storage coverage is an INVENTORY with checksums.

    A count of rows would pass while every key, checksum and size was dropped --
    which is a backup of nothing, because a ``FINALIZED`` row whose checksum is
    empty cannot be re-materialised or refused.
    """
    backup_dir = tmp_path / "bk"
    manifest = bk.backup_sqlite(sqlite_source["path"], backup_dir)

    assert manifest["storage_metadata"]["objects"] >= 1
    assert manifest["storage_metadata"]["note"].startswith("METADATA ONLY")

    # The inventory COLLECTION is the same code the PostgreSQL path writes into
    # storage_inventory.json; asserting on it means this test would still fail if
    # the PostgreSQL path dropped a field.
    engine, session = bk.session_for(sqlite_source["dsn"])
    try:
        collected = bk._storage_inventory(session)
    finally:
        session.close()
        engine.dispose()
    assert collected["present"] is True
    assert collected["count"] == 1
    assert collected["by_state"]["FINALIZED"] == 1
    assert collected["bytes_declared"] == 11
    entry = collected["objects"][0]
    for field_name in ("object_key", "checksum", "size_bytes", "state", "backend",
                       "kind", "ref_type", "ref_id"):
        assert entry.get(field_name) not in (None, ""), (field_name, entry)
    assert len(entry["checksum"]) == 64, entry
    assert entry["checksum"] == "a" * 64


def test_storage_inventory_markdown_hides_bytes_not_keys(tmp_path):
    """A FINALIZED row whose bytes are absent must be visibly absent.

    The inventory is the honest part of the media backup: it says what SHOULD
    be on the volume, with a checksum, so a restore can tell present bytes from
    missing bytes instead of reporting a healthy media library.
    """
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    from app.db import normalize_database_url
    from app.migrations.runner import run_migrations
    from app.models import StorageObject, Workspace

    database = tmp_path / "s.sqlite3"
    engine = create_engine(normalize_database_url(f"sqlite:///{database.as_posix()}"),
                           future=True)
    session = Session(engine)
    try:
        run_migrations(session)
        session.commit()
        ws = Workspace(name="Media", slug="media")
        session.add(ws)
        session.flush()
        session.add(StorageObject(
            workspace_id=ws.id, object_key="ws/x/gone.mp4", checksum="b" * 64,
            size_bytes=4096, kind="render", state=StorageObject.FINALIZED,
            backend="local"))
        session.commit()
    finally:
        session.close()
        engine.dispose()
    sqlite3.connect(database).close()

    inventory = bk.storage_inventory_markdown(f"sqlite:///{database.as_posix()}")
    assert "ws/x/gone.mp4" in inventory
    assert "MISSING" in inventory, inventory
    assert "b" * 64 in inventory


# ---------------------------------------------------------------------------
# (a) the headline: a PostgreSQL round trip that PROVES the restore replaced data
# ---------------------------------------------------------------------------


@requires_pg
def test_restore_returns_the_pre_mutation_value(pg_source, pg_target, tmp_path):
    """backup -> mutate -> DROP -> restore -> the ORIGINAL value is back.

    The mutation is not a formality. Without it, "the restore restored nothing"
    and "the restore restored everything" leave the same database state, and the
    test passes for both. With it, only a restore that actually replaced the
    data can produce the original value -- and the assertions below read the
    value back through the SAME public API an operator would.
    """
    dsn = pg_source["dsn"]
    backup_dir = tmp_path / "backup"
    manifest = bk.backup(dsn, backup_dir, container=PG_CONTAINER or None)

    # -- the mutation: change money, a title, lineage and a lease ------------
    mutation = bk.apply_mutation(
        dsn, table="cost_entries",
        set={"amount_usd": 777.0}, where={"id": pg_source["actual_cost_id"]})
    assert mutation["rows_updated"] == 1
    assert float(mutation["before"]["amount_usd"]) == pg_source["actual_amount"] == 0.42
    assert float(mutation["after"]["amount_usd"]) == 777.0

    bk.apply_mutation(dsn, table="content_items",
                      set={"status": "MUTATED_BY_DRILL"},
                      where={"id": pg_source["child_id"]})

    # -- destroy the environment, for real ------------------------------------
    bk.drop_database(PG_DSN, pg_source["name"], container=PG_CONTAINER or None)
    assert not bk.database_exists(PG_DSN, pg_source["name"],
                                 container=PG_CONTAINER or None), (
        "the drill could not destroy its own environment")

    # -- restore into a brand new database ------------------------------------
    bk.restore(backup_dir, PG_DSN, pg_target, container=PG_CONTAINER or None,
               replace=True)
    target_dsn = _source_dsn(pg_target)

    # (1) the pre-mutation money is back
    engine, session = bk.session_for(target_dsn)
    try:
        restored_amount = float(session.execute(text(
            "SELECT amount_usd FROM cost_entries WHERE id = :i"),
            {"i": pg_source["actual_cost_id"]}).scalar())
        assert restored_amount == pg_source["actual_amount"], (
            "the restore did not replace the mutated ledger row")

        # (2) the pre-mutation status is back
        status = session.execute(text("SELECT status FROM content_items WHERE id = :i"),
                                 {"i": pg_source["child_id"]}).scalar()
        assert status == "PUBLISHED", (status, pg_source["child_id"])

        # (3) the Work 15.9 authority picture survived
        detail = session.execute(text(
            "SELECT detail_json FROM cost_entries WHERE id = :i"),
            {"i": pg_source["actual_cost_id"]}).scalar()
        assert detail["spend_authority"] == "WORKSPACE_OWNED", detail
        assert detail["actor_authority"] == "WORKSPACE_OWNER", detail
        assert detail["budget_source"] == "WORKSPACE_BUDGET", detail
        assert detail["owned_workspace_id"] == pg_source["workspace_id"], detail
        assert detail["charged_workspace_id"] == pg_source["workspace_id"], detail
    finally:
        session.close()
        engine.dispose()

    report = bk.verify(target_dsn, manifest, database=pg_target)
    assert report.ok, [c.to_dict() for c in report.failures]
    assert len(report.checks) >= 20, len(report.checks)


@requires_pg
def test_restore_covers_the_documented_tables(pg_source, pg_target, tmp_path):
    """Every table the DoD names, with real row counts read back after restore."""
    dsn = pg_source["dsn"]
    backup_dir = tmp_path / "backup"
    manifest = bk.backup(dsn, backup_dir, container=PG_CONTAINER or None)
    bk.restore(backup_dir, PG_DSN, pg_target, container=PG_CONTAINER or None,
               replace=True)
    target_dsn = _source_dsn(pg_target)

    engine, session = bk.session_for(target_dsn)
    try:
        # schema_migrations: every version, count matching the source
        versions = [str(row[0]) for row in session.execute(
            text("SELECT version FROM schema_migrations ORDER BY version")).all()]
        assert len(versions) == manifest["canonical"]["schema_migrations"]["count"]
        assert "0037_budget_rollups" in versions, versions[-4:]
        assert "0035_job_leases" in versions, versions[-4:]

        # cost_entries: amounts, is_estimate, and the authority fields
        rows = session.execute(text(
            "SELECT id, category, amount_usd, is_estimate, detail_json "
            "FROM cost_entries ORDER BY category")).all()
        assert len(rows) == 3
        assert sorted(float(r.amount_usd) for r in rows) == [0.0175, 0.09, 0.42]
        estimates = [r for r in rows if r.is_estimate]
        assert len(estimates) == 1, "is_estimate did not survive the restore"
        assert estimates[0].detail_json["cost_outcome"] == "UNKNOWN_EXPOSURE"
        authorities = {r.detail_json["spend_authority"] for r in rows}
        assert authorities == {"WORKSPACE_OWNED", "SYSTEM_OWNED"}, authorities

        # content lineage: parent/root/derivation, and every link resolves
        lineage = session.execute(text(
            "SELECT topic, parent_content_id, root_content_id, derivation_type "
            "FROM content_items ORDER BY topic")).all()
        by_topic = {row.topic: row for row in lineage}
        assert by_topic["Drill -> root"].root_content_id == pg_source["root_id"]
        assert by_topic["Drill -> root"].derivation_type == "variant"
        assert by_topic["Drill -> child"].parent_content_id == pg_source["child_id"]
        assert by_topic["Drill -> child"].root_content_id == pg_source["root_id"]
        assert by_topic["Drill -> child"].derivation_type == "platform_cut"
        dangling = session.execute(text(
            "SELECT COUNT(*) FROM content_items c WHERE c.root_content_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM content_items r "
            "WHERE r.id = c.root_content_id)")).scalar()
        assert dangling == 0

        # Work 15.8/15.9 paid-execution columns
        video = session.execute(text(
            "SELECT cost_outcome, submission_operation_id, submission_state, "
            "provider_task_id FROM videos WHERE id = :i"),
            {"i": pg_source["video_id"]}).one()
        assert video.cost_outcome == "ACTUAL"
        assert video.submission_operation_id == "op-paid-7f3a"
        assert video.submission_state == "SUBMITTED"
        assert video.provider_task_id == "task-abc-123"

        lipsync = session.execute(text(
            "SELECT execution_outcome, cost_outcome FROM lipsync_jobs WHERE id = :i"),
            {"i": pg_source["lipsync_id"]}).one()
        assert lipsync.execution_outcome == "SUBMISSION_UNKNOWN"
        assert lipsync.cost_outcome == "UNKNOWN_EXPOSURE"

        # Work 16 §2 lease columns
        job = session.execute(text(
            "SELECT status, claimed_by, lease_expires_at FROM jobs WHERE id = :i"),
            {"i": pg_source["job_id"]}).one()
        assert job.status == "RUNNING"
        assert job.claimed_by == pg_source["job_claimed_by"] == "worker-backup-3"
        assert job.lease_expires_at is not None

        # object-storage inventory
        objects = session.execute(text(
            "SELECT object_key, checksum, size_bytes, state FROM storage_objects "
            "ORDER BY object_key")).all()
        assert len(objects) == 3
        for row in objects:
            assert len(row.checksum) == 64, row.object_key
            assert row.state == "FINALIZED"
            assert row.size_bytes > 0
    finally:
        session.close()
        engine.dispose()

    report = bk.verify(target_dsn, manifest, database=pg_target)
    assert report.ok, [c.to_dict() for c in report.failures]


@requires_pg
def test_partial_postgres_restore_is_detected(pg_source, pg_target, tmp_path):
    """(d) ``pg_restore`` exited 0 and half the data is missing -> FAIL.

    Restoring everything except the ledger is the failure mode that matters: the
    schema is right, the migrations are right, the app boots, and the money is
    gone.
    """
    dsn = pg_source["dsn"]
    backup_dir = tmp_path / "backup"
    manifest = bk.backup(dsn, backup_dir, container=PG_CONTAINER or None)
    bk.restore(backup_dir, PG_DSN, pg_target, container=PG_CONTAINER or None,
               replace=True)
    target_dsn = _source_dsn(pg_target)
    assert bk.verify(target_dsn, manifest, database=pg_target).ok

    engine, session = bk.session_for(target_dsn)
    try:
        session.execute(text("DELETE FROM cost_entries"))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    report = bk.verify(target_dsn, manifest, database=pg_target)
    assert not report.ok, "a ledger-less restore passed verification"
    failed = {check.name for check in report.failures}
    assert {"rows:cost_entries", "digest:cost_entries",
            "cost_entries.total_usd", "cost_entries.authority_digest"} <= failed, failed


@requires_pg
def test_verify_rejects_a_restore_into_the_wrong_database(pg_source, pg_target, tmp_path):
    """Verifying against a database that is not the restored one is not a pass."""
    backup_dir = tmp_path / "backup"
    manifest = bk.backup(pg_source["dsn"], backup_dir, container=PG_CONTAINER or None)
    bk.restore(backup_dir, PG_DSN, pg_target, container=PG_CONTAINER or None,
               replace=True)

    # The LIVE source still holds the same rows, so every digest matches -- the
    # only thing that distinguishes the two databases is which one was asked.
    report = bk.verify(pg_source["dsn"], manifest, database=pg_target)
    assert not report.ok
    assert "connected_to_expected_database" in {c.name for c in report.failures}


@requires_pg
def test_restore_refuses_to_overwrite_the_source_database(pg_source, tmp_path):
    """A restore that can drop the database it recovers from can destroy the
    last good copy. The guard is a refusal, not a warning."""
    backup_dir = tmp_path / "backup"
    bk.backup(pg_source["dsn"], backup_dir, container=PG_CONTAINER or None)
    with pytest.raises(bk.BackupError) as caught:
        bk.restore(backup_dir, PG_DSN, pg_source["name"], container=PG_CONTAINER or None,
                   replace=True)
    assert "refusing to restore into the source database" in str(caught.value)
    assert bk.database_exists(PG_DSN, pg_source["name"], container=PG_CONTAINER or None)


@requires_pg
def test_restore_refuses_a_truncated_dump(pg_source, pg_target, tmp_path):
    """The dump is checksummed at backup time and re-checked before any restore."""
    backup_dir = tmp_path / "backup"
    bk.backup(pg_source["dsn"], backup_dir, container=PG_CONTAINER or None)
    dump = backup_dir / "database.pgc"
    payload = bytearray(dump.read_bytes())
    payload[-32:] = b"\x00" * 32
    dump.write_bytes(bytes(payload))

    with pytest.raises(bk.BackupError) as caught:
        bk.restore(backup_dir, PG_DSN, pg_target, container=PG_CONTAINER or None,
                   replace=True)
    assert "checksum mismatch" in str(caught.value)
    assert not bk.database_exists(PG_DSN, pg_target, container=PG_CONTAINER or None), (
        "a corrupt dump must not create a target database")


@requires_pg
def test_apply_mutation_refuses_a_table_that_is_not_canonical(pg_source):
    """A tool that can destroy an arbitrary table is a tool that will."""
    with pytest.raises(bk.BackupError) as caught:
        bk.apply_mutation(pg_source["dsn"], table="workspaces",
                          set={"name": "gone"}, where={"id": pg_source["workspace_id"]})
    assert "not a canonical table" in str(caught.value)


@requires_pg
def test_drill_runs_the_whole_cycle_and_reports_every_command(
        pg_source, tmp_path, capsys):
    """THE DoD DRILL, in one call: backup -> DESTROY -> restore -> boot -> verify.

    ``boot`` is a real process: a subprocess that imports ``app.main`` and
    builds the FastAPI app against the RESTORED database. A restore that
    produces a database the application cannot open is a file with tables in it.
    """
    mutation = {"table": "cost_entries", "set": {"amount_usd": 4242.0},
                "where": {"id": pg_source["actual_cost_id"]}}
    backup_dir = tmp_path / "drill"
    result = bk.drill(pg_source["dsn"], backup_dir, container=PG_CONTAINER or None,
                      restored_database="", mutation=mutation)

    named = [entry["step"] for entry in result.steps]
    assert named == ["backup", "mutate", "destroy", "restore", "boot", "verify"], named
    assert float(result.manifest["canonical"]["cost_entries"]["total_usd"]) > 0
    assert result.ok, [c for c in result.report["checks"] if not c["ok"]]

    # The mutation is recorded with before AND after, which is what makes the
    # restore provable rather than merely asserted.
    assert result.report["ok"] is True
    evidence = json.loads((backup_dir / bk.DRILL_NAME).read_text(encoding="utf-8"))
    assert float(evidence["mutation"]["before"]["amount_usd"]) == 0.42
    assert float(evidence["mutation"]["after"]["amount_usd"]) == 4242.0

    # Real commands, with real durations, are in the evidence file.
    argv_lines = [" ".join(str(part) for part in entry["command"])
                  for entry in evidence["commands"]]
    assert any("pg_dump" in line for line in argv_lines), argv_lines
    assert any("pg_restore" in line for line in argv_lines), argv_lines
    assert any("dropdb" in line for line in argv_lines), argv_lines
    assert all("seconds" in entry for entry in evidence["commands"])
    assert any(float(entry["seconds"]) > 0 for entry in evidence["commands"])
    assert evidence["elapsed_s"] > 0

    # The application booted against the restored database.
    boot_step = next(s for s in result.steps if s["step"] == "boot")
    assert "BOOT OK" in boot_step["detail"], boot_step


@requires_pg
def test_boot_of_a_restored_database_succeeds(pg_source, pg_target, tmp_path):
    """``boot`` is called directly so its failure mode is visible on its own."""
    backup_dir = tmp_path / "backup"
    bk.backup(pg_source["dsn"], backup_dir, container=PG_CONTAINER or None)
    bk.restore(backup_dir, PG_DSN, pg_target, container=PG_CONTAINER or None,
               replace=True)
    output = bk.boot(_source_dsn(pg_target))
    assert output.startswith("BOOT OK"), output


# ---------------------------------------------------------------------------
# the module's own CLI, as an operator would run it
# ---------------------------------------------------------------------------


def test_cli_help_exits_cleanly():
    finished = subprocess.run(
        [sys.executable, "-m", "app.scripts.backup_restore", "--help"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        check=False)
    assert finished.returncode == 0, finished.stderr
    for command in ("backup", "restore", "verify", "drill"):
        assert command in finished.stdout


def test_cli_backup_then_verify_runs_end_to_end(sqlite_source, tmp_path):
    """The operator's path: ``main(["backup"])`` then ``main(["verify"])``."""
    backup_dir = tmp_path / "bk"
    assert bk.main(["--dsn", sqlite_source["dsn"], "--out", str(backup_dir),
                    "backup"]) == 0

    target = tmp_path / "restored.sqlite3"
    bk.restore_sqlite(backup_dir, target)
    target_dsn = f"sqlite:///{target.as_posix()}"
    # No ``--database`` here: on SQLite "the database" is a file path whose
    # spelling depends on how the driver was handed it, so asserting on it here
    # would test the driver's string handling rather than the restore. The
    # database-identity check is exercised against PostgreSQL instead, in
    # ``test_verify_rejects_a_restore_into_the_wrong_database``.
    assert bk.main(["--dsn", target_dsn, "--out", str(backup_dir), "verify"]) == 0

    engine, session = bk.session_for(target_dsn)
    try:
        session.execute(text("DELETE FROM workspaces"))
        session.commit()
    finally:
        session.close()
        engine.dispose()
    # A failed verify must be a non-zero exit code, not a printed warning.
    assert bk.main(["--dsn", target_dsn, "--out", str(backup_dir), "verify"]) == 1
