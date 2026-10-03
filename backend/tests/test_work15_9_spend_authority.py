"""Work 15.9 §1/§2/§8/§12 -- every billable operation has exactly one owner,
and the localization lane stops losing it.

Two defects, both real money and both invisible:

* ``paid_provider.PaidOperation.authorize`` logged a warning and returned
  WITHOUT reserving when ``workspace_id`` was empty. The request still went
  out. ``llm_paid.SpendAuthorization`` did the same with ``enforced = False``.
  So "no budget" was silently "no limit": localization translation
  (``engine/localization/pipeline.py:351`` hardcoded ``workspace_id=""`` while
  every other stage in that file passed ``self.row.workspace_id``) and every
  ownerless LLM leg were billable requests no cap could see.
* ``SYSTEM`` was not an authority, it was a synonym for "workspace missing".
  There was no way to say "YMONEY's own money" and no way to say "nobody's
  money, and here is the proof".

What this file locks down:

* :class:`OwnerlessSpendRefused` -- the typed refusal, raised BEFORE the
  external call, subclass of ``BudgetExceededError`` so every existing handler
  already treats it as "nothing was sent".
* ``SYSTEM_OWNED`` requires an explicitly configured system budget
  (``YMONEY_SYSTEM_BUDGET_USD``). With none configured it blocks, exactly like
  an ownerless call.
* Localization propagates the canonical row's workspace through EVERY billable
  leg (cues, graphics, metadata, TTS), and no stage passes ``""``.
* Workspace A cannot consume workspace B's budget through that path.
* Five restart crash points, each read back through a FRESH engine and session,
  because an ORM identity-map read passes with no persistence at all.

No migration was needed: the authority picture rides on ``cost_entries.detail_json``
alongside the operation id that was already there. No new dependency.

Mutation results are in the Work 15.9 report.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.db import session_scope
from app.models import CostEntry, Workspace
from app.models.localization import LocalizedContent
from app.providers import dubbing as dubbing_mod
from app.services import cost as cost_mod
from app.services.paid_provider import (
    ACTOR_AUTHORITIES,
    SPEND_AUTHORITIES,
    SYSTEM_BUDGET_ENV,
    SYSTEM_LEDGER_OWNER,
    ActorAuthority,
    OwnerlessSpendRefused,
    SpendAuthority,
    coerce_authority,
    paid_operation,
    resolve_ownership,
    system_budget_usd,
)

TRANSLATE = dubbing_mod.TRANSLATION_CATEGORY


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


def _make_workspace(*, daily: float = 100.0, per_call: float = 100.0) -> str:
    """A tenant with explicit caps. BOTH, or the refusal is for the wrong reason."""
    with session_scope() as s:
        ws = Workspace(name="W", slug=f"w159-{uuid.uuid4().hex[:10]}",
                       niche="AI money")
        s.add(ws)
        s.flush()
        ws.settings_json = {"safety": {"daily_budget_usd": float(daily),
                                       "per_video_budget_usd": float(per_call)}}
        return ws.id


def _set_budgets(workspace_id: str, *, daily: float, per_call: float) -> None:
    with session_scope() as s:
        row = s.get(Workspace, workspace_id)
        settings = dict(row.settings_json or {})
        safety = dict(settings.get("safety") or {})
        safety["daily_budget_usd"] = float(daily)
        safety["per_video_budget_usd"] = float(per_call)
        settings["safety"] = safety
        row.settings_json = settings


@pytest.fixture()
def ws_a():
    return _make_workspace()


@pytest.fixture()
def ws_b():
    return _make_workspace()


def cost_rows(workspace_id: str, category: str = "") -> list[CostEntry]:
    with session_scope() as s:
        query = s.query(CostEntry).filter(CostEntry.workspace_id == workspace_id)
        if category:
            query = query.filter(CostEntry.category == category)
        return list(query.order_by(CostEntry.created_at, CostEntry.id).all())


def ledger_size() -> int:
    """Every row in the ledger, whatever its owner.

    The test database is session-scoped and shared, so an assertion about "the
    whole ledger" has to be compared against a snapshot taken in the SAME test.
    Otherwise it measures whatever earlier tests happened to bill, and a guard
    that cannot fail is not a guard.
    """
    with session_scope() as s:
        return int(s.query(CostEntry).count())


def cost_rows_by_category(category: str) -> list[CostEntry]:
    """Rows of ONE category, whatever workspace owns them.

    A per-test unique category is what makes "this operation wrote nothing"
    observable next to other tests' real reservations.
    """
    with session_scope() as s:
        return list(s.query(CostEntry).filter(
            CostEntry.category == category).all())


def unique_category() -> str:
    """A category no other test in this session can collide with."""
    return f"w159-{uuid.uuid4().hex[:10]}"


class FakeLLM:
    """Stands in for ``providers.llm.complete_json`` and records what it was told.

    Counting here is the only place "no billable request was made" is
    observable; the caller's own state cannot prove a negative about the wire.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def complete_json(self, *, system: str, user: str, workspace_id: str = "",
                      **kwargs) -> dict:
        import json as _json

        self.calls.append({"workspace_id": workspace_id, "system": system})
        return {"lines": [f"[{line}]"
                          for line in _json.loads(user)["lines"]]}


