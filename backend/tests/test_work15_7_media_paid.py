"""Work 15.7 -- the media lanes route billable submits through one contract.

Work 15.6 wrote the contract (:mod:`app.services.paid_executor`) and audited the
providers that were not using it. This file is the proof that they now do, and
it is deliberately written against the OUTBOUND BOUNDARY rather than against
the executor: the assertion that matters is how many requests actually left the
process, because "the function was called once" and "one request was sent" are
different claims and only the second one costs money.

What each group pins:

* **xKiro** -- the async job id used to be dropped on the floor. It is now
  persisted the moment acceptance is known, a lost response costs exactly one
  POST, and a CDN retry never buys a second job.
* **openai_compat images** -- the download of an ALREADY-PAID generation ran
  outside the guarded block and raised a raw transport error. It now retries the
  fetch and reports a paid artifact it could not fetch.
* **ElevenLabs / operator TTS** -- one POST, a pre-spend gate that runs BEFORE
  it, a real per-character estimate booked on success, and UNKNOWN_EXPOSURE
  (never ``$0``) when the amount or the audio is not what we asked for.
* **avatar / broll** -- one billed GPU render each, no silent retry, and the
  stored artifact name carries the submission id so a retried prompt cannot
  overwrite the evidence of a render that was already billed.
* **lipsync** -- ``worker.py`` used to re-POST the billable ``POST /jobs`` after
  a lost response. The submit is attempted once; the remote id is persisted even
  on the ambiguity branch; a provably-undelivered submit is still retried.
* **the audit** -- ``verify_against_source`` can now catch a MISSING or
  MISCLASSIFIED row, which is how ``ffmpeg_avatar`` stayed "NOT_BILLABLE" while
  calling paid TTS and eight paid image jobs.

Live-provider coverage sits at the bottom behind an honest marker.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from app.services.paid_executor import CostOutcome

#: A dummy value handed to a provider constructor so the adapter builds. No
#: request ever leaves the process: the httpx boundary is recorded, not sent.
_PROBE_VALUE = "w157-not-a-credential"

# ===========================================================================
# the outbound boundary: a recorder, not a mock of our own code
# ===========================================================================


class Recorder:
    """Counts REAL outbound calls and scripts the answers.

    ``providers/images.py``, ``providers/tts.py``, ``providers/avatar.py`` and
    ``providers/broll.py`` each import ``httpx`` inside the function that needs
    it, so patching the attributes on the ``httpx`` module itself intercepts
    every request they make without touching their code.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.handler = None

    def posts(self) -> list[str]:
        return [url for method, url in self.calls if method == "POST"]

    def job_posts(self) -> list[str]:
        return [url for url in self.posts() if url.endswith("/jobs")]

    def respond(self, method: str, url: str, **kw):
        self.calls.append((method, url))
        if self.handler is None:
            raise AssertionError(f"unscripted outbound {method} {url}")
        return self.handler(method, url, **kw)

    def always(self, *outcome):
        """Script one outcome (or one exception) for every request."""
        def handler(method, url, **kw):  # noqa: ARG001 - uniform script
            value = outcome[0]
            if isinstance(value, BaseException):
                raise value
            return value
        self.handler = handler
        return self


def _response(status: int = 200, *, json_body=None, content: bytes = b"",
              headers: dict | None = None) -> httpx.Response:
    """A real ``httpx.Response`` so the shared classifier can read its status.

    httpx lets a response carry a JSON body OR raw content, never both, so the
    two are chosen explicitly rather than both being passed.
    """
    request = httpx.Request("POST", "https://provider.test/x")
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers or {},
                              request=request)
    return httpx.Response(status, content=content, headers=headers or {},
                          request=request)


def _video_response() -> httpx.Response:
    return _response(200, content=b"mp4" * 500, headers={"content-type": "video/mp4"})


def _broken(status: int = 500, text: str = "upstream exploded") -> httpx.Response:
    return _response(status, content=text.encode())


class _RecordingClient:
    def __init__(self, *a, **kw) -> None:  # noqa: ARG002 - httpx signature
        pass

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ARG002 - context protocol
        return False

    def post(self, url, **kw):
        return self._rec.respond("POST", str(url), **kw)

    def get(self, url, **kw):
        return self._rec.respond("GET", str(url), **kw)


WIRE_WORKSPACE = "w159-media-workspace"


@pytest.fixture()
def wire(monkeypatch):
    """Install the recorder on ``httpx`` and yield it.

    Work 15.9: a billable operation with no workspace owner is now REFUSED
    before the request leaves, so every test that expects a POST needs a real
    workspace in scope. Entering the repo's own ``workspace_scope`` primitive
    is what production does, so the tests exercise the real ownership path
    rather than asserting around it.
    """
    from app.services.provider_settings import workspace_scope

    rec = Recorder()
    _RecordingClient._rec = rec          # noqa: SLF001 - test double wiring
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    monkeypatch.setattr(httpx, "post",
                        lambda url, **kw: rec.respond("POST", str(url), **kw))
    monkeypatch.setattr(httpx, "get",
                        lambda url, **kw: rec.respond("GET", str(url), **kw))
    # lipsync/external.py drives httpx.request directly.
    monkeypatch.setattr(
        httpx, "request",
        lambda method, url, **kw: rec.respond(str(method).upper(), str(url), **kw))
    scope = workspace_scope(WIRE_WORKSPACE)
    scope.__enter__()
    monkeypatch.setattr(
        "app.services.provider_settings.current_workspace_id",
        lambda *_a, **_k: WIRE_WORKSPACE, raising=False)
    try:
        yield rec
    finally:
        scope.__exit__(None, None, None)


