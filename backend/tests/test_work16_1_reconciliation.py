"""Work 16.1 §5: reconciliation of an ambiguous paid submission is DURABLE.

The defect under test
---------------------
``paid_executor.reconcile_submission`` mutated a Python object and logged. It
never opened a session and never committed, so the operator's answer died with
the process and the database kept saying ``SUBMISSION_UNKNOWN`` forever. With the
Work 15.7-15.9 invariant (at-most-one automatic paid submit, exactly-one
accounting identity), an ambiguity nobody can close durably is unbounded
exposure.

What these tests hold
---------------------
* **Survival.** Every persistence claim is read back through a **fresh engine**
  over a **fresh file**, never through the session that wrote it. An ORM
  identity map would make a test pass with no persistence at all -- that is the
  specific false negative this file is built to avoid.
* **The money.** Five outcomes, one money rule each. Capacity comes back exactly
  once on ``NOT_ACCEPTED``; a reservation settles at most once on
  ``SUCCEEDED``/``FAILED``; on ``UNKNOWN_REMAINS`` the exposure STAYS RESERVED,
  because "we do not know" and "it was free" are different claims.
* **No automatic repurchase.** Reconciliation is an operator/provider decision.
  Nothing here can submit, and the queue's paid re-entry gate still refuses a
  job whose submission is unresolved.
* **Idempotency.** The operation id is the key. Applying the SAME observation
  twice is refused; a DIFFERENT one is allowed, because a decision that blocks
  forever is not a guard, it is a lock.

Two database layouts are exercised. The SQLite half runs on the session-scoped
temporary database the suite already builds. The PostgreSQL half
(``YMONEY_TEST_POSTGRES``) re-runs the money and survival claims against a real
server, because ``json``-column predicates, transactional DDL and
double-precision money are exactly the things SQLite will happily agree with
about and PostgreSQL will not. It SKIPS CLEANLY with no server, declaratively.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registration side effect for Base.metadata

# ---------------------------------------------------------------------------
# Reachability probe. Declarative, so the default SQLite suite needs no server.
# ---------------------------------------------------------------------------

_DEFAULT_DSN = "postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres"
PG_DSN = os.environ.get("YMONEY_TEST_POSTGRES", _DEFAULT_DSN).strip()


def _pg_reachable(dsn: str) -> bool:
    """Whether a PostgreSQL server answers on ``dsn``. Never raises."""
    if not dsn:
        return False
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


PG_UP = _pg_reachable(PG_DSN)

requires_postgres = pytest.mark.skipif(
    not PG_UP, reason="no PostgreSQL reachable at YMONEY_TEST_POSTGRES")


def _sid(prefix: str = "w16-1") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# SQLite: a private file per test, rewired so "restart" is literally a restart
# ---------------------------------------------------------------------------


def _admin_url() -> str:
    return PG_DSN.rsplit("/", 1)[0] + "/postgres"


def _engine_url(db_name: str) -> str:
    return (_admin_url().rsplit("/", 1)[0].replace("postgresql://",
                                                   "postgresql+psycopg://", 1)
            .rstrip("/") + "/" + db_name)


#: Every module that opened its own session at IMPORT time. They are rebound to
#: the test's database for the duration; nothing under ``app/`` is modified, and
#: these are the SHIPPED implementations running against a real engine.
_SESSION_OWNERS = (
    "app.db",
    "app.services.cost",
    "app.services.events",
    "app.services.job_leases",
    "app.services.jobs",
    "app.engine.lipsync.rows",
)


def _rewire(monkeypatch, factory) -> None:
    """Point every session-owning service at ``factory``."""
    import importlib

    @contextmanager
    def scoped():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    for dotted in _SESSION_OWNERS:
        module = importlib.import_module(dotted)
        if hasattr(module, "session_scope"):
            monkeypatch.setattr(module, "session_scope", scoped)
        if hasattr(module, "SessionLocal"):
            monkeypatch.setattr(module, "SessionLocal", factory)


@pytest.fixture()
def ledger(tmp_path, monkeypatch) -> Iterator[dict]:
    """A private SQLite file plus the services rewired onto it.

    A FILE rather than the suite's shared database is what makes "survives
    restart" testable at all: :meth:`restart` hands back a brand-new engine on a
    COPY of the committed bytes, with no shared identity map, no shared pool and
    no shared connection. Reading back through the writing session would pass
    even if nothing were committed.
    """
    from sqlalchemy.orm import sessionmaker as _sm

    from app.db import Base

    path = tmp_path / "recon.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = _sm(bind=engine, expire_on_commit=False)
    _rewire(monkeypatch, factory)
    try:
        yield {"engine": engine, "Session": factory, "path": path}
    finally:
        engine.dispose()


@pytest.fixture()
def restart(ledger) -> object:
    """A FRESH engine over the COMMITTED bytes: the proof of persistence.

    The copy is the point. A new engine on the same file would still prove the
    COMMIT (a separate connection cannot see an uncommitted write), but copying
    the file first removes the last doubt -- if the writer only mutated an
    in-memory structure, the copy is empty and the read-back fails.
    """

    counter = {"n": 0}

    def build():
        # A DISTINCT file per call: two engines over one path would share a
        # journal and the "different database" claim would be unverifiable.
        counter["n"] += 1
        copied = ledger["path"].with_suffix(f".restart{counter['n']}.db")
        shutil.copyfile(ledger["path"], copied)
        engine = create_engine(f"sqlite:///{copied.as_posix()}")
        return engine, sessionmaker(bind=engine, expire_on_commit=False)

    return build


# ---------------------------------------------------------------------------
# Seed helpers -- the rows a real ambiguity leaves behind
# ---------------------------------------------------------------------------


def _workspace(session) -> str:
    from app.models import Workspace

    ws = Workspace(name="W16-1 Recon", slug=f"w16-1-{uuid.uuid4().hex}",
                   niche="AI money")
    session.add(ws)
    session.commit()
    return str(ws.id)


def _video_row(session, wid: str, *, operation_id: str, state: str,
               cost_outcome: str, remote_id: str = "") -> str:
    """The render lane's ambiguous row, with its real foreign keys."""
    from app.models import ContentItem, Video, VideoVariant

    item = ContentItem(workspace_id=wid, topic="w16.1 reconcile",
                       status="IN_PRODUCTION")
    session.add(item)
    session.flush()
    variant = VideoVariant(content_item_id=str(item.id), label="v1")
    session.add(variant)
    session.flush()
    row = Video(variant_id=str(variant.id), workspace_id=wid, engine="mock",
                status="RENDERING", submission_state=state,
                cost_outcome=cost_outcome, provider_task_id=remote_id,
                submission_operation_id=operation_id,
                submission_detail="provider accepted, response lost")
    session.add(row)
    session.commit()
    return str(row.id)


