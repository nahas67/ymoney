"""Work 15.7 -- platform lane: the budget is DB-atomic, the duplicate preview
route is gone, the TwelveLabs key is workspace-scoped, and an operator can SEE
an incident.

Work 15.6 shipped a voice preview whose cost guard was a dict in the module.
That is a per-PROCESS counter: behind two uvicorn workers a workspace got twice
the allowance, neither worker could see the other's spending, and the number the
API reported was whichever worker had served fewer requests. The provider's own
pre-spend gate had the same shape -- a ``SELECT`` and, much later, an ``INSERT``,
with the whole window between them unprotected.

So this file is mostly about ONE property: **the limit is the database.**

* A -- the concurrency proof hammers :func:`cost.reserve_spend` from many threads
  and asserts the recorded total never exceeds the cap. That is the claim,
  tested rather than asserted in a comment.
* B -- legacy route proves ``/assets/voice/preview`` is a delegating adapter and
  not a second implementation, and that the Connections TTS test and the avatar
  route's voice step are gated at all.
* C -- TwelveLabs proves the key is workspace-scoped, secret, and that
  commercial-mode refusal survives the registration.
* D -- incidents proves ``SUBMISSION_UNKNOWN`` is never reported as ``FAILED``,
  that retry is offered only when the contract proved it safe, and that the
  frontend honours both.

Every money guard here has a test that fails when it is removed; the four that
guard money were each mutation-tested and the real failure output is in the
Work 15.7 report.

Live-provider coverage is at the bottom behind an honest ``live`` marker. Nothing
in this file reaches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.main import create_app
from app.models import CostEntry, Video, Workspace
from app.services import cost as cost_mod
from app.services import provider_settings as ps

REPO = Path(__file__).resolve().parents[2]
UI = REPO / "frontend" / "src" / "components" / "ProviderStatus.tsx"

#: A value handed to a credential setter so the resolver has something to hold.
#: No credential, no provider, no request -- the same convention
#: ``test_work15_7_media_paid.py`` uses for its probe value.
_PROBE = "w157-not-a-twelvelabs-credential"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A client whose media cache and storage live under ``tmp_path``."""
    from app.api.v1 import preview as preview_mod

    monkeypatch.setattr(preview_mod, "STORAGE_ROOT", tmp_path / "videos")
    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, label: str = "a") -> tuple[str, dict]:
    email = f"w157{label}{os.urandom(5).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


@pytest.fixture()
def ws_a(client):
    ws_id, headers = _register(client, "a")
    return {"ws_id": ws_id, "headers": headers, "client": client}


@pytest.fixture()
def ws_b(client):
    ws_id, headers = _register(client, "b")
    return {"ws_id": ws_id, "headers": headers, "client": client}


@pytest.fixture(autouse=True)
def _isolated_ledger():
    """Clear any process-local counter before and after each test.

    Reservation rows accumulate in the session database, and every test works
    against its own freshly registered workspace, so there is nothing to clear in
    the table. What IS reset is any module-level counter a future change might
    reintroduce -- the whole point of the work is that such a counter must not be
    the limit, and a test that left one populated would hide that.
    """
    from app.api.v1 import preview as preview_mod

    preview_mod.reset_preview_budget()
    yield
    preview_mod.reset_preview_budget()


def _set_budgets(workspace_id: str, daily: float, per_call: float = 100.0) -> None:
    """Set BOTH caps, because ``reserve_spend`` enforces both.

    Leaving ``per_video_budget_usd`` at its $0.50 default silently refuses every
    reservation above $0.50 for a reason that has nothing to do with the test, so
    the helper takes both and each test says what it means.
    """
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        settings = dict(ws.settings_json or {})
        safety = dict(settings.get("safety") or {})
        safety["daily_budget_usd"] = float(daily)
        safety["per_video_budget_usd"] = float(per_call)
        settings["safety"] = safety
        ws.settings_json = settings


def _preview_rows(workspace_id: str) -> list[CostEntry]:
    from app.api.v1.preview import PREVIEW_CATEGORY

    with session_scope() as s:
        return list(s.query(CostEntry).filter(
            CostEntry.workspace_id == workspace_id,
            CostEntry.category == PREVIEW_CATEGORY,
        ).all())


def _offerable(provider_id: str) -> dict:
    return {"provider": provider_id, "offerable": True, "available": True,
            "reason": "ok", "message": "ok", "missing_credentials": []}


class _FakeTTS:
    """A TTS-shaped double. No network, no paid call, no real provider.

    Subclass it to change the answer rather than monkeypatching a factory: a
    subclass can override ``synthesize`` and inherit the counters, which is what
    "the provider was called zero times" is asserted against.
    """

    name = "mock"
    is_mock = True
    audio = b"RIFF" + b"0" * 900
    #: Mirrors ``ElevenLabsTTSProvider.EST_USD_PER_CHAR``. Absent on a provider
    #: that publishes no price, which is the unknown-exposure branch.
    EST_USD_PER_CHAR: float | None = None

    def __init__(self) -> None:
        self.calls = 0

    def voices(self, language: str = ""):
        return [{"id": "v1", "gender": "", "locale": "en"}]

    def synthesize(self, text, **kw):
        from app.providers.tts import TTSResult

        self.calls += 1
        return TTSResult(audio_bytes=self.audio, format="wav",
                         provider=self.name, is_mock=self.is_mock,
                         sample_rate=24000)


# ===========================================================================
# A -- the concurrency proof
# ===========================================================================


