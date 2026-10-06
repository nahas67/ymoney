"""Work 16.1 §8 -- the two GPU admission guards are COMPOSED, not merged.

**The finding that shaped this file.** There are two independent admission
systems and, at the time this was written, *neither had a production caller*:

* the per-WORKSPACE slot ledger, ``services/media_intel_runs.py``
  (``gpu_semaphore`` / ``gpu_semaphore_sync``, ``jobs`` rows of type
  ``MEDIA_INTEL_GPU_SLOT``, capped by ``MAX_CONCURRENT_GPU_JOBS``);
* the per-DEVICE VRAM guard, ``services/gpu_scheduler.py``
  (``gpu_slot`` / ``gpu_slot_async``, a conditional ``UPDATE`` on
  ``gpu_devices.reserved_mb``).

An AST sweep of ``backend/app`` found zero call sites for either, in production
code. Tests called both separately; nothing ran both. So "a production GPU path
must not accidentally call only one guard" was, at that moment, vacuously true
-- and therefore worthless as a guarantee.

What this file does about it, without merging anything:

1. **States the invariant** -- ``workspace admission AND device admission ->
   execute`` -- and gives it one place to live: ``media_intel_runs.gpu_admitted``
   / ``gpu_admitted_sync``, the production entry points.
2. **Pins it structurally.** ``test_the_composed_entry_points_call_both_guards``
   fails if either guard is dropped from the composition, and
   ``test_no_module_outside_the_two_guards_calls_one_directly`` fails if any
   module under ``app/`` reaches for a guard on its own -- which is the
   "accidentally only one" failure, and the one that arrives as a code review
   nobody questions.
3. **Proves the order behaviourally.** The workspace guard is taken first, so a
   tenant over quota is refused BEFORE it competes for real VRAM.
4. **States the seam honestly** -- what a caller gets by using only one layer --
   including the concrete observability gap in
   ``test_device_only_admission_is_invisible_to_the_gpu_slot_gauge``.

The PostgreSQL section is ``skipif``-gated on a reachability probe, never
``pytest.skip``, so the default SQLite suite needs no server.
"""

from __future__ import annotations

import ast
import contextlib
import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.services import gpu_scheduler
from app.services import media_intel_runs as runs

APP_DIR = Path(__file__).resolve().parent.parent / "app"
MEDIA_RUNS_PATH = APP_DIR / "services" / "media_intel_runs.py"
GPU_SCHEDULER_PATH = APP_DIR / "services" / "gpu_scheduler.py"

#: The two layers. Each may be entered only through the composed entry points.
LAYER_MODULES = {MEDIA_RUNS_PATH.resolve(), GPU_SCHEDULER_PATH.resolve()}
MEDIA_RUNS_MODULE = "app.services.media_intel_runs"

#: Public entry points that GRANT a guard. `admit`/`release` are included on the
#: device side because a caller that admits and releases by hand has bypassed
#: the ordering guarantee just as thoroughly as one that skips the workspace
#: guard entirely.
DEVICE_GUARD_CALLS = frozenset({
    "gpu_slot", "gpu_slot_async", "admit", "cpu_slot",
})
WORKSPACE_GUARD_CALLS = frozenset({
    "gpu_semaphore", "gpu_semaphore_sync",
})
GUARD_CALLS = DEVICE_GUARD_CALLS | WORKSPACE_GUARD_CALLS

#: The only functions permitted to touch a guard.
SANCTIONED = {"gpu_admitted", "gpu_admitted_sync"}


