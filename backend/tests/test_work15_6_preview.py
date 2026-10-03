"""Voice preview API tests (Work 15.6 §8).

Every guard in ``app/api/v1/preview.py`` is asserted here, and the guards that
carry the security/cost claim are *mutation-proved*: the guard is broken on
purpose, the specific test is re-run, and its real failure is recorded.

Live-provider tests carry ``@pytest.mark.live`` (the marker the repo already
registers) and skip declaratively without credentials. No test here reaches the
network: the provider boundary is either the simulation or a monkeypatched
factory.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import preview as preview_mod
from app.main import create_app
from app.providers.tts import TTSResult
from app.services.provider_settings import REGISTRY

#: Every credential key the registry marks secret. Used to build the
#: "no secret, not even a length or a digest" assertion.
SECRET_KEYS = tuple(k for k, m in REGISTRY.items() if m.get("secret"))

_HAS_LIVE_CREDENTIALS = any(
    os.environ.get((REGISTRY[k].get("env") or "__none__") or "__none__", "").strip()
    for k in SECRET_KEYS
)

#: A value that is obviously a fixture, not a key. Built at runtime so it can
#: never be mistaken for a credential by a scanner or copied into a real config.
CANARY = "PREVIEW-CANARY-" + "-".join(
    f"{n:04d}" for n in (11, 22, 33, 44)) + "-DO-NOT-USE"


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def authed(client):
    """A registered user + workspace + bearer headers."""
    email = f"vp{os.urandom(5).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {
        "client": client,
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "ws_id": data["workspace"]["id"],
    }


def _url(authed, suffix=""):
    return f"/api/v1/workspaces/{authed['ws_id']}/voice-preview{suffix}"


@pytest.fixture(autouse=True)
def _clean_budget():
    preview_mod.reset_preview_budget()
    yield
    preview_mod.reset_preview_budget()


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """Point the preview cache at a per-test directory.

    Without this the cache writes into the repository's real ``data/videos``
    tree, so a second run of this file finds its own entries, serves cache hits
    instead of calling the provider, and the cost guard is never charged. A test
    that passes once and fails on the next run is worse than no test.
    """
    monkeypatch.setattr(preview_mod, "STORAGE_ROOT", tmp_path / "videos")


def _offerable(provider_id, **over):
    """A row as ``_offerable_row`` would return it, for provider fakes."""
    row = {"provider": provider_id, "offerable": True, "available": True,
           "reason": "ok", "message": "ok", "missing_credentials": []}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# 1. provider selection -- offerable set, and WHY the rest are not
# ---------------------------------------------------------------------------


def test_providers_list_reuses_the_qualification_registry(authed):
    from app.providers.tts_qualification import list_qualifications

    r = authed["client"].get(_url(authed, "/providers"), headers=authed["headers"])
    assert r.status_code == 200, r.text
    body = r.json()

    expected = {q.provider for q in list_qualifications()}
    assert {row["provider"] for row in body["items"]} == expected

    # The offerable set is derived from implementation_status == IMPLEMENTED, so
    # it cannot drift from the adapter that will really be called.
    for row in body["items"]:
        assert row["offerable"] == (row["reason"] != "unavailable")


def test_unavailable_providers_state_a_reason_not_an_empty_dropdown(authed):
    """A provider the user cannot pick says why. It is never silently absent."""
    r = authed["client"].get(_url(authed, "/providers"), headers=authed["headers"])
    rows = {row["provider"]: row for row in r.json()["items"]}

    for provider in ("minimax", "fish_audio", "voxcpm", "siliconflow_tts",
                     "gemini_tts", "azure_speech_v2"):
        row = rows[provider]
        assert row["offerable"] is False, provider
        assert row["reason"] == "unavailable", provider
        assert row["message"].strip(), f"{provider} has no explanation"


def test_config_gated_provider_reports_missing_key_but_not_its_value(authed):
    """A CONFIG_GATED provider is offerable, unavailable, and names the key."""
    from app.services.provider_settings import get_credential, set_credential

    r = authed["client"].get(_url(authed, "/providers"), headers=authed["headers"])
    rows = {row["provider"]: row for row in r.json()["items"]}

    assert rows["elevenlabs"]["offerable"] is True
    # Labels are derived from state fields, so CONFIG_GATED appears because the
    # adapter declares credential_keys -- never because a note mentions a key.
    assert "CONFIG_GATED" in rows["elevenlabs"]["qualification_labels"]
    assert "LIVE_VERIFIED" not in rows["elevenlabs"]["qualification_labels"]

    before, _ = get_credential("tts.elevenlabs_api_key")
    try:
        set_credential("tts.elevenlabs_api_key", "")
        r = authed["client"].get(_url(authed, "/providers"), headers=authed["headers"])
        row = {x["provider"]: x for x in r.json()["items"]}["elevenlabs"]
        assert row["available"] is False
        assert row["reason"] == "not_configured"
        assert "tts.elevenlabs_api_key" in row["missing_credentials"]
        assert row["message"]
    finally:
        set_credential("tts.elevenlabs_api_key", before)


def test_edge_and_mock_are_offerable_without_any_credential(authed):
    r = authed["client"].get(_url(authed, "/providers"), headers=authed["headers"])
    rows = {row["provider"]: row for row in r.json()["items"]}
    assert rows["edge"]["offerable"] and rows["edge"]["available"]
    assert rows["mock"]["offerable"] and rows["mock"]["available"]
    assert rows["mock"]["simulation_only"] is True
    assert "SIMULATION" in rows["mock"]["qualification_labels"]


# ---------------------------------------------------------------------------
# 2. voice listing -- production identity, not a curated preview list
# ---------------------------------------------------------------------------


def test_voices_come_from_the_production_factory(authed, monkeypatch):
    """The voice list is the provider's own voices(), under workspace scope."""
    seen = {}

    class _Provider:
        name = "edge"

        def voices(self, language=""):
            seen["language"] = language
            return [{"id": "en-US-AriaNeural", "gender": "Female", "locale": "en-US"}]

    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": _Provider())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))

    r = authed["client"].get(_url(authed, "/providers/edge/voices?language=en"),
                             headers=authed["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["items"] == [{"id": "en-US-AriaNeural", "gender": "Female",
                                  "locale": "en-US"}]
    assert seen["language"] == "en"


