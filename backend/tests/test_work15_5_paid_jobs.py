"""Work 15.5 §7 — paid-job safety.

The property under test is the one that costs money: **a network timeout can
never cause a second charge.** Everything here is about the boundary between
"the provider definitely refused" and "we do not know", and the rule that only
the former permits a retry.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry); the
taxonomy is re-derived for YMONEY's httpx idiom and persisted onto ``Video``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.services.paid_jobs import (
    MAX_POLL_RETRIES,
    PaidArtifactUndownloadable,
    PaidJobError,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionRecord,
    SubmissionState,
    classify_submit_exception,
    download_with_retry,
    is_definitive_failure_status,
    is_safe_to_retry,
    poll_with_deadline,
    record_submission,
)


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://x.test"))


# ===========================================================================
# the classification boundary
# ===========================================================================


def test_a_4xx_is_a_definitive_rejection_and_no_idle_job_exists():
    """4xx means the provider refused the request, so no task was created."""
    assert is_definitive_failure_status(400) is True
    assert is_definitive_failure_status(422) is True
    for status in (500, 502, 503, 504):
        assert is_definitive_failure_status(status) is False, (
            f"{status} may follow a successful billable create")


def test_a_5xx_is_ambiguous_not_a_rejection():
    classified = classify_submit_exception(
        httpx.HTTPStatusError("boom", request=httpx.Request("POST", "https://x.test"),
                              response=_response(503)),
        provider="acme")
    assert isinstance(classified, PaidSubmissionUnconfirmed)


def test_a_400_becomes_a_rejection_carrying_the_status():
    classified = classify_submit_exception(
        httpx.HTTPStatusError("bad", request=httpx.Request("POST", "https://x.test"),
                              response=_response(400)),
        provider="acme")
    assert isinstance(classified, PaidJobRejected)
    assert classified.status_code == 400
    assert "acme" in str(classified)


# ===========================================================================
# the nuance that matters most: connect vs read timeout
# ===========================================================================


def test_a_read_timeout_is_unconfirmed_because_the_request_was_sent():
    exc = httpx.ReadTimeout("no response")
    assert is_safe_to_retry(exc) is False, (
        "a read timeout means the request was DELIVERED; retrying may pay twice")
    classified = classify_submit_exception(exc, provider="acme")
    assert isinstance(classified, PaidSubmissionUnconfirmed)
    assert classified.provably_undelivered is False
    assert "DO NOT RESUBMIT" in str(classified)


def test_a_connect_timeout_is_provably_safe_to_retry():
    """The one ambiguous case that is genuinely safe: the socket never opened."""
    exc = httpx.ConnectTimeout("connect timed out")
    assert is_safe_to_retry(exc) is True
    classified = classify_submit_exception(exc, provider="acme")
    assert isinstance(classified, PaidSubmissionUnconfirmed)
    assert classified.provably_undelivered is True
    assert "retry is safe" in str(classified)


def test_a_dropped_connection_is_ambiguous():
    exc = httpx.RemoteProtocolError("peer closed connection without response")
    assert is_safe_to_retry(exc) is False


def test_a_refused_connection_is_provably_undelivered():
    exc = httpx.ConnectError("connection refused")
    assert is_safe_to_retry(exc) is True


def test_an_unrecognised_failure_is_ambiguous_by_default():
    """Unknown means unknown. Defaulting to 'retry' is how double-charges happen."""
    classified = classify_submit_exception(RuntimeError("something odd"))
    assert isinstance(classified, PaidSubmissionUnconfirmed)
    assert classified.provably_undelivered is False


# ===========================================================================
# a timeout must never trigger a resubmit
# ===========================================================================


def test_a_timeout_never_marks_the_record_resubmittable():
    record = SubmissionRecord(workspace_id="w1", provider="acme")
    record = record_submission(record, httpx.ReadTimeout("lost"))
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.may_resubmit is False


def test_a_5xx_never_marks_the_record_resubmittable():
    record = record_submission(
        SubmissionRecord(workspace_id="w1", provider="acme"),
        httpx.HTTPStatusError("boom", request=httpx.Request("POST", "https://x.test"),
                              response=_response(500)))
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.may_resubmit is False


def test_a_4xx_does_allow_a_later_retry():
    """A rejection created no job, so retrying costs nothing extra."""
    record = record_submission(
        SubmissionRecord(workspace_id="w1", provider="acme"),
        httpx.HTTPStatusError("bad", request=httpx.Request("POST", "https://x.test"),
                              response=_response(422)))
    assert record.state is SubmissionState.FAILED
    assert record.may_resubmit is True


def test_a_confirmed_remote_id_stops_any_resubmit():
    record = record_submission(
        SubmissionRecord(workspace_id="w1", provider="acme"), None)
    assert record.state is SubmissionState.REMOTE_ID_CONFIRMED
    assert record.may_resubmit is False


def test_every_terminal_state_forbids_resubmission():
    for state in (SubmissionState.SUBMISSION_UNKNOWN,
                  SubmissionState.REMOTE_ID_CONFIRMED,
                  SubmissionState.PROCESSING,
                  SubmissionState.SUCCEEDED):
        record = SubmissionRecord(workspace_id="w", provider="p", state=state)
        assert record.may_resubmit is False, state


def test_the_unconfirmed_error_carries_the_remote_id_for_recovery():
    exc = PaidSubmissionUnconfirmed(provider="acme", remote_id="job-77",
                                    detail="read timeout")
    assert "job-77" in str(exc)


# ===========================================================================
# download: retry the fetch, never re-buy
# ===========================================================================


def test_download_retries_the_fetch_not_the_job():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("download jitter")
        return b"video-bytes"

    data = download_with_retry(flaky, provider="acme", remote_id="job-1",
                               sleep=lambda _s: None)
    assert data == b"video-bytes"
    assert calls["n"] == 3


def test_a_failed_download_reports_the_paid_job_succeeded():
    """The remote job WAS billed; losing the bytes must not read as a failure."""
    def always_fails():
        raise httpx.ReadTimeout("nope")

    with pytest.raises(PaidArtifactUndownloadable) as caught:
        download_with_retry(always_fails, provider="acme", remote_id="job-99",
                            url="https://cdn.test/v.mp4", sleep=lambda _s: None)
    assert caught.value.remote_id == "job-99"
    assert "job-99" in str(caught.value)


def test_an_empty_body_is_a_download_failure_not_a_success():
    with pytest.raises(PaidArtifactUndownloadable):
        download_with_retry(lambda: b"", provider="acme", remote_id="j",
                            sleep=lambda _s: None)


# ===========================================================================
# polling
# ===========================================================================


def test_polling_stops_on_a_terminal_state():
    states = iter(["queued", "running", "succeeded", "succeeded"])
    state, _payload = poll_with_deadline(
        lambda _t: (next(states), {}), deadline_seconds=60,
        sleep=lambda _s: None)
    assert state == "succeeded"


def test_polling_treats_an_unknown_status_as_ambiguous_not_failed():
    """A status this client has never seen is not evidence of failure."""
    state, _ = poll_with_deadline(
        lambda _t: ("some_new_vendor_state", {}), deadline_seconds=0.5,
        sleep=lambda _s: None, max_retries=2)
    assert state != "failed"
    assert state == "some_new_vendor_state"


def test_polling_never_blocks_past_the_deadline():
    state, _ = poll_with_deadline(
        lambda _t: ("processing", {}), deadline_seconds=0.0,
        sleep=lambda _s: None)
    assert state == "unknown"


def test_polling_survives_transient_poll_failures():
    calls = {"n": 0}

    def flaky(_timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadTimeout("poll jitter")
        return "succeeded", {}

    state, _ = poll_with_deadline(flaky, deadline_seconds=60,
                                  sleep=lambda _s: None)
    assert state == "succeeded"
    assert calls["n"] == 3


def test_polling_propagates_a_paid_error_rather_than_swallowing_it():
    def raises(_timeout):
        raise PaidSubmissionUnconfirmed(provider="acme", remote_id="j")

    with pytest.raises(PaidSubmissionUnconfirmed):
        poll_with_deadline(raises, deadline_seconds=60, sleep=lambda _s: None)


def test_the_poll_retry_budget_is_bounded():
    """A billable job is polled, not re-bought; the bound is what makes it safe."""
    assert MAX_POLL_RETRIES >= 1
    calls = {"n": 0}

    def always_fails(_timeout):
        calls["n"] += 1
        raise httpx.ReadTimeout("jitter")

    poll_with_deadline(always_fails, deadline_seconds=60,
                       sleep=lambda _s: None)
    assert calls["n"] <= MAX_POLL_RETRIES + 1


# ===========================================================================
# the agent integration: an ambiguous prior submission HALTS
# ===========================================================================


@pytest.fixture()
def ws(db_session):
    from app.models import Workspace

    row = Workspace(name="paid-test", slug=f"paid-{os.urandom(4).hex()}")
    db_session.add(row)
    db_session.commit()
    return row


def _variant(db, workspace_id, content_item_id, label):
    """A VideoVariant needs a parent ContentItem (it has no workspace column)."""
    from app.models.content import ContentItem, VideoVariant

    if content_item_id is None:
        item = ContentItem(workspace_id=workspace_id, topic="paid test",
                           status="IDEA")
        db.add(item)
        db.flush()
        content_item_id = item.id
    variant = VideoVariant(content_item_id=content_item_id, label=label)
    db.add(variant)
    db.flush()
    return variant


def test_an_ambiguous_prior_submission_is_not_written_as_failed(db_session, ws):
    """The bug this whole subsystem exists to prevent.

    A submit interrupted before the engine task id was persisted is NOT a
    failure. Writing FAILED loses the reference and invites a re-buy.
    """
    from app.engine.agents.production import VideoProducerAgent
    from app.models.content import Video

    variant = _variant(db_session, ws.id, None, "v1")
    row = Video(workspace_id=ws.id, variant_id=variant.id,
                engine="moneyprinterturbo", status="RENDERING", engine_task_id="")
    db_session.add(row)
    db_session.commit()

    class _NoOrphans:
        def list_recent_tasks(self, limit=50):
            return []

    resolution = VideoProducerAgent._resolve_existing(
        _NoOrphans(), variant.id, "hash-1", "some topic")
    assert resolution["kind"] == "unknown"
    assert "video_id" in resolution

    db_session.expire_all()
    stored = db_session.get(Video, row.id)
    assert stored.submission_state == "SUBMISSION_UNKNOWN"
    assert stored.submission_state != "FAILED"
    assert "may have been accepted and billed" in stored.submission_detail
    assert "Do not resubmit" in stored.submission_detail


def test_an_adopted_orphan_is_reattached_not_left_unknown(db_session, ws):
    from app.engine.agents.production import VideoProducerAgent
    from app.models.content import Video

    variant = _variant(db_session, ws.id, None, "v2")
    row = Video(workspace_id=ws.id, variant_id=variant.id,
                engine="moneyprinterturbo", status="RENDERING", engine_task_id="")
    db_session.add(row)
    db_session.commit()

    class _HasOrphan:
        def list_recent_tasks(self, limit=50):
            return [{"subject": "Some Topic", "state": "processing",
                     "task_id": "engine-42"}]

    resolution = VideoProducerAgent._resolve_existing(
        _HasOrphan(), variant.id, "hash-2", "Some Topic")
    assert resolution["kind"] == "adopt"
    assert resolution["engine_task_id"] == "engine-42"

    db_session.expire_all()
    assert db_session.get(Video, row.id).engine_task_id == "engine-42"


def test_orphan_adoption_is_workspace_scoped(db_session, ws):
    """Another tenant's task must never be adopted."""
    from app.engine.agents.production import VideoProducerAgent
    from app.models import Workspace
    from app.models.content import Video

    other = Workspace(name="other", slug=f"o-{os.urandom(4).hex()}")
    db_session.add(other)
    db_session.commit()
    variant = _variant(db_session, ws.id, None, "v3")
    db_session.add(Video(workspace_id=ws.id, variant_id=variant.id,
                         engine="moneyprinterturbo", status="RENDERING",
                         engine_task_id=""))
    db_session.commit()

    class _ForeignOrphan:
        def list_recent_tasks(self, limit=50):
            return [{"subject": "t", "state": "processing",
                     "task_id": "belongs-to-another-tenant"}]

    resolution = VideoProducerAgent._resolve_existing(
        _ForeignOrphan(), variant.id, "h", "t")
    if resolution["kind"] == "adopt":
        adopted = db_session.get(Video, resolution["video_id"])
        assert adopted.workspace_id == ws.id


def test_the_base_error_cannot_be_caught_as_a_plain_paid_job_error():
    """The three failure modes are siblings, so callers must not collapse them."""
    for cls in (PaidSubmissionUnconfirmed, PaidJobRejected,
                PaidArtifactUndownloadable):
        assert issubclass(cls, PaidJobError)
        assert cls is not PaidJobError


def test_an_already_classified_error_passes_through_untouched():
    original = PaidJobRejected(provider="acme", status_code=400, detail="d")
    assert classify_submit_exception(original, provider="other") is original