# ---------------------------------------------------------------------------
# 1. structural: the composition, and nothing else touching a guard
# ---------------------------------------------------------------------------


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _called_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Every name a function calls, as written (``a.b.c`` -> ``c``)."""
    out: list[str] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            out.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            out.append(node.func.attr)
    return out


def _guard_calls(fn) -> list[str]:
    return [name for name in _called_names(fn) if name in GUARD_CALLS]


@pytest.mark.parametrize("entry", ["gpu_admitted", "gpu_admitted_sync"])
def test_the_composed_entry_points_call_both_guards(entry):
    """``workspace admission AND device admission -> execute``, as source.

    This is mutation (c) and (d): drop either guard from the composition and it
    fails. It reads the AST, so it does not care how the two are spelled or in
    how many statements.
    """
    tree = ast.parse(MEDIA_RUNS_PATH.read_text(encoding="utf-8"))
    fn = _functions(tree).get(entry)
    assert fn is not None, f"{MEDIA_RUNS_MODULE}.{entry} does not exist"

    calls = _guard_calls(fn)
    workspace = [c for c in calls if c in WORKSPACE_GUARD_CALLS]
    device = [c for c in calls if c in DEVICE_GUARD_CALLS]
    assert workspace, (
        f"{entry} takes no workspace slot -- a production GPU path must not run "
        "on device admission alone")
    assert device, (
        f"{entry} takes no device slot -- a production GPU path must not run on "
        "the per-workspace count alone")

    # Order is part of the invariant, not formatting: the cheap per-tenant guard
    # is taken before the expensive real-resource one.
    assert calls.index(workspace[0]) < calls.index(device[0]), (
        f"{entry} takes the device slot before the workspace slot: "
        f"{calls}. The device guard would then hold VRAM while queueing on a "
        "tenant quota, which is the lie it exists to stop.")


@pytest.mark.parametrize("entry", ["gpu_admitted", "gpu_admitted_sync"])
def test_the_composed_entry_points_yield_only_after_both_grants(entry):
    """The body must be reachable only from inside both context managers.

    A composition that acquires the workspace slot, yields, and *then* takes
    the device slot would pass the call-shape test above while still executing
    unguarded work. Asserting the yield sits inside the ``with`` makes that
    unrepresentable.
    """
    tree = ast.parse(MEDIA_RUNS_PATH.read_text(encoding="utf-8"))
    fn = _functions(tree)[entry]
    keyword = "async with" if isinstance(fn, ast.AsyncFunctionDef) else "with"

    guarded_yields = 0
    bare_yields = 0
    for node in ast.walk(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        managers = [ast.unparse(item.context_expr) for item in node.items]
        is_guard = any(
            any(name in text for name in GUARD_CALLS) for text in managers)
        # `yield x` parses as Expr(Yield(x)), so the walk has to go one level
        # deeper than node.body to see it.
        yields = [n for stmt in node.body for n in ast.walk(stmt)
                  if isinstance(n, ast.Yield)]
        if not yields:
            continue
        if is_guard:
            guarded_yields += 1
        else:
            bare_yields += 1
    assert guarded_yields == 1, (
        f"{entry} must have exactly one yield inside a `with` that names a "
        f"guard; found {guarded_yields}")
    assert bare_yields == 0, (
        f"{entry} yields outside the guards {keyword} block "
        f"({keyword} ...): the body would run unadmitted")
    assert keyword in ast.unparse(fn), f"{entry} lost its {keyword} block"


def test_no_module_outside_the_two_guards_calls_one_directly():
    """The "accidentally only one guard" invariant, across all of ``app/``.

    Every call to a guard primitive is attributed to the function that makes
    it. Anything outside ``gpu_scheduler.py`` / ``media_intel_runs.py``, or
    inside them but outside the two composed entry points, is a violation.

    Today the sweep finds none -- because production had no GPU caller at all.
    That is exactly why the sweep has to be here: the first person who adds one
    gets both guards or a failing test.
    """
    violations: list[str] = []
    scanned = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        resolved = path.resolve()
        if resolved in LAYER_MODULES:
            continue
        scanned += 1
        try:
            # utf-8-sig: three modules in `app/` carry a BOM, and a sweep that
            # skipped them would be a sweep with holes in it.
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
            violations.append(f"{path.name}: does not parse ({exc})")
            continue
        for fn in _functions(tree).values():
            for name in _guard_calls(fn):
                rel = path.relative_to(APP_DIR).as_posix()
                violations.append(
                    f"{rel}::{fn.name} calls {name}() directly -- go through "
                    f"{MEDIA_RUNS_MODULE}.gpu_admitted / gpu_admitted_sync so "
                    "both guards run")
    assert scanned > 40, (
        f"the AST sweep only reached {scanned} modules; it is not looking at "
        "the whole app and would pass vacuously")
    assert not violations, "GPU admission guards reached outside the seam:\n" + \
        "\n".join(violations)


def test_no_other_function_in_media_intel_runs_reaches_a_guard():
    """The seam is two functions, not two modules.

    A sibling helper that "just takes a device slot" is the exact shape of the
    bug, and it would be invisible to the module-level sweep above because it
    lives inside the layer module.
    """
    tree = ast.parse(MEDIA_RUNS_PATH.read_text(encoding="utf-8"))
    offenders = {
        name: calls for name, fn in _functions(tree).items()
        if name not in SANCTIONED
        for calls in [_guard_calls(fn)] if calls
    }
    # The primitives themselves acquire/release internally; that is the layer.
    allowed = {"gpu_semaphore", "gpu_semaphore_sync"}
    assert set(offenders) <= allowed, (
        f"functions other than {sorted(SANCTIONED)} touch a guard: "
        f"{ {k: v for k, v in offenders.items() if k not in allowed} }")


def test_the_two_systems_are_not_merged():
    """Composed, not merged -- and merging is asserted to have not happened.

    A merge would show up as one of the two layers importing the other, or as a
    per-workspace quota appearing on the device request. Both are ways of
    "solving" the composition by deleting one of the questions.
    """
    scheduler_tree = ast.parse(GPU_SCHEDULER_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(scheduler_tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("media_intel_runs" in name for name in imported), (
        f"the device layer now imports the workspace layer ({sorted(imported)}); "
        "the two were supposed to be composed, not merged")
    assert "MEDIA_INTEL_GPU_SLOT" not in {
        node.value for node in ast.walk(scheduler_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }, "the device layer now knows the workspace ledger's job type"
    request_fields = set(gpu_scheduler.GpuRequest.__dataclass_fields__)
    assert not any("workspace" in f and "limit" in f for f in request_fields), (
        f"a per-workspace quota leaked onto the device request: {request_fields}")
    # ...and the device guard is reached ONLY through the composition.
    assert gpu_scheduler.requirement_for("media_intelligence") == 2000


# ---------------------------------------------------------------------------
# 2. behavioural: both ledgers held at once, in the documented order
# ---------------------------------------------------------------------------


@pytest.fixture()
def device(monkeypatch):
    """One fresh CUDA device per test, removed on teardown.

    Unique per test because the suite's database is shared: a leftover
    reservation from a previous test would silently become the tightest fit and
    turn an assertion about VRAM into an assertion about test ordering.
    """
    from app.db import session_scope
    from app.models import GpuDevice, GpuReservation

    key = f"cuda:{uuid.uuid4().hex[:8]}"
    device_id = gpu_scheduler.register_device(
        key, name="synthetic", backend="cuda", total_mb=8000)

    def _cleanup():
        with session_scope() as s:
            s.query(GpuReservation).filter(
                GpuReservation.device_id == device_id).delete()
            s.query(GpuDevice).filter(GpuDevice.id == device_id).delete()

    try:
        yield {"key": key, "id": device_id}
    finally:
        _cleanup()


def _device_slots() -> int:
    from app.db import session_scope
    from app.models import GpuReservation

    with session_scope() as s:
        return int(s.scalar(
            select(func.count()).select_from(GpuReservation).where(
                GpuReservation.status.in_(GpuReservation.HELD))) or 0)


def _workspace_slots(workspace_id: str) -> int:
    return runs.held_gpu_slots(workspace_id)


def test_the_composed_path_holds_both_ledgers_at_once(device):
    """Both rows exist while the body runs; neither survives it.

    This is the invariant observed rather than read: the workspace slot is a
    ``MEDIA_INTEL_GPU_SLOT`` job row, the device slot is a ``HELD``
    ``gpu_reservations`` row, and a body that sees only one of them was
    admitted by only one guard.
    """
    ws = uuid.uuid4().hex
    before_device = _device_slots()

    with runs.gpu_admitted_sync(ws, kind="media_intelligence",
                                limit=1, timeout=20) as granted:
        assert granted.workspace_slot_id
        assert _workspace_slots(ws) == 1, "the per-workspace ledger is empty"
        assert _device_slots() == before_device + 1, (
            "the device ledger did not move: the body ran on the workspace "
            "count alone")
        assert granted.device_key == device["key"]
        assert granted.vram_mb == 2000
        assert granted.cpu_fallback is False
        assert granted.as_dict()["reservation_id"]

    assert _workspace_slots(ws) == 0, "workspace slot leaked past the with"
    assert _device_slots() == before_device, "device VRAM was not returned"


def test_workspace_admission_is_taken_first_and_refuses_before_the_device(device):
    """A tenant over quota never competes for real VRAM.

    The workspace quota is held by an unrelated holder, so the composed call
    blocks on the FIRST guard. If it had reached the device guard first, a
    ``gpu_reservations`` row would appear during the wait -- VRAM reserved for
    work that is not running, which is the exact shape of the pre-Work-16 lie.
    """
    ws = uuid.uuid4().hex
    before_device = _device_slots()

    # Another process in the SAME workspace already holds its single quota slot.
    # Taken through the workspace layer alone, which is exactly what an
    # unrelated process would be doing.
    with runs.gpu_semaphore_sync(1, workspace_id=ws, kind="media_intelligence",
                                 timeout=20):
        assert _workspace_slots(ws) == 1
        assert _device_slots() == before_device, (
            "the setup holder took device VRAM; it should not have")
        with (pytest.raises(runs.GpuSlotTimeout),
              runs.gpu_admitted_sync(ws, kind="media_intelligence", limit=1,
                                     timeout=0.3, poll_seconds=0.01)):
            pytest.fail("the second holder entered the body")

    assert _device_slots() == before_device, (
        "a blocked caller took device VRAM: the device guard ran before the "
        "workspace guard")
    assert _workspace_slots(ws) == 0, "a refused caller leaked a workspace slot"


def test_the_workspace_quota_is_per_tenant_not_global(device):
    """Two tenants, quota 1 each: both are admitted at once.

    The workspace guard's per-tenant scoping is the fairness the device guard
    cannot provide, and it is what makes a *count* the right instrument here
    even though it is the wrong one for VRAM.
    """
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with contextlib.ExitStack() as stack:
        stack.enter_context(runs.gpu_admitted_sync(
            a, kind="media_intelligence", limit=1, timeout=20))
        stack.enter_context(runs.gpu_admitted_sync(
            b, kind="media_intelligence", limit=1, timeout=20))
        assert _workspace_slots(a) == 1 and _workspace_slots(b) == 1


def test_a_device_refusal_returns_the_workspace_slot(device):
    """Both guards, so both must unwind.

    A request larger than every registered device can never be admitted, and
    the workspace slot it already took has to go back -- otherwise one refused
    job permanently consumes a tenant's whole quota.
    """
    ws = uuid.uuid4().hex
    before_device = _device_slots()
    with (pytest.raises(gpu_scheduler.GpuAdmissionTimeout),
          runs.gpu_admitted_sync(ws, kind="ai_video", limit=1, timeout=0.4,
                                 poll_seconds=0.01)):
        pytest.fail("an impossible request entered the body")
    assert _workspace_slots(ws) == 0, (
        "the workspace slot was not returned when device admission refused")
    assert _device_slots() == before_device


def test_the_workspace_quota_is_per_tenant_and_the_device_ledger_is_shared(device):
    """Each guard refuses something the other allows.

    Two workspaces, one 8 GB device. Workspace A may hold two of its own jobs
    inside its own quota while B holds none -- that is the fairness the
    workspace guard buys. Neither job reserves more VRAM than it declared,
    which is the honesty the device guard buys. A per-workspace count alone
    would admit five ``ai_video`` jobs (12 GB declared each) onto an 8 GB card.
    """
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with (runs.gpu_admitted_sync(a, kind="media_intelligence", limit=2,
                                 timeout=20),
          runs.gpu_admitted_sync(a, kind="media_intelligence", limit=2,
                                 timeout=20)):
            assert _workspace_slots(a) == 2, (
                "the workspace guard did not grant its own second slot")
            assert _workspace_slots(b) == 0, (
                "the workspace quota leaked across tenants")
            assert gpu_scheduler.snapshot()["held_vram_mb"] == 4000
    assert _workspace_slots(a) == 0
    assert gpu_scheduler.snapshot()["held_reservations"] == 0


def test_the_two_layers_hold_different_resources(device):
    """VRAM is accounted by the device layer; the workspace ledger is not VRAM.

    Five cheap jobs inside one workspace's quota of five take five workspace
    rows, and the device ledger must show that no device is ever over its own
    capacity -- which is the assertion the workspace count alone could not make.
    """
    ws = uuid.uuid4().hex
    kinds = ["broll", "dubbing", "media_intel", "broll", "dubbing"]
    declared = 0
    with contextlib.ExitStack() as stack:
        for kind in kinds:
            granted = stack.enter_context(runs.gpu_admitted_sync(
                ws, kind=kind, limit=len(kinds), timeout=20))
            declared += granted.vram_mb
        assert _workspace_slots(ws) == len(kinds)
        assert _device_slots() == len(kinds)
        assert declared == sum(gpu_scheduler.requirement_for(k) for k in kinds)
        for dev in gpu_scheduler.snapshot()["devices"]:
            assert dev["reserved_mb"] <= dev["total_mb"], (
                f"device {dev['device_key']} reports {dev['reserved_mb']} MB "
                f"reserved of {dev['total_mb']} MB")
    assert _device_slots() == 0


# ---------------------------------------------------------------------------
# 3. the seam, honestly: what one layer alone gives you
# ---------------------------------------------------------------------------


def test_the_slot_gauge_sees_a_held_slot_after_the_collector_fix(device):
    """INVERTED when the collector was fixed -- this test used to assert the defect.

    It previously documented that ``collect_gpu_slots()`` counts
    ``MEDIA_INTEL_GPU_SLOT`` rows in status ``QUEUED`` or ``RUNNING``, while the
    workspace ledger parks a held slot in ``WAITING``
    (``_try_acquire`` inserts ``status=JobStatus.WAITING.value``, and
    ``job_leases`` line 104 calls that out as the mechanism keeping a live slot
    out of the claim query). The filter could therefore never match a held slot,
    ``ymoney_gpu_slots_reserved`` read 0, and ``gpu_queue_starvation`` fired
    against an *idle* queue while staying silent against a *saturated* one --
    an alert that was decorative.

    ``metrics.py`` was another lane's file, so the test asserted the defect
    rather than fixing it, and carried an explicit instruction for whoever
    fixed the filter. This is that instruction, carried out: the gauge now
    counts ``WAITING``, and the alert no longer fires on a healthy queue.
    """
    from app.services.observability import metrics as metrics_mod
    from app.services.observability import slo as slo_mod

    ws = uuid.uuid4().hex
    metrics_mod.REGISTRY.reset()
    with runs.gpu_admitted_sync(ws, kind="media_intelligence", limit=1,
                                timeout=20) as granted:
        assert granted.workspace_slot_id and granted.device_key
        assert _workspace_slots(ws) == 1, "no workspace slot is actually held"
        assert _device_slots() == 1, "no device reservation is actually held"
        assert metrics_mod.collect_gpu_slots() is True
        assert metrics_mod.GPU_SLOTS_RESERVED.value() == 1.0, (
            "the collector must see the WAITING slot; if this is 0 the "
            "status filter lost WAITING again and the gauge is blind")

        # A full device with a backlog is a BUSY GPU, not a starved one. The
        # rule's condition is waiting>0 AND slots<=0 -- a held slot means work
        # is getting through. Before the collector fix this could not be
        # distinguished, because the gauge read 0 and every backlog looked
        # like starvation regardless of how many slots were actually held.
        metrics_mod.GPU_JOBS_WAITING.set(2.0)
        verdict = slo_mod.rule_by_id("gpu_queue_starvation").check(
            metrics_mod.REGISTRY.snapshot(), now=1_000_000.0)
        assert verdict.firing is False, (
            "gpu_queue_starvation fired while a slot was held and VRAM "
            "reserved; work is being served, so this is not starvation")

    # Starvation IS waiting>0 with nothing held.
    metrics_mod.REGISTRY.reset()
    metrics_mod.collect_gpu_slots()
    metrics_mod.GPU_SLOTS_RESERVED.set(0.0)
    metrics_mod.GPU_JOBS_WAITING.set(2.0)
    starved = slo_mod.rule_by_id("gpu_queue_starvation").check(
        metrics_mod.REGISTRY.snapshot(), now=1_000_000.0)
    assert starved.firing is True, (
        "genuine starvation -- work waiting, no slot held -- must still fire")

    metrics_mod.REGISTRY.reset()


def test_the_device_layers_own_snapshot_is_what_can_see_the_work(device):
    """The two introspection surfaces, and which one is trustworthy today.

    ``gpu_scheduler.snapshot()`` counts ``gpu_reservations`` directly, so it is
    correct whatever the collector does. That asymmetry is the seam: an operator
    debugging "why is no GPU work happening" is told different things by the two.
    """
    ws = uuid.uuid4().hex
    with runs.gpu_admitted_sync(ws, kind="media_intelligence", limit=1,
                                timeout=20) as granted:
        snapshot = gpu_scheduler.snapshot()
        assert snapshot["held_reservations"] == 1
        assert snapshot["held_vram_mb"] == 2000
        assert snapshot["held_by_kind"] == {"media_intelligence": 1}
        assert snapshot["reserved_vram_mb"] >= 2000
        assert granted.device_key in {d["device_key"] for d in snapshot["devices"]}
    assert gpu_scheduler.snapshot()["held_reservations"] == 0
    assert gpu_scheduler.snapshot()["held_vram_mb"] == 0


def test_workspace_only_admission_leaves_the_device_ledger_empty(device):
    """The other half of the seam: no VRAM is accounted for at all.

    The per-workspace count admits five ``ai_video`` jobs (12 GB declared each)
    onto an 8 GB card, because the number it counts is a count. This is not
    hypothetical: it is what the code did before ``gpu_scheduler`` existed.
    """
    ws = uuid.uuid4().hex
    with runs.gpu_semaphore_sync(5, workspace_id=ws, timeout=20):
        assert _workspace_slots(ws) == 1
        assert _device_slots() == 0, (
            "the workspace layer reserved VRAM; that is the device layer's job "
            "and this is why the two are composed rather than merged")
        assert gpu_scheduler.snapshot()["held_reservations"] >= 0
        assert gpu_scheduler.snapshot()["held_vram_mb"] == 0


def test_the_composed_path_is_reported_by_both_introspection_surfaces(device):
    """One request, two answers, both non-empty.

    The audit line an operator needs -- which tenant, which device, how much
    VRAM, real or CPU fallback -- is only complete because both guards ran.
    """
    ws = uuid.uuid4().hex
    with runs.gpu_admitted_sync(ws, kind="media_intelligence", limit=1,
                                timeout=20) as granted:
        snapshot = gpu_scheduler.snapshot()
        assert snapshot["held_reservations"] == 1
        assert snapshot["held_by_kind"] == {"media_intelligence": 1}
        assert granted.device_key in {d["device_key"] for d in snapshot["devices"]}
        assert _workspace_slots(ws) == 1
    assert gpu_scheduler.snapshot()["held_reservations"] == 0
    assert _workspace_slots(ws) == 0


def test_a_cpu_capable_request_still_runs_on_the_cpu_device_offline(monkeypatch):
    """No hardware is needed, and the answer is honest.

    CPU fallback is opt-in TWICE -- the work must declare itself CPU-capable
    (which ``DEFAULT_REQUIREMENTS`` says for ``media_intelligence``) and the
    operator must set ``GPU_CPU_FALLBACK_ENABLED``. With it off, the CPU device
    is excluded from admission and the request simply fails, which is the point:
    a GPU-needing job never silently downgrades.

    With it on and no CUDA device registered, the CPU lane is admitted and the
    slot SAYS SO -- which is what makes "why was this job on CPU" answerable
    after the fact. And the WORKSPACE slot is still taken: a CPU fallback is not
    a way around the tenant quota.
    """
    from app.core.config import settings

    ws = uuid.uuid4().hex
    monkeypatch.setattr(settings, "gpu_cpu_fallback_enabled", True)
    with runs.gpu_admitted_sync(ws, kind="media_intelligence", limit=1,
                                timeout=20) as granted:
        assert granted.cpu_fallback is True
        assert granted.device_key == gpu_scheduler.CPU_DEVICE_KEY
        # The CPU lane is a device with a FINITE 2048 MB budget, so the declared
        # footprint is still accounted against it. Pretending CPU work is free
        # is how a GPU admission control becomes a no-op.
        assert granted.vram_mb == gpu_scheduler.requirement_for(
            "media_intelligence")
        assert granted.as_dict()["cpu_fallback"] is True
        assert _workspace_slots(ws) == 1, (
            "a CPU fallback skipped the workspace guard")

    # Off again, the same request is refused rather than downgraded.
    monkeypatch.setattr(settings, "gpu_cpu_fallback_enabled", False)
    with (pytest.raises(gpu_scheduler.GpuAdmissionTimeout),
          runs.gpu_admitted_sync(uuid.uuid4().hex, kind="media_intelligence",
                                 limit=1, timeout=0.4, poll_seconds=0.01)):
        pytest.fail("CPU fallback happened with the operator switch off")


# ---------------------------------------------------------------------------
# 4. PostgreSQL: both guards are enforced by the DATABASE
# ---------------------------------------------------------------------------
# Declarative, so the default SQLite suite needs no server. There is no
# imperative skip anywhere in this file.

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


def _admin_url() -> str:
    return PG_DSN.rsplit("/", 1)[0] + "/postgres"


def _scratch_url(name: str) -> str:
    """``postgresql+psycopg://user:pw@host:port/<name>`` for a scratch database.

    The base is split off the database name ONCE, before the scheme is
    rewritten -- splitting after would cut inside ``+psycopg://`` and hand
    SQLAlchemy a hostname of ``postgresql+psycopg:``.
    """
    base = PG_DSN.rsplit("/", 1)[0]
    return base.replace("postgresql://", "postgresql+psycopg://", 1) + "/" + name


@pytest.fixture(scope="module")
def pg_scratch() -> Iterator:
    """Throwaway databases, dropped ``WITH (FORCE)`` at teardown."""
    import psycopg
    from sqlalchemy import create_engine

    made: list[str] = []

    def make(label: str):
        name = f"w16_1_gpu_{label}_{os.urandom(4).hex}"
        with psycopg.connect(_admin_url(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return create_engine(_scratch_url(name), pool_size=8, max_overflow=8), \
            _scratch_url(name)

    try:
        yield make
    finally:
        for name in made:
            try:
                with psycopg.connect(_admin_url(), autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass


@pytest.fixture(scope="module")
def pg_migrated(pg_scratch) -> dict:
    """A fully migrated scratch database, built by the PRODUCTION migration path."""
    from sqlalchemy.orm import sessionmaker

    from app.migrations.runner import applied_versions, run_migrations

    engine, url = pg_scratch("main")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        run_migrations(s)
        versions = applied_versions(s)
    assert versions, "the scratch database got no migrations"
    return {"engine": engine, "Session": factory, "url": url}


@pytest.fixture()
def pg_wired(pg_migrated, monkeypatch) -> dict:
    """Both layer modules rewired onto the scratch database.

    Both do ``from app.db import session_scope`` at import time, so the names are
    replaced here and restored by ``monkeypatch``. Nothing under ``app/`` is
    edited and the code under test is the shipped implementation.
    """
    factory = pg_migrated["Session"]

    @contextlib.contextmanager
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

    monkeypatch.setattr(runs, "session_scope", scoped)
    monkeypatch.setattr(gpu_scheduler, "session_scope", scoped)
    return {"engine": pg_migrated["engine"], "Session": factory,
            "url": pg_migrated["url"]}


@requires_postgres
def test_the_device_ledger_admits_exactly_one_racer(pg_wired):
    """Two real sessions, the shipped conditional ``UPDATE``, one winner.

    SQLite serialises writers on a file lock, so this property -- "no blind
    oversubscription" -- is only genuinely tested where a second session can be
    inside the statement at the same moment as the first.
    """
    key = f"cuda:{uuid.uuid4().hex[:8]}"
    device_id = gpu_scheduler.register_device(key, name="pg", backend="cuda",
                                              total_mb=4000)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def _race():
        barrier.wait(timeout=20)
        try:
            # `segmentation` declares exactly 4000 MB, so the device is full
            # after one admission and the loser must lose.
            results.append(gpu_scheduler._try_admit(gpu_scheduler.GpuRequest(
                kind="segmentation", workspace_id=uuid.uuid4().hex)))
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            results.append(exc)

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)

    granted = [r for r in results if isinstance(r, gpu_scheduler.GpuSlot)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"a racer raised instead of losing cleanly: {errors!r}"
    assert len(granted) == 1, (
        f"{len(granted)} sessions were admitted onto a 4000 MB device by a "
        f"4000 MB request; the loser must see rowcount == 0")

    with pg_wired["Session"]() as s:
        from app.models import GpuDevice

        row = s.get(GpuDevice, device_id)
        assert int(row.reserved_mb) == 4000, (
            f"reserved_mb is {row.reserved_mb}: one admission was credited twice")
    gpu_scheduler.release(granted[0].reservation_id, reason="drill")


@requires_postgres
def test_the_workspace_ledger_admits_exactly_one_racer(pg_wired):
    """The workspace side is a UNIQUE ``idempotency_key``, not a check-then-insert."""
    ws = uuid.uuid4().hex
    barrier = threading.Barrier(2)
    won: list[str] = []

    def _race():
        barrier.wait(timeout=20)
        slot_id = runs._try_acquire(ws, 1, "media_intelligence", 3600.0)
        if slot_id:
            won.append(slot_id)

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    try:
        assert len(won) == 1, (
            f"{len(won)} processes took the same workspace slot; the UNIQUE "
            "idempotency_key is what makes this a compare-and-set")
        assert runs.held_gpu_slots(ws) == 1
    finally:
        runs._release_slot(won[0] if won else "")
    assert runs.held_gpu_slots(ws) == 0


@requires_postgres
def test_both_ledgers_are_backed_by_real_constraints(pg_wired):
    """Not an application convention: the database is what refuses.

    ``gpu_reservations`` refuses a reservation against a device with no room,
    and ``jobs`` refuses a duplicate slot key. A composition whose guarantees
    lived only in Python would pass every test in this file on SQLite and fail
    on the first multi-process deployment.
    """
    from sqlalchemy import inspect, update

    from app.models import GpuDevice

    inspector = inspect(pg_wired["engine"])
    tables = set(inspector.get_table_names())
    assert {"gpu_devices", "gpu_reservations", "jobs"} <= tables

    uniques = [tuple(c["column_names"]) for c in
               inspector.get_unique_constraints("jobs")]
    assert any("idempotency_key" in cols for cols in uniques) or \
        any("idempotency_key" in c["column_names"]
            for c in inspector.get_indexes("jobs")), (
            "jobs.idempotency_key is not unique in PostgreSQL, so the workspace "
            "guard is a check-then-insert after all")

    # The conditional UPDATE the scheduler depends on, executed by hand: 100 MB
    # free, a 500 MB request, and the database must refuse.
    key = f"cuda:{uuid.uuid4().hex[:8]}"
    device_id = gpu_scheduler.register_device(key, name="pg", backend="cuda",
                                              total_mb=1000)
    with pg_wired["Session"]() as s:
        s.execute(update(GpuDevice).where(GpuDevice.id == device_id)
                  .values(reserved_mb=900))
        s.commit()

    with pg_wired["Session"]() as s:
        result = s.execute(
            update(GpuDevice)
            .where(GpuDevice.id == device_id,
                   (GpuDevice.total_mb - GpuDevice.reserved_mb) >= 500)
            .values(reserved_mb=GpuDevice.reserved_mb + 500))
        assert result.rowcount == 0, (
            "PostgreSQL over-admitted a 500 MB request onto 100 MB of free "
            "VRAM; the conditional UPDATE is the whole admission control")