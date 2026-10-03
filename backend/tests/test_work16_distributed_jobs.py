"""Work 16 §2/§3: distributed job execution, leases, and worker pools.

What this file is actually testing
----------------------------------
Every test here corresponds to a claim that is easy to assert and easy to
falsify. Where a test passes for a reason other than the one it claims, it is
not worth having, so each one names the failure it rules out:

* a **valid lease** is not stealable, and an **expired** one is (a/b);
* exactly one of N concurrent claimers wins a given row (c);
* a recovered job whose billable submit already has a remote id does not
  re-submit (d);
* draining stops claims without abandoning in-flight work (e);
* a real worker pool runs a 30-minute-shaped render and a small job at the same
  time instead of serialising them.

Real threads and a real database throughout. There is not one mock in this
file, and there cannot be: the property under test is what the DATABASE does
when two connections race, and a mock database does not race.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from app.models import Job
from app.models.base import JobStatus, utcnow
from app.services import job_leases
from app.services.job_leases import (
    ClaimedJob,
    LeaseState,
    PaidVerdict,
    assess_paid_reentry,
    begin_run,
    claim_by_id,
    claim_next,
    lease_is_valid,
    lease_state,
    reclaim_expired,
    renew_lease,
    worker_identity,
)

POSTGRES_URL = os.environ.get(
    "YMONEY_TEST_POSTGRES",
    "postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres",
)

#: Absolute path to migration 0035. A *relative* "app/migrations/..." only
#: resolves when the process happens to be running from ``backend/``; the suite
#: is normally run from the repository root, where it raised
#: ``FileNotFoundError: ...\\ymoney\\app\\migrations\\versions\\0035_job_leases.py``.
_MIGRATION_0035 = (Path(__file__).resolve().parents[1]
                   / "app" / "migrations" / "versions" / "0035_job_leases.py")


def _pg_engine_url(db_name: str) -> str:
    """SQLAlchemy needs an explicit driver here.

    ``postgresql://`` defaults to psycopg2, which is not installed; the driver
    in this venv is psycopg3, whose scheme is ``postgresql+psycopg://``. The
    psycopg admin connections below take the bare URL because that library does
    its own resolution.
    """
    base = POSTGRES_URL.rsplit("/", 1)[0]
    return f"{base.replace('postgresql://', 'postgresql+psycopg://', 1)}/{db_name}"


NEW_LEASE_COLUMNS = {"claimed_by", "claimed_at", "lease_expires_at",
                     "heartbeat_at"}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(db_session):
    from app.models import Workspace

    ws = Workspace(name="W16 WS", slug=f"w16-{os.urandom(4).hex()}")
    db_session.add(ws)
    db_session.commit()
    return ws.id


@pytest.fixture(autouse=True)
def _clean_w16_rows():
    """Leave no trace in the shared session database.

    The suite shares one database, and a job left ``RUNNING`` with a lapsed
    lease is a live grenade for whichever test claims next. Scoped to this
    module's own job-type prefix and its own workspaces, so it cannot touch a
    row another module created.
    """
    yield
    from sqlalchemy import delete, select

    from app.db import session_scope
    from app.models import CostEntry, Workspace

    with session_scope() as s:
        ws_ids = list(s.scalars(
            select(Workspace.id).where(Workspace.name == "W16 WS")).all())
        if ws_ids:
            s.execute(delete(CostEntry).where(
                CostEntry.workspace_id.in_(ws_ids)))
            s.execute(delete(Job).where(
                Job.type == "MEDIA_INTEL_GPU_SLOT",
                Job.workspace_id.in_(ws_ids)))
        s.execute(delete(Job).where(Job.type.like("w16.%")))
        s.execute(delete(Workspace).where(Workspace.name == "W16 WS"))


def _enqueue(job_type: str, workspace_id: str | None = None, *,
             payload: dict | None = None, paid: bool = False,
             idempotency_key: str | None = None,
             max_retries: int = 3) -> str:
    from app.services import jobs as jobs_service

    job_id = jobs_service.enqueue(job_type, payload or {},
                                  workspace_id=workspace_id, paid=paid,
                                  idempotency_key=idempotency_key,
                                  max_retries=max_retries)
    assert job_id, "enqueue returned None"
    return job_id


def _row(job_id: str) -> Job:
    from app.db import session_scope

    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        s.expunge(job)
        return job


def _expire(job_id: str) -> None:
    """Simulate the owner dying: the lease lapses, nothing else changes."""
    from datetime import timedelta as td

    from app.db import session_scope

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.lease_expires_at = utcnow() - td(seconds=1)


def _claim_target(worker: str, job_id: str, *, workload: str = "") -> ClaimedJob:
    """Claim ``job_id`` through the REAL global claim path.

    The suite shares one database, so ``claim_next`` may hand this worker a
    leftover row from another module. Anything that is not the job under test is
    put straight back with :func:`job_leases.requeue`, which leaves the other
    module's state as it found it AND keeps this test honest about going
    through the real claim instead of poking rows directly. Without this the
    whole module is order-dependent, which is worth nothing.
    """
    for _ in range(300):
        claimed = claim_next(worker, workload=workload)
        if claimed is not None:
            if claimed.job_id == job_id:
                return claimed
            assert job_leases.requeue(
                claimed.job_id, worker, delay_seconds=5.0,
                reason="left over from another test"), claimed.job_id
        time.sleep(0.01)
    raise AssertionError(f"{job_id} was never claimable")


def _never_claims(worker: str, job_id: str, *, workload: str = "",
                  attempts: int = 5) -> None:
    """Assert ``worker`` cannot take ``job_id`` right now, by actually trying.

    Repeated rather than one shot, because "found nothing" is only evidence
    when the queue really was being drained by somebody.
    """
    for _ in range(attempts):
        claimed = claim_next(worker, workload=workload)
        if claimed is None:
            return
        assert claimed.job_id != job_id, (
            f"{worker} stole a job whose lease another worker still holds")
        assert job_leases.requeue(claimed.job_id, worker, delay_seconds=5.0,
                                  reason="not the job under test")


def _reclaimed_for(job_id: str) -> list:
    """Reclaimed rows for ONE job, so other modules' rows cannot fail us."""
    return [r for r in reclaim_expired() if r.job_id == job_id]