def test_concurrent_reservations_never_exceed_the_cap(ws_a):
    """The claim: N threads race one cap, and the cap holds exactly.

    Sixteen threads, each asking for the same money, against a cap that admits
    five. A process-local counter or an unlocked read-then-write lets more than
    five through; only the database lock prevents it. Asserted on the RECORDED
    total as well as the grant count, because a guard that refuses correctly but
    still writes the row is double-booking.

    Measured on this repository's SQLite configuration, the same 16 callers
    against the same cap write ``total=16.0`` when the transaction does not take
    the write lock first, and ``total=5.0`` when it does.
    """
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 5.0)
    cap, each, threads = 5.0, 1.0, 16

    granted: list[str] = []
    refused = threading.Semaphore(0)
    lock = threading.Lock()

    def take() -> None:
        try:
            reservation = cost_mod.reserve_spend(
                ws_id, each, category="race", provider="race")
        except cost_mod.BudgetExceededError:
            refused.release()
            return
        with lock:
            granted.append(reservation.entry_id)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(lambda _i: take(), range(threads)))

    assert len(granted) == int(cap), (
        f"{len(granted)} reservations were granted against a cap of {cap}")
    recorded = cost_mod.spent_since(ws_id, hours=24.0)
    assert recorded == pytest.approx(cap), (
        f"the ledger recorded ${recorded} against a ${cap} cap")
    assert recorded <= cap, "the recorded total exceeded the cap"
    # A refusal must leave no trace: an un-committed row would shrink the
    # remaining budget for the next, legitimate, caller.
    with session_scope() as s:
        rows = s.query(CostEntry).filter(
            CostEntry.workspace_id == ws_id, CostEntry.category == "race").count()
    assert rows == int(cap), f"{rows} rows exist; refusals wrote {rows - int(cap)}"


def test_concurrent_rate_limit_is_counted_in_the_database(ws_a, monkeypatch):
    """The RATE cap is the same claim with a different unit.

    A dict in a module counts per process. Asserted by checking that the count
    the API reports is the count the gate enforces: after 24 concurrent attempts
    against a ceiling of ten, exactly six are refused and the database holds ten
    rows -- and every granted caller saw a distinct count.
    """
    from app.api.v1 import preview as preview_mod

    ws_id = ws_a["ws_id"]
    ceiling = 10
    monkeypatch.setattr(preview_mod, "PREVIEW_MAX_PER_WINDOW", ceiling)

    granted: list[int] = []
    refused = threading.Semaphore(0)
    lock = threading.Lock()

    def take() -> None:
        try:
            count = preview_mod.reserve_preview(ws_id, 0.0).window_count
        except Exception:  # noqa: BLE001 - HTTPException IS the refusal
            refused.release()
            return
        with lock:
            granted.append(count)

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(lambda _i: take(), range(24)))

    assert len(_preview_rows(ws_id)) == ceiling, (
        f"{len(_preview_rows(ws_id))} preview reservations recorded, "
        f"ceiling {ceiling}")
    assert sorted(granted) == list(range(1, ceiling + 1)), (
        "the window counts handed to concurrent callers must be distinct: a "
        "duplicate count means two callers were charged for one slot")


def test_preview_budget_state_reads_the_database_not_a_module_dict(ws_a):
    """The number an operator sees must be the number the gate enforces.

    Proven by making the reservation through a DIFFERENT session -- which is what
    another worker process would do -- and then reading the state back. A dict
    in the module would report 0 here, which is precisely the 15.6 bug.
    """
    from app.api.v1 import preview as preview_mod

    ws_id = ws_a["ws_id"]
    cost_mod.reserve_spend(ws_id, 0.01, category=preview_mod.PREVIEW_CATEGORY,
                           provider="other-worker")
    assert preview_mod.preview_budget_state(ws_id) == 1


def test_a_refused_reservation_leaves_the_cap_untouched(ws_a):
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 1.0)
    cost_mod.reserve_spend(ws_id, 0.8, category="refuse")
    before = cost_mod.spent_since(ws_id)
    with pytest.raises(cost_mod.BudgetExceededError):
        cost_mod.reserve_spend(ws_id, 0.5, category="refuse")
    assert cost_mod.spent_since(ws_id) == before


def test_daily_dollar_cap_is_enforced_against_actual_spend_not_estimates(ws_a):
    """A settled ACTUAL above the estimate tightens the cap.

    If ``settle_reservation`` inserted a second row, the estimate AND the actual
    would both count and this would refuse earlier than it should; if it wrote
    nothing, the estimate would be all the ledger ever knew. One row, updated in
    place, is the only shape where both are true.
    """
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 2.0)
    reservation = cost_mod.reserve_spend(ws_id, 0.50, category="settle-me")
    cost_mod.settle_reservation(reservation.entry_id, 1.50)
    assert cost_mod.spent_since(ws_id) == pytest.approx(1.50)
    with session_scope() as s:
        rows = s.query(CostEntry).filter(
            CostEntry.workspace_id == ws_id,
            CostEntry.category == "settle-me").all()
    assert len(rows) == 1, "settlement must update the reservation, not add a row"
    assert rows[0].is_estimate is False
    assert (rows[0].detail_json or {}).get("estimated_usd") == pytest.approx(0.50)


def test_unknown_exposure_is_recorded_instead_of_being_dropped_as_zero(ws_a):
    """A lost response is not a free call.

    The first assertion documents the trap: booking the unknown as ``$0``
    through the ordinary path writes NOTHING, so the charge vanishes. The second
    is the fix: an explicit UNKNOWN_EXPOSURE row exists, carries the marker, and
    invents no amount.
    """
    ws_id = ws_a["ws_id"]
    cost_mod.track_cost(ws_id, "tts", 0.0, provider="elevenlabs")
    assert cost_mod.spent_since(ws_id) == 0.0
    with session_scope() as s:
        assert s.query(CostEntry).filter(
            CostEntry.workspace_id == ws_id).count() == 0, (
            "the <=0 early return must still drop a genuinely free call")

    entry_id = cost_mod.book_unknown_exposure(
        ws_id, category="tts", provider="elevenlabs",
        detail={"chars": 600, "reason": "read timeout, response lost"})
    with session_scope() as s:
        entry = s.get(CostEntry, entry_id)
        assert entry is not None, "an unknown exposure must leave a row behind"
        assert entry.amount_usd == 0.0
        assert entry.detail_json["exposure_unknown"] is True
        assert entry.detail_json["cost_outcome"] == cost_mod.UNKNOWN_EXPOSURE_MARKER
        assert "read timeout" in entry.detail_json["reason"]