def install_llm(monkeypatch) -> FakeLLM:
    from app.providers import llm as llm_mod

    double = FakeLLM()
    monkeypatch.setattr(llm_mod, "llm_available", double.available)
    monkeypatch.setattr(llm_mod, "complete_json", double.complete_json)
    monkeypatch.setattr(dubbing_mod, "_translation_model", lambda: "test-model")
    return double


class _Crash(RuntimeError):
    """The process died here. Deliberate, not a bug."""


def fresh_engine():
    """A brand-new engine + session factory against the same database file.

    The whole point of the §12 tests: a read served from the identity map that
    wrote the row proves nothing about durability, so every restart assertion
    goes through here.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings

    engine = create_engine(settings.database_url)
    return engine, sessionmaker(bind=engine)


def fresh_read_cost_rows(workspace_id: str) -> list[CostEntry]:
    engine, factory = fresh_engine()
    try:
        with factory() as s:
            rows = list(s.query(CostEntry).filter(
                CostEntry.workspace_id == workspace_id).all())
            s.expunge_all()
            return rows
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# localization scaffolding
# ---------------------------------------------------------------------------

SOURCE_L1 = "Save 20% of every paycheck with YMONEY and follow for more."
SOURCE_L2 = "Step two: pay off $5,000 in 90 days at https://ymoney.example.com"
STANDARD_STRATEGY = {
    "title": "How to save $5,000 fast",
    "description": "A 90-day plan that works",
    "hashtags": ["#saveMoney", "#ymoney"],
    "metrics": {"views": 1200},
}


def _build_source(workspace_id: str) -> str:
    """A source content item with a timeline and two scenes, like Work 07's."""
    from app.engine import timeline as tl
    from app.models import ContentItem, ContentTimeline, Scene

    with session_scope() as db:
        doc = tl.create_empty(workspace_id, duration_seconds=6.0, fps=30.0,
                              aspect="9:16")
        tl.add_clip(doc, track="video", clip_id="src_1", name="opening",
                    start=0.0, duration=6.0, source={"asset": "source.mp4"})
        tl.add_clip(doc, track="text", clip_id="src_txt", name="lower third",
                    start=0.0, duration=3.0, text={"content": "How to save money"})
        content = ContentItem(workspace_id=workspace_id, topic="save money fast",
                              status="READY",
                              strategy_json=dict(STANDARD_STRATEGY))
        db.add(content)
        db.flush()
        timeline = ContentTimeline(workspace_id=workspace_id,
                                   content_item_id=content.id, name="main",
                                   fps=30.0, duration_seconds=6.0,
                                   tracks_json=doc, version=1)
        db.add(timeline)
        db.flush()
        for i, (start, end, text) in enumerate(
                [(0.0, 3.0, SOURCE_L1), (3.0, 6.0, SOURCE_L2)]):
            db.add(Scene(workspace_id=workspace_id,
                         content_item_id=content.id, timeline_id=timeline.id,
                         index=i, title=f"scene {i}", narration=text,
                         script_segment=text, start_seconds=start,
                         end_seconds=end))
        content_id = content.id
    return content_id


def _prepare(workspace_id: str, source_content_id: str) -> str:
    from app.engine.localization.pipeline import prepare_localizations

    with session_scope() as db:
        return prepare_localizations(
            db, workspace_id=workspace_id, source_content_id=source_content_id,
            target_languages=["es"], locales={"es": "es-ES"},
            translation_version=1)[0].id


