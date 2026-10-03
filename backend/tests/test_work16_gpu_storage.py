"""Work 16 §4/§5: GPU admission control and canonical storage objects.

What makes a test in this file worth having
-------------------------------------------
Each test names a specific way the system can be wrong, and the naming is the
assertion. The failure modes being ruled out:

* **oversubscription** -- two jobs admitted onto a card that cannot hold both,
  which is not a queue problem but an OOM after the money is spent (a/b);
* **admission after execution** -- the body runs before the slot exists (c);
* **leaked VRAM** -- a failed, cancelled or timed-out job keeps its slot (d/e/f);
* **leaked VRAM from a CRASH** -- a dead worker's slot is never reclaimed (g/h);
* **stealing from a live job** -- recovery reclaims a slot whose job lease is
  still valid (i);
* **unbounded wait** -- admission blocks forever instead of refusing (j);
* **silent CPU downgrade** -- work that requires a GPU runs on CPU because the
  GPU was busy (k/l);
* **cross-workspace reads** -- workspace A reads workspace B's object (m);
* **temp state becoming canonical** -- a scratch file is served as an object,
  or a half-written upload is addressable (n/o/p);
* **unverifiable objects** -- a finalized row whose bytes are corrupt or
  truncated is indistinguishable from a good one (q/r).

Real rows and a real database throughout. A mocked scheduler would test the
mock. The device is a SIMULATED one (``backend='simulated'``) so the suite does
not need a card; the admission ARITHMETIC is identical either way, and the one
test that wants real hardware is `skipif`-gated on ``nvidia-smi``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.models import GpuDevice, GpuReservation, Job, StorageObject
from app.models.base import JobStatus, utcnow
from app.services import gpu_scheduler as gpu
from app.services import storage_objects as objects
from app.services import streaming_io

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace_a(workspace_with_user):
    return workspace_with_user["workspace"]


@pytest.fixture()
def workspace_b(db_session):
    """A second, unrelated workspace. The isolation tests are meaningless
    without one -- "A cannot read B" and "there is nothing to read" look the
    same when B was never created."""
    from app.models import Workspace

    ws = Workspace(name="W16 Storage B", slug=f"w16b-{os.urandom(4).hex()}")
    db_session.add(ws)
    db_session.commit()
    yield ws.id
    db_session.rollback()


@pytest.fixture(autouse=True)
def _clean_gpu_and_objects():
    """Leave no reservation or object behind.

    Scoped to THIS module's own devices and to the ``W16 Storage B`` workspace
    it creates. A leaked reservation is a live grenade for the next test in the
    file -- the card it is holding has less VRAM than the next test assumes --
    and a blanket delete would be a shared-resource hazard, because the suite
    shares one database and other modules have rows here.
    """
    yield
    from sqlalchemy import delete, select

    from app.db import session_scope
    from app.models import Workspace

    with session_scope() as s:
        keys = list(s.scalars(select(GpuDevice.device_key)).all())
        mine = [str(k) for k in keys if str(k).startswith("sim:")]
        if mine or "cpu" in {str(k) for k in keys}:
            s.execute(delete(GpuReservation).where(
                GpuReservation.device_id.in_(
                    select(GpuDevice.id).where(
                        GpuDevice.device_key.in_(mine + ["cpu"])))))
            s.execute(delete(GpuDevice).where(GpuDevice.device_key.in_(mine)))
        ws_ids = list(s.scalars(
            select(Workspace.id).where(Workspace.name == "W16 Storage B")).all())
        if ws_ids:
            s.execute(delete(StorageObject).where(
                StorageObject.workspace_id.in_(ws_ids)))


@pytest.fixture()
def card():
    """A simulated 8 GB card. ``reserved_mb`` is the whole admission gate, so a
    simulated device exercises the real arithmetic without needing a GPU."""
    gpu.register_device("sim:card0", name="Simulated RTX", backend="simulated",
                        total_mb=8192)
    return "sim:card0"


@pytest.fixture()
def big_card():
    gpu.register_device("sim:card1", name="Simulated A100", backend="simulated",
                        total_mb=24576)
    return "sim:card1"


# ---------------------------------------------------------------------------
# A. Device / VRAM model
# ---------------------------------------------------------------------------


def test_device_capacity_is_a_row_not_a_constant(card, workspace_a):
    """Capacity is per-machine. A laptop card and a datacenter card differ by
    20x, and a config constant cannot be right for both."""
    devices = gpu.available_devices()
    entry = next(d for d in devices if d["device_key"] == "sim:card0")
    assert entry["total_mb"] == 8192
    assert entry["free_mb"] == 8192, "a fresh card has all of its memory free"
    assert entry["backend"] == "simulated"


def test_declared_requirements_cover_every_gpu_lane():
    """The lanes Work 16 §4 names must all declare a requirement, and a lane
    that can run on CPU must SAY so -- that declaration is what makes fallback
    an opt-in rather than a guess."""
    for kind in ("musetalk", "lipsync", "segmentation", "ai_video", "avatar",
                 "media_intel"):
        spec = gpu.describe(kind)
        assert spec["known"], f"{kind} has no declared VRAM requirement"
        assert spec["vram_mb"] > 0
    # A lane that CANNOT run without its device must not be CPU-capable, or
    # fallback would silently produce a worse artifact than the operator asked.
    assert gpu.describe("musetalk")["cpu_capable"] is False
    assert gpu.describe("ai_video")["cpu_capable"] is False
    assert gpu.describe("avatar")["cpu_capable"] is True


def test_unknown_kind_gets_a_conservative_default():
    """An undeclared lane must not be assumed free. A default of 0 would let
    an undeclared multi-GB job onto a card with no room."""
    assert gpu.requirement_for("something-new") > 0
    assert gpu.describe("something-new")["known"] is False


# ---------------------------------------------------------------------------
# B. No blind oversubscription  (the mutation target)
# ---------------------------------------------------------------------------


def test_admission_refuses_a_job_that_does_not_fit(card, workspace_a):
    """A request bigger than the whole device is refused, not squeezed in.

    This is the single most important test in the file: without it, a 12 GB
    AI-video job is admitted onto an 8 GB card and dies of OOM after the
    provider has already been paid.
    """
    req = gpu.GpuRequest(kind="ai_video", workspace_id=workspace_a)  # 12000 MB
    with pytest.raises(gpu.GpuAdmissionTimeout) as exc:
        gpu.admit(req, timeout=0.2, poll_seconds=0.01)
    assert "12000 MB" in str(exc.value)
    devices = gpu.available_devices()
    entry = next(d for d in devices if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0, "a refused request reserves nothing"


def test_admission_refuses_the_last_of_the_memory(card, workspace_a):
    """Two 5 GB jobs do not fit on an 8 GB card, and the second one is told so
    rather than admitted into a memory state the driver will kill."""
    first = gpu.GpuRequest(kind="default", vram_mb=5000, workspace_id=workspace_a,
                           cpu_capable=False)
    held = gpu.admit(first, timeout=1.0)
    try:
        assert held.vram_mb == 5000
        second = gpu.GpuRequest(kind="default", vram_mb=5000,
                                workspace_id=workspace_a, cpu_capable=False)
        with pytest.raises(gpu.GpuAdmissionTimeout):
            gpu.admit(second, timeout=0.2, poll_seconds=0.01)
    finally:
        gpu.release(held.reservation_id)
    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0


def test_concurrent_admission_never_exceeds_capacity(card, workspace_a):
    """Eight threads race for a card with room for exactly three 2 GB jobs.

    Threads, not coroutines: the guarantee under test is that the DATABASE
    resolves the race, and an asyncio test would only prove the event loop
    does. The winners HOLD their slot for a second while the losers poll, so
    this measures the instantaneous capacity rather than the admission rate --
    otherwise a test that merely lets threads through one at a time would pass
    against a scheduler that over-admits.

    A sampler thread reads ``reserved_mb`` throughout, so the assertion is on
    the counter the card is actually being charged, not on the scheduler's own
    bookkeeping.
    """
    limit = 3
    need = 2600  # 8192 // 2600 == 3: the fourth job would need 10400 MB
    admitted: list[str] = []
    refused: list[str] = []
    barrier = threading.Barrier(8)
    samples: list[int] = []
    lock = threading.Lock()
    stop = threading.Event()

    def _sampler() -> None:
        while not stop.is_set():
            with lock:
                samples.append(next(d for d in gpu.available_devices()
                                    if d["device_key"] == "sim:card0")["reserved_mb"])
            time.sleep(0.01)

    def _worker() -> None:
        req = gpu.GpuRequest(kind="default", vram_mb=need,
                             workspace_id=workspace_a, cpu_capable=False)
        barrier.wait()
        try:
            slot = gpu.admit(req, timeout=0.4, poll_seconds=0.01)
        except gpu.GpuAdmissionTimeout:
            refused.append("timeout")
            return
        with lock:
            admitted.append(slot.reservation_id)
        try:
            time.sleep(1.0)  # hold, so the losers poll against a full card
        finally:
            gpu.release(slot.reservation_id)

    sampler = threading.Thread(target=_sampler, daemon=True)
    sampler.start()
    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    stop.set()
    sampler.join(timeout=5)

    assert len(admitted) == limit, (
        f"expected exactly {limit} concurrent admits, got {len(admitted)}")
    assert len(refused) == 8 - limit
    assert samples, "the sampler observed the device at least once"
    assert max(samples) == limit * need, (
        f"the card was charged up to {max(samples)} MB for {limit}x{need} MB "
        f"of work; the capacity gate did not hold")
    assert min(samples) >= 0, "the counter never went negative"
    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0, "every slot was released"


def test_a_job_larger_than_free_memory_goes_to_a_bigger_card(card, big_card,
                                                             workspace_a):
    """Two devices, one with room. Admission must find the one that fits rather
    than refusing because the first candidate was full."""
    with gpu.gpu_slot(gpu.GpuRequest(kind="default", vram_mb=7000,
                                     workspace_id=workspace_a,
                                     cpu_capable=False), timeout=1.0) as held:
        assert held.device_key == "sim:card0"
    with gpu.gpu_slot(gpu.GpuRequest(kind="ai_video", workspace_id=workspace_a),
                      timeout=1.0) as held:
        assert held.device_key == "sim:card1", (
            "a 12 GB request belongs on the 24 GB card")
        assert held.vram_mb == 12000


# ---------------------------------------------------------------------------
# C. Admission happens BEFORE execution
# ---------------------------------------------------------------------------


def test_the_body_does_not_run_until_a_slot_is_granted(card, workspace_a):
    """Structural ordering: the body is inside the ``with``, so 'admitted then
    executed' is not a convention each caller has to remember."""
    observed: list[tuple[int, int]] = []
    with gpu.gpu_slot(gpu.GpuRequest(kind="default", vram_mb=1000,
                                     workspace_id=workspace_a),
                      timeout=1.0) as held:
        entry = next(d for d in gpu.available_devices()
                     if d["device_key"] == "sim:card0")
        observed.append((entry["reserved_mb"], held.vram_mb))
    assert observed == [(1000, 1000)], (
        "VRAM must already be reserved when the body first executes")


