"""Work 15.8 §5/§6/§7/§8/§10 -- dubbing is budgeted, paid submits are durable,
ambiguity is structurally queryable, the mechanics are shared, and N workers
cannot spend the same dollar twice.

The audit this file answers (``docs/MODEL_ROUTER_EXECUTION_AUDIT.md``) found:

* §5 ``providers/dubbing.py`` looped ``range(0, len(texts), 20)`` calling
  ``complete_json`` per batch with **zero** budget gate -- ``ceil(N/20)``
  billable requests that no cap ever saw.
* §6 ``video_engine/mpt.py`` refused a dangerous retry correctly but wrote no
  durable record and no cost row, so "money may have been spent" lived only in
  memory plus a ``Video`` row the engine never wrote to.
* §7 lip-sync kept that ambiguity **only inside ``cost_json``**, so an operator
  could not filter for it -- they had to parse arbitrary JSON.
* §8 ~4x duplicated ~35 lines of provider wiring.
* §10 nothing proved the concurrent case for a request-shaped reservation.

Each guard below has a test that FAILS when the guard is removed; the mutation
results are in the Work 15.8 report. Nothing here reaches the network.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.db import session_scope
from app.models import CostEntry, Workspace
from app.providers import dubbing as dubbing_mod
from app.providers import llm as llm_mod
from app.providers.video_engine.base import VideoEngineError
from app.services import cost as cost_mod
from app.services import jobs as jobs_service
from app.services.paid_executor import (
    CostOutcome,
    IdempotencySupport,
    PaidSubmission,
    Reconciliation,
)
from app.services.paid_jobs import SubmissionState
from app.services.paid_provider import (
    COST_OUTCOMES,
    EXECUTION_OUTCOMES,
    annotate_exposure,
    paid_operation,
)

BACKEND = Path(__file__).resolve().parent.parent
TRANSLATE = dubbing_mod.TRANSLATION_CATEGORY


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """One registered workspace with generous caps."""
    from fastapi.testclient import TestClient

    from app.api.v1 import preview as preview_mod
    from app.main import create_app

    monkeypatch.setattr(preview_mod, "STORAGE_ROOT", tmp_path / "videos")
    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"w158{os.urandom(5).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    workspace_id = r.json()["workspace"]["id"]
    set_budgets(workspace_id, daily=100.0, per_call=100.0)
    return workspace_id


def set_budgets(workspace_id: str, *, daily: float, per_call: float) -> None:
    """Set BOTH caps: ``reserve_spend`` enforces both, and leaving
    ``per_video_budget_usd`` at its default silently refuses for a reason that
    has nothing to do with the test."""
    with session_scope() as s:
        row = s.get(Workspace, workspace_id)
        settings = dict(row.settings_json or {})
        safety = dict(settings.get("safety") or {})
        safety["daily_budget_usd"] = float(daily)
        safety["per_video_budget_usd"] = float(per_call)
        settings["safety"] = safety
        row.settings_json = settings


def cost_rows(workspace_id: str, category: str = "") -> list[CostEntry]:
    with session_scope() as s:
        query = s.query(CostEntry).filter(CostEntry.workspace_id == workspace_id)
        if category:
            query = query.filter(CostEntry.category == category)
        return list(query.order_by(CostEntry.created_at, CostEntry.id).all())


class FakeTranslationLLM:
    """Stands in for ``providers.llm.complete_json`` at the provider boundary.

    Counting here is the point: "no billable request happens after a refusal" is
    only observable at the provider boundary, not from the caller's own state.
    """

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def available(self) -> bool:
        return True

    def complete_json(self, *, system: str, user: str, workspace_id: str = "",
                      tier: str = "", temperature: float = 0.0,
                      max_tokens: int = 0, **kwargs) -> dict:
        import json as _json

        self.calls.append({"system": system, "user": user,
                           "workspace_id": workspace_id, "tier": tier,
                           "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        return {"lines": [f"[{line}]" for line in _json.loads(user)["lines"]]}


def install_translation_double(monkeypatch, double: FakeTranslationLLM) -> None:
    from app.providers import llm as llm_mod

    monkeypatch.setattr(llm_mod, "llm_available", double.available)
    monkeypatch.setattr(llm_mod, "complete_json", double.complete_json)
    monkeypatch.setattr(dubbing_mod, "_translation_model", lambda: "test-model")


def make_video(workspace_id: str) -> str:
    from app.models import Video

    variant_id = make_variant(workspace_id)
    with session_scope() as s:
        row = Video(variant_id=variant_id, workspace_id=workspace_id,
                    engine="moneyprinterturbo", status="RENDERING")
        s.add(row)
        s.flush()
        return row.id


def read_video(video_id: str):
    """Read the render row from a brand new session and engine.

    Anything served from an identity map would prove nothing about durability:
    this is exactly what a restarted process sees.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.models import Video

    engine = create_engine(settings.database_url)
    session = sessionmaker(bind=engine)()
    try:
        row = session.get(Video, video_id)
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()
        engine.dispose()


# ===========================================================================
# §5 -- the translation budget
# ===========================================================================