class RecordingTranslator:
    """A translate hook that records the workspace it was handed, per call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, texts, target_lang, *, workspace_id="", glossary=None):
        self.calls.append({"workspace_id": workspace_id, "n": len(texts),
                           "glossary": glossary})
        return [f"[{t}]" for t in texts]


class RecordingTTS:
    """A TTS hook that records the workspace it was handed."""

    def __init__(self, out_dir: Path) -> None:
        self.calls: list[dict] = []
        self.out_dir = out_dir

    def __call__(self, texts, voice, work_dir, workspace_id=""):
        self.calls.append({"workspace_id": workspace_id, "n": len(texts)})
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        made = []
        for i, _ in enumerate(texts):
            p = work_dir / f"seg_{i}.wav"
            p.write_bytes(b"RIFF0000WAVE")
            made.append(p)
        return made


def _run_pipeline(workspace_id: str, localized_id: str, *, translator, tts):
    from app.engine.localization.pipeline import LocalizationPipeline

    with session_scope() as db:
        return LocalizationPipeline(
            db, localized_content_id=localized_id, workspace_id=workspace_id,
            translate_fn=translator, tts_fn=tts,
            voice_fn=lambda lang, explicit="": f"{lang}-voice-1").run()


# ===========================================================================
# §1/§8 -- the authority model
# ===========================================================================


def test_the_authority_vocabularies_are_closed_and_non_empty():
    """Two closed sets, one for money and one for the actor.

    They are separate because "whose money" and "who asked" are different
    questions, and a single combined enum would force an operator to write
    nonsense like ``TENANT_AUTONOMOUS`` to answer the first one.
    """
    assert SPEND_AUTHORITIES == ("WORKSPACE_OWNED", "SYSTEM_OWNED",
                                 "EXPLICIT_NONBILLABLE")
    assert ACTOR_AUTHORITIES == ("USER", "AUTONOMOUS_POLICY",
                                 "APPROVED_WORKFLOW", "SYSTEM")
    for enum in (SpendAuthority, ActorAuthority):
        assert all(isinstance(m, str) for m in enum), (
            f"{enum.__name__} members must be their queryable spelling, so a "
            "ledger row can hold one without a second name for the same fact")


def test_an_empty_workspace_blocks_the_billable_call(ws_a):
    """The core change: no workspace -> no request, and a real error.

    Mutation target: ``PaidOperation.authorize``. Put the old "warn and return"
    back and this fails with ``DID NOT RAISE``.
    """
    category = unique_category()
    operation = paid_operation(provider="probe", operation="ownerless",
                               workspace_id="", category=category,
                               estimated_cost=0.02)

    with pytest.raises(OwnerlessSpendRefused) as caught:
        operation.authorize()

    assert operation.reservation is None
    assert operation.entry_id == ""
    assert cost_rows_by_category(category) == []
    detail = str(caught.value)
    assert "Nothing was sent" in detail, detail
    assert "workspace_id is empty" in detail, detail
    assert caught.value.actor == "USER"
    assert caught.value.authority == "WORKSPACE_OWNED"
    assert caught.value.provider == "probe"


def test_the_refusal_is_a_budget_error_so_existing_handlers_never_resend(
        ws_a):
    """Subclassing ``BudgetExceededError`` is what makes this safe to ship.

    ``providers/dubbing.py`` and ``engine/agents/production.py`` already wrap
    ``paid.authorize()`` in ``except BudgetExceededError`` and re-raise as "the
    budget refused, nothing was sent". A brand-new exception type would slip
    past those handlers and reach the provider's own except arm, which on some
    providers marks the call rejected/retryable -- i.e. it would turn "blocked"
    into "safe to send again".
    """
    assert issubclass(OwnerlessSpendRefused, cost_mod.BudgetExceededError)
    operation = paid_operation(provider="probe", operation="ownerless",
                               workspace_id="   ", category=unique_category(),
                               estimated_cost=0.01)
    with pytest.raises(cost_mod.BudgetExceededError):
        operation.authorize()


def test_the_ownerless_llm_leg_is_refused_not_merely_warned(monkeypatch):
    """``llm_paid`` had the same hole with a nicer log message.

    ``SpendAuthorization.__post_init__`` set ``enforced = False`` and returned;
    ``authorize()`` then returned immediately and the POST went out for money
    no cap could see. Mutation target: that early return.
    """
    from app.engine.intelligence.llm_paid import SpendAuthorization

    leg = SpendAuthorization(workspace_id="", amount_usd=0.004)
    assert leg.enforced is False and leg.ownerless_reason

    with pytest.raises(OwnerlessSpendRefused) as caught:
        leg.authorize()

    assert leg.reservation is None
    assert "workspace_id is empty" in caught.value.reason


def test_run_completion_refuses_an_ownerless_leg_before_the_wire():
    """The whole stack: no workspace -> ``LLMCompletionBudgetRefused``, 0 POSTs.

    Asserted at the transport, which is the only place a negative about the
    network is observable.
    """
    from app.engine.intelligence import llm_paid

    posts: list[dict] = []

    def transport(*_args, **kwargs):
        posts.append(kwargs)
        raise AssertionError("the transport was reached by an ownerless leg")

    with pytest.raises(llm_paid.LLMCompletionBudgetRefused):
        llm_paid.run_completion(
            transport=transport, url="http://llm.test/v1/chat/completions",
            api_key="k", workspace_id="",
            candidates=llm_paid.build_candidates("m", {"messages": []}))

    assert posts == []


def test_the_localization_translation_leg_never_reaches_the_provider(
        ws_a, monkeypatch):
    """The concrete victim: metadata translation reached ``paid_operation``
    with a hardcoded ``workspace_id=""``.

    Proved at the provider boundary, because a refusal that still called the
    provider would have spent the money.
    """
    double = install_llm(monkeypatch)
    before = ledger_size()

    with pytest.raises(dubbing_mod.DubError, match="no workspace to authorise"):
        dubbing_mod.translate_segments(["one", "two"], "es", "")

    assert double.calls == [], "a billable request was sent with no owner"
    assert ledger_size() == before, (
        "an unreserved ownerless request must leave no trace anywhere: a row "
        "with no owner is a charge nobody can reconcile, and a row with a "
        "guessed owner is worse")


def test_system_owned_without_a_configured_system_budget_still_blocks(
        ws_a, monkeypatch):
    """SYSTEM is an authority, not a synonym for "workspace missing".

    Mutation target: the ``cap <= 0`` refusal in ``resolve_ownership``. Delete
    it and this test fails with ``DID NOT RAISE`` -- and a system-owned leg
    starts running with no ceiling at all, which is the same hole as before
    with a friendlier name.
    """
    monkeypatch.delenv(SYSTEM_BUDGET_ENV, raising=False)
    assert system_budget_usd() == 0.0

    operation = paid_operation(provider="probe", operation="maintenance",
                               workspace_id="", category=unique_category(),
                               estimated_cost=0.02)
    operation.declared(authority=SpendAuthority.SYSTEM_OWNED,
                       actor=ActorAuthority.SYSTEM)

    with pytest.raises(OwnerlessSpendRefused) as caught:
        operation.authorize()

    assert "no system budget is configured" in caught.value.reason
    assert SYSTEM_BUDGET_ENV in str(caught.value)
    assert operation.reservation is None


def test_the_system_actor_does_not_relax_the_workspace_requirement(
        ws_a, monkeypatch):
    """``actor=SYSTEM`` names WHO asked. It grants nothing.

    This is the precise shape of the bug: an operator-run job with no tenant
    must not become an unlimited spender just because it is "system" work.
    """
    monkeypatch.delenv(SYSTEM_BUDGET_ENV, raising=False)
    operation = paid_operation(provider="probe", operation="maintenance",
                               workspace_id="", category=unique_category(),
                               estimated_cost=0.02)
    operation.declared(actor=ActorAuthority.SYSTEM)

    assert operation.authority is SpendAuthority.WORKSPACE_OWNED, (
        "the actor must not silently promote the authority")
    with pytest.raises(OwnerlessSpendRefused):
        operation.authorize()


def test_a_configured_system_budget_reserves_against_the_system_ledger_owner(
        ws_a, monkeypatch):
    """The legitimate ``SYSTEM_OWNED`` path, and where the money is booked.

    Charged to ``SYSTEM_LEDGER_OWNER`` rather than to whichever tenant
    happened to trigger the job: a system row that looks like a tenant row is
    how an operator's maintenance spend is silently attributed to a customer.
    """
    monkeypatch.setenv(SYSTEM_BUDGET_ENV, "0.50")
    category = unique_category()
    operation = paid_operation(provider="probe", operation="maintenance",
                               workspace_id=ws_a, category=category,
                               estimated_cost=0.20)
    operation.declared(authority=SpendAuthority.SYSTEM_OWNED,
                       actor=ActorAuthority.SYSTEM)
    operation.authorize()

    assert operation.reservation.workspace_id == SYSTEM_LEDGER_OWNER
    rows = cost_rows_by_category(category)
    assert len(rows) == 1
    detail = dict(rows[0].detail_json or {})
    assert detail["spend_authority"] == "SYSTEM_OWNED"
    assert detail["actor_authority"] == "SYSTEM"
    assert detail["budget_source"] == "SYSTEM_BUDGET"
    assert detail["owned_workspace_id"] == ws_a, (
        "the tenant that triggered the work is still recorded")
    assert detail["charged_workspace_id"] == SYSTEM_LEDGER_OWNER
    assert detail["system_budget_usd"] == pytest.approx(0.50)
    assert cost_rows(ws_a, category) == [], (
        "the tenant's own cap must not carry the system's spend")


def test_the_system_ceiling_is_actually_enforced(ws_a, monkeypatch):
    """A configured budget that is never checked is a comment.

    ``reserve_spend`` takes explicit caps for exactly this; without them the
    system row would be measured against ``settings.daily_budget_usd``, which
    is a per-tenant default and has nothing to do with the operator's ceiling.
    """
    monkeypatch.setenv(SYSTEM_BUDGET_ENV, "0.10")
    category = unique_category()
    operation = paid_operation(provider="probe", operation="maintenance",
                               workspace_id="", category=category,
                               estimated_cost=0.25)
    operation.declared(authority=SpendAuthority.SYSTEM_OWNED,
                       actor=ActorAuthority.SYSTEM)

    with pytest.raises(cost_mod.BudgetExceededError):
        operation.authorize()

    assert cost_rows_by_category(category) == [], (
        "a refused system reservation must write nothing")


def test_a_non_numeric_system_budget_is_unconfigured_not_infinite(monkeypatch):
    """A typo in the operator's manifest must fail closed.

    ``float("lots")`` raising would be a crash in a background job; parsing to
    a big number would be an unlimited system budget. Neither is acceptable, so
    the honest answer -- "not configured" -- is what it gets.
    """
    monkeypatch.setenv(SYSTEM_BUDGET_ENV, "lots")
    assert system_budget_usd() == 0.0
    with pytest.raises(OwnerlessSpendRefused):
        resolve_ownership(workspace_id="", provider="p", operation="o",
                          authority=SpendAuthority.SYSTEM_OWNED)


def test_explicit_nonbillable_is_an_assertion_not_an_inference(ws_a):
    """The escape hatch exists, and nothing but a caller can open it.

    A missing workspace must never promote itself to non-billable: that would
    restore the exact hole this work order closed, one keyword away.
    """
    blocked = paid_operation(provider="local_tts", operation="speak",
                             workspace_id="", category=unique_category(),
                             estimated_cost=0.0)
    with pytest.raises(OwnerlessSpendRefused):
        blocked.authorize()

    category = unique_category()
    operation = paid_operation(provider="local_tts", operation="speak",
                               workspace_id="", category=category,
                               estimated_cost=0.0)
    operation.declared(authority=SpendAuthority.EXPLICIT_NONBILLABLE,
                       actor=ActorAuthority.SYSTEM)
    operation.authorize()

    assert operation.reservation is None
    assert operation.ownership.billable is False
    assert cost_rows_by_category(category) == [], (
        "a non-billable assertion writes no ledger row: a zero row would be "
        "indistinguishable from a lost response")


def test_an_unknown_authority_spelling_is_refused_not_defaulted(ws_a):
    """A misspelled authority stored as free text is a permanent,
    silently un-authorised spend. So it raises at declaration time."""
    operation = paid_operation(provider="probe", operation="typo",
                               workspace_id=ws_a, category=unique_category())
    with pytest.raises(ValueError, match="unknown spend authority"):
        operation.declared(authority="TENANT_OWED")
    with pytest.raises(ValueError, match="unknown actor authority"):
        operation.declared(actor="AUTOPILOT")
    assert coerce_authority(SpendAuthority, "SYSTEM_OWNED",
                          "spend authority") is SpendAuthority.SYSTEM_OWNED


def test_the_ledger_row_records_the_whole_authority_picture(ws_a):
    """Persisted per operation: owner, actor, budget source, operation type,
    provider, estimate and the operation id that pairs it with the submit."""
    operation = paid_operation(provider="openai_compatible_llm",
                               operation="dubbing.translate_batch",
                               workspace_id=ws_a, category=TRANSLATE,
                               estimated_cost=0.017)
    operation.declared(actor=ActorAuthority.AUTONOMOUS_POLICY)
    operation.authorize()

    rows = cost_rows(ws_a, TRANSLATE)
    assert len(rows) == 1
    detail = dict(rows[0].detail_json or {})
    assert detail["operation"] == "dubbing.translate_batch"
    assert detail["provider"] == "openai_compatible_llm"
    assert detail["estimated_usd"] == pytest.approx(0.017)
    assert detail["operation_id"] == operation.operation_id
    assert detail["spend_authority"] == "WORKSPACE_OWNED"
    assert detail["actor_authority"] == "AUTONOMOUS_POLICY"
    assert detail["budget_source"] == "WORKSPACE_BUDGET"
    assert detail["owned_workspace_id"] == ws_a
    assert detail["charged_workspace_id"] == ws_a
    assert detail["reservation"] is True
    assert operation.ownership.reservation_id == rows[0].id, (
        "the resolved owner must carry the reservation id, so an operator can "
        "join an operation to its ledger row without re-deriving anything")


def test_workspace_owned_spend_still_reserves_normally(ws_a):
    """The strict default must not break the ordinary path."""
    category = unique_category()
    operation = paid_operation(provider="probe", operation="normal",
                               workspace_id=ws_a, category=category,
                               estimated_cost=0.02)
    operation.authorize()
    assert operation.reservation.workspace_id == ws_a
    operation.mark_succeeded()
    assert len(cost_rows(ws_a, category)) == 1
    assert cost_mod.spent_since(ws_a) == pytest.approx(0.02)


def test_a_workspace_without_a_category_is_still_refused(ws_a):
    """Unchanged, and still first: an uncountable row is not a gate."""
    operation = paid_operation(provider="probe", operation="no-category",
                               workspace_id=ws_a)
    with pytest.raises(ValueError, match="needs a category"):
        operation.authorize()


# ===========================================================================
# §2 -- localization propagates the canonical workspace
# ===========================================================================


def test_localize_metadata_passes_the_workspace_it_was_given(ws_a):
    """The unit that hardcoded ``workspace_id=""``.

    Mutation target: that literal. Put it back and this fails on
    ``seen == [""]``.
    """
    from app.engine.localization.pipeline import localize_metadata

    translator = RecordingTranslator()
    out, changed = localize_metadata(
        {"strategy": {"title": "How to save money", "metrics": {"views": 12}}},
        "es", translator, workspace_id=ws_a)

    assert [c["workspace_id"] for c in translator.calls] == [ws_a]
    assert changed == 1, "the prose was translated, so exactly one field moved"
    assert out["strategy"]["title"] == "[How to save money]"
    assert out["strategy"]["metrics"] == {"views": 12}, (
        "numbers are still copied verbatim")


def test_every_billable_stage_of_a_real_run_carries_the_workspace(
        ws_a, tmp_path, storage_sandbox):
    """Run the whole pipeline and read the workspace off each billable hook.

    Four hooks in one run (cue translation, on-screen text, metadata, TTS). Any
    one of them passing ``""`` shows up here, which is what makes this a
    coverage test rather than a spot check.
    """
    content_id = _build_source(ws_a)
    localized_id = _prepare(ws_a, content_id)
    translator = RecordingTranslator()
    tts = RecordingTTS(tmp_path)

    result = _run_pipeline(ws_a, localized_id, translator=translator, tts=tts)

    assert result["qc_status"] in ("PASS", "PASS_WITH_WARNINGS"), result
    assert translator.calls, "the run never translated anything"
    assert [c["workspace_id"] for c in translator.calls] == [ws_a] * len(
        translator.calls)
    assert "" not in [c["workspace_id"] for c in translator.calls]
    assert [c["workspace_id"] for c in tts.calls] == [ws_a] * len(tts.calls)
    assert "" not in [c["workspace_id"] for c in tts.calls]


def test_the_pipeline_ships_no_empty_workspace_literal_for_a_billable_call():
    """A static guard on the regression itself.

    The behavioural test above proves the CURRENT code; this proves the
    literal cannot quietly come back. Both matter: a future refactor that
    reintroduces ``workspace_id=""`` in a stage the fixtures do not exercise
    would still be caught here.
    """
    import ast
    import inspect

    from app.engine.localization import pipeline as pipeline_mod

    tree = ast.parse(inspect.getsource(pipeline_mod))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "workspace_id" and isinstance(kw.value, ast.Constant) \
                    and not (kw.value.value or ""):
                offenders.append(node.lineno)
    assert offenders == [], (
        f"pipeline.py passes an empty workspace_id to a billable call at "
        f"line(s) {offenders}")


def test_the_dubbing_tts_leg_scopes_the_workspace_for_the_provider(
        ws_a, tmp_path, monkeypatch):
    """TTS resolves its tenant from an explicit argument, else from an ambient
    context variable. The dubbing layer now sets that scope explicitly.

    Mutation target: the ``workspace_scope`` block. Remove it and the provider
    reads whatever outer scope happens to hold -- here ``None``, i.e. no owner.
    """
    from app.providers import tts as tts_mod
    from app.services import provider_settings

    seen: list[str | None] = []

    class Spy:
        def synthesize(self, text, *, voice="", rate=1.0, **kwargs):
            seen.append(provider_settings.current_workspace_id())
            return tts_mod.TTSResult(audio_bytes=b"RIFF0", format="wav",
                                  provider="spy")

    monkeypatch.setattr(tts_mod, "get_tts_provider", lambda: Spy())

    dubbing_mod.synthesize_segments(["hola", "adios"], "es-voice",
                                    tmp_path / "dub", workspace_id=ws_a)

    assert seen == [ws_a, ws_a], (
        f"the TTS provider resolved {seen}; it must see the workspace the "
        "canonical lineage already knows")


def test_no_empty_workspace_reaches_a_billable_operation_in_the_dubbing_lane(
        ws_a, monkeypatch):
    """The dubbing provider is the boundary between localization and spend.

    ``translate_segments`` used to forward ``workspace_id or ""`` and let the
    helper shrug. Now the refusal is explicit and happens before the model is
    even chosen, so nothing billable is constructed for a call that cannot be
    paid for.
    """
    double = install_llm(monkeypatch)
    with pytest.raises(dubbing_mod.DubError, match="no workspace to authorise"):
        dubbing_mod.translate_segments(["x"], "es", None)
    assert double.calls == []


# ===========================================================================
# §2 -- cross-workspace isolation through the localization path
# ===========================================================================


def test_workspace_b_cannot_consume_workspace_as_budget(ws_a, ws_b, monkeypatch):
    """The headline isolation case, through the localization path itself.

    A is empty, B is rich. A's localization run must be refused AND must leave
    B's ledger untouched -- an ownerless spend lands on ``""``, never on
    whichever neighbour happened to have headroom.
    """
    _set_budgets(ws_a, daily=0.0, per_call=100.0)
    _set_budgets(ws_b, daily=100.0, per_call=100.0)
    double = install_llm(monkeypatch)

    with pytest.raises(dubbing_mod.DubError, match="budget refused before batch"):
        dubbing_mod.translate_segments(["uno", "dos"], "es", ws_a)

    assert double.calls == []
    assert cost_rows(ws_b, TRANSLATE) == [], (
        "workspace A's refused run charged workspace B")
    assert cost_mod.spent_since(ws_b) == pytest.approx(0.0)


def test_one_localization_run_cannot_spend_the_other_localization_run(
        ws_a, ws_b, monkeypatch):
    """Two tenants, two live localization rows, one shared-looking budget.

    B's translation succeeds and is charged to B. A then runs and is refused
    on A's own cap. The order matters: if the resolution leaked, whichever ran
    first would pay for both.
    """
    _set_budgets(ws_a, daily=0.0, per_call=100.0)
    _set_budgets(ws_b, daily=100.0, per_call=100.0)
    double = install_llm(monkeypatch)

    dubbing_mod.translate_segments(["uno"], "es", ws_b)
    assert len(double.calls) == 1
    assert len(cost_rows(ws_b, TRANSLATE)) == 1

    with pytest.raises(dubbing_mod.DubError, match="budget refused before batch"):
        dubbing_mod.translate_segments(["uno"], "es", ws_a)

    assert len(double.calls) == 1, "workspace A reached the provider"
    assert len(cost_rows(ws_b, TRANSLATE)) == 1, (
        "a second reservation appeared on workspace B")
    assert cost_rows(ws_a, TRANSLATE) == []
    assert cost_mod.spent_since(ws_b) == pytest.approx(
        cost_rows(ws_b, TRANSLATE)[0].amount_usd)


def test_an_ambient_workspace_scope_cannot_launder_one_tenants_spend(
        ws_a, ws_b, monkeypatch):
    """The realistic cross-tenant leak: falling back to the ambient scope.

    ``providers/tts.py`` resolves its tenant from the context variable when the
    caller passes none, which is exactly why this shape deserves a test: a
    translation layer that "helpfully" did the same would charge whichever
    workspace happened to be in scope. Here B is in scope and generously
    funded, and A is broke -- the refusal has to hold anyway.
    """
    from app.services import provider_settings

    _set_budgets(ws_a, daily=0.0, per_call=100.0)
    _set_budgets(ws_b, daily=100.0, per_call=100.0)
    double = install_llm(monkeypatch)
    token = provider_settings.workspace_context(ws_b)
    try:
        with pytest.raises(dubbing_mod.DubError, match="budget refused"):
            dubbing_mod.translate_segments(["uno"], "es", ws_a)
    finally:
        provider_settings.reset_workspace_context(token)

    assert double.calls == [], "workspace A's request went out on B's money"
    assert cost_rows(ws_b, TRANSLATE) == [], (
        "the ambient scope leaked: B paid for A's translation")
    assert cost_rows(ws_a, TRANSLATE) == []


def test_the_localization_run_of_one_tenant_reads_only_its_own_lineage(
        ws_a, ws_b, tmp_path, storage_sandbox):
    """Isolation of the ROW, not just the money.

    A row for workspace B cannot be driven by workspace A's pipeline at all, so
    no spend can be attributed to the wrong tenant by reusing its id.
    """
    from app.engine.localization.pipeline import (
        LocalizationError,
        LocalizationPipeline,
    )

    content_id = _build_source(ws_b)
    localized_id = _prepare(ws_b, content_id)

    with session_scope() as db, pytest.raises(
            LocalizationError, match="not found in this workspace"):
        LocalizationPipeline(
            db, localized_content_id=localized_id, workspace_id=ws_a,
            translate_fn=RecordingTranslator(), tts_fn=RecordingTTS(tmp_path))


def test_the_system_ledger_owner_is_not_a_tenant(ws_a, monkeypatch):
    """A system row must not be countable as a tenant's spend.

    Otherwise an operator's maintenance job would shrink some customer's
    remaining budget by an amount neither of them can explain.
    """
    monkeypatch.setenv(SYSTEM_BUDGET_ENV, "1.00")
    category = unique_category()
    operation = paid_operation(provider="probe", operation="maintenance",
                               workspace_id=ws_a, category=category,
                               estimated_cost=0.05)
    operation.declared(authority=SpendAuthority.SYSTEM_OWNED,
                       actor=ActorAuthority.SYSTEM)
    operation.authorize()

    rows = cost_rows_by_category(category)
    assert len(rows) == 1
    assert rows[0].workspace_id == SYSTEM_LEDGER_OWNER
    assert cost_rows(ws_a, category) == [], (
        "a system-owned reservation charged the tenant that triggered it")
    assert cost_mod.spent_since(ws_a) == pytest.approx(0.0)


# ===========================================================================
# §12 -- restart durability: five crash points, each read through a NEW engine
# ===========================================================================

CRASH_POINTS = ("after_reservation", "after_attempt_persisted",
                "after_http_write", "after_remote_id_received",
                "before_settlement")


def _mark_attempt(localized_id: str, operation_id: str) -> None:
    """Durable evidence the request is ABOUT TO leave. Before the request."""
    with session_scope() as s:
        row = s.get(LocalizedContent, localized_id)
        lineage = dict(row.lineage_json or {})
        lineage["submission"] = {
            "operation_id": operation_id, "state": "SUBMISSION_ATTEMPTED"}
        row.lineage_json = lineage


def _record_execution(localized_id: str, state: str, *, remote_id: str = "",
                      detail: str = "") -> None:
    with session_scope() as s:
        row = s.get(LocalizedContent, localized_id)
        lineage = dict(row.lineage_json or {})
        payload = dict(lineage.get("submission") or {})
        payload.update({"state": state, "detail": detail})
        if remote_id:
            payload["remote_id"] = remote_id
        lineage["submission"] = payload
        row.lineage_json = lineage


def _record_remote(localized_id: str, remote_id: str) -> None:
    _record_execution(localized_id, "REMOTE_ID_CONFIRMED",
                      remote_id=remote_id)


def drive_translation_until_crash(workspace_id: str, localized_id: str,
                                  crash_at: str, *, wire: list) -> None:
    """One localization translation, interrupted at ``crash_at``.

    The lifecycle order is the one ``paid_provider`` documents and the one the
    provider lane uses: authorize -> mark_attempt -> ONE outbound write ->
    remote id -> close the book. Each crash point is a place the process can
    genuinely die, so what survives a restart is exactly what was committed.
    """
    operation = paid_operation(
        provider="openai_compatible_llm", operation="dubbing.translate_batch",
        workspace_id=workspace_id, category=TRANSLATE, estimated_cost=0.02)
    operation.declared(actor=ActorAuthority.APPROVED_WORKFLOW)
    operation.bind(
        on_attempt=lambda oid: _mark_attempt(localized_id, oid),
        on_execution=lambda state, detail: _record_execution(
            localized_id, state, detail=detail),
        on_remote_id=lambda rid: _record_remote(localized_id, rid))

    operation.authorize()
    if crash_at == "after_reservation":
        raise _Crash(crash_at)

    operation.mark_attempt()
    if crash_at == "after_attempt_persisted":
        raise _Crash(crash_at)

    wire.append({"body": "the one outbound write"})
    if crash_at == "after_http_write":
        raise _Crash(crash_at)

    operation.mark_accepted("remote-abc-123")
    if crash_at == "after_remote_id_received":
        raise _Crash(crash_at)

    operation.close_book()
    if crash_at == "before_settlement":
        raise _Crash(crash_at)


def fresh_lineage(localized_id: str) -> dict:
    """The caller's durable row, read by a process that has never seen it."""
    engine, factory = fresh_engine()
    try:
        with factory() as s:
            row = s.get(LocalizedContent, localized_id)
            payload = dict(row.lineage_json or {}) if row is not None else {}
        return payload
    finally:
        engine.dispose()