def test_refused_work_never_executes(card, workspace_a):
    """A job that cannot be admitted must not run and then fail. The body is
    the only thing that would touch the device, and it never runs."""
    ran = []
    # noqa below: the nested form IS the assertion -- the slot must be acquired
    # before the body runs, which a flat `with` cannot express.
    with pytest.raises(gpu.GpuAdmissionTimeout):  # noqa: SIM117
        with gpu.gpu_slot(gpu.GpuRequest(kind="ai_video", workspace_id=workspace_a),
                          timeout=0.2, poll_seconds=0.01):
            ran.append("executed")  # pragma: no cover - must not be reached
    assert ran == [], "a refused request must not execute its body"


async def test_async_slot_admits_before_and_releases_after(card, workspace_a):
    """The async twin has the same ordering and the same guaranteed release."""
    seen = []
    async with gpu.gpu_slot_async(
            gpu.GpuRequest(kind="default", vram_mb=1500, workspace_id=workspace_a),
            timeout=1.0):
        entry = next(d for d in gpu.available_devices()
                     if d["device_key"] == "sim:card0")
        seen.append(entry["reserved_mb"])
    assert seen == [1500]
    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0


# ---------------------------------------------------------------------------
# D-F. Release on every exit
# ---------------------------------------------------------------------------