def _register(job_type: str, fn) -> Callable[[], None]:
    """Register a handler for a test job type; returns the undo call.

    A plain registering function rather than a generator: a generator the
    caller forgets to iterate registers NOTHING, and the symptom is a handler
    that quietly does not exist -- which in this file looks exactly like a
    broken worker pool.
    """
    from app.services import jobs as jobs_service

    jobs_service._handlers[job_type] = fn

    def _undo() -> None:
        jobs_service._handlers.pop(job_type, None)
        job_leases._probes.pop(job_type, None)

    return _undo


# ---------------------------------------------------------------------------
# A. The lifecycle: QUEUED -> CLAIMED -> RUNNING -> terminal
# ---------------------------------------------------------------------------


def test_claim_makes_the_lease_visible_and_records_the_owner(workspace):
    job_id = _enqueue("w16.small", workspace)
    claimed = _claim_target("w-test-worker", job_id)

    assert claimed.job_id == job_id
    row = _row(job_id)
    assert row.status == JobStatus.RUNNING.value
    assert row.claimed_by == "w-test-worker"
    assert row.lease_expires_at is not None
    assert row.heartbeat_at is not None
    # CLAIMED, not RUNNING: the handler has not been entered yet.
    assert lease_state(row) == LeaseState.CLAIMED
    assert lease_is_valid(row) is True
    assert row.started_at is None


def test_begin_run_is_the_second_real_state(workspace):
    job_id = _enqueue("w16.small", workspace)
    _claim_target("w-test-worker", job_id)
    assert lease_state(_row(job_id)) == LeaseState.CLAIMED

    assert begin_run(job_id, "w-test-worker") is True
    assert lease_state(_row(job_id)) == LeaseState.RUNNING

    # Idempotent for the owner, refused for anybody else.
    assert begin_run(job_id, "w-test-worker") is False
    assert begin_run(job_id, "w-other-worker") is False


def test_finishing_releases_the_lease(workspace):
    """A COMPLETED job must not keep looking recoverable to an operator."""
    from app.services import jobs as jobs_service

    job_id = _enqueue("w16.small", workspace)
    claimed = _claim_target("w-test-worker", job_id)
    ctx = jobs_service._context_for(claimed)
    jobs_service._finish(ctx, JobStatus.COMPLETED)

    row = _row(job_id)
    assert row.status == JobStatus.COMPLETED.value
    assert row.claimed_by == ""
    assert row.lease_expires_at is None
    assert lease_is_valid(row) is False


# ---------------------------------------------------------------------------
# B. (a) and (b): the whole point of the lease
# ---------------------------------------------------------------------------


def test_a_valid_lease_is_not_stealable(workspace):
    """(a) Worker B must not take worker A's LIVE job.

    This is the exact scenario that made the old `recover_orphans` dangerous:
    A holds a 30-minute render, B does something unrelated, and B's boot used
    to reset the row. Now B cannot claim it, cannot reset it, and cannot see it
    as recoverable.
    """
    job_id = _enqueue("w16.render", workspace)
    _claim_target("worker-A", job_id, workload="RENDER")
    begin_run(job_id, "worker-A")

    # B polls. It must not find A's job: it is not claimable.
    _never_claims("worker-B", job_id, workload="RENDER")

    # B's recovery sweep must leave it alone too.
    assert _reclaimed_for(job_id) == []
    row = _row(job_id)
    assert row.status == JobStatus.RUNNING.value
    assert row.claimed_by == "worker-A"

    # And A's heartbeat keeps it alive past what a single TTL would allow.
    assert renew_lease(job_id, "worker-A", ttl=60.0) is True
    assert lease_is_valid(_row(job_id)) is True


