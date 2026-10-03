"""Work 15.9 §4/§3/§5/§6 -- provider parity, captured BEFORE the migration.

Work 15.9 §8 pulled the money mechanics out of four providers into
:mod:`app.services.paid_provider`. The rule for a refactor that touches the code
path a vendor bills for is that the OBSERVABLE REQUEST CONTRACT must not move:
how many requests left, what was in them, how long we waited, what came back and
what a caller sees when it all goes wrong. Those are the clauses in this file's
**Group A**, and every one of them was run against the pre-migration code and
again after it.

**Group A** (parity: passes before AND after) pins, per provider:

* the number of outbound requests -- a second POST is a second purchase;
* the exact method, URL, JSON body / multipart fields and headers;
* the timeout handed to the transport;
* the artifact the caller receives;
* the polling cadence shape for the async lane (xKiro);
* the error TYPE and the class of message for a lost response, a 4xx, a 2xx with
  no artifact and an unreadable artifact;
* that an ambiguous submit NEVER becomes a second request.

**Group B** pins the accounting that §1/§6 deliberately CHANGED, and each of
those is expected to fail before the migration:

* a billable operation with no tenant is REFUSED before any request leaves
  (``OwnerlessSpendRefused``), instead of running unreserved and uncharged;
* an unpriced accepted render leaves a ledger row marked ``UNKNOWN_EXPOSURE``
  instead of no row at all plus a log line;
* a priced operation books exactly ONE owned ledger row, settled in place;
* reattaching to a remote job does not reserve, does not double-settle, and
  yields exactly one accounting identity.

Everything is asserted at the OUTBOUND BOUNDARY and the LEDGER, never at a
private helper. A test that imported ``images._paid`` would be asserting the
shape of the refactor rather than the behaviour it had to preserve.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest

from app.services.paid_executor import CostOutcome

#: A dummy value handed to a provider constructor so the adapter builds. No
#: request ever leaves the process: the httpx boundary is recorded, not sent.
_PROBE_VALUE = "w159-not-a-credential"


# ===========================================================================
# the outbound boundary: a recorder, not a mock of our own code
# ===========================================================================


class Recorder:
    """Counts REAL outbound calls and scripts the answers.

    Every provider imports ``httpx`` inside the function that needs it, so
    patching the attributes on the ``httpx`` module itself intercepts every
    request without touching provider code.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.handler = None
        #: Timeouts every ``httpx.Client`` was constructed with. A provider that
        #: sets its deadline on the CLIENT rather than the call is not a different
        #: provider -- but a wrong number here is a 30-second-or-30-minute bill.
        self.client_timeouts: list[object] = []

    def posts(self) -> list[str]:
        return [url for method, url, _ in self.calls if method == "POST"]

    def gets(self) -> list[str]:
        return [url for method, url, _ in self.calls if method == "GET"]

    def kwargs_of(self, method: str, url: str, index: int = 0) -> dict:
        """The recorded kwargs of the ``index``-th matching request."""
        seen = [kw for m, u, kw in self.calls if m == method and u == url]
        if index >= len(seen):
            raise AssertionError(
                f"no recorded {method} {url} #{index}: {self.calls}")
        return seen[index]

    def respond(self, method: str, url: str, **kw):
        self.calls.append((method, url, kw))
        if self.handler is None:
            raise AssertionError(f"unscripted outbound {method} {url}")
        return self.handler(method, url, **kw)

    def always(self, *outcome):
        def handler(_method, _url, **_kw):  # noqa: ARG001 - uniform script
            value = outcome[0]
            if isinstance(value, BaseException):
                raise value
            return value
        self.handler = handler
        return self


class _RecordingClient:
    def __init__(self, *a, **kw) -> None:
        _RecordingClient._rec.client_timeouts.append(kw.get("timeout"))

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def post(self, url, **kw):
        return self._rec.respond("POST", str(url), **kw)

    def get(self, url, **kw):
        return self._rec.respond("GET", str(url), **kw)


@pytest.fixture()
def wire(monkeypatch):
    rec = Recorder()
    _RecordingClient._rec = rec          # noqa: SLF001 - test double wiring
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    monkeypatch.setattr(httpx, "post",
                        lambda url, **kw: rec.respond("POST", str(url), **kw))
    monkeypatch.setattr(httpx, "get",
                        lambda url, **kw: rec.respond("GET", str(url), **kw))
    return rec


@pytest.fixture()
def events(monkeypatch):
    """Capture what each provider persists in the activity feed.

    Providers import ``app.services.events`` INSIDE the function that needs it,
    so patching the module attribute is enough and no provider code has to know
    this test exists.
    """
    seen: list[dict] = []

    def record_event(workspace_id, kind, message, level="info", source="system",
                     data=None):
        seen.append({"workspace_id": workspace_id, "kind": kind,
                     "message": str(message), "level": level, "source": source,
                     "data": dict(data or {})})
        return {"id": "evt"}

    monkeypatch.setattr("app.services.events.record_event", record_event)
    return seen


# ===========================================================================
# owners and the ledger
# ===========================================================================


@pytest.fixture()
def owner(db_session):
    """A real tenant id, because an ownerless spend is refused by §1."""
    from app.models import Workspace

    ws = Workspace(name="paid159", slug=f"p159-{os.urandom(4).hex()}",
                   niche="test")
    db_session.add(ws)
    db_session.flush()
    db_session.commit()
    return str(ws.id)


@pytest.fixture()
def scoped(owner):
    """The tenant in scope, the way a request thread would see it."""
    from app.services import provider_settings

    with provider_settings.workspace_scope(owner):
        yield owner