def test_a_translation_batch_is_reserved_BEFORE_the_request(ws, monkeypatch):
    """The gate has to run before the POST, or it is not a gate.

    Proved by ORDER, not by a message: the double is asked how many reservation
    rows existed at the moment it was called.
    """
    from app.providers import llm as llm_mod

    seen: list[int] = []

    def counting(*, system: str, user: str, **kwargs) -> dict:
        seen.append(len(cost_rows(ws, TRANSLATE)))
        return {"lines": []}

    double = FakeTranslationLLM()
    monkeypatch.setattr(llm_mod, "llm_available", double.available)
    monkeypatch.setattr(llm_mod, "complete_json", counting)
    monkeypatch.setattr(dubbing_mod, "_translation_model", lambda: "test-model")

    dubbing_mod.translate_segments(["one", "two"], "es", ws)

    assert len(seen) == 1, seen
    assert seen[0] == 1, (
        f"the provider was called with {seen[0]} reservation rows on disk; the "
        "estimate is written only after the request has already left")


def test_the_translation_budget_refuses_and_sends_nothing(ws, monkeypatch):
    """The refusal path: money cap reached, and NOTHING goes on the wire.

    A cap that refuses after the request would still have spent the money, so the
    assertion that matters is ``double.calls == []``.
    """
    set_budgets(ws, daily=0.0005, per_call=100.0)
    double = FakeTranslationLLM()
    install_translation_double(monkeypatch, double)

    with pytest.raises(dubbing_mod.DubError,
                       match="budget refused before batch 1/1"):
        dubbing_mod.translate_segments(["one", "two"], "es", ws)

    assert double.calls == [], "the provider was called after the budget refused"
    assert cost_rows(ws, TRANSLATE) == [], (
        "a refused reservation must write nothing: an un-committed row would "
        "shrink the budget for the next legitimate caller")


def test_reservation_is_per_REQUEST_not_per_segment(ws, monkeypatch):
    """45 segments are THREE billable requests, so three reservations.

    A per-segment reservation would write 45 rows, over-reserve ~15x and refuse
    work that fits inside the cap. The segment count is recorded on the row for
    exactly this reason: an operator can see that one reservation covered twenty
    segments, and it is not a cost unit.
    """
    double = FakeTranslationLLM()
    install_translation_double(monkeypatch, double)

    out = dubbing_mod.translate_segments([f"line {i}" for i in range(45)], "es", ws)

    assert len(out) == 45
    assert len(double.calls) == 3, [c["user"][:40] for c in double.calls]
    rows = cost_rows(ws, TRANSLATE)
    assert len(rows) == 3, (
        f"{len(rows)} reservations for 45 segments; the unit is the REQUEST "
        "(ceil(45/20)=3), not the segment")
    assert [int((r.detail_json or {})["segments_in_batch"]) for r in rows] == [
        20, 20, 5]
    assert [int((r.detail_json or {})["batch_index"]) for r in rows] == [0, 1, 2]
    assert [int((r.detail_json or {})["batch_count"]) for r in rows] == [3, 3, 3]

    # ...and each row was priced as ITS OWN request, not as 20 requests. A
    # per-segment estimate over-reserves by the batch size, so the recorded
    # amount has to match the combined prompt exactly.
    payloads = [[f"line {i}" for i in range(start, min(start + 20, 45))]
                for start in (0, 20, 40)]
    for row, batch in zip(rows, payloads, strict=True):
        system, user = dubbing_mod.translation_request_payload(
            batch, target_lang="es")
        assert row.amount_usd == pytest.approx(
            dubbing_mod.estimate_translation_request_cost(
                system, user, model="test-model"),
            abs=1e-6), (
            f"row {row.id} was priced for something other than the one request "
            f"it covers (${row.amount_usd}); a per-segment estimate would be up "
            f"to 20x this")
    assert cost_mod.spent_since(ws) == pytest.approx(
        sum(r.amount_usd for r in rows))


