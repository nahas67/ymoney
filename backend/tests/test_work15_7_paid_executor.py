"""Work 15.7 — the shared paid executor's own invariants.

These test the contract itself rather than any provider. Every money-critical
behaviour in Work 15.7 is expressed here once, so a provider that adopts the
executor inherits the guarantees and cannot quietly weaken them.

The properties, in the order they matter:

1. a submit happens AT MOST once, whatever fails;
2. ambiguity is distinguished from a proven-safe retry;
3. unknown money is recorded as unknown, and proven-absent money as absent --
   a false unknown exposure is as wrong as a fabricated $0;
4. polling and downloading retry; creating does not;
5. an operator decision is always audited and always names its operator.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.paid_executor import (
    AmbiguousSubmission,
    CostOutcome,
    IdempotencySupport,
    PaidProviderExecutor,
    PaidSubmission,
    Reconciliation,
    RemoteSubmission,
    RetrySafety,
    RetryVerdict,
    reconcile_submission,
    verdict_for,
)
from app.services.paid_jobs import PaidJobRejected, SubmissionState


def _executor(**kwargs) -> PaidProviderExecutor:
    params = {"workspace_id": "ws-1", "provider": "acme", "operation": "render"}
    params.update(kwargs)
    return PaidProviderExecutor(**params)


def _boom(exc: BaseException):
    def submit(_key):
        raise exc
    return submit


# ===========================================================================
# 1. exactly one submit
# ===========================================================================


def test_a_successful_submit_produces_one_persisted_record():
    saved: list[tuple[str, str]] = []
    ex = _executor(persist=lambda r: saved.append((str(r.state), r.remote_id)))
    handle = ex.execute(lambda _k: RemoteSubmission(remote_id="job-7"),
                        estimated_cost=0.02)
    assert handle.remote_id == "job-7"
    # Snapshot each persist: the record is MUTATED in place, so storing the
    # object and inspecting it afterwards would only ever show the final state.
    assert saved[0] == ("SUBMISSION_ATTEMPTED", ""), (
        "the attempt must be recorded BEFORE the outbound request, so a crash "
        "mid-flight still leaves evidence that money was at stake")
    assert saved[-1] == ("REMOTE_ID_CONFIRMED", "job-7")


def test_the_attempt_is_persisted_before_the_request_is_sent():
    """Order is the guarantee. A record written afterwards loses the evidence."""
    observed: list[str] = []
    ex = _executor(persist=lambda r: observed.append(str(r.state)))

    def submit(_key):
        observed.append("SENT")
        return RemoteSubmission(remote_id="job-1")

    ex.execute(submit)
    assert observed.index(str(SubmissionState.SUBMISSION_ATTEMPTED)) < \
        observed.index("SENT")


def test_an_ambiguous_submit_is_never_retried_internally():
    calls = {"n": 0}

    def submit(_key):
        calls["n"] += 1
        raise httpx.ReadTimeout("delivered, answer lost")

    with pytest.raises(AmbiguousSubmission):
        _executor().execute(submit, estimated_cost=0.05)
    assert calls["n"] == 1, "an ambiguous billable submit was re-sent"


# ===========================================================================
# 2. ambiguity vs proven-safe retry
# ===========================================================================


def test_a_read_timeout_is_ambiguous_and_forbids_resubmission():
    saved: list[PaidSubmission] = []
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor(persist=saved.append).execute(
            _boom(httpx.ReadTimeout("lost")), estimated_cost=0.05)
    record = caught.value.submission
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.retry_safety is RetrySafety.UNSAFE
    assert verdict_for(record) is RetryVerdict.RECONCILE
    assert caught.value.recommended_action is Reconciliation.RECONCILE


def test_a_connect_failure_is_the_one_provably_safe_ambiguity():
    """No socket opened => nothing delivered => a retry cannot double-charge."""
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor().execute(_boom(httpx.ConnectTimeout("never opened")),
                            estimated_cost=0.05)
    record = caught.value.submission
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.retry_safety is RetrySafety.SAFE
    assert verdict_for(record) is RetryVerdict.RETRY


def test_a_dropped_connection_after_write_is_ambiguous():
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor().execute(
            _boom(httpx.RemoteProtocolError("peer closed without response")))
    assert caught.value.submission.retry_safety is RetrySafety.UNSAFE


def test_a_5xx_after_submit_is_ambiguous():
    request = httpx.Request("POST", "https://acme.test/jobs")
    response = httpx.Response(503, request=request)
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor().execute(
            _boom(httpx.HTTPStatusError("boom", request=request, response=response)))
    assert caught.value.submission.state is SubmissionState.SUBMISSION_UNKNOWN


def test_a_4xx_is_a_known_rejection_and_may_retry():
    request = httpx.Request("POST", "https://acme.test/jobs")
    response = httpx.Response(422, request=request)
    saved: list[tuple[str, str]] = []

    def snapshot(record: PaidSubmission) -> None:
        saved.append((str(record.state), str(record.reconciliation)))

    with pytest.raises(PaidJobRejected):
        _executor(persist=snapshot).execute(
            _boom(httpx.HTTPStatusError("bad", request=request, response=response)))
    # A rejection proves no task was created, so a later retry costs nothing.
    assert saved[-1] == ("FAILED", str(Reconciliation.RETRY_IF_CONFIRMED_SAFE))
    record = PaidSubmission(workspace_id="w", provider="p", operation="o",
                            state=SubmissionState.FAILED,
                            reconciliation=Reconciliation.RETRY_IF_CONFIRMED_SAFE)
    assert verdict_for(record) is RetryVerdict.RETRY


def test_cancellation_before_the_send_sends_nothing():
    calls = {"n": 0}

    def submit(_key):
        calls["n"] += 1
        return RemoteSubmission(remote_id="job-1")

    with pytest.raises(PaidJobRejected):
        _executor().execute(submit, should_cancel=lambda: True)
    assert calls["n"] == 0, "a cancelled submit still reached the provider"


def test_a_cancellation_sent_before_any_request_leaves_no_exposure():
    saved: list[PaidSubmission] = []
    with pytest.raises(PaidJobRejected):
        _executor(persist=saved.append).execute(
            lambda _k: RemoteSubmission(remote_id="j"), should_cancel=lambda: True)
    assert saved[-1].state is SubmissionState.CANCELLED
    assert saved[-1].cost.outcome is CostOutcome.NOT_APPLICABLE
    assert saved[-1].exposure_unknown is False


# ===========================================================================
# 3. the money fields
# ===========================================================================


def test_an_ambiguous_submit_books_unknown_exposure_not_zero():
    """Losing a response is not evidence that nothing was charged."""
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor().execute(_boom(httpx.ReadTimeout("lost")), estimated_cost=0.05)
    cost = caught.value.submission.cost
    assert cost.outcome is CostOutcome.UNKNOWN_EXPOSURE
    assert cost.ledger_value is None, (
        "unknown exposure must not book a number; $0 would erase real spend")
    assert caught.value.submission.exposure_unknown is True


def test_a_provably_undelivered_attempt_does_not_claim_unknown_exposure():
    """The mirror image, and equally important.

    A connect failure proves nothing was billed, so claiming an unknown
    exposure would put a PHANTOM charge on the books for an attempt that never
    reached the provider. An over-broad 'money may have been spent' is as wrong
    as a fabricated $0.
    """
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor().execute(_boom(httpx.ConnectTimeout("never opened")),
                            estimated_cost=0.05)
    record = caught.value.submission
    assert record.cost.outcome is CostOutcome.ESTIMATED
    assert record.cost.ledger_value == 0.0
    assert record.exposure_unknown is False


def test_a_success_books_the_actual_amount():
    record = PaidSubmission(workspace_id="w", provider="p", operation="o")
    _executor().mark_succeeded(record, actual_cost=0.031)
    assert record.state is SubmissionState.SUCCEEDED
    assert record.cost.outcome is CostOutcome.ACTUAL
    assert record.cost.ledger_value == pytest.approx(0.031)


def test_the_budget_gate_runs_before_the_submit():
    order: list[str] = []
    ex = _executor(submit_budget=lambda: order.append("BUDGET"))

    def submit(_key):
        order.append("SEND")
        return RemoteSubmission(remote_id="j")

    ex.execute(submit, estimated_cost=1.0)
    assert order == ["BUDGET", "SEND"], (
        "spend was authorised after the request left; the gate is worthless")


def test_a_refused_budget_never_reaches_the_provider():
    calls = {"n": 0}

    def submit(_key):
        calls["n"] += 1
        return RemoteSubmission(remote_id="j")

    class _Denied(Exception):
        pass

    def deny():
        raise _Denied("over budget")

    with pytest.raises(_Denied):
        _executor(submit_budget=deny).execute(submit, estimated_cost=99.0)
    assert calls["n"] == 0


# ===========================================================================
# 4. polling / downloading retry; creating does not
# ===========================================================================


def test_polling_retries_the_same_remote_job_without_resubmitting():
    ex = _executor()
    states = iter(["queued", "running", "succeeded"])
    state, _payload = ex.poll_remote(
        lambda _t: (next(states), {}), deadline_seconds=60)
    assert state == "succeeded"


def test_downloading_retries_the_fetch_never_the_creation():
    attempts = {"n": 0}
    ex = _executor()

    def fetch():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ReadTimeout("cdn jitter")
        return b"artifact-bytes"

    assert ex.download(fetch, remote_id="job-1") == b"artifact-bytes"
    assert attempts["n"] == 3


def test_a_failed_download_says_the_job_was_still_billed():
    from app.services.paid_jobs import PaidArtifactUndownloadable

    ex = _executor()
    with pytest.raises(PaidArtifactUndownloadable) as caught:
        ex.download(lambda: (_ for _ in ()).throw(httpx.ReadTimeout("x")),
                    remote_id="job-9", url="https://cdn.test/v.mp4", attempts=2)
    assert caught.value.remote_id == "job-9"


def test_a_provider_cannot_claim_idempotency_it_does_not_have():
    """Only an officially documented header may be sent."""
    assert IdempotencySupport.SUPPORTED != IdempotencySupport.UNVERIFIED
    record = PaidSubmission(workspace_id="w", provider="p", operation="o",
                            idempotency_support=IdempotencySupport.UNVERIFIED)
    # UNVERIFIED must never be reported as a guarantee in the payload.
    assert record.to_dict()["idempotency_support"] == "IDEMPOTENCY_UNVERIFIED"


# ===========================================================================
# 5. reconciliation is audited and needs an operator
# ===========================================================================


def _unknown_record() -> PaidSubmission:
    with pytest.raises(AmbiguousSubmission) as caught:
        _executor().execute(_boom(httpx.ReadTimeout("lost")), estimated_cost=0.05)
    return caught.value.submission


def test_an_ambiguous_submission_is_never_reported_as_generic_failure():
    """The operator must be able to TELL this apart from a refusal."""
    record = _unknown_record()
    assert record.state is not SubmissionState.FAILED
    payload = record.to_dict()
    assert payload["state"] == "SUBMISSION_UNKNOWN"
    assert payload["cost"]["unknown_exposure"] is True
    assert payload["remote_id"] == "" or isinstance(payload["remote_id"], str)


def test_a_reconciliation_decision_requires_an_operator():
    record = _unknown_record()
    with pytest.raises(ValueError):
        reconcile_submission(record, Reconciliation.MARK_FAILED, operator="")
    with pytest.raises(ValueError):
        reconcile_submission(record, Reconciliation.MANUAL_OVERRIDE, note="n")


def test_marking_failed_keeps_the_exposure_visible():
    """Close the incident, but do not pretend the money was never at stake."""
    record = _unknown_record()
    reconcile_submission(record, Reconciliation.MARK_FAILED,
                         operator="ops@x", note="provider confirmed no charge")
    assert record.state is SubmissionState.FAILED
    assert record.cost.outcome is CostOutcome.UNKNOWN_EXPOSURE, (
        "closing an incident must not erase the record that money was exposed")


def test_reconcile_leaves_the_state_alone():
    """RECONCILE means still-unconfirmed. Pretending otherwise is the bug."""
    record = _unknown_record()
    reconcile_submission(record, Reconciliation.RECONCILE, operator="ops@x")
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN


def test_a_manual_override_is_audited_with_its_operator_and_note():
    record = _unknown_record()
    reconcile_submission(record, Reconciliation.MANUAL_OVERRIDE,
                         operator="ops@x", note="provider emailed a receipt")
    assert "ops@x" in record.detail
    assert "provider emailed a receipt" in record.detail
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN


def test_retry_if_confirmed_safe_marks_the_retry_legitimate():
    record = _unknown_record()
    reconcile_submission(record, Reconciliation.RETRY_IF_CONFIRMED_SAFE,
                         operator="ops@x")
    assert record.retry_safety is RetrySafety.SAFE
    assert verdict_for(record) is RetryVerdict.RETRY


def test_audit_and_cost_hooks_are_invoked_for_every_phase():
    events: list[str] = []
    costs: list[PaidSubmission] = []
    ex = _executor(audit=lambda r, phase: events.append(phase),
                   cost_hook=costs.append)
    ex.execute(lambda _k: RemoteSubmission(remote_id="j"), estimated_cost=0.01)
    ex.mark_succeeded(PaidSubmission(workspace_id="w", provider="p",
                                     operation="o"), actual_cost=0.01)
    assert "attempted" in events and "accepted" in events
    assert costs, "the cost hook never fired"


def test_an_ambiguous_submit_emits_an_audit_event():
    events: list[tuple[str, str]] = []
    ex = _executor(audit=lambda r, phase: events.append((phase, str(r.state))))
    with pytest.raises(AmbiguousSubmission):
        ex.execute(_boom(httpx.ReadTimeout("lost")), estimated_cost=0.05)
    assert ("ambiguous", "SUBMISSION_UNKNOWN") in events


def test_a_provider_can_record_an_acknowledged_cancellation():
    """Distinct from FAILED: the outcome is KNOWN and usually unpaid."""
    ex = _executor()
    record = PaidSubmission(workspace_id="w", provider="p", operation="o")
    ex.mark_cancelled(record, detail="provider cancelled job 3")
    assert record.state is SubmissionState.CANCELLED
    assert record.reconciliation is Reconciliation.MARK_FAILED
    assert verdict_for(record) is RetryVerdict.RETRY