def ledger(workspace_id: str) -> list[dict]:
    """Every ledger row this workspace owns, as plain dicts."""
    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as s:
        return [{"entry_id": row.id, "category": row.category,
                 "amount_usd": float(row.amount_usd or 0.0),
                 "provider": row.provider,
                 "detail": dict(row.detail_json or {})}
                for row in s.query(CostEntry)
                .filter(CostEntry.workspace_id == workspace_id)
                .all()]


def rows_without_owner() -> list[dict]:
    """Ledger rows nobody owns. Every one of these is an unattributable charge."""
    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as s:
        return [{"entry_id": row.id, "category": row.category,
                 "amount_usd": float(row.amount_usd or 0.0),
                 "provider": row.provider, "detail": dict(row.detail_json or {})}
                for row in s.query(CostEntry)
                .filter(CostEntry.workspace_id.in_(["", None])).all()]


def _response(status: int = 200, *, json_body=None, content: bytes = b"",
              headers: dict | None = None, method: str = "POST") -> httpx.Response:
    """A real ``httpx.Response`` so the shared classifier can read its status."""
    request = httpx.Request(method, "https://provider.test/x")
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers or {},
                              request=request)
    return httpx.Response(status, content=content, headers=headers or {},
                          request=request)


def _video_response() -> httpx.Response:
    return _response(200, content=b"mp4" * 500, headers={"content-type": "video/mp4"})


def _broken(status: int = 500, text: str = "upstream exploded") -> httpx.Response:
    return _response(status, content=text.encode(), method="GET")


# ===========================================================================
# GROUP A -- parity: request count, payload, timeout, result, errors
# ===========================================================================


# ---------------------------------------------------------------------------
# images.openai_compat
# ---------------------------------------------------------------------------


@pytest.fixture()
def openai_images():
    from app.providers.images import OpenAICompatImageProvider

    return OpenAICompatImageProvider(base_url="https://img.test/v1",
                                     api_key=_PROBE_VALUE, model="m-1")


def test_openai_compat_sends_exactly_one_post_with_the_documented_payload(
        openai_images, wire, scoped):
    """One billable POST, the body the vendor documents, the configured timeout."""
    import base64 as _b64

    raw = b"\x89PNG" + b"x" * 64
    wire.always(_response(200, json_body={
        "data": [{"b64_json": _b64.b64encode(raw).decode()}]}))

    out = openai_images.generate("a red balloon", size="1024x576", n=1)

    assert wire.posts() == ["https://img.test/v1/images/generations"], wire.calls
    kw = wire.kwargs_of("POST", "https://img.test/v1/images/generations")
    assert kw["json"] == {"prompt": "a red balloon", "n": 1, "size": "1024x576",
                          "response_format": "b64_json", "model": "m-1"}
    assert kw["headers"]["Authorization"] == f"Bearer {_PROBE_VALUE}"
    assert kw["headers"]["Content-Type"] == "application/json"
    assert kw["timeout"] == openai_images.timeout == 120.0
    assert out == [raw]


def test_openai_compat_fetches_a_paid_url_without_posting_again(
        openai_images, wire, scoped):
    """The generation is already paid for; re-POSTing it would bill twice."""
    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"data": [{"url": "https://cdn.test/i.png"}]})
        if method == "POST" else
        _response(200, content=b"\x89PNG" + b"y" * 2048, method="GET"))

    out = openai_images.generate("a red balloon")

    assert out == [b"\x89PNG" + b"y" * 2048]
    assert wire.posts() == ["https://img.test/v1/images/generations"], wire.calls
    assert wire.gets() == ["https://cdn.test/i.png"]


def test_openai_compat_bills_two_images_as_two_posts_and_two_bodies(
        openai_images, wire, scoped):
    """``n=2`` is one request with ``n: 2`` here, and the caller gets 2 bytes."""
    encoded = base64.b64encode(b"z" * 32).decode()
    wire.always(_response(200, json_body={"data": [{"b64_json": encoded},
                                                    {"b64_json": encoded}]}))

    out = openai_images.generate("two balloons", n=2)

    assert len(out) == 2
    assert len(wire.posts()) == 1, wire.calls
    assert wire.kwargs_of("POST", wire.posts()[0])["json"]["n"] == 2


def test_openai_compat_an_ambiguous_timeout_costs_exactly_one_post(
        openai_images, wire, scoped):
    """A read timeout proves the POST was DELIVERED. A retry buys a second image."""
    from app.providers.images import ImageProviderError

    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(ImageProviderError) as caught:
        openai_images.generate("a red balloon")

    assert len(wire.posts()) == 1, f"a retry bought a second image: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_openai_compat_a_4xx_is_a_refusal_not_an_ambiguity(
        openai_images, wire, scoped):
    """The provider refused, so the caller may try again -- and it must say so."""
    from app.providers.images import ImageProviderError

    wire.always(_response(400, content=b"bad prompt"))

    with pytest.raises(ImageProviderError) as caught:
        openai_images.generate("a red balloon")

    assert len(wire.posts()) == 1, wire.calls
    assert "HTTP 400" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" not in str(caught.value)


def test_openai_compat_a_2xx_with_no_image_is_an_ambiguity(
        openai_images, wire, scoped):
    """Accepted, billed, unfulfilled: NOT a clean failure."""
    from app.providers.images import ImageProviderError

    wire.always(_response(200, json_body={"data": []}))

    with pytest.raises(ImageProviderError) as caught:
        openai_images.generate("a red balloon")

    assert len(wire.posts()) == 1, wire.calls
    assert "may have been billed" in str(caught.value)


# ---------------------------------------------------------------------------
# images.xkiro (the async lane: submit once, poll, download)
# ---------------------------------------------------------------------------