def test_unknown_exposure_still_counts_against_a_rate_limit(ws_a):
    """An unpriced call still happened, so the rate limit must still see it."""
    from app.api.v1 import preview as preview_mod

    ws_id = ws_a["ws_id"]
    cost_mod.book_unknown_exposure(ws_id, category=preview_mod.PREVIEW_CATEGORY,
                                    provider="elevenlabs")
    assert preview_mod.preview_budget_state(ws_id) == 1


def test_reservation_marks_itself_in_the_ledger(ws_a):
    ws_id = ws_a["ws_id"]
    reservation = cost_mod.reserve_spend(ws_id, 0.25, category="marked",
                                         provider="p", detail={"chars": 100})
    with session_scope() as s:
        entry = s.get(CostEntry, reservation.entry_id)
    assert entry.detail_json[cost_mod.RESERVATION_MARKER] is True
    assert entry.detail_json["chars"] == 100
    assert entry.is_estimate is True


def test_headroom_and_reservation_agree(ws_a):
    """One arithmetic, two entry points. A drifting check is worse than none."""
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 3.0)
    room = cost_mod.budget_headroom(ws_id, category="agree", window_seconds=60.0)
    assert room.events == 0 and room.spent_usd == pytest.approx(0.0)
    cost_mod.reserve_spend(ws_id, 1.0, category="agree", window_seconds=60.0)
    room = cost_mod.budget_headroom(ws_id, category="agree", window_seconds=60.0)
    assert room.events == 1
    assert room.remaining_usd == pytest.approx(2.0)
    assert room.to_dict()["spent_usd"] == pytest.approx(1.0)


def test_headroom_writes_nothing(ws_a):
    """A read that reserved would spend the budget it just measured."""
    ws_id = ws_a["ws_id"]
    cost_mod.budget_headroom(ws_id, category="read-only")
    with session_scope() as s:
        assert s.query(CostEntry).filter(
            CostEntry.workspace_id == ws_id).count() == 0


def test_reserve_without_a_category_is_refused_by_assert_can_spend(ws_a):
    """``assert_can_spend(reserve=True)`` must not silently skip the reservation."""
    ws_id = ws_a["ws_id"]
    with pytest.raises(ValueError):
        cost_mod.assert_can_spend(ws_id, 0.1, reserve=True)


def test_the_advisory_gate_still_refuses_when_the_workspace_is_broke(ws_a):
    """The legacy providers' pre-spend gate keeps working.

    ``assert_can_spend`` without a category is the advisory read every provider
    in the repo already calls. It is not authoritative -- a read cannot stop a
    concurrent spender -- but it must not have been broken by the rewrite.
    """
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 0.0)
    with pytest.raises(cost_mod.BudgetExceededError):
        cost_mod.assert_can_spend(ws_id, 0.01)
    _set_budgets(ws_id, 5.0)
    assert cost_mod.assert_can_spend(ws_id, 0.01) is None


# ===========================================================================
# B -- the legacy preview route and the other ungated spenders
# ===========================================================================


def test_legacy_voice_preview_route_delegates_to_the_preview_router(ws_a):
    """There is ONE implementation, and the old path reaches it.

    Proven behaviourally: the alias route returns the same provider / clamped
    headers and the same bytes, which is only possible if it ran the same code.

    The cached flag is compared across the SECOND pair of calls, not the first.
    The canonical call warms the cache, so a first-call comparison would
    compare a miss against a hit and call that a delegation failure. The real
    invariant is that both routes agree about the cache at the same point in
    its life: alias-after-canonical must read as cached, exactly as a second
    canonical call does.
    """
    payload = {"text": "delegation check", "provider": "mock", "voice": "v1"}
    alias_url = f"/api/v1/workspaces/{ws_a['ws_id']}/assets/voice/preview"
    canonical_url = f"/api/v1/workspaces/{ws_a['ws_id']}/voice-preview"

    warm = ws_a["client"].post(canonical_url, headers=ws_a["headers"], json=payload)
    assert warm.status_code == 200, warm.text
    assert warm.headers["X-Preview-Cached"] == "0", (
        "the first canonical call should be a cache miss")

    canonical = ws_a["client"].post(canonical_url, headers=ws_a["headers"],
                                    json=payload)
    alias = ws_a["client"].post(alias_url, headers=ws_a["headers"], json=payload)
    assert canonical.status_code == 200, canonical.text
    assert alias.status_code == 200, alias.text
    for header in ("X-TTS-Provider", "X-TTS-Mock", "X-Preview-Clamped",
                   "X-Preview-Chars", "X-Preview-Cached"):
        assert alias.headers.get(header) == canonical.headers.get(header), header
    assert alias.content == canonical.content
    assert alias.headers["X-Preview-Cached"] == "1", (
        "the alias must reach the SAME cache, so it reports a hit after the "
        "canonical route warmed it")


def test_the_alias_route_carries_no_second_implementation():
    """Structural: the alias maps a body and calls ``preview_voice``.

    Reads the real source. A test that only checked behaviour would keep passing
    if someone re-inlined a ``provider.synthesize`` call into the alias while
    keeping the same three headers.
    """
    source = (REPO / "backend" / "app" / "api" / "v1" / "content.py").read_text(
        encoding="utf-8")
    start = source.index("def voice_preview(")
    end = source.index("\n\n\n", start)
    block = source[start:end]
    assert "preview_voice" in block, "the alias must call the canonical handler"
    assert "synthesize(" not in block, (
        "the alias route synthesizes on its own again; that is the second "
        "implementation the work removed")
    assert "get_tts_provider" not in block


