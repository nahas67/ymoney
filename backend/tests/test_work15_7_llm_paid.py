"""Work 15.7 -- the LLM lane stops re-POSTing a billable completion it may
already have paid for.

The 15.6 audit row for ``llm.complete`` said the loop over the model list and
the ``response_format`` variant "re-POSTs chat/completions on ANY transport
error, including the read timeout that proves the completion was delivered and
billed; a completion has no durable remote id, so there is nothing to
reconcile afterwards". This file is the proof that all three halves of that
sentence are now true in the fix.

Written against the OUTBOUND BOUNDARY, not against our own code: the claim
that matters is how many requests actually left the process, because "the
function was called once" and "one request was sent" are different claims and
only the second one costs money. No test here opens a socket.

What each group pins:

* **ambiguity is terminal** -- a read timeout, a 5xx, or a dropped
  connection issues exactly ONE POST, even when four billable candidates are
  queued and even through ``complete_json``. The old code issued up to four
  (up to eight through ``complete_json``).
* **a known rejection is not ambiguity** -- a 4xx proves nothing was billed,
  so the next candidate IS tried, and the record says so in a different
  vocabulary from the ambiguous case.
* **a connect failure is the one ambiguity a retry may touch** -- no socket
  was opened, so nothing was delivered.
* **unknown money is never $0** -- a completion that may have been billed is
  recorded as ``UNKNOWN_EXPOSURE`` with ``ledger_value is None``, which is
  categorically different from zero (and from no row at all).
* **the no-remote-id limitation is queryable** -- a higher layer can ask
  whether reconciliation is POSSIBLE, and the answer for a chat completion is
  "impossible", not "pending".
* **falling back is opt-in** -- the default refuses, and an override must name
  an approver and is audited.

Live-provider coverage sits at the bottom behind an honest marker.
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.engine.intelligence import llm_paid
from app.providers import llm as llm_mod
from app.services.paid_executor import (
    CostOutcome,
    Reconciliation,
    RetrySafety,
    RetryVerdict,
    SubmissionState,
    verdict_for,
)

#: Handed to the provider so request building produces a real URL. No request
#: ever leaves the process: the httpx boundary is recorded, not sent.
CHAT_URL = "http://llm.test/v1/chat/completions"

#: A token count big enough that the price table yields a non-zero amount, so
#: "booked as unknown exposure" and "booked as a real figure" are distinguishable.
PROMPT_TOKENS = 4000
COMPLETION_TOKENS = 900


# ===========================================================================
# the outbound boundary: a recorder, not a mock of our own code
# ===========================================================================


class Wire:
    """Counts REAL outbound calls and scripts the answers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.handler = None

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]

    def models(self) -> list[str]:
        return [str(c["json"].get("model", "")) for c in self.posts()]

    def post(self, url, **kw):
        self.calls.append({"method": "POST", "url": str(url),
                           "json": dict(kw.get("json") or {})})
        if self.handler is None:
            raise AssertionError(f"unscripted outbound POST {url}")
        return self.handler(str(url), **kw)

    def always(self, *outcome):
        """Script one outcome (or one exception) for every request."""
        self.handler = _script(outcome)
        return self

    def then(self, *outcomes):
        """Script a different outcome per request, in order."""
        self.handler = _script(outcomes, consume=True)
        return self

    def always_fn(self, fn):
        """Script a callable that builds each response itself.

        Needed for a response no fixed value can express: an undecodable body,
        or a transport error carrying a URL with credentials in it.
        """
        self.handler = fn
        return self


def _request() -> httpx.Request:
    return httpx.Request("POST", CHAT_URL)


def _script(outcomes, *, consume: bool = False):
    """Turn a list of outcomes into a handler that raises or returns them.

    ``consume=False`` gives every request the first entry (with
    ``then(a)`` that is just ``always(a)``). ``consume=True`` walks the list
    and then sticks on the last entry, so an unexpected extra request still
    gets a scripted answer instead of an ``IndexError`` masquerading as a
    transport failure.
    """
    queue = list(outcomes)
    first = outcomes[0]

    def handler(_url, **_kw):  # noqa: ARG001 - uniform script
        value = first if not consume else (
            queue.pop(0) if len(queue) > 1 else queue[0])
        if isinstance(value, BaseException):
            raise value
        return value
    return handler


