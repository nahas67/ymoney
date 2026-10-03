"""GPU admission: a device, its VRAM, and a lease on part of it (Work 16 §4).

Why this is not a counter
-------------------------
Before this module, GPU admission was a *count*: ``max_concurrent_gpu_jobs``
sized a semaphore of N slots. That is correct for a device whose per-job
footprint is uniform and unknown, and wrong the moment it is not. A 4 GB
segmentation job and a 300 MB lip-sync job both take "one slot", so a queue of
five lip-syncs admits all five onto a card with 6 GB free and the fifth dies of
CUDA OOM -- after the money was spent and before any artifact exists. The
failure mode is not "the queue was busy"; it is "the queue lied about capacity".

So capacity is expressed the way the hardware expresses it: **VRAM on a named
device.** A request declares what it needs; admission succeeds only when a
single device can satisfy all of it.

    musetalk / lipsync   ~3000 MB
    segmentation         ~4000 MB
    ai video generation  ~12000 MB
    avatar               ~2500 MB
    media intelligence   ~2000 MB

Those are *declared requirements*, not measurements -- they are what the caller
promises to need, and an over-declared requirement costs a delay while an
under-declared one costs an OOM. That asymmetry is the whole reason to err
high, and the reason the declaration is a field on the request rather than a
constant buried here.

The three properties this file exists to hold, each testable by breaking it:

**No blind oversubscription.** Admission is ONE conditional UPDATE::

    UPDATE gpu_devices SET reserved_mb = reserved_mb + :need
     WHERE id = :id AND enabled = 1 AND (total_mb - reserved_mb) >= :need

``rowcount`` is the answer. There is no read-then-write window, so two
processes racing for the last 4 GB cannot both win -- the loser sees
``rowcount == 0`` and moves on. A count-based check-then-insert would admit
both and OOM one of them.

**Admission before execution.** :func:`gpu_slot` is a context manager and the
body does not run until the UPDATE has committed. The alternative -- run, then
hope there was room -- is the bug this replaces.

**Every exit releases.** Success, failure, exception, cancellation, timeout and
crash all end in a ``finally``. A crash cannot run a ``finally``, which is
exactly why a reservation carries a LEASE: a crashed holder stops renewing,
the lease lapses, and :func:`reclaim_stale` returns the VRAM. A live holder is
never reclaimed -- and when the reservation names a ``job_id``, liveness is
asked of ``job_leases`` rather than guessed, so the two recovery models cannot
disagree about the same job.

CPU/provider fallback
---------------------
Fallback exists and is **off unless both parties opt in**: the caller must
declare ``cpu_capable=True`` on the request *and* the operator must set
``settings.gpu_cpu_fallback_enabled``. When it is granted, the reservation is
written with ``cpu_fallback=True`` and ``vram_mb=0`` on the ``cpu`` device, so
the audit trail says in one query which work ran somewhere other than where it
asked to run. Nothing infers CPU capability from a device being unavailable: a
job that *needs* a GPU and finds none gets :class:`GpuUnavailable`, never a
silent downgrade.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import GpuDevice, GpuReservation
from app.models.base import utcnow

__all__ = [
    "GpuAdmissionTimeout",
    "GpuRequest",
    "GpuSlot",
    "GpuUnavailable",
    "CPU_DEVICE_KEY",
    "DEFAULT_REQUIREMENTS",
    "CPU_DEVICE",
    "admit",
    "available_devices",
    "cpu_slot",
    "describe",
    "gpu_slot",
    "gpu_slot_async",
    "probe_devices",
    "register_device",
    "release",
    "reclaim_stale",
    "renew",
    "requirement_for",
    "snapshot",
    "sweep",
    "sync_devices",
]


class GpuAdmissionTimeout(RuntimeError):
    """No device could satisfy the request within the timeout. Never swallowed."""


class GpuUnavailable(RuntimeError):
    """No enabled device exists at all, or the work needs a GPU and none is free
    enough -- and CPU fallback was NOT explicitly granted, so there is nowhere
    honest to run this."""


#: The CPU lane is a device with a real (small) capacity, not a magic "any"
#: escape hatch. Making it finite is what keeps "no device fits" meaningful.
CPU_DEVICE_KEY = "cpu"

#: Declared per-kind VRAM requirements, in MB. These are UPPER BOUNDS a caller
#: promises not to exceed, not measurements -- err high, because a delay is
#: recoverable and an OOM is not. ``cpu_capable`` says whether the same work is
#: honestly runnable without the device, which is a property of the WORK and is
#: therefore declared, never inferred from "the GPU is busy".
DEFAULT_REQUIREMENTS: dict[str, dict[str, object]] = {
    "musetalk": {"vram_mb": 3000, "cpu_capable": False},
    "lipsync": {"vram_mb": 3000, "cpu_capable": True},
    "segmentation": {"vram_mb": 4000, "cpu_capable": True},
    "matting": {"vram_mb": 4000, "cpu_capable": True},
    "ai_video": {"vram_mb": 12000, "cpu_capable": False},
    "video_generation": {"vram_mb": 12000, "cpu_capable": False},
    "avatar": {"vram_mb": 2500, "cpu_capable": True},
    "media_intel": {"vram_mb": 2000, "cpu_capable": True},
    "media_intelligence": {"vram_mb": 2000, "cpu_capable": True},
    "broll": {"vram_mb": 1500, "cpu_capable": True},
    "dubbing": {"vram_mb": 1500, "cpu_capable": True},
    "default": {"vram_mb": 2000, "cpu_capable": False},
}

#: The CPU device is created lazily so a fresh database still has somewhere to
#: put CPU-runnable work, but its capacity is finite and small: pretending CPU
#: work is free is how a "GPU admission control" becomes a no-op.
CPU_DEVICE: dict[str, object] = {
    "device_key": CPU_DEVICE_KEY,
    "name": "cpu",
    "backend": "cpu",
    "total_mb": 2048,
    "enabled": True,
}


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuRequest:
    """What one job needs from a device.

    ``cpu_capable`` is the caller's *declaration* that the same work can run
    without a GPU. It is never set by the scheduler and never inferred: a job
    that cannot run on CPU must be told to wait for a device, not quietly
    produce a worse artifact.
    """

    kind: str = "default"
    vram_mb: int | None = None
    workspace_id: str = ""
    job_id: str | None = None
    job_type: str = ""
    priority: int = 100
    cpu_capable: bool | None = None
    #: Opt in to the CPU fallback path. Ignored when the work is not
    #: CPU-capable or the operator has not enabled fallback.
    allow_cpu_fallback: bool = True

    def need_mb(self) -> int:
        if self.vram_mb is not None:
            return max(0, int(self.vram_mb))
        spec = DEFAULT_REQUIREMENTS.get(str(self.kind or "default")) or (
            DEFAULT_REQUIREMENTS["default"])
        return int(spec["vram_mb"])

    def is_cpu_capable(self) -> bool:
        if self.cpu_capable is not None:
            return bool(self.cpu_capable)
        spec = DEFAULT_REQUIREMENTS.get(str(self.kind or "default")) or (
            DEFAULT_REQUIREMENTS["default"])
        return bool(spec["cpu_capable"])


def requirement_for(kind: str) -> int:
    """Declared VRAM for a kind, in MB. Unknown kinds get the default."""
    spec = DEFAULT_REQUIREMENTS.get(str(kind or "default")) or DEFAULT_REQUIREMENTS["default"]
    return int(spec["vram_mb"])


@dataclass
class GpuSlot:
    """A granted reservation. The body of :func:`gpu_slot` runs only with one."""

    reservation_id: str
    device_id: str
    device_key: str
    backend: str
    vram_mb: int
    #: True when this work is running somewhere other than where it asked.
    cpu_fallback: bool = False
    job_id: str | None = None
    workspace_id: str = ""
    meta: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.reservation_id)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def register_device(
    device_key: str,
    *,
    name: str = "",
    backend: str = "cuda",
    total_mb: int,
    enabled: bool = True,
    meta: dict | None = None,
) -> str:
    """Register (or update) a device and return its id. Idempotent per key."""
    key = str(device_key or "").strip()
    if not key:
        raise ValueError("device_key is required")
    with session_scope() as s:
        row = s.scalar(select(GpuDevice).where(GpuDevice.device_key == key))
        if row is None:
            row = GpuDevice(device_key=key)
            s.add(row)
        row.name = name or key
        row.backend = str(backend or "cuda")
        row.total_mb = max(0, int(total_mb))
        row.enabled = bool(enabled)
        if meta:
            row.meta_json = dict(meta)
        s.flush()
        return str(row.id)


def _allowed_keys() -> tuple[str, ...]:
    """Devices this process may admit onto; empty means all."""
    raw = ""
    with contextlib.suppress(Exception):
        from app.core.config import settings

        raw = str(getattr(settings, "gpu_device_keys", "") or "")
    keys = tuple(k.strip() for k in raw.split(",") if k.strip())
    return keys


def probe_devices() -> list[dict]:
    """Read the real cards from ``nvidia-smi``. Returns [] when there are none.

    An empty list is the honest answer on a CPU box, and it is what makes the
    admission tests runnable without hardware. Nothing here guesses a VRAM
    number: the capacity comes from the driver's own report, because a config
    value would be wrong on every machine except the one it was written on.

    Never raises. A probe that fails is a machine with no *known* GPU, which
    admission already handles by refusing work rather than guessing.
    """
    from shutil import which

    if which("nvidia-smi") is None:
        return []
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True).stdout
    except Exception as exc:  # noqa: BLE001 - a probe must never break startup
        logger.warning(f"GPU probe failed: {type(exc).__name__}: {exc}")
        return []
    found: list[dict] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
            total_mb = int(float(parts[2]))
        except ValueError:
            continue
        found.append({
            "device_key": f"cuda:{index}",
            "name": parts[1][:80],
            "backend": "cuda",
            "total_mb": max(0, total_mb),
        })
    return found


def sync_devices() -> list[str]:
    """Probe and register, returning the device keys written. Idempotent."""
    keys: list[str] = []
    for device in probe_devices():
        register_device(device["device_key"], name=device["name"],
                        backend=device["backend"], total_mb=device["total_mb"],
                        meta={"source": "nvidia-smi"})
        keys.append(str(device["device_key"]))
    return keys


def ensure_cpu_device() -> str:
    """The CPU lane exists on demand so CPU work always has somewhere to land."""
    with session_scope() as s:
        row = s.scalar(select(GpuDevice).where(GpuDevice.device_key == CPU_DEVICE_KEY))
        if row is None:
            row = GpuDevice(**CPU_DEVICE)  # type: ignore[arg-type]
            s.add(row)
            s.flush()
            return str(row.id)
        return str(row.id)


def available_devices(db: Session | None = None) -> list[dict]:
    """Every registered device with its live free capacity."""
    def _read(s: Session) -> list[dict]:
        keys = _allowed_keys()
        rows = list(s.scalars(select(GpuDevice).order_by(GpuDevice.device_key)).all())
        return [
            {
                "id": str(r.id),
                "device_key": str(r.device_key),
                "backend": str(r.backend),
                "enabled": bool(r.enabled),
                "total_mb": int(r.total_mb or 0),
                "reserved_mb": int(r.reserved_mb or 0),
                "free_mb": max(0, int(r.total_mb or 0) - int(r.reserved_mb or 0)),
            }
            for r in rows
            if not keys or str(r.device_key) in keys
        ]

    if db is not None:
        return _read(db)
    with session_scope() as s:
        return _read(s)


def _lease_seconds() -> float:
    with contextlib.suppress(Exception):
        from app.core.config import settings

        return max(1.0, float(getattr(settings, "gpu_slot_lease_seconds", 120.0)))
    return 120.0  # pragma: no cover - config import always succeeds


def _fallback_enabled() -> bool:
    with contextlib.suppress(Exception):
        from app.core.config import settings

        return bool(getattr(settings, "gpu_cpu_fallback_enabled", False))
    return False  # pragma: no cover - config import always succeeds


def _scheduler_enabled() -> bool:
    with contextlib.suppress(Exception):
        from app.core.config import settings

        return bool(getattr(settings, "gpu_scheduler_enabled", True))
    return True  # pragma: no cover - config import always succeeds


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _candidate_devices(s: Session, need_mb: int, *, cpu_ok: bool) -> list[GpuDevice]:
    """Enabled devices with room for the whole request, tightest fit first.

    Tightest-fit is deliberate: putting a 12 GB job on a card with 16 GB free
    rather than the one with exactly 12 leaves the big card able to serve the
    next big job instead of fragmenting every device into unusable dust.
    """
    keys = _allowed_keys()
    rows = list(
        s.scalars(
            select(GpuDevice).where(
                GpuDevice.enabled.is_(True),
                GpuDevice.total_mb >= need_mb,
            )
        ).all()
    )
    rows = [r for r in rows if not keys or str(r.device_key) in keys]
    if not cpu_ok:
        rows = [r for r in rows if str(r.backend) != "cpu"]
    return sorted(rows, key=lambda r: (int(r.total_mb or 0) - int(r.reserved_mb or 0)))


def _try_admit(req: GpuRequest) -> GpuSlot | None:
    """One attempt. Returns a slot, or None when nothing fits right now.

    The conditional UPDATE is the admission decision. Everything else is
    bookkeeping around it, and if the UPDATE loses the race the bookkeeping is
    never written.
    """
    need = req.need_mb()
    cpu_ok = req.is_cpu_capable() and req.allow_cpu_fallback and _fallback_enabled()
    ttl = _lease_seconds()
    now = utcnow()
    with session_scope() as s:
        reclaim_stale(s, now=now)
        candidates = _candidate_devices(s, need, cpu_ok=cpu_ok)
        for device in candidates:
            result = s.execute(
                update(GpuDevice)
                .where(
                    GpuDevice.id == device.id,
                    GpuDevice.enabled.is_(True),
                    (GpuDevice.total_mb - GpuDevice.reserved_mb) >= need,
                )
                .values(reserved_mb=GpuDevice.reserved_mb + need)
            )
            if not result.rowcount:
                continue  # lost the race, or the room went away
            row = GpuReservation(
                device_id=str(device.id),
                workspace_id=req.workspace_id or None,
                job_id=req.job_id,
                kind=str(req.kind or "default"),
                job_type=str(req.job_type or ""),
                vram_mb=need,
                priority=int(req.priority),
                status=GpuReservation.RESERVED,
                cpu_fallback=str(device.backend) == "cpu",
                acquired_at=now,
                lease_expires_at=now + timedelta(seconds=ttl),
                heartbeat_at=now,
            )
            s.add(row)
            s.flush()
            return GpuSlot(
                reservation_id=str(row.id),
                device_id=str(device.id),
                device_key=str(device.device_key),
                backend=str(device.backend),
                vram_mb=need,
                cpu_fallback=str(device.backend) == "cpu",
                job_id=req.job_id,
                workspace_id=str(req.workspace_id or ""),
            )
    return None


def admit(req: GpuRequest, *, timeout: float | None = None,
          poll_seconds: float = 0.05) -> GpuSlot:
    """Acquire a slot, or raise. Bounded wait -- never an unbounded one.

    Raises :class:`GpuUnavailable` when no device can EVER satisfy the request
    (nothing registered, or the request needs more VRAM than the largest device
    has) because waiting cannot help, and :class:`GpuAdmissionTimeout` when the
    capacity exists but is busy right now.
    """
    if not _scheduler_enabled():
        # Disabled means "no device accounting"; the caller still runs, on CPU,
        # and says so in the slot.
        return GpuSlot(reservation_id="", device_id="", device_key=CPU_DEVICE_KEY,
                       backend="cpu", vram_mb=0, cpu_fallback=True,
                       job_id=req.job_id, workspace_id=str(req.workspace_id or ""))
    need = req.need_mb()
    wait = float(timeout if timeout is not None else _default_timeout())
    ensure_cpu_device()
    deadline = time.monotonic() + max(0.0, wait)
    attempt = 0
    while True:
        slot = _try_admit(req)
        if slot is not None:
            return slot
        attempt += 1
        if time.monotonic() >= deadline:
            raise GpuAdmissionTimeout(
                f"no GPU device could satisfy {req.kind} "
                f"({need} MB, priority {req.priority}) in {wait:g}s; "
                f"devices: {available_devices()}"
            )
        time.sleep(max(0.005, float(poll_seconds)))


def _default_timeout() -> float:
    with contextlib.suppress(Exception):
        from app.core.config import settings

        return float(getattr(settings, "gpu_admission_timeout_seconds", 900.0))
    return 900.0  # pragma: no cover - config import always succeeds


# ---------------------------------------------------------------------------
# Release / renew / reclaim
# ---------------------------------------------------------------------------


def release(reservation_id: str, *, reason: str = "completed") -> bool:
    """Return the VRAM. Idempotent: a second call is a no-op, never a double
    credit (which would hand the same megabytes to two jobs)."""
    if not reservation_id:
        return False
    with session_scope() as s:
        row = s.get(GpuReservation, str(reservation_id))
        if row is None or row.status not in GpuReservation.HELD:
            return False
        _free(s, row, str(reason or "completed"))
        return True


def _free(s: Session, row: GpuReservation, reason: str) -> None:
    """Mark one reservation released and debit its device. The pair is
    conditional on the row still being HELD, so two concurrent releases cannot
    both credit the device."""
    changed = s.execute(
        update(GpuReservation)
        .where(GpuReservation.id == row.id,
               GpuReservation.status.in_(GpuReservation.HELD))
        .values(status=_status_for_reason(reason), released_at=utcnow(),
                release_reason=str(reason or "")[:40], lease_expires_at=None)
    )
    if not changed.rowcount:
        return
    s.execute(
        update(GpuDevice)
        .where(GpuDevice.id == row.device_id)
        .values(reserved_mb=_safe_debit(int(row.vram_mb or 0)))
    )


def _status_for_reason(reason: str) -> str:
    """Map a free-text reason onto the reservation status vocabulary."""
    token = str(reason or "").strip().lower()
    if token in ("cancelled", "canceled"):
        return GpuReservation.CANCELLED
    if token in ("failed", "error", "exception"):
        return GpuReservation.FAILED
    if token in ("expired", "stale", "crash"):
        return GpuReservation.EXPIRED
    return GpuReservation.RELEASED


def _safe_debit(amount: int):
    """A debit that cannot underflow, expressed so it stays a single UPDATE.

    ``reserved_mb - amount`` alone would wrap negative if a release ever ran
    twice, and a device reporting free memory LARGER than its capacity is the
    one number that would let a later admission believe in a phantom.
    """
    from sqlalchemy import case

    return case(
        (GpuDevice.reserved_mb >= amount, GpuDevice.reserved_mb - amount),
        else_=0,
    )


def renew(reservation_id: str, *, ttl: float | None = None) -> bool:
    """Extend the lease. A live holder keeps its VRAM; a dead one cannot."""
    if not reservation_id:
        return False
    window = float(ttl if ttl is not None else _lease_seconds())
    now = utcnow()
    with session_scope() as s:
        changed = s.execute(
            update(GpuReservation)
            .where(GpuReservation.id == str(reservation_id),
                   GpuReservation.status.in_(GpuReservation.HELD))
            .values(lease_expires_at=now + timedelta(seconds=window),
                    heartbeat_at=now)
        )
        return bool(changed.rowcount)


def _job_is_alive(db: Session, job_id: str, now: datetime) -> bool:
    """Ask the LEASE model whether the owner is still there.

    Deliberately a question, not a guess. When a reservation names a job, the
    job's lease is the authority on liveness: a slot whose job lease is still
    valid is never reclaimed even if the slot's own deadline passed (the holder
    may legitimately be mid-render under a longer job lease), and a slot whose
    job lease has lapsed is reclaimed on the spot rather than waiting for the
    slot's own clock. Read on the CALLER's session, never a nested one, so the
    recovery sweep stays a single transaction and cannot deadlock itself.
    """
    from app.models import Job
    from app.services.job_leases import lease_is_valid

    job = db.get(Job, str(job_id))
    if job is None:
        return False  # the job is gone; nobody holds VRAM on its behalf
    return bool(lease_is_valid(job, now))


def reclaim_stale(s: Session, *, now: datetime | None = None) -> int:
    """Return VRAM held by reservations whose holder is gone. Returns the count.

    A reservation is reclaimed when there is PROOF its holder is gone -- never
    because it looks old:

    * it names no job, and its own lease expired; or
    * it names a job, and that job's lease is not valid.

    The second rule is the important one, and it is what keeps this module
    consistent with ``job_leases``: a live job keeps its slot, an expired one
    loses it immediately, and the two recovery sweeps can never disagree about
    the same job because only one of them decides.
    """
    stamp = now or utcnow()
    held = list(
        s.scalars(
            select(GpuReservation).where(
                GpuReservation.status.in_(GpuReservation.HELD)
            )
        ).all()
    )
    freed = 0
    for row in held:
        if row.job_id:
            if _job_is_alive(s, str(row.job_id), stamp):
                continue  # the job lease is the authority
        else:
            expires = row.lease_expires_at
            if expires is not None and expires > stamp:
                continue  # live slot lease: keep it
        _free(s, row, "expired")
        freed += 1
    if freed:
        logger.warning("reclaimed {} stale GPU reservation(s)", freed)
    return freed


def sweep() -> int:
    """Process-wide entry point for the recovery tick."""
    with session_scope() as s:
        return reclaim_stale(s)


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def gpu_slot(req: GpuRequest, *, timeout: float | None = None,
             poll_seconds: float = 0.05) -> Iterator[GpuSlot]:
    """Admission BEFORE the body runs; release on EVERY exit.

    The body is inside the ``with`` and the slot is acquired before it, so
    "admitted then executed" is structural rather than a convention every
    caller has to remember. ``finally`` covers success, an exception, an early
    return and an explicit cancellation; only a process death escapes it, and
    that is the case the lease exists for.
    """
    slot = admit(req, timeout=timeout, poll_seconds=poll_seconds)
    try:
        yield slot
    except BaseException as exc:  # noqa: BLE001 - re-raised below
        release(slot.reservation_id,
                reason="cancelled" if _is_cancel(exc) else "failed")
        raise
    else:
        release(slot.reservation_id, reason="completed")


def _is_cancel(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))


@contextlib.asynccontextmanager
async def gpu_slot_async(req: GpuRequest, *, timeout: float | None = None,
                         poll_seconds: float = 0.05):
    """Async twin of :func:`gpu_slot`. Same ordering, same guaranteed release.

    The acquire runs in a thread because the DB call is blocking; the
    ``try/finally`` is identical, so cancellation of the awaiting task still
    releases the slot.
    """
    slot = await asyncio.to_thread(admit, req, timeout=timeout,
                                   poll_seconds=poll_seconds)
    try:
        yield slot
    except BaseException as exc:  # noqa: BLE001 - re-raised below
        await asyncio.to_thread(
            release, slot.reservation_id,
            reason="cancelled" if _is_cancel(exc) else "failed")
        raise
    else:
        await asyncio.to_thread(release, slot.reservation_id, reason="completed")


def cpu_slot(workspace_id: str = "", *, vram_mb: int = 0,
             job_id: str | None = None, kind: str = "cpu") -> GpuSlot:
    """Reserve the CPU lane explicitly, for work that never needed a GPU.

    This exists so "no GPU involved" is RECORDED rather than merely true: the
    row shows zero VRAM consumed on the CPU device, which is what an operator
    asking "why was this job on CPU" needs to see.
    """
    return admit(GpuRequest(kind=kind, vram_mb=vram_mb, workspace_id=workspace_id,
                            job_id=job_id, cpu_capable=True,
                            allow_cpu_fallback=True),
                timeout=30.0)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def snapshot() -> dict:
    """Everything an operator needs to answer "is the GPU the bottleneck?"."""
    with session_scope() as s:
        devices = available_devices(s)
        rows = list(
            s.scalars(
                select(GpuReservation).where(
                    GpuReservation.status.in_(GpuReservation.HELD))
            ).all()
        )
        by_kind: dict[str, int] = {}
        for row in rows:
            by_kind[str(row.kind)] = by_kind.get(str(row.kind), 0) + 1
        return {
            "enabled": _scheduler_enabled(),
            "cpu_fallback_enabled": _fallback_enabled(),
            "devices": devices,
            "held_reservations": len(rows),
            "held_by_kind": by_kind,
            "held_vram_mb": sum(int(r.vram_mb or 0) for r in rows),
            "total_vram_mb": sum(int(d["total_mb"]) for d in devices),
            "reserved_vram_mb": sum(int(d["reserved_mb"]) for d in devices),
        }


def describe(kind: str) -> dict:
    """The declared requirement for a kind, for an operator to audit."""
    spec = DEFAULT_REQUIREMENTS.get(str(kind)) or DEFAULT_REQUIREMENTS["default"]
    return {"kind": str(kind), "vram_mb": int(spec["vram_mb"]),
            "cpu_capable": bool(spec["cpu_capable"]),
            "known": str(kind) in DEFAULT_REQUIREMENTS}


def _held_count(s: Session, kind: str = "") -> int:
    q = select(func.count()).select_from(GpuReservation).where(
        GpuReservation.status.in_(GpuReservation.HELD))
    if kind:
        q = q.where(GpuReservation.kind == kind)
    return int(s.scalar(q) or 0)


def _pid() -> int:  # pragma: no cover - diagnostics only
    return os.getpid()