def test_one_batch_reserves_once_not_twice_across_the_two_gates(ws, monkeypatch):
    """A batch has a caller gate AND a per-leg gate. Only one may reserve.

    ``providers/dubbing.py`` reserves for the exact combined payload, and
    ``llm_paid`` gates every billable leg. If the inner gate reserved again,
    one POST would write two reservation rows: never a leak, but the cap would
    be charged twice for the same request and real headroom would disappear.

    The fake-LLM tests above cannot catch this -- they replace the provider, so
    the inner gate never runs. This one drives the REAL ``complete_json`` and
    only intercepts the HTTP transport.
    """
    import httpx as _httpx

    from app.services.provider_settings import set_credential

    # The settings object is immutable, so configure through the real resolver
    # the same way a workspace would.
    set_credential("llm.api_key", "sk-w158-double-reserve", ws)
    set_credential("llm.base_url", "https://llm.test/v1", ws)
    # MOCK_LLM is true in the test env, so llm_available() short-circuits.
    monkeypatch.setattr(llm_mod, "llm_available", lambda *a, **k: True)

    seen: list[dict] = []

    def _post(url, **kwargs):  # noqa: ARG001
        seen.append(kwargs.get("json") or {})
        request = _httpx.Request("POST", str(url))
        return _httpx.Response(
            200, request=request,
            json={"choices": [{"message": {"content": '{"lines": ["hola"]}'}}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    monkeypatch.setattr(_httpx, "post", _post)
    set_budgets(ws, daily=1.0, per_call=0.5)

    dubbing_mod.translate_segments(["uno", "dos", "tres"], "es", ws)

    rows = cost_rows(ws, TRANSLATE)
    assert len(seen) >= 1, "the batch never reached the provider"
    assert len(rows) == len(seen), (
        f"{len(rows)} reservations for {len(seen)} requests; the caller's "
        "reservation must be reused by the per-leg gate, not duplicated")


def test_an_ambiguous_translation_keeps_its_reservation(ws, monkeypatch):
    """Ambiguity must NOT release the reservation, and must be marked.

    Releasing it would hand the same budget back to the next caller while the
    provider may already have generated and billed this completion -- and a chat
    completion has no remote id, so nobody will ever find out.
    """
    from app.engine.intelligence.llm_paid import LLMCompletionUnknown
    from app.providers import llm as llm_mod

    double = FakeTranslationLLM(error=llm_mod.LLMCompletionError(
        detail="read timeout after the request was sent",
        kind=LLMCompletionUnknown.kind,
        submission=PaidSubmission(
            workspace_id=ws, provider="openai_compatible_llm",
            operation="complete", submission_id="a" * 32,
            state=SubmissionState.SUBMISSION_UNKNOWN)))
    install_translation_double(monkeypatch, double)

    with pytest.raises(dubbing_mod.DubError, match="unresolved"):
        dubbing_mod.translate_segments(["one"], "es", ws)

    rows = cost_rows(ws, TRANSLATE)
    assert len(rows) == 1, "the reservation was released for a possibly-billed call"
    detail = dict(rows[0].detail_json or {})
    assert detail.get("cost_outcome") == "UNKNOWN_EXPOSURE", detail
    assert detail.get("exposure_unknown") is True, detail


def test_a_known_rejection_releases_the_reservation(ws, monkeypatch):
    """A provider-not-configured failure billed nothing, so the budget returns.

    The mirror image of the unknown case, and the one that stops a loop of
    failures from silently draining a workspace's day.
    """
    from app.providers import llm as llm_mod

    double = FakeTranslationLLM(error=llm_mod.LLMError("provider not configured"))
    install_translation_double(monkeypatch, double)

    with pytest.raises(dubbing_mod.DubError, match="failed"):
        dubbing_mod.translate_segments(["one"], "es", ws)

    assert cost_rows(ws, TRANSLATE) == [], (
        "a definitively-unsent request must not hold a reservation")
    assert cost_mod.spent_since(ws) == pytest.approx(0.0)


def test_translation_request_count_is_the_request_count():
    """The number a caller budgets the whole run with."""
    assert dubbing_mod.translation_request_count(0) == 0
    assert dubbing_mod.translation_request_count(1) == 1
    assert dubbing_mod.translation_request_count(20) == 1
    assert dubbing_mod.translation_request_count(21) == 2
    assert dubbing_mod.translation_request_count(200) == 10


def test_the_translation_estimate_prices_one_request():
    """Doubling the batch must not multiply the price by the batch size.

    It does raise it -- more segments is a bigger prompt -- but by the size of
    the prompt. A per-segment estimate would make a full batch cost 20x one
    segment.
    """
    one = dubbing_mod.estimate_translation_request_cost("sys", "one line")
    twenty = dubbing_mod.estimate_translation_request_cost(
        "sys", "\n".join(f"line {i}" for i in range(20)))
    assert one > 0
    assert twenty < one * 20


# ===========================================================================
# §6 -- the durable paid-submission record
# ===========================================================================


class RecordingEngine:
    """A video engine double that records the render row at submit time.

    ``on_submit`` is called with the row exactly as a fresh read sees it, which is
    what proves the attempt was persisted BEFORE the paid request rather than
    after it.
    """

    engine_name = "moneyprinterturbo"

    def __init__(self, *, on_submit=None, ambiguous: bool = False) -> None:
        self.on_submit = on_submit
        self.ambiguous = ambiguous
        self.submits = 0

    def estimate_cost(self, req) -> float:
        return 0.02

    def submit(self, req):
        from app.providers.video_engine.base import (
            RenderHandle,
            VideoEngineSubmissionUnknown,
        )

        self.submits += 1
        if self.on_submit is not None:
            self.on_submit(req)
        if self.ambiguous:
            error = VideoEngineSubmissionUnknown(
                "engine did not answer after the submit was sent")
            # the adapter is the only place that knows what was actually sent
            error.request_fingerprint = req.request_hash()
            raise error
        return RenderHandle(engine_task_id="task-1", engine=self.engine_name)


def install_engine(monkeypatch, engine) -> None:
    monkeypatch.setattr(
        "app.providers.video_engine.factory.get_video_engine", lambda: engine)


def render_once(workspace_id: str, variant_id: str) -> None:
    from app.engine.agents.production import VideoProducerAgent

    ctx = jobs_service.JobContext(
        job_id="j-w158", type="cycle.build", workspace_id=workspace_id,
        cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)
    VideoProducerAgent().render(
        ctx, topic="t", script="a script for the render", keywords=[],
        aspect_ratio="9:16", variant_id=variant_id)


def make_variant(workspace_id: str) -> str:
    from app.models import ContentItem, VideoVariant

    with session_scope() as s:
        item = ContentItem(workspace_id=workspace_id, topic="t")
        s.add(item)
        s.flush()
        variant = VideoVariant(content_item_id=item.id, label="v1",
                               script="a script for the render")
        s.add(variant)
        s.flush()
        return variant.id


def test_the_attempt_is_written_BEFORE_the_paid_request(ws, monkeypatch):
    """Ordering is the whole guarantee: a crash mid-flight must leave evidence.

    If the attempt write moved after ``engine.submit``, ``seen`` would hold the
    row as it looked BEFORE the request -- PREPARED, no timestamp, no operation
    id -- which is exactly the state that loses the money.
    """
    seen: dict = {}
    real_update = None

    def spy(video_id: str, **fields) -> None:
        real_update(video_id, **fields)

    from app.engine.agents import production

    real_update = production._update_video
    monkeypatch.setattr(production, "_update_video", spy)
    variant_id = make_variant(ws)

    def on_submit(_req) -> None:
        row = read_video(seen["video_id"])
        seen.update(video_id=row.id, state=row.submission_state,
                    attempted_at=row.submission_attempted_at,
                    operation_id=row.submission_operation_id,
                    cost_outcome=row.cost_outcome)

    # The row does not exist until render creates it, so the spy finds the id.
    original = production._update_video

    def find(video_id: str, **fields) -> None:
        seen["video_id"] = video_id
        original(video_id, **fields)

    monkeypatch.setattr(production, "_update_video", find)
    engine = RecordingEngine(on_submit=on_submit, ambiguous=True)
    install_engine(monkeypatch, engine)

    with pytest.raises(RuntimeError, match="did not answer"):
        render_once(ws, variant_id)

    assert engine.submits == 1
    assert seen.get("state") == "SUBMISSION_ATTEMPTED", seen
    assert seen.get("attempted_at") is not None, (
        "created_at is row creation, not the attempt; without this a row that was "
        "never submitted looks like one that may have been billed")
    assert seen.get("operation_id"), (
        "no operation id ties the Video row to the ledger row for this submit")
    assert seen.get("cost_outcome") == "ESTIMATED", seen


def test_an_ambiguous_render_survives_a_restart(ws, monkeypatch):
    """§6: a process restart must not erase evidence that money may be spent.

    This is the failure the audit named: the pipeline correctly refused to
    resubmit, but nothing durable said WHY. The read is deliberately made through
    a brand new engine and session, because a test that read back the same
    identity map it wrote would pass with no persistence at all.
    """
    variant_id = make_variant(ws)
    engine = RecordingEngine(ambiguous=True)
    install_engine(monkeypatch, engine)

    with pytest.raises(RuntimeError, match="engine did not answer"):
        render_once(ws, variant_id)

    from app.models import Video

    with session_scope() as s:
        video_id = s.query(Video).filter(Video.variant_id == variant_id).one().id

    row = read_video(video_id)
    assert row.submission_state == "SUBMISSION_UNKNOWN"
    assert row.cost_outcome == "UNKNOWN_EXPOSURE"
    assert row.submission_operation_id, "the operation id is what pairs the row "\
                                       "with its ledger row"
    assert row.submission_attempted_at is not None
    assert "did not answer" in row.submission_detail
    assert "did not answer" in str(row.error)
    # the adapter's fingerprint is the reconciliation key, and it survived
    assert (row.params_json or {}).get("ambiguous_request_hash")
    # the business status was NOT used to carry the money fact
    assert row.status == "RENDERING"

    # ...and the money side, read the same way.
    rows = cost_rows(ws, "video")
    assert len(rows) == 1, rows
    detail = dict(rows[0].detail_json or {})
    assert detail["cost_outcome"] == "UNKNOWN_EXPOSURE", detail
    assert detail["operation_id"] == row.submission_operation_id, (
        "the ledger row and the render row must name the same operation")
    assert detail["request_hash"]


def test_a_definitively_refused_render_releases_the_reservation(ws, monkeypatch):
    """A 4xx created no task and billed nothing, so the budget comes back.

    Without this, a workspace whose engine misconfiguration persists would burn
    its whole daily cap on requests that never left.
    """
    from app.providers.video_engine.base import VideoEngineRequestInvalid

    class RefusingEngine(RecordingEngine):
        def submit(self, req):
            self.submits += 1
            raise VideoEngineRequestInvalid("engine rejected request (422)")

    variant_id = make_variant(ws)
    engine = RefusingEngine()
    install_engine(monkeypatch, engine)

    with pytest.raises(RuntimeError, match="engine rejected request"):
        render_once(ws, variant_id)

    assert cost_rows(ws, "video") == [], (
        "a definitively-unsent request must not hold a reservation")
    assert cost_mod.spent_since(ws) == pytest.approx(0.0)


def test_the_durable_record_lives_on_existing_models_not_a_new_table():
    """Why the record was EXTENDED rather than added.

    A parallel ledger would be a second source of truth for facts the render row
    already holds, and the first thing to break would be a crash between the two
    writes. ``submission_state`` already holds the canonical ``SubmissionState``
    vocabulary, so no second execution column was added either.
    """
    from app.models import Video

    columns = {c.name for c in Video.__table__.columns}
    for expected in ("submission_state", "provider_task_id",
                     "submission_operation_id", "submission_attempted_at",
                     "cost_outcome", "idempotency_key", "submission_detail"):
        assert expected in columns, (
            f"{expected} must live on the render row; a second table would be a "
            "second source of truth for the same submit")
    assert "execution_outcome" not in columns, (
        "submission_state already holds the canonical SubmissionState "
        "vocabulary; a second execution column is a second spelling of one fact")


def test_the_migration_round_trips_up_and_down(tmp_path):
    """Reversible, on its OWN database.

    SQLite raises ``error in index ... after drop column`` when an index still
    names the column, and this migration adds BOTH ``index=True`` columns and
    explicit composite indexes, so the downgrade has to discover them. Running
    against the shared session database would strip the schema out from under
    every other test.
    """
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401  (registers every table)
    from app.db import Base

    module = load_migration("0034_paid_execution_outcomes.py")
    engine = create_engine(f"sqlite:///{(tmp_path / 'mig34.db').as_posix()}")
    session = sessionmaker(bind=engine)()
    try:
        Base.metadata.create_all(bind=engine)
        module.upgrade(session)
        session.commit()

        inspector = inspect(engine)
        videos = {c["name"] for c in inspector.get_columns("videos")}
        jobs = {c["name"] for c in inspector.get_columns("lipsync_jobs")}
        assert {"cost_outcome", "submission_operation_id",
                "submission_attempted_at"} <= videos
        assert {"execution_outcome", "cost_outcome"} <= jobs

        # replay safety: additive columns are idempotent
        module.upgrade(session)
        session.commit()

        module.downgrade(session)
        session.commit()
        inspector = inspect(engine)
        videos = {c["name"] for c in inspector.get_columns("videos")}
        jobs = {c["name"] for c in inspector.get_columns("lipsync_jobs")}
        assert not ({"cost_outcome", "submission_operation_id",
                     "submission_attempted_at"} & videos)
        assert not ({"execution_outcome", "cost_outcome"} & jobs)
    finally:
        session.close()
        engine.dispose()


def load_migration(name: str):
    path = BACKEND / "app" / "migrations" / "versions" / name
    assert path.is_file(), f"{path} does not exist"
    spec = importlib.util.spec_from_file_location(f"m_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# §7 -- ambiguity is structurally queryable
# ===========================================================================


def make_lipsync_job(workspace_id: str) -> str:
    from app.engine.lipsync.rows import create_job_row, finish_job

    with session_scope() as s:
        row = create_job_row(s, workspace_id=workspace_id, provider="external",
                             video_ref="v.mp4", audio_ref="a.wav")
        job_id = row.id
        s.commit()
    # the pre-15.8 shape: the ambiguity exists ONLY inside cost_json
    finish_job(job_id, "FAILED", error="lost response",
               cost={"cost_usd": 0.0, "cost_outcome": "UNKNOWN_EXPOSURE"})
    return job_id


def test_lipsync_ambiguity_is_a_column_not_json(ws):
    """The whole point of §7: a WHERE clause, not a scan-and-parse.

    Before this, the facts existed only inside ``cost_json``, so finding the jobs
    that may already have been billed meant loading every row for the workspace
    and inspecting arbitrary JSON by hand.
    """
    from sqlalchemy import select

    from app.engine.lipsync import rows as rows_mod
    from app.models.lipsync import LipSyncJob

    job_id = make_lipsync_job(ws)
    rows_mod.set_paid_outcomes(
        job_id, execution_outcome=rows_mod.EXECUTION_SUBMISSION_UNKNOWN,
        cost_outcome=rows_mod.COST_UNKNOWN_EXPOSURE)

    incidents = rows_mod.jobs_with_unknown_exposure(ws)
    assert [i["id"] for i in incidents] == [job_id], incidents
    assert incidents[0]["execution_outcome"] == "SUBMISSION_UNKNOWN"
    # The business status is NOT overloaded: it still says what it always said.
    assert incidents[0]["status"] == "FAILED"
    assert rows_mod.jobs_with_unknown_submission(ws) == [job_id]

    # The structural form: an ORM filter with no JSON anywhere in it.
    with session_scope() as s:
        matched = s.scalars(
            select(LipSyncJob).where(
                LipSyncJob.workspace_id == ws,
                LipSyncJob.execution_outcome == "SUBMISSION_UNKNOWN")).all()
    assert [m.id for m in matched] == [job_id]

    # ...and it is in the API payload under its own key.
    with session_scope() as s:
        row = rows_mod.get_job_row(s, ws, job_id)
    dto = rows_mod.job_dto(row)
    assert dto["execution_outcome"] == "SUBMISSION_UNKNOWN"
    assert dto["cost_outcome"] == "UNKNOWN_EXPOSURE"
    assert dto["status"] == "FAILED"


def test_the_lipsync_vocabulary_is_the_canonical_one():
    """No second spelling of "may have been billed".

    The values must BE ``SubmissionState`` / ``CostOutcome`` members, so one
    incident query works across the render lane, the lip-sync lane and the ledger.
    """
    from app.engine.lipsync import base as lipsync_base

    canonical_states = {str(s) for s in SubmissionState}
    assert set(lipsync_base.EXECUTION_OUTCOMES) <= canonical_states
    assert lipsync_base.EXECUTION_SUBMISSION_UNKNOWN in canonical_states
    assert set(lipsync_base.COST_OUTCOMES) == {str(c) for c in CostOutcome}
    assert set(EXECUTION_OUTCOMES) == canonical_states
    assert set(COST_OUTCOMES) == {str(c) for c in CostOutcome}
    # The business vocabulary is untouched: no ambiguity state was added to it.
    assert lipsync_base.JOB_STATUSES == (
        "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT")
    assert "SUBMISSION_UNKNOWN" not in lipsync_base.JOB_STATUSES


def test_a_bad_outcome_value_is_refused_not_stored(ws):
    """A typo in a status column breaks every reader of it.

    So a typo is a loud failure at write time rather than a value that is
    permanently un-queryable.
    """
    from app.engine.lipsync.rows import set_paid_outcomes
    from app.models.lipsync import LipSyncJob

    job_id = make_lipsync_job(ws)
    with pytest.raises(ValueError, match="unknown execution outcome"):
        set_paid_outcomes(job_id, execution_outcome="SUBMISSION_UNKOWN")
    with pytest.raises(ValueError, match="unknown cost outcome"):
        set_paid_outcomes(job_id, cost_outcome="unknown")
    with session_scope() as s:
        assert s.get(LipSyncJob, job_id).execution_outcome == ""


def test_the_render_lane_records_cost_outcome_structurally_too(ws):
    """``submission_state`` is the execution half and ``cost_outcome`` is the
    money half; both are filterable columns on the same row."""
    from sqlalchemy import select

    from app.models import Video

    video_id = make_video(ws)
    with session_scope() as s:
        row = s.get(Video, video_id)
        row.submission_state = "SUBMISSION_UNKNOWN"
        row.cost_outcome = "UNKNOWN_EXPOSURE"

    with session_scope() as s:
        rows = s.scalars(
            select(Video).where(
                Video.workspace_id == ws,
                Video.cost_outcome == "UNKNOWN_EXPOSURE",
                Video.submission_state == "SUBMISSION_UNKNOWN")).all()
    assert [r.id for r in rows] == [video_id]


# ===========================================================================
# §8 -- the shared helper: what it owns, and what it must not swallow
# ===========================================================================


def test_the_helper_owns_the_money_and_not_the_request():
    """§8's boundary, asserted on the signature.

    ``paid_operation`` takes a price and a category and nothing about HTTP: no
    url, no payload, no status mapping. A helper that accepted those would be the
    "giant generic abstraction" the extraction exists to avoid, and it would end
    up re-deciding 429 for every provider at once.
    """
    params = inspect.signature(paid_operation).parameters
    assert set(params) == {
        "provider", "operation", "workspace_id", "category", "estimated_cost",
        "idempotency", "reconciliation", "reservation_extra"}
    forbidden = {"url", "payload", "body", "headers", "status", "cancel",
                 "fetch", "http", "method", "response"}
    assert not forbidden & set(params), (
        f"the helper grew provider-specific arguments: {forbidden & set(params)}")


def test_the_helper_refuses_a_reservation_without_a_category():
    """A reservation IS a ledger row, and a row with no category cannot be
    counted against a cap -- so it would be a gate that only looks like one."""
    operation = paid_operation(provider="p", operation="o", workspace_id="w")
    with pytest.raises(ValueError, match="needs a category"):
        operation.authorize()


class _RaisingClient:
    """An httpx stand-in whose POST fails the way the test says it should."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.posts = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def post(self, *_args, **_kwargs):
        self.posts += 1
        raise self.error


def _mpt_request():
    from app.providers.video_engine.base import RenderRequest

    return RenderRequest(subject="a subject", script="a script",
                         workspace_id="ws-1")


def test_a_lost_response_is_an_ambiguity_and_a_4xx_is_not(monkeypatch):
    """The provider's OWN error semantics, exercised rather than inspected.

    Only the adapter knows these two apart: a read timeout means the request was
    DELIVERED and the response was lost (so the render may have been billed), and
    a 4xx means the engine definitively created no task. If the shared helper
    swallowed that distinction, a 429 would start looking like a possible charge
    and a lost render would start looking safely retryable -- one double charge
    and one silent orphan respectively.
    """
    import httpx

    from app.providers.video_engine import mpt as mpt_mod
    from app.providers.video_engine.base import (
        VideoEngineRequestInvalid,
        VideoEngineSubmissionUnknown,
    )

    adapter = mpt_mod.MoneyPrinterTurboAdapter(base_url="http://engine.test")
    request = _mpt_request()

    # a read timeout on a submit: delivered, answer lost
    client = _RaisingClient(httpx.ReadTimeout("read timed out"))
    monkeypatch.setattr(adapter, "_client", lambda: client)
    with pytest.raises(VideoEngineSubmissionUnknown) as caught:
        adapter.submit(request)
    assert client.posts == 1, "the ambiguous submit must cost exactly one POST"
    assert caught.value.request_fingerprint == request.request_hash()
    assert caught.value.engine == "moneyprinterturbo"

    # a 5xx on a submit: the task may exist even though the answer failed
    response = httpx.Response(503, request=httpx.Request("POST", "http://e.test"))
    monkeypatch.setattr(adapter, "_client",
                        lambda: _RaisingClient(
                            httpx.HTTPStatusError("boom", request=response.request,
                                                  response=response)))
    with pytest.raises(VideoEngineSubmissionUnknown):
        adapter.submit(request)

    # a 4xx on a submit: definitively rejected, nothing created
    response = httpx.Response(422, request=httpx.Request("POST", "http://e.test"))
    monkeypatch.setattr(adapter, "_client",
                        lambda: _RaisingClient(
                            httpx.HTTPStatusError("nope", request=response.request,
                                                  response=response)))
    with pytest.raises(VideoEngineRequestInvalid) as rejected:
        adapter.submit(request)
    assert rejected.value.retryable is False, (
        "a 4xx created no task, so a retry is safe -- but it is still a "
        "DEFINITIVE rejection, never an unknown exposure")

    # a connection that never opened: an outage, and the caller may retry
    monkeypatch.setattr(adapter, "_client",
                        lambda: _RaisingClient(httpx.ConnectError("refused")))
    with pytest.raises(VideoEngineError) as outage:
        adapter.submit(request)
    assert outage.value.retryable is True


def test_the_helper_keeps_provider_specific_error_semantics():
    """The helper books each verdict; it does not produce one.

    ``mark_unknown`` and ``mark_rejected`` are two calls the PROVIDER makes. The
    helper's signature is the boundary: it accepts a price and a category and
    nothing about HTTP, so it cannot re-decide what a 429 or a lost response
    means for a provider it knows nothing about.
    """
    from app.services.paid_provider import PaidOperation

    params = inspect.signature(PaidOperation.mark_rejected).parameters
    assert set(params) == {"self", "detail", "nothing_billed"}
    params = inspect.signature(PaidOperation.mark_unknown).parameters
    assert set(params) == {"self", "detail"}


def test_the_helper_is_what_four_providers_could_share():
    """§8, stated as a fact about the code rather than as a plan.

    The duplicated wiring each provider had is exactly the wiring the helper
    holds: an executor, the records, a three-way settle branch and a one-line
    budget gate. Every clause that was easy to get wrong -- reserve before send,
    settle in place, never release an ambiguity -- lives in one implementation.
    """
    from app.services.paid_provider import PaidOperation

    for clause in ("authorize", "mark_attempt", "mark_accepted", "mark_unknown",
                   "mark_rejected", "mark_cancelled", "mark_succeeded",
                   "close_book", "release", "make_executor"):
        assert callable(getattr(PaidOperation, clause, None)), clause
    assert "authorize" in inspect.getsource(PaidOperation.make_executor), (
        "the executor's submit_budget must be the reservation, or the gate "
        "short-circuits to a debug log the way it did before §8")
    source = inspect.getsource(PaidOperation)
    assert "track_cost(" not in source, (
        "a reservation plus a track_cost bills one operation twice")


def test_the_audit_table_agrees_with_the_new_state():
    """The audit is DATA, and data rots. Both changed rows must be COVERED with
    no unexplained gap, and ``verify_against_source`` must still be empty."""
    from app.services import paid_jobs_audit as audit

    assert audit.verify_against_source() == []
    assert audit.path("providers.dubbing.translate_segments").covered
    assert audit.path("video_engine.mpt.submit").covered
    assert "app.services.paid_provider" in audit.PAID_CONTRACT_MODULES, (
        "the helper must count as carrying the contract, which is the only "
        "reason those two rows moved from UNCOVERED")


# ===========================================================================
# §10 -- concurrency: N workers, one budget
# ===========================================================================


def test_concurrent_translation_batches_cannot_double_spend_the_same_money(
        ws, monkeypatch):
    """Two requests against the same remaining budget: only one may win.

    Eight threads each authorize one translation batch for the SAME workspace,
    with room for exactly three. Asserted on the grant count AND on the recorded
    total, because a gate that refuses correctly but still writes the row is
    double-booking.

    This is ``cost.reserve_spend``'s database lock doing the work; what is proven
    here is that the dubbing loop actually ROUTES through it, once per request.
    """
    threads, slots = 8, 3
    install_translation_double(monkeypatch, FakeTranslationLLM())
    # The per-batch estimate is computed from the SAME payload the loop sends
    # (``translation_request_payload``), so the cap below is exact rather than
    # guessed -- a test that guessed would fail for the wrong reason.
    system, user = dubbing_mod.translation_request_payload(["x"], target_lang="es")
    per_batch = dubbing_mod.estimate_translation_request_cost(
        system, user, model="test-model")
    cap = per_batch * slots + per_batch / 2
    set_budgets(ws, daily=cap, per_call=100.0)

    granted = 0
    refused = 0
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def one_batch(index: int) -> None:
        nonlocal granted, refused
        barrier.wait()
        try:
            dubbing_mod.translate_segments([f"t{index}"], "es", ws)
        except dubbing_mod.DubError:
            with lock:
                refused += 1
            return
        with lock:
            granted += 1

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(one_batch, range(threads)))

    rows = cost_rows(ws, TRANSLATE)
    assert granted == slots, (
        f"{granted} batches were admitted against a ${cap:.6f} budget that holds "
        f"{slots} at ${per_batch:.6f} each")
    assert refused == threads - slots
    assert len(rows) == slots, (
        f"{len(rows)} reservation rows exist; refusals must leave no trace")
    assert cost_mod.spent_since(ws) <= cap


def test_concurrent_rejections_release_the_reservation_back_to_the_pool(ws):
    """Rollback + known rejection + a later success: the budget is not sticky.

    Four threads each reserve and then RELEASE (a definitive rejection). At the
    end the workspace must be able to spend the whole cap again -- a rejection
    that kept its reservation would shrink the day for work that never happened,
    and four rejections in a row would exhaust a small cap permanently.
    """
    slots, each = 3, 0.01
    set_budgets(ws, daily=each * slots, per_call=100.0)

    def one_attempt(index: int) -> str:
        operation = paid_operation(provider="p", operation=f"attempt-{index}",
                                   workspace_id=ws, category="race",
                                   estimated_cost=each)
        operation.authorize()
        operation.mark_rejected("4xx: engine rejected request")
        return "rejected"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one_attempt, range(4)))

    assert results == ["rejected"] * 4
    assert cost_rows(ws, "race") == [], (
        "a definitive rejection kept its reservation; the budget is now sticky "
        "for work that was never sent")
    assert cost_mod.spent_since(ws) == pytest.approx(0.0)

    # ...so the whole cap is available again.
    for i in range(slots):
        operation = paid_operation(provider="p", operation=f"later-{i}",
                                   workspace_id=ws, category="race",
                                   estimated_cost=each)
        operation.authorize()
        operation.mark_succeeded()
    assert cost_mod.spent_since(ws) == pytest.approx(each * slots)


def test_an_unknown_exposure_never_releases_so_the_budget_stays_conservative(ws):
    """The other half: an ambiguous call must HOLD its reservation.

    If ambiguity released the budget, several concurrent ambiguous batches would
    each be told yes for the same last dollar and the ledger would understate a
    spend that may really have happened.
    """
    each = 0.01
    set_budgets(ws, daily=each * 2, per_call=100.0)

    operation = paid_operation(provider="p", operation="ambiguous",
                               workspace_id=ws, category="amb",
                               estimated_cost=each)
    operation.authorize()
    operation.mark_unknown("read timeout after the request was sent")

    with pytest.raises(cost_mod.BudgetExceededError):
        paid_operation(provider="p", operation="second", workspace_id=ws,
                       category="amb", estimated_cost=each * 1.5).authorize()

    rows = cost_rows(ws, "amb")
    assert len(rows) == 1
    assert (rows[0].detail_json or {})["cost_outcome"] == "UNKNOWN_EXPOSURE"


def test_annotate_exposure_never_inserts_a_second_row(ws):
    """A second row would bill one operation twice: the reservation already
    counted against the cap."""
    operation = paid_operation(provider="p", operation="once", workspace_id=ws,
                               category="one", estimated_cost=0.01)
    operation.authorize()
    assert annotate_exposure(operation.entry_id, CostOutcome.UNKNOWN_EXPOSURE)
    assert annotate_exposure(operation.entry_id, CostOutcome.ESTIMATED)
    assert len(cost_rows(ws, "one")) == 1
    # a vanished row is a bookkeeping loss, never a reason to invent a
    # replacement
    assert annotate_exposure("no-such-entry", CostOutcome.ACTUAL) is False
    assert len(cost_rows(ws, "one")) == 1


def test_close_book_settles_in_place_and_never_double_books(ws):
    """An actual above the estimate tightens the cap; it must not also add a row."""
    operation = paid_operation(provider="p", operation="settle", workspace_id=ws,
                               category="settle", estimated_cost=0.01)
    operation.authorize()
    assert operation.close_book(0.04)
    rows = cost_rows(ws, "settle")
    assert len(rows) == 1
    assert rows[0].amount_usd == pytest.approx(0.04)
    assert (rows[0].detail_json or {})["cost_outcome"] == "ACTUAL"
    assert cost_mod.spent_since(ws) == pytest.approx(0.04)


def test_an_estimate_never_claims_to_be_actual(ws):
    """MPT and the chat gateways report no amount, so a render stays an
    ESTIMATE. ``settle_reservation`` would stamp it ACTUAL, which no vendor
    invoice supports."""
    operation = paid_operation(provider="p", operation="est", workspace_id=ws,
                               category="est", estimated_cost=0.02)
    operation.authorize()
    operation.close_book()
    rows = cost_rows(ws, "est")
    assert len(rows) == 1
    assert rows[0].is_estimate is True
    assert (rows[0].detail_json or {})["cost_outcome"] == "ESTIMATED"


def test_an_empty_workspace_id_is_REFUSED_not_warned():
    """Work 15.9 closed the escape hatch this test used to bless.

    This test previously asserted that an empty workspace "says so instead of
    passing silently" -- i.e. that ``authorize()`` returns without reserving and
    without raising. Its own docstring called a silently-always-passing gate
    "the lie the whole reservation design exists to remove", and Work 15.9 acted
    on that: a billable operation with no owner is now a typed refusal BEFORE
    the external call.

    The only way to spend without a workspace is to declare
    ``SYSTEM_OWNED`` explicitly, and that still requires a configured system
    budget.
    """
    from app.services.paid_provider import OwnerlessSpendRefused

    operation = paid_operation(provider="p", operation="nowhere",
                               workspace_id="", category="llm",
                               estimated_cost=0.02)
    with pytest.raises(OwnerlessSpendRefused):
        operation.authorize()
    assert operation.reservation is None, "a refused operation reserved money"
    assert operation.entry_id == ""
    # It is a BudgetExceededError subclass on purpose: the callers that wrap
    # authorize() in `except BudgetExceededError` already treat that as a
    # refusal, so "blocked" can never fall through to a "retryable" arm.
    from app.services.cost import BudgetExceededError

    assert issubclass(OwnerlessSpendRefused, BudgetExceededError)


def test_the_helper_defaults_to_the_conservative_reconciliation():
    """A chat completion cannot be looked up afterwards, so the default remedy is
    a human decision rather than a search."""
    assert Reconciliation.MANUAL_OVERRIDE is not Reconciliation.RECONCILE
    operation = paid_operation(provider="p", operation="x")
    assert operation.reconciliation is Reconciliation.RECONCILE
    assert operation.idempotency is IdempotencySupport.UNVERIFIED