def test_release_on_success(card, workspace_a):
    with gpu.gpu_slot(gpu.GpuRequest(kind="avatar", workspace_id=workspace_a),
                      timeout=1.0):
        pass
    assert gpu.snapshot()["held_reservations"] == 0
    assert next(d for d in gpu.available_devices()
                if d["device_key"] == "sim:card0")["reserved_mb"] == 0


def test_release_on_failure(card, workspace_a):
    # The nested form IS the assertion: the slot must be held when the body
    # raises, so the two `with` statements cannot be collapsed.
    with pytest.raises(RuntimeError, match="provider blew up"):  # noqa: SIM117
        with gpu.gpu_slot(gpu.GpuRequest(kind="avatar", workspace_id=workspace_a),
                          timeout=1.0):
            raise RuntimeError("provider blew up")
    assert gpu.snapshot()["held_reservations"] == 0


def test_release_on_cancellation(card, workspace_a):
    """A cancelled job must give its VRAM back immediately, not at the sweep.

    This matters more than it looks: a cancelled GPU job is often followed by a
    retry on the same card, and a slot held until the next sweep is a slot the
    retry cannot get.
    """
    holder: list[str] = []

    async def _cancel_midway() -> None:
        task = asyncio.create_task(_hold(holder))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def _hold(store: list[str]) -> None:
        async with gpu.gpu_slot_async(
                gpu.GpuRequest(kind="default", vram_mb=2000,
                               workspace_id=workspace_a),
                timeout=5.0) as held:
            store.append(held.reservation_id)
            await asyncio.sleep(30)

    asyncio.run(_cancel_midway())
    assert gpu.snapshot()["held_reservations"] == 0, (
        "a cancelled job released its VRAM")
    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0


def test_release_is_idempotent_and_cannot_credit_twice(card, workspace_a):
    """A double release would hand the same megabytes to two jobs -- the one
    bug that makes a counter-based scheduler hand out capacity it does not have.
    """
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=3000,
                                    workspace_id=workspace_a), timeout=1.0)
    assert gpu.release(slot.reservation_id) is True
    assert gpu.release(slot.reservation_id) is False, (
        "the second release must be a no-op, not a second credit")
    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0, "the device is whole again, not doubled"


# ---------------------------------------------------------------------------
# G-I. Crash recovery: stale reservations and the lease model  (mutation target)
# ---------------------------------------------------------------------------


def _running_job(db, workspace_id: str, *, lease_valid_for: float | None,
                 claimed_by: str = "w-crash") -> Job:
    """A job row that reads as in-flight, with its lease set as asked.

    ``lease_valid_for=None`` writes NULL, which is exactly what a crashed
    worker's row looks like once its lease has lapsed: ``job_leases.
    lease_is_valid`` returns False, and the GPU slot must follow.
    """
    job = Job(type="W16_RENDER", workspace_id=workspace_id,
              status=JobStatus.RUNNING.value, payload={},
              claimed_by=claimed_by, started_at=utcnow(), claimed_at=utcnow(),
              heartbeat_at=utcnow(),
              lease_expires_at=(None if lease_valid_for is None
                                else utcnow() + timedelta(seconds=lease_valid_for)))
    db.add(job)
    db.commit()
    return job


def _set_slot_deadline(reservation_id: str, *, seconds_ago: float) -> None:
    """Move a reservation's own lease into the past.

    This is how a crash is expressed in a test: a holder writes its slot
    exactly as a live one would, then stops renewing. No process is killed,
    because the absence of a renewal IS the evidence a crash leaves.
    """
    from app.db import session_scope

    with session_scope() as s:
        row = s.get(GpuReservation, reservation_id)
        row.lease_expires_at = utcnow() - timedelta(seconds=seconds_ago)
        s.flush()


def test_a_crashed_workers_slot_is_reclaimed(db_session, card, workspace_a):
    """The crash path. A worker died holding a slot: nothing released it, no
    ``finally`` ran, and the only evidence is an expired lease.

    Simulated honestly -- a reservation is written exactly as a live holder
    would write it, then the clock moves past the lease. No process is killed;
    the death is expressed by the lease, which is precisely the evidence a
    crashed process leaves behind.
    """
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=4000,
                                    workspace_id=workspace_a), timeout=1.0)
    _set_slot_deadline(slot.reservation_id, seconds_ago=1)

    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 4000, "before the sweep, the slot is held"

    assert gpu.sweep() == 1, "the abandoned slot is reclaimed"
    entry = next(d for d in gpu.available_devices()
                 if d["device_key"] == "sim:card0")
    assert entry["reserved_mb"] == 0, "the crashed worker's VRAM is back"
    assert gpu.snapshot()["held_reservations"] == 0


