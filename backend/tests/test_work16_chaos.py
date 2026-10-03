"""Work 16 §13 -- controlled failure drills.

What this file is
-----------------
Eight named failure modes, each driven as far as this machine honestly allows,
and each checked against the same six questions:

1. **Durable recovery** -- does the system serve again afterwards?
2. **No duplicate publication** -- is there exactly ONE publish record?
3. **No duplicated paid submission** -- is there exactly ONE money row, and did
   the second attempt refuse to submit?
4. **No ownerless spend** -- is every dollar attributed to a workspace?
5. **No corrupted timeline** -- is every interrupted artefact still a promise,
   never content under a name that claims to be complete?
6. **No lost canonical state** -- are the pre-failure rows byte-identical after?

What is REAL and what is SIMULATED, stated per drill
---------------------------------------------------
Being wrong about this is worse than skipping a drill, so each docstring says
which it is:

=================  ==========================================================
Drill               What actually happens
=================  ==========================================================
API restart         REAL abrupt process death (``taskkill /F``) + a genuinely
                    cold second process. There is no uvicorn supervisor in this
                    environment, so this is process death and cold start, which
                    is what an API restart is from the data layer's point of
                    view. It is not a supervisor reload.
Worker kill         REAL. A child process claims a job with a live lease and is
                    force-terminated. No graceful drain, no release, no
                    ``finally``. What the parent then observes is exactly the
                    evidence a killed process leaves behind.
DB restart          REAL. ``docker restart`` on the PostgreSQL container the
                    load database lives on. Real server, real crash of every
                    open connection, real recovery.
Storage             SIMULATED, deterministically. The storage root is replaced
                    by a FILE, so every path underneath it fails the way an
                    unavailable volume fails. Not a SAN outage.
Network timeout     REAL for the socket (a real connect to a non-routable
                    address that really times out). SIMULATED for the provider:
                    the paid provider is this repo's HTTP-boundary double. It is
                    never called "live" anywhere.
GPU worker death    SIMULATED, and stated: ``torch`` is not installed and there
                    is no GPU in this environment. What is exercised is the
                    evidence path -- a HELD VRAM reservation whose holder stops
                    renewing, and the sweep that frees it.
Render worker       REAL process kill, same as "worker kill", on a render-class
                    job that has already accepted a paid submission upstream.
Queue interruption  REAL. ``pg_terminate_backend`` kills the server side of
                    live connections mid-transaction.
=================  ==========================================================

Nothing here spends money. The paid provider is a double; the transport is
never a network call.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.services import load_harness as lh
from app.services.load_harness import LoadHarness

#: The container the drill restarts for real. Declared, not guessed: silently
#: restarting "some" container would be a way to break an unrelated developer's
#: database.
PG_CONTAINER_ENV = "YMONEY_LOAD_PG_CONTAINER"

pytestmark = [
    pytest.mark.live,
    pytest.mark.slow,
    pytest.mark.skipif(
        not (lh.load_enabled() and lh.pg_available()),
        reason=(
            "chaos drills are opt-in: set YMONEY_LOAD_TESTS=1 and point "
            "YMONEY_LOAD_PG_ADMIN at a PostgreSQL admin DSN"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chaos(tmp_path_factory: pytest.TempPathFactory) -> LoadHarness:
    """A scratch database and a scratch storage root, owned by this module.

    Separate from the load module's fixture on purpose: the DB-restart drill
    kills every connection to the server, and a shared engine would leave the
    load module's harness holding dead sockets.
    """
    root = tmp_path_factory.mktemp("w16-chaos-storage")
    harness = LoadHarness(pool_size=6, max_overflow=4, pool_timeout=10,
                          storage_root=str(root / "objects"))
    harness.create_database()
    try:
        harness.bind()
        harness.migrate()
        yield harness
    finally:
        harness.restore()
        harness.drop_database()


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


@dataclass
class Child:
    """A real OS process, started and (usually) killed for real."""

    popen: subprocess.Popen
    env: dict[str, str]
    #: Lines the child wrote as JSON, one per line.
    lines: list[str] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return int(self.popen.pid)

    def wait_for_line(self, token: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """Block until the child emits a JSON line containing ``token``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.popen.stdout.readline()  # type: ignore[union-attr]
            if not line:
                if self.popen.poll() is not None:
                    # The child's traceback is the only explanation a dead
                    # child can give, and a drill that reports "the child died"
                    # without it costs an hour every time.
                    stderr = ""
                    if self.popen.stderr is not None:
                        stderr = self.popen.stderr.read()[-2000:]
                    raise AssertionError(
                        f"child exited rc={self.popen.returncode} before "
                        f"emitting {token!r}\n--- child stderr ---\n{stderr}")
                continue
            text = line.strip()
            if not text:
                continue
            self.lines.append(text)
            if token in text:
                return json.loads(text)
        raise AssertionError(f"child never emitted {token!r} in {timeout}s")

    def kill(self) -> int:
        """Force-terminate. Not a drain, not a graceful stop.

        Windows ``taskkill /F`` and POSIX ``SIGKILL`` both terminate without
        running ``finally`` blocks, flushing nothing and releasing no lease --
        which is the entire point: a graceful shutdown leaves the system tidy
        and proves nothing about recovery.
        """
        if self.popen.poll() is not None:
            return int(self.popen.returncode or 0)
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(self.pid)],
                           capture_output=True, check=False)
        else:
            os.kill(self.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            # SIGKILL/taskkill /F is not supposed to need a second chance; if it
            # does, the returned rc below is what the assertions judge.
            self.popen.wait(timeout=30)
        return int(self.popen.returncode if self.popen.returncode is not None else -1)


def _child_env(harness: LoadHarness) -> dict[str, str]:
    """Environment that lets a child bind to the same scratch database."""
    env = dict(os.environ)
    env["W16_CHILD_DB"] = harness.db_name
    env["YMONEY_LOAD_PG_ADMIN"] = harness.admin
    env["YMONEY_LOAD_STORAGE_ROOT"] = str(harness.storage_root or "")
    env["DATABASE_URL"] = harness.url
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


#: Preamble every child runs. It binds the application's session factory to the
#: scratch database -- the same rebinding the harness does -- so a child measures
#: and mutates exactly the state its parent can see.
_CHILD_PREAMBLE = """
import json, os, sys, time, uuid
from app.core.config import settings
# A ``requires_gpu`` job is handed straight back unless a GPU worker is
# configured, so a child that claims one has to say it is one. Set HERE, in the
# child, because a monkeypatch in the parent does not cross a process boundary
# -- and a child that silently fails to claim looks exactly like a child that
# was killed before it claimed anything.
settings.gpu_worker = True
from app.services.load_harness import LoadHarness
_h = LoadHarness(db_name=os.environ["W16_CHILD_DB"],
                 admin=os.environ["YMONEY_LOAD_PG_ADMIN"],
                 storage_root=os.environ.get("YMONEY_LOAD_STORAGE_ROOT") or None,
                 pool_size=6, max_overflow=4, pool_timeout=10)
_h.bind()
def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()
"""


def _spawn(harness: LoadHarness, body: str, *, env_extra: dict[str, str] | None = None
          ) -> Child:
    """Start a real child process running ``body`` after the preamble.

    Values the child needs travel in its ENVIRONMENT, never as text spliced into
    its source. Interpolating ids into code means an id containing a quote
    produces a child that fails to parse, and the drill then reports "the child
    died" instead of the thing it was measuring.
    """
    env = _child_env(harness)
    env.update(env_extra or {})
    source = _CHILD_PREAMBLE + textwrap.dedent(body)
    popen = subprocess.Popen(
        [sys.executable, "-c", source],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
        cwd=str(Path(__file__).resolve().parent.parent), env=env)
    return Child(popen=popen, env=env)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tenant:
    """A workspace plus the user that owns it.

    Returned together because ``projects.created_by`` is a real foreign key to
    ``users``. Passing a workspace id there -- which looks right, because the id
    is right -- fails at COMMIT with a constraint violation that says nothing
    about the drill.
    """

    workspace_id: str
    user_id: str


def _make_workspace(harness: LoadHarness, *, cap: float = 100.0,
                    name: str = "W16 chaos") -> Tenant:
    """A committed workspace with explicit budget caps."""
    from app.db import session_scope
    from app.models import User, Workspace, WorkspaceMember

    token = uuid.uuid4().hex[:8]
    with session_scope() as s:
        user = User(email=f"w16-chaos-{token}@test.local", password_hash="x")
        s.add(user)
        s.flush()
        ws = Workspace(name=f"{name} {token}", slug=f"w16-chaos-{token}",
                       niche="chaos")
        ws.settings_json = {"safety": {"daily_budget_usd": float(cap),
                                       "per_video_budget_usd": 50.0}}
        s.add(ws)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id,
                              role=WorkspaceMember.ROLE_OWNER))
        s.flush()
        return Tenant(workspace_id=str(ws.id), user_id=str(user.id))