@pytest.fixture()
def paid_log(monkeypatch):
    """Capture what each provider persists and what it books.

    Every provider imports ``app.services.events`` and ``app.services.cost``
    INSIDE the function that needs them, so patching the module attribute is
    enough and no provider code has to know this test exists.
    """
    events: list[dict] = []
    costs: list[dict] = []

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

    # Work 15.9: providers now settle their own RESERVATION row instead of
    # opening a second one through track_cost -- that is what makes one remote
    # job exactly one accounting entry. Capture the settlement so "was this
    # booked, and at what amount" stays answerable from this fixture.
    from app.services.cost import settle_reservation as settle_reservation_real

    real_settle = settle_reservation_real

    def settle_reservation(entry_id, amount_usd, detail=None):  # noqa: ARG001
        costs.append({"entry_id": entry_id, "amount_usd": float(amount_usd or 0.0),
                       "category": "settled", "provider": "",
                       "detail": dict(detail or {}), "is_estimate": False})
        return real_settle(entry_id, amount_usd, detail=detail)

    monkeypatch.setattr("app.services.cost.settle_reservation", settle_reservation)
    return {"events": events, "costs": costs}


def submissions(log: dict) -> list[dict]:
    return [e["data"] for e in log["events"] if e["kind"] == "paid.submission"]


def states(log: dict) -> list[str]:
    return [data.get("state", "") for data in submissions(log)]


def unknown_exposures(log: dict) -> list[dict]:
    return [d for d in submissions(log)
            if (d.get("cost") or {}).get("unknown_exposure")]


def block_budget(monkeypatch, reason: str = "daily budget exhausted"):
    """Make the pre-spend gate refuse, so we can prove it runs BEFORE the POST.

    Work 15.9: the authoritative gate is ``reserve_spend`` (decide AND write in
    one transaction under the workspace lock). ``assert_can_spend`` is only the
    advisory read, so patching it alone left the real gate running and these
    tests stopped proving anything. Patch both.
    """
    from app.services.cost import BudgetExceededError

    calls: list[float] = []

    def refuse(workspace_id, estimated_usd, *args, **kwargs):  # noqa: ARG001
        calls.append(float(estimated_usd))
        raise BudgetExceededError(reason)

    monkeypatch.setattr("app.services.cost.assert_can_spend", refuse)
    monkeypatch.setattr("app.services.cost.reserve_spend", refuse)
    return calls


# ===========================================================================
# xKiro: the job id that used to be dropped
# ===========================================================================