def test_a_live_slot_is_never_reclaimed(db_session, card, workspace_a):
    """The other half of the guarantee: recovery must not eat a running job.

    A recovery sweep that reclaims live work is worse than no sweep -- it
    double-books the GPU and produces two half-rendered artifacts.
    """
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=4000,
                                    workspace_id=workspace_a), timeout=1.0)
    assert gpu.sweep() == 0, "a slot with a live lease survives the sweep"
    assert gpu.snapshot()["held_reservations"] == 1
    gpu.release(slot.reservation_id)


def test_a_slot_on_a_dead_job_is_reclaimed_even_with_a_live_slot_lease(
        db_session, card, workspace_a):
    """The two models must agree.

    The slot's own lease says "alive"; the JOB's lease says the owner died.
    ``job_leases`` is the authority on liveness, so the slot is reclaimed on
    the spot rather than waiting out its own clock. If the two recovery sweeps
    could disagree about the same job, one of them would double-book the GPU.
    """
    job = _running_job(db_session, workspace_a, lease_valid_for=None)
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=4000,
                                    workspace_id=workspace_a, job_id=job.id),
                     timeout=1.0)
    assert gpu.sweep() == 1, (
        "a slot whose job lease is gone is reclaimed immediately")
    assert gpu.snapshot()["held_reservations"] == 0
    assert not gpu.renew(slot.reservation_id), (
        "a reclaimed slot cannot be resurrected by a late heartbeat")


def test_a_slot_on_a_live_job_survives_its_own_deadline(db_session, card,
                                                        workspace_a):
    """The inverse, and the case a naive timeout sweep gets wrong.

    The slot's own deadline has passed (the holder missed a heartbeat) but the
    JOB's lease is still valid: the worker is alive, mid-render, on a longer
    lease. Reclaiming here kills a live paid render.
    """
    job = _running_job(db_session, workspace_a, lease_valid_for=900)
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=4000,
                                    workspace_id=workspace_a, job_id=job.id),
                     timeout=1.0)
    _set_slot_deadline(slot.reservation_id, seconds_ago=1)

    assert gpu.sweep() == 0, (
        "a live job's slot is not reclaimable on the slot's clock alone")
    assert gpu.snapshot()["held_reservations"] == 1
    gpu.release(slot.reservation_id)


def test_renewing_a_slot_keeps_it_alive(card, workspace_a):
    """The heartbeat path: a holder that proves it is alive keeps its memory."""
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=1000,
                                    workspace_id=workspace_a), timeout=1.0)
    _set_slot_deadline(slot.reservation_id, seconds_ago=5)
    assert gpu.renew(slot.reservation_id, ttl=300) is True
    assert gpu.sweep() == 0
    gpu.release(slot.reservation_id)


def test_renewing_a_released_slot_is_refused(card, workspace_a):
    """After release the slot is history; a late heartbeat must not resurrect
    it, or a slow cleanup path could put a phantom reservation back on the card."""
    slot = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=1000,
                                    workspace_id=workspace_a), timeout=1.0)
    gpu.release(slot.reservation_id)
    assert gpu.renew(slot.reservation_id) is False


# ---------------------------------------------------------------------------
# J. Timeout is bounded
# ---------------------------------------------------------------------------


def test_admission_times_out_instead_of_deadlocking(card, workspace_a):
    """A wait with no deadline is a hang. The timeout must raise with a message
    an operator can act on, not block a worker thread forever."""
    held = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=8192,
                                    workspace_id=workspace_a,
                                    cpu_capable=False), timeout=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(gpu.GpuAdmissionTimeout) as exc:
            gpu.admit(gpu.GpuRequest(kind="default", vram_mb=8192,
                                     workspace_id=workspace_a,
                                     cpu_capable=False),
                      timeout=0.3, poll_seconds=0.01)
    finally:
        gpu.release(held.reservation_id)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the wait was bounded, took {elapsed:.2f}s"
    assert "sim:card0" in str(exc.value), (
        "the error names the device so an operator can act")


# ---------------------------------------------------------------------------
# K-L. CPU fallback is opt-in TWICE  (never a silent downgrade)
# ---------------------------------------------------------------------------


def test_cpu_work_is_recorded_as_cpu_and_consumes_no_vram(card, workspace_a,
                                                          monkeypatch):
    """A lane that never needed a GPU should still LEAVE A RECORD. The row says
    the CPU device and its metered footprint, which is what answers 'why was
    this on CPU?'.

    The card is held full first, so the request cannot simply run on it -- the
    record is only meaningful if it reflects a real decision.
    """
    monkeypatch.setattr(gpu, "_fallback_enabled", lambda: True)
    busy = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=8192,
                                    workspace_id=workspace_a,
                                    cpu_capable=False), timeout=1.0)
    try:
        slot = gpu.admit(gpu.GpuRequest(kind="avatar", vram_mb=1000,
                                        workspace_id=workspace_a,
                                        cpu_capable=True), timeout=1.0)
        assert slot.backend == "cpu"
        assert slot.cpu_fallback is True
        cpu_entry = next(d for d in gpu.available_devices()
                         if d["device_key"] == "cpu")
        assert cpu_entry["reserved_mb"] == 1000, (
            "even CPU work is metered, so the CPU lane is not a free-for-all")
        gpu.release(slot.reservation_id)
    finally:
        gpu.release(busy.reservation_id)
    entry = next(d for d in gpu.available_devices() if d["device_key"] == "cpu")
    assert entry["reserved_mb"] == 0, "the CPU lane is released too"