@pytest.mark.parametrize("crash_at", CRASH_POINTS)
def test_every_restart_point_keeps_the_money_visible(ws_a, crash_at):
    """The five crash points, and what a restarted process can still see.

    Each case asserts BOTH halves of the recovery answer -- the caller's row
    state and the ledger -- through a brand-new engine. Reading through the
    session that wrote them would pass with no persistence at all.
    """
    content_id = _build_source(ws_a)
    localized_id = _prepare(ws_a, content_id)
    wire: list[dict] = []

    with pytest.raises(_Crash):
        drive_translation_until_crash(ws_a, localized_id, crash_at, wire=wire)

    rows = fresh_read_cost_rows(ws_a)
    assert len(rows) == 1, (
        "the reservation is the row that proves the spend was authorised; "
        "losing it makes the day understate work that may already be billed")
    detail = dict(rows[0].detail_json or {})
    assert detail["spend_authority"] == "WORKSPACE_OWNED"
    assert detail["actor_authority"] == "APPROVED_WORKFLOW"
    assert detail["budget_source"] == "WORKSPACE_BUDGET"
    assert rows[0].amount_usd == pytest.approx(0.02)
    assert rows[0].is_estimate is True

    lineage = fresh_lineage(localized_id)
    submission = dict(lineage.get("submission") or {})

    if crash_at == "after_reservation":
        assert wire == []
        assert submission == {}, (
            "nothing was sent, so a restarted process must find no evidence a "
            "request left; the reservation is releasable, not ambiguous")
        assert fresh_read_cost_rows(ws_a)[0].is_estimate is True
    elif crash_at == "after_attempt_persisted":
        assert wire == []
        assert submission["state"] == "SUBMISSION_ATTEMPTED"
        assert submission["operation_id"], (
            "without the operation id the row cannot be paired with its "
            "ledger row after a restart")
    elif crash_at == "after_http_write":
        assert len(wire) == 1
        assert submission["state"] == "SUBMISSION_ATTEMPTED", (
            "the write left and the answer never came back: a restart has to "
            "treat this as possible exposure, not as a failed attempt")
        assert submission.get("remote_id", "") == ""
    elif crash_at == "after_remote_id_received":
        assert len(wire) == 1
        assert submission["state"] == "REMOTE_ID_CONFIRMED"
        assert submission["remote_id"] == "remote-abc-123", (
            "the remote id is what makes the restart safe: the same job can be "
            "polled and must never be resubmitted")
    else:  # before_settlement
        assert len(wire) == 1
        assert submission["state"] == "REMOTE_ID_CONFIRMED"
        assert submission["remote_id"] == "remote-abc-123"
        assert cost_mod.spent_since(ws_a) == pytest.approx(0.02), (
            "an unsettled-but-accepted reservation still holds the money: "
            "releasing it here would hand the same dollar to the next caller "
            "while this one is already paid")