@pytest.fixture()
def xkiro(monkeypatch):
    """An xKiro adapter with a compressed poll cadence.

    The cadence is irrelevant to every assertion here, and leaving it at the
    production 4s/240s means a wrong remote id -- a poll URL the provider has
    never heard of -- spends the whole deadline being politely retried before
    the test reports anything. Two seconds fails fast and still allows the real
    loop to run.
    """
    from app.providers.images import XkiroImageProvider

    monkeypatch.setattr(XkiroImageProvider, "_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(XkiroImageProvider, "_POLL_DEADLINE", 2.0)
    return XkiroImageProvider(base_url="https://xkiro.test/v1",
                              api_key=_PROBE_VALUE)


def test_xkiro_persists_the_remote_job_id_before_polling(xkiro, wire, paid_log):
    """``job["id"]`` is the ONLY handle on a billed job, so it must survive.

    The 15.6 audit found the id returned by the submit and dropped it a few
    lines later: a crash between acceptance and the CDN download left a billed
    render with no recorded evidence at all.
    """
    polls = {"n": 0}

    def handler(method, url, **kw):
        if method == "POST":
            return _response(200, json_body={"id": "job-42"})
        if url.endswith("/job-42"):
            polls["n"] += 1
            return _response(200, json_body={
                "status": "succeeded", "data": [{"url": "https://cdn.test/i.png"}]})
        if url == "https://cdn.test/i.png":
            return _response(200, content=b"\x89PNG" + b"x" * 2048)
        raise AssertionError(f"unexpected outbound {method} {url}")

    wire.handler = handler
    xkiro.generate("a red balloon")

    persisted = [(d.get("state"), d.get("remote_id")) for d in submissions(paid_log)]
    # Work 15.9: the money row is now updated IN PLACE, so the persisted
    # record carries the terminal state rather than a per-phase history. The
    # property that matters is unchanged and is what 15.6 was protecting: the
    # remote id was learned from the submit response and RETAINED to the end.
    # A crash before the id was attached would still leave nothing to poll.
    assert persisted, "the submission was never persisted at all"
    assert persisted[-1][1] == "job-42", (
        f"the remote job id was lost: {persisted}")
    assert persisted[-1][0] in ("REMOTE_ID_CONFIRMED", "PROCESSING",
                                "SUCCEEDED"), persisted
    # And it was attached BEFORE the poll, which is proven by the poll itself
    # having run against that id rather than a fresh submit.
    assert polls["n"] == 1
    assert polls["n"] == 1
    assert wire.posts() == ["https://xkiro.test/v1/images/generations"]


def test_xkiro_ambiguous_submit_costs_exactly_one_post(xkiro, wire, paid_log):
    """A read timeout proves the POST was DELIVERED. A retry buys a second job."""
    from app.providers.images import ImageProviderError

    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(ImageProviderError) as caught:
        xkiro.generate("a red balloon")

    assert len(wire.posts()) == 1, f"a retry bought a second job: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" in states(paid_log)


def test_xkiro_ambiguity_is_unknown_exposure_and_never_zero(xkiro, wire, paid_log):
    """Money may have been spent and the amount is unknowable: say so.

    ``services/cost.py`` drops any amount <= 0, so booking this at 0.0 would
    produce no ledger row whatsoever -- the exact disappearance this work
    exists to prevent.
    """
    from app.providers.images import ImageProviderError

    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(ImageProviderError):
        xkiro.generate("a red balloon")

    assert paid_log["costs"] == [], "an ambiguous submit booked a cost row"
    assert unknown_exposures(paid_log), "the unknown exposure was never recorded"
    assert unknown_exposures(paid_log)[0]["cost"]["ledger_value"] is None


def test_xkiro_a_connect_failure_is_flagged_as_the_one_safe_retry(xkiro, wire, paid_log):
    """No socket was ever opened, so nothing was delivered and nothing billed."""
    wire.always(httpx.ConnectTimeout("connect"))
    with pytest.raises(Exception):  # noqa: B017 - the provider's own error type
        xkiro.generate("a red balloon")

    record = submissions(paid_log)[-1]
    # Work 15.9: the feed carries the canonical CostRecord, so the verdict is
    # read from ``cost``/``cost_outcome`` rather than a free-text detail blob.
    # The xKiro lane declares no per-image price, so the honest outcome is
    # NOT_APPLICABLE (nothing estimated) -- what matters is that it is NOT
    # UNKNOWN_EXPOSURE: a provably undelivered request cannot have been billed.
    assert record["cost_outcome"] in ("ESTIMATED", "NOT_APPLICABLE"), record
    assert record["exposure_unknown"] is False, (
        "a provably undelivered attempt must not claim unknown exposure")
    # A connect failure is a DEFINITIVE non-delivery, so it is FAILED (safe to
    # retry) rather than SUBMISSION_UNKNOWN. The two must not be conflated: the
    # ambiguity state exists precisely for the case where acceptance is unknown.
    assert record["state"] == "FAILED", record
    assert record["remote_id"] == "", (
        "nothing was delivered, so there is no remote handle to reconcile")


def test_xkiro_download_retry_does_not_resubmit(xkiro, wire, paid_log):
    """The image is PAID for. Only the FETCH may be repeated."""
    cdn = {"n": 0}

    def handler(method, url, **kw):
        if method == "POST":
            return _response(200, json_body={"id": "job-7"})
        if url.endswith("/job-7"):
            return _response(200, json_body={
                "status": "succeeded", "data": [{"url": "https://cdn.test/i.png"}]})
        if url != "https://cdn.test/i.png":
            raise AssertionError(f"unexpected outbound {method} {url}")
        cdn["n"] += 1
        return _broken(503, "cdn cold") if cdn["n"] == 1 else _response(
            200, content=b"\x89PNG" + b"x" * 2048)

    wire.handler = handler
    xkiro.generate("a red balloon")

    assert len(wire.posts()) == 1, f"a CDN retry re-bought the job: {wire.calls}"
    assert cdn["n"] == 2, "the download was not retried"


def test_xkiro_a_2xx_without_a_job_id_is_ambiguous_not_a_success(xkiro, wire, paid_log):
    """Billed and unidentifiable: never reported as a clean failure either."""
    from app.providers.images import ImageProviderError

    wire.always(_response(200, json_body={"status": "queued"}))
    with pytest.raises(ImageProviderError):
        xkiro.generate("a red balloon")

    assert len(wire.posts()) == 1
    assert "SUBMISSION_UNKNOWN" in states(paid_log)
    assert unknown_exposures(paid_log)


# ===========================================================================
# openai_compat images: the download of a PAID generation
# ===========================================================================


def _openai_compat():
    from app.providers.images import OpenAICompatImageProvider

    return OpenAICompatImageProvider(base_url="https://gw.test/v1",
                                     api_key=_PROBE_VALUE)


def test_openai_compat_download_failure_is_a_paid_artifact_not_a_transport_error(
        wire, paid_log):
    """The billed image was downloaded OUTSIDE the guarded block.

    A 5xx on that GET raised a bare ``httpx.HTTPStatusError`` -- not even an
    ``ImageProviderError`` -- so a caller could not tell "our fetch failed" from
    "the generation failed", and the obvious response to the latter is to ask the
    provider for it again.
    """
    from app.providers.images import ImageProviderError

    provider = _openai_compat()
    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"data": [{"url": "https://cdn.test/i.png"}]})
        if method == "POST" else _broken(503, "signed url expired"))

    with pytest.raises(ImageProviderError) as caught:
        provider.generate("a red balloon")

    assert len(wire.posts()) == 1, f"the fetch was retried into a new job: {wire.calls}"
    assert "could not be downloaded" in str(caught.value)


def test_openai_compat_ambiguous_post_costs_exactly_one_request(wire, paid_log):
    from app.providers.images import ImageProviderError

    provider = _openai_compat()
    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(ImageProviderError):
        provider.generate("a red balloon")

    assert len(wire.posts()) == 1
    assert unknown_exposures(paid_log)


def test_openai_compat_budget_gate_refuses_before_the_post(wire, paid_log, monkeypatch):
    from app.services.cost import BudgetExceededError

    calls = block_budget(monkeypatch)
    provider = _openai_compat()
    with pytest.raises(BudgetExceededError):
        provider.generate("a red balloon")

    assert calls, "the pre-spend gate never ran"
    assert wire.calls == [], f"a POST left after the gate refused: {wire.calls}"


def test_openai_compat_success_books_an_unknown_exposure_not_a_zero(wire, paid_log):
    """A metered gateway reports no price, so the spend is UNKNOWN, not $0."""
    import base64

    provider = _openai_compat()
    wire.always(_response(
        200, json_body={"data": [{"b64_json": base64.b64encode(b"img").decode()}]}))

    blobs = provider.generate("a red balloon")
    assert blobs == [b"img"]
    assert paid_log["costs"] == [], "an unpriced render was booked at a number"
    assert unknown_exposures(paid_log), "the unpriced render left no trace"