def test_the_cpu_lane_can_itself_run_out(card, workspace_a, monkeypatch):
    """The CPU lane is finite, so fallback can fail too.

    Without this, "the CPU device" is a hole every GPU-only request falls
    through, and the whole module is a no-op that never says no.
    """
    monkeypatch.setattr(gpu, "_fallback_enabled", lambda: True)
    busy = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=8192,
                                    workspace_id=workspace_a,
                                    cpu_capable=False), timeout=1.0)
    try:
        first = gpu.admit(gpu.GpuRequest(kind="avatar", vram_mb=2048,
                                         workspace_id=workspace_a,
                                         cpu_capable=True), timeout=1.0)
        assert first.backend == "cpu", (
            "with the card full, the CPU lane takes the work")
        with pytest.raises(gpu.GpuAdmissionTimeout):
            gpu.admit(gpu.GpuRequest(kind="avatar", vram_mb=2048,
                                     workspace_id=workspace_a,
                                     cpu_capable=True), timeout=0.2,
                      poll_seconds=0.01)
        gpu.release(first.reservation_id)
    finally:
        gpu.release(busy.reservation_id)


def test_gpu_only_work_is_refused_when_no_device_fits(card, workspace_a,
                                                      monkeypatch):
    """A job that needs a GPU gets no device and NO CPU substitute.

    Running MuseTalk on CPU because the card was busy would produce a
    low-quality artifact that looks like success. Failing is the honest answer.
    """
    monkeypatch.setattr(gpu, "_fallback_enabled", lambda: True)
    with pytest.raises(gpu.GpuAdmissionTimeout):
        gpu.admit(gpu.GpuRequest(kind="musetalk", vram_mb=20000,
                                 workspace_id=workspace_a,
                                 cpu_capable=False, allow_cpu_fallback=True),
                  timeout=0.2, poll_seconds=0.01)


def test_fallback_is_refused_when_the_operator_did_not_enable_it(
        card, workspace_a, monkeypatch):
    """Opt-in TWICE: the caller may declare the work CPU-capable, but the
    operator's switch is the second half and defaults to off."""
    monkeypatch.setattr(gpu, "_fallback_enabled", lambda: False)
    busy = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=8192,
                                    workspace_id=workspace_a,
                                    cpu_capable=False), timeout=1.0)
    try:
        with pytest.raises(gpu.GpuAdmissionTimeout):
            gpu.admit(gpu.GpuRequest(kind="avatar", vram_mb=1000,
                                     workspace_id=workspace_a,
                                     cpu_capable=True,
                                     allow_cpu_fallback=True),
                      timeout=0.2, poll_seconds=0.01)
    finally:
        gpu.release(busy.reservation_id)


def test_fallback_runs_when_both_halves_opt_in(card, workspace_a, monkeypatch):
    """The positive case, so the two refusal tests above are not vacuous."""
    monkeypatch.setattr(gpu, "_fallback_enabled", lambda: True)
    busy = gpu.admit(gpu.GpuRequest(kind="default", vram_mb=8192,
                                    workspace_id=workspace_a,
                                    cpu_capable=False), timeout=1.0)
    slot = None
    try:
        slot = gpu.admit(gpu.GpuRequest(kind="avatar", vram_mb=1000,
                                        workspace_id=workspace_a,
                                        cpu_capable=True,
                                        allow_cpu_fallback=True),
                         timeout=1.0)
        assert busy.backend == "simulated"
        assert slot.backend == "cpu", (
            "the card is full, so the declared-CPU-capable work runs on CPU")
        assert slot.cpu_fallback is True
    finally:
        gpu.release(busy.reservation_id)
        if slot is not None:
            gpu.release(slot.reservation_id)


def test_the_cpu_device_has_finite_capacity(card, workspace_a, monkeypatch):
    """CPU is a device with a real budget, not 'unlimited'.

    A CPU lane that admits everything would make 'no device fits' impossible to
    reach, which would quietly turn this whole module into a no-op.
    """
    monkeypatch.setattr(gpu, "_fallback_enabled", lambda: True)
    gpu.ensure_cpu_device()
    entry = next(d for d in gpu.available_devices() if d["device_key"] == "cpu")
    assert entry["total_mb"] > 0, "the CPU lane has a stated, finite capacity"
    assert entry["free_mb"] == entry["total_mb"]
    assert entry["total_mb"] <= gpu.CPU_DEVICE["total_mb"], (
        "the CPU lane's capacity is the configured one, never silently raised")


def test_snapshot_reports_capacity_for_an_operator():
    """The question an operator actually asks is 'is the GPU the bottleneck?'."""
    snap = gpu.snapshot()
    assert {"devices", "held_reservations", "total_vram_mb"} <= set(snap)
    assert snap["cpu_fallback_enabled"] is False, "default is off"


# ---------------------------------------------------------------------------
# Real hardware (declarative skip; not required for the suite)
# ---------------------------------------------------------------------------

def _has_nvidia() -> bool:
    from shutil import which

    return which("nvidia-smi") is not None


_HAS_NVIDIA = _has_nvidia()


@pytest.mark.skipif(not _HAS_NVIDIA, reason="no NVIDIA driver on this host")
def test_real_device_probe_reports_actual_vram():
    """Reads ``nvidia-smi``. Skipped declaratively when there is no card, so
    the suite never depends on hardware -- but on a machine that HAS one, the
    device table is populated from the real card and not from a guess."""
    from app.services.gpu_scheduler import probe_devices

    found = probe_devices()
    assert found, "a host with nvidia-smi must report at least one device"
    assert all(d["total_mb"] > 0 for d in found), (
        "reported VRAM must be a real number, not a placeholder")