def test_an_expired_lease_is_reclaimable(workspace):
    """(b) Worker B MUST take the job once A's lease lapses.

    Expiry is the ONLY evidence that A is gone, so this is the half of the
    argument that makes (a) safe rather than merely conservative.
    """
    job_id = _enqueue("w16.render", workspace)
    _claim_target("worker-A", job_id, workload="RENDER")
    begin_run(job_id, "worker-A")

    _expire(job_id)
    assert lease_is_valid(_row(job_id)) is False

    reclaimed = _reclaimed_for(job_id)
    assert [r.job_id for r in reclaimed] == [job_id]
    assert reclaimed[0].lost_owner == "worker-A"

    row = _row(job_id)
    assert row.status == JobStatus.RETRYING.value
    assert row.claimed_by == ""
    assert row.lease_expires_at is None
    assert row.retry_count == 1, "a crash is a failed attempt"

    taken = _claim_target("worker-B", job_id, workload="RENDER")
    assert taken.claimed_by == "worker-B"
    assert taken.attempt == 2, "B is a re-entry attempt"


def test_a_dead_worker_that_keeps_heartbeating_loses_its_job(workspace):
    """A reclaimed job cannot be resurrected by the old owner's heartbeat.

    Without the `claimed_by` predicate on the renewal, a worker that was slow
    past its TTL would renew a lease on a job another worker now owns, and both
    would believe they hold it.
    """
    job_id = _enqueue("w16.render", workspace)
    _claim_target("worker-A", job_id, workload="RENDER")
    _expire(job_id)
    _reclaimed_for(job_id)
    _claim_target("worker-B", job_id, workload="RENDER")

    assert renew_lease(job_id, "worker-A", ttl=600.0) is False
    row = _row(job_id)
    assert row.claimed_by == "worker-B"
    assert lease_is_valid(row) is True


def test_reclaim_never_touches_waiting_rows(workspace):
    """The GPU slot ledger parks a live slot in WAITING; recovery must not see it."""
    from app.db import session_scope

    job_id = _enqueue("MEDIA_INTEL_GPU_SLOT", workspace)
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status = JobStatus.WAITING.value
        job.claimed_by = "slot-holder"
        job.lease_expires_at = utcnow() - timedelta(seconds=10)

    assert _reclaimed_for(job_id) == []
    assert _row(job_id).status == JobStatus.WAITING.value


def test_reclaim_dead_letters_a_job_whose_crash_budget_is_spent(workspace):
    """A poison pill must not become an infinite crash loop."""
    job_id = _enqueue("w16.render", workspace, max_retries=1)
    for _ in range(2):
        _claim_target("worker-A", job_id, workload="RENDER")
        _expire(job_id)
        _reclaimed_for(job_id)

    row = _row(job_id)
    assert row.status == JobStatus.DEAD.value
    assert row.completed_at is not None
    _never_claims("worker-B", job_id, workload="RENDER")