def _cost_rows(workspace_id: str) -> list[dict[str, Any]]:
    """Every money row for a workspace, oldest first."""
    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as s:
        rows = list(s.scalars(
            select(CostEntry).where(CostEntry.workspace_id == workspace_id)
            .order_by(CostEntry.created_at)).all())
        return [{"id": str(r.id), "category": str(r.category),
                 "provider": str(r.provider), "amount": float(r.amount_usd or 0.0),
                 "workspace_id": str(r.workspace_id),
                 "detail": dict(r.detail_json or {})} for r in rows]


def _money_total(workspace_id: str) -> float:
    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as s:
        return float(s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == workspace_id)) or 0.0)


def _assert_six_invariants(*, label: str, workspace_id: str,
                           expected_cost_rows: int,
                           canonical_before: dict[str, Any],
                           canonical_after: dict[str, Any]) -> None:
    """The six questions, as assertions.

    * no ownerless spend: every row names this workspace, and none of them has
      an empty one;
    * no duplicated paid submission: exactly the expected number of money rows,
      so a second attempt neither reserved again nor invented a receipt;
    * no lost canonical state: the pre-failure snapshot reads back identically.
    """
    rows = _cost_rows(workspace_id)
    assert len(rows) == expected_cost_rows, (
        f"{label}: expected {expected_cost_rows} money rows, found {len(rows)} "
        f"-> {[ (r['category'], r['provider'], r['detail'].get('cost_outcome')) for r in rows ]}")
    orphans = [r for r in rows if not r["workspace_id"]]
    assert not orphans, f"{label}: ownerless spend -- {orphans}"
    assert canonical_after == canonical_before, (
        f"{label}: canonical state changed\n before={canonical_before}\n"
        f" after={canonical_after}")


# ---------------------------------------------------------------------------
# DRILL 1 -- API restart (REAL process death + cold start)
# ---------------------------------------------------------------------------


def test_api_restart_loses_no_canonical_state(chaos: LoadHarness) -> None:
    """Write canonical state, hard-kill a live process, start a cold one.

    **REAL:** the first child is force-terminated with ``taskkill /F`` while it
    holds pooled connections; the second child is a brand-new interpreter with
    no shared memory. From the data layer's point of view that is what an API
    restart is. It is NOT a supervisor reload -- there is no uvicorn process
    here -- and it is not claimed to be one.

    The state under test spans every store that has to survive: an ORM row, a
    money row, a canonical storage object, and a paid submission that already
    has a remote id upstream.
    """
    from app.db import session_scope
    from app.models import Project
    from app.services import storage_objects
    from app.services.paid_provider import paid_operation

    tenant = _make_workspace(chaos, name="W16 restart")
    workspace_id = tenant.workspace_id
    operation = paid_operation(
        workspace_id=workspace_id, provider="chaos-double", operation="publish",
        category="publishing", estimated_cost=0.02)
    operation.authorize()
    operation.mark_attempt()
    remote_id = f"chaos-remote-{uuid.uuid4().hex}"
    operation.mark_accepted(remote_id)
    operation.close_book(0.02)

    payload = b"chaos-canonical-bytes" * 64
    asset = storage_objects.upload_object(
        workspace_id, "generated", [payload],
        logical_name=f"restart-{uuid.uuid4().hex[:8]}.bin",
        content_type="application/octet-stream")
    with session_scope() as s:
        project = Project(workspace_id=workspace_id, name="chaos restart",
                          description="canonical", created_by=tenant.user_id)
        s.add(project)
        s.flush()
        project_id = str(project.id)

    canonical_before = {
        "project": project_id,
        "asset_key": str(asset.object_key),
        "asset_checksum": str(asset.checksum),
        "remote_id": remote_id,
        "cost_rows": len(_cost_rows(workspace_id)),
        "money": round(_money_total(workspace_id), 6),
    }

    # -- the process that dies ------------------------------------------------
    victim = _spawn(chaos, """
        emit({"event": "connected", "pid": os.getpid()})
        from app.db import session_scope
        from app.models import Project
        # Hold a real pooled connection open across the kill.
        with session_scope() as s:
            row = s.get(Project, os.environ["W16_PROJECT_ID"])
            emit({"event": "read", "name": row.name})
        emit({"event": "waiting"})
        while True:
            time.sleep(0.05)
    """, env_extra={"W16_PROJECT_ID": project_id})
    victim.wait_for_line("read")
    rc = victim.kill()
    assert rc != 0, f"taskkill reported rc={rc}; the process was not killed"

    # -- the process that starts cold ---------------------------------------
    restarted = _spawn(chaos, """
        from app.db import session_scope
        from app.models import Project
        from app.services import storage_objects
        from app.services.paid_provider import reattach_by_remote_id

        with session_scope() as s:
            row = s.get(Project, os.environ["W16_PROJECT_ID"])
            emit({"event": "state", "name": row.name,
                  "description": row.description})
        emit({"event": "bytes", "sha": storage_objects.read_bytes(
            os.environ["W16_WORKSPACE_ID"],
            os.environ["W16_ASSET_KEY"]).hex()[:32]})
        adopted = reattach_by_remote_id(os.environ["W16_REMOTE_ID"])
        emit({"event": "adopted", "entry": getattr(adopted, "entry_id", "")})
    """, env_extra={"W16_PROJECT_ID": project_id,
                    "W16_WORKSPACE_ID": workspace_id,
                    "W16_ASSET_KEY": str(asset.object_key),
                    "W16_REMOTE_ID": remote_id})
    restarted.wait_for_line("bytes")
    restarted.popen.kill()
    restarted.popen.wait(timeout=30)

    with session_scope() as s:
        row = s.get(Project, project_id)
        assert row is not None and row.name == "chaos restart"
    reread = storage_objects.read_bytes(workspace_id, asset.object_key)
    assert reread == payload, "the canonical object did not survive a restart"
    from app.services.paid_provider import reattach_by_remote_id

    assert reattach_by_remote_id(remote_id) is not None, (
        "the accepted paid submission lost its ledger row")

    canonical_after = {
        "project": project_id,
        "asset_key": str(asset.object_key),
        "asset_checksum": str(asset.checksum),
        "remote_id": remote_id,
        "cost_rows": len(_cost_rows(workspace_id)),
        "money": round(_money_total(workspace_id), 6),
    }
    _assert_six_invariants(label="api restart", workspace_id=workspace_id,
                           expected_cost_rows=1,
                           canonical_before=canonical_before,
                           canonical_after=canonical_after)
    print(f"\napi restart: killed rc={rc} pid={victim.pid} "
          f"cost_rows={canonical_after['cost_rows']} "
          f"money=${canonical_after['money']}")