@pytest.mark.skipif(not _HAS_NVIDIA, reason="no NVIDIA driver on this host")
def test_real_device_capacity_is_not_overcommitted():
    """On a real card the invariant that matters is the one the driver cares
    about: we never promise more VRAM than the card has."""
    gpu.sync_devices()
    for device in gpu.probe_devices():
        entry = next(d for d in gpu.available_devices()
                     if d["device_key"] == device["device_key"])
        assert entry["reserved_mb"] <= entry["total_mb"]
        assert entry["total_mb"] == device["total_mb"], (
            "the registered capacity is the driver's number, not a guess")


# ---------------------------------------------------------------------------
# M. Storage: workspace isolation  (the mutation target)
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    src = tmp_path / name
    src.write_bytes(payload)
    return src


def test_workspace_a_cannot_read_workspace_bs_object(tmp_path, workspace_a,
                                                     workspace_b):
    """The isolation invariant, from BOTH directions and at BOTH layers.

    Checked against the key prefix AND against the resolved filesystem path,
    because either check alone is bypassable: a forged prefix passes a prefix
    check, and a traversal passes a path check.
    """
    src = _write(tmp_path, "secret.mp4", b"workspace B private bytes")
    stored = objects.write_object(workspace_b, "source", src,
                                  logical_name="secret.mp4",
                                  extension=".mp4", content_type="video/mp4")
    assert stored.workspace_id == workspace_b

    with pytest.raises(objects.CanonicalRuleViolation):
        objects.resolve(workspace_a, stored.object_key)
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.get(workspace_a, stored.object_key)
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.resolve(workspace_a, f"{workspace_b}/{stored.object_key}")


def test_a_traversing_key_cannot_escape_the_workspace(tmp_path, workspace_a):
    """``../`` in a key is refused by the SHARED boundary, not a private one.

    This reuses ``storage.validate_storage_key`` deliberately: if this module
    grew its own weaker check, the storage boundary would stop being one
    boundary.
    """
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.resolve(workspace_a, f"{workspace_a}/../../etc/passwd")
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.resolve(workspace_a, "C:/Windows/system32/config/SAM")


def test_each_workspace_gets_its_own_path_for_the_same_name(tmp_path,
                                                            workspace_a,
                                                            workspace_b):
    """Identical logical names in two workspaces must be different objects."""
    a = objects.write_object(workspace_a, "render",
                             _write(tmp_path, "final.mp4", b"AAA"),
                             logical_name="final.mp4", extension=".mp4")
    b = objects.write_object(workspace_b, "render",
                             _write(tmp_path, "final.mp4", b"BBB"),
                             logical_name="final.mp4", extension=".mp4")
    assert a.object_key != b.object_key
    assert objects.read_bytes(workspace_a, a.object_key) == b"AAA"
    assert objects.read_bytes(workspace_b, b.object_key) == b"BBB"


# ---------------------------------------------------------------------------
# N-P. The canonical-state rule  (the mutation target)
# ---------------------------------------------------------------------------


def test_a_temp_file_can_never_be_finalized_as_canonical(tmp_path, workspace_a):
    """THE INVARIANT. A scratch file may exist; it may never become canonical.

    Enforced by refusing to finalize FROM the staging root, so the scratch
    directory and the media directory cannot be confused at the boundary.
    """
    scratch = objects.StagingRoot(prefix="mut-canon")
    try:
        part = scratch.child("render.part")
        part.write_bytes(b"half a render")
        pending = objects.register_pending(workspace_a, "render",
                                           "final-1.mp4", extension=".mp4",
                                           temp_path=str(part))
        with pytest.raises(objects.CanonicalRuleViolation):
            objects.finalize(pending.id, part)
        row = _row(pending.id)
        assert row.state == StorageObject.PENDING, (
            "a refused finalize leaves the row a promise, not content")
        assert objects.resolve(workspace_a, row.object_key).exists() is False
    finally:
        scratch.close()


def test_a_staging_path_is_never_resolvable(tmp_path, workspace_a):
    """Even asked directly, the storage boundary will not hand back a path
    inside the staging root."""
    scratch = objects.StagingRoot(prefix="mut-resolve")
    try:
        with pytest.raises(objects.CanonicalRuleViolation):
            objects.resolve(workspace_a, str(scratch.child("x.mp4")))
        with pytest.raises(objects.CanonicalRuleViolation):
            objects.assert_not_staging(scratch.path / "anything.bin")
    finally:
        scratch.close()


def test_a_pending_object_is_not_readable(tmp_path, workspace_a):
    """PENDING is a promise. Reading one as content is how a half-uploaded file
    reaches a render and produces a truncated artifact."""
    scratch = objects.StagingRoot(prefix="mut-pending")
    try:
        part = scratch.child("upload.mp4")
        part.write_bytes(b"still uploading")
        pending = objects.register_pending(workspace_a, "source", "upload.mp4",
                                           temp_path=str(part))
        with pytest.raises(objects.ObjectNotFound):
            objects.get(workspace_a, pending.object_key)
        with pytest.raises(objects.ObjectNotFound):
            objects.read_bytes(workspace_a, pending.object_key)
    finally:
        scratch.close()


def test_no_part_file_survives_a_successful_finalize(tmp_path, workspace_a):
    """After finalization there is no ``.part`` left next to the object: the
    rename consumed it. A leftover ``.part`` is how scratch becomes
    indistinguishable from canonical in a directory listing."""
    src = _write(tmp_path, "final.mp4", b"complete render bytes")
    stored = objects.write_object(workspace_a, "render", src,
                                  logical_name="final.mp4", extension=".mp4")
    dest = objects.resolve(workspace_a, stored.object_key)
    assert dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()
    assert dest.read_bytes() == b"complete render bytes"