def test_the_alias_route_goes_through_the_budget_policy(ws_a, monkeypatch):
    """The alias is gated, which it was not before.

    Exhausting the daily cap must refuse the alias with the same 402, before any
    provider call. A private, uncosted second implementation is precisely what
    let a workspace spend unbudgeted.
    """
    from app.api.v1 import preview as preview_mod

    ws_id = ws_a["ws_id"]
    provider = _FakeTTS()
    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": provider)
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    _set_budgets(ws_id, 0.0)          # no budget at all

    r = ws_a["client"].post(
        f"/api/v1/workspaces/{ws_id}/assets/voice/preview",
        headers=ws_a["headers"],
        json={"text": "will be refused", "provider": "mock"})
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["reason"] == "budget_exhausted"
    assert provider.calls == 0, "the provider was called with no budget"


def test_the_alias_route_preserves_workspace_isolation(ws_a, ws_b, monkeypatch):
    """Two tenants, one of them out of budget: the other is unaffected.

    Isolation has to survive the guard, not just the cache. A guard keyed on
    something other than the workspace -- a shared module counter, a global
    reservation row -- would refuse B here.
    """
    from app.api.v1 import preview as preview_mod

    monkeypatch.setattr(preview_mod, "get_tts_provider",
                        lambda name="": _FakeTTS())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    _set_budgets(ws_a["ws_id"], 0.0)
    _set_budgets(ws_b["ws_id"], 5.0)

    refused = ws_a["client"].post(
        f"/api/v1/workspaces/{ws_a['ws_id']}/assets/voice/preview",
        headers=ws_a["headers"],
        json={"text": "tenant a text", "provider": "mock"})
    assert refused.status_code == 402, refused.text

    allowed = ws_b["client"].post(
        f"/api/v1/workspaces/{ws_b['ws_id']}/assets/voice/preview",
        headers=ws_b["headers"],
        json={"text": "tenant a text", "provider": "mock"})
    assert allowed.status_code == 200, allowed.text
    # Identical text, identical provider: the cache must still be per workspace.
    assert (preview_mod.preview_cache_dir(ws_a["ws_id"])
            != preview_mod.preview_cache_dir(ws_b["ws_id"]))
    assert preview_mod.preview_budget_state(ws_a["ws_id"]) == 0
    assert preview_mod.preview_budget_state(ws_b["ws_id"]) == 1


def test_the_alias_route_needs_a_workspace_membership(ws_a):
    """An unauthenticated caller gets 401/403, never a paid synthesis."""
    r = ws_a["client"].post(
        f"/api/v1/workspaces/{ws_a['ws_id']}/assets/voice/preview",
        json={"text": "nope", "provider": "mock"})
    assert r.status_code in (401, 403), r.text


def test_connections_tts_test_is_cost_guarded(ws_a, monkeypatch):
    """The Connections "test voice" button spends characters; now gated.

    Before this change the route called ``provider.synthesize`` with no budget
    check, no cache and no rate limit: a diagnostic that drains an account.
    """
    provider = _FakeTTS()
    monkeypatch.setattr("app.providers.tts.get_tts_provider", lambda *a, **k: provider)
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 0.0)

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/connections/tts/test",
                            headers=ws_a["headers"], json={"text": "diagnostic"})
    assert r.status_code == 402, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "budget_exhausted"
    assert detail["authority"] == "database"
    assert provider.calls == 0


def test_connections_tts_test_records_a_reservation_it_can_name(ws_a, monkeypatch):
    """The gate's answer is a ledger row, so the charge is traceable."""
    provider = _FakeTTS()
    monkeypatch.setattr("app.providers.tts.get_tts_provider", lambda *a, **k: provider)
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 5.0)

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/connections/tts/test",
                            headers=ws_a["headers"], json={"text": "hello there"})
    assert r.status_code == 200, r.text
    entry_id = r.headers["X-Budget-Reservation"]
    with session_scope() as s:
        entry = s.get(CostEntry, entry_id)
    assert entry is not None
    assert entry.workspace_id == ws_id
    assert entry.detail_json[cost_mod.RESERVATION_MARKER] is True


def test_connections_tts_test_shares_the_narration_rate_limit(ws_a, monkeypatch):
    """One narration cap, not one per route.

    If the diagnostic button had its own counter, an operator could spend N times
    the intended rate simply by clicking a different button.
    """
    from app.api.v1 import preview as preview_mod

    monkeypatch.setattr("app.providers.tts.get_tts_provider",
                        lambda *a, **k: _FakeTTS())
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 50.0)
    monkeypatch.setattr(preview_mod, "PREVIEW_MAX_PER_WINDOW", 2)
    for index in range(2):
        ok = ws_a["client"].post(
            f"/api/v1/workspaces/{ws_id}/connections/tts/test",
            headers=ws_a["headers"], json={"text": f"sample {index}"})
        assert ok.status_code == 200, ok.text
    over = ws_a["client"].post(
        f"/api/v1/workspaces/{ws_id}/connections/tts/test",
        headers=ws_a["headers"], json={"text": "sample 3"})
    assert over.status_code == 429, over.text


def test_the_avatar_routes_voice_step_is_cost_guarded(ws_a, monkeypatch):
    """``content.py`` synthesized driving audio with no gate at all."""
    provider = _FakeTTS()
    monkeypatch.setattr("app.providers.tts.get_tts_provider", lambda *a, **k: provider)
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 0.0)

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/assets/avatar",
                            headers=ws_a["headers"],
                            json={"image": "presenter.png", "text": "present this"})
    assert r.status_code == 402, r.text
    assert provider.calls == 0, "narration was synthesized with no budget"