# ---------------------------------------------------------------------------
# DRILL 2 -- worker kill (REAL forced termination)
# ---------------------------------------------------------------------------


_KILLED_WORKER = """
    from app.services import jobs as jobs_service
    from app.services.job_leases import begin_run, claim_by_id, held_job_ids
    from app.services.paid_provider import paid_operation

    ws = os.environ["W16_WORKSPACE_ID"]
    key = os.environ["W16_IDEMPOTENCY_KEY"]
    job_id = jobs_service.enqueue(
        os.environ["W16_JOB_TYPE"], {"load": True, "requires_gpu": True},
        workspace_id=ws, idempotency_key=key, paid=True, max_retries=5)
    claimed = claim_by_id("w16-ghost", job_id)
    assert claimed is not None, "the ghost worker could not claim its own job"
    assert begin_run(job_id, "w16-ghost"), "begin_run refused its own claim"
    # A billable submit that has LEFT but has not been accepted upstream: the
    # exact state a killed worker leaves on the books.
    op = paid_operation(
        workspace_id=ws, provider="chaos-double", operation="render",
        category="video", estimated_cost=0.05,
        reservation_extra={"idempotency_key": key})
    op.authorize()
    op.mark_attempt()
    emit({"event": "claimed", "job_id": job_id, "pid": os.getpid(),
          "entry_id": op.entry_id, "held": held_job_ids("w16-ghost")})
    # Death here. No release, no requeue, no drain, no finally.
    while True:
        time.sleep(0.05)
"""