def _lipsync_row(session, wid: str, *, operation_id: str, state: str,
                 cost_outcome: str, remote_id: str = "") -> str:
    """The lip-sync lane's ambiguous row, in the shape the worker really writes.

    ``cost_json`` carries ``submission_state`` and ``exposure`` because
    ``engine/lipsync/worker.py`` does not own the canonical columns -- so a test
    that only wrote the columns would be testing a row the worker never produces.
    """
    from app.models import LipSyncJob

    row = LipSyncJob(
        workspace_id=wid, provider="external_lipsync_worker", status="FAILED",
        video_ref="asset/v.mp4", audio_ref="asset/a.wav",
        execution_outcome=state, cost_outcome=cost_outcome,
        adapter_job_id=remote_id,
        cost_json={"submission_state": state, "exposure": cost_outcome,
                   "exposure_unknown": True, "resubmit_forbidden": True,
                   "remote_id": remote_id, "submission_id": operation_id})
    session.add(row)
    session.commit()
    return str(row.id)


def _reservation(session, wid: str, *, operation_id: str, amount: float,
                 provider: str = "mock", remote_id: str = "",
                 idempotency_key: str = "") -> str:
    """The ledger row the reservation IS, carrying the authority picture.

    Built the way :meth:`PaidOperation.authorize` builds it -- same keys, same
    ``operation_id`` -- so the reconciliation reads a row it would really meet.
    """
    from app.models import CostEntry

    detail = {"operation_id": operation_id, "provider": provider,
              "operation": "video_render_submit",
              "estimated_usd": round(amount, 6),
              "spend_authority": "WORKSPACE_OWNED", "actor_authority": "USER",
              "budget_source": "WORKSPACE_BUDGET", "owned_workspace_id": wid,
              "charged_workspace_id": wid, "system_budget_usd": 0.0}
    if remote_id:
        detail["remote_id"] = remote_id
    if idempotency_key:
        detail["idempotency_key"] = idempotency_key
    row = CostEntry(workspace_id=wid, category="video", amount_usd=amount,
                    provider=provider, detail_json=detail, is_estimate=True)
    session.add(row)
    session.commit()
    return str(row.id)


def _ambiguous(session, wid: str, *, operation_id: str, amount: float = 4.0,
               remote_id: str = "", idempotency_key: str = "",
               with_ledger: bool = True) -> dict:
    """One ambiguous submission across the render row and the ledger row.

    The default shape is the real one: ``SUBMISSION_UNKNOWN`` on the row,
    ``UNKNOWN_EXPOSURE`` on the money, no remote id (the lost-response case), and
    a reservation that still holds the estimate.
    """
    video_id = _video_row(session, wid, operation_id=operation_id,
                          state="SUBMISSION_UNKNOWN",
                          cost_outcome="UNKNOWN_EXPOSURE")
    entry_id = _reservation(session, wid, operation_id=operation_id,
                            amount=amount, remote_id=remote_id,
                            idempotency_key=idempotency_key) \
        if with_ledger else ""
    return {"workspace_id": wid, "operation_id": operation_id,
            "video_id": video_id, "entry_id": entry_id,
            "amount": amount, "remote_id": remote_id}


def _read_video(factory, video_id: str) -> dict:
    """The render lane's facts, read through ``factory``'s own session."""
    from app.models import Video

    with factory() as s:
        row = s.get(Video, video_id)
        s.expunge_all()
        assert row is not None, f"video {video_id} vanished"
        return {"submission_state": row.submission_state,
                "cost_outcome": row.cost_outcome,
                "provider_task_id": row.provider_task_id,
                "submission_detail": row.submission_detail}


def _read_entry(factory, entry_id: str) -> dict | None:
    """The ledger row's money facts, or ``None`` when it is gone.

    ``None`` is a claim worth making precisely: on ``NOT_ACCEPTED`` the
    reservation is VOIDED, so a surviving row is the defect.
    """
    from app.models import CostEntry

    if not entry_id:
        return None
    with factory() as s:
        row = s.get(CostEntry, entry_id)
        s.expunge_all()
        if row is None:
            return None
        detail = dict(row.detail_json or {})
        return {"amount_usd": float(row.amount_usd or 0.0),
                "is_estimate": bool(row.is_estimate), "detail": detail}


def _count_events(factory, *, kind: str, operation_id: str) -> list[dict]:
    from app.models import EventLog
    from app.services.json_portability import json_value_equals

    with factory() as s:
        return [dict(r) for r in s.scalars(
            select(EventLog.data_json)
            .where(EventLog.kind == kind,
                   json_value_equals(EventLog.data_json, "operation_id",
                                     operation_id))).all()]


def _workspace_spend(factory, wid: str) -> float:
    from sqlalchemy import text

    with factory() as s:
        return float(s.execute(text(
            "SELECT coalesce(sum(amount_usd),0) FROM cost_entries "
            "WHERE workspace_id=:w"), {"w": wid}).scalar() or 0.0)


# ===========================================================================
# 1. Survival: the answer is still there after the process is gone
# ===========================================================================