def test_a_failed_write_leaves_nothing_addressable(tmp_path, workspace_a):
    """If the stream dies mid-transfer, the object is absent -- not partial.

    A reader must never be able to open a half-written file under a name that
    claims to be a finished render.
    """
    def _dying() -> list[bytes]:
        yield b"first chunk"
        raise OSError("the network died")

    pending = objects.register_pending(workspace_a, "render", "big.mp4",
                                       extension=".mp4")
    with pytest.raises(OSError, match="network died"):
        objects.upload_object(workspace_a, "render", _dying(), logical_name="big.mp4",
                              extension=".mp4")
    row = _row(pending.id)
    assert row.state == StorageObject.PENDING
    with pytest.raises(objects.ObjectNotFound):
        objects.get(workspace_a, row.object_key)


def test_finalize_verifies_the_declared_checksum(tmp_path, workspace_a):
    """A checksum declared at registration is enforced at finalization.

    Without this, a truncated copy still produces a file, and the row records a
    checksum nobody ever compared.
    """
    src = _write(tmp_path, "claimed.mp4", b"the real bytes")
    pending = objects.register_pending(workspace_a, "source", "claimed.mp4",
                                       extension=".mp4")
    with pytest.raises(ValueError, match="checksum mismatch"):
        objects.finalize(pending.id, src,
                         checksum="0" * 64)
    assert _row(pending.id).state == StorageObject.PENDING


# ---------------------------------------------------------------------------
# Q-R. Object identity, checksums, size, content type
# ---------------------------------------------------------------------------


def test_object_keys_are_stable_across_calls(workspace_a):
    """Stable means: the same logical object resolves to the same key on every
    call, forever. That is what makes a retried upload idempotent instead of
    ``final-1 (2).mp4``."""
    first = objects.object_key(workspace_a, "render", "final-1.mp4",
                               extension=".mp4")
    second = objects.object_key(workspace_a, "render", "final-1.mp4",
                                extension=".mp4")
    assert first == second
    assert first.startswith(f"{workspace_a}/render/")


def test_different_logical_names_do_not_collide(workspace_a):
    """Two names that sanitise to the same stem must stay distinct objects."""
    a = objects.object_key(workspace_a, "render", "final-1.mp4", extension=".mp4")
    b = objects.object_key(workspace_a, "render", "final-2.mp4", extension=".mp4")
    assert a != b


def test_an_unknown_kind_is_refused(workspace_a):
    """A kind outside the vocabulary is a bug in the caller; guessing would put
    an object's lifetime in question."""
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.object_key(workspace_a, "not-a-kind", "x.mp4")


def test_a_retried_upload_is_idempotent(tmp_path, workspace_a):
    """Same logical name, written twice -> one row, one path, new bytes."""
    first = objects.write_object(workspace_a, "render",
                                 _write(tmp_path, "a.mp4", b"attempt one"),
                                 logical_name="final.mp4", extension=".mp4")
    second = objects.write_object(workspace_a, "render",
                                  _write(tmp_path, "b.mp4", b"attempt two"),
                                  logical_name="final.mp4", extension=".mp4")
    assert first.id == second.id, "the retry reused the same object row"
    assert objects.read_bytes(workspace_a, first.object_key) == b"attempt two"
    assert objects.describe(workspace_a)["objects"] == 1, (
        "no duplicate object was created by the retry")


def test_content_type_size_and_checksum_are_recorded(tmp_path, workspace_a):
    """The three facts a caller needs to decide whether it can use the bytes."""
    src = _write(tmp_path, "clip.mp4", b"x" * 5000)
    stored = objects.write_object(workspace_a, "render", src,
                                  logical_name="clip.mp4", extension=".mp4",
                                  content_type="video/mp4")
    assert stored.state == StorageObject.FINALIZED
    assert stored.content_type == "video/mp4"
    assert stored.size_bytes == 5000
    assert stored.checksum == streaming_io.sha256_file(src)
    assert stored.finalized_at is not None
    assert stored.temp_path == "", "a finalized object keeps no temp path"


def test_corrupt_bytes_are_detected(tmp_path, workspace_a):
    """A FINALIZED row is a claim until somebody re-reads the bytes.

    This is the final-artifact verification step. A row that cannot detect
    corruption is a row nobody should trust.
    """
    src = _write(tmp_path, "good.mp4", b"intact bytes")
    stored = objects.write_object(workspace_a, "render", src,
                                  logical_name="good.mp4", extension=".mp4")
    path = objects.resolve(workspace_a, stored.object_key)
    path.write_bytes(b"CORRUPTED but the same length!!!!")
    with pytest.raises(objects.CanonicalRuleViolation, match="corrupt"):
        objects.checksum(workspace_a, stored.object_key)


def test_truncated_bytes_are_detected(tmp_path, workspace_a):
    """Truncation is the failure a partial upload produces, and it is the one a
    size check catches when the hash somehow still matches."""
    src = _write(tmp_path, "long.mp4", b"y" * 4096)
    stored = objects.write_object(workspace_a, "render", src,
                                  logical_name="long.mp4", extension=".mp4")
    path = objects.resolve(workspace_a, stored.object_key)
    path.write_bytes(b"y" * 100)
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.checksum(workspace_a, stored.object_key)


# ---------------------------------------------------------------------------
# Signed access + cleanup
# ---------------------------------------------------------------------------


def test_signed_url_refuses_a_foreign_object(workspace_b, workspace_a):
    """A token path must not exist for another workspace's object. This is the
    same isolation rule, at the one place where it becomes externally visible."""
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.get(workspace_a, f"{workspace_b}/render/x-final.mp4")
    with pytest.raises(objects.CanonicalRuleViolation):
        objects.signed_url(workspace_a, f"{workspace_b}/render/x-final.mp4")
    # A key that is *inside* this workspace but does not exist must fail
    # closed too -- an unguessable key is not a capability.
    with pytest.raises(objects.ObjectNotFound):
        objects.signed_url(workspace_a, f"{workspace_a}/render/nope.mp4")