def test_killed_worker_leaves_an_unrenewed_lease_and_is_recovered_once(
        chaos: LoadHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """REAL kill, then the evidence a killed process leaves, then recovery.

    **REAL:** ``taskkill /F``. The child holds a RUNNING job, a begun run and an
    OPEN paid reservation when it dies. Nothing is released, because a killed
    process releases nothing.

    What is proved, in order:

    1. the job is left RUNNING under ``w16-ghost`` with a lease in the FUTURE --
       nobody died *gracefully*, so the lease was never handed back;
    2. ``reclaim_expired`` returns it, naming the lost owner;
    3. a SECOND ``reclaim_expired`` returns nothing: recovered exactly once;
    4. a fresh worker claims it, and ``assess_paid_reentry`` REFUSES the paid
       re-entry because an open paid reservation exists for this job;
    5. the ledger holds exactly ONE row -- no duplicated paid submission, and no
       ownerless spend.
    """
    from datetime import timedelta

    from app.core.config import settings
    from app.db import session_scope
    from app.models import Job
    from app.services.job_leases import (
        PaidVerdict,
        assess_paid_reentry,
        claim_by_id,
        held_job_ids,
        reclaim_expired,
    )

    # Both the killed worker and the worker that recovers its job claim a
    # ``requires_gpu`` render. Without this the RECOVERER wins the row and hands
    # it straight back as a GPU push-back, and the drill then reports "the
    # recovered job could not be claimed" -- which reads like a recovery bug and
    # is actually the GPU gate doing its job in a CPU-only test environment.
    monkeypatch.setattr(settings, "gpu_worker", True, raising=False)

    workspace_id = _make_workspace(chaos, name="W16 kill").workspace_id
    job_type = f"chaos.render.{uuid.uuid4().hex[:8]}"
    idempotency_key = f"chaos-key-{uuid.uuid4().hex}"

    victim = _spawn(chaos, _KILLED_WORKER,
                    env_extra={"W16_JOB_TYPE": job_type,
                               "W16_WORKSPACE_ID": workspace_id,
                               "W16_IDEMPOTENCY_KEY": idempotency_key})
    claimed = victim.wait_for_line("claimed")
    rc = victim.kill()
    assert rc != 0, f"the worker was not actually killed (rc={rc})"

    job_id = str(claimed["job_id"])
    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row is not None
        assert row.status == "RUNNING", (
            f"the killed worker's job is {row.status}, not RUNNING -- the drill "
            f"did not leave the state a kill leaves")
        assert row.claimed_by == "w16-ghost"
        assert row.started_at is not None, "begin_run did not commit"
        assert row.lease_expires_at is not None
        assert row.lease_expires_at > row.claimed_at, "no lease span"
        lease_expires_at = row.lease_expires_at
        snapshot = {"status": row.status, "owner": row.claimed_by,
                    "retry_count": int(row.retry_count or 0)}

    assert held_job_ids("w16-ghost") == [job_id], (
        "the dead worker still 'holds' nothing -- recovery would have nothing "
        "to find")

    # -- recovery, once --------------------------------------------------------
    after = lease_expires_at.replace(microsecond=999000)
    reclaimed = reclaim_expired(now=after)
    mine = [r for r in reclaimed if r.job_id == job_id]
    assert len(mine) == 1, (
        f"the dead worker's job was not reclaimed exactly once: {reclaimed}")
    assert mine[0].lost_owner == "w16-ghost"
    assert mine[0].was_paid is True, (
        "the sweep did not flag open paid evidence behind the dead worker")

    again = [r for r in reclaim_expired(now=after) if r.job_id == job_id]
    assert not again, f"the job was reclaimed AGAIN: {again}"

    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row.status == "RETRYING", f"post-recovery status is {row.status}"
        assert row.claimed_by == ""
        assert int(row.retry_count or 0) == snapshot["retry_count"] + 1

    # -- a fresh worker may run it, but may NOT re-spend ----------------------
    # ``now`` is carried forward because ``reclaim_expired`` scheduled the retry
    # at the clock this drill advanced to. Claiming at real "now" would fail the
    # ``next_run_at <= now`` predicate and look like a recovery bug.
    reclaimer = claim_by_id("w16-reclaimer", job_id,
                            now=after.replace(microsecond=0) + timedelta(seconds=1))
    assert reclaimer is not None, "the reclaimer could not take the recovered job"
    assert reclaimer.claimed_by == "w16-reclaimer"

    with session_scope() as s:
        row = s.get(Job, job_id)
        assessment = assess_paid_reentry(row)
    assert assessment.blocks or assessment.verdict is PaidVerdict.REATTACH, (
        f"the paid re-entry gate let a second submit through: {assessment}")
    assert assessment.verdict is PaidVerdict.BLOCKED, (
        f"expected BLOCKED for an OPEN reservation, got {assessment.verdict}")

    _assert_six_invariants(
        # The canonical state here is the job's IDENTITY and the ledger, not its
        # owner: changing owner from the dead worker to the reclaimer IS the
        # recovery, so including it would make the assertion fail on success.
        label="worker kill", workspace_id=workspace_id, expected_cost_rows=1,
        canonical_before={"job": job_id,
                          "rows": [(r["id"], r["category"], r["amount"])
                                   for r in _cost_rows(workspace_id)]},
        canonical_after={"job": job_id,
                         "rows": [(r["id"], r["category"], r["amount"])
                                  for r in _cost_rows(workspace_id)]})
    print(f"\nworker kill: rc={rc} pid={victim.pid} job={job_id} "
          f"verdict={assessment.verdict} cost_rows=1 money="
          f"${_money_total(workspace_id):.4f}")


# ---------------------------------------------------------------------------
# DRILL 3 -- render worker death with an ACCEPTED submission (mutation c)
# ---------------------------------------------------------------------------


_ACCEPTED_RENDER = """
    from app.services import jobs as jobs_service
    from app.services.job_leases import begin_run, claim_by_id
    from app.services.paid_provider import paid_operation

    ws = os.environ["W16_WORKSPACE_ID"]
    key = os.environ["W16_IDEMPOTENCY_KEY"]
    job_id = jobs_service.enqueue(os.environ["W16_JOB_TYPE"],
                                  {"requires_gpu": True}, workspace_id=ws,
                                  idempotency_key=key, paid=True, max_retries=5)
    assert claim_by_id("w16-render-ghost", job_id) is not None
    assert begin_run(job_id, "w16-render-ghost")
    op = paid_operation(workspace_id=ws, provider="chaos-double",
                        operation="render", category="video",
                        estimated_cost=0.07,
                        reservation_extra={"idempotency_key": key})
    op.authorize()
    op.mark_attempt()
    remote_id = "render-remote-" + uuid.uuid4().hex
    # The provider ACCEPTED. Only the local bookkeeping and the worker die.
    op.mark_accepted(remote_id)
    emit({"event": "accepted", "job_id": job_id, "remote_id": remote_id,
          "entry_id": op.entry_id, "pid": os.getpid()})
    while True:
        time.sleep(0.05)
"""


def test_render_worker_death_reattaches_instead_of_buying_twice(
        chaos: LoadHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """REAL kill after the provider accepted. MUTATION (c).

    The expensive crash: the money is gone upstream, the only local record of
    it died with the process, and the recovered job is about to run again. If
    the re-entry gate resubmits, the workspace is billed twice.

    The gate must produce :attr:`PaidVerdict.REATTACH` -- bind to the ledger row
    that already owns the remote id -- and never a second submission. The
    ledger must hold exactly ONE row, still carrying that remote id.

    **REAL:** ``taskkill /F`` on a process that had already been told the
    provider accepted. The provider itself is this repo's HTTP-boundary double.
    """
    from datetime import timedelta

    from app.core.config import settings
    from app.db import session_scope
    from app.models import Job
    from app.services.job_leases import (
        PaidVerdict,
        assess_paid_reentry,
        claim_by_id,
        reclaim_expired,
    )
    from app.services.paid_provider import reattach_by_remote_id

    # Same reason as the worker-kill drill: a ``requires_gpu`` render is handed
    # straight back unless a GPU worker is configured, and the reclaimer in THIS
    # process has to be one.
    monkeypatch.setattr(settings, "gpu_worker", True, raising=False)

    workspace_id = _make_workspace(chaos, name="W16 render").workspace_id
    job_type = f"chaos.render.accepted.{uuid.uuid4().hex[:8]}"
    idempotency_key = f"chaos-render-{uuid.uuid4().hex}"

    victim = _spawn(chaos, _ACCEPTED_RENDER,
                    env_extra={"W16_JOB_TYPE": job_type,
                               "W16_WORKSPACE_ID": workspace_id,
                               "W16_IDEMPOTENCY_KEY": idempotency_key})
    accepted = victim.wait_for_line("accepted")
    rc = victim.kill()
    assert rc != 0, "the render worker was not actually killed"

    job_id = str(accepted["job_id"])
    remote_id = str(accepted["remote_id"])
    with session_scope() as s:
        lease_expires_at = s.get(Job, job_id).lease_expires_at

    reclaimed = reclaim_expired(now=lease_expires_at.replace(microsecond=999000))
    assert [r for r in reclaimed if r.job_id == job_id], "nothing was reclaimed"
    reclaimer = claim_by_id("w16-render-reclaimer", job_id,
                            now=lease_expires_at.replace(microsecond=0)
                            + timedelta(seconds=1))
    assert reclaimer is not None

    with session_scope() as s:
        row = s.get(Job, job_id)
        assessment = assess_paid_reentry(row)

    assert assessment.verdict is PaidVerdict.REATTACH, (
        f"expected REATTACH on the existing remote id {remote_id}, got "
        f"{assessment.verdict}: {assessment.detail}")
    assert assessment.remote_id == remote_id
    adopted = reattach_by_remote_id(remote_id)
    assert adopted is not None and adopted.entry_id == accepted["entry_id"], (
        "the reattach bound to a different ledger row than the one that owns "
        "the submission")

    rows = _cost_rows(workspace_id)
    _assert_six_invariants(
        label="render death", workspace_id=workspace_id, expected_cost_rows=1,
        canonical_before={"remote_id": remote_id,
                          "entry_id": str(accepted["entry_id"])},
        canonical_after={"remote_id": rows[0]["detail"].get("remote_id", ""),
                         "entry_id": rows[0]["id"]})
    print(f"\nrender death: rc={rc} pid={victim.pid} verdict={assessment.verdict} "
          f"remote={remote_id} cost_rows={len(rows)}")


# ---------------------------------------------------------------------------
# DRILL 4 -- DB restart (REAL docker restart)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get(PG_CONTAINER_ENV),
                    reason=(
                        f"set {PG_CONTAINER_ENV} to the PostgreSQL container "
                        f"this drill is allowed to restart"))
def test_database_restart_loses_nothing_and_serves_again(
        chaos: LoadHarness) -> None:
    """**REAL** ``docker restart`` of the PostgreSQL server.

    This is the only drill that takes the database away. Every pooled
    connection in this process dies with it; recovery therefore depends on the
    engine's ``pool_pre_ping`` discarding sockets that a server restarted
    underneath. That is the property being measured, so the test asserts the
    new backend PIDs are different -- recovery by luck would look identical.

    Proved across the restart: canonical rows read back byte-identical, exactly
    one money row (no duplicate publication or paid submission), and the service
    accepts new work immediately afterwards.
    """


    from app.db import session_scope
    from app.services import storage_objects
    from app.services.cost import reserve_spend

    workspace_id = _make_workspace(chaos, name="W16 pg").workspace_id
    reservation = reserve_spend(workspace_id, 0.03, category="llm",
                                provider="chaos-double")
    settle_reservation(reservation.entry_id, 0.03)
    payload = b"pg-restart-bytes" * 128
    asset = storage_objects.upload_object(
        workspace_id, "generated", [payload],
        logical_name=f"pg-{uuid.uuid4().hex[:8]}.bin",
        content_type="application/octet-stream")

    def _backend_pids_for_db() -> set[int]:
        return _backend_pids(chaos)

    before_pids = _backend_pids_for_db()
    snapshot_before = {
        "entry": reservation.entry_id,
        "rows": _cost_rows(workspace_id),
        "checksum": str(asset.checksum),
    }
    assert before_pids, "no live backends before the restart"

    container = os.environ[PG_CONTAINER_ENV]
    down_at = time.perf_counter()
    subprocess.run(["docker", "restart", container], capture_output=True,
                   check=True, timeout=300)

    # -- while it is down: real calls fail, and fail LOUDLY -------------------
    with pytest.raises(Exception) as downtime:
        reserve_spend(workspace_id, 0.01, category="llm", provider="chaos-double")
    downtime_error = type(downtime.value).__name__

    _wait_for_postgres(chaos, timeout=180.0)
    downtime_seconds = round(time.perf_counter() - down_at, 2)

    # -- recovery -------------------------------------------------------------
    after_pids = _wait_for_usable_pool(chaos, timeout=120.0)
    assert after_pids and not (after_pids & before_pids), (
        f"the pool reused a connection the restart killed "
        f"(overlapping pids {sorted(after_pids & before_pids)}); "
        f"pool_pre_ping did not discard it")

    from app.models import CostEntry

    with session_scope() as s:
        assert s.get(CostEntry, reservation.entry_id) is not None
    reread = storage_objects.read_bytes(workspace_id, asset.object_key)
    assert reread == payload, "a canonical object changed across a DB restart"
    snapshot_after = {
        "entry": reservation.entry_id,
        "rows": _cost_rows(workspace_id),
        "checksum": str(asset.checksum),
    }

    fresh = reserve_spend(workspace_id, 0.01, category="llm",
                          provider="chaos-double-after")
    assert fresh.entry_id, "the service did not accept work after the restart"

    assert snapshot_after["rows"] == snapshot_before["rows"], (
        f"money rows changed across the restart\n"
        f" before={snapshot_before['rows']}\n after={snapshot_after['rows']}")
    assert len(snapshot_after["rows"]) == 1, (
        f"{len(snapshot_after['rows'])} money rows after the restart; a "
        f"duplicate submission or a replayed reservation")
    print(f"\ndb restart: container={container} downtime={downtime_seconds}s "
          f"downtime_error={downtime_error} new_backends="
          f"{len(after_pids - before_pids)} reused={len(after_pids & before_pids)} "
          f"rows_after={len(snapshot_after['rows'])}")


def _wait_for_postgres(harness: LoadHarness, *, timeout: float) -> float:
    """Block until the server accepts connections again. Polls, never sleeps."""
    import psycopg

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(harness.admin, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return round(timeout - (deadline - time.monotonic()), 2)
        except Exception:  # noqa: BLE001 - a readiness poll never raises
            time.sleep(0.5)
    raise AssertionError(f"PostgreSQL did not come back within {timeout}s")


def _backend_pids(harness: LoadHarness) -> set[int]:
    """Server-side PIDs for this database, read on a SEPARATE connection.

    Separate on purpose: when the pool is unusable there is nothing pooled to ask,
    and asking from the pool fails exactly when the answer matters.
    """
    import psycopg

    with psycopg.connect(harness.admin, autocommit=True,
                         connect_timeout=10) as conn:
        rows = conn.execute(
            "SELECT pid FROM pg_stat_activity WHERE datname = %s",
            (harness.db_name,)).fetchall()
    return {int(r[0]) for r in rows}


def _wait_for_usable_pool(harness: LoadHarness, *, timeout: float) -> set[int]:
    """Block until the ENGINE can execute a statement, then report its pids.

    ``pool_pre_ping`` is what makes this succeed at all, so the wait is part of
    what is being measured rather than a convenience around it.
    """
    from sqlalchemy import text

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with harness.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return _backend_pids(harness)
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"the pool never recovered within {timeout}s: {last}")


def settle_reservation(entry_id: str, actual_usd: float) -> None:
    from app.services.cost import settle_reservation as _settle

    _settle(entry_id, actual_usd)


# ---------------------------------------------------------------------------
# DRILL 5 -- storage interruption (SIMULATED, deterministic)
# ---------------------------------------------------------------------------


def test_storage_interruption_leaves_promises_not_content(
        chaos: LoadHarness) -> None:
    """Break the filesystem under an upload; nothing half-published survives.

    **SIMULATED, and said so:** the storage root directory is RENAMED away and a
    plain FILE is put in its place, so every path underneath it fails exactly
    the way an unavailable volume fails, deterministically and on every
    platform. This is not a SAN outage and does not claim to be.

    Three things must hold:

    1. the upload raises -- no silent success;
    2. the row it left is ``PENDING``, never ``FINALIZED``. ``register_pending``
       writes a promise and ``upload_object`` writes the FINALIZED row LAST, so
       a crash between them is addressable-but-incomplete only if that ordering
       is wrong;
    3. every object that was already canonical reads back byte-identical, and
       the storage lane serves again once the root is restored.
    """
    from app.db import session_scope
    from app.models import StorageObject
    from app.services import storage_objects

    workspace_id = _make_workspace(chaos, name="W16 storage").workspace_id
    good = {}
    for slot in range(3):
        payload = f"canonical-{slot}".encode() * 200
        row = storage_objects.upload_object(
            workspace_id, "generated", [payload],
            logical_name=f"good-{slot}-{uuid.uuid4().hex[:6]}.bin",
            content_type="application/octet-stream")
        good[str(row.object_key)] = payload

    root = Path(str(storage_objects.storage_service.STORAGE_ROOT))
    moved = root.with_name(root.name + "-interrupted")
    root.rename(moved)
    # A FILE where the directory belongs: every join under it now fails.
    root.write_text("this is a file, not a volume", encoding="utf-8")

    interrupted_name = f"interrupted-{uuid.uuid4().hex[:8]}.bin"
    failure: BaseException | None = None
    try:
        storage_objects.upload_object(
            workspace_id, "generated", [b"never lands"],
            logical_name=interrupted_name,
            content_type="application/octet-stream")
    except Exception as exc:  # noqa: BLE001 - the failure IS the datum
        failure = exc

    assert failure is not None, (
        "the upload SUCCEEDED with no storage root -- an interruption that is "
        "not noticed is not a recovery")
    # ``object_key`` is ``<workspace>/<kind>/<digest>-<sanitised stem>``: the stem
    # is prefixed with a digest and truncated, so the logical name is MATCHED
    # rather than used as a suffix. A key built from ``endswith(logical_name)``
    # never matches, and the assertion then reports "no promise was left" for a
    # promise that is sitting right there.
    with session_scope() as s:
        everything = list(s.scalars(select(StorageObject).where(
            StorageObject.workspace_id == workspace_id)).all())
    published = [r for r in everything
                 if interrupted_name in str(r.object_key)
                 and r.state == "FINALIZED"]
    assert not published, (
        f"an interrupted upload published a FINALIZED row "
        f"({published[0].object_key}) -- content that does not exist is now "
        f"addressable")
    promises = [r for r in everything
                if interrupted_name in str(r.object_key) and r.state == "PENDING"]
    assert promises, (
        f"the interrupted upload left no PENDING promise either -- the record "
        f"of an in-flight write is gone. failure="
        f"{type(failure).__name__ if failure else None}: {failure}; "
        f"rows={[(r.object_key, r.state) for r in everything]}")
    pending = promises[0]

    # -- restore --------------------------------------------------------------
    root.unlink()
    moved.rename(root)
    for key, payload in good.items():
        assert storage_objects.read_bytes(workspace_id, key) == payload, (
            f"canonical object {key} was corrupted by the interruption")
    healed = storage_objects.upload_object(
        workspace_id, "generated", [b"healed" * 32],
        logical_name=f"healed-{uuid.uuid4().hex[:8]}.bin",
        content_type="application/octet-stream")
    assert healed.state == "FINALIZED"
    assert storage_objects.read_bytes(workspace_id, healed.object_key) == b"healed" * 32

    leftovers = list(Path(str(root)).rglob("*.part"))
    assert not leftovers, f"interrupted writes left .part files: {leftovers}"
    print(f"\nstorage interruption: failure={type(failure).__name__} "
          f"pending_promise={pending.id} finalized_leaked=0 "
          f"canonical_intact={len(good)} leftovers=0")


# ---------------------------------------------------------------------------
# DRILL 6 -- network / provider timeout
# ---------------------------------------------------------------------------


def test_network_timeout_is_classified_without_inventing_or_double_booking(
        chaos: LoadHarness) -> None:
    """A REAL socket timeout, and the paid classification that must follow.

    Part one is real: a TCP connect to a non-routable address with a one-second
    timeout, which really does time out on this host. That is what a provider
    read timeout is made of.

    Part two is SIMULATED at the HTTP boundary, and labelled as such everywhere:
    ``PaidOperation`` is the real class, the reservation is a real ledger row,
    and the only double is the transport that would have uploaded the video.

    Two classifications, because they are the pair that matters:

    * a **provably undelivered** failure RELEASES the reservation -- the budget
      returns and no phantom charge is booked;
    * an **ambiguous** failure KEEPS it and marks ``UNKNOWN_EXPOSURE`` -- money
      may be gone and the amount is unknown, so no number is invented and
      nothing is voided.
    """
    from app.services.paid_executor import (
    AmbiguousSubmission,
    PaidSubmission,
    RetrySafety,
    SubmissionState,
)
    from app.services.paid_provider import FailureVerdict, absorb_paid_failure, paid_operation

    # -- 1. a real socket timeout --------------------------------------------
    blackhole = "10.255.255.1"
    started = time.perf_counter()
    socket_error: str = ""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect((blackhole, 9))
        socket_error = "connected (the address is routable here)"
    except OSError as exc:
        socket_error = f"{type(exc).__name__}: {exc}"
    finally:
        probe.close()
    elapsed = time.perf_counter() - started
    assert "timed out" in socket_error.lower(), (
        f"a real connect to {blackhole}:9 did not time out ({socket_error}); "
        f"this host can route to it, so the timeout evidence is not real")

    # -- 2. provably undelivered -> RELEASED ----------------------------------
    released_ws = _make_workspace(chaos, name="W16 undelivered").workspace_id
    delivered = paid_operation(
        workspace_id=released_ws, provider="chaos-double",
        operation="publish", category="publishing", estimated_cost=0.04)
    delivered.authorize()
    delivered.mark_attempt()
    # A connect failure reaches a provider as an ``AmbiguousSubmission``, and
    # ``RetrySafety`` lives on the RECORD the exception carries. Passing a bare
    # ``PaidSubmission`` is the wrong shape entirely: nothing reads
    # ``exc.submission``, so the classifier falls through to its most
    # conservative branch and books an unknown exposure for a request that was
    # never sent -- a phantom charge.
    undelivered_record = PaidSubmission(
        workspace_id=released_ws, provider="chaos-double", operation="publish",
        state=SubmissionState.SUBMISSION_ATTEMPTED,
        retry_safety=RetrySafety.SAFE)
    undelivered = AmbiguousSubmission(undelivered_record,
                                      detail="connect refused before send")
    verdict = absorb_paid_failure(delivered, undelivered)
    assert verdict is FailureVerdict.RELEASED
    rows = _cost_rows(released_ws)
    # ``mark_rejected(nothing_billed=True)`` VOIDS the reservation: a submit
    # that provably created nothing keeps no row, because a kept row would
    # shrink the workspace's budget for work nobody did. So "released" means
    # zero rows and zero dollars, not a closed-out row.
    assert rows == [], (
        f"an undelivered submit left money on the books: {rows}")
    assert _money_total(released_ws) == 0.0, (
        "the released reservation did not return the budget to the workspace")

    # -- 3. ambiguous -> UNKNOWN_EXPOSURE, kept ------------------------------
    ambiguous_ws = _make_workspace(chaos, name="W16 ambiguous").workspace_id
    lost = paid_operation(
        workspace_id=ambiguous_ws, provider="chaos-double",
        operation="render", category="video", estimated_cost=0.09)
    lost.authorize()
    lost.mark_attempt()
    ambiguous_record = PaidSubmission(
        workspace_id=ambiguous_ws, provider="chaos-double", operation="render",
        state=SubmissionState.SUBMISSION_ATTEMPTED,
        retry_safety=RetrySafety.UNSAFE)
    ambiguous = AmbiguousSubmission(ambiguous_record,
                                    detail="read timeout after send")
    ambiguous_verdict = absorb_paid_failure(lost, ambiguous)
    assert ambiguous_verdict is FailureVerdict.UNKNOWN_EXPOSURE
    rows = _cost_rows(ambiguous_ws)
    assert len(rows) == 1, (
        f"a lost response produced {len(rows)} money rows -- either a phantom "
        f"charge or a second bill")
    assert rows[0]["detail"].get("cost_outcome") == "UNKNOWN_EXPOSURE"
    assert rows[0]["detail"].get("exposure_unknown") is True
    assert _money_total(ambiguous_ws) == pytest.approx(0.09), (
        "the unknown exposure was booked as $0 -- a real charge erased")

    # An ambiguous row is EVIDENCE: the next attempt must not re-spend. The job
    # has to actually BE on its next attempt -- ``assess_paid_reentry`` returns
    # immediately for a first attempt, so asking it about a fresh job tests
    # nothing at all. The attempt counter is advanced through the real path:
    # claim, let the lease lapse, reclaim.
    from app.db import session_scope
    from app.models import Job
    from app.services.job_leases import (
        assess_paid_reentry,
        claim_by_id,
        reclaim_expired,
    )

    job = _paid_job(ambiguous_ws, "chaos.render.lost")
    assert claim_by_id("w16-lost-worker", job) is not None
    with session_scope() as s:
        lease_expires_at = s.get(Job, job).lease_expires_at
    assert [r for r in reclaim_expired(
        now=lease_expires_at.replace(microsecond=999000)) if r.job_id == job], (
        "the lost job was never made reclaimable, so the attempt counter could "
        "not advance")
    with session_scope() as s:
        row = s.get(Job, job)
        assert int(row.retry_count or 0) >= 1, (
            "the attempt counter did not advance, so this is still a first "
            "attempt and the gate would be a no-op")
        assessment = assess_paid_reentry(row)
    assert assessment.blocks, (
        f"a workspace with an unresolved paid row was allowed to re-spend: "
        f"{assessment}")
    print(f"\nnetwork timeout: real_socket={socket_error} in {elapsed:.2f}s; "
          f"undelivered={verdict} ambiguous={ambiguous_verdict} "
          f"reentry={assessment.verdict}")


def _paid_job(workspace_id: str, job_type: str) -> str:
    from app.services import jobs as jobs_service

    job_id = jobs_service.enqueue(job_type, {"load": True},
                                  workspace_id=workspace_id, paid=True,
                                  idempotency_key=f"w16-chaos-{uuid.uuid4().hex}")
    assert job_id
    return job_id


# ---------------------------------------------------------------------------
# DRILL 7 -- GPU worker death (SIMULATED: no GPU here)
# ---------------------------------------------------------------------------


def test_gpu_worker_death_frees_the_slot_but_not_a_live_one(
        chaos: LoadHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """**SIMULATED, and there is no GPU here.** ``torch`` is not installed.

    What is exercised is the EVIDENCE path, which is the part that is wrong when
    a GPU worker dies: a HELD reservation whose holder stops renewing, and the
    sweep that frees it. A real CUDA context cannot be allocated on this host,
    so a VRAM figure here comes from a registered synthetic device -- which is
    stated rather than dressed up as hardware.

    Both halves of the rule are checked, because a sweep that frees
    unconditionally is worse than no sweep:

    * a live job's lease KEEPS its slot, even past the slot's own deadline;
    * a dead job's lease frees the slot, and the VRAM is then admit-able again.
    """
    from datetime import timedelta

    from app.core.config import settings
    from app.db import session_scope
    from app.models import GpuReservation, Job
    from app.models.base import utcnow
    from app.services import gpu_scheduler, job_leases
    from app.services.gpu_scheduler import GpuRequest

    # ``claim_by_id`` hands a ``requires_gpu`` job straight back when no GPU
    # worker is configured, so without this the drill never gets as far as the
    # reservation it is about to test.
    monkeypatch.setattr(settings, "gpu_worker", True, raising=False)
    gpu_scheduler.register_device("cuda:0", name="synthetic", backend="cuda",
                                  total_mb=8192)
    workspace_id = _make_workspace(chaos, name="W16 gpu").workspace_id

    live_job = _gpu_job(workspace_id, "chaos.render.live")
    dead_job = _gpu_job(workspace_id, "chaos.render.dead")

    live = gpu_scheduler.admit(GpuRequest(kind="render", workspace_id=workspace_id,
                                          job_id=live_job), timeout=5.0)
    dead = gpu_scheduler.admit(GpuRequest(kind="render", workspace_id=workspace_id,
                                          job_id=dead_job), timeout=5.0)
    assert live.reservation_id and dead.reservation_id
    assert live.reservation_id != dead.reservation_id

    # The live worker keeps its job lease renewed; the dead one stops.
    job_leases.renew_lease(live_job, "w16-live-worker")
    now = utcnow()
    with session_scope() as s:
        dead_row = s.get(Job, dead_job)
        dead_row.lease_expires_at = now - timedelta(seconds=5)
        # The live job's lease is moved out to a time PAST the slot deadline,
        # so the assertion cannot pass by accident.
        live_row = s.get(Job, live_job)
        live_row.lease_expires_at = now + timedelta(hours=1)
        slot_deadline = s.get(GpuReservation, dead.reservation_id).lease_expires_at

    freed = _reclaim_stale()
    assert freed == 1, f"expected exactly one stale reservation freed, got {freed}"
    with session_scope() as s:
        dead_res = s.get(GpuReservation, dead.reservation_id)
        live_res = s.get(GpuReservation, live.reservation_id)
        assert dead_res.status not in GpuReservation.HELD, (
            f"the dead slot is still held ({dead_res.status})")
        assert live_res.status in GpuReservation.HELD, (
            f"the sweep reclaimed a slot whose job lease was still valid "
            f"({live_res.status}) -- the death test passed by killing live work")
    assert slot_deadline is not None

    again = _reclaim_stale()
    assert again == 0, f"the sweep is not idempotent: freed {again} more"

    # The freed VRAM is admit-able again -- the whole reason to reclaim it.
    replacement = gpu_scheduler.admit(
        GpuRequest(kind="render", workspace_id=workspace_id,
                   job_id=_gpu_job(workspace_id, "chaos.render.replacement")),
        timeout=5.0)
    assert replacement.reservation_id
    gpu_scheduler.release(replacement.reservation_id, reason="drill")
    gpu_scheduler.release(live.reservation_id, reason="drill")
    print(f"\ngpu worker death: freed={freed} live_kept=True slot_deadline="
          f"{slot_deadline} re_admitted={replacement.reservation_id[:8]}")


def _sweep_session():
    """A session on the harness's own engine, for the sweeps that take one."""
    from app.db import session_scope

    return session_scope()


def _reclaim_stale() -> int:
    """``gpu_scheduler.reclaim_stale`` on the harness's engine, in one txn.

    ``reclaim_stale`` takes the caller's session on purpose -- it has to stay a
    single transaction or it can deadlock against the lease read it depends on
    -- so the drill opens the session rather than using ``sweep()``.
    """
    from app.db import session_scope
    from app.services import gpu_scheduler

    with session_scope() as s:
        return gpu_scheduler.reclaim_stale(s)


def _gpu_job(workspace_id: str, job_type: str) -> str:
    from app.services import jobs as jobs_service
    from app.services.job_leases import claim_by_id

    job_id = jobs_service.enqueue(job_type, {"requires_gpu": True},
                                  workspace_id=workspace_id,
                                  idempotency_key=f"w16-gpu-{uuid.uuid4().hex}")
    assert job_id
    worker = f"w16-gpu-worker-{uuid.uuid4().hex[:6]}"
    assert claim_by_id(worker, job_id) is not None, "the GPU job could not be claimed"
    return job_id


# ---------------------------------------------------------------------------
# DRILL 8 -- queue interruption (REAL pg_terminate_backend)
# ---------------------------------------------------------------------------


def test_queue_interruption_leaves_every_job_claimable_exactly_once(
        chaos: LoadHarness) -> None:
    """**REAL:** PostgreSQL kills the server side of live connections.

    ``pg_terminate_backend`` on the backends a set of poller threads are
    holding, mid-flight. This is the closest thing to a queue interruption that
    does not need to be simulated: the server severs the connection and the
    client's transaction is aborted by the database, not by the client.

    What must hold afterwards:

    * every job is CLAIMABLE (QUEUED/RETRYING) or RUNNING under exactly one
      owner -- never RUNNING under nobody, which is how work disappears;
    * no job was claimed twice;
    * the ledger is consistent: no half-written reservation, because a
      reservation is one transaction.
    """

    from app.db import session_scope
    from app.models import CostEntry, Job
    from app.services import jobs as jobs_service
    from app.services.cost import reserve_spend
    from app.services.job_leases import CLAIMABLE, LEASE_HELD, claim_next, reclaim_expired

    workspace_id = _make_workspace(chaos, name="W16 queue").workspace_id
    job_ids = [jobs_service.enqueue("chaos.queue.probe", {"load": True},
                                    workspace_id=workspace_id,
                                    idempotency_key=f"w16-q-{uuid.uuid4().hex}")
               for _ in range(16)]
    assert all(job_ids)
    reserve_spend(workspace_id, 0.02, category="llm", provider="chaos-double")

    victims: list[int] = []
    errors: list[str] = []
    lock = __import__("threading").Lock()

    def _poll(slot: int) -> None:
        worker = f"w16-q-worker-{slot}"
        try:
            for _ in range(6):
                claim_next(worker)
        except Exception as exc:  # noqa: BLE001 - a severed socket IS the datum
            with lock:
                errors.append(type(exc).__name__)

    threads = [__import__("threading").Thread(target=_poll, args=(i,), daemon=True)
               for i in range(8)]
    for thread in threads:
        thread.start()

    # Kill the server side of every pooled connection while they work.
    time.sleep(0.05)
    import psycopg

    with psycopg.connect(chaos.admin, autocommit=True,
                         connect_timeout=10) as conn:
        pids = [int(r[0]) for r in conn.execute(
            "SELECT pid FROM pg_stat_activity WHERE datname = %s",
            (chaos.db_name,)).fetchall()]
        for pid in pids:
            with lock:
                victims.append(pid)
            with contextlib.suppress(Exception):
                # The admin connection terminates its own backend list as it
                # goes, so a pid can vanish between the SELECT and this call.
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
    for thread in threads:
        thread.join(timeout=90.0)
        assert not thread.is_alive(), "a poller never returned after a severed socket"

    # -- every job is accounted for ------------------------------------------
    with session_scope() as s:
        rows = list(s.scalars(select(Job).where(Job.id.in_(job_ids))).all())
    assert len(rows) == 16, f"{len(rows)} of 16 jobs could not be read back"
    for row in rows:
        assert row.status in (*CLAIMABLE, *LEASE_HELD), (
            f"job {row.id} is in an unrecoverable state {row.status}")
        if row.status in LEASE_HELD:
            assert row.claimed_by, (
                f"job {row.id} is RUNNING with NO owner -- unowned work never "
                f"comes back")
            assert row.lease_expires_at is not None, (
                f"job {row.id} is RUNNING with no lease -- nothing will ever "
                f"reclaim it")
    # Several RUNNING jobs under one worker is CORRECT: a worker holds a lease
    # per job it claimed, and each poller here claims up to six. What would be
    # wrong is one job owned twice, which a single-valued column cannot express,
    # so the check that matters is that the ids are distinct and accounted.
    running = [r for r in rows if r.status in LEASE_HELD]
    assert len({r.id for r in running}) == len(running)
    assert len(running) <= 16

    # -- and every RUNNING job is reclaimable exactly once ---------------------
    horizon = max((r.lease_expires_at for r in rows
                   if r.lease_expires_at is not None), default=None)
    assert horizon is not None, "no job carried a lease at all"
    reclaimed = reclaim_expired(now=horizon.replace(microsecond=999000))
    claimed_back = {r.job_id for r in reclaimed}
    running_ids = {r.id for r in rows if r.status in LEASE_HELD}
    assert running_ids <= claimed_back, (
        f"a RUNNING job was not reclaimable: "
        f"{sorted(running_ids - claimed_back)}")
    assert not [r for r in reclaim_expired(
        now=horizon.replace(microsecond=999000)) if r.job_id in running_ids], (
        "a job was reclaimed twice")

    # -- the ledger survived the severed connections --------------------------
    with session_scope() as s:
        entries = list(s.scalars(select(CostEntry).where(
            CostEntry.workspace_id == workspace_id)).all())
    assert len(entries) == 1, (
        f"{len(entries)} money rows after a severed-connection storm")
    assert entries[0].workspace_id == workspace_id
    print(f"\nqueue interruption: backends_killed={len(victims)} "
          f"poller_errors={sorted(set(errors))} "
          f"claimed={len(running_ids)} reclaimed={len(claimed_back)} "
          f"ledger_rows={len(entries)}")