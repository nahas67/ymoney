"""Work 15.9 §7/§9 -- the re-ask policy, and 100% inventory coverage.

Two things are pinned here, and they are different kinds of claim.

**§7 -- a re-ask is a SECOND PURCHASE.** ``providers.llm.complete_json`` used
to re-ask on unparseable output by default, from inside a parser helper, with no
policy, no budget and no record. These tests hold the line at three points: the
free local repair that runs FIRST, the typed policy that gates the second
completion, and the durable record of what was bought.

**§9 -- every billable operation in the repository is CLASSIFIED.** Coverage
here means a safety classification, not remote reconciliation: ``llm.complete``
may stay UNRECONCILABLE forever (a chat completion has no remote id) and still
be covered, because "here is exactly what happens when the response is lost, and
here is the test that proves it" is a decision. The bar is arithmetic --
discovered == classified, unexplained == 0 -- and every discovered call site is
discovered by reading the source, not by trusting the table that is being
checked.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from app.providers import llm as llm_mod

# ===========================================================================
# §7 -- the local repair, which is tried BEFORE anything costs money
# ===========================================================================


@pytest.mark.parametrize(
    ("reply", "expected", "expected_repair"),
    [
        # The strict parser already tolerates a bare fence, so the case that
        # needs the REPAIR is a fence that ALSO has a defect.
        ('```json\n{"lines": ["a"],}\n```', {"lines": ["a"]},
         "stripped-markdown-fence"),
        ('We need JSON:\n{"lines": ["a"],}', {"lines": ["a"]},
         "stripped-trailing-comma"),
        ('{"ok": True, "why": None}', {"ok": True, "why": None},
         "json-literals"),
        ('prefix\n{"a": [1, 2,],}', {"a": [1, 2]},
         "stripped-trailing-comma"),
    ],
)
def test_a_free_local_repair_saves_the_second_completion(reply, expected,
                                                         expected_repair):
    """The repair is deterministic and local, so it is always tried first."""
    value, repairs = llm_mod._extract_json_with_repair(reply)

    assert value == expected, repairs
    assert expected_repair in repairs, repairs


def test_a_bare_markdown_fence_never_reaches_the_repair_pass():
    """``_extract_json``'s brace scan already handles it, so nothing is charged
    and nothing is claimed as a repair."""
    value, repairs = llm_mod._extract_json_with_repair(
        '```json\n{"lines": ["a"]}\n```')

    assert value == {"lines": ["a"]}
    assert repairs == [], repairs


def test_the_repair_never_rewrites_a_value_inside_a_quoted_string():
    """``it\u2019s`` must survive as-is: a parse that succeeds with corrupted
    content is worse than a parse that fails honestly."""
    value, _repairs = llm_mod._extract_json_with_repair(
        '{"caption": "it\u2019s fine", "n": False}')

    assert value["caption"] == "it\u2019s fine"
    assert value["n"] is False


def test_the_strict_parser_is_unchanged_by_the_repair_work():
    """``_extract_json`` is what the whole product already depends on.

    The repair pass is additive and lives BEHIND it, so a reply that always
    parsed still parses identically -- including a dict, and including prose
    around a brace span.
    """
    assert llm_mod._extract_json('{"a": 1}') == {"a": 1}
    assert llm_mod._extract_json({"a": 1}) == {"a": 1}
    assert llm_mod._extract_json('noise {"a": 1} noise') == {"a": 1}
    assert llm_mod._extract_json("not json") is None


# ===========================================================================
# §7 -- the typed policy
# ===========================================================================


def test_the_reask_policy_defaults_to_conservative():
    """Unparseable reply -> failure. The second completion is not a default."""
    policy = llm_mod.ReaskPolicy()
    verdict = policy.decide(reason="unparseable", estimated_usd=0.01,
                            workspace_id="ws-1")

    assert verdict.allowed is False
    assert str(verdict.decision) == "REFUSE_REASK"
    assert "conservative" in verdict.reason


def test_the_reask_policy_is_typed_and_exposes_its_four_clauses():
    """``allow_reask``, ``max_reasks``, ``additional_budget`` and ``authority``
    are declared, not inferred from a boolean."""
    params = set(inspect.signature(llm_mod.ReaskPolicy).parameters)
    assert {"allow_reask", "max_reasks", "additional_budget_usd",
            "authority"} <= params, params
    assert str(llm_mod.ReaskAuthority.NEVER) == "NEVER"
    assert str(llm_mod.ReaskAuthority.CALLER_EXPLICIT) == "CALLER_EXPLICIT"


def test_a_reask_needs_a_named_approver():
    """An unattributed second charge is a bug three weeks later."""
    with pytest.raises(ValueError, match="approved_by"):
        llm_mod.ReaskPolicy(allow_reask=True)


def test_a_reask_needs_additional_budget():
    """Guard (g): authorising without a ceiling is not authorising."""
    policy = llm_mod.ReaskPolicy(allow_reask=True, approved_by="ops")
    verdict = policy.decide(reason="unparseable", estimated_usd=0.01,
                            workspace_id="ws-1")

    assert verdict.allowed is False
    assert "additional_budget_usd is 0.0" in verdict.reason


def test_a_reask_budget_below_the_estimate_is_refused():
    """A ceiling the request can overshoot is a budget that lies."""
    policy = llm_mod.ReaskPolicy(allow_reask=True, approved_by="ops",
                                 additional_budget_usd=0.001)
    verdict = policy.decide(reason="unparseable", estimated_usd=0.5,
                            workspace_id="ws-1")

    assert verdict.allowed is False
    assert "does not cover" in verdict.reason


def test_a_reask_needs_somebody_to_charge():
    """An ownerless second completion is the §1 hole one layer down."""
    policy = llm_mod.ReaskPolicy(allow_reask=True, approved_by="ops",
                                 additional_budget_usd=1.0)
    verdict = policy.decide(reason="unparseable", estimated_usd=0.01,
                            workspace_id="")

    assert verdict.allowed is False
    assert "no workspace to charge it to" in verdict.reason


def test_a_fully_declared_reask_is_allowed():
    policy = llm_mod.ReaskPolicy(allow_reask=True, approved_by="release-bot",
                                 additional_budget_usd=0.05,
                                 reason="the prompt is regenerated from a schema")
    verdict = policy.decide(reason="unparseable", estimated_usd=0.01,
                            workspace_id="ws-1")

    assert verdict.allowed is True
    assert "release-bot" in verdict.reason
    assert verdict.to_dict()["reask_allowed"] is True


def test_the_legacy_boolean_alone_can_no_longer_buy_a_second_completion():
    """``reask_if_unparseable=True`` IS the hole. It must not open it again."""
    with pytest.raises(ValueError, match="reask_policy"):
        llm_mod.complete_json("sys", "user", reask_if_unparseable=True)


# ===========================================================================
# §7 -- at the outbound boundary: one POST unless a policy bought the second
# ===========================================================================


class _Wired:
    """A scripted chat-completions transport that records every POST."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.posts: list[dict] = []

    def __call__(self, url, **kw):
        self.posts.append({"url": url, "json": kw.get("json")})
        body = self.replies.pop(0) if self.replies else "{}"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": body}}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            request=httpx.Request("POST", str(url)))