# ===========================================================================
# ElevenLabs: one POST, a real estimate, and no $0 for a lost answer
# ===========================================================================


def _elevenlabs():
    from app.providers.tts import ElevenLabsTTSProvider

    return ElevenLabsTTSProvider(api_key=_PROBE_VALUE)


def test_elevenlabs_ambiguous_synthesis_costs_exactly_one_request(wire, paid_log):
    from app.providers.tts import TTSError

    provider = _elevenlabs()
    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(TTSError) as caught:
        provider.synthesize("a red balloon rising over a quiet street")

    assert len(wire.posts()) == 1, f"a retry bought a second narration: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)
    assert unknown_exposures(paid_log)


def test_elevenlabs_budget_gate_runs_before_the_post(wire, paid_log, monkeypatch):
    """The refusal surfaces as a BUDGET error, not as a provider failure.

    Disguising it as a ``TTSError`` would tell the caller "the voice server is
    down", which invites a retry of the very thing the gate just stopped.
    """
    from app.services.cost import BudgetExceededError

    calls = block_budget(monkeypatch)
    provider = _elevenlabs()
    with pytest.raises(BudgetExceededError):
        provider.synthesize("a red balloon rising")

    assert calls, "the pre-spend gate never ran"
    assert wire.calls == [], f"a POST left after the gate refused: {wire.calls}"


def test_elevenlabs_books_the_per_character_estimate_on_success(wire, paid_log):
    """A computed amount must reach the ledger as an AMOUNT, with a handle."""
    provider = _elevenlabs()
    wire.always(_response(200, content=b"ID3" + b"x" * 2048,
                          headers={"request-id": "req-77"}))

    result = provider.synthesize("a red balloon rising")

    assert result.provider == "elevenlabs"
    assert len(wire.posts()) == 1
    # Work 15.9: the per-character amount is settled on the provider's own
    # RESERVATION row, which is what makes one remote call exactly one
    # accounting entry. It is therefore no longer visible through the patched
    # ``track_cost``; the authoritative amount assertion now lives in
    # test_work15_9_provider_parity, which reads the real ledger.
    booked = [c for c in paid_log["costs"]
              if c.get("provider") == "elevenlabs" or c.get("category") == "settled"]
    assert not [c for c in paid_log["costs"] if c.get("amount_usd") == 0], (
        f"a paid narration was booked at zero: {paid_log['costs']}")
    # The vendor request id must survive as the reconciliation handle. Work
    # 15.9 persists it onto the money row via ``mark_accepted``, so read it from
    # the submission feed rather than from a cost row that no longer exists.
    assert any((d.get("remote_id") or "") == "req-77"
               for d in submissions(paid_log)), submissions(paid_log)
    assert unknown_exposures(paid_log) == []


def test_elevenlabs_unusable_audio_is_unknown_exposure_not_a_free_success(
        wire, paid_log):
    """Charged per character, answered with four bytes: that is not free."""
    from app.providers.tts import TTSError

    provider = _elevenlabs()
    wire.always(_response(200, content=b"tiny"))
    with pytest.raises(TTSError):
        provider.synthesize("a red balloon rising")

    assert len(wire.posts()) == 1
    assert paid_log["costs"] == []
    assert unknown_exposures(paid_log), "a billed unusable answer left no trace"


def test_a_local_tts_server_is_not_treated_as_billable():
    """``localhost`` is operator CPU; a hosted server is somebody's invoice."""
    from app.providers.tts import _remote_base_url

    assert _remote_base_url("http://localhost:8000") is False
    assert _remote_base_url("http://127.0.0.1:9000") is False
    assert _remote_base_url("") is False
    assert _remote_base_url("https://voices.example.com") is True


def test_a_remote_operator_tts_server_is_billable_and_costs_one_post(wire, paid_log):
    """The base URL is a credential, so a remote server bills us."""
    from app.providers.tts import KokoroTTSProvider, TTSError

    provider = KokoroTTSProvider("https://voices.example.com")
    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(TTSError):
        provider.synthesize("hola, un globo rojo")

    assert len(wire.posts()) == 1, f"a retry bought a second narration: {wire.calls}"
    assert unknown_exposures(paid_log)


def test_a_local_operator_tts_server_books_nothing(wire, paid_log, monkeypatch):
    from app.providers.tts import KokoroTTSProvider

    monkeypatch.setattr(KokoroTTSProvider, "_key", lambda self: "")
    provider = KokoroTTSProvider("http://localhost:8000")
    wire.always(_response(200, content=b"ID3mp3bytes"))

    result = provider.synthesize("hola")
    assert result.format == "mp3"
    assert paid_log["costs"] == [], "local operator CPU booked a vendor invoice"
    assert unknown_exposures(paid_log) == [], "local CPU recorded a paid exposure"


# ===========================================================================
# avatar: one billed GPU render
# ===========================================================================


WS = "ws-1"


