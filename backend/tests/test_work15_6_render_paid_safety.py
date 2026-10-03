"""Work 15.6 §5 — the primary render engine must not double-charge.

Work 15.5 established the paid-job contract for a generic provider. It did not
apply it to ``video_engine.mpt``, which is the *primary* render path.

The bug: ``submit()`` translated a lost response into ``VideoEngineUnavailable``,
whose ``retryable = True``. A READ timeout proves the POST was DELIVERED and the
answer was lost -- the engine may have accepted and billed the render -- so
reporting it as a retryable outage invites a second submit that buys a second
render. A CONNECT failure is genuinely safe to retry, because no socket was ever
opened, so lumping the two together is wrong in both directions.

The fix is context-sensitive and deliberately so: ``_translate_http_error``
stays context-free (a 5xx on a POLL really is a retryable outage, and
``test_mpt_adapter.py`` pins that), while ``submit()`` applies the stricter
billable rule. These tests pin both halves.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.video_engine.base import (
    RenderRequest,
    VideoEngineRequestInvalid,
    VideoEngineSubmissionUnknown,
    VideoEngineUnavailable,
)
from app.providers.video_engine.mpt import MoneyPrinterTurboAdapter


def _translate(exc: httpx.HTTPError):
    return MoneyPrinterTurboAdapter._translate_http_error(exc)


def _submit_raising(exc: httpx.HTTPError):
    """Drive the real ``submit()`` with a transport that raises ``exc``."""
    adapter = MoneyPrinterTurboAdapter()

    class _Boom:
        def __init__(self):
            self.raised = False

        def __call__(self, *_a, **_kw):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, *_a, **_kw):
            self.raised = True
            raise exc

    boom = _Boom()
    adapter._client = lambda: boom          # noqa: SLF001 - seam under test
    try:
        adapter.submit(RenderRequest(subject="s", script="x"))
    except BaseException as translated:      # noqa: BLE001 - inspecting the raise
        assert boom.raised, "the submit never reached the network"
        return translated
    raise AssertionError("submit unexpectedly succeeded")


@pytest.fixture()
def _request():
    return RenderRequest(subject="topic", script="a script")


# ===========================================================================
# the distinction that protects money
# ===========================================================================


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
def test_a_connect_failure_is_still_retryable(exc):
    """No socket was opened, so the request cannot have been delivered."""
    translated = _submit_raising(exc)
    assert isinstance(translated, VideoEngineUnavailable)
    assert translated.retryable is True


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("no response"),
        httpx.RemoteProtocolError("peer closed without response"),
    ],
)
def test_a_read_failure_on_submit_is_NEVER_retryable(exc):
    """The request WAS delivered. Retrying may buy a second render."""
    translated = _submit_raising(exc)
    assert isinstance(translated, VideoEngineSubmissionUnknown)
    assert translated.retryable is False


def test_submission_unknown_is_not_an_outage():
    """It must not masquerade as the retryable class, or callers resubmit."""
    translated = _submit_raising(httpx.ReadTimeout("lost"))
    assert not isinstance(translated, VideoEngineUnavailable)
    assert isinstance(translated, VideoEngineSubmissionUnknown)


def test_the_ambiguous_error_explains_the_reconciliation_requirement():
    """The message is the operator's only clue; it must name the risk."""
    text = str(_submit_raising(httpx.ReadTimeout("lost"))).lower()
    assert "billed" in text
    assert "reconcil" in text


def test_a_5xx_on_submit_is_ambiguous_but_a_5xx_translation_stays_retryable():
    """The distinction is contextual, and that is the whole design.

    ``test_mpt_adapter.py`` pins a bare 5xx as retryable, because for a POLL
    that is correct. For a SUBMIT it can follow a billable create, so the same
    status must become ambiguous.
    """
    request = httpx.Request("POST", "https://engine.test/api/v1/videos")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("server error", request=request, response=response)

    on_submit = _submit_raising(exc)
    assert isinstance(on_submit, VideoEngineSubmissionUnknown)
    assert on_submit.retryable is False

    # The context-free translator is unchanged, so polling still retries.
    assert isinstance(_translate(exc), VideoEngineUnavailable)