@pytest.fixture()
def scripted_llm(monkeypatch):
    from app.engine.intelligence import llm_paid

    def install(replies: list[str]) -> _Wired:
        transport = _Wired(replies)
        monkeypatch.setattr(llm_mod.httpx, "post", transport)
        # The conftest's autouse fake drives `_effective`; keep it, it already
        # reports a configured, non-mock endpoint.
        monkeypatch.setattr(llm_paid, "DEFAULT_MAX_ATTEMPTS", 1,
                            raising=False)
        return transport

    return install


def _policy(**over) -> llm_mod.ReaskPolicy:
    base = {"allow_reask": True, "approved_by": "test",
            "additional_budget_usd": 1.0}
    base.update(over)
    return llm_mod.ReaskPolicy(**base)


def test_a_repairable_reply_costs_exactly_one_post(scripted_llm):
    """The whole point of the repair pass: one completion, not two."""
    transport = scripted_llm(['```json\n{"lines": ["hola"],}\n```'])
    report: dict = {}

    out = llm_mod.complete_json("sys", "user", workspace_id="ws-1",
                                report=report)

    assert out == {"lines": ["hola"]}
    assert len(transport.posts) == 1, transport.posts
    assert report["purchased"] is False
    assert report["rescued"] is True
    assert report["repairs"], report


def test_an_unrepairable_reply_without_a_policy_costs_exactly_one_post(
        scripted_llm):
    """Default conservative: fail rather than buy the same answer twice."""
    transport = scripted_llm(["not json at all"])

    with pytest.raises(llm_mod.LLMError) as caught:
        llm_mod.complete_json("sys", "user", workspace_id="ws-1")

    assert len(transport.posts) == 1, transport.posts
    assert "no re-ask policy permits" in str(caught.value)