@pytest.fixture()
def xkiro(monkeypatch):
    from app.providers.images import XkiroImageProvider

    monkeypatch.setattr(XkiroImageProvider, "_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(XkiroImageProvider, "_POLL_DEADLINE", 5.0)
    return XkiroImageProvider(base_url="https://xkiro.test/v1",
                              api_key=_PROBE_VALUE)


def _xkiro_script(*, polls: list[str], job_id: str = "job-42",
                  size: str = "1792x1024") -> None:
    def handler(method, url, **kw):
        if method == "POST":
            return _response(200, json_body={"id": job_id})
        if url.endswith(f"/images/generations/{job_id}"):
            polls.append(url)
            return _response(200, json_body={
                "status": "succeeded",
                "data": [{"url": "https://cdn.test/i.png"}]}, method="GET")
        if url == "https://cdn.test/i.png":
            return _response(200, content=b"\x89PNG" + b"x" * 2048, method="GET")
        raise AssertionError(f"unexpected outbound {method} {url}")
    return handler


def test_xkiro_posts_once_polls_the_job_it_submitted_and_returns_the_bytes(
        xkiro, wire, scoped):
    """The submitted job id is the only handle on a billed job: it must persist."""
    polls: list[str] = []
    wire.handler = _xkiro_script(polls=polls)

    out = xkiro.generate("a red balloon", size="1024x576", n=1)

    assert out == [b"\x89PNG" + b"x" * 2048]
    assert wire.posts() == ["https://xkiro.test/v1/images/generations"], wire.calls
    assert polls == ["https://xkiro.test/v1/images/generations/job-42"]
    assert wire.gets() == ["https://xkiro.test/v1/images/generations/job-42",
                           "https://cdn.test/i.png"]
    kw = wire.kwargs_of("POST", "https://xkiro.test/v1/images/generations")
    assert kw["json"] == {"model": "sensenova/sensenova-u1.5-lite",
                          "prompt": "a red balloon", "n": 1, "size": "1792x1024"}
    # The submit's timeout is the CLIENT's, not the call's -- the poll GET then
    # re-derives a bounded phase timeout from the remaining deadline.
    assert wire.client_timeouts, "the job client carried no timeout at all"
    assert wire.client_timeouts[0] == xkiro.timeout == 30.0
    assert 0 < wire.kwargs_of(
        "GET", "https://xkiro.test/v1/images/generations/job-42"
    )["timeout"] <= 5.0


def test_xkiro_snaps_every_requested_size_to_a_supported_one(xkiro, wire, scoped):
    """A portrait job goes out as ``1024x1536``, never as the asker's string."""
    polls: list[str] = []
    wire.handler = _xkiro_script(polls=polls)

    xkiro.generate("a red balloon", size="1080x1920")

    kw = wire.kwargs_of("POST", "https://xkiro.test/v1/images/generations")
    assert kw["json"]["size"] == "1024x1536"


def test_xkiro_n_images_is_n_jobs_not_one_call(xkiro, wire, scoped):
    """The API takes one image per job, so ``n=3`` is three billable submits."""
    polls: list[str] = []
    wire.handler = _xkiro_script(polls=polls)

    out = xkiro.generate("three balloons", n=3)

    assert len(out) == 3
    assert len(wire.posts()) == 3, wire.calls
    for posted in wire.posts():
        assert wire.kwargs_of("POST", posted)["json"]["n"] == 1


def test_xkiro_an_ambiguous_submit_costs_exactly_one_post(xkiro, wire, scoped):
    from app.providers.images import ImageProviderError

    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(ImageProviderError) as caught:
        xkiro.generate("a red balloon")

    assert len(wire.posts()) == 1, f"a retry bought a second job: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_xkiro_a_failed_job_reports_the_provider_message(xkiro, wire, scoped):
    """Provider status semantics stay LOCAL: the message is the vendor's."""
    from app.providers.images import ImageProviderError

    def handler(method, url, **kw):
        if method == "POST":
            return _response(200, json_body={"id": "job-42"})
        return _response(200, json_body={
            "status": "blocked", "error": {"message": "safety filter"}},
            method="GET")

    wire.handler = handler
    with pytest.raises(ImageProviderError) as caught:
        xkiro.generate("a red balloon")

    assert "xkiro job blocked: safety filter" in str(caught.value)
    assert len(wire.posts()) == 1, wire.calls


# ---------------------------------------------------------------------------
# tts.elevenlabs (the priced lane)
# ---------------------------------------------------------------------------


@pytest.fixture()
def elevenlabs():
    from app.providers.tts import ElevenLabsTTSProvider

    return ElevenLabsTTSProvider(api_key=_PROBE_VALUE)


def test_elevenlabs_posts_once_with_the_documented_body(
        elevenlabs, wire, scoped):
    """Per-character billing means the body length IS the price."""
    wire.always(_response(200, content=b"mp3" * 400,
                          headers={"content-type": "audio/mpeg"}))

    result = elevenlabs.synthesize("hello there", rate=1.0)

    url = "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
    assert wire.posts() == [url], wire.calls
    kw = wire.kwargs_of("POST", url)
    assert kw["json"] == {
        "text": "hello there",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    assert kw["headers"]["xi-api-key"] == _PROBE_VALUE
    assert kw["headers"]["Accept"] == "audio/mpeg"
    assert kw["timeout"] == 120.0
    assert result.format == "mp3"
    assert result.provider == "elevenlabs"
    assert result.audio_bytes == b"mp3" * 400


def test_elevenlabs_sends_speed_only_when_it_differs(elevenlabs, wire, scoped):
    """A rate of exactly 1.0 adds no key; anything else is clamped and sent."""
    wire.always(_response(200, content=b"mp3" * 400))

    elevenlabs.synthesize("hello", rate=1.0)
    assert "speed" not in wire.kwargs_of("POST", wire.posts()[0])["json"]

    elevenlabs.synthesize("hello", rate=9.0)
    assert wire.kwargs_of("POST", wire.posts()[0], index=1)["json"]["speed"] == 1.2


def test_elevenlabs_an_ambiguous_timeout_costs_exactly_one_post(
        elevenlabs, wire, scoped):
    from app.providers.tts import TTSError

    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(TTSError) as caught:
        elevenlabs.synthesize("hello there")

    assert len(wire.posts()) == 1, f"a retry bought a second narration: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_elevenlabs_tiny_audio_is_an_ambiguity_not_a_success(
        elevenlabs, wire, scoped):
    """Charged per character, and the answer is unusable: a second call bills."""
    from app.providers.tts import TTSError

    wire.always(_response(200, content=b"mp3"))

    with pytest.raises(TTSError) as caught:
        elevenlabs.synthesize("hello there")

    assert len(wire.posts()) == 1, wire.calls
    assert "suspiciously small audio" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_elevenlabs_a_4xx_is_a_refusal(elevenlabs, wire, scoped):
    from app.providers.tts import TTSError

    wire.always(_response(401, content=b"bad key"))

    with pytest.raises(TTSError) as caught:
        elevenlabs.synthesize("hello there")

    assert len(wire.posts()) == 1, wire.calls
    assert "401" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" not in str(caught.value)


# ---------------------------------------------------------------------------
# tts operator speech server (the billable-if-remote lane)
# ---------------------------------------------------------------------------


@pytest.fixture()
def kokoro():
    from app.providers.tts import KokoroTTSProvider

    return KokoroTTSProvider(base_url="https://kokoro.test")


def test_operator_speech_posts_once_with_the_documented_body(kokoro, wire, scoped):
    wire.always(_response(200, content=b"mp3" * 400,
                          headers={"content-type": "audio/mpeg"}))

    result = kokoro.synthesize("hello there", rate=1.5, voice="af_heart")

    assert wire.posts() == ["https://kokoro.test/audio/speech"], wire.calls
    kw = wire.kwargs_of("POST", "https://kokoro.test/audio/speech")
    assert kw["json"] == {"model": "kokoro", "input": "hello there",
                          "voice": "af_heart", "response_format": "mp3",
                          "speed": 1.5}
    assert kw["timeout"] == 180
    assert result.audio_bytes == b"mp3" * 400


def test_operator_speech_clamps_the_speed_it_sends(kokoro, wire, scoped):
    wire.always(_response(200, content=b"mp3" * 400))

    kokoro.synthesize("hello", rate=99.0)
    assert wire.kwargs_of("POST", wire.posts()[0])["json"]["speed"] == 4.0


def test_operator_speech_a_2xx_with_no_audio_is_an_ambiguity(
        kokoro, wire, scoped):
    from app.providers.tts import TTSError

    wire.always(_response(200, content=b""))

    with pytest.raises(TTSError) as caught:
        kokoro.synthesize("hello there")

    assert len(wire.posts()) == 1, wire.calls
    assert "empty body" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_operator_speech_an_ambiguous_timeout_costs_exactly_one_post(
        kokoro, wire, scoped):
    from app.providers.tts import TTSError

    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(TTSError) as caught:
        kokoro.synthesize("hello there")

    assert len(wire.posts()) == 1, f"a retry bought a second narration: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_a_local_operator_server_is_not_charged_but_is_still_guarded(
        monkeypatch, wire, events):
    """A localhost Kokoro is operator CPU, so no ledger row -- but the
    classification of a lost response is unchanged (never a clean refusal)."""
    from app.providers.tts import KokoroTTSProvider, TTSError

    monkeypatch.setattr("app.providers.tts._remote_base_url",
                        lambda base_url: False)
    prov = KokoroTTSProvider(base_url="http://127.0.0.1:8080")
    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(TTSError) as caught:
        prov.synthesize("hello there")

    assert len(wire.posts()) == 1, wire.calls
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