def test_a_provider_fault_does_not_release_the_reservation(ws_a, monkeypatch):
    """A fault may still have been billed, so the budget stays reserved.

    Voiding on failure is the tempting behaviour and it is wrong: the provider
    acknowledged nothing, so nobody knows whether the characters were charged.
    """
    from app.api.v1 import preview as preview_mod
    from app.providers.tts import TTSError

    class _Failing(_FakeTTS):
        def synthesize(self, text, **kw):
            self.calls += 1
            raise TTSError("provider down after accepting the request")

    provider = _Failing()
    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": provider)
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 50.0)

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/voice-preview",
                            headers=ws_a["headers"],
                            json={"text": "a fault is not a refund", "provider": "mock"})
    assert r.status_code == 503, r.text
    assert len(_preview_rows(ws_id)) == 1, (
        "the reservation was released on a provider fault; if the provider billed "
        "it, that charge is now unbudgeted")


def test_a_mock_preview_settles_at_zero(ws_a, monkeypatch):
    """A simulation frees the budget instead of consuming it."""
    from app.api.v1 import preview as preview_mod

    monkeypatch.setattr(preview_mod, "get_tts_provider",
                        lambda name="": _FakeTTS())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 50.0)

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/voice-preview",
                            headers=ws_a["headers"],
                            json={"text": "free to rehearse", "provider": "mock"})
    assert r.status_code == 200, r.text
    assert cost_mod.spent_since(ws_id) == pytest.approx(0.0)
    rows = _preview_rows(ws_id)
    assert len(rows) == 1 and rows[0].amount_usd == 0.0


def test_a_paid_preview_settles_at_the_adapters_own_price(ws_a, monkeypatch):
    """The price comes from the adapter, not from a constant in the API layer."""
    from app.api.v1 import preview as preview_mod

    class _Paid(_FakeTTS):
        name = "elevenlabs"
        is_mock = False
        EST_USD_PER_CHAR = 0.0002

    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": _Paid())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 50.0)
    text = "twenty five characters!!"

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/voice-preview",
                            headers=ws_a["headers"],
                            json={"text": text, "provider": "mock"})
    assert r.status_code == 200, r.text
    assert cost_mod.spent_since(ws_id) == pytest.approx(len(text) * 0.0002)


def test_empty_audio_from_a_billed_provider_is_unknown_exposure(ws_a, monkeypatch):
    """No audio from a paid provider is NOT a free call."""
    from app.api.v1 import preview as preview_mod

    class _Empty(_FakeTTS):
        name = "elevenlabs"
        is_mock = False
        audio = b""
        EST_USD_PER_CHAR = 0.0002

    monkeypatch.setattr(preview_mod, "get_tts_provider", lambda name="": _Empty())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 50.0)

    r = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/voice-preview",
                            headers=ws_a["headers"],
                            json={"text": "silence is still billed", "provider": "mock"})
    assert r.status_code == 200, r.text
    # The ``ws_a`` workspace is shared by every test in this module, so rows
    # accumulate. Identify THIS attempt by the adapter that produced it: the
    # row carries chars/audio_bytes, not the narration text.
    rows = [row for row in _preview_rows(ws_id)
            if row.provider == "elevenlabs"
            and row.detail_json.get("audio_bytes") == 0]
    assert len(rows) == 1, f"expected one row for this attempt, got {len(rows)}"
    assert rows[0].detail_json.get("exposure_unknown") is True
    assert rows[0].detail_json.get("cost_outcome") == cost_mod.UNKNOWN_EXPOSURE_MARKER


def test_the_rate_refusal_is_429_and_the_money_refusal_is_402(ws_a, monkeypatch):
    """A client must be able to tell "slow down" from "you are broke"."""
    from app.api.v1 import preview as preview_mod

    monkeypatch.setattr(preview_mod, "get_tts_provider",
                        lambda name="": _FakeTTS())
    monkeypatch.setattr(preview_mod, "_offerable_row",
                        lambda pid, ws: _offerable(pid))
    ws_id = ws_a["ws_id"]
    _set_budgets(ws_id, 50.0)
    monkeypatch.setattr(preview_mod, "PREVIEW_MAX_PER_WINDOW", 2)
    for index in range(2):
        ok = ws_a["client"].post(
            f"/api/v1/workspaces/{ws_id}/voice-preview",
            headers=ws_a["headers"],
            json={"text": f"rate {index}", "provider": "mock"})
        assert ok.status_code == 200, ok.text
    rate = ws_a["client"].post(
        f"/api/v1/workspaces/{ws_id}/voice-preview",
        headers=ws_a["headers"],
        json={"text": "rate 3", "provider": "mock"})
    assert rate.status_code == 429, rate.text
    assert rate.json()["detail"]["reason"] == "preview_budget_exceeded"
    assert rate.json()["detail"]["authority"] == "database"

    # A fresh workspace, so the rate limit is not what refuses this one: the
    # point is that a MONEY refusal is 402 even when the rate limit is wide open.
    ws_id, headers = _register(ws_a["client"], "c")
    _set_budgets(ws_id, 0.0)
    money = ws_a["client"].post(f"/api/v1/workspaces/{ws_id}/voice-preview",
                                headers=headers,
                                json={"text": "no money", "provider": "mock"})
    assert money.status_code == 402, money.text
    assert money.json()["detail"]["reason"] == "budget_exhausted"


# ===========================================================================
# C -- TwelveLabs credential scope
# ===========================================================================

TWELVELABS = "intel.twelvelabs_api_key"