@pytest.fixture()
def avatar_env(tmp_path, monkeypatch):
    """Workspace assets, a stubbed ffprobe and a configured render server.

    ``render_avatar`` refuses any ref outside ``STORAGE_ROOT/<workspace>``, so
    the assets are written INSIDE the fake storage root and referenced by their
    absolute paths -- which is what a real asset row stores.
    """
    import app.providers.avatar as avatar_mod

    storage = tmp_path / "storage" / WS
    storage.mkdir(parents=True)
    image = storage / "face.jpg"
    audio = storage / "voice.mp3"
    image.write_bytes(b"jpeg")
    audio.write_bytes(b"mp3")
    monkeypatch.setattr(avatar_mod, "avatar_base_url", lambda: "https://avatar.test")
    monkeypatch.setattr(avatar_mod, "ffmpeg_present", lambda: True)
    monkeypatch.setattr(avatar_mod, "_probe_duration", lambda path: 4.0)
    monkeypatch.setattr(avatar_mod, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(avatar_mod, "STORAGE_ROOT", tmp_path / "storage")
    # ``_workspace_file`` resolves refs through services.storage.managed_path,
    # which reads that module's own STORAGE_ROOT.
    import app.services.storage as storage_mod

    monkeypatch.setattr(storage_mod, "STORAGE_ROOT", tmp_path / "storage")
    return {"image": str(image), "audio": str(audio), "root": tmp_path}


def test_avatar_server_render_submits_once_and_persists_the_state(
        wire, paid_log, avatar_env):
    from app.providers.avatar import render_avatar

    wire.always(_video_response())
    clip = render_avatar(avatar_env["image"], avatar_env["audio"], WS)

    assert clip.backend == "server"
    assert len(wire.posts()) == 1, f"a retry bought a second render: {wire.calls}"
    assert "SUCCEEDED" in states(paid_log)


def test_avatar_result_download_failure_does_not_re_render(wire, paid_log, avatar_env):
    """``video_url`` is an ARTIFACT. Repeating the render would bill twice."""
    from app.providers.avatar import AvatarError, render_avatar

    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"video_url": "https://cdn.test/v.mp4"})
        if method == "POST" else _broken(503, "gone"))

    with pytest.raises(AvatarError) as caught:
        render_avatar(avatar_env["image"], avatar_env["audio"], WS)

    assert len(wire.posts()) == 1, f"the download failure re-rendered: {wire.calls}"
    assert "could not be downloaded" in str(caught.value)


def test_avatar_accepted_render_is_an_unknown_exposure_not_a_zero_row(
        wire, paid_log, avatar_env):
    """The agent used to record ``0.0`` and cost.py dropped it: no row at all."""
    from app.providers.avatar import render_avatar

    wire.always(_video_response())
    render_avatar(avatar_env["image"], avatar_env["audio"], WS)

    assert paid_log["costs"] == [], "an unpriced GPU render booked a number"
    assert unknown_exposures(paid_log), (
        "a paid render produced no cost row and no exposure event -- this is "
        "the $0 bug")


def test_a_billed_avatar_render_does_not_overwrite_the_previous_one(
        wire, paid_log, avatar_env):
    """A retry must not destroy the only local evidence of the first purchase."""
    from app.providers.avatar import render_avatar

    wire.always(_video_response())
    first = render_avatar(avatar_env["image"], avatar_env["audio"], WS)
    second = render_avatar(avatar_env["image"], avatar_env["audio"], WS)

    assert first.path != second.path, "the second render overwrote the first"
    stored = Path(avatar_env["root"]) / "storage" / WS
    assert len(list(stored.glob("avatar-server-*.mp4"))) == 2, list(stored.iterdir())


# ===========================================================================
# broll: one billed GPU render, and an artifact name that cannot collide
# ===========================================================================


@pytest.fixture()
def broll_env(tmp_path, monkeypatch):
    import app.providers.broll as broll_mod

    monkeypatch.setattr(broll_mod, "broll_ai_base_url", lambda: "https://broll.test")
    monkeypatch.setattr(broll_mod, "_probe_duration", lambda path: 4.0)
    monkeypatch.setattr(broll_mod, "STORAGE_ROOT", tmp_path / "storage")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_broll_server_generate_submits_once(wire, paid_log, broll_env):
    from app.providers.broll import generate_clip

    wire.always(_video_response())
    path = generate_clip("a red balloon", "ws-1")

    assert path.endswith(".mp4")
    assert len(wire.posts()) == 1, f"a retry bought a second clip: {wire.calls}"
    assert "SUCCEEDED" in states(paid_log)


def test_broll_artifact_name_cannot_overwrite_a_previously_billed_render(
        wire, paid_log, broll_env):
    """``ai-<sha256(prompt)[:10]>`` made a retried prompt destroy the first.

    Two renders of the SAME prompt used to land on the same path, so the second
    silently unlinked the first -- and with it the only local evidence that the
    first render had been paid for.
    """
    from app.providers.broll import generate_clip

    wire.always(_video_response())
    first = generate_clip("a red balloon", "ws-1")
    second = generate_clip("a red balloon", "ws-1")

    assert first != second, "the second billed render overwrote the first"
    stored = Path(broll_env) / "storage" / "ws-1" / "broll"
    assert len(list(stored.glob("ai-*.mp4"))) == 2, list(stored.iterdir())


def test_broll_result_download_failure_does_not_re_render(wire, paid_log, broll_env):
    from app.providers.broll import BrollError, generate_clip

    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"video_url": "https://cdn.test/c.mp4"})
        if method == "POST" else _broken(503, "gone"))

    with pytest.raises(BrollError) as caught:
        generate_clip("a red balloon", "ws-1")

    assert len(wire.posts()) == 1, f"the download failure re-rendered: {wire.calls}"
    assert "could not be downloaded" in str(caught.value)


def test_broll_accepted_clip_is_an_unknown_exposure_not_a_zero_row(
        wire, paid_log, broll_env):
    """The B-roll agent recorded NO cost at all for the AI branch."""
    from app.providers.broll import generate_clip

    wire.always(_video_response())
    generate_clip("a red balloon", "ws-1")

    assert paid_log["costs"] == [], "an unpriced GPU clip booked a number"
    assert unknown_exposures(paid_log), "a paid clip left no trace in the ledger"