# ---------------------------------------------------------------------------
# avatar.server_render
# ---------------------------------------------------------------------------


@pytest.fixture()
def avatar_env(tmp_path, monkeypatch, scoped):
    import app.providers.avatar as avatar_mod

    storage = tmp_path / "storage"
    image = storage / scoped / "presenter.jpg"
    audio = storage / scoped / "voice.mp3"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"jpeg")
    audio.write_bytes(b"mp3")
    monkeypatch.setattr(avatar_mod, "avatar_base_url", lambda: "https://avatar.test")
    monkeypatch.setattr(avatar_mod, "ffmpeg_present", lambda: True)
    monkeypatch.setattr(avatar_mod, "_probe_duration", lambda path: 4.0)
    monkeypatch.setattr(avatar_mod, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(avatar_mod, "STORAGE_ROOT", storage)
    import app.services.storage as storage_mod

    monkeypatch.setattr(storage_mod, "STORAGE_ROOT", storage)
    return {"image": str(image), "audio": str(audio), "root": tmp_path,
            "owner": scoped}


def test_avatar_posts_one_multipart_render_with_the_render_timeout(
        wire, avatar_env):
    """One billed GPU render, multipart, and the long timeout it really needs."""
    from app.providers.avatar import render_avatar

    wire.always(_video_response())
    clip = render_avatar(avatar_env["image"], avatar_env["audio"],
                         avatar_env["owner"])

    assert clip.backend == "server"
    assert len(wire.posts()) == 1, f"a retry bought a second render: {wire.calls}"
    kw = wire.kwargs_of("POST", "https://avatar.test/render")
    assert set(kw["files"]) == {"image", "audio"}
    assert kw["files"]["image"][0] == "presenter.jpg"
    assert kw["files"]["image"][2] == "image/jpeg"
    assert kw["files"]["audio"][2] == "audio/mpeg"
    assert kw["timeout"] == 1800
    assert clip.duration == 4.0


def test_avatar_stores_the_clip_under_a_name_carrying_the_submission(
        wire, avatar_env):
    """Two billed renders of the same assets must not overwrite each other."""
    from app.providers.avatar import render_avatar

    wire.always(_video_response())
    first = render_avatar(avatar_env["image"], avatar_env["audio"],
                          avatar_env["owner"])
    second = render_avatar(avatar_env["image"], avatar_env["audio"],
                           avatar_env["owner"])

    assert first.path != second.path, "the second render overwrote the first"
    stored = Path(avatar_env["root"]) / "storage" / avatar_env["owner"]
    assert len(list(stored.glob("avatar-server-*.mp4"))) == 2, list(stored.iterdir())


def test_avatar_a_failed_artifact_fetch_does_not_re_render(wire, avatar_env):
    """``video_url`` is an artifact. Repeating the render bills twice."""
    from app.providers.avatar import AvatarError, render_avatar

    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"video_url": "https://cdn.test/v.mp4"})
        if method == "POST" else _broken(503, "gone"))

    with pytest.raises(AvatarError) as caught:
        render_avatar(avatar_env["image"], avatar_env["audio"],
                      avatar_env["owner"])

    assert len(wire.posts()) == 1, f"the download failure re-rendered: {wire.calls}"
    assert "could not be downloaded" in str(caught.value)


