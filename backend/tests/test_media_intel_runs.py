"""Run lifecycle, cache, chunks, GPU admission and isolation (Work 12 Lane A).

Contracts §16 rows owned by this file: worker cancellation, GPU concurrency,
workspace isolation -- plus the contracts §3 rules they depend on (cache key,
status machine, retry rules, chunk resume) and the readiness probe.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.engine.intel import base as intel_base
from app.services import media_intel_runs as runs

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _ws(db, workspace_with_user):
    from app.models import Workspace

    return db.get(Workspace, workspace_with_user["workspace"])


def _asset(db, workspace_id: str, checksum: str = "sum-1"):
    from app.models import MediaAsset

    row = MediaAsset(workspace_id=workspace_id, type="audio",
                     storage_key="src.wav", checksum=checksum)
    db.add(row)
    db.flush()
    return row


def _run(db, ws, *, kind="alignment", checksum="sum-1", provider="whisperx_alignment",
         model="v1", params=None):
    asset = _asset(db, ws.id, checksum=checksum)
    return runs.create_run(db, ws, kind=kind, asset=asset, provider_key=provider,
                           model_version=model, params=params or {"model": "large"})


# ---------------------------------------------------------------------------
# cache key (contracts §3)
# ---------------------------------------------------------------------------


def test_canonical_params_is_order_independent():
    a = runs.canonical_params({"b": 1, "a": {"y": 2, "x": 3}})
    b = runs.canonical_params({"a": {"x": 3, "y": 2}, "b": 1})
    assert a == b
    assert runs.params_hash({"b": 1}) == runs.params_hash({"b": 1})
    assert runs.params_hash({"b": 1}) != runs.params_hash({"b": 2})


def test_cache_key_changes_with_every_input():
    base = runs.cache_key("sum", "prov", "v1", {"a": 1})
    assert base != runs.cache_key("sum2", "prov", "v1", {"a": 1})
    assert base != runs.cache_key("sum", "prov2", "v1", {"a": 1})
    assert base != runs.cache_key("sum", "prov", "v2", {"a": 1})
    assert base != runs.cache_key("sum", "prov", "v1", {"a": 2})
    assert base == runs.cache_key("sum", "prov", "v1", {"a": 1})
    assert len(base) == 64


# ---------------------------------------------------------------------------
# create / cache / force
# ---------------------------------------------------------------------------


def test_create_run_records_the_manifest_as_pending(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    assert row.status == "PENDING"
    assert row.workspace_id == ws.id
    assert row.params_hash == runs.params_hash({"model": "large"})
    assert row.asset_checksum == "sum-1"
    assert dto["cache_hit"] is False
    assert dto["terminal"] is False


def test_cache_hit_does_not_create_a_second_run(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    first = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, first["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row, metrics={"words": 3}, processing_ms=42)
    db_session.commit()

    second = _run(db_session, ws)
    assert second["cache_hit"] is True
    assert second["id"] == first["id"]
    assert second["status"] == "COMPLETED"
    assert second["processing_ms"] == 42


def test_force_bypasses_the_cache(db_session, workspace_with_user):
    from app.models import MediaIntelRun

    ws = _ws(db_session, workspace_with_user)
    first = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, first["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row)
    db_session.commit()

    asset = _asset(db_session, ws.id)
    forced = runs.create_run(db_session, ws, kind="alignment", asset=asset,
                             provider_key="whisperx_alignment", model_version="v1",
                             params={"model": "large"}, force=True)
    assert forced["cache_hit"] is False
    assert forced["id"] != first["id"]
    mine = (db_session.query(MediaIntelRun)
            .filter(MediaIntelRun.workspace_id == ws.id).count())
    assert mine == 2
    # once the forced run completes, the cache points at the FORCED run
    forced_row = runs.get_run(db_session, ws.id, forced["id"])
    runs.start_run(db_session, forced_row)
    runs.complete_run(db_session, forced_row)
    again = _run(db_session, ws)
    assert again["cache_hit"] is True
    assert again["id"] == forced["id"]


def test_a_failed_prior_run_is_not_a_cache_hit(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    first = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, first["id"])
    runs.start_run(db_session, row)
    runs.fail_run(db_session, row, "PROVIDER_TIMEOUT")
    db_session.commit()

    second = _run(db_session, ws)
    assert second["cache_hit"] is False
    assert second["id"] != first["id"]
    assert second["status"] == "PENDING"


# ---------------------------------------------------------------------------
# status machine
# ---------------------------------------------------------------------------


def test_status_machine_completed(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    assert row.status == "RUNNING"
    assert row.started_at is not None
    runs.complete_run(db_session, row, metrics={"lufs": -16.0}, warnings=["clipped"],
                      processing_ms=120, gpu_ms=80, cost_micros=500)
    assert row.status == "COMPLETED"
    assert row.finished_at is not None
    assert row.progress == 100
    assert row.warnings_json == ["clipped"]
    assert row.metrics_json == {"lufs": -16.0}
    assert (row.processing_ms, row.gpu_ms, row.cost_micros) == (120, 80, 500)


def test_a_terminal_run_cannot_restart(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.fail_run(db_session, row, "BOOM")
    with pytest.raises(ValueError):
        runs.start_run(db_session, row)


def test_unavailable_is_a_first_class_terminal_state(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.unavailable_run(db_session, row, "whisperx not installed")
    assert row.status == "UNAVAILABLE"
    assert row.error_code == "PROVIDER_UNAVAILABLE"
    assert row.metrics_json["reason"] == "whisperx not installed"
    assert runs.run_dto(row)["terminal"] is True
    assert runs.run_dto(row)["retryable"] is True


def test_cancel_sets_the_flag_and_the_terminal_state(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.cancel_run(db_session, row)
    assert row.cancel_requested is True
    assert row.status == "CANCELLED"
    assert row.error_code == "CANCELLED"
    # idempotent: a second cancel keeps the terminal state
    runs.cancel_run(db_session, row)
    assert row.status == "CANCELLED"


def test_cancel_keeps_partial_chunk_state_for_a_resume(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.plan_run_chunks(db_session, row, 1800, chunk_seconds=600)
    runs.mark_chunk(db_session, row, 0, "COMPLETED", input_checksum="c0")
    runs.mark_chunk(db_session, row, 1, "RUNNING", input_checksum="c1")
    runs.cancel_run(db_session, row)
    assert row.chunks_done == 1
    assert [c.idx for c in runs.pending_chunks(db_session, row)] == [1, 2]


# ---------------------------------------------------------------------------
# retry rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "UNAVAILABLE"])
def test_retry_is_allowed_for_the_three_retryable_states(db_session, workspace_with_user, state):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    if state == "FAILED":
        runs.fail_run(db_session, row, "BOOM")
    elif state == "CANCELLED":
        runs.cancel_run(db_session, row)
    else:
        runs.unavailable_run(db_session, row, "no backend")
    runs.retry_run(db_session, row)
    assert row.status == "PENDING"
    assert row.finished_at is None
    assert row.error_code == ""
    assert row.cancel_requested is False


@pytest.mark.parametrize("state", ["PENDING", "RUNNING", "COMPLETED"])
def test_retry_is_refused_for_live_and_successful_runs(db_session, workspace_with_user, state):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    if state in {"RUNNING", "COMPLETED"}:
        runs.start_run(db_session, row)
    if state == "COMPLETED":
        runs.complete_run(db_session, row)
    with pytest.raises(runs.RunNotRetryable):
        runs.retry_run(db_session, row)


# ---------------------------------------------------------------------------
# chunks + resumability
# ---------------------------------------------------------------------------


def test_plan_chunks_splits_and_keeps_a_short_tail():
    assert runs.plan_chunks(0) == []
    assert runs.plan_chunks(-5) == []
    chunks = runs.plan_chunks(1250, 600)
    assert [c["idx"] for c in chunks] == [0, 1, 2]
    assert chunks[0] == {"idx": 0, "start_s": 0.0, "end_s": 600.0}
    assert chunks[-1] == {"idx": 2, "start_s": 1200.0, "end_s": 1250.0}
    with pytest.raises(ValueError):
        runs.plan_chunks(10, 0)


def test_plan_run_chunks_records_total_and_is_idempotent(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    first = runs.plan_run_chunks(db_session, row, 1250, chunk_seconds=600)
    assert len(first) == 3
    assert row.chunks_total == 3
    runs.mark_chunk(db_session, row, 0, "COMPLETED", "c0")
    runs.plan_run_chunks(db_session, row, 1250, chunk_seconds=600)  # re-plan
    assert row.chunks_total == 3
    assert row.chunks_done == 1, "re-planning must not wipe chunk state"
    assert [c.idx for c in runs.pending_chunks(db_session, row)] == [1, 2]


def test_mark_chunk_rejects_an_unknown_status(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.plan_run_chunks(db_session, row, 300, chunk_seconds=100)
    with pytest.raises(ValueError):
        runs.mark_chunk(db_session, row, 0, "ALMOST_DONE")
    assert runs.pending_chunks(db_session, row)[0].status == "PENDING"


def test_resume_skips_completed_chunks(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.plan_run_chunks(db_session, row, 1800, chunk_seconds=600)
    runs.mark_chunk(db_session, row, 0, "COMPLETED", input_checksum="c0")
    runs.mark_chunk(db_session, row, 1, "FAILED", input_checksum="c1")
    todo = runs.resume_plan(db_session, row, current_checksums={0: "c0", 1: "c1", 2: ""})
    assert [c["idx"] for c in todo] == [1, 2]


def test_resume_invalidates_only_the_changed_chunk(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.plan_run_chunks(db_session, row, 1800, chunk_seconds=600)
    for idx in (0, 1):
        runs.mark_chunk(db_session, row, idx, "COMPLETED", input_checksum=f"c{idx}")
    runs.mark_chunk(db_session, row, 2, "COMPLETED", input_checksum="c2")
    # chunk 1's input changed upstream; 0 and 2 are still valid
    todo = runs.resume_plan(db_session, row, current_checksums={0: "c0", 1: "CHANGED", 2: "c2"})
    assert [c["idx"] for c in todo] == [1]
    assert row.chunks_done == 2
    again = runs.resume_plan(db_session, row, current_checksums={0: "c0", 1: "CHANGED", 2: "c2"})
    assert [c["idx"] for c in again] == [1], "the invalidated chunk stays pending"


def test_resume_without_new_checksums_trusts_completed_chunks(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.plan_run_chunks(db_session, row, 1200, chunk_seconds=600)
    runs.mark_chunk(db_session, row, 0, "COMPLETED", input_checksum="c0")
    assert runs.resume_plan(db_session, row) == [{"idx": 1, "start_s": 600.0, "end_s": 1200.0}]


# ---------------------------------------------------------------------------
# worker cancellation (contracts §16 matrix row)
# ---------------------------------------------------------------------------


def test_worker_cancellation_ends_cancelled_with_state_intact(db_session, workspace_with_user):
    """A chunk loop polling cancel_requested stops and the run ends CANCELLED."""
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.plan_run_chunks(db_session, row, 1800, chunk_seconds=600)

    def should_cancel() -> bool:
        db_session.refresh(row)
        return bool(row.cancel_requested)

    processed = []
    with pytest.raises(intel_base.ProviderCancelled):
        for chunk in runs.plan_chunks(1800, 600):
            intel_base.check_control(should_cancel, intel_base.deadline_in(30))
            runs.mark_chunk(db_session, row, chunk["idx"], "COMPLETED",
                            input_checksum=f"c{chunk['idx']}")
            processed.append(chunk["idx"])
            if chunk["idx"] == 0:
                runs.cancel_run(db_session, row)  # operator hits cancel mid-run
    assert processed == [0]
    assert row.status == "CANCELLED"
    assert row.chunks_done == 1
    assert [c.idx for c in runs.pending_chunks(db_session, row)] == [1, 2]


def test_cancellation_after_a_timeout_still_terminates_cleanly(db_session, workspace_with_user):
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    with pytest.raises(intel_base.ProviderTimeout):
        intel_base.check_control(lambda: False, time.monotonic() - 0.001)
    runs.fail_run(db_session, row, "TIMEOUT")
    assert row.status == "FAILED"
    assert row.error_code == "TIMEOUT"


# ---------------------------------------------------------------------------
# GPU admission (contracts §3 + §16 matrix row)
# ---------------------------------------------------------------------------


def test_gpu_limit_falls_back_to_one(monkeypatch):
    """Explicit limit wins, then settings, then the env override, then 1."""
    from app.core.config import settings

    monkeypatch.delenv("INTEL_MAX_CONCURRENT_GPU_JOBS", raising=False)
    assert runs.gpu_limit(4) == 4
    assert runs.gpu_limit(0) == 1, "a non-positive explicit limit is ignored"

    # settings.max_concurrent_gpu_jobs is the contract source (contracts §3);
    # it is a real field now, so it is patched directly and must win.
    if hasattr(settings, "max_concurrent_gpu_jobs"):
        assert runs.gpu_limit() == settings.max_concurrent_gpu_jobs
        monkeypatch.setattr(settings, "max_concurrent_gpu_jobs", 3)
        assert runs.gpu_limit() == 3
        assert runs.gpu_limit(2) == 2, "an explicit limit still wins"
    # the env override only matters when the setting is absent/zero
    monkeypatch.setattr(settings, "max_concurrent_gpu_jobs", 0, raising=False)
    monkeypatch.setenv("INTEL_MAX_CONCURRENT_GPU_JOBS", "4")
    assert runs.gpu_limit() == 4
    monkeypatch.setenv("INTEL_MAX_CONCURRENT_GPU_JOBS", "not-a-number")
    assert runs.gpu_limit() == 1, "a malformed env value is ignored, never fatal"


async def _hold(semaphore_factory, counter, peak):
    async with semaphore_factory() as slot:
        counter[0] += 1
        peak[0] = max(peak[0], counter[0])
        await asyncio.sleep(0.02)
        counter[0] -= 1
        return slot


@pytest.mark.parametrize("limit,expected_peak", [(1, 1), (2, 2)])
async def test_gpu_concurrency_limit_is_enforced(workspace_with_user, limit, expected_peak):
    ws_id = workspace_with_user["workspace"]
    counter = [0]
    peak = [0]

    def factory():
        return runs.gpu_semaphore(limit, workspace_id=ws_id, timeout=10, poll_seconds=0.005)

    slots = await asyncio.gather(*[_hold(factory, counter, peak) for _ in range(5)])
    assert peak[0] == expected_peak, f"expected at most {expected_peak} concurrent GPU slots"
    assert len(set(slots)) == 5, "each holder gets its own slot row"
    assert runs.held_gpu_slots(ws_id) == 0, "every slot must be released"


async def test_gpu_slots_are_released_when_the_body_raises(workspace_with_user):
    ws_id = workspace_with_user["workspace"]

    with pytest.raises(RuntimeError):
        async with runs.gpu_semaphore(1, workspace_id=ws_id, timeout=5):
            raise RuntimeError("provider blew up")
    assert runs.held_gpu_slots(ws_id) == 0


async def test_gpu_semaphore_times_out_instead_of_deadlocking(workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    # noqa below: the nested form IS the assertion -- the second acquisition must
    # happen while the single slot is still held, which a flat `with` cannot express
    with runs.gpu_semaphore_sync(1, workspace_id=ws_id, timeout=5):  # noqa: SIM117
        with pytest.raises(runs.GpuSlotTimeout) as excinfo:
            with runs.gpu_semaphore_sync(1, workspace_id=ws_id, timeout=0.2, poll_seconds=0.01):
                pass  # pragma: no cover - never reached
    assert "no GPU slot free" in str(excinfo.value)
    assert runs.held_gpu_slots(ws_id) == 0


def test_gpu_admission_is_per_workspace(workspace_with_user, tmp_path):
    from app.models import User, Workspace, WorkspaceMember

    other = Workspace(name="Other", slug="ws-other-intel", niche="AI money")
    db = None
    from app.db import session_scope

    with session_scope() as session:
        user = User(email="gpu@test.local", password_hash="x")
        session.add_all([other, user])
        session.flush()
        session.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                                    role=WorkspaceMember.ROLE_OWNER))
        db = other.id
    with runs.gpu_semaphore_sync(1, workspace_id=db, timeout=5), \
            runs.gpu_semaphore_sync(1, workspace_id=workspace_with_user["workspace"],
                                    timeout=5) as slot:
        # a different workspace is unaffected by the first one's single slot
        assert slot
    assert runs.held_gpu_slots(db) == 0


def test_complete_run_leaks_no_gpu_slot(db_session, workspace_with_user):
    """CPU-only accounting must never touch the semaphore."""
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row, gpu_ms=10, cost_micros=0)
    assert runs.held_gpu_slots(ws.id) == 0


# ---------------------------------------------------------------------------
# cost + events (never on a cache hit)
# ---------------------------------------------------------------------------


def test_cost_and_events_recorded_on_completion(db_session, workspace_with_user, monkeypatch):
    from app.services import cost as cost_service
    from app.services import events as events_service

    costs: list[dict] = []
    events: list[tuple] = []
    monkeypatch.setattr(
        cost_service, "track_cost",
        lambda workspace_id, category, amount, **kw: costs.append(
            {"ws": workspace_id, "category": category, "amount": amount, **kw}
        ),
    )
    monkeypatch.setattr(
        events_service, "record_event",
        lambda workspace_id, kind, message, **kw: events.append((kind, message)),
    )
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row, cost_micros=250)
    assert len(costs) == 1
    assert costs[0]["ws"] == ws.id
    assert costs[0]["category"] == "media_intel"
    assert costs[0]["amount"] == pytest.approx(0.00025)
    assert [e[0] for e in events] == [
        runs.EVENT_RUN_CREATED, runs.EVENT_RUN_STARTED, runs.EVENT_RUN_COMPLETED,
    ]


def test_a_cache_hit_records_neither_cost_nor_event(db_session, workspace_with_user, monkeypatch):
    from app.services import cost as cost_service
    from app.services import events as events_service

    costs: list = []
    events: list = []
    monkeypatch.setattr(cost_service, "track_cost", lambda *a, **kw: costs.append(a))
    monkeypatch.setattr(
        events_service, "record_event",
        lambda ws_id, kind, message, **kw: events.append((kind, message)),
    )
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row, cost_micros=250)
    events.clear()
    costs.clear()

    hit = _run(db_session, ws)
    assert hit["cache_hit"] is True
    assert costs == [], "a cache hit is not new work: no cost entry"
    assert events == [], "a cache hit is not new work: no activity event"


def test_zero_cost_run_records_no_cost_entry(db_session, workspace_with_user, monkeypatch):
    from app.services import cost as cost_service

    calls: list = []
    monkeypatch.setattr(cost_service, "track_cost", lambda *a, **kw: calls.append(a))
    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    row = runs.get_run(db_session, ws.id, dto["id"])
    runs.start_run(db_session, row)
    runs.complete_run(db_session, row, cost_micros=0)
    assert calls == []


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_get_run_is_workspace_scoped(db_session, workspace_with_user):
    from app.models import User, Workspace, WorkspaceMember

    ws = _ws(db_session, workspace_with_user)
    dto = _run(db_session, ws)
    other = Workspace(name="Other WS", slug="ws-intel-other", niche="AI money")
    user = User(email="iso@test.local", password_hash="x")
    db_session.add_all([other, user])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                                   role=WorkspaceMember.ROLE_OWNER))
    db_session.flush()

    assert runs.get_run(db_session, ws.id, dto["id"]) is not None
    assert runs.get_run(db_session, other.id, dto["id"]) is None
    assert runs.get_run(db_session, ws.id, "does-not-exist") is None


def test_list_runs_is_workspace_scoped_and_filterable(db_session, workspace_with_user):
    from app.models import User, Workspace, WorkspaceMember

    ws = _ws(db_session, workspace_with_user)
    _run(db_session, ws, kind="alignment")
    _run(db_session, ws, kind="diarization", provider="pyannote_diarization")
    other = Workspace(name="Other WS", slug="ws-intel-other-2", niche="AI money")
    user = User(email="iso2@test.local", password_hash="x")
    db_session.add_all([other, user])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                                   role=WorkspaceMember.ROLE_OWNER))
    db_session.flush()
    _run(db_session, other, kind="alignment")

    mine = runs.list_runs(db_session, ws.id)
    assert len(mine) == 2
    assert {item["workspace_id"] for item in mine} == {ws.id}
    assert len(runs.list_runs(db_session, other.id)) == 1
    assert len(runs.list_runs(db_session, ws.id, kind="alignment")) == 1
    assert runs.list_runs(db_session, ws.id, kind="nope") == []


# ---------------------------------------------------------------------------
# HTTP surface: routes + 404 isolation
# ---------------------------------------------------------------------------


def _register(client, email=None):
    import uuid

    email = email or f"mi{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.v1.media_intel import media_intel_router
    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    app = create_app()
    prefix = "/api/v1/workspaces/{workspace_id}/media-intel"
    if not any(p.startswith(prefix) for p in app.openapi()["paths"]):
        # the orchestrator registers the router at integration time; mount it
        # explicitly until it does, so the route contract is exercised either way
        app.include_router(media_intel_router)
    return TestClient(app, raise_server_exceptions=False)


def test_providers_route_reports_honest_availability(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/providers", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and body["items"]
    assert "alignment" in body["kinds"]
    for item in body["items"]:
        assert set(item["health"]) == {"available", "reason", "version", "mode", "detail"}
        assert set(item["license"]) >= {"code_license", "model_license", "commercial_use"}
    # CI installs no ML packages: only local ffmpeg/math providers may resolve,
    # and the advertised `available` list must match the items exactly.
    available = {item["key"] for item in body["items"] if item["health"]["available"]}
    assert not (available & {"whisperx_alignment", "pyannote_diarization",
                             "mediapipe_faces", "sam2_segmentation"}), available
    assert set(body["available"]) == available

    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/providers/whisperx_alignment",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["health"]["available"] is False
    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/providers/nope", headers=headers)
    assert r.status_code == 404, r.text


def test_runs_routes_and_foreign_id_is_404(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Workspace

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    with session_scope() as s:
        dto = _run(s, s.get(Workspace, ws_id))
        run_id = dto["id"]

    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/runs", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1

    r = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/runs/{run_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == run_id

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/runs/{run_id}/cancel",
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELLED"

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/runs/{run_id}/retry",
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING"

    # retrying a PENDING run is a conflict, not a silent no-op
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/runs/{run_id}/retry",
                    headers=headers)
    assert r.status_code == 409, r.text

    # a foreign workspace reads as 404, never 403
    other_ws, other_headers = _register(client)
    r = client.get(f"/api/v1/workspaces/{other_ws}/media-intel/runs/{run_id}",
                   headers=other_headers)
    assert r.status_code == 404, r.text
    r = client.post(f"/api/v1/workspaces/{other_ws}/media-intel/runs/{run_id}/cancel",
                    headers=other_headers)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def test_readiness_reports_media_intel_without_blocking_startup():
    from app.services import readiness as rd

    data = rd.run_readiness()
    ids = {c["id"] for c in data["checks"]}
    assert "media_intel" in ids
    check = next(c for c in data["checks"] if c["id"] == "media_intel")
    assert check["status"] in {"passed", "failed"}
    assert check["blocking"] is False, "no intel backend must never block production START"
    assert isinstance(check["latency_ms"], int)
    if check["status"] == "passed":
        assert check["remediation"] == ""


def test_readiness_media_intel_probe_is_directly_callable():
    from app.services import readiness as rd

    ok, detail, blocking, remediation = rd._check_media_intel()
    assert isinstance(ok, bool) and detail
    assert blocking is False
    assert isinstance(remediation, str)