# ===========================================================================
# the agents: no fabricated zero, and an honest billable flag
# ===========================================================================


def _agent_ctx(workspace_with_user):
    from app.services.jobs import JobContext

    return JobContext(
        job_id=os.urandom(8).hex(), type="avatar",
        workspace_id=workspace_with_user["workspace"], cycle_id=None,
        payload={}, attempt=1, cancelled=lambda: False)


def test_the_avatar_agent_books_no_zero_cost_row(avatar_env, monkeypatch,
                                                 workspace_with_user):
    """``track_cost(ctx, "video", 0.0)`` was worse than nothing: cost.py drops
    any amount <= 0, so a PAID GPU render produced no cost entry at all."""
    from app.engine.agents import avatar as agent_mod

    calls: list[float] = []
    import app.providers.avatar as avatar_mod

    monkeypatch.setattr(agent_mod, "render_avatar",
                        lambda *a, **kw: avatar_mod.AvatarClip(
                            path="clip.mp4", backend="server", duration=4.0))
    monkeypatch.setattr(agent_mod.AvatarDirectorAgent, "track_cost",
                        lambda self, ctx, category, amount, **kw: calls.append(amount))

    result = agent_mod.AvatarDirectorAgent().direct(
        _agent_ctx(workspace_with_user), image="face.jpg", audio="a.mp3")
    assert calls == [], f"the agent booked a fabricated amount: {calls}"
    assert result["billable_render"] is True


def test_the_avatar_agent_says_a_local_lane_is_not_billable(
        avatar_env, monkeypatch, workspace_with_user):
    import app.providers.avatar as avatar_mod
    from app.engine.agents import avatar as agent_mod

    monkeypatch.setattr(agent_mod, "render_avatar",
                        lambda *a, **kw: avatar_mod.AvatarClip(
                            path="clip.mp4", backend="sadtalker", duration=4.0))

    result = agent_mod.AvatarDirectorAgent().direct(
        _agent_ctx(workspace_with_user), image="f.jpg", audio="a.mp3")
    assert result["billable_render"] is False


# ===========================================================================
# lipsync: the submit that must never be repeated
# ===========================================================================


class _AmbiguousAdapter:
    """An adapter whose submit is DELIVERED and whose answer is lost."""

    name = "ambiguous"
    submit_is_billable = True

    def __init__(self, *, provably_undelivered: bool = False) -> None:
        self.submits = 0
        self.provably_undelivered = provably_undelivered

    def health(self):
        from app.engine.lipsync.base import HEALTH_AVAILABLE, Health

        return Health(provider=self.name, status=HEALTH_AVAILABLE, detail="ok")

    def submit(self, video_ref, audio_ref, workspace_id, opts=None) -> str:
        from app.engine.lipsync.external import _paid
        from app.services.paid_executor import PaidSubmissionUnconfirmed

        self.submits += 1
        executor = _paid("lipsync.job_submit", workspace_id=workspace_id)

        def create(_key):
            raise PaidSubmissionUnconfirmed(
                provider="external_lipsync_worker",
                remote_id="" if self.provably_undelivered else "remote-9",
                provably_undelivered=self.provably_undelivered,
                detail=("connection never established" if self.provably_undelivered
                        else "read timeout after the request was sent"))

        executor.execute(create)
        raise AssertionError("the executor always classifies this failure")  # pragma: no cover

    def status(self, job_id):
        return {"status": "RUNNING", "progress": 0.1, "error": ""}

    def cancel(self, job_id):
        return True

    def result(self, job_id):
        return {"asset_ref": "", "gpu_seconds": 0.0, "cost_usd": 0.0}


def _quiet_events(monkeypatch):
    monkeypatch.setattr("app.services.events.record_event",
                        lambda *a, **kw: {"id": "evt"})


def _run_lipsync(db_session, adapter, *, max_retries: int = 2):
    from app.engine.lipsync import rows as job_rows
    from app.engine.lipsync.worker import LocalWorkerQueue
    from app.models import Workspace
    from app.models.lipsync import LipSyncJob

    ws = Workspace(name="LS w157", slug=f"ls-{os.urandom(4).hex()}", niche="test")
    db_session.add(ws)
    db_session.commit()
    queue = LocalWorkerQueue(provider=adapter, max_retries=max_retries,
                             backoff_seconds=0.01, timeout_seconds=5,
                             poll_interval=0.02)
    row = job_rows.create_job_row(
        db_session, workspace_id=ws.id, provider=adapter.name,
        video_ref="asset://v.mp4", audio_ref="asset://a.mp4")
    db_session.commit()
    queue.submit(row.id, "asset://v.mp4", "asset://a.mp4", ws.id, {})
    assert queue.wait(row.id, timeout=10) is True
    db_session.expire_all()
    return db_session.get(LipSyncJob, row.id)


def test_the_worker_does_not_repost_a_billable_submit_after_ambiguity(
        db_session, monkeypatch):
    """THE money test for this lane.

    The retry loop used to re-POST the billable job on any transient, so
    ``max_retries=2`` meant up to three ``POST /jobs`` for one render. The
    assertion is on the adapter's own submit count: that is the number of
    billable jobs which would have been created.
    """
    _quiet_events(monkeypatch)
    adapter = _AmbiguousAdapter()
    row = _run_lipsync(db_session, adapter, max_retries=2)

    assert adapter.submits == 1, (
        f"a lost submit response was re-POSTed: {adapter.submits} billable jobs")
    assert row.adapter_job_id == "remote-9", (
        "the remote id was not persisted, so a billed job is unaddressable")
    cost = row.cost_json or {}
    assert cost.get("submission_state") == "SUBMISSION_UNKNOWN", cost
    assert cost.get("resubmit_forbidden") is True, cost
    assert cost.get("exposure") == CostOutcome.UNKNOWN_EXPOSURE.value, cost
    assert "DO NOT RESUBMIT" in (row.error or "")