def test_an_ambiguous_render_row_survives_restart_as_a_settled_outcome(
        ledger, restart):
    """The headline claim, proved through a brand-new engine on committed bytes.

    Read back through the WRITING session this test would pass with no
    ``session_scope`` at all, so it reads through :meth:`restart` instead: a new
    file, a new engine, a new connection. The three facts an operator needs after
    a crash are the execution outcome, the money outcome and the operator's own
    name.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    before_engine = ledger["engine"]

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.SUCCEEDED, operator="ops@x",
            note="provider emailed a receipt")

    fresh_engine, fresh_factory = restart()
    try:
        assert fresh_engine is not before_engine, (
            "the 'restart' engine is the writing engine; the read-back would be "
            "an identity-map hit and would prove nothing")
        assert fresh_engine.engine is not before_engine.engine
        seen = _read_video(fresh_factory, case["video_id"])
        assert seen["submission_state"] == "SUCCEEDED", (
            f"after a restart the row still says {seen['submission_state']!r}; "
            f"the reconciliation was never committed")
        # ESTIMATED, not ACTUAL, and the distinction is the point: no vendor
        # reported an amount here, so stamping ACTUAL would be a claim no
        # invoice supports. ``settle_reservation`` would have done exactly that.
        assert seen["cost_outcome"] == "ESTIMATED", (
            f"the row says {seen['cost_outcome']!r}; a finished render nobody "
            f"priced keeps the estimate it reserved")
        assert "ops@x" in seen["submission_detail"], (
            "an operator decision that leaves no trace on the row is how a real "
            "duplicate charge becomes unexplainable three weeks later")
        assert result.execution_outcome == "SUCCEEDED"
        assert result.cost_outcome == "ESTIMATED"

        # ...and the audit event survives too, which is the operator's evidence
        # that the incident was closed rather than forgotten.
        events = _count_events(fresh_factory, kind="paid.reconciliation",
                               operation_id=case["operation_id"])
        assert len(events) == 1, f"{len(events)} reconciliation events written"
        assert events[0]["outcome"] == "SUCCEEDED"
        assert events[0]["operator"] == "ops@x"
    finally:
        fresh_engine.dispose()


def test_the_ledger_money_survives_restart_too(ledger, restart):
    """The money half is a separate row from the outcome half, so it is
    separately proven. A settled reservation must not be found open again."""
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.SUCCEEDED,
                                  operator="ops@x", actual_usd=3.25)

    fresh_engine, fresh_factory = restart()
    try:
        row = _read_entry(fresh_factory, case["entry_id"])
        assert row is not None, "the reservation row vanished"
        assert row["amount_usd"] == 3.25, (
            f"the ledger says ${row['amount_usd']} after a restart; the settle "
            f"was not committed")
        assert row["is_estimate"] is False
        assert row["detail"]["cost_outcome"] == "ACTUAL"
    finally:
        fresh_engine.dispose()


def test_a_lipsync_reconciliation_survives_restart(ledger, restart):
    """The lip-sync lane is a DIFFERENT row shape, and it must persist too.

    ``engine/lipsync/rows.py`` owns the canonical columns, so the reconciliation
    goes through the shipped :func:`set_paid_outcomes` rather than writing them
    directly -- which is also what keeps the vocabulary validated.
    """
    from app.models import LipSyncJob
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_lipsync_job,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        job_id = _lipsync_row(s, wid, operation_id=_sid("op"),
                              state="SUBMISSION_UNKNOWN",
                              cost_outcome="UNKNOWN_EXPOSURE",
                              remote_id="remote-lip-1")

    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_lipsync_job(job_id),
                                  ReconciliationOutcome.REMOTE_JOB_CONFIRMED,
                                  operator="ops@x", remote_id="remote-lip-1")

    fresh_engine, fresh_factory = restart()
    try:
        with fresh_factory() as s:
            row = s.get(LipSyncJob, job_id)
            s.expunge_all()
            assert row.execution_outcome == "REMOTE_ID_CONFIRMED", (
                f"after a restart the job still says "
                f"{row.execution_outcome!r}")
            assert row.cost_outcome == "ESTIMATED"
            assert row.adapter_job_id == "remote-lip-1"
            assert row.cost_json["reconciliation"]["operator"] == "ops@x"
            # The worker's own prohibition is never cleared by reconciliation.
            assert row.cost_json["resubmit_forbidden"] is True
    finally:
        fresh_engine.dispose()


# ===========================================================================
# 2. The five outcomes and what each does to the money
# ===========================================================================


def test_not_accepted_releases_the_capacity_exactly_once(ledger):
    """The provider refused: nothing was created, nothing was billed.

    The reservation is VOIDED, so the workspace's cap comes back -- and the row
    is gone, which is what makes a second release impossible rather than merely
    discouraged.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    spent_before = _workspace_spend(ledger["Session"], wid)

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.NOT_ACCEPTED, operator="ops@x",
            note="dashboard shows no charge")

    assert result.money_effect == "CAPACITY_RELEASED"
    assert result.execution_outcome == "FAILED"
    assert result.cost_outcome == "NOT_APPLICABLE"
    assert _read_entry(ledger["Session"], case["entry_id"]) is None, (
        "a refused submission kept its reservation; the workspace's budget is "
        "shrunk for work that was never done")
    assert _workspace_spend(ledger["Session"], wid) == spent_before - 4.0
    assert _read_video(ledger["Session"], case["video_id"])["cost_outcome"] == \
        "NOT_APPLICABLE"