def test_avatar_an_ambiguous_render_costs_exactly_one_post(wire, avatar_env):
    from app.providers.avatar import AvatarError, render_avatar

    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(AvatarError) as caught:
        render_avatar(avatar_env["image"], avatar_env["audio"],
                      avatar_env["owner"])

    assert len(wire.posts()) == 1, f"a retry bought a second render: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_avatar_a_4xx_render_is_a_refusal(wire, avatar_env):
    from app.providers.avatar import AvatarError, render_avatar

    wire.always(_response(503, content=b"renderer exploded"))

    with pytest.raises(AvatarError) as caught:
        render_avatar(avatar_env["image"], avatar_env["audio"],
                      avatar_env["owner"])

    assert len(wire.posts()) == 1, wire.calls
    assert "503" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_avatar_a_2xx_with_no_artifact_is_an_ambiguity(wire, avatar_env):
    from app.providers.avatar import AvatarError, render_avatar

    wire.always(_response(200, json_body={}))

    with pytest.raises(AvatarError) as caught:
        render_avatar(avatar_env["image"], avatar_env["audio"],
                      avatar_env["owner"])

    assert len(wire.posts()) == 1, wire.calls
    assert "no video_url" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


# ---------------------------------------------------------------------------
# broll.ai_server.generate
# ---------------------------------------------------------------------------


@pytest.fixture()
def broll_env(tmp_path, monkeypatch, scoped):
    import app.providers.broll as broll_mod

    monkeypatch.setattr(broll_mod, "broll_ai_base_url", lambda: "https://broll.test")
    monkeypatch.setattr(broll_mod, "broll_ai_backend", lambda: "server")
    monkeypatch.setattr(broll_mod, "_probe_duration", lambda path: 4.0)
    monkeypatch.setattr(broll_mod, "STORAGE_ROOT", tmp_path / "storage")
    monkeypatch.chdir(tmp_path)
    return {"owner": scoped, "root": tmp_path}


def test_broll_posts_one_json_generate_with_the_render_timeout(wire, broll_env):
    from app.providers.broll import generate_clip

    wire.always(_video_response())
    path = generate_clip("a red balloon", broll_env["owner"], seconds=4.0,
                         aspect="9:16")

    assert path.endswith(".mp4")
    assert len(wire.posts()) == 1, f"a retry bought a second clip: {wire.calls}"
    kw = wire.kwargs_of("POST", "https://broll.test/generate")
    assert kw["json"] == {"prompt": "a red balloon", "seconds": 4.0,
                          "aspect": "9:16"}
    assert kw["timeout"] == 1800


def test_broll_adds_an_image_ref_only_when_it_resolves(wire, broll_env):
    """An image that is not in the workspace is NOT forwarded as a path."""
    from app.providers.broll import generate_clip

    wire.always(_video_response())
    generate_clip("a red balloon", broll_env["owner"], image_ref="missing.jpg")
    assert "image_url" not in wire.kwargs_of("POST", wire.posts()[0])["json"]


def test_broll_stores_the_clip_under_a_name_carrying_the_submission(
        wire, broll_env):
    """``ai-<sha256(prompt)[:10]>`` used to collide and destroy the first file."""
    from app.providers.broll import generate_clip

    wire.always(_video_response())
    first = generate_clip("a red balloon", broll_env["owner"])
    second = generate_clip("a red balloon", broll_env["owner"])

    assert first != second, "the second billed render overwrote the first"
    stored = Path(broll_env["root"]) / "storage" / broll_env["owner"] / "broll"
    assert len(list(stored.glob("ai-*.mp4"))) == 2, list(stored.iterdir())


def test_broll_a_failed_artifact_fetch_does_not_re_render(wire, broll_env):
    from app.providers.broll import BrollError, generate_clip

    wire.handler = lambda method, url, **kw: (
        _response(200, json_body={"video_url": "https://cdn.test/c.mp4"})
        if method == "POST" else _broken(503, "gone"))

    with pytest.raises(BrollError) as caught:
        generate_clip("a red balloon", broll_env["owner"])

    assert len(wire.posts()) == 1, f"the download failure re-rendered: {wire.calls}"
    assert "could not be downloaded" in str(caught.value)