def test_signed_url_for_a_local_object_uses_the_existing_token_mint(
        tmp_path, workspace_a):
    """Local access delegates to ``services.public_links``. Two HMAC schemes in
    one codebase is one too many, and the existing one is what publishers
    already trust."""
    from app.core.config import settings

    monkey = settings.public_base_url
    settings.public_base_url = "https://ymoney.example"
    try:
        stored = objects.write_object(workspace_a, "render",
                                     _write(tmp_path, "p.mp4", b"pub"),
                                     logical_name="p.mp4", extension=".mp4")
        url = objects.signed_url(workspace_a, stored.object_key)
        assert url.startswith("https://ymoney.example/api/v1/public/media/")
    finally:
        settings.public_base_url = monkey


def test_cleanup_drops_promises_nobody_kept(tmp_path, workspace_a):
    """A PENDING row older than the TTL is an upload that died.

    Left alone it is a permanent lie: the system reports an object that has no
    bytes and never will.
    """
    scratch = objects.StagingRoot(prefix="mut-sweep")
    try:
        part = scratch.child("abandoned.mp4.part")
        part.write_bytes(b"never finished")
        pending = objects.register_pending(workspace_a, "source",
                                           "abandoned.mp4",
                                           temp_path=str(part))
        assert _row(pending.id).state == StorageObject.PENDING
        result = objects.sweep(workspace_id=workspace_a,
                               pending_ttl_seconds=0.0,
                               now=utcnow() + timedelta(days=1))
        assert result["pending_dropped"] >= 1
        with pytest.raises(objects.ObjectNotFound):
            objects.get(workspace_a, pending.object_key)
    finally:
        scratch.close()


def test_cleanup_leaves_a_healthy_finalized_object_alone(tmp_path, workspace_a):
    """The sweep must not be a deletion policy. Retention owns that decision
    (``services.retention``); two sweeps with delete authority is how the same
    bytes get deleted twice."""
    stored = objects.write_object(workspace_a, "render",
                                  _write(tmp_path, "keep.mp4", b"keep me"),
                                  logical_name="keep.mp4", extension=".mp4")
    result = objects.sweep(workspace_id=workspace_a, pending_ttl_seconds=0.0,
                           now=utcnow() + timedelta(days=1))
    assert result["pending_dropped"] == 0
    assert objects.read_bytes(workspace_a, stored.object_key) == b"keep me"


def test_delete_removes_bytes_and_is_idempotent(tmp_path, workspace_a):
    stored = objects.write_object(workspace_a, "export",
                                  _write(tmp_path, "e.zip", b"export bytes"),
                                  logical_name="e.zip", extension=".zip")
    path = objects.resolve(workspace_a, stored.object_key)
    assert objects.delete(workspace_a, stored.object_key) is True
    assert not path.exists(), "the bytes are gone"
    assert objects.delete(workspace_a, stored.object_key) is False, (
        "deleting twice is a no-op, not a second deletion")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def db_session_scope():
    from app.db import session_scope

    return session_scope()


def _row(object_id: str) -> StorageObject:
    """Re-read a row on a FRESH session.

    ``expire_on_commit=False`` means the object handed back by the service is a
    snapshot from before the write committed; reading it again would assert the
    pre-write value and pass for the wrong reason.
    """
    from app.db import session_scope

    with session_scope() as s:
        row = s.get(StorageObject, object_id)
        if row is None:
            raise objects.ObjectNotFound(object_id)
        s.expunge(row)
        return row


def _load_0036():
    path = (Path(__file__).resolve().parent.parent
            / "app" / "migrations" / "versions" / "0036_gpu_and_storage.py")
    spec = importlib.util.spec_from_file_location("m0036_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------


def test_0036_round_trips_upgrade_and_downgrade(tmp_path):
    """Reversible, not merely applicable.

    Runs on its OWN throwaway database: the suite shares one session-scoped
    SQLite file, and a downgrade on that connection strips the schema out from
    under every test that runs afterwards.
    """
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401  (registers every table)
    from app.db import Base

    engine = create_engine(f"sqlite:///{(tmp_path / 'm36.db').as_posix()}")
    session = sessionmaker(bind=engine)()
    tables = ("gpu_devices", "gpu_reservations", "storage_objects")
    try:
        Base.metadata.create_all(bind=engine)
        module = _load_0036()

        module.upgrade(session)
        session.commit()
        inspector = inspect(engine)
        for table in tables:
            assert table in inspector.get_table_names()
        assert "total_mb" in {c["name"] for c in
                              inspector.get_columns("gpu_devices")}
        assert "checksum" in {c["name"] for c in
                              inspector.get_columns("storage_objects")}

        module.upgrade(session)  # replay is a no-op
        session.commit()

        module.downgrade(session)
        session.commit()
        inspector = inspect(engine)
        for table in tables:
            assert table not in inspector.get_table_names(), (
                f"{table} survived the downgrade")

        module.upgrade(session)
        session.commit()
        inspector = inspect(engine)
        for table in tables:
            assert table in inspector.get_table_names(), (
                f"{table} did not come back on re-upgrade")
    finally:
        session.close()
        engine.dispose()


def test_migrations_apply_twice_without_error():
    """The shared-database replay path, which is what actually runs in CI."""
    from app.db import session_scope
    from app.migrations.runner import run_migrations

    with session_scope() as s:
        assert run_migrations(s) == []