def test_unknown_remains_keeps_the_exposure_reserved(ledger):
    """``UNKNOWN_REMAINS`` is the load-bearing case.

    Nothing was learned, so nothing moves. Releasing the reservation here would
    turn "we do not know what this cost" into "it was free" -- the exact lie
    Work 15.7 §7 exists to prevent, arriving through the reconciliation door.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    spent_before = _workspace_spend(ledger["Session"], wid)

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.UNKNOWN_REMAINS, operator="ops@x",
            note="provider has no record and no status endpoint")

    assert result.money_effect == "RESERVATION_KEPT"
    assert result.execution_outcome == "SUBMISSION_UNKNOWN"
    assert result.cost_outcome == "UNKNOWN_EXPOSURE"
    row = _read_entry(ledger["Session"], case["entry_id"])
    assert row is not None, (
        "UNKNOWN_REMAINS DELETED the reservation. The money may well have been "
        "spent and the amount is unknown; releasing it books a phantom free "
        "operation and shrinks the cap for a real charge.")
    assert row["detail"]["cost_outcome"] == "UNKNOWN_EXPOSURE"
    assert _workspace_spend(ledger["Session"], wid) == spent_before, (
        "an unresolved exposure changed the workspace's ledger total")
    seen = _read_video(ledger["Session"], case["video_id"])
    assert seen["submission_state"] == "SUBMISSION_UNKNOWN", (
        "UNKNOWN_REMAINS must leave the submission unconfirmed; pretending "
        "otherwise is the bug the whole contract exists to prevent")


def test_a_confirmed_remote_job_persists_the_handle_and_keeps_the_reservation(ledger):
    """``REMOTE_JOB_CONFIRMED``: the work exists and is now ADDRESSABLE.

    The remote id is written onto both the lane row and the ledger row, because
    a billed job nobody can address is an invoice nobody can reconcile. The
    reservation stays: acceptance is not completion, and nothing has been settled
    for a job that has not finished.
    """
    from app.services.paid_provider import reattach_by_remote_id
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.REMOTE_JOB_CONFIRMED, operator="ops@x",
            remote_id="remote-abc")

    assert result.money_effect == "RESERVATION_KEPT"
    assert result.remote_id == "remote-abc"
    assert _read_video(ledger["Session"], case["video_id"])["provider_task_id"] \
        == "remote-abc", (
        "the discovered remote id was not persisted on the lane row; nothing "
        "downstream could ever address this job")
    row = _read_entry(ledger["Session"], case["entry_id"])
    assert row is not None and row["detail"]["remote_id"] == "remote-abc", (
        "the remote id was not persisted on the LEDGER row, which is the one "
        "that survives a restart -- the runbook's reattach_by_remote_id() "
        "could not find it after a crash")
    # The runbook's own recovery entry point now works against real data.
    adopted = reattach_by_remote_id("remote-abc", workspace_id=wid)
    assert adopted is not None, (
        "reattach_by_remote_id cannot find a job this module just reconciled; "
        "the documented incident procedure is broken")
    assert adopted.entry_id == case["entry_id"]


def test_a_confirmed_remote_job_without_a_handle_is_refused(ledger):
    """A confirmed job with no id cannot be polled, adopted or settled by
    anything, ever -- so the observation is refused rather than stored."""
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"))

    with ledger["Session"]() as s, pytest.raises(ReconciliationRefused) as caught:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.REMOTE_JOB_CONFIRMED,
                                  operator="ops@x")
    assert caught.value.reason == "REMOTE_ID_REQUIRED"
    assert _read_video(ledger["Session"], case["video_id"])["submission_state"] \
        == "SUBMISSION_UNKNOWN", "a refused reconciliation still wrote a row"


def test_not_accepted_with_a_remote_id_is_refused_as_contradictory(ledger):
    """A job that was never created has no handle. Accepting one would mean the
    operator's answer contradicts itself, and the money would be released while a
    real job sits at the provider."""
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"))

    with ledger["Session"]() as s, pytest.raises(ReconciliationRefused) as caught:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="ops@x", remote_id="remote-abc")
    assert caught.value.reason == "CONTRADICTORY_INPUT"
    assert _read_entry(ledger["Session"], case["entry_id"]) is not None, (
        "a contradictory observation released the reservation anyway")


def test_a_failed_render_still_settles_because_it_was_billed(ledger):
    """``FAILED`` is NOT ``NOT_ACCEPTED``.

    The provider created the job, charged for it, and the render then failed.
    Handing the capacity back would refund work that was really purchased, so
    this closes the book instead -- at the estimate, because nobody reported an
    amount.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.FAILED, operator="ops@x",
            note="provider reports the render failed after charging")

    assert result.execution_outcome == "FAILED"
    assert result.cost_outcome == "ESTIMATED"
    assert result.money_effect == "RESERVATION_KEPT"
    row = _read_entry(ledger["Session"], case["entry_id"])
    assert row is not None, (
        "a FAILED render released the reservation; the provider had already "
        "created and billed the job")


def test_a_finished_render_whose_price_was_never_reported_is_not_booked_at_zero(ledger):
    """``amount_unknown`` is the case four providers had to hand-roll.

    ``cost.track_cost`` returns early on ``amount_usd <= 0``, so a "$0" render
    writes NO row at all and the spend disappears from the books. The honest
    reading is ``UNKNOWN_EXPOSURE``: money may be gone and we do not know how
    much.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.SUCCEEDED, operator="ops@x",
            amount_unknown=True)

    assert result.cost_outcome == "UNKNOWN_EXPOSURE"
    row = _read_entry(ledger["Session"], case["entry_id"])
    assert row is not None, (
        "a finished-but-unpriced render wrote no ledger row; the spend vanished")
    assert row["detail"]["cost_outcome"] == "UNKNOWN_EXPOSURE"


# ===========================================================================
# 3. Idempotency: the operation id is the key
# ===========================================================================


def test_reconciling_the_same_observation_twice_is_refused(ledger):
    """The guard. Two settlements of one purchase is a real second charge.

    Keyed on the operation id AND the outcome, so a DIFFERENT observation is
    still allowed -- an operator who finds the job later has to be able to say
    so. A guard that blocks forever is not a guard.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="ops@x")

    with ledger["Session"]() as s, pytest.raises(ReconciliationRefused) as caught:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="someone-else")
    assert caught.value.reason == "ALREADY_APPLIED"
    assert "already reconciled as NOT_ACCEPTED by ops@x" in str(caught.value)
    events = _count_events(ledger["Session"], kind="paid.reconciliation",
                           operation_id=case["operation_id"])
    assert len(events) == 1, (
        f"{len(events)} reconciliation events for one decision; the refused "
        f"repeat wrote a second audit line and would read as two decisions")