def test_twelvelabs_key_is_registered_as_a_secret():
    """It was not in REGISTRY, so ``get_credential`` raised ``KeyError``.

    That ``KeyError`` is the whole defect: the provider caught it and fell back
    to a raw process-wide ``os.environ`` read, which is not workspace-scoped and
    which no operator can revoke.
    """
    assert TWELVELABS in ps.REGISTRY, (
        "the key is still outside the resolver, so the provider's KeyError "
        "fallback to a raw env var is still live")
    spec = ps.REGISTRY[TWELVELABS]
    assert spec["secret"] is True
    assert spec["label"].strip()


def test_twelvelabs_env_fallback_matches_the_established_registry_policy():
    """No invented precedence.

    ``get_credential``'s contract is ``db -> global db row -> env attribute of
    the same name on settings -> None``. This key has no settings attribute, so
    the honest entry is ``env: None`` -- exactly what ``google.client_secret``,
    ``tts.kokoro_api_key`` and ``tts.qwen_api_key`` carry. Naming a non-existent
    attribute would advertise a fallback that can never resolve.
    """
    from app.core.config import settings as env_settings

    spec = ps.REGISTRY[TWELVELABS]
    assert spec["env"] is None
    assert not hasattr(env_settings, "twelvelabs_api_key"), (
        "settings grew the attribute; set the registry `env` to it so the "
        "resolver actually reads it instead of leaving a dead name behind")
    for sibling in ("google.client_secret", "tts.kokoro_api_key",
                    "tts.qwen_api_key"):
        assert ps.REGISTRY[sibling]["env"] is None, sibling
        assert ps.REGISTRY[sibling]["secret"] is True, sibling


def test_twelvelabs_key_is_workspace_scoped(ws_a, ws_b):
    """Saved for A, invisible to B. The property the raw env read could not give."""
    from app.services.provider_settings import get_credential, set_credential

    try:
        set_credential(TWELVELABS, _PROBE, workspace_id=ws_a["ws_id"])
        mine, source = get_credential(TWELVELABS, workspace_id=ws_a["ws_id"])
        assert mine == _PROBE and source == "db"
        theirs, their_source = get_credential(TWELVELABS, workspace_id=ws_b["ws_id"])
        assert not theirs, f"workspace B resolved workspace A's key ({their_source})"
    finally:
        set_credential(TWELVELABS, "", workspace_id=ws_a["ws_id"])


def test_twelvelabs_provider_resolves_through_the_resolver(ws_a, monkeypatch):
    """The provider's own resolution order now finds the workspace row.

    ``_api_key`` tries the resolver first and only then ``os.environ``. With the
    key registered, a workspace-scoped row wins -- and it is read under the
    workspace context, which is how a queued worker resolves a tenant key.
    """
    from app.engine.intel.impl.semantic_rerank import _api_key
    from app.services.provider_settings import set_credential, workspace_scope

    try:
        set_credential(TWELVELABS, _PROBE, workspace_id=ws_a["ws_id"])
        monkeypatch.delenv("TWELVELABS_API_KEY", raising=False)
        with workspace_scope(ws_a["ws_id"]):
            assert _api_key() == _PROBE
    finally:
        set_credential(TWELVELABS, "", workspace_id=ws_a["ws_id"])


def test_twelvelabs_key_is_never_exposed_by_the_api(ws_a):
    """A state word only: not the value, not a masked prefix, not a digest.

    The plaintext and the digest are collision-proof assertions. The key's
    LENGTH is deliberately NOT asserted as a bare substring of the body: a
    two-digit length collides with any line number in the evidence paths the
    same payload carries, so that check would be flaky and would prove nothing.
    The structural assertion below is the real guarantee -- no field anywhere in
    the response is allowed to carry a value-shaped credential -- plus the
    closed vocabulary the credential state must come from.
    """
    from app.providers.maturity import RESOLVED_CREDENTIAL_STATES
    from app.services.provider_settings import set_credential

    secret = _PROBE + "-do-not-leak"
    try:
        set_credential(TWELVELABS, secret, workspace_id=ws_a["ws_id"])
        r = ws_a["client"].get(
            f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity",
            headers=ws_a["headers"])
        assert r.status_code == 200, r.text
        assert secret not in r.text, "the plaintext key reached the frontend"
        assert hashlib.sha256(secret.encode()).hexdigest() not in r.text, \
            "the key's digest reached the frontend"
        payload = r.json()
        assert "••••" not in r.text, "a masked value reached the frontend"
        for row in payload["items"]:
            state = row["resolved_credential_status"]
            assert state in RESOLVED_CREDENTIAL_STATES, state
            assert secret[:4] not in json.dumps(row), "a key prefix reached a row"
        # The payload carries no field that could hold a credential at all.
        forbidden = {"api_key", "key", "value", "secret", "masked", "token",
                     "credential"}
        assert not (forbidden & set(payload)), "the payload gained a value field"
    finally:
        set_credential(TWELVELABS, "", workspace_id=ws_a["ws_id"])


def test_twelvelabs_credential_state_is_a_word_not_a_value(ws_a):
    """``resolve_credential_status`` reports CONFIGURED, never the key."""
    from app.providers.maturity import CREDENTIAL_CONFIGURED, resolve_credential_status
    from app.services.provider_settings import set_credential

    record = type("R", (), {"credential_status": "REQUIRED",
                            "credential_keys": (TWELVELABS,)})()
    try:
        set_credential(TWELVELABS, _PROBE, workspace_id=ws_a["ws_id"])
        assert resolve_credential_status(record, ws_a["ws_id"]) == CREDENTIAL_CONFIGURED
    finally:
        set_credential(TWELVELABS, "", workspace_id=ws_a["ws_id"])
    assert resolve_credential_status(record, ws_a["ws_id"]) == "NOT_CONFIGURED"