def test_a_4xx_on_submit_is_a_clean_rejection_not_ambiguous():
    """A 422 definitively refused the request, so no task was created."""
    request = httpx.Request("POST", "https://engine.test/api/v1/videos")
    response = httpx.Response(422, request=request)
    exc = httpx.HTTPStatusError("bad", request=request, response=response)
    translated = _submit_raising(exc)
    assert isinstance(translated, VideoEngineRequestInvalid)
    assert not isinstance(translated, VideoEngineSubmissionUnknown)


def test_a_429_on_submit_is_retryable():
    """A rate limit means nothing was created."""
    request = httpx.Request("POST", "https://engine.test/api/v1/videos")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("busy", request=request, response=response)
    assert isinstance(_submit_raising(exc), VideoEngineUnavailable)


def test_an_ambiguous_submit_costs_exactly_one_outbound_request():
    """The no-double-submit property, asserted on the primary render path."""
    attempts = {"n": 0}

    class _Counting:
        def __init__(self):
            self.resp = httpx.Response(
                200, request=httpx.Request("POST", "https://engine.test"),
                json={"status": 200, "data": {"task_id": "t1"}})

        def __call__(self, *_a, **_kw):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, *_a, **_kw):
            attempts["n"] += 1
            return self.resp

    adapter = MoneyPrinterTurboAdapter()
    adapter._client = lambda: _Counting()   # noqa: SLF001 - seam under test
    handle = adapter.submit(RenderRequest(subject="s", script="x"))
    assert handle.engine_task_id == "t1"
    assert attempts["n"] == 1, "a successful submit must not be issued twice"


# ===========================================================================
# unambiguous rejections keep their existing meaning
# ===========================================================================


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retryable_and_not_ambiguous(status):
    request = httpx.Request("POST", "https://engine.test/api/v1/videos")
    response = httpx.Response(status, request=request)
    exc = httpx.HTTPStatusError("auth", request=request, response=response)
    translated = _translate(exc)
    assert isinstance(translated, VideoEngineRequestInvalid)
    assert translated.retryable is False


def test_429_is_retryable():
    request = httpx.Request("POST", "https://engine.test/api/v1/videos")
    response = httpx.Response(429, request=request)
    exc = httpx.HTTPStatusError("busy", request=request, response=response)
    assert isinstance(_translate(exc), VideoEngineUnavailable)


# ===========================================================================
# the agent persists the ambiguous state instead of FAILED
# ===========================================================================


def test_the_agent_writes_submission_unknown_not_failed():
    """A regression guard on the exact wiring, not just the error taxonomy.

    ``VideoProducerAgent`` writes ``status=FAILED`` for every non-ambiguous
    engine error. If someone routes an ambiguous error back into that branch,
    the reference is destroyed and a re-buy is invited. This asserts the
    handler exists and is registered ahead of the generic one.
    """
    import inspect

    from app.engine.agents import production

    source = inspect.getsource(production.VideoProducerAgent.render)
    assert "VideoEngineSubmissionUnknown" in source
    unknown_at = source.index("except VideoEngineSubmissionUnknown")
    generic_at = source.index("except VideoEngineError")
    assert unknown_at < generic_at, (
        "the ambiguous handler must precede the generic one, or Python will "
        "never reach it")
    assert 'submission_state="SUBMISSION_UNKNOWN"' in source
    # The ambiguous branch must NOT write status=FAILED.
    branch = source[unknown_at:generic_at]
    assert 'status="FAILED"' not in branch


def test_the_ambiguous_error_carries_no_secret_in_its_message():
    """The message is persisted and surfaced in the UI."""
    request = httpx.Request("POST", "https://engine.test/api/v1/videos?api_key=sk-secret")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    from app.engine.intelligence.sanitize import redact_error_text

    cleaned = redact_error_text(str(_submit_raising(exc)))
    assert "sk-secret" not in cleaned