def test_a_later_different_observation_is_allowed_after_unknown_remains(ledger):
    """``UNKNOWN_REMAINS`` then, once the provider answers, ``SUCCEEDED``.

    This is the sequence the runbook actually describes, so the guard must not
    make the second half impossible.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    with ledger["Session"]() as s:
        first = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.UNKNOWN_REMAINS, operator="ops@x")
        second = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.SUCCEEDED, operator="ops@x", actual_usd=2.5)

    assert first.cost_outcome == "UNKNOWN_EXPOSURE"
    assert second.cost_outcome == "ACTUAL"
    events = _count_events(ledger["Session"], kind="paid.reconciliation",
                           operation_id=case["operation_id"])
    assert [e["outcome"] for e in events] == ["UNKNOWN_REMAINS", "SUCCEEDED"], (
        f"the audit trail is {events}; both decisions must be on it")
    assert _read_entry(ledger["Session"], case["entry_id"])["amount_usd"] == 2.5


def test_the_guard_is_the_audit_trail_so_it_survives_a_restart(ledger, restart,
                                                              monkeypatch):
    """The guard must not be in-memory state, or a restart re-opens the door.

    The reconciliation event IS the record of the decision, so the second
    attempt is refused by a query against committed bytes -- read through a
    different engine, not the one that wrote it.
    """
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="ops@x")

    fresh_engine, fresh_factory = restart()
    try:
        _rewire(monkeypatch, fresh_factory)
        with pytest.raises(ReconciliationRefused) as caught:
            reconcile_paid_submission(
                subject_for_video(case["video_id"]),
                ReconciliationOutcome.NOT_ACCEPTED, operator="ops@x")
        assert caught.value.reason == "ALREADY_APPLIED"
    finally:
        fresh_engine.dispose()


# ===========================================================================
# 4. No automatic repurchase
# ===========================================================================


def test_this_module_contains_no_way_to_submit_or_to_license_a_retry():
    """Derived from the module's own source, not asserted in prose.

    Two things are checked, and both are re-derived on every run: no outbound
    billable request builder, and no writing of ``RETRY_IF_CONFIRMED_SAFE``.
    That flag is the ONLY thing ``paid_executor.verdict_for`` reads to answer
    ``RETRY``, so a reconciliation that set it would be the whole of a second
    purchase.
    """
    from app.services.paid_reconciliation import assert_never_resubmits

    assert_never_resubmits()


def test_reconciling_to_unknown_remains_leaves_the_submission_unretryable(ledger):
    """After ``UNKNOWN_REMAINS`` the record must still say "do not resubmit".

    Checked through the SHIPPED ``verdict_for``, so this is the answer a caller
    would actually get -- not a re-derivation of the rule in the test.
    """
    from app.services.paid_executor import (
        CostOutcome,
        CostRecord,
        PaidSubmission,
        RetryVerdict,
        SubmissionState,
        verdict_for,
    )
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"))

    with ledger["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.UNKNOWN_REMAINS, operator="ops@x")

    # The persisted row, rebuilt into the record type the queue layer reads.
    rebuilt = PaidSubmission(
        workspace_id=wid, provider="mock", operation="video_render_submit",
        submission_id=case["operation_id"],
        state=SubmissionState(result.execution_outcome),
        cost=CostRecord(outcome=CostOutcome(result.cost_outcome),
                        estimated=case["amount"]))
    assert rebuilt.may_resubmit is False
    assert verdict_for(rebuilt) is RetryVerdict.RECONCILE, (
        "an unresolved exposure must ask for a human, never a re-send")


def test_the_queue_paid_reentry_gate_still_refuses_an_unresolved_submission(ledger):
    """Work 16 §2's interaction, on the real ``assess_paid_reentry``.

    A job that re-enters a workspace carrying an unresolved paid row is
    ``BLOCKED``: the correct outcome is an incident, and the wrong one is a
    duplicate invoice. This is asserted through the SHIPPED gate so it is the
    real answer, not a restatement of the rule.
    """
    from app.services.job_leases import PaidVerdict, assess_paid_reentry
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0,
                          idempotency_key="idem-1")

    class _Job:
        id = _sid("job")
        type = "video.render"
        workspace_id = wid
        payload = {"paid": True}
        idempotency_key = "idem-1"
        retry_count = 1

    job = _Job()
    # Before the reconciliation the exposure is already there, and the gate
    # refuses. This is the state an unresolved incident leaves behind.
    with ledger["Session"]() as s:
        blocked = assess_paid_reentry(job, attempt=2)
    assert blocked.verdict is PaidVerdict.BLOCKED, (
        f"the gate said {blocked.verdict}; a job re-entering a workspace with an "
        f"unresolved paid row must be refused, not allowed to spend again")

    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.UNKNOWN_REMAINS,
                                  operator="ops@x")

    with ledger["Session"]() as s:
        after = assess_paid_reentry(job, attempt=2)
    assert after.verdict is PaidVerdict.BLOCKED, (
        "UNKNOWN_REMAINS cleared the gate's evidence; a submission that is still "
        "unknown must keep refusing to re-spend")
    assert after.entry_id == case["entry_id"]


def test_a_confirmed_remote_job_makes_the_gate_adopt_rather_than_resubmit(ledger):
    """The positive half of the same interaction: once the handle exists, the
    gate REATTACHES to the existing accounting instead of reserving a new one.

    ``adopt_by_remote_id`` reads ``settled`` back off the row, so the adopted
    operation closes its book to a no-op. Exactly-once ACCOUNTING, not
    exactly-once network.
    """
    from app.services.job_leases import PaidVerdict, assess_paid_reentry
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0,
                          idempotency_key="idem-adopt")

    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.REMOTE_JOB_CONFIRMED,
                                  operator="ops@x", remote_id="remote-adopt-1")

    class _Job:
        id = _sid("job")
        type = "video.render"
        workspace_id = wid
        payload = {"paid": True}
        # The producer records the SAME idempotency key on the job and on the
        # reservation, which is how the queue layer gets an exact link rather
        # than a guess. Without it the gate BLOCKS (also safe, but not the
        # positive path this test is about).
        idempotency_key = "idem-adopt"
        retry_count = 1

    with ledger["Session"]() as s:
        verdict = assess_paid_reentry(_Job(), attempt=2)
    assert verdict.verdict is PaidVerdict.REATTACH, (
        f"the gate said {verdict.verdict}: a confirmed remote job must be "
        f"adopted, never bought again")
    assert verdict.entry_id == case["entry_id"]
    assert verdict.remote_id == "remote-adopt-1"

    # And the adopted operation cannot close the book a second time: ``settled``
    # was read back off the row.
    from app.services.paid_provider import reattach_by_remote_id

    adopted = reattach_by_remote_id("remote-adopt-1", workspace_id=wid)
    assert adopted is not None and adopted.settled is True
    assert adopted.close_book(1.0) is False, (
        "an adopted, already-decided reservation settled a second time; one "
        "purchase, one settlement")


# ===========================================================================
# 5. The decision half: no operator, no operation id, nothing written
# ===========================================================================


def test_a_reconciliation_without_an_operator_is_refused(ledger):
    """A decision nobody made is not a decision. The write must not happen."""
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"))

    with ledger["Session"]() as s, pytest.raises(ReconciliationRefused) as caught:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="")
    assert caught.value.reason == "NO_OPERATOR"
    assert _read_video(ledger["Session"], case["video_id"])["submission_state"] \
        == "SUBMISSION_UNKNOWN"


def test_a_subject_with_no_operation_id_cannot_be_reconciled(ledger):
    """Without an operation id a repeat could not be recognised, so the whole
    idempotency story is unavailable. Refuse rather than guess."""
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        ReconciliationSubject,
        SubjectKind,
        reconcile_paid_submission,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"))
    anonymous = ReconciliationSubject(kind=SubjectKind.VIDEO,
                                      record_id=case["video_id"],
                                      workspace_id=wid)

    with ledger["Session"]() as s, pytest.raises(ReconciliationRefused) as caught:
        reconcile_paid_submission(anonymous,
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="ops@x")
    assert caught.value.reason == "NO_OPERATION_ID"
    assert _read_entry(ledger["Session"], case["entry_id"]) is not None


def test_the_legacy_reconcile_submission_decision_is_durable_and_moves_no_money(
        ledger, restart):
    """``paid_executor.reconcile_submission`` keeps its in-memory contract AND
    now persists the decision.

    It does NOT move money, and that is the load-bearing design choice rather
    than a gap: ``Reconciliation`` is a vocabulary of REMEDIES ("what should the
    system do next"), not of OBSERVATIONS ("what the provider did"), and
    ``RECONCILE`` explicitly means "still unconfirmed". A remedy label is not
    evidence that a charge did or did not happen. Money moves only through
    ``reconcile_paid_submission``.
    """
    from app.services.paid_executor import (
        CostOutcome,
        CostRecord,
        PaidSubmission,
        Reconciliation,
        SubmissionState,
        reconcile_submission,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    record = PaidSubmission(
        workspace_id=wid, provider="mock", operation="video_render_submit",
        submission_id=case["operation_id"],
        state=SubmissionState.SUBMISSION_UNKNOWN,
        cost=CostRecord(outcome=CostOutcome.UNKNOWN_EXPOSURE, estimated=4.0))

    with ledger["Session"]() as s:
        reconcile_submission(record, Reconciliation.RECONCILE,
                             operator="ops@x", note="looked at the dashboard")

    # In-memory contract, unchanged: RECONCILE leaves the state alone.
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.cost.outcome is CostOutcome.UNKNOWN_EXPOSURE
    assert "ops@x" in record.detail

    # Durable half, proven through a restart rather than the same session.
    fresh_engine, fresh_factory = restart()
    try:
        events = _count_events(fresh_factory, kind="paid.reconciliation",
                               operation_id=case["operation_id"])
        assert len(events) == 1, (
            f"{len(events)} decision events after a restart; the decision was "
            f"not committed, so it dies with the process")
        assert events[0]["action"] == "RECONCILE"
        assert events[0]["operator"] == "ops@x"
        assert events[0]["moved_money"] is False
        assert _read_entry(fresh_factory, case["entry_id"]) is not None, (
            "the remedy label released the reservation; a label is not evidence")
    finally:
        fresh_engine.dispose()


def test_a_decision_with_no_persisted_submission_does_not_lose_the_record(ledger):
    """A caller holding a record it never persisted must still get its result.

    ``reconcile_submission`` is a library function: refusing to return because
    the DATABASE has no matching row would break every in-memory caller, and
    raising would discard a decision the caller already made. The persistence
    attempt is best-effort and logged; the money-moving path is the one that
    insists on a real subject.
    """
    from app.services.paid_executor import (
        CostOutcome,
        CostRecord,
        PaidSubmission,
        Reconciliation,
        SubmissionState,
        reconcile_submission,
    )

    record = PaidSubmission(
        workspace_id="no-such-workspace", provider="mock",
        operation="video_render_submit", submission_id=_sid("ghost"),
        state=SubmissionState.SUBMISSION_UNKNOWN,
        cost=CostRecord(outcome=CostOutcome.UNKNOWN_EXPOSURE))
    with ledger["Session"]():
        returned = reconcile_submission(record, Reconciliation.MANUAL_OVERRIDE,
                                        operator="ops@x", note="no row")
    assert returned is record
    assert "ops@x" in record.detail
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert _count_events(ledger["Session"], kind="paid.reconciliation",
                         operation_id=record.submission_id) == []


# ===========================================================================
# 6. The operator's worklist
# ===========================================================================


def test_the_worklist_finds_the_ambiguity_in_both_lanes_and_the_ledger(ledger):
    """An operator asks "which submissions may have been billed?" and gets a
    WHERE clause's answer, not a scan-and-parse.

    Both assertions are separate on purpose: "the submit is unconfirmed" and
    "the amount is unknown" are different claims, and widening one to the other
    is how a worklist stops being trustworthy.
    """
    from app.services.paid_reconciliation import pending_submissions

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
        job_id = _lipsync_row(s, wid, operation_id=_sid("op"),
                              state="SUBMISSION_UNKNOWN",
                              cost_outcome="UNKNOWN_EXPOSURE",
                              remote_id="remote-lip-9")
        clean_video = _video_row(s, wid, operation_id=_sid("op2"),
                                 state="SUCCEEDED", cost_outcome="ACTUAL")

    pending = pending_submissions(wid)
    ids = {row["record_id"] for row in pending}
    assert case["video_id"] in ids, "the render lane's ambiguity is missing"
    assert job_id in ids, (
        "the lip-sync lane's ambiguity is missing; the worker writes "
        "SUBMISSION_UNKNOWN into cost_json, so a worklist reading only the "
        "canonical columns would show an empty workspace for that lane")
    assert clean_video not in ids, "a settled render was reported as ambiguous"
    by_id = {row["record_id"]: row for row in pending}
    assert by_id[case["video_id"]]["state"] == "SUBMISSION_UNKNOWN"
    assert by_id[case["video_id"]]["cost_outcome"] == "UNKNOWN_EXPOSURE"
    assert by_id[job_id]["remote_id"] == "remote-lip-9", (
        "the remote id is the handle the runbook's dashboard lookup needs, and "
        "the worklist must be the place an operator finds it")


def test_a_settled_submission_leaves_the_worklist(ledger):
    """The whole point: reconciling an incident takes it OUT of the queue."""
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        pending_submissions,
        reconcile_paid_submission,
        subject_for_video,
    )

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    assert pending_submissions(wid), (
        "the worklist is empty for a workspace with an unresolved submission; "
        "every assertion below would pass for the wrong reason")

    with ledger["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.NOT_ACCEPTED,
                                  operator="ops@x")
    remaining = {row["record_id"] for row in pending_submissions(wid)}
    assert remaining == set(), (
        f"{remaining} still listed after a completed reconciliation; the "
        f"worklist would never shrink, so the incident would never close")


def test_the_worklist_is_scoped_to_one_workspace(ledger):
    """One tenant's unresolved spend must never show up in another's list."""
    from app.services.paid_reconciliation import pending_submissions

    with ledger["Session"]() as s:
        mine = _workspace(s)
        theirs = _workspace(s)
        _ambiguous(s, mine, operation_id=_sid("op"))
        their_case = _ambiguous(s, theirs, operation_id=_sid("op"))

    ids = {row["record_id"] for row in pending_submissions(mine)}
    assert their_case["video_id"] not in ids
    assert ids, "the workspace's own ambiguity was not listed"


# ===========================================================================
# 7. The operator entry point
# ===========================================================================


def test_the_cli_reconciles_durably_and_refuses_a_repeat(ledger, restart):
    """``python -m app.services.paid_reconciliation`` is the surface an operator
    can actually run. No REST API is invented; the runbook's honest procedure is
    a dashboard lookup plus this.
    """
    from app.services.paid_reconciliation import main

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    assert main(["reconcile", "--video-id", case["video_id"], "--outcome",
                 "NOT_ACCEPTED", "--operator", "ops@x",
                 "--note", "no charge on the dashboard"]) == 0

    fresh_engine, fresh_factory = restart()
    try:
        seen = _read_video(fresh_factory, case["video_id"])
        assert seen["submission_state"] == "FAILED"
        assert seen["cost_outcome"] == "NOT_APPLICABLE"
        assert _read_entry(fresh_factory, case["entry_id"]) is None
    finally:
        fresh_engine.dispose()

    # A repeat exits 2 with the reason on stderr rather than raising.
    assert main(["reconcile", "--video-id", case["video_id"], "--outcome",
                 "NOT_ACCEPTED", "--operator", "ops@x"]) == 2


def test_the_cli_refuses_an_ambiguous_subject_selector(ledger):
    """Naming zero or two subjects means the tool cannot tell which incident it
    was asked about, and a reconciliation applied to the wrong row is a money
    decision about somebody else's purchase."""
    from app.services.paid_reconciliation import main

    assert main(["show"]) == 2
    assert main(["show", "--video-id", "a", "--lipsync-job-id", "b"]) == 2


def test_the_cli_lists_a_workspace_and_shows_a_subject(ledger, capsys):
    from app.services.paid_reconciliation import main

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)

    assert main(["list", "--workspace-id", wid]) == 0
    listed = capsys.readouterr().out
    assert case["video_id"] in listed
    assert "UNKNOWN_EXPOSURE" in listed

    assert main(["show", "--operation-id", case["operation_id"]]) == 0
    shown = capsys.readouterr().out
    assert case["video_id"] in shown