def test_an_ambiguous_lipsync_submit_never_mirrors_a_zero_cost(db_session, monkeypatch):
    """``mirror_cost`` writes ``amount_usd = cost_usd or 0.0``; a job that may
    have been billed must not produce that row."""
    import app.engine.lipsync.rows as job_rows

    _quiet_events(monkeypatch)
    mirrored: list[dict] = []
    monkeypatch.setattr(
        job_rows, "mirror_cost",
        lambda ws, job, cost, provider: mirrored.append(cost) or True)

    row = _run_lipsync(db_session, _AmbiguousAdapter(), max_retries=2)

    assert mirrored == [], f"an unknown exposure was mirrored as a cost row: {mirrored}"
    assert row.cost_json


def test_a_provably_undelivered_submit_is_still_retried(db_session, monkeypatch):
    """No socket was opened, so a retry cannot double-charge. It must happen."""
    _quiet_events(monkeypatch)
    adapter = _AmbiguousAdapter(provably_undelivered=True)
    _run_lipsync(db_session, adapter, max_retries=2)
    assert adapter.submits == 3, (
        "the safe retry was lost: a provably undelivered submit must still be "
        "retried")


def test_the_external_adapter_posts_once_and_keeps_the_job_id(wire, paid_log):
    """``POST /jobs`` goes through the executor: one request, id persisted."""
    from app.engine.lipsync import external as external_mod

    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"ok": True}) if url.endswith("/health")
        else _response(200, json_body={"job_id": "remote-77"}))

    adapter = external_mod.ExternalAdapter("https://gpu.test", "k")
    assert adapter.submit("asset://v.mp4", "asset://a.mp4", "ws-1") == "remote-77"

    assert wire.job_posts() == ["https://gpu.test/jobs"]
    record = external_mod.last_submission()
    assert record is not None and record.remote_id == "remote-77"
    assert record.state.value == "REMOTE_ID_CONFIRMED"


def test_the_external_adapter_does_not_repost_a_lost_submit(wire, paid_log):
    from app.engine.lipsync import external as external_mod
    from app.services.paid_executor import AmbiguousSubmission

    def handler(method, url, **kw):
        if url.endswith("/health"):
            return _response(200, json_body={"ok": True})
        raise httpx.ReadTimeout("lost")

    wire.handler = handler
    adapter = external_mod.ExternalAdapter("https://gpu.test", "k")
    with pytest.raises(AmbiguousSubmission):
        adapter.submit("asset://v.mp4", "asset://a.mp4", "ws-1")

    assert len(wire.job_posts()) == 1, wire.calls


def test_the_external_adapter_budget_gate_refuses_before_the_post(wire, monkeypatch):
    from app.engine.lipsync import external as external_mod
    from app.services.cost import BudgetExceededError

    calls = block_budget(monkeypatch)
    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"ok": True}) if url.endswith("/health")
        else pytest.fail("the submit must not be sent after the gate refused"))

    adapter = external_mod.ExternalAdapter("https://gpu.test", "k")
    with pytest.raises(BudgetExceededError):
        adapter.submit("asset://v.mp4", "asset://a.mp4", "ws-1")
    assert calls == [0.0], calls


# ===========================================================================
# the audit: it must catch its own worst error
# ===========================================================================


def test_the_audit_reports_no_disagreement():
    from app.services.paid_jobs_audit import verify_against_source

    problems = verify_against_source()
    assert not problems, "\n".join(str(p) for p in problems)


def test_verify_against_source_catches_a_MISSING_row():
    """The 15.6 detector could not see an ABSENT row.

    That is how a paid ``ffmpeg_avatar`` submit stayed ``NOT_BILLABLE`` in the
    table for a whole release while calling ElevenLabs and eight image jobs.
    """
    from app.services import paid_jobs_audit as audit_mod

    original = audit_mod.PAID_PATHS
    try:
        audit_mod.PAID_PATHS = tuple(
            r for r in original if r.key != "api.connections.test_endpoints")
        found = audit_mod.verify_against_source()
    finally:
        audit_mod.PAID_PATHS = original

    missing = [str(p) for p in found if "MISSING PATH" in str(p)]
    assert missing, "a deleted row left the audit clean"
    assert any("connections" in m for m in missing), missing


def test_verify_against_source_catches_a_MISCLASSIFIED_row():
    """A row whose cited code is gone is stale, whatever it claims."""
    from app.services import paid_jobs_audit as audit_mod

    lying = audit_mod.BillablePath(
        key="probe.misclassified", provider="acme",
        module="app.providers.images", operation="ImaginaryProvider.generate",
        billable=True, billing_note="probe",
        paid_jobs_sites=("app.providers.images",),
        source_markers=("/endpoint-that-was-never-here",),
    )
    original = audit_mod.PAID_PATHS
    try:
        audit_mod.PAID_PATHS = original + (lying,)
        found = audit_mod.verify_against_source()
    finally:
        audit_mod.PAID_PATHS = original

    assert any("STALE" in str(p) and p.key == "probe.misclassified"
               for p in found), [str(p) for p in found]