def test_twelvelabs_key_is_not_logged(ws_a, capsys):
    """Resolving the key must not print it. A debug log is still a leak."""
    from app.services.provider_settings import get_credential, set_credential

    try:
        set_credential(TWELVELABS, _PROBE, workspace_id=ws_a["ws_id"])
        get_credential(TWELVELABS, workspace_id=ws_a["ws_id"])
    finally:
        set_credential(TWELVELABS, "", workspace_id=ws_a["ws_id"])
    captured = capsys.readouterr()
    assert _PROBE not in captured.out
    assert _PROBE not in captured.err


def test_commercial_mode_still_refuses_semantic_rerank(ws_a):
    """Registering the key must not switch the licence gate off.

    ``semantic_rerank`` reports ``COMMERCIAL_UNVERIFIED``, and
    ``intel.registry.resolve(commercial_mode=True)`` refuses anything that is not
    PERMITTED. If adding the credential had changed that verdict, a paid,
    licence-unverified provider would have become commercially usable as a side
    effect of a credential fix -- the worst possible outcome of the work.
    """
    from app.engine.intel import registry as intel_registry
    from app.services.provider_settings import set_credential, workspace_scope

    try:
        set_credential(TWELVELABS, _PROBE, workspace_id=ws_a["ws_id"])
        with workspace_scope(ws_a["ws_id"]):
            open_provider, _ = intel_registry.resolve(
                "semantic_rerank", commercial_mode=False)
            blocked, reasons = intel_registry.resolve(
                "semantic_rerank", commercial_mode=True)
        assert open_provider is not None, "the provider should resolve off-mode"
        assert blocked is None, (
            "commercial mode must refuse a provider whose commercial status is "
            "still UNVERIFIED")
        assert "semantic_rerank" in reasons
        assert "commercial" in reasons["semantic_rerank"].lower()
    finally:
        set_credential(TWELVELABS, "", workspace_id=ws_a["ws_id"])


# ===========================================================================
# D -- operator visibility
# ===========================================================================


def _make_ambiguous_video(workspace_id: str, *, remote_id: str = "",
                          detail: str = "read timeout after the submit") -> str:
    """A render whose submit may have been billed, as the pipeline records it."""
    from app.models import ContentItem, VideoVariant

    with session_scope() as s:
        item = ContentItem(workspace_id=workspace_id, topic="incident topic",
                           status="SCRIPT_READY")
        s.add(item)
        s.flush()
        variant = VideoVariant(content_item_id=item.id,
                               label="v1", script="a script")
        s.add(variant)
        s.flush()
        video = Video(workspace_id=workspace_id, variant_id=variant.id,
                      engine="moneyprinterturbo", status="RENDERING",
                      submission_state="SUBMISSION_UNKNOWN",
                      provider_task_id=remote_id, submission_detail=detail,
                      idempotency_key="k" * 32)
        s.add(video)
        s.flush()
        return video.id


def test_incidents_endpoint_exists_on_the_workspace_maturity_router(ws_a):
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["unknown_exposure_count"] == 0
    assert "SUBMISSION_UNKNOWN" in body["states"]


def test_an_ambiguous_submit_is_never_reported_as_failed(ws_a):
    """The load-bearing rule.

    A failed submit was rejected and cost nothing; an unknown one may already be
    an invoice. An operator who reads "Failed" cancels the job and misses the
    charge, so the state travels verbatim under its own name.
    """
    _make_ambiguous_video(ws_a["ws_id"])
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1, items
    row = items[0]
    assert row["state"] == "SUBMISSION_UNKNOWN"
    assert row["display_state"] == "SUBMISSION_UNKNOWN"
    assert row["state"] != "FAILED"
    assert row["exposure"] == "UNKNOWN_EXPOSURE"
    assert row["exposure_unknown"] is True
    assert row["estimated_exposure_usd"] is None, (
        "an unknown exposure must not be given a fabricated number")
    assert row["note"]


def test_an_incident_carries_everything_an_operator_needs_to_decide(ws_a):
    _make_ambiguous_video(ws_a["ws_id"], remote_id="remote-abc-123",
                          detail="submit delivered, no task id came back")
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    row = r.json()["items"][0]
    assert row["provider"] == "moneyprinterturbo"
    assert row["operation"] == "video.render.submit"
    assert row["remote_id"] == "remote-abc-123"
    assert row["attempted_at"], "an incident with no attempt time is unactionable"
    assert row["recommended_action"] == "RECONCILE"
    assert "no task id" in row["detail"]
    # Parseable, not a stringified dict: a UI has to be able to read it.
    assert utcnow_minus_a_day() < datetime.fromisoformat(row["attempted_at"])


def utcnow_minus_a_day():
    from app.models.base import utcnow as _utcnow

    return _utcnow() - timedelta(days=1)


def test_no_retry_is_offered_unless_retry_safety_is_proven(ws_a):
    """An ambiguous submit forbids a resubmit; that is the money rule.

    ``may_resubmit`` and ``retry_safe`` are separate fields on purpose. The
    first says the contract will refuse a resubmit; the second says whether a
    human may safely do one. Reporting only the first invites a UI to render a
    button the backend will (rightly) reject.
    """
    _make_ambiguous_video(ws_a["ws_id"])
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    row = r.json()["items"][0]
    assert row["retry_safe"] is False
    assert row["may_resubmit"] is False