def test_a_reask_costs_exactly_two_posts_and_records_both(scripted_llm):
    """Guard (f): the second POST needs the explicit policy -- and then happens."""
    transport = scripted_llm(["not json at all", '{"lines": ["hola"]}'])
    report: dict = {}

    out = llm_mod.complete_json("sys", "user", workspace_id="ws-1",
                                reask_policy=_policy(), report=report)

    assert out == {"lines": ["hola"]}
    assert len(transport.posts) == 2, transport.posts
    assert report["purchased"] is True
    # The first request's outcome is persisted alongside the second's cost.
    assert report["additional_budget_usd"] == 1.0
    assert report["reask_reason"], report


def test_the_reask_is_recorded_as_a_durable_paid_event(scripted_llm,
                                                       monkeypatch):
    """A second charge nobody can explain later is the failure this removes."""
    seen: list[dict] = []

    def record_event(workspace_id, kind, message, level="info", source="system",
                     data=None):
        seen.append({"workspace_id": workspace_id, "kind": kind,
                     "message": str(message), "level": level, "source": source,
                     "data": dict(data or {})})
        return {"id": "evt"}

    monkeypatch.setattr("app.services.events.record_event", record_event)
    transport = scripted_llm(["not json at all", '{"lines": ["hola"]}'])

    llm_mod.complete_json("sys", "user", workspace_id="ws-1",
                          reask_policy=_policy())

    reasks = [e for e in seen if e["kind"] == "paid.llm.reask"]
    assert len(reasks) == 1, [e["kind"] for e in seen]
    data = reasks[0]["data"]
    for field in ("phase", "reask_reason", "reask_allowed",
                  "additional_budget_usd", "estimated_usd", "submission_id",
                  "model", "cost_usd"):
        assert field in data, (field, sorted(data))
    assert data["reask_allowed"] is True
    assert data["purchased"] is True
    assert transport.posts


def test_a_rescue_is_recorded_so_no_one_pays_for_a_formatting_bug(scripted_llm,
                                                                  monkeypatch):
    seen: list[dict] = []

    def record_event(workspace_id, kind, message, level="info", source="system",
                     data=None):
        seen.append({"kind": kind, "data": dict(data or {})})
        return {"id": "evt"}

    monkeypatch.setattr("app.services.events.record_event", record_event)
    transport = scripted_llm(['```json\n{"lines": ["hola"],}\n```'])

    llm_mod.complete_json("sys", "user", workspace_id="ws-1")

    repairs = [e for e in seen if e["kind"] == "paid.llm.repair"]
    assert len(repairs) == 1, [e["kind"] for e in seen]
    assert repairs[0]["data"]["rescued"] is True
    assert repairs[0]["data"]["repairs"], repairs[0]["data"]
    assert len(transport.posts) == 1


def test_an_ambiguous_first_leg_still_costs_one_post_and_never_reasks(
        scripted_llm, monkeypatch):
    """Guard (b), restated for the JSON lane: ambiguity is not unparseability."""
    posts: list[str] = []

    def boom(url, **kw):
        posts.append(str(url))
        raise httpx.ReadTimeout("lost")

    monkeypatch.setattr(llm_mod.httpx, "post", boom)

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        llm_mod.complete_json("sys", "user", workspace_id="ws-1",
                              reask_policy=_policy())

    # ``complete()`` raises before returning text, so the re-ask is unreachable
    # and the whole call costs exactly one POST -- even with a policy that
    # WOULD have paid for a second one.
    assert len(posts) == 1, posts
    assert caught.value.kind == "AMBIGUOUS", caught.value.kind


# ===========================================================================
# §9 -- inventory coverage: discovered == classified, unexplained == 0
# ===========================================================================


def test_the_audit_agrees_with_the_source_in_both_directions():
    """A stale row and an un-audited call site are both failures."""
    from app.services import paid_jobs_audit as audit

    assert audit.verify_against_source() == [], audit.verify_against_source()