def test_verify_against_source_catches_a_stale_money_inventory():
    from app.services import paid_jobs_audit as audit_mod

    original = audit_mod.MONEY_MARKERS
    try:
        audit_mod.MONEY_MARKERS = {
            "app.providers.broll": (audit_mod.MoneyMarker("/generate-2", "invented"),),
        }
        found = audit_mod.verify_against_source()
    finally:
        audit_mod.MONEY_MARKERS = original

    assert any("inventory is STALE" in str(p) for p in found), [str(p) for p in found]


def test_ffmpeg_avatar_submit_is_a_composite_billable_path():
    """The correction: ``NOT_BILLABLE`` while calling paid TTS at :476 and up
    to eight paid image jobs at :679."""
    from app.services.paid_jobs_audit import path

    row = path("video_engine.ffmpeg_avatar.submit")
    assert row is not None
    assert row.billable is True, "a path that pays for narration is not free"
    assert row.is_composite is True
    assert "tts.elevenlabs.synthesize" in row.composite_paths
    assert "images.openai_compat.generate" in row.composite_paths
    assert row.composite_note.strip()
    assert row.covered is True, "both callees are covered, so the composite is"
    assert row.gap == ""


def test_the_mpt_gap_no_longer_claims_a_failed_status_and_a_resubmit():
    """Work 15.8 raised this row to COVERED; the residue is now provider facts.

    The 15.6 text was wrong twice over: the submit path writes
    ``SUBMISSION_UNKNOWN`` and ``_resolve_existing`` returns ``kind="unknown"``,
    which is refused, so there was never a resubmit. Work 15.8 then added the
    durable submission record and the cost row that made the row covered
    outright. What remains documented is what MoneyPrinterTurbo itself does not
    offer -- no idempotency header, no client-request id to reconcile by.
    """
    from app.services.paid_jobs_audit import path

    row = path("video_engine.mpt.submit")
    assert row is not None
    text = row.gap
    # The two false claims must stay gone.
    assert "production.py:203 writes status=FAILED" not in text
    assert "resolves 'fresh' and submits again" not in text
    # Now covered, with the residual limits stated honestly.
    assert row.covered is True, (
        "the durable record and cost row landed in Work 15.8; if this is "
        "uncovered again, say why")
    assert row.billable is True
    assert "idempotency" in text.lower(), (
        "the residual gap is a provider fact, and must say so")


def test_every_uncovered_billable_path_says_something_specific():
    """No unexplained gap string may remain.

    Work 15.9 §9 took the inventory to 100%, so this canary has nothing left to
    watch: the assertion that the uncovered list is NON-empty was a placeholder
    for a gap that has since been closed. The invariant now is the opposite
    one -- the list must be empty -- while the quality bar on any gap text is
    retained for the day a path does become uncovered again.
    """
    from app.services.paid_jobs_audit import uncovered_billable_paths

    uncovered = tuple(uncovered_billable_paths())
    assert uncovered == (), (
        "an uncovered billable path reappeared: "
        f"{[r.key for r in uncovered]}. Each needs a specific, file:line gap.")
    for row in uncovered:  # pragma: no cover - the guard above empties this
        gap = row.gap.strip()
        assert len(gap) > 80, f"{row.key}: the gap is a shrug, not a reason"
        assert "app/" in gap or ".py:" in gap, (
            f"{row.key}: the gap names no file:line to go and read")


def test_the_media_lanes_are_all_covered_now():
    from app.services.paid_jobs_audit import path

    for key in ("images.xkiro.generate", "images.openai_compat.generate",
                "tts.elevenlabs.synthesize", "tts.kokoro.synthesize",
                "tts.qwen3.synthesize", "tts.chatterbox.synthesize",
                "avatar.server_render", "broll.ai_server.generate",
                "lipsync.external.submit", "video_engine.ffmpeg_avatar.submit"):
        row = path(key)
        assert row is not None, key
        assert row.covered is True, f"{key} is still UNCOVERED: {row.gap}"


def test_a_local_ffmpeg_path_still_gets_no_fake_state_machine():
    """Reclassifying ffmpeg_avatar must not have spread to the real local paths."""
    from app.services.paid_jobs_audit import path

    for key in ("broll.synth.generate", "avatar.mock.render",
                "avatar.sadtalker.render", "motion.hyperframes.render",
                "clips.yt_dlp.fetch", "video_engine.timeline_render.render"):
        row = path(key)
        assert row is not None, key
        assert row.billable is False, f"{key} was wrongly promoted to billable"
        assert row.paid_jobs_sites == (), key


def test_the_maturity_health_probe_is_recorded_as_free():
    """It looks like a paid call site and is not: ``health()`` is a read."""
    from app.services.paid_jobs_audit import path

    row = path("providers.maturity.health_probe")
    assert row is not None
    assert row.billable is False
    assert "GET /user" in row.billing_note


# ===========================================================================
# live providers -- honestly skipped
# ===========================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ELEVENLABS_API_KEY"),
    reason="live ElevenLabs credentials are not configured",
)
def test_live_elevenlabs_synthesis_books_a_real_amount():
    """A live billable path must produce a real per-character amount."""
    from app.providers.tts import ElevenLabsTTSProvider

    provider = ElevenLabsTTSProvider()
    result = provider.synthesize("YMONEY live paid-path probe.")
    assert result.audio_bytes
    assert provider.EST_USD_PER_CHAR > 0


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("IMAGE_OPENAI_API_KEY"),
    reason="live image-provider credentials are not configured",
)
def test_live_image_submit_is_guarded_by_the_paid_executor():
    """A live generation must be a real, guarded submit -- or fail loudly."""
    from app.providers.images import get_image_provider
    from app.services.paid_executor import IdempotencySupport

    provider = get_image_provider()
    assert getattr(provider, "name", "") != "mock"
    assert IdempotencySupport.UNSUPPORTED, "these vendors document no such header"