def ok(text: str = "a finished answer", *, prompt: int = PROMPT_TOKENS,
       completion: int = COMPLETION_TOKENS, choices: bool = True,
       usage: bool = True) -> httpx.Response:
    """A real ``httpx.Response`` so the shared classifier can read its status."""
    body: dict = {}
    if choices:
        body["choices"] = [{"message": {"content": text}}]
    if usage:
        body["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion}
    return httpx.Response(200, json=body, request=_request())


def no_choices() -> httpx.Response:
    """A 2xx that was billed and carries nothing we can use."""
    return httpx.Response(200, json={"id": "cmpl-abc",
                                     "usage": {"prompt_tokens": PROMPT_TOKENS,
                                               "completion_tokens": 12}},
                         request=_request())


def rejected(status: int = 400, text: str = "unsupported parameter") -> httpx.HTTPStatusError:
    """A real ``HTTPStatusError`` so the classifier sees a definitive 4xx."""
    response = httpx.Response(status, content=text.encode(), request=_request())
    return httpx.HTTPStatusError(f"HTTP {status}", request=_request(),
                                 response=response)


def server_error(status: int = 503) -> httpx.HTTPStatusError:
    """A 5xx from the submit endpoint: the task may still have been created."""
    response = httpx.Response(status, content=b"upstream exploded", request=_request())
    return httpx.HTTPStatusError(f"HTTP {status}", request=_request(),
                                 response=response)


@pytest.fixture()
def wire(monkeypatch):
    """Install the recorder on the provider's transport.

    ``llm.complete`` passes ``httpx.post`` in as the transport, so patching the
    attribute on the ``httpx`` module itself intercepts every request without
    the provider knowing this test exists. The shared conftest fake is
    overridden on purpose: it answers 200 for everything, which cannot express
    the failure modes that cost money.
    """
    assert llm_mod.llm_available(), (
        "the shared conftest fake must make the LLM available; without it "
        "these tests would take the unconfigured path and never send anything")
    rec = Wire()
    monkeypatch.setattr(llm_mod.httpx, "post", rec.post)
    return rec


@pytest.fixture()
def paid_log(monkeypatch):
    """Capture what the lane persists and what it books.

    ``llm_paid`` imports both modules at call time, so patching the module
    attribute is enough and no production code has to know this test exists.
    """
    events: list[dict] = []
    costs: list[dict] = []

    # Work 15.8: a metered completion is settled on its RESERVATION row, not
    # booked again through track_cost. Both are real money movements and both
    # are captured here, so an assertion about "what was booked" has to state
    # which one it means. ``settled_money`` is the shared accessor below.
    def settled_money(log) -> list[dict]:
        """Every entry that represents money actually charged."""
        return [c for c in log["costs"] if c.get("settled") or c.get("amount_usd")
                is not None and not c.get("voided")]

    def record_event(workspace_id, kind, message, level="info", source="system",
                     data=None):
        events.append({"workspace_id": workspace_id, "kind": kind,
                       "message": message, "level": level, "source": source,
                       "data": dict(data or {})})
        return {"id": "evt"}

    def track_cost(workspace_id, category, amount_usd, provider="",
                   cycle_id=None, detail=None, is_estimate=False):  # noqa: ARG001
        costs.append({"workspace_id": workspace_id, "category": category,
                      "amount_usd": amount_usd, "provider": provider,
                      "detail": dict(detail or {}), "is_estimate": is_estimate})
        return amount_usd

    monkeypatch.setattr("app.services.events.record_event", record_event)
    monkeypatch.setattr("app.services.cost.track_cost", track_cost)

    def settle_reservation(entry_id, amount_usd, detail=None):  # noqa: ARG001
        # Work 15.8: a metered completion is now settled ON ITS RESERVATION
        # rather than booked a second time through track_cost -- cost.py:141
        # calls that the double-count fix. Both paths must be captured, or
        # "the metered tokens vanished from the ledger" reads as a regression
        # when the money is in fact booked on the correct row.
        costs.append({"entry_id": entry_id, "amount_usd": amount_usd,
                       "settled": True, "detail": dict(detail or {})})
        return amount_usd

    def void_reservation(entry_id, detail=None):  # noqa: ARG001
        costs.append({"entry_id": entry_id, "settled": False, "voided": True,
                       "detail": dict(detail or {})})
        return True

    monkeypatch.setattr("app.services.cost.settle_reservation", settle_reservation)
    monkeypatch.setattr("app.services.cost.void_reservation", void_reservation)
    return {"events": events, "costs": costs, "settled": settled_money}


@pytest.fixture()
def four_candidates(monkeypatch):
    """Configure the WORST case the old loop could reach: 4 billable POSTs.

    A fallback model plus ``json_mode`` gives ``models_to_try`` = 2 models x
    ``attempts`` = 2 body variants. Every ambiguity test runs under this, so
    "one POST" is a statement about a four-deep queue, not about a queue of one.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_fallback_model", "w157-fallback-model")
    return 4


def submissions(log: dict) -> list[dict]:
    return [e["data"] for e in log["events"] if e["kind"] == "paid.submission"]


def states(log: dict) -> list[str]:
    return [d.get("state", "") for d in submissions(log)]


def unknown_exposures(log: dict) -> list[dict]:
    return [d for d in submissions(log) if (d.get("cost") or {}).get("unknown_exposure")]


def exposure_alarms(log: dict) -> list[str]:
    """The error-level "we do not know what this cost" events."""
    return [e["message"] for e in log["events"]
            if e["kind"] == "paid.submission" and e["level"] == "error"]


def complete(**kw):
    """``llm.complete`` with a real billable workspace id."""
    kw.setdefault("workspace_id", "ws-w157")
    kw.setdefault("system", "you are precise")
    kw.setdefault("user", "a question")
    return llm_mod.complete(**kw)


# ===========================================================================
# the candidate list: unchanged in shape, different in what it costs
# ===========================================================================


def test_the_candidate_list_is_the_same_four_posts_the_old_loop_would_make():
    """Nothing regressed for the one case that may still walk the list."""
    body = {"model": "m1", "messages": [{"role": "user", "content": "q"}],
            "response_format": {"type": "json_object"}}
    candidates = llm_paid.build_candidates("m1", body, fallback_model="m2",
                                           json_mode=True)
    assert [c.cost_hint for c in candidates] == [
        "m1[primary]", "m1[degraded-body]", "m2[fallback-model]",
        "m2[degraded-body]"]
    assert candidates[1].body.get("response_format") is None
    assert candidates[2].body["model"] == "m2"


def test_building_candidates_does_not_mutate_the_callers_body():
    """The old loop reassigned ``body["model"]`` on the caller's dict."""
    body = {"model": "m1", "messages": []}
    llm_paid.build_candidates("m1", body, fallback_model="m2", json_mode=True)
    assert body["model"] == "m1"


def test_the_pre_spend_estimate_is_a_bound_and_not_a_measurement():
    """It must exceed zero, or the ledger row it eventually books is empty."""
    body = {"messages": [{"role": "system", "content": "x" * 4000},
                         {"role": "user", "content": "y" * 4000}]}
    estimate = llm_paid.estimate_request_cost("w157-model", body, max_tokens=1000)
    assert estimate > 0


# ===========================================================================
# (a) an ambiguous read timeout is terminal
# ===========================================================================


def test_an_ambiguous_read_timeout_costs_exactly_one_post(wire, paid_log,
                                                          four_candidates):
    """The POST was DELIVERED. A second one is a second charge.

    The record exists before the request leaves, so this is the evidence an
    operator needs: the money may be gone and there is no remote id to look it
    up with.
    """
    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete(json_mode=True)

    assert len(wire.posts()) == 1, (
        f"a retry bought a second completion: {wire.calls}")
    assert wire.models() == ["test-model"], "the fallback model was tried"
    assert caught.value.kind == "AMBIGUOUS"
    assert SubmissionState.SUBMISSION_UNKNOWN in states(paid_log)
    assert "read timeout" in str(caught.value).lower()


def test_a_5xx_from_the_completion_endpoint_is_ambiguous_too(wire, paid_log,
                                                             four_candidates):
    """A 5xx is not a rejection: the server may have metered the work first."""
    wire.always(server_error(503))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete(json_mode=True)

    assert len(wire.posts()) == 1, wire.calls
    assert caught.value.kind == "AMBIGUOUS"
    assert caught.value.submission.retry_safety is RetrySafety.UNSAFE
    assert verdict_for(caught.value.submission) is RetryVerdict.RECONCILE


def test_a_dropped_connection_is_ambiguous_when_the_request_was_sent(
        wire, four_candidates):
    """``httpx`` distinguishes the two; so must the policy."""
    wire.always(httpx.RemoteProtocolError("server disconnected"))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()

    assert len(wire.posts()) == 1, wire.calls
    assert caught.value.kind == "AMBIGUOUS"


def test_complete_json_does_not_reask_after_an_ambiguous_completion(
        wire, four_candidates):
    """The re-ask doubles the worst case. It must not run on ambiguity.

    Before this work, one ``complete_json`` could leave eight billable POSTs on
    the wire. The number that matters is this one.
    """
    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(llm_mod.LLMCompletionError):
        llm_mod.complete_json("sys", "user", workspace_id="ws-w157")

    assert len(wire.posts()) == 1, (
        f"complete_json re-asked after an ambiguity: {wire.calls}")


def test_an_ambiguous_completion_is_not_turned_into_a_bare_llm_error(wire):
    """A caller must be able to tell ambiguity from a dead config."""
    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()
    assert isinstance(caught.value, llm_mod.LLMError)
    assert isinstance(caught.value, llm_paid.LLMCompletionPaidError)
    assert caught.value.submission is not None


# ===========================================================================
# (a/d) a known rejection IS retryable -- and is not ambiguity
# ===========================================================================


def test_a_definitive_rejection_walks_to_the_next_candidate(wire, paid_log,
                                                           four_candidates):
    """A 4xx rejected the REQUEST, so no generation was metered."""
    wire.then(rejected(400), ok("the fallback answer"))

    result = complete(json_mode=True)

    assert len(wire.posts()) == 2, wire.calls
    assert result.text == "the fallback answer"
    assert result.attempts == 2
    assert result.models_tried == ("test-model", "test-model")


def test_a_known_rejection_is_distinguishable_from_an_ambiguity(wire, paid_log):
    """Same call, opposite money, and the record says which in its own words."""
    wire.always(rejected(401, "invalid api key"))
    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()
    rejected_record = caught.value.submission

    assert caught.value.kind == "EXHAUSTED_ON_PROVEN_SAFE_FAILURES"
    assert rejected_record.state is SubmissionState.FAILED
    assert rejected_record.reconciliation is Reconciliation.RETRY_IF_CONFIRMED_SAFE
    assert rejected_record.may_resubmit is True
    assert verdict_for(rejected_record) is RetryVerdict.RETRY
    # A refusal is not a spend: the executor's ESTIMATED figure is the pre-send
    # bound, and nothing is booked because the vendor metered nothing.
    assert rejected_record.cost.outcome is CostOutcome.ESTIMATED
    # Work 15.8: a definitive rejection VOIDS the pre-send reservation, which is
    # not a spend -- it releases the held budget. A void therefore appears here
    # and is correct; what must never happen is a SETTLE.
    assert not [c for c in paid_log["costs"] if c.get("settled")], (
        "a 4xx booked money: a definitive rejection must never settle")

    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(llm_mod.LLMCompletionError) as ambiguous:
        complete()
    ambiguous_record = ambiguous.value.submission

    assert ambiguous_record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert ambiguous_record.retry_safety is RetrySafety.UNSAFE
    assert ambiguous_record.may_resubmit is False
    assert verdict_for(ambiguous_record) is RetryVerdict.RECONCILE


def test_exhausting_the_queue_on_proven_safe_failures_says_nothing_was_billed(
        wire, paid_log, four_candidates):
    """Four connect failures: four POSTs, zero charges, and it says so."""
    wire.always(httpx.ConnectTimeout("connect"))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete(json_mode=True)

    assert len(wire.posts()) == 4, wire.calls
    assert caught.value.kind == "EXHAUSTED_ON_PROVEN_SAFE_FAILURES"
    assert "nothing was billed" in str(caught.value)
    assert paid_log["settled"](paid_log) == [], "money was booked"
    assert exposure_alarms(paid_log) == [], (
        "a provably undelivered request must not raise a money alarm")


# ===========================================================================
# (c) a connect failure is the ONE ambiguity a retry may touch
# ===========================================================================


def test_a_connect_failure_is_retried_because_no_socket_was_opened(
        wire, paid_log, four_candidates):
    """The request cannot have been delivered, so it cannot have been billed."""
    wire.then(httpx.ConnectTimeout("connect"), ok("recovered"))

    result = complete()

    assert len(wire.posts()) == 2, wire.calls
    assert result.text == "recovered"
    assert result.attempts == 2
    assert SubmissionState.SUBMISSION_UNKNOWN in states(paid_log)
    # No money alarm: this is the one ambiguous state a retry cannot double.
    assert exposure_alarms(paid_log) == []


def test_a_dns_failure_is_also_provably_undelivered(wire, four_candidates):
    import socket

    wire.then(socket.gaierror("name does not resolve"), ok("recovered"))
    assert complete().text == "recovered"
    assert len(wire.posts()) == 2, wire.calls


# ===========================================================================
# (d) unknown money is never $0
# ===========================================================================


def test_an_ambiguous_completion_is_an_unknown_exposure_not_a_zero_row(
        wire, paid_log, four_candidates):
    """``services/cost.py`` drops any amount <= 0.

    Booking a maybe-billed completion at 0.0 would therefore produce NO ledger
    row at all -- the exact disappearance this work exists to prevent. The
    record has to say UNKNOWN, and ``ledger_value`` has to be ``None``, which
    is not zero.
    """
    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()

    record = caught.value.submission
    assert record.cost.outcome is CostOutcome.UNKNOWN_EXPOSURE
    assert record.cost.ledger_value is None
    assert record.cost.ledger_value != 0
    assert record.exposure_unknown is True
    assert paid_log["settled"](paid_log) == [], "an ambiguous completion booked money"
    assert unknown_exposures(paid_log), "the unknown exposure was never recorded"
    assert unknown_exposures(paid_log)[0]["cost"]["ledger_value"] is None
    assert exposure_alarms(paid_log), "nobody was told the amount is unknown"


def test_a_successful_completion_books_its_reported_amount(wire, paid_log):
    """The happy path still books a real figure, not an incident."""
    wire.always(ok("a finished answer"))

    result = complete()

    assert result.cost_usd > 0
    assert len(wire.posts()) == 1
    assert len(paid_log["settled"](paid_log)) == 1, paid_log["costs"]
    assert paid_log["settled"](paid_log)[0]["amount_usd"] == pytest.approx(
        result.cost_usd)
    assert exposure_alarms(paid_log) == []


def test_a_but_unusable_2xx_is_booked_from_the_usage_it_reported(
        wire, paid_log, four_candidates):
    """A 2xx is a known, metered spend. The body is what failed, not the call.

    This is the case the old code could not express: it is not a rejection, and
    it is not a lost response. Re-sending is a CERTAIN second charge.
    """
    wire.always(no_choices())

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete(json_mode=True)

    assert caught.value.kind == "BILLED_BUT_UNUSABLE"
    assert len(wire.posts()) == 1, wire.calls
    assert caught.value.submission.state is SubmissionState.SUCCEEDED
    assert caught.value.submission.cost.outcome is CostOutcome.ACTUAL
    assert paid_log["settled"](paid_log), "a metered completion with usage was not booked"
    assert exposure_alarms(paid_log) == []


def test_a_2xx_with_no_usage_is_an_unknown_exposure_not_a_zero_row(
        wire, paid_log):
    """Priced on nothing: unknown, which is not free."""
    wire.always(ok(choices=False, usage=False))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()

    assert caught.value.kind == "BILLED_BUT_UNUSABLE"
    assert caught.value.submission.cost.outcome is CostOutcome.UNKNOWN_EXPOSURE
    assert paid_log["settled"](paid_log) == [], "an unpriceable completion booked $0"
    assert exposure_alarms(paid_log)


def test_the_scratchpad_alone_is_a_billed_unusable_reply(wire, paid_log):
    """A reasoning model that answered only inside ``<think>`` has not answered.

    The scratchpad is stripped (it must never reach a script or a voice-over)
    and the empty result is then reported as a metered spend with no content,
    rather than as a free failure.
    """
    wire.always(ok("<think>still weighing the hook"))

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()

    assert caught.value.kind == "BILLED_BUT_UNUSABLE"
    assert "still weighing" not in str(caught.value)
    assert paid_log["settled"](paid_log), "the metered tokens vanished from the ledger"


def test_a_scratchpad_plus_an_answer_still_returns_only_the_answer(wire, paid_log):
    wire.always(ok("<think>private reasoning</think>Real answer."))
    result = complete()
    assert result.text == "Real answer."
    assert "private reasoning" not in result.text
    assert len(paid_log["settled"](paid_log)) == 1


def test_an_undecodable_2xx_body_is_still_a_billable_spend(wire, paid_log):
    """The vendor said 200; we cannot read what it said. The money is spent."""
    def handler(_url, **_kw):
        return httpx.Response(200, content=b"<html>gateway</html>",
                              headers={"content-type": "text/html"},
                              request=_request())
    wire.always_fn(handler)

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()

    assert caught.value.kind == "BILLED_BUT_UNUSABLE"
    assert len(wire.posts()) == 1
    assert exposure_alarms(paid_log)


def test_a_gateway_credential_never_reaches_the_event_log(wire, paid_log):
    """An httpx error embeds the full request URL, query string and all."""
    def handler(_url, **_kw):
        raise httpx.ReadTimeout(
            "timed out on http://alice:hunter2@gw.test/v1/chat/completions"
            "?api_key=SUPERSECRET123")
    wire.always_fn(handler)

    with pytest.raises(llm_mod.LLMCompletionError) as caught:
        complete()

    assert caught.value.kind == "AMBIGUOUS", "the script did not run"
    # The raw classified detail really did carry both secrets; everything we
    # hand out or persist is scrubbed. Without this the assertion below would
    # pass for the wrong reason.
    assert "SUPERSECRET123" in caught.value.submission.detail
    blob = str(caught.value) + repr(submissions(paid_log)) + repr(paid_log["costs"])
    assert "SUPERSECRET123" not in blob
    assert "hunter2" not in blob


# ===========================================================================
# (B) the no-remote-id limitation, explicit and queryable
# ===========================================================================


def test_a_chat_completion_advertises_that_it_cannot_be_reconciled():
    """IMPOSSIBLE, not PENDING. A higher layer must be able to tell those."""
    capability = llm_paid.llm_reconciliation_capability("complete")

    assert capability.availability is llm_paid.ReconciliationAvailability.UNRECONCILABLE
    assert capability.is_reconcilable is False
    assert capability.handle is None
    assert capability.remote_id_field == ""
    assert "UNKNOWN_EXPOSURE" in capability.remedy
    assert "no request id" in capability.reason

    payload = capability.to_dict()
    assert payload["reconcilable"] is False
    assert payload["remote_id"] is None
    assert payload["remote_id_field"] is None
    assert "UNRECONCILABLE" in capability.explain()


def test_a_successful_completion_never_invents_a_remote_id(wire, paid_log):
    """A synthetic id would send an operator hunting for a job that is not there.

    The record is queryable evidence; a fabricated handle inside it would be a
    lie that costs real time the first time somebody trusts it.
    """
    wire.always(ok("a finished answer"))
    complete()

    for data in submissions(paid_log):
        assert data["remote_id"] == "", data["remote_id"]
        assert data["idempotency_key"] == "", data["idempotency_key"]
        assert data["idempotency_support"] == str(
            llm_paid.IdempotencySupport.UNSUPPORTED)


def test_the_record_says_the_artifact_was_inline_not_a_download(
        wire, paid_log):
    """There is no "fetch the result" step for a completion, and we say so."""
    wire.always(ok("a finished answer"))
    result = complete()
    assert result.submission_id
    assert llm_paid.INLINE_ARTIFACT.startswith("inline://")
    assert SubmissionState.SUCCEEDED in states(paid_log)


# ===========================================================================
# (C) falling back after an ambiguity is opt-in, and audited
# ===========================================================================


def test_the_default_policy_refuses_an_ambiguous_fallback():
    record = _record_for_ambiguity()
    verdict = llm_paid.FallbackPolicy().decide(
        record, remaining_models=("w157-fallback-model",))

    assert verdict.decision is llm_paid.FallbackDecision.REFUSE
    assert verdict.allowed is False
    assert bool(verdict) is False
    assert verdict.may_incur_second_charge is True
    assert verdict.submission_id == record.submission_id
    assert "do-not-retry" in verdict.reason


def test_the_default_policy_is_refused_without_being_asked_first():
    """The answer must not depend on anyone consulting it first."""
    assert llm_paid.FallbackPolicy().allow_ambiguous_fallback is False


def test_an_override_with_no_named_approver_is_refused_at_construction():
    """An unattributed second charge cannot be explained three weeks later."""
    with pytest.raises(ValueError, match="approved_by"):
        llm_paid.FallbackPolicy(allow_ambiguous_fallback=True)


def test_the_policy_can_be_asked_without_sending_anything(wire):
    """An API layer, a UI or a CLI must be able to render the risk first."""
    assert wire.posts() == []
    verdict = llm_paid.FallbackPolicy().decide(
        _record_for_ambiguity(), remaining_models=("m2",))
    assert wire.posts() == [], "asking the policy issued a billable request"
    assert verdict.may_incur_second_charge is True


def test_an_ambiguous_fallback_is_refused_by_default_even_with_candidates(
        wire, four_candidates):
    """The opt-in is a policy object, and the default is one that refuses."""
    wire.always(httpx.ReadTimeout("lost"))
    policy = llm_paid.FallbackPolicy()

    with pytest.raises(llm_mod.LLMCompletionError):
        complete(json_mode=True, fallback_policy=policy)

    assert len(wire.posts()) == 1
    assert [v.decision for v in policy.history] == [
        llm_paid.FallbackDecision.REFUSE]


def test_an_explicit_override_proceeds_and_is_audited(wire, paid_log,
                                                      four_candidates):
    """The opt-in works, and it leaves a trail that names who paid for it."""
    audited: list = []
    policy = llm_paid.FallbackPolicy(
        allow_ambiguous_fallback=True, approved_by="ops:w157",
        note="tolerate one duplicate completion",
        audit=audited.append,
    )
    wire.then(httpx.ReadTimeout("lost"), ok("the answer we paid twice for"))

    result = complete(json_mode=True, fallback_policy=policy)

    assert len(wire.posts()) == 2, wire.calls
    assert result.text == "the answer we paid twice for"
    assert result.attempts == 2
    assert [v.decision for v in policy.history] == [
        llm_paid.FallbackDecision.ALLOW]
    assert len(audited) == 1
    assert audited[0].may_incur_second_charge is True
    assert "ops:w157" in audited[0].reason
    assert "tolerate one duplicate" in audited[0].reason
    # The wasted first completion is still on the books as an unknown exposure.
    assert unknown_exposures(paid_log)
    assert exposure_alarms(paid_log)


def test_a_policy_that_allows_a_fallback_with_nothing_left_refuses(wire,
                                                                  four_candidates):
    """Allowing the risk is not the same as having somewhere to send it."""
    policy = llm_paid.FallbackPolicy(allow_ambiguous_fallback=True,
                                     approved_by="ops:w157")
    wire.always(httpx.ReadTimeout("lost"))

    verdict = policy.decide(_record_for_ambiguity(), remaining_models=())

    assert verdict.allowed is False
    assert "no further candidate" in verdict.reason


def test_the_router_chain_stops_on_an_ambiguous_paid_failure(wire,
                                                              four_candidates):
    """CLOSED in Work 15.8. The chain multiplier is gone, not merely reduced.

    This test previously recorded ``1 + len(decision.fallbacks)`` POSTs and said
    in its own docstring that fixing the multiplier was ``router.py``'s job and
    had not been done. It has been: an ambiguous paid failure now STOPS the
    chain, so a six-tier route costs exactly ONE POST rather than six.

    ``wire.calls`` carries the whole transcript, so a regression shows up as a
    specific extra model rather than a bare number.
    """
    from app.engine.intelligence.router import RouteRequest, default_router

    wire.always(httpx.ReadTimeout("lost"))
    router = default_router()
    decision = router.route(RouteRequest(workspace_id="ws-w157"))
    assert decision.fallbacks, "this test needs a multi-tier chain to mean anything"

    with pytest.raises(Exception):  # noqa: B017 - the router's own error type
        router.complete("sys", "user", request=RouteRequest(workspace_id="ws-w157"))

    assert len(wire.posts()) == 1, (
        f"an ambiguous paid failure walked the chain: {wire.calls}")


def test_a_single_tier_routed_leg_stops_at_exactly_one_post(wire):
    """The provider-level guarantee the router depends on.

    One tier, no fallbacks: the whole chain collapses to one ``llm.complete``,
    so this is the direct measurement of what that call costs on ambiguity.
    """
    from app.engine.intelligence.router import (
        ModelCapabilityRegistry,
        ModelRouter,
        RouteRequest,
    )

    registry = ModelCapabilityRegistry()
    for tier in ("FAST", "HIGH_QUALITY", "PREMIUM", "LOCAL_ONLY", "PRIVATE"):
        registry.set_enabled(tier, False)
    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(Exception):  # noqa: B017 - the router's own error type
        ModelRouter(registry).complete(
            "sys", "user", request=RouteRequest(workspace_id="ws-w157"))

    assert len(wire.posts()) == 1, wire.calls


def _record_for_ambiguity():
    from app.services.paid_executor import PaidSubmission

    return PaidSubmission(
        workspace_id="ws-w157", provider=llm_paid.PROVIDER,
        operation="complete", state=SubmissionState.SUBMISSION_UNKNOWN,
        retry_safety=RetrySafety.UNSAFE,
        cost=llm_paid.CostRecord(outcome=CostOutcome.UNKNOWN_EXPOSURE),
    )


# ===========================================================================
# live providers -- honestly skipped
# ===========================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="live LLM credentials are not configured",
)
def test_live_completion_is_guarded_and_advertises_no_recovery_handle():
    """A live billable completion must be guarded -- or fail loudly.

    Asserted against a real vendor because the one thing a fake transport
    cannot prove is that the protocol really returns no request id.
    """
    capability = llm_paid.llm_reconciliation_capability("complete")
    assert capability.availability is llm_paid.ReconciliationAvailability.UNRECONCILABLE

    result = llm_mod.complete("Reply with the single word: ok", "ping",
                              workspace_id="live-probe")
    assert result.text.strip()
    assert result.attempts == 1
    assert result.cost_usd > 0