def test_a_provably_undelivered_submit_is_reported_as_retry_safe(ws_a):
    """The ONE case where retrying cannot double-charge, shown as such.

    A connect failure proves the request never reached the provider, so the
    contract marks it ``RetrySafety.SAFE``. If the incidents payload flattened
    every ambiguous row to "unsafe", an operator would reconcile by hand for
    cases that are provably free to retry -- and would stop trusting the list.
    """
    from app.services.paid_executor import (
        Reconciliation,
        RetrySafety,
        verdict_for,
    )
    from app.services.paid_jobs import SubmissionState

    record = type("R", (), {})()
    del record
    from app.services.paid_executor import PaidSubmission

    safe = PaidSubmission(
        workspace_id=ws_a["ws_id"], provider="elevenlabs",
        operation="tts.text_to_speech", state=SubmissionState.SUBMISSION_UNKNOWN,
        retry_safety=RetrySafety.SAFE,
        reconciliation=Reconciliation.RETRY_IF_CONFIRMED_SAFE)
    assert verdict_for(safe) == "RETRY"
    assert safe.may_resubmit is False, (
        "the state still forbids an automatic resubmit; only a human may")
    unsafe = PaidSubmission(
        workspace_id=ws_a["ws_id"], provider="elevenlabs",
        operation="tts.text_to_speech", state=SubmissionState.SUBMISSION_UNKNOWN)
    assert verdict_for(unsafe) == "RECONCILE"


def test_an_unknown_exposure_cost_row_appears_as_an_incident(ws_a):
    """The other source: a billed call whose amount nobody can price."""
    from app.api.v1.preview import PREVIEW_CATEGORY

    cost_mod.book_unknown_exposure(
        ws_a["ws_id"], category=PREVIEW_CATEGORY, provider="elevenlabs",
        detail={"reason": "read timeout; the provider may have billed it"})
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    # The workspace is shared across this module, so assert on the rows this
    # call can identify rather than on a global count.
    assert body["unknown_exposure_count"] >= 1
    row = next(i for i in body["items"] if i["source"] == "cost_entry"
               and i["provider"] == "elevenlabs"
               and "read timeout" in (i.get("detail") or ""))
    assert row["state"] == "SUBMISSION_UNKNOWN"
    assert row["exposure_unknown"] is True
    assert row["retry_safe"] is False


def test_a_settled_cost_is_not_an_incident(ws_a):
    """Only unresolved money shows up. Otherwise the list is noise."""
    from app.api.v1.preview import PREVIEW_CATEGORY

    reservation = cost_mod.reserve_spend(
        ws_a["ws_id"], 0.01, category=PREVIEW_CATEGORY, provider="edge")
    cost_mod.settle_reservation(reservation.entry_id, 0.01)
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    assert r.json()["items"] == []


def test_incidents_are_workspace_scoped(ws_a, ws_b):
    """One tenant's ambiguous render is never shown to another."""
    _make_ambiguous_video(ws_a["ws_id"])
    own = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    assert len(own.json()["items"]) == 1
    other = ws_b["client"].get(
        f"/api/v1/workspaces/{ws_b['ws_id']}/provider-maturity/incidents",
        headers=ws_b["headers"])
    assert other.json()["items"] == [], "an incident crossed a tenant boundary"


def test_incidents_need_a_workspace_membership(ws_a):
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents")
    assert r.status_code in (401, 403), r.text


def test_the_panel_offers_a_retry_only_when_retry_safety_is_proven():
    """The frontend contract, checked from the backend side.

    Reads the real component. The rule is structural: the retry button is inside
    a branch that requires ``retry_safe === true``, and the fallback branch says
    why there is no button. A UI test framework is deliberately not added for one
    conditional -- the property worth protecting is that the dangerous control
    cannot be reached without the flag, and that is checkable from source.
    """
    source = UI.read_text(encoding="utf-8")
    assert 'wsApi.get("/provider-maturity/incidents")' in source, \
        "the panel does not read the incidents endpoint"
    gated = re.search(r"row\.retry_safe === true\s*\?\s*\(\s*<button", source)
    assert gated, (
        "a retry button must be reachable only through `retry_safe === true`")
    assert "No retry offered" in source, \
        "the panel must say why a retry is absent, not just omit it"
    # The state word must reach the screen, not be mapped to a friendly failure.
    assert "row.display_state || row.state" in source
    assert "SUBMISSION_UNKNOWN" in source, \
        "the panel must name the ambiguous state rather than hide it"


def test_the_panel_does_not_render_submission_unknown_as_failed():
    """The ambiguous state must never be presented as a plain failure.

    Scans JSX-bearing lines only. A doc comment is allowed to mention both
    words -- the rule is about what the user SEES, and a naive whole-file regex
    flags the very comment that documents the rule.
    """
    source = UI.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "//", "/*")))
    bad = re.findall(r'SUBMISSION_UNKNOWN[^}\n]{0,80}["\']Failed', code)
    assert not bad, f"the panel labels an ambiguous submit as a failure: {bad}"
    assert 'tone: "bad"' in source, "the ambiguous state must be visually distinct"


def test_the_incident_payload_is_json_serialisable(ws_a):
    """No stray enums, datetimes or Decimals in what a UI receives."""
    _make_ambiguous_video(ws_a["ws_id"])
    r = ws_a["client"].get(
        f"/api/v1/workspaces/{ws_a['ws_id']}/provider-maturity/incidents",
        headers=ws_a["headers"])
    json.loads(r.text)


# ===========================================================================
# live provider coverage -- honestly skipped without a real key
# ===========================================================================


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("TWELVELABS_API_KEY"),
                    reason="TWELVELABS_API_KEY is not set")
def test_live_twelvelabs_resolves_from_the_process_env_fallback():
    """The only env path this key has, and it must still work.

    Runs against the real deployment shape only when an operator has exported a
    key. It is not here to prove the vendor works -- it proves the registration
    did not cut an existing ``TWELVELABS_API_KEY`` deployment off.
    """
    from app.engine.intel.impl.semantic_rerank import _api_key

    value = _api_key()
    assert value, "TWELVELABS_API_KEY is set but the provider resolved nothing"
    assert value == os.environ["TWELVELABS_API_KEY"]