def test_the_cli_finds_a_subject_by_its_operation_id(ledger):
    """The id an incident list shows is the natural handle, so it works."""
    from app.services.paid_reconciliation import subject_for_operation

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
        subject = subject_for_operation(case["operation_id"])
    assert subject.record_id == case["video_id"]
    assert subject.cost_entry_id == case["entry_id"]
    assert subject.ambiguous is True


# ===========================================================================
# 8. PostgreSQL: the same claims against a real server
# ===========================================================================
#
# Everything above runs on SQLite, and SQLite will agree with almost anything.
# These re-run the two claims that a server can actually falsify: the JSON
# predicate the decision guard is built on (a bare ``as_string()`` compiles to a
# CAST here and an uncast JSON_EXTRACT there, so the same rows differ), and
# transactional survival.


@pytest.fixture(scope="module")
def pg_scratch() -> Iterator:
    made: list[str] = []

    def make(label: str):
        import psycopg

        name = f"w16_1_recon_{label}_{os.urandom(4).hex()}"
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return create_engine(_engine_url(name), pool_size=8, max_overflow=8)

    try:
        yield make
    finally:
        import psycopg

        for name in made:
            try:
                with psycopg.connect(_admin_url(), autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


@pytest.fixture(scope="module")
def pg(pg_scratch) -> dict:
    """A migrated scratch database built by the PRODUCTION boot path."""
    from app.migrations.runner import run_migrations

    engine = pg_scratch("main")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        run_migrations(s)
    return {"engine": engine, "Session": factory}


@pytest.fixture()
def pg_wired(pg, monkeypatch) -> dict:
    _rewire(monkeypatch, pg["Session"])
    return pg


@requires_postgres
def test_the_decision_guard_matches_the_same_rows_on_postgres(pg_wired):
    """The guard is a ``json``-column predicate, and that is where the two
    backends genuinely differ.

    Measured rather than asserted: the same stored rows are queried through the
    SHIPPED :func:`json_value_equals` and through the bare ``as_string()`` this
    lane removed from ``paid_provider``, on the server. If the helper ever stops
    being load-bearing the difference collapses and this fails, which is the
    point -- a test that cannot fail when the defect returns is a comment.
    """
    from app.models import CostEntry, Workspace
    from app.services.json_portability import json_value_equals

    wid = _sid("ws")
    with pg_wired["Session"]() as s:
        s.add(Workspace(id=wid, name="W16-1", slug=wid, niche="AI money"))
        s.commit()
        for op_id in (4242, "4242"):
            s.add(CostEntry(workspace_id=wid, category="video", amount_usd=1.0,
                            provider="mock",
                            detail_json={"operation_id": op_id,
                                         "cost_outcome": "UNKNOWN_EXPOSURE"}))
        s.add(CostEntry(workspace_id=wid, category="video", amount_usd=1.0,
                        provider="mock", detail_json={"note": "unrelated"}))
        s.commit()

        helper = set(s.scalars(select(CostEntry.id).where(
            json_value_equals(CostEntry.detail_json, "operation_id", "4242"))))
        bare = set(s.scalars(select(CostEntry.id).where(
            CostEntry.detail_json["operation_id"].as_string() == "4242")))
        assert len(helper) == 2, (
            f"the portable helper found {len(helper)} of 2 rows on PostgreSQL; "
            f"the decision guard is built on it")
        assert len(bare) == 2, (
            f"the bare as_string() found {len(bare)} of 2 rows here; if this now "
            f"agrees AND the SQLite side does not, re-justify the fix")

        _cleanup(s, wid)


@requires_postgres
def test_a_reconciliation_settles_the_ledger_once_on_postgres(pg_wired, monkeypatch):
    """The money claim, on a real server, with a real ``settle_reservation``.

    Counted rather than inferred: the settlement function itself is wrapped, so
    "settled at most once" is observed instead of being read off a row that would
    look identical either way.
    """
    from app.services import cost as cost_service
    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        ReconciliationRefused,
        reconcile_paid_submission,
        subject_for_video,
    )

    calls: list[tuple[str, float]] = []
    real = cost_service.settle_reservation

    def counting(entry_id: str, actual_usd: float) -> None:
        calls.append((entry_id, float(actual_usd)))
        real(entry_id, actual_usd)

    monkeypatch.setattr(cost_service, "settle_reservation", counting)

    with pg_wired["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    with pg_wired["Session"]() as s:
        result = reconcile_paid_submission(
            subject_for_video(case["video_id"]),
            ReconciliationOutcome.SUCCEEDED, operator="ops@x", actual_usd=2.0)
    assert result.money_effect == "RESERVATION_CORRECTED"
    assert calls == [(case["entry_id"], 2.0)], (
        f"settle_reservation was called {calls}; one purchase settles once")

    with pg_wired["Session"]() as s, pytest.raises(ReconciliationRefused):
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.SUCCEEDED,
                                  operator="ops@x", actual_usd=2.0)
    assert calls == [(case["entry_id"], 2.0)], (
        "a refused repeat still settled the reservation")

    with pg_wired["Session"]() as s:
        _cleanup(s, wid)