def test_every_discovered_billable_call_site_is_classified():
    """Discovered == classified. Nothing sits in the source unclassified.

    Discovery is INDEPENDENT of the table: it walks the literal money markers
    and the path keys, and asks the two sets about each other.
    """
    from app.services import paid_jobs_audit as audit

    discovered = {(module, marker.marker)
                  for module, markers in audit.MONEY_MARKERS.items()
                  for marker in markers}
    classified = {(item.module, marker)
                  for item in audit.PAID_PATHS
                  for marker in item.source_markers}

    unexplained = discovered - classified
    assert unexplained == set(), (
        "billable call sites no row classifies: "
        f"{sorted(f'{m}:{k}' for m, k in unexplained)}")


def test_every_money_marker_exists_in_the_module_it_names():
    """The inventory itself must not rot: a marker nobody can find is a lie."""
    from app.services import paid_jobs_audit as audit

    for module, markers in audit.MONEY_MARKERS.items():
        text = audit._source_text(module)  # noqa: SLF001 - the audit's own reader
        assert text, f"{module} does not exist on disk"
        for entry in markers:
            assert entry.marker in text, (
                f"{module}: {entry.marker!r} is gone but still inventoried")
            assert entry.why.strip(), f"{module}: {entry.marker!r} has no reason"


def test_every_billable_row_declares_its_safety_standing():
    """A billable row is either covered, or covered-with-a-named-limitation.

    The limitation is allowed to be irreducible -- a chat completion really
    cannot be reconciled -- but it has to be SPELLED OUT and name the code that
    implements it, which is what makes it a classification rather than a shrug.
    """
    from app.services import paid_jobs_audit as audit

    for item in audit.billable_paths():
        assert item.coverage in audit.COVERAGE, item.key
        assert item.billing_note.strip(), f"{item.key}: no billing reason"
        if not item.covered:
            assert item.gap.strip(), f"{item.key}: uncovered with no gap"
            assert len(item.gap) > 80, (
                f"{item.key}: the gap is too thin to be a real classification")


def test_no_billable_row_claims_coverage_on_a_module_that_does_not_import():
    """``covered`` is COMPUTED from the source; this proves the computation
    reads the source rather than a hand-written boolean."""
    from app.services import paid_jobs_audit as audit

    for item in audit.billable_paths():
        if item.is_composite:
            continue
        for site in item.paid_jobs_sites:
            assert audit._source_path(site).is_file(), (  # noqa: SLF001
                f"{item.key}: {site} does not exist")
            assert audit._carries_paid_contract(site), (  # noqa: SLF001
                f"{item.key}: {site} claims coverage without the contract")


def test_the_four_migrated_providers_carry_the_shared_helper():
    """§3, asserted on the source: each provider really routes through it."""
    from app.services import paid_jobs_audit as audit

    for module in ("app.providers.images", "app.providers.tts",
                   "app.providers.avatar", "app.providers.broll"):
        text = audit._source_text(module)  # noqa: SLF001
        assert "paid_operation(" in text, module
        assert "absorb_paid_failure(" in text, (
            f"{module}: the failure classification is still local")

    # Only the TTS lane has a legitimately non-billable case (a LOCAL operator
    # server), so it is the only one that declares an authority. The other three
    # default to WORKSPACE_OWNED, which is the only default that can refuse.
    tts = audit._source_text("app.providers.tts")  # noqa: SLF001
    assert ".declared(" in tts, "a local operator server must declare itself free"
    assert "EXPLICIT_NONBILLABLE" in tts, tts[:200]


def test_no_migrated_provider_keeps_its_own_settle_branch():
    """The duplication is gone: no provider re-derives the three-way branch."""
    from app.services import paid_jobs_audit as audit

    for module in ("app.providers.images", "app.providers.tts",
                   "app.providers.avatar", "app.providers.broll"):
        text = audit._source_text(module)  # noqa: SLF001
        assert "def settle(" not in text, (
            f"{module} still hand-rolls a settle closure")
        assert "def _book_cost(" not in text, (
            f"{module} still books cost outside the reservation")
        assert "cost_service.track_cost(" not in text, (
            f"{module} still inserts a second ledger row")


def test_the_helper_grew_no_provider_specific_arguments():
    """Work 15.8's boundary, re-asserted: a helper that accepted the request
    would be the ``url/payload/status/cancel`` mega-adapter."""
    from app.services.paid_provider import paid_operation

    params = set(inspect.signature(paid_operation).parameters)
    assert params == {"provider", "operation", "workspace_id", "category",
                      "estimated_cost", "idempotency", "reconciliation",
                      "reservation_extra"}, params
    forbidden = {"url", "payload", "body", "headers", "status", "cancel",
                 "fetch", "http", "method", "response", "poll", "parse"}
    assert not forbidden & params, forbidden & params