def test_broll_an_ambiguous_render_costs_exactly_one_post(wire, broll_env):
    from app.providers.broll import BrollError, generate_clip

    wire.always(httpx.ReadTimeout("lost"))

    with pytest.raises(BrollError) as caught:
        generate_clip("a red balloon", broll_env["owner"])

    assert len(wire.posts()) == 1, f"a retry bought a second clip: {wire.calls}"
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_broll_a_2xx_with_no_artifact_is_an_ambiguity(wire, broll_env):
    from app.providers.broll import BrollError, generate_clip

    wire.always(_response(200, json_body={}))

    with pytest.raises(BrollError) as caught:
        generate_clip("a red balloon", broll_env["owner"])

    assert len(wire.posts()) == 1, wire.calls
    assert "no video_url" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" in str(caught.value)


def test_broll_a_4xx_render_is_a_refusal(wire, broll_env):
    from app.providers.broll import BrollError, generate_clip

    wire.always(_response(422, content=b"prompt too long"))

    with pytest.raises(BrollError) as caught:
        generate_clip("a red balloon", broll_env["owner"])

    assert len(wire.posts()) == 1, wire.calls
    assert "422" in str(caught.value)
    assert "SUBMISSION_UNKNOWN" not in str(caught.value)


# ===========================================================================
# GROUP B -- §1/§6: the accounting that the migration deliberately changed
# ===========================================================================


def test_an_ownerless_image_job_is_refused_before_the_post(wire):
    """§1: no tenant means no cap, and an uncapped billable call is not run.

    ``images`` resolves its tenant from the ambient scope, so "no scope" is a
    real and common state (a CLI, a test, a background thread). It used to POST
    anyway and book the cost against ``workspace_id=""``.

    The refusal propagates AS ITSELF rather than being flattened into a provider
    error: it is a :class:`~app.services.cost.BudgetExceededError`, so every
    caller that already catches a budget refusal keeps treating it correctly
    (no request went out, nothing billed) without knowing about this work.
    """
    from app.providers.images import XkiroImageProvider
    from app.services.paid_provider import OwnerlessSpendRefused

    prov = XkiroImageProvider(base_url="https://xkiro.test/v1",
                              api_key=_PROBE_VALUE)
    polls: list[str] = []
    wire.handler = _xkiro_script(polls=polls)

    with pytest.raises(OwnerlessSpendRefused) as caught:
        prov.generate("a red balloon")

    assert "xkiro.image_job" in str(caught.value), str(caught.value)
    assert wire.posts() == [], (
        f"a refused ownerless operation still sent {wire.posts()}")


def test_an_ownerless_narration_is_refused_before_the_post(elevenlabs, wire):
    """ElevenLabs bills per character; an unowned narration is unbudgeted spend."""
    from app.services.paid_provider import OwnerlessSpendRefused

    wire.always(_response(200, content=b"mp3" * 400))

    with pytest.raises(OwnerlessSpendRefused) as caught:
        elevenlabs.synthesize("hello there")

    assert "tts.text_to_speech" in str(caught.value), str(caught.value)
    assert wire.posts() == [], (
        f"a refused ownerless operation still sent {wire.posts()}")