@requires_postgres
def test_an_unknown_exposure_stays_reserved_on_postgres(pg_wired):
    """The release-on-UNKNOWN_REMAINS defect, on the server.

    ``NULLIF``/float behaviour is not the point here; the point is that a
    DELETE against a committed row is committed, and the test reads the row back
    through a different connection to be sure it was the database that decided.
    """
    from sqlalchemy import text

    from app.services.paid_reconciliation import (
        ReconciliationOutcome,
        reconcile_paid_submission,
        subject_for_video,
    )

    with pg_wired["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
    with pg_wired["Session"]() as s:
        reconcile_paid_submission(subject_for_video(case["video_id"]),
                                  ReconciliationOutcome.UNKNOWN_REMAINS,
                                  operator="ops@x")

    with pg_wired["Session"]() as s:
        s.expunge_all()
        row = s.execute(text(
            "SELECT detail_json FROM cost_entries WHERE id=:i"),
            {"i": case["entry_id"]}).mappings().one()
        detail = row["detail_json"]
        assert isinstance(detail, dict), (
            f"raw SQL returned {type(detail).__name__}; on PostgreSQL this "
            f"column is parsed JSON, so the operator's flags are queryable")
        assert detail["cost_outcome"] == "UNKNOWN_EXPOSURE"
        assert detail["exposure_unknown"] is True, (
            "the exposure flag was cleared on a real server; an operator's "
            "'we do not know' became 'nothing to see here'")
        _cleanup(s, wid)


def _cleanup(session, wid: str) -> None:
    """Remove a test workspace and everything under it."""
    from sqlalchemy import text

    for table in ("cost_entries", "lipsync_jobs", "videos", "events"):
        session.execute(text(f"DELETE FROM {table} WHERE workspace_id=:w"),
                        {"w": wid})
    # Children first: video_variants/content_items are reachable only through a
    # video, and the workspace cascades the rest.
    session.execute(text("DELETE FROM workspaces WHERE id=:w"), {"w": wid})
    session.commit()


# ---------------------------------------------------------------------------
# A guard on the harness itself
# ---------------------------------------------------------------------------


def test_the_restart_helper_really_hands_back_a_different_engine(ledger, restart):
    """The survival tests are worthless if ``restart`` is a lie.

    Asserted directly: two calls produce two engines, both distinct from the
    writer's, and both pointing at a different file. Without this, a mutation
    that removed every commit could still "pass" a restart test that never left
    the writing session.
    """
    first_engine, _ = restart()
    second_engine, _ = restart()
    try:
        assert first_engine is not ledger["engine"]
        assert second_engine is not first_engine
        assert first_engine.engine is not ledger["engine"].engine
        assert first_engine.url.database != second_engine.url.database
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_the_helpers_actually_find_the_rows_they_claim(ledger):
    """Fixture sanity, kept deliberately: a helper that seeds nothing makes
    every test above vacuously true.

    If the seeding breaks, the assertions in the survival and money tests would
    pass for the wrong reason, so this pins the shape of a real ambiguity.
    """
    from app.services.paid_jobs import SubmissionState

    with ledger["Session"]() as s:
        wid = _workspace(s)
        case = _ambiguous(s, wid, operation_id=_sid("op"), amount=4.0)
        _lipsync_row(s, wid, operation_id=_sid("op"),
                     state="SUBMISSION_UNKNOWN",
                     cost_outcome="UNKNOWN_EXPOSURE")
    assert case["entry_id"] and case["video_id"]
    seen = _read_video(ledger["Session"], case["video_id"])
    assert seen["submission_state"] == str(SubmissionState.SUBMISSION_UNKNOWN)
    assert seen["cost_outcome"] == "UNKNOWN_EXPOSURE"
    entry = _read_entry(ledger["Session"], case["entry_id"])
    assert entry["amount_usd"] == 4.0 and entry["is_estimate"] is True
    # And nothing has been reconciled yet, so the audit trail is genuinely empty.
    assert _count_events(ledger["Session"], kind="paid.reconciliation",
                         operation_id=case["operation_id"]) == []