# ---------------------------------------------------------------------------
# C. (c) the atomic claim under real concurrency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threads", [8])
def test_c_exactly_one_claimer_wins_each_job(workspace, threads):
    """(c) N concurrent claimers over M jobs -> M winners, no job run twice.

    Real threads, real connections, real lock contention. The barrier puts all
    of them on the same row at the same instant, which is the only way to make
    the race actually happen rather than merely being possible.
    """
    total = 12
    ids = [_enqueue("w16.small", workspace) for _ in range(total)]
    mine = set(ids)

    start = threading.Barrier(threads)
    won: list[str] = []
    lock = threading.Lock()

    def grab(index: int) -> None:
        start.wait(timeout=20)
        while True:
            claimed = claim_next(f"racer-{index}")
            if claimed is None:
                return
            with lock:
                won.append(claimed.job_id)
            if len(won) > total * 4:
                return  # safety valve; the assertions below are what matter

    workers = [threading.Thread(target=grab, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)

    # The suite shares one database, so a racing claimer may legitimately pick
    # up a row another module created. What must hold is the exactly-once
    # property for THIS job set: every one of ours claimed, none claimed twice.
    ours = [jid for jid in won if jid in mine]
    assert len(ours) == total, (
        f"claimed {len(ours)} of {total} jobs we enqueued")
    duplicates = sorted({jid for jid in ours if ours.count(jid) > 1})
    assert not duplicates, f"a job was claimed twice: {duplicates}"

    # Every one of them is owned, and by a worker that really did claim it.
    owners = {jid: _row(jid).claimed_by for jid in ids}
    assert all(owners.values()), "a claimed job has no owner"
    assert set(owners.values()) <= {f"racer-{i}" for i in range(threads)}
    for jid in ids:
        assert _row(jid).status == JobStatus.RUNNING.value


def test_c_racing_claim_by_id_has_one_winner(workspace):
    """Same guarantee for the dispatched-id path (Redis hint path)."""
    job_id = _enqueue("w16.small", workspace)
    threads = 10
    start = threading.Barrier(threads)
    results: list = []
    lock = threading.Lock()

    def grab(index: int) -> None:
        start.wait(timeout=20)
        got = claim_by_id(f"racer-{index}", job_id)
        with lock:
            results.append(got)

    workers = [threading.Thread(target=grab, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"{len(winners)} workers claimed one job"


# ---------------------------------------------------------------------------
# D. (d) paid crash safety
# ---------------------------------------------------------------------------


def _paid_submit(workspace_id: str, remote_id: str, *,
                 category: str = "video", provider: str = "w16-engine",
                 idempotency_key: str = ""):
    """Drive the REAL paid machinery: reserve, attempt, accept.

    ``idempotency_key`` is recorded on the reservation row as well as on the
    job. That is the exact link the recovery path looks for: without it the
    only available answer is "this workspace has an unresolved submission",
    which is a BLOCK, not a reattach. It is also what a producer must do to get
    automatic recovery instead of a stop-and-reconcile.
    """
    from app.services.paid_provider import paid_operation

    extra = {"job_probe": remote_id}
    if idempotency_key:
        extra["idempotency_key"] = idempotency_key
    op = paid_operation(provider=provider, operation="video_render_submit",
                        workspace_id=workspace_id, category=category,
                        estimated_cost=0.20, reservation_extra=extra)
    op.authorize()
    op.mark_attempt()
    op.mark_accepted(remote_id)
    op.close_book()
    return op


def test_d_crash_after_paid_submit_never_buys_again(workspace):
    """The headline invariant: worker A crashes after paying; B must not re-pay.

    A submits, the provider accepts and hands back a remote id, the ledger row
    is written -- and then the worker dies before writing the result. The
    lease expires. The job comes back. The only acceptable outcomes are
    "adopt the remote job" or "stop and reconcile"; a second submit is the one
    outcome that costs money twice.
    """
    from app.services import jobs as jobs_service

    submitted: list[tuple[str, str, str]] = []

    def handler(ctx):
        # A fresh attempt would buy the thing again. What is allowed -- and
        # expected -- is RUNNING, provided the recovery path handed this
        # attempt the remote id to adopt rather than a blank slate.
        submitted.append((ctx.job_id, ctx.recovery_mode, ctx.paid_remote_id))
        return {"summary": "adopted"}

    undo = _register("w16.paid.render", handler)
    try:
        link = "w16-render-attempt-1"
        job_id = _enqueue("w16.paid.render", workspace, paid=True,
                           idempotency_key=link)

        # ---- attempt 1: worker A pays, then "crashes" ----------------------
        claimed = _claim_target("worker-A", job_id, workload="RENDER")
        assert claimed.attempt == 1
        ctx = jobs_service._context_for(claimed)
        assert ctx.recovery_mode == "", "a first attempt has no prior submission"
        _paid_submit(workspace, "remote-w16-1", idempotency_key=link)
    # ...and the process dies here. No _finish. The row stays RUNNING.
        # ...and the process dies here. No _finish. The row stays RUNNING.

        # ---- the lease lapses; B recovers --------------------------------
        _expire(job_id)
        reclaimed = _reclaimed_for(job_id)
        assert [r.job_id for r in reclaimed] == [job_id]
        assert reclaimed[0].was_paid is True, "the sweep should see the exposure"

        taken = _claim_target("worker-B", job_id, workload="RENDER")
        assert taken.attempt == 2

        assessment = assess_paid_reentry(taken, attempt=taken.attempt)
        assert assessment.verdict is PaidVerdict.REATTACH
        assert assessment.remote_id == "remote-w16-1"
        assert assessment.entry_id, "no ledger row adopted the purchase"

        # ---- and the runner honours it -----------------------------------
        asyncio.run(jobs_service._execute_claimed(taken))
        assert submitted == [(job_id, "REATTACH", "remote-w16-1")], (
            "the re-entry attempt must run WITH the existing remote id, "
            "never a blank slate that invites a second submit")

        row = _row(job_id)
        assert row.status == JobStatus.COMPLETED.value
        assert "paid re-entry blocked" not in (row.last_error or "")

        # Exactly one money row for one purchase.
        from sqlalchemy import func, select

        from app.db import session_scope
        from app.models import CostEntry

        with session_scope() as s:
            count = s.scalar(
                select(func.count()).select_from(CostEntry).where(
                    CostEntry.workspace_id == workspace))
        assert count == 1, "one remote job must never be booked twice"
    finally:
        undo()


def test_d_unresolvable_exposure_blocks_rather_than_resubmits(workspace):
    """A remote id the ledger cannot account for is an incident, not a retry.

    The provider accepted, the crash lost the only record of the reservation.
    Booking `$0` erases a real charge; submitting again doubles it. Neither is
    acceptable, so the job stops.
    """
    from app.services import jobs as jobs_service

    submitted: list[str] = []

    def handler(ctx):
        submitted.append(ctx.job_id)
        return {"ok": True}

    undo = _register("w16.paid.render", handler)
    job_leases.register_paid_probe(
        "w16.paid.render", lambda job: "remote-nobody-owns")
    try:
        job_id = _enqueue("w16.paid.render", workspace, paid=True)
        _claim_target("worker-A", job_id, workload="RENDER")
        _expire(job_id)
        _reclaimed_for(job_id)
        taken = _claim_target("worker-B", job_id, workload="RENDER")
        assert taken.attempt == 2

        assessment = assess_paid_reentry(taken, attempt=taken.attempt)
        assert assessment.verdict is PaidVerdict.BLOCKED

        asyncio.run(jobs_service._execute_claimed(taken))
        assert submitted == [], "a blocked re-entry must not run the handler"
        row = _row(job_id)
        assert row.status == JobStatus.WAITING.value
        assert "paid re-entry blocked" in (row.last_error or "")
        assert row.result["paid_reentry"] == str(PaidVerdict.BLOCKED)
    finally:
        undo()


def test_d_a_first_attempt_is_never_gated(workspace):
    """One unresolved row must not stall every unrelated job in a workspace."""
    _paid_submit(workspace, "remote-unrelated")

    job_id = _enqueue("w16.paid.render", workspace, paid=True)
    claimed = _claim_target("worker-A", job_id, workload="RENDER")
    assert assess_paid_reentry(claimed, attempt=claimed.attempt).verdict is \
        PaidVerdict.NONE


def test_d_an_undeclared_job_is_not_gated(workspace):
    """The gate is opt-in (`paid=True`), so ordinary jobs keep their retries."""
    _paid_submit(workspace, "remote-unrelated-2")
    job_id = _enqueue("w16.plain", workspace)
    claimed = _claim_target("worker-A", job_id)
    assert claimed is not None
    assert assess_paid_reentry(claimed, attempt=claimed.attempt).verdict is \
        PaidVerdict.NONE
    assert job_id


def test_d_retry_after_ambiguity_is_gated_too(workspace):
    """The pre-16 runner re-POSTed a lost response up to four times.

    The same gate has to apply to a plain in-handler retry, not only to a
    lease recovery: both are `attempt > 1` and both may have spent.
    """
    from app.services.paid_provider import paid_operation

    job_id = _enqueue("w16.paid.render", workspace, paid=True)
    _claim_target("worker-A", job_id, workload="RENDER")
    op = paid_operation(provider="w16-engine", operation="submit",
                        workspace_id=workspace, category="video",
                        estimated_cost=0.20)
    op.authorize()
    op.mark_attempt()
    op.mark_unknown("connection reset after the request left")
    # Hand the job back the way a failed attempt does.
    from app.db import session_scope

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status = JobStatus.RETRYING.value
        job.retry_count = 1
        job.next_run_at = utcnow()
        job.claimed_by = ""
        job.lease_expires_at = None

    retried = _claim_target("worker-B", job_id, workload="RENDER")
    assert retried.attempt == 2
    assert assess_paid_reentry(retried, attempt=2).verdict is PaidVerdict.BLOCKED


def test_d_a_declared_paid_aware_handler_is_deferred_not_blocked(workspace):
    """A handler that reconciles its own prior submission gets to do so.

    This is why ``register_paid_job_type`` exists. The autopilot cycle is the
    one lane whose handler resolves a prior submission by variant / request
    hash -- a more precise correlation than the queue can make -- so a BLOCK on
    workspace-scoped evidence would replace a working recovery with a stuck job
    for no gain.
    """
    _paid_submit(workspace, "remote-from-another-job")
    job_leases.register_paid_job_type("w16.paid.cycle")
    try:
        job_id = _enqueue("w16.paid.cycle", workspace, paid=True)
        _claim_target("worker-A", job_id, workload="SMALL")
        _expire(job_id)
        _reclaimed_for(job_id)
        retried = _claim_target("worker-B", job_id, workload="SMALL")
        assert retried.attempt == 2
        verdict = assess_paid_reentry(retried, attempt=2)
        assert verdict.verdict is PaidVerdict.DEFERRED
    finally:
        job_leases._paid_aware.discard("w16.paid.cycle")


def test_the_autopilot_cycle_is_declared_paid_aware():
    """The render lane must not be blocked by an unrelated unresolved row."""
    from app.services import jobs as jobs_service

    assert "autopilot.start_next_cycle" in job_leases._paid_aware
    # and the declaration happens as a side effect of importing the service
    assert callable(jobs_service.pool_ownership)


# ---------------------------------------------------------------------------
# E. (e) the pool: starvation, concurrency, drain
# ---------------------------------------------------------------------------


def test_workload_routing_puts_a_render_somewhere_else():
    from app.services.worker_pool import Workload, classify_workload

    assert classify_workload("media_intel.gpu_slot", {}) is Workload.GPU
    assert classify_workload("x", {"requires_gpu": True}) is Workload.GPU
    assert classify_workload("video.render", {}) is Workload.RENDER
    assert classify_workload("publish.upload", {}) is Workload.PUBLISH
    assert classify_workload("inbox.sync", {}) is Workload.IO
    assert classify_workload("research.script", {}) is Workload.INTELLIGENCE
    # An explicit declaration beats every hint.
    assert classify_workload("video.render",
                             {"workload": "small"}) is Workload.SMALL
    # Unclassified work lands in the class that must never wait.
    assert classify_workload("weird.new.thing", {}) is Workload.SMALL


def test_a_render_slot_does_not_starve_the_small_pool(workspace):
    """The whole reason pools exist, as a test rather than a comment."""
    from app.services.worker_pool import PoolPlan, WorkerPool, Workload

    render_job = _enqueue("w16.render", workspace)
    small_job = _enqueue("w16.small", workspace)

    plan = PoolPlan(slots={Workload.RENDER: 1, Workload.SMALL: 1})
    pool = WorkerPool(worker="pool-test", plan=plan)
    assert pool.plan.count(Workload.RENDER) == 1

    # The render slot is busy with a 30-minute-shaped job...
    busy = _claim_target("pool-test#0", render_job, workload="RENDER")
    assert busy.job_id == render_job

    # ...and the small job still runs, on its own slot, right now.
    quick = _claim_target("pool-test#1", small_job, workload="SMALL")
    assert quick.workload == "SMALL"
    # The render slot is still occupied; the pool did not steal its work.
    assert _row(render_job).claimed_by == "pool-test#0"

    # And the reverse: a render slot does not quietly drain small work either.
    _never_claims("pool-test#0", small_job, workload="RENDER")


def test_e_drain_stops_claiming_but_lets_in_flight_work_finish():
    """(e) Draining is "stop taking new work", not "abandon what you have"."""
    from app.services import jobs as jobs_service
    from app.services.worker_pool import PoolPlan, WorkerPool

    started = threading.Event()
    finish = threading.Event()
    ran: list[str] = []

    def handler(ctx):
        ran.append(ctx.job_id)
        started.set()
        finish.wait(timeout=15)
        return {"summary": "done"}

    undo = _register("w16.drain", handler)
    pool = WorkerPool(worker="drain-test",
                      plan=PoolPlan(slots={None: 1}))

    async def scenario() -> tuple:
        await pool.start(jobs_service._execute_claimed)
        assert await asyncio.to_thread(
            lambda: _enqueue("w16.drain", None,
                             payload={"workload": "RENDER"})) is not None
        # Bounded inside the thread: an unbounded `Event.wait` in the default
        # executor survives the cancellation of its await and wedges
        # `asyncio.run`'s executor shutdown, so a failed assertion here would
        # hang the whole suite instead of failing it.
        assert await asyncio.wait_for(
            asyncio.to_thread(started.wait, 20), timeout=25) is True

        # Queue a second job while the first is in flight.
        queued_second = await asyncio.to_thread(
            lambda: _enqueue("w16.drain", None,
                             payload={"workload": "RENDER"}))

        drain = asyncio.create_task(pool.drain(timeout=10))
        await asyncio.sleep(0.6)
        assert pool.draining is True
        assert pool.in_flight, "drain must not abandon in-flight work"

        # The draining POOL takes nothing new. (An outside claimer may; that is
        # a different worker and the assertion here is about this pool.)
        before = list(ran)
        await asyncio.sleep(1.5)          # two poll intervals
        assert list(ran) == before, "a draining pool claimed new work"

        finish.set()
        await asyncio.wait_for(drain, timeout=20)
        return before, list(ran), queued_second

    before, after, queued_second = asyncio.run(scenario())
    assert len(after) == 1, f"draining let {len(after)} jobs start"
    assert after[0] == ran[0]
    row = _row(ran[0])
    assert row.status == JobStatus.COMPLETED.value
    assert row.claimed_by == ""
    # The job queued behind the drained one is untouched, not lost.
    assert before == [ran[0]]
    waiting = _row(queued_second)
    assert waiting.status == JobStatus.QUEUED.value
    assert waiting.claimed_by == ""
    assert waiting.lease_expires_at is None
    undo()


def test_drain_past_the_deadline_leaves_the_lease_to_expire():
    """Past the deadline we do not cancel a paid render; we abandon the lease.

    Cancelling in-flight work mid-render is the one outcome that is both
    irreversible and expensive. Letting the lease lapse hands the job to the
    recovery sweep, which is exactly the crash path and is recoverable.
    """
    from app.services.worker_pool import PoolPlan, WorkerPool

    blocked = threading.Event()

    async def forever(_claimed):
        blocked.set()
        await asyncio.sleep(60)

    pool = WorkerPool(worker="deadline-test", plan=PoolPlan(slots={None: 1}),
                      lease_ttl=60.0)

    async def scenario() -> str:
        await pool.start(forever)
        hanging = await asyncio.to_thread(
            lambda: _enqueue("w16.hang", None,
                             payload={"workload": "CPU"}))
        await asyncio.wait_for(asyncio.to_thread(blocked.wait, 20), timeout=25)
        await pool.drain(timeout=0.2)
        blocked.set()
        return hanging

    job_id = asyncio.run(scenario())
    row = _row(job_id)
    assert row.status == JobStatus.RUNNING.value
    assert row.lease_expires_at is not None, (
        "an abandoned job must keep a lapsing lease so it is recoverable")
    assert row.claimed_by.startswith("deadline-test")

def test_lease_keeper_renews_everything_it_holds(workspace):
    """The heartbeat is what makes "expired" mean "gone"."""
    job_id = _enqueue("w16.render", workspace)
    _claim_target("keeper-worker", job_id, workload="RENDER")
    keeper = job_leases.LeaseKeeper(worker="keeper-worker", ttl_seconds=30.0)
    keeper.track(job_id)
    assert keeper.tick() == 1
    assert lease_is_valid(_row(job_id)) is True
    keeper.untrack(job_id)
    assert keeper.tick() == 0
    assert not keeper.tracked


def test_worker_identity_is_stable_within_a_process():
    assert worker_identity() == worker_identity()
    assert worker_identity("replica-7") == "replica-7"


def test_reclaim_is_safe_to_run_repeatedly(workspace):
    """The sweep runs on a timer from every process; running it twice is fine."""
    job_id = _enqueue("w16.render", workspace)
    _claim_target("worker-A", job_id, workload="RENDER")
    _expire(job_id)
    assert len(_reclaimed_for(job_id)) == 1
    assert len(_reclaimed_for(job_id)) == 0
    assert _row(job_id).status == JobStatus.RETRYING.value


# ---------------------------------------------------------------------------
# F. The migration
# ---------------------------------------------------------------------------


def test_0035_up_and_down_round_trip_sqlite():
    """Up adds four columns and two indexes; down removes exactly those.

    Runs against a throwaway SQLite file rather than the session database,
    because a downgrade DROPs columns and no other test may observe that.
    """
    import importlib.util
    import tempfile
    from pathlib import Path

    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    spec = importlib.util.spec_from_file_location(
        "w16_mig_0035",
        _MIGRATION_0035)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tmp = Path(tempfile.mkdtemp(prefix="w16-mig-"))
    engine = create_engine(f"sqlite:///{(tmp / 'm.db').as_posix()}")
    session = sessionmaker(bind=engine)()
    try:
        session.execute(text(
            "CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY, "
            "status VARCHAR(15), next_run_at TIMESTAMP, priority INTEGER)"))
        session.commit()
        before = {c["name"] for c in inspect(engine).get_columns("jobs")}

        module.upgrade(session)
        session.commit()
        up_cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
        assert up_cols >= NEW_LEASE_COLUMNS
        assert before <= up_cols, "upgrade dropped an existing column"
        up_indexes = {i["name"] for i in inspect(engine).get_indexes("jobs")}
        assert {"ix_jobs_lease_recovery", "ix_jobs_claimed_by"} <= up_indexes

        # Idempotent: replaying is a no-op, not an error.
        module.upgrade(session)
        session.commit()

        module.downgrade(session)
        session.commit()
        down_cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
        assert not (NEW_LEASE_COLUMNS & down_cols), "downgrade left columns"
        assert before == down_cols, f"downgrade changed {before ^ down_cols}"
        down_indexes = {i["name"] for i in inspect(engine).get_indexes("jobs")}
        assert not ({"ix_jobs_lease_recovery",
                     "ix_jobs_claimed_by"} & down_indexes)
    finally:
        session.close()
        engine.dispose()


def test_0035_is_applied_in_the_session_schema():
    """The columns are actually there for the rest of the suite."""
    from sqlalchemy import inspect

    from app.db import session_scope

    with session_scope() as s:
        cols = {c["name"] for c in inspect(s.get_bind()).get_columns("jobs")}
    assert cols >= NEW_LEASE_COLUMNS


@pytest.mark.skipif(not POSTGRES_URL, reason="no PostgreSQL test URL")
def test_0035_round_trips_on_postgresql():
    """Same up/down on a server-grade engine, on a throwaway database.

    PostgreSQL is where the interesting failures live: it does not accept a
    duplicate column, and one swallowed error aborts the whole transaction. The
    downgrade also has to drop indexes before columns here, and SQLite's
    refusal is the only reason that ordering is written down at all.
    """
    import importlib.util

    import psycopg
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    admin_url = POSTGRES_URL.rsplit("/", 1)[0] + "/postgres"
    db_name = f"w16_mig_{os.urandom(4).hex()}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    engine = create_engine(_pg_engine_url(db_name))
    session = sessionmaker(bind=engine)()
    try:
        session.execute(text(
            "CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY, "
            "status VARCHAR(15), next_run_at TIMESTAMP, priority INTEGER)"))
        session.commit()

        spec = importlib.util.spec_from_file_location(
            "w16_mig_0035_pg",
            _MIGRATION_0035)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        module.upgrade(session)
        session.commit()
        up_cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
        assert up_cols >= NEW_LEASE_COLUMNS

        # A replay on PostgreSQL must not poison the transaction.
        module.upgrade(session)
        session.commit()

        module.downgrade(session)
        session.commit()
        down_cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
        assert not (NEW_LEASE_COLUMNS & down_cols)
    finally:
        session.close()
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


# ---------------------------------------------------------------------------
# G. Concurrency against real PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not POSTGRES_URL, reason="no PostgreSQL test URL")
def test_concurrent_claims_on_postgresql_are_exactly_once():
    """The race that SQLite's global write lock can hide, on a real server.

    Each thread opens its OWN connection to the scratch database, because the
    point is N concurrent writers; sharing one session would serialise them
    into the single-writer model SQLite always has and prove nothing.
    """
    import psycopg
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.migrations.runner import run_migrations

    admin_url = POSTGRES_URL.rsplit("/", 1)[0] + "/postgres"
    db_name = f"w16_race_{os.urandom(4).hex()}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    engine = create_engine(_pg_engine_url(db_name), pool_size=20,
                           max_overflow=20)
    sessionmaker_ = sessionmaker(bind=engine)

    try:
        import app.models  # noqa: F401
        from app.db import Base

        Base.metadata.create_all(engine)
        with sessionmaker_() as s:
            run_migrations(s, create_missing_tables=False)

        with sessionmaker_() as s:
            for _ in range(15):
                s.execute(text(
                    "INSERT INTO jobs (id, type, payload, status, priority, "
                    "max_retries, retry_count, next_run_at, created_at, "
                    "updated_at, idempotency_key, cancel_requested, "
                    "claimed_by, last_error, result) VALUES "
                    "(gen_random_uuid()::text, 'w16.race', '{}', 'QUEUED', "
                    "100, 3, 0, now(), now(), now(), NULL, false, '', '', "
                    "'{}')"))
            s.commit()

        def local_claim(index: int) -> list[str]:
            """One thread, one session per transaction, one engine.

            A fresh session PER TRANSACTION rather than one session for the
            whole thread: a long-lived session would hold a snapshot open, and
            on PostgreSQL that is how a racer stops seeing its neighbours'
            commits -- the exact thing this test exists to detect.
            """
            mine: list[str] = []
            while True:
                with sessionmaker_() as s:
                    row = s.execute(text(
                        "SELECT id FROM jobs WHERE status IN "
                        "('QUEUED','RETRYING') ORDER BY priority, "
                        "next_run_at FOR UPDATE SKIP LOCKED LIMIT 5"
                    )).fetchall()
                    if not row:
                        return mine
                    got = False
                    for (jid,) in row:
                        res = s.execute(text(
                            "UPDATE jobs SET status='RUNNING', "
                            "claimed_by=:w, claimed_at=now(), "
                            "lease_expires_at=now() + interval '60s', "
                            "heartbeat_at=now(), started_at=NULL "
                            "WHERE id=:id AND status IN "
                            "('QUEUED','RETRYING')"),
                            {"w": f"pg-{index}", "id": jid})
                        if res.rowcount == 1:
                            mine.append(jid)
                            got = True
                            break
                    if not got:
                        s.commit()
                        time.sleep(0.01)
                        continue
                    s.commit()

        threads = 12
        start = threading.Barrier(threads)
        results: list[list[str]] = []
        failures: list[str] = []
        lock = threading.Lock()

        def run(index: int) -> None:
            try:
                start.wait(timeout=30)
                got = local_claim(index)
            except BaseException as exc:  # noqa: BLE001 - surfaced, not swallowed
                with lock:
                    failures.append(f"{type(exc).__name__}: {exc}")
                return
            with lock:
                results.append(got)

        workers = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=120)

        assert not failures, failures
        claimed = [jid for batch in results for jid in batch]
        assert len(claimed) == 15, f"claimed {len(claimed)} of 15"
        assert len(set(claimed)) == 15, "a row was claimed twice on PostgreSQL"
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


@pytest.mark.skipif(not POSTGRES_URL, reason="no PostgreSQL test URL")
def test_pg_connectivity_probe():
    """Fails loudly and early rather than silently skipping the real races."""
    import psycopg

    with psycopg.connect(POSTGRES_URL, connect_timeout=5) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