def test_an_ownerless_render_is_refused_before_the_post(wire, tmp_path,
                                                        monkeypatch):
    """avatar/broll are driven from a workspace asset, so an empty tenant is
    refused at the boundary -- long before any billable call is considered."""
    import app.providers.avatar as avatar_mod
    from app.providers.avatar import AvatarError, render_avatar

    storage = tmp_path / "storage"
    image = storage / "presenter.jpg"
    audio = storage / "voice.mp3"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"jpeg")
    audio.write_bytes(b"mp3")
    monkeypatch.setattr(avatar_mod, "avatar_base_url", lambda: "https://avatar.test")
    monkeypatch.setattr(avatar_mod, "ffmpeg_present", lambda: True)
    monkeypatch.setattr(avatar_mod, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(avatar_mod, "STORAGE_ROOT", storage)
    import app.services.storage as storage_mod

    monkeypatch.setattr(storage_mod, "STORAGE_ROOT", storage)
    wire.always(_video_response())

    with pytest.raises(AvatarError):
        render_avatar(str(image), str(audio), "")

    assert wire.posts() == [], (
        f"a refused ownerless operation still sent {wire.posts()}")


def test_an_ownerless_broll_render_is_refused_before_the_post(wire, tmp_path,
                                                              monkeypatch):
    """B-roll writes under ``workspace_id``, so an empty one has no ledger owner."""
    import app.providers.broll as broll_mod
    from app.providers.broll import generate_clip
    from app.services.paid_provider import OwnerlessSpendRefused

    monkeypatch.setattr(broll_mod, "broll_ai_base_url", lambda: "https://broll.test")
    monkeypatch.setattr(broll_mod, "broll_ai_backend", lambda: "server")
    monkeypatch.setattr(broll_mod, "_probe_duration", lambda path: 4.0)
    monkeypatch.setattr(broll_mod, "STORAGE_ROOT", tmp_path / "storage")
    monkeypatch.chdir(tmp_path)
    wire.always(_video_response())

    with pytest.raises(OwnerlessSpendRefused) as caught:
        generate_clip("a red balloon", "")

    assert "broll.server_generate" in str(caught.value), str(caught.value)
    assert wire.posts() == [], (
        f"a refused ownerless operation still sent {wire.posts()}")


def test_a_refused_ownerless_operation_never_reaches_the_ledger():
    """A refusal writes NOTHING. A rolled-back phantom would shrink the cap."""
    assert rows_without_owner() == [], (
        "an ownerless ledger row exists: every one of those is an "
        "unattributable charge")


def test_a_priced_narration_books_exactly_one_owned_row(elevenlabs, wire, scoped):
    """One operation, one reservation, settled in place -- never two rows."""
    wire.always(_response(200, content=b"mp3" * 400))

    elevenlabs.synthesize("hello there")

    rows = ledger(scoped)
    assert len(rows) == 1, f"one narration booked {len(rows)} rows: {rows}"
    assert rows[0]["category"] == "tts"
    assert rows[0]["provider"] == "elevenlabs"
    assert rows[0]["amount_usd"] == pytest.approx(
        len("hello there") * elevenlabs.EST_USD_PER_CHAR, abs=1e-9)
    assert rows[0]["detail"]["operation_id"], rows[0]
    # The row is the ESTIMATE that was reserved, not an invented "unknown":
    # ElevenLabs charges per character, so the amount is knowable in advance.
    assert rows[0]["detail"]["cost_outcome"] == str(CostOutcome.ESTIMATED), rows[0]
    assert rows[0]["detail"]["exposure_unknown"] is False, rows[0]


def test_an_accepted_image_job_leaves_one_unknown_exposure_row(openai_images,
                                                               wire, scoped):
    """The image lane's accepted-but-unpriced render: a ROW, not silence."""
    import base64 as _b64

    wire.always(_response(200, json_body={
        "data": [{"b64_json": _b64.b64encode(b"\x89PNG" + b"x" * 64).decode()}]}))

    openai_images.generate("a red balloon")

    rows = ledger(scoped)
    assert len(rows) == 1, f"one accepted generation booked {len(rows)}: {rows}"
    assert rows[0]["category"] == "image"
    assert rows[0]["provider"] == "openai_compatible"
    assert rows[0]["detail"]["cost_outcome"] == str(CostOutcome.UNKNOWN_EXPOSURE)
    assert rows[0]["detail"]["exposure_unknown"] is True


def test_a_connect_failure_releases_the_reservation_because_nothing_was_sent(
        elevenlabs, wire, scoped):
    """The third answer, and the one that runs in the OPPOSITE direction.

    ``ConnectError`` arrives as an ambiguity -- the classifier cannot tell what
    happened -- but it PROVES no socket opened, so nothing was billed. Keeping
    the reservation would put a phantom charge on the books for a request that
    never reached the vendor.
    """
    from app.services.paid_executor import CostOutcome as _CO

    wire.always(httpx.ConnectError("refused"))

    from app.providers.tts import TTSError

    with pytest.raises(TTSError) as caught:
        elevenlabs.synthesize("hello there")

    assert "SUBMISSION_UNKNOWN" in str(caught.value), (
        "the classification must not change: an unproven delivery is still an "
        "ambiguity to the caller")
    rows = ledger(scoped)
    assert rows == [], (
        f"a connect failure booked a phantom charge: {rows}")
    assert _CO.UNKNOWN_EXPOSURE is not _CO.ESTIMATED


def test_a_refused_submit_releases_the_reservation(elevenlabs, wire, scoped):
    """A 4xx created nothing, so the money goes back to the workspace."""
    wire.always(_response(401, content=b"bad key"))

    from app.providers.tts import TTSError

    with pytest.raises(TTSError):
        elevenlabs.synthesize("hello there")

    assert ledger(scoped) == [], (
        "a definitively refused submit kept its reservation")


def test_an_unpriced_render_leaves_one_unknown_exposure_row(wire, avatar_env):
    """Accepted, billed, and unpriceable: a ROW marked unknown, not silence.

    Before §6 this left no ledger row at all (``track_cost`` drops ``<= 0``) and
    only an activity-feed line. The reservation is the ledger row now, so the
    exposure is queryable with a WHERE clause.
    """
    from app.providers.avatar import render_avatar

    wire.always(_video_response())
    render_avatar(avatar_env["image"], avatar_env["audio"], avatar_env["owner"])

    rows = ledger(avatar_env["owner"])
    assert len(rows) == 1, f"one render booked {len(rows)} rows: {rows}"
    assert rows[0]["category"] == "video"
    assert rows[0]["detail"]["cost_outcome"] == str(CostOutcome.UNKNOWN_EXPOSURE)
    assert rows[0]["detail"]["exposure_unknown"] is True
    assert rows[0]["detail"]["spend_authority"] == "WORKSPACE_OWNED"
    assert rows[0]["detail"]["charged_workspace_id"] == avatar_env["owner"]


def test_an_ambiguous_submit_keeps_one_reservation_and_marks_it_unknown(
        xkiro, wire, scoped):
    """The reservation is the proof the money may be gone. Deleting it hides it."""
    from app.providers.images import ImageProviderError

    wire.always(httpx.ReadTimeout("lost"))
    with pytest.raises(ImageProviderError):
        xkiro.generate("a red balloon")

    rows = ledger(scoped)
    assert len(rows) == 1, f"an ambiguous submit booked {len(rows)} rows: {rows}"
    assert rows[0]["detail"]["cost_outcome"] == str(CostOutcome.UNKNOWN_EXPOSURE)
    assert rows[0]["detail"]["exposure_unknown"] is True


def test_reattaching_adopts_the_right_row_when_the_provider_has_many():
    """Two jobs from one provider, two money rows: the adoption must be exact.

    Matching on the provider alone would hand job A's recovery the row that paid
    for job B -- which is worse than finding nothing, because the caller would
    then settle the wrong operation.
    """
    from app.services.paid_provider import PaidOperation, reattach_by_remote_id

    for name, amount in (("first", 0.1), ("second", 0.2)):
        op = PaidOperation(provider="xkiro", operation="xkiro.image_job",
                           workspace_id="ws-many", category="image",
                           estimated_cost=amount)
        op.authorize()
        op.mark_accepted(f"remote-{name}")

    # Ask for the OLDER job on purpose: any implementation that settles for
    # "a row from this provider" will hand back the newest one, which is the
    # wrong job's money.
    adopted = reattach_by_remote_id("remote-first", provider="xkiro")
    assert adopted is not None
    assert adopted.entry_id != "", "the adopted row has no identity"
    rows = {r["entry_id"]: r for r in ledger("ws-many")}
    assert rows[adopted.entry_id]["amount_usd"] == pytest.approx(0.1), (
        f"reattach adopted the wrong reservation: {rows}")


# ===========================================================================
# §5 -- reattach / recovery accounting: exactly-once ACCOUNTING
# ===========================================================================


def test_the_remote_id_is_persisted_on_the_reservation_row(elevenlabs, wire,
                                                           scoped):
    """Without the remote id on the money row, a restart cannot find its work.

    The provider's own row is not a recovery handle -- it lives in the caller's
    table and dies with the request that wrote it. The ledger row survives.
    """
    wire.always(_response(200, content=b"mp3" * 400,
                          headers={"request-id": "req-777"}))

    elevenlabs.synthesize("hello there")

    rows = ledger(scoped)
    assert rows[0]["detail"].get("remote_id") == "req-777", rows[0]["detail"]


def test_reattaching_to_a_remote_job_does_not_reserve_again():
    """A restart that finds the same remote job must not spend a second dollar.

    The invariant is exactly-once ACCOUNTING, not exactly-once network: the
    remote job is already paid for, so the recovery path must adopt the existing
    reservation rather than open a new one.
    """
    from app.services.paid_provider import (
        PaidOperation,
        reattach_by_remote_id,
    )

    first = PaidOperation(provider="p", operation="o", workspace_id="ws-reattach",
                          category="tts", estimated_cost=0.5)
    first.authorize()
    first.mark_accepted("remote-42")

    again = reattach_by_remote_id("remote-42", provider="p", operation="o")

    assert again is not None, "the remote id found no accounting row to adopt"
    assert again.entry_id == first.entry_id, (
        "reattach invented a second accounting identity for one remote job")
    # Authorizing a reattached operation must be a no-op, not a second reserve.
    before = len(ledger("ws-reattach"))
    again.authorize()
    again.close_book()
    assert len(ledger("ws-reattach")) == before, (
        "reattach reserved again")


def test_reattaching_does_not_double_settle():
    """One remote job settles ONCE -- and the SECOND amount must not win.

    A second settlement is an UPDATE on the same row, not an insert, so counting
    rows cannot see it. The observable is the AMOUNT: a recovery path that
    settles "just to be sure", with its own idea of the price, silently
    overwrites the number the original settlement recorded.
    """
    from app.services.paid_provider import PaidOperation, reattach_by_remote_id

    op = PaidOperation(provider="p", operation="o", workspace_id="ws-settle",
                       category="tts", estimated_cost=0.5)
    op.authorize()
    op.mark_accepted("remote-99")
    op.mark_succeeded(0.4)
    assert ledger("ws-settle")[0]["amount_usd"] == pytest.approx(0.4)

    reattached = reattach_by_remote_id("remote-99", provider="p", operation="o")
    assert reattached is not None, "the adopted operation must know it is closed"
    assert reattached.settled is True, (
        "a reattach adopted a row whose book was already closed and does not "
        "know it")
    reattached.mark_succeeded(0.9)
    reattached.close_book()

    rows = ledger("ws-settle")
    assert len(rows) == 1, f"a reattach settled twice: {rows}"
    assert rows[0]["amount_usd"] == pytest.approx(0.4), (
        f"a second settlement overwrote the first: {rows[0]}")


def test_one_remote_id_yields_one_accounting_identity_across_restarts():
    """Two independent processes that both learn the remote id agree on the row."""
    from app.services.paid_provider import PaidOperation, reattach_by_remote_id

    op = PaidOperation(provider="xkiro", operation="xkiro.image_job",
                       workspace_id="ws-identity", category="image",
                       estimated_cost=0.0)
    op.authorize()
    op.mark_accepted("remote-identity-1")

    first = reattach_by_remote_id("remote-identity-1", provider="xkiro")
    second = reattach_by_remote_id("remote-identity-1", provider="xkiro")
    assert first is not None and second is not None
    assert first.entry_id == second.entry_id == op.entry_id

    rows = ledger("ws-identity")
    assert len(rows) == 1, f"one remote job produced {len(rows)} rows: {rows}"


def test_reattaching_an_unknown_remote_id_reserves_nothing():
    """Nothing to adopt means nothing is adopted -- and nothing is charged."""
    from app.services.paid_provider import reattach_by_remote_id

    assert reattach_by_remote_id("never-submitted", provider="p") is None


def test_a_reattached_operation_still_reports_its_owner():
    """The adopted row carries the ownership picture it was written with."""
    from app.services.paid_provider import PaidOperation, reattach_by_remote_id

    op = PaidOperation(provider="p", operation="o", workspace_id="ws-owner",
                       category="tts", estimated_cost=0.25)
    op.authorize()
    op.mark_accepted("remote-owner-1")

    adopted = reattach_by_remote_id("remote-owner-1", provider="p")
    assert adopted is not None
    assert adopted.ownership is not None
    assert adopted.ownership.charged_workspace_id == "ws-owner"
    assert adopted.ownership.authority == "WORKSPACE_OWNED"
    assert adopted.operation_id == op.operation_id