def test_a_restart_after_the_http_write_reads_as_unknown_exposure(ws_a):
    """The recovery answer for the one ambiguous point, spelled out.

    After the wire write and before the remote id, "was this billed?" is
    unknowable. Marking it ``ESTIMATED`` on a settled-looking row would let an
    operator close the incident as if the money were still counted.
    """
    content_id = _build_source(ws_a)
    localized_id = _prepare(ws_a, content_id)
    wire: list[dict] = []

    with pytest.raises(_Crash):
        drive_translation_until_crash(ws_a, localized_id, "after_http_write",
                                      wire=wire)
    rows = fresh_read_cost_rows(ws_a)
    detail = dict(rows[0].detail_json or {})
    assert "cost_outcome" not in detail, (
        "nothing reported a cost outcome, so none may be claimed")

    # the operator's remedy: keep the reservation, mark the exposure unknown.
    from app.services.paid_executor import CostOutcome
    from app.services.paid_provider import annotate_exposure

    entry_id = rows[0].id
    assert annotate_exposure(entry_id, CostOutcome.UNKNOWN_EXPOSURE)
    reread = fresh_read_cost_rows(ws_a)
    assert len(reread) == 1, "annotating must never insert a second row"
    marked = dict(reread[0].detail_json or {})
    assert marked["cost_outcome"] == "UNKNOWN_EXPOSURE"
    assert marked["exposure_unknown"] is True
    assert cost_mod.spent_since(ws_a) == pytest.approx(0.02), (
        "the reservation is held through a restart: an ambiguity never "
        "releases the budget back to the pool")


def test_the_authority_survives_a_restart_with_the_money(ws_a):
    """Ownership is read from the ledger, not remembered by the process.

    After a restart nobody can re-derive "which tenant paid" from memory, so
    it has to be a durable fact on the row.
    """
    content_id = _build_source(ws_a)
    localized_id = _prepare(ws_a, content_id)
    with pytest.raises(_Crash):
        drive_translation_until_crash(ws_a, localized_id,
                                      "after_remote_id_received", wire=[])

    row = fresh_read_cost_rows(ws_a)[0]
    detail = dict(row.detail_json or {})
    assert detail["charged_workspace_id"] == ws_a
    assert detail["spend_authority"] == "WORKSPACE_OWNED"
    assert detail["operation_id"] == fresh_lineage(localized_id)[
        "submission"]["operation_id"], (
        "the ledger row and the caller's row must name the same operation "
        "after a restart, or neither can be attributed")


@pytest.fixture()
def storage_sandbox(tmp_path, monkeypatch):
    """Keep subtitle/TTS writes inside the test sandbox (not a schema change)."""
    from app.services import storage

    root = tmp_path / "videos"
    monkeypatch.setattr(storage, "STORAGE_ROOT", root)
    return root