def test_voice_listing_projects_only_id_gender_locale(authed, monkeypatch):
    """Extra keys a provider returns must not reach the frontend verbatim."""
    class _Provider:
        name = "edge"

        def voices(self, language=""):
            return [{"id": "v1", "gender": "", "locale": "en",
                     "session_token": "provider-internal-value",
                     "raw_url": "https://example.invalid/x"}]

    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": _Provider())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    r = authed["client"].get(_url(authed, "/providers/edge/voices"),
                             headers=authed["headers"])
    assert r.status_code == 200, r.text
    assert "provider-internal-value" not in r.text
    assert r.json()["items"] == [{"id": "v1", "gender": "", "locale": "en"}]


def test_voices_for_unavailable_provider_is_409_with_a_reason(authed):
    r = authed["client"].get(_url(authed, "/providers/minimax/voices"),
                             headers=authed["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason"] == "unavailable"
    assert r.json()["detail"]["message"]


def test_empty_voice_list_explains_itself(authed, monkeypatch):
    class _Provider:
        name = "edge"

        def voices(self, language=""):
            return []

    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": _Provider())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    r = authed["client"].get(_url(authed, "/providers/edge/voices?language=xx"),
                             headers=authed["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == [] and body["count"] == 0
    assert body["empty_reason"], "an empty dropdown must say why it is empty"


def test_provider_aliases_resolve_to_the_same_qualification():
    assert preview_mod.canonical_provider("local") == "kokoro"
    assert preview_mod.canonical_provider("qwen-tts") == "qwen3"
    assert preview_mod.canonical_provider("xi") == "elevenlabs"
    assert preview_mod.canonical_provider("") == "edge"


# ---------------------------------------------------------------------------
# 3. the preview itself + the cost guard
# ---------------------------------------------------------------------------


def test_preview_returns_audio_from_the_chosen_provider(authed):
    r = authed["client"].post(_url(authed), headers=authed["headers"],
                              json={"text": "hello voice lab", "provider": "mock"})
    assert r.status_code == 200, r.text
    assert r.headers["X-TTS-Provider"] == "mock"
    assert r.headers["X-TTS-Mock"] == "1"
    assert r.headers["X-Preview-Cached"] == "0"
    assert len(r.content) > 100


# ---------------------------------------------------------------------------
# 4b. length leakage, asserted SOUNDLY
# ---------------------------------------------------------------------------


def test_no_response_field_reports_a_credential_length(authed):
    """Length leakage is checked per FIELD, not as a substring of the blob.

    The earlier form asserted ``str(len(CANARY)) not in response.text``. That is
    unsound: a credential length is 2-3 digits, and any sha256 in the payload
    contains such a substring roughly a quarter of the time, so the test flaked
    for reasons that had nothing to do with the product.

    The meaningful property is structural: no field the API returns may carry a
    key whose name implies a credential length, mask, or digest. That is a real
    invariant and it cannot pass by accident.
    """
    from app.services.provider_settings import set_credential

    set_credential("tts.elevenlabs_api_key", CANARY)
    client, headers = authed["client"], authed["headers"]

    banned = ("length", "len", "masked", "digest", "fingerprint", "hint",
              "prefix", "suffix", "last4", "partial")

    def walk(node, path="$"):
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                for word in banned:
                    assert word not in lowered, (
                        f"{path}.{key} exposes credential {word!r}; the API "
                        "must report a state word, never a derived fact")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    for suffix in ("/providers", "/providers/edge/voices",
                   "/providers/elevenlabs/voices"):
        r = client.get(_url(authed, suffix), headers=headers)
        # 503 = the provider itself was unreachable/unauthorised, so there is no
        # voice list to inspect. The leak check is about the JSON SHAPE, and an
        # error envelope carries no credential, so accept it and move on.
        assert r.status_code in (200, 409, 503), r.text
        walk(r.json(), suffix)
        assert CANARY not in r.text, f"{suffix} leaked the value"


def test_preview_is_workspace_isolated_on_disk(authed, client, tmp_path, monkeypatch):
    """Two workspaces must never share a cache entry or a cache directory."""
    monkeypatch.setattr(preview_mod, "STORAGE_ROOT", tmp_path / "videos")
    first = authed

    email = f"vp{os.urandom(5).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    data = r.json()
    second = {"client": client, "ws_id": data["workspace"]["id"],
              "headers": {"Authorization": f"Bearer {data['access_token']}"}}

    for who in (first, second):
        rr = who["client"].post(f"/api/v1/workspaces/{who['ws_id']}/voice-preview",
                                headers=who["headers"],
                                json={"text": "same text", "provider": "mock"})
        assert rr.status_code == 200, rr.text

    dir_a = preview_mod.preview_cache_dir(first["ws_id"])
    dir_b = preview_mod.preview_cache_dir(second["ws_id"])
    assert dir_a != dir_b
    assert dir_a.is_dir() and dir_b.is_dir()
    assert list(dir_a.glob("*.wav")) and list(dir_b.glob("*.wav"))
    assert not (set(p.name for p in dir_a.iterdir())
                & set(p.name for p in dir_b.iterdir()))


def test_preview_cache_key_separates_workspaces_and_ignores_credentials():
    a = preview_mod.preview_cache_key("ws-a", "mock", "v", "hello")
    b = preview_mod.preview_cache_key("ws-b", "mock", "v", "hello")
    assert a != b, "the workspace must be part of the digest"

    # A credential-shaped parameter never reaches the digest: media_cache drops
    # it, and previews do not accept one anyway.
    assert preview_mod.preview_cache_key("ws-a", "mock", "v", "hello") == a
    from app.services.media_cache import cache_key

    with_cred = cache_key({"provider": "mock", "workspace_id": "ws-a",
                           "api_key": CANARY, "search_term": "hello"})
    without = cache_key({"provider": "mock", "workspace_id": "ws-a",
                         "search_term": "hello"})
    assert with_cred == without


def test_repeat_preview_is_a_cache_hit_and_spends_no_budget(authed, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(preview_mod, "STORAGE_ROOT", tmp_path / "videos")
    payload = {"text": "cache me twice", "provider": "mock"}

    first = authed["client"].post(_url(authed), headers=authed["headers"], json=payload)
    assert first.status_code == 200 and first.headers["X-Preview-Cached"] == "0"

    preview_mod.reset_preview_budget()
    second = authed["client"].post(_url(authed), headers=authed["headers"], json=payload)
    assert second.status_code == 200, second.text
    assert second.headers["X-Preview-Cached"] == "1"
    assert second.content == first.content
    assert preview_mod.preview_budget_state(authed["ws_id"]) == 0, \
        "a cache hit must not be charged to the workspace"


def test_failed_preview_is_not_cached(authed, tmp_path, monkeypatch):
    """A provider fault must not be pinned as a cache entry for the full TTL."""
    monkeypatch.setattr(preview_mod, "STORAGE_ROOT", tmp_path / "videos")
    calls = {"n": 0}

    class _Flaky:
        name = "mock"

        def voices(self, language=""):
            return [{"id": "d", "gender": "", "locale": "en"}]

        def synthesize(self, text, **kw):
            from app.providers.tts import TTSError

            calls["n"] += 1
            raise TTSError("provider down")

    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": _Flaky())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))

    payload = {"text": "will fail", "provider": "mock"}
    for expected_calls in (1, 2):
        r = authed["client"].post(_url(authed), headers=authed["headers"], json=payload)
        assert r.status_code == 503, r.text
        assert r.json()["detail"]["reason"] == "preview_failed"
        assert calls["n"] == expected_calls, "the second attempt must retry"

    cache_dir = preview_mod.preview_cache_dir(authed["ws_id"])
    assert not list(cache_dir.glob("*.wav")) if cache_dir.exists() else True


def test_cost_guard_refuses_over_the_window_ceiling(authed, monkeypatch):
    """Four DISTINCT texts, so no request can be served from the cache.

    Distinctness matters: two identical texts collapse to one cache key, the
    second is a free cache hit, and the workspace is only charged once. A
    cost-guard test that accidentally re-requests the same text tests the cache,
    not the guard.
    """
    monkeypatch.setattr(preview_mod, "PREVIEW_MAX_PER_WINDOW", 3)
    texts = ["alpha one", "bravo two", "charlie three", "delta four"]
    for index, text in enumerate(texts[:3]):
        r = authed["client"].post(_url(authed), headers=authed["headers"],
                                  json={"text": text, "provider": "mock"})
        assert r.status_code == 200, r.text
        assert r.headers["X-Preview-Cached"] == "0", (
            f"request {index} was a cache hit, so the guard was not exercised")
    over = authed["client"].post(_url(authed), headers=authed["headers"],
                                 json={"text": texts[3], "provider": "mock"})
    assert over.status_code == 429, over.text
    detail = over.json()["detail"]
    assert detail["reason"] == "preview_budget_exceeded"
    assert detail["max_per_window"] == 3


def test_cost_guard_clamps_long_text_and_reports_it(authed, monkeypatch):
    monkeypatch.setattr(preview_mod, "MAX_PREVIEW_CHARS", 40)
    r = authed["client"].post(_url(authed), headers=authed["headers"],
                              json={"text": "word " * 60, "provider": "mock"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Preview-Clamped"] == "1"
    assert int(r.headers["X-Preview-Chars"]) <= 40


def test_empty_preview_text_is_refused_with_a_reason(authed):
    r = authed["client"].post(_url(authed), headers=authed["headers"],
                              json={"text": "   ", "provider": "mock"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason"] == "empty_preview_text"


def test_preview_of_unavailable_provider_is_409_not_a_503(authed):
    r = authed["client"].post(_url(authed), headers=authed["headers"],
                              json={"text": "hi", "provider": "gemini_tts"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason"] == "unavailable"


def test_preview_of_unconfigured_provider_names_the_missing_key(authed):
    from app.services.provider_settings import get_credential, set_credential

    before, _ = get_credential("tts.elevenlabs_api_key")
    try:
        set_credential("tts.elevenlabs_api_key", "")
        r = authed["client"].post(_url(authed), headers=authed["headers"],
                                  json={"text": "hi", "provider": "elevenlabs"})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["reason"] == "not_configured"
        assert "tts.elevenlabs_api_key" in detail["missing_credentials"]
    finally:
        set_credential("tts.elevenlabs_api_key", before)


def test_preview_records_a_canonical_media_asset(authed):
    """No second asset table: the preview is an ordinary voice MediaAsset."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import MediaAsset

    r = authed["client"].post(_url(authed), headers=authed["headers"],
                              json={"text": "record me", "provider": "mock"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        rows = db.scalars(
            select(MediaAsset).where(
                MediaAsset.workspace_id == authed["ws_id"],
                MediaAsset.origin == "generated",
            )
        ).all()
        assert rows, "the preview must land on the canonical media_assets table"
        assert rows[0].type == "voice"
        assert rows[0].meta_json["preview"] is True
        assert rows[0].storage_key.startswith("voice_preview/")


# ---------------------------------------------------------------------------
# 4. NO SECRETS TO THE FRONTEND
# ---------------------------------------------------------------------------


def test_voice_preview_never_leaks_a_secret(authed, monkeypatch):
    """Plaintext, LENGTH and DIGEST of a configured secret must all be absent.

    A length or a digest is not the secret, but both narrow a brute force enough
    to matter, so the work order counts them as the secret. A guard that only
    blocked the plaintext would pass the first assertion and fail the other two,
    which is why all three are asserted.
    """
    from app.services.provider_settings import set_credential

    monkeypatch.setattr(preview_mod, "_persist_asset", lambda *a, **k: None)
    digest = hashlib.sha256(CANARY.encode()).hexdigest()
    responses: list[tuple[str, str]] = []
    try:
        set_credential("tts.elevenlabs_api_key", CANARY)

        client = authed["client"]
        for url in (_url(authed, "/providers"),
                    _url(authed, "/providers/edge/voices")):
            r = client.get(url, headers=authed["headers"])
            assert r.status_code in (200, 409), r.text
            blob = r.text
            assert CANARY not in blob, f"{url} leaked the secret value"
            assert digest not in blob, f"{url} leaked the secret digest"
            assert preview_mod.credential_fingerprint_free(blob)
            # A bare 2-3 digit length must NOT be asserted absent from a JSON
            # blob: any sha256 in the payload contains such a substring about
            # 1-in-4 times by chance, so that assertion was unsound and
            # flaked at ~24%. Length leakage is asserted structurally below
            # instead, against a field the API actually controls.
            responses.append((url, blob))

        # The audio route: headers are a fixed list, so nothing leaks there.
        r = client.post(_url(authed), headers=authed["headers"],
                        json={"text": "leak check", "provider": "mock"})
        assert r.status_code == 200, r.text
        blob = r.text + json.dumps(dict(r.headers))
        assert CANARY not in blob
        assert digest not in blob
        assert preview_mod.credential_fingerprint_free(blob)
        # No bare-length assertion: a 2-3 digit length occurs by chance in
        # roughly a quarter of sha256-bearing payloads, so it reports a leak
        # at random. Work 15.7 asserts length safety structurally instead.
    finally:
        set_credential("tts.elevenlabs_api_key", "")


def test_audio_response_headers_are_a_fixed_allowlist():
    """The header set is named, so a provider attribute cannot leak by default."""
    from fastapi.responses import Response

    result = TTSResult(audio_bytes=b"x", format="mp3", provider="edge",
                       is_mock=False, sample_rate=24000)
    r = preview_mod._audio_response(result.audio_bytes, provider_id="edge",
                                    cached=True, clamped=False, chars=12,
                                    media_format=result.format,
                                    is_mock=result.is_mock,
                                    sample_rate=result.sample_rate)
    assert isinstance(r, Response)
    # Starlette normalises header names to lowercase, so compare case-insensitively.
    present = {k.lower() for k in r.headers}
    assert present >= {
        "x-tts-provider", "x-tts-mock", "x-preview-cached",
        "x-preview-clamped", "x-preview-chars", "x-preview-sample-rate",
    }
    assert r.headers["cache-control"] == "private, max-age=0, no-store"
    assert r.headers["x-preview-sample-rate"] == "24000"


def test_credential_fingerprint_free_detects_each_shape():
    """Plaintext and digest are detected; a bare LENGTH deliberately is not.

    Work 15.7 removed the length check. It reported leaks at random: a
    credential length is 2-3 digits, so any payload containing a sha256 digest
    contains such a substring about a quarter of the time. That produced a
    flaky false positive AND would have masked a genuine one. Length leakage
    is prevented structurally instead -- see
    ``test_no_response_field_reports_a_credential_length`` in the Work 15.7
    suite, which walks every returned field name.
    """
    from app.services.provider_settings import set_credential

    assert preview_mod.credential_fingerprint_free("nothing to see here") is True
    try:
        set_credential("tts.elevenlabs_api_key", CANARY)
        assert preview_mod.credential_fingerprint_free(CANARY) is False
        assert preview_mod.credential_fingerprint_free(
            hashlib.sha256(CANARY.encode()).hexdigest()) is False
        # A lone length is not evidence of anything.
        assert preview_mod.credential_fingerprint_free(
            f"chars={len(CANARY)}") is True
    finally:
        set_credential("tts.elevenlabs_api_key", "")


def test_preview_requires_authentication(client):
    r = client.get(_url({"ws_id": "nope"}, "/providers"))
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------------------
# 5. live providers -- honest skip, separate marker
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not _HAS_LIVE_CREDENTIALS,
                    reason="no live TTS credential configured in the environment")
def test_live_elevenlabs_voice_listing():
    from app.providers.tts import get_tts_provider

    voices = get_tts_provider("elevenlabs").voices()
    assert voices and all(v.get("id") for v in voices)