def test_the_audit_keeps_the_llm_row_explicit_about_being_unreconcilable():
    """§9: ``llm.complete`` stays UNRECONCILABLE and is still COVERED.

    Coverage is a safety classification, not a claim that a lost chat completion
    can be looked up afterwards. It cannot be, and pretending otherwise is the
    error this row exists to prevent.
    """
    from app.services import paid_jobs_audit as audit

    row = audit.path("llm.complete")
    assert row is not None and row.billable
    assert row.covered, "the LLM lane must be classified, not silently omitted"
    assert "UNRECONCILABLE" in row.gap, row.gap
    assert "llm_reconciliation_capability" in row.gap, row.gap


def test_the_dubbing_row_describes_the_reask_it_can_no_longer_hide():
    """The row that documented the provider's hidden second charge must now
    describe the policy that replaced it."""
    from app.services import paid_jobs_audit as audit

    row = audit.path("providers.dubbing.translate_segments")
    assert row is not None
    assert "re-ask" in row.gap, row.gap
    assert "ReaskPolicy" in row.gap, row.gap


def test_the_inventory_totals_are_the_ones_the_matrix_publishes():
    """§9: discovered == classified, unexplained == 0, and the doc agrees.

    ``uncovered_billable == 0`` is the target of this work and is asserted, not
    hoped for: every billable row is either covered by a site that really
    imports the contract, or composite over leaves that are.
    """
    from app.services import paid_jobs_audit as audit

    stats = audit.summary()
    assert stats["uncovered_billable"] == 0, stats
    assert stats["covered"] == stats["billable"], stats
    for key in stats["uncovered_keys"]:
        row = audit.path(key)
        assert row is not None and row.gap.strip(), key
    table = audit.render_table()
    assert table.count("\n") == len(audit.PAID_PATHS) + 1


def test_the_published_totals_match_the_computed_ones():
    """The matrix document quotes numbers; they must be the numbers."""
    import re
    from pathlib import Path

    from app.services import paid_jobs_audit as audit

    doc = (Path(__file__).resolve().parents[2] / "docs"
           / "YMONEY_PAID_PROVIDER_MATRIX.md").read_text(encoding="utf-8")
    stats = audit.summary()

    def quoted(label: str) -> int:
        match = re.search(rf"\|\s*{re.escape(label)}\s*\|\s*\*?\*?(\d+)\*?\**",
                          doc)
        assert match, f"the matrix no longer quotes a {label!r} row"
        return int(match.group(1))

    assert quoted("Audited operations") == stats["total"]
    assert quoted("Billable") == stats["billable"]
    assert quoted("Billable and covered") == stats["covered"]
    assert quoted("Billable and uncovered") == stats["uncovered_billable"]


# ===========================================================================
# §5 -- the one reattach path in the repo that still double-books, named
# ===========================================================================


def test_the_mpt_reattach_path_is_still_a_second_ledger_row():
    """A KNOWN VIOLATION, pinned so it cannot be forgotten.

    ``engine/agents/production.py`` skips the reservation when it reattaches to
    an existing engine task (:195, ``if not existing_task: paid.authorize()``)
    -- which is right, the work is already paid for. But at completion it books
    with ``self.track_cost(...)`` instead of adopting the original reservation
    (:456-469), so every reattach adds a SECOND ``cost_entries`` row for the
    same remote task.

    Exactly-once ACCOUNTING is violated for the mpt lane. The fix belongs in
    ``production.py`` (call :func:`reattach_by_remote_id` on
    ``engine_task_id``) and that file is not in this worker's lane, so the
    violation is reported and tested rather than silently patched.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "engine" / "agents"
              / "production.py").read_text(encoding="utf-8")

    assert "if not existing_task:" in source and "paid.authorize()" in source, (
        "the mpt reservation guard changed shape; re-read this test")
    assert "reattach_by_remote_id" in source, (
        "production.py no longer recovers the existing cost row on a reattach, "
        "so one remote job can be charged twice")
    assert 'self.track_cost(ctx, "video", estimate' not in source, (
        "the reattach path books a SECOND cost row for a remote task that "
        "already has one; that is the double-book this test was written for")
    assert "reattach_unowned" in source, (
        "an unowned reattach must surface as an unknown exposure rather than "
        "quietly charging again")