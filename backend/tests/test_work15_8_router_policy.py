"""Work 15.8 -- a routed chain is a money decision, and this file is the proof.

``docs/MODEL_ROUTER_EXECUTION_AUDIT.md`` measured three defects in
``app/engine/intelligence/router.py``:

1. ``ModelRouter.complete`` caught bare ``Exception`` and advanced, so a leg that
   may already have been billed was indistinguishable from a clean 4xx -- a
   six-tier chain could become six paid POSTs;
2. the resolver returned the literal string ``"local"`` for a local tier, which
   is truthy and therefore reached a remote gateway, and returned ``""`` for a
   remote tier, which ``llm.complete`` silently replaced with its own default,
   discarding the selected cost tier (PREMIUM is 8.0x baseline);
3. no LLM leg was budget-gated, so no LLM spend was pre-authorised.

Every claim below is made at the OUTBOUND BOUNDARY. The number that matters is
how many requests actually left the process -- "the function was called once"
and "one request was sent" are different claims and only the second costs money.
No test here opens a socket.

Groups:

* **policy (§9)** -- a typed, persisted, audited budget for one chain.
* **multiplication (§2)** -- the chain stops on anything unproven and is capped
  on paid legs, while outage / 4xx / connect failure still fall through.
* **local vs remote (§3)** -- an execution target, so ``"local"`` cannot reach a
  gateway and a local tier cannot silently become a paid remote call.
* **budget (§4)** -- estimate -> assert -> reserve -> attempt -> call ->
  reconcile, and a refusal that never reaches the wire.
* **the silent no-op (§5)** -- the DecisionEngine's router integration, which
  never actually routed.
"""

from __future__ import annotations

import pytest

from app.engine.intelligence import llm_paid
from app.engine.intelligence import router as routing
from app.engine.intelligence.llm_paid import LegOutcome
from app.engine.intelligence.providers.base import ProviderUnavailable
from app.engine.intelligence.providers.llm_provider import LLMDecisionProvider
from app.engine.intelligence.router import (
    AmbiguousLegStopped,
    ChainRecord,
    ExecutionTarget,
    FailureClass,
    FallbackPolicy,
    LocalExecutionRefused,
    ModelCapabilityRegistry,
    ModelRouter,
    PrivacyRefusal,
    RouteRequest,
    UnresolvedModel,
)
from app.providers import llm as llm_mod
from app.services import cost as cost_mod
from app.services.paid_executor import (
    Reconciliation,
    RetryVerdict,
    SubmissionState,
    verdict_for,
)

CHAT_URL = "http://llm.test/v1/chat/completions"

#: Long enough that the price table yields a non-zero estimate, so "budgeted"
#: and "unbudgeted" are distinguishable from "free".
PROMPT_TOKENS = 4000
COMPLETION_TOKENS = 900

ALL_TIERS = ("FAST", "BALANCED", "HIGH_QUALITY", "PREMIUM",
             "LOCAL_ONLY", "PRIVATE")


def _settle_event(amount: float) -> str:
    """The ledger event for booking ``amount``, priced by the real table."""
    return f"settle:{amount:.6f}"


#: What one 4000/900-token completion costs at the repo's default prices. Used
#: so an assertion on the ledger is exact without hardcoding a price table.
SETTLED_USD = cost_mod.estimate_llm_cost("test-model", PROMPT_TOKENS,
                                          COMPLETION_TOKENS)


# ===========================================================================
# the outbound boundary: a recorder, not a mock of our own code
# ===========================================================================


class Wire:
    """Counts REAL outbound requests and scripts the answers."""

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
        self.handler = _script(outcome)
        return self

    def then(self, *outcomes):
        self.handler = _script(outcomes, consume=True)
        return self


def _request() -> object:
    import httpx

    return httpx.Request("POST", CHAT_URL)


def _script(outcomes, *, consume: bool = False):
    queue = list(outcomes)
    first = outcomes[0]

    def handler(_url, **_kw):
        value = first if not consume else (
            queue.pop(0) if len(queue) > 1 else queue[0])
        if isinstance(value, BaseException):
            raise value
        return value
    return handler


def _ok(text: str = "a finished answer", *, choices: bool = True,
        usage: bool = True):
    import httpx

    body: dict = {}
    if choices:
        body["choices"] = [{"message": {"content": text}}]
    if usage:
        body["usage"] = {"prompt_tokens": PROMPT_TOKENS,
                         "completion_tokens": COMPLETION_TOKENS}
    return httpx.Response(200, json=body, request=_request())


def _no_choices():
    """A 2xx that was billed and carries nothing we can use."""
    import httpx

    return httpx.Response(200, json={"id": "cmpl-abc",
                                     "usage": {"prompt_tokens": PROMPT_TOKENS,
                                               "completion_tokens": 12}},
                          request=_request())


def _rejected(status: int = 400, text: str = "unsupported parameter"):
    import httpx

    response = httpx.Response(status, content=text.encode(), request=_request())
    return httpx.HTTPStatusError(f"HTTP {status}", request=_request(),
                                 response=response)


def _server_error(status: int = 503):
    import httpx

    response = httpx.Response(status, content=b"upstream exploded",
                              request=_request())
    return httpx.HTTPStatusError(f"HTTP {status}", request=_request(),
                                 response=response)


@pytest.fixture()
def wire(monkeypatch):
    """Install the recorder on the provider's transport.

    ``llm.complete`` passes ``httpx.post`` in as the transport, so patching the
    attribute on the ``httpx`` module intercepts every request without the
    provider knowing this test exists. The shared conftest fake is overridden
    on purpose: it answers 200 for everything, which cannot express the
    failure modes that cost money.
    """
    assert llm_mod.llm_available(), (
        "the shared conftest fake must make the LLM available; without it "
        "these tests would take the unconfigured path and send nothing")
    rec = Wire()
    monkeypatch.setattr(llm_mod.httpx, "post", rec.post)
    return rec


@pytest.fixture()
def ledger(monkeypatch):
    """The budget gate, in memory. Records ORDER, because order is the claim.

    ``SpendAuthorization`` calls ``cost.assert_can_spend(..., reserve=True)``
    and then ``settle_reservation`` / ``void_reservation``; ``_book_cost``
    calls ``track_cost``. All four are patched here, so a test can assert that
    the gate ran BEFORE the request and that the money was written once.
    """
    events: list[str] = []
    authorizations: list[dict] = []

    def assert_can_spend(workspace_id, estimated_usd, *, category="",
                         provider="", reserve=False, **_kw):
        events.append("authorize")
        authorizations.append({"workspace_id": workspace_id,
                               "estimated_usd": float(estimated_usd or 0.0),
                               "category": category, "provider": provider,
                               "reserve": reserve})
        return _reservation()

    def settle_reservation(entry_id, actual_usd):
        events.append(f"settle:{actual_usd:.6f}")

    def void_reservation(entry_id):  # noqa: ARG001
        events.append("void")
        return True

    def track_cost(workspace_id, category, amount_usd, **_kw):  # noqa: ARG001
        events.append(f"track:{amount_usd:.6f}")
        return amount_usd

    monkeypatch.setattr(cost_mod, "assert_can_spend", assert_can_spend)
    monkeypatch.setattr(cost_mod, "settle_reservation", settle_reservation)
    monkeypatch.setattr(cost_mod, "void_reservation", void_reservation)
    monkeypatch.setattr(cost_mod, "track_cost", track_cost)
    return {"events": events, "authorizations": authorizations}


def _reservation():
    class _Res:
        entry_id = "reservation-1"
        amount_usd = 0.0
    return _Res()


@pytest.fixture()
def paid_log(monkeypatch):
    events: list[dict] = []

    def record_event(workspace_id, kind, message, level="info", source="system",
                     data=None):
        events.append({"workspace_id": workspace_id, "kind": kind,
                       "message": message, "level": level, "source": source,
                       "data": dict(data or {})})
        return {"id": "evt"}

    monkeypatch.setattr("app.services.events.record_event", record_event)
    return {"events": events}


def _req(**kw) -> RouteRequest:
    base = {"task_type": "router-policy", "workspace_id": "ws-w158"}
    base.update(kw)
    return RouteRequest(**base)


def _six_tier_chain(router: ModelRouter, request: RouteRequest) -> RouteRequest:
    """Force the full six-tier chain, so "not six paid POSTs" means something."""
    decision = router.route(request)
    assert len([decision.tier, *decision.fallbacks]) == len(ALL_TIERS), (
        f"this test needs every tier in the chain, got "
        f"{[decision.tier, *decision.fallbacks]}")
    return request


# ===========================================================================
# §9 the policy is typed, bounded, and persisted
# ===========================================================================


class TestFallbackPolicy:
    def test_the_default_policy_funds_one_paid_fallback_and_no_unknown(self):
        """The default is the claim: six tiers are not six paid POSTs."""
        policy = routing.DEFAULT_FALLBACK_POLICY
        assert policy.max_paid_attempts == 2, (
            "one paid fallback on a proven-safe failure, not five")
        assert policy.allow_after_unknown is False
        assert policy.additional_budget is None
        assert set(policy.safe_failure_classes) == {
            FailureClass.KNOWN_REJECTION, FailureClass.RATE_LIMITED,
            FailureClass.CONNECT_FAILURE, FailureClass.PROVIDER_OUTAGE,
            # A local runner costs nothing, so its failure is money-safe; the
            # separate question of reaching a PAID leg is allow_local_to_remote.
            FailureClass.LOCAL_UNAVAILABLE}

    def test_allowing_a_second_charge_after_an_unknown_names_an_approver(self):
        with pytest.raises(ValueError, match="requires approver"):
            FallbackPolicy(allow_after_unknown=True)

    def test_a_failure_class_that_does_not_prove_safety_cannot_be_listed_safe(self):
        with pytest.raises(ValueError, match="do not prove anything"):
            FallbackPolicy(safe_failure_classes=(FailureClass.READ_TIMEOUT,))

    def test_an_unknown_failure_class_is_a_typed_error(self):
        with pytest.raises(ValueError, match="unknown failure class"):
            FallbackPolicy(safe_failure_classes=("MOSTLY_FINE",))

    def test_a_workspace_cannot_grant_itself_money_authority(self):
        settings = {"intelligence": {"fallback_policy": {
            "allow_after_unknown": True}}}
        with pytest.raises(routing.LLMRoutingError, match="not usable"):
            routing.resolve_fallback_policy(settings)

    def test_an_unknown_policy_field_is_refused_rather_than_ignored(self):
        settings = {"intelligence": {"fallback_policy": {"max_tries": 9}}}
        with pytest.raises(routing.LLMRoutingError, match="unknown fallback_policy"):
            routing.resolve_fallback_policy(settings)

    def test_a_workspace_policy_tightens_the_chain(self):
        settings = {"intelligence": {"fallback_policy": {"max_paid_attempts": 1}}}
        policy = routing.resolve_fallback_policy(settings)
        assert policy.max_paid_attempts == 1
        assert routing.resolve_fallback_policy(None) is routing.DEFAULT_FALLBACK_POLICY

    def test_the_effective_decision_is_persisted_with_the_chain(self, wire, ledger):
        """An operator can answer "what authorised this second request?" later."""
        wire.always(_rejected())
        router = ModelRouter()
        policy = FallbackPolicy(max_paid_attempts=2, additional_budget=0.5,
                                approver="ops:w158", authority="incident-42")
        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=_req(), policy=policy)

        records = router.chain_log("ws-w158")
        assert len(records) == 1
        record = records[0]
        assert isinstance(record, dict)
        assert record["policy"]["approver"] == "ops:w158"
        assert record["policy"]["authority"] == "incident-42"
        assert record["policy"]["additional_budget"] == 0.5
        assert record["paid_legs"] == 2
        assert record["stop_reason"]
        assert [leg["target"] for leg in record["legs"]][:2] == ["remote", "remote"]
        assert any(leg["outcome"] == FailureClass.KNOWN_REJECTION
                   for leg in record["legs"])
        assert ChainRecord(**{k: v for k, v in record.items()
                              if k in ChainRecord.__dataclass_fields__})

    def test_the_chain_log_is_workspace_isolated(self, wire, ledger):
        wire.always(_rejected())
        router = ModelRouter()
        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=_req(workspace_id="ws-a"))
        assert router.chain_log("ws-b") == []
        assert router.chain_log("ws-a")


# ===========================================================================
# §2 a six-tier chain must not become six paid POSTs
# ===========================================================================


class TestPaidTierMultiplication:
    def test_a_six_tier_chain_makes_at_most_two_paid_posts_by_default(
            self, wire, ledger):
        """(a) The whole point, counted at the wire.

        Every remote leg is refused with a 4xx, which proves nothing was
        billed, so the chain keeps falling through -- and still stops buying
        generations after the cap. Before Work 15.8 the bare ``except
        Exception`` walked all six.
        """
        wire.always(_rejected())
        router = ModelRouter()
        request = _six_tier_chain(router, _req())

        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=request)

        assert len(wire.posts()) == 2, wire.calls
        assert routing.DEFAULT_FALLBACK_POLICY.max_paid_attempts == 2
        record = router.chain_log("ws-w158")[0]
        assert record["paid_legs"] == 2
        assert sum(1 for leg in record["legs"]
                   if leg["outcome"] == "refused") >= 2

    def test_a_read_timeout_stops_the_chain_after_one_post(self, wire, ledger):
        """(b) Delivered, response lost: the money may be gone."""
        import httpx

        wire.always(httpx.ReadTimeout("lost"))
        router = ModelRouter()
        _six_tier_chain(router, _req())

        with pytest.raises(AmbiguousLegStopped) as caught:
            router.complete("sys", "user", request=_req())

        assert len(wire.posts()) == 1, wire.calls
        assert caught.value.failure_class is FailureClass.READ_TIMEOUT
        assert caught.value.may_incur_second_charge is True
        assert router.chain_log("ws-w158")[0]["stop_reason"] == (
            "unsafe_failure:READ_TIMEOUT")

    def test_a_write_timeout_stops_the_chain_after_one_post(self, wire, ledger):
        """(c) A partial write is the same money fact as a lost read."""
        import httpx

        wire.always(httpx.WriteTimeout("write stalled"))
        router = ModelRouter()
        _six_tier_chain(router, _req())

        with pytest.raises(AmbiguousLegStopped) as caught:
            router.complete("sys", "user", request=_req())

        assert len(wire.posts()) == 1, wire.calls
        assert caught.value.failure_class is FailureClass.WRITE_TIMEOUT

    @pytest.mark.parametrize("failure", [
        pytest.param("read_timeout", id="read-timeout"),
        pytest.param("write_timeout", id="write-timeout"),
        pytest.param("pool_timeout", id="pool-timeout"),
        pytest.param("dropped", id="dropped-connection"),
        pytest.param("server_error", id="http-500"),
        pytest.param("billed_unusable", id="billed-but-unusable-2xx"),
        pytest.param("unclassified", id="unclassified-exception"),
    ])
    def test_every_unproven_failure_stops_the_chain(self, wire, ledger, failure):
        """The unsafe set, one case at a time. One POST, always."""
        import httpx

        scripted = {
            "read_timeout": httpx.ReadTimeout("lost"),
            "write_timeout": httpx.WriteTimeout("stalled"),
            "pool_timeout": httpx.PoolTimeout("no connection available"),
            "dropped": httpx.RemoteProtocolError("server disconnected"),
            "server_error": _server_error(500),
            "billed_unusable": _no_choices(),
            "unclassified": RuntimeError("who knows"),
        }
        wire.always(scripted[failure])
        router = ModelRouter()
        _six_tier_chain(router, _req())

        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=_req())

        assert len(wire.posts()) == 1, f"{failure}: {wire.calls}"

    def test_a_four_xx_permits_the_configured_fallback(self, wire, ledger):
        """(d) Intentional fallback survives, and is still bounded."""
        wire.always(_rejected())
        router = ModelRouter()
        _six_tier_chain(router, _req())
        policy = FallbackPolicy(max_attempts=6, max_paid_attempts=4,
                                additional_budget=1.0, approver="ops:w158")

        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=_req(), policy=policy)

        assert len(wire.posts()) == 4, wire.calls
        fell = [leg for leg in router.chain_log("ws-w158")[0]["legs"]
                if leg["fell_through"] and leg["paid"]]
        assert len(fell) == 4
        assert all(leg["outcome"] == str(FailureClass.KNOWN_REJECTION)
                   for leg in fell)

    @pytest.mark.parametrize("failure", [
        pytest.param("outage", id="gateway-503"),
        pytest.param("connect", id="connect-error"),
        pytest.param("refused", id="connect-refused"),
    ])
    def test_an_outage_or_connect_failure_still_falls_through(self, wire, ledger,
                                                              failure):
        """The three classes that must NOT stop the chain."""
        import httpx

        if failure == "outage":
            first = _server_error(503)
        elif failure == "connect":
            first = httpx.ConnectError("name resolution failed")
        else:
            first = ConnectionRefusedError("connection refused")
        wire.then(first, _ok("the fallback served it"))
        router = ModelRouter()

        result = router.complete("sys", "user", request=_req())

        assert result.text == "the fallback served it"
        assert len(wire.posts()) == 2, wire.calls

    def test_an_explicit_approved_paid_fallback_after_ambiguity_works(
            self, wire, ledger):
        """(e) The override exists, it is typed, and it is attributed."""
        import httpx

        wire.then(httpx.ReadTimeout("lost"), _ok("the answer we paid twice for"))
        router = ModelRouter()
        policy = FallbackPolicy(
            allow_after_unknown=True,
            approver="ops:w158",
            authority="incident-42: gateway kept dropping the response",
            note="tolerate one duplicate completion",
        )

        result = router.complete("sys", "user", request=_req(), policy=policy)

        assert result.text == "the answer we paid twice for"
        assert len(wire.posts()) == 2, wire.calls
        record = router.chain_log("ws-w158")[0]
        assert record["policy"]["allow_after_unknown"] is True
        assert record["policy"]["approver"] == "ops:w158"
        assert record["policy"]["authority"].startswith("incident-42")
        first_leg = record["legs"][0]
        assert first_leg["outcome"] == str(FailureClass.READ_TIMEOUT)
        assert first_leg["fell_through"] is True
        # The wasted first completion is still an unknown exposure, not $0.
        assert first_leg["estimated_usd"] > 0

    def test_the_default_policy_refuses_the_same_second_charge(self, wire, ledger):
        """The override is what makes (e) possible -- nothing else is."""
        import httpx

        wire.always(httpx.ReadTimeout("lost"))
        router = ModelRouter()
        with pytest.raises(AmbiguousLegStopped):
            router.complete("sys", "user", request=_req())
        assert len(wire.posts()) == 1, wire.calls

    def test_a_repeated_4xx_walks_the_chain_once_per_tier_not_once_per_candidate(
            self, wire, ledger, monkeypatch):
        """One leg is one POST even when the provider's own candidates are many.

        The audit's complaint about the 4xx case was latency, not a second
        charge: ``max_attempts`` is what bounds it now.
        """
        from app.core.config import settings as cfg

        monkeypatch.setattr(cfg, "llm_fallback_model", "w158-fallback-model")
        wire.always(_rejected())
        router = ModelRouter()
        policy = FallbackPolicy(max_attempts=2, max_paid_attempts=2)

        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=_req(), json_mode=True,
                            policy=policy)

        # 2 legs x 4 candidates (a fallback model and json_mode each double the
        # list) = 8 POSTs, all of them rejections that billed nothing, and the
        # chain still stops at the attempt cap instead of walking six tiers.
        assert len(wire.posts()) == 8, wire.calls
        assert router.chain_log("ws-w158")[0]["stop_reason"] == "attempt_cap_reached"


# ===========================================================================
# §3 "local" must never reach a gateway
# ===========================================================================


class TestExecutionTarget:
    def test_a_local_tier_resolves_to_no_gateway_identifier(self):
        """The literal ``"local"`` is gone from the resolver entirely."""
        registry = ModelCapabilityRegistry()
        router = ModelRouter(registry)
        model, source = router._resolve_model(
            "LOCAL_ONLY", routing.get_intelligence_settings(None))
        assert model == ""
        assert source == "NO_LOCAL_PROVIDER"
        assert "local" not in model

    def test_the_router_refuses_to_send_the_word_local_to_a_gateway(self):
        """A direct assertion of the invariant, independent of any chain."""
        router = ModelRouter()
        for word in sorted(routing.NON_PROVIDER_MODEL_IDENTIFIERS):
            if not word:
                continue
            with pytest.raises(UnresolvedModel, match="routing vocabulary"):
                router._assert_remote_model("FAST", word, "WORKSPACE_OVERRIDE")

    def test_no_post_body_ever_carries_a_non_provider_identifier(
            self, wire, ledger):
        """(f) The invariant, measured at the wire across a whole chain.

        The reachable leak was the last-resort local tiers at the end of every
        chain: they resolved to ``"local"``, which is truthy, so
        ``model or None`` kept it and the gateway was POSTed
        ``{"model": "local"}``.
        """
        wire.always(_rejected())
        router = ModelRouter()
        _six_tier_chain(router, _req())
        with pytest.raises(routing.LLMRoutingError):
            router.complete("sys", "user", request=_req())

        assert wire.posts(), "this test needs the chain to have called out"
        for sent in wire.models():
            assert sent.strip().lower() not in routing.NON_PROVIDER_MODEL_IDENTIFIERS, (
                f"a non-provider identifier reached the gateway: {sent!r}")

    def test_a_workspace_override_naming_a_remote_model_for_a_local_tier_is_refused(
            self):
        """``models.local_only = "<remote>"`` used to be returned unexamined."""
        ws = {"intelligence": {"models": {"local_only": "some-remote-model"}}}
        router = ModelRouter()
        with pytest.raises(LocalExecutionRefused, match="not a registered local"):
            router.route(_req(tier="LOCAL_ONLY", workspace_settings=ws))
        # It is a PrivacyRefusal on purpose: this is a privacy promise being
        # broken, and the API already maps that to 403.
        assert issubclass(LocalExecutionRefused, PrivacyRefusal)

    def test_a_registered_local_model_runs_without_touching_a_gateway(self, wire):
        calls: list[tuple[str, str]] = []
        registry = ModelCapabilityRegistry()
        registry.register_local_model(
            "w158-local-llm",
            lambda system, user: calls.append((system, user)) or "local answer")
        router = ModelRouter(registry)
        ws = {"intelligence": {"models": {"local_only": "w158-local-llm"}}}

        result = router.complete(
            "sys", "hi", request=_req(tier="LOCAL_ONLY", workspace_settings=ws))

        assert result == "local answer"
        assert calls == [("sys", "hi")]
        assert wire.posts() == [], "a local leg must not open a gateway request"
        record = router.chain_log("ws-w158")[0]
        assert record["legs"][0]["target"] == "local"
        assert record["paid_legs"] == 0

    def test_an_unavailable_local_model_never_becomes_a_paid_remote_call(
            self, wire, ledger):
        """A LOCAL tier first in the chain, with no local provider behind it."""
        wire.always(_ok())
        router = ModelRouter()

        with pytest.raises(routing.LLMRoutingError, match="all routed models"):
            router.complete("sys", "hi", request=_req(tier="LOCAL_ONLY"))

        assert wire.posts() == [], (
            "an unavailable local model turned into a paid remote call: "
            f"{wire.calls}")
        record = router.chain_log("ws-w158")[0]
        assert record["legs"][0]["outcome"] == str(FailureClass.LOCAL_UNAVAILABLE)
        assert any("may not become a paid remote call" in leg["reason"]
                   for leg in record["legs"])

    def test_that_fallback_is_available_per_policy(self, wire, ledger):
        """The same case, with a policy that explicitly buys the remote leg."""
        wire.always(_ok("the paid answer"))
        router = ModelRouter()
        policy = FallbackPolicy(max_paid_attempts=2, additional_budget=1.0,
                                allow_local_to_remote=True, approver="ops:w158")

        result = router.complete("sys", "hi", request=_req(tier="LOCAL_ONLY"),
                                 policy=policy)

        assert result.text == "the paid answer"
        assert len(wire.posts()) == 1, wire.calls
        assert router.chain_log("ws-w158")[0]["policy"]["approver"] == "ops:w158"

    def test_a_contradictory_target_is_refused(self):
        router = ModelRouter()
        with pytest.raises(PrivacyRefusal, match="contradictory"):
            router.route(_req(tier="LOCAL_ONLY", target=ExecutionTarget.REMOTE))
        with pytest.raises(PrivacyRefusal, match="contradictory"):
            router.route(_req(tier="FAST", target=ExecutionTarget.LOCAL,
                              require_remote=True))

    def test_a_remote_target_under_a_local_only_workspace_is_refused(self):
        router = ModelRouter()
        ws = {"intelligence": {"privacy_mode": "local_only"}}
        with pytest.raises(PrivacyRefusal, match="REMOTE execution target"):
            router.route(_req(target=ExecutionTarget.REMOTE, workspace_settings=ws))

    def test_the_decision_carries_the_target_and_its_source(self):
        router = ModelRouter()
        remote = router.route(_req())
        assert remote.target is ExecutionTarget.REMOTE
        assert remote.model_source
        assert remote.relative_cost > 0
        local = router.route(_req(tier="PRIVATE"))
        assert local.target is ExecutionTarget.LOCAL
        assert local.resolved is False

    def test_a_registry_entry_marked_remote_on_a_local_tier_still_runs_locally(self):
        """Variant 1 from the audit: ``remote=True`` on a LOCAL tier.

        Such an entry is possible via a workspace capability override, and it
        used to pass the remote gate and POST ``"local"`` at a gateway.
        """
        caps = routing._default_capabilities()
        for cap in caps:
            if cap.tier == "LOCAL_ONLY":
                cap.remote = True
                cap.provider = routing.REMOTE_SLOT
        registry = ModelCapabilityRegistry(caps)
        router = ModelRouter(registry)
        ws = {"intelligence": {"provider_preference": "LOCAL_ONLY"}}

        decision = router.route(_req(workspace_settings=ws))

        # The entry claims remote, so it IS treated as remote -- but then the
        # model name is checked, and "local" is not a provider identifier.
        assert decision.tier == "LOCAL_ONLY"
        assert decision.target is ExecutionTarget.REMOTE
        with pytest.raises(UnresolvedModel):
            router._assert_remote_model(decision.tier, "local",
                                        decision.model_source)
        with pytest.raises(UnresolvedModel, match="UNRESOLVED|no provider model"):
            router.complete("sys", "hi", request=_req(workspace_settings=ws))


# ===========================================================================
# §3b the cost tier must not be silently discarded
# ===========================================================================


class TestCostTierMismatch:
    def test_an_unresolved_remote_tier_is_refused_not_downgraded(self, wire,
                                                                ledger, monkeypatch):
        """``model=""`` used to become ``gpt-4o-mini`` on a PREMIUM request."""
        from app.core.config import settings as cfg

        monkeypatch.setattr(
            "app.services.provider_settings.effective_llm",
            lambda *a, **k: {"model": "", "tiers": {}})
        monkeypatch.setattr(cfg, "llm_model", "")
        router = ModelRouter()

        decision = router.route(_req(tier="PREMIUM"))
        assert decision.model == ""
        assert decision.model_source == "UNRESOLVED"
        assert decision.resolved is False
        assert decision.relative_cost == 8.0, "PREMIUM is 8.0x baseline"

        with pytest.raises(UnresolvedModel) as caught:
            router.complete("sys", "hi", request=_req(tier="PREMIUM"))

        assert "8.0x baseline" in str(caught.value)
        assert wire.posts() == [], "a refused mismatch must not reach the gateway"

    def test_the_routing_log_records_the_unresolved_mismatch(self, monkeypatch):
        from app.core.config import settings as cfg

        monkeypatch.setattr(
            "app.services.provider_settings.effective_llm",
            lambda *a, **k: {"model": "", "tiers": {}})
        monkeypatch.setattr(cfg, "llm_model", "")
        router = ModelRouter()
        router.route(_req(tier="PREMIUM"))
        entry = router.log("ws-w158")[-1]
        assert entry["model"] == ""
        assert entry["tier"] == "PREMIUM"


# ===========================================================================
# §4 every billable leg is budgeted before it is sent
# ===========================================================================


class TestLLMBudgetContract:
    def test_the_leg_runs_estimate_reserve_attempt_call_reconcile(self, wire,
                                                                   ledger):
        """The order, recorded by two independent observers.

        ``wire`` sees the request, ``ledger`` sees the money, and the claim is
        that the money comes first and is written once.
        """
        wire.always(_ok())
        assert llm_mod.llm_available()

        result = llm_mod.complete("sys", "a question", workspace_id="ws-w158")

        assert result.text == "a finished answer"
        assert len(wire.posts()) == 1
        assert ledger["events"] == ["authorize", _settle_event(SETTLED_USD)], (
            f"budget order wrong: {ledger['events']}")

    def test_the_reservation_is_authorised_with_a_category_and_in_reserving_mode(
            self, wire, ledger):
        wire.always(_ok())
        llm_mod.complete("sys", "a question", workspace_id="ws-w158")
        assert len(ledger["authorizations"]) == 1
        auth = ledger["authorizations"][0]
        assert auth["reserve"] is True, (
            "a non-reserving check cannot exclude a concurrent spender")
        assert auth["category"] == "llm"
        assert auth["workspace_id"] == "ws-w158"
        assert auth["estimated_usd"] > 0

    def test_the_budget_gate_refuses_before_the_request(self, wire, ledger,
                                                        monkeypatch):
        """(g) A refusal that never reaches the wire.

        Mutation-proofed: ``PaidProviderExecutor.check_budget`` short-circuits
        to a debug log when ``submit_budget`` is ``None``, which is exactly the
        pre-Work-15.8 state -- and then this POST happens and this test fails.
        """
        def refuse(*_a, **_k):
            raise cost_mod.BudgetExceededError("daily budget exhausted")

        monkeypatch.setattr(cost_mod, "assert_can_spend", refuse)
        wire.always(_ok())

        with pytest.raises(llm_mod.LLMCompletionError) as caught:
            llm_mod.complete("sys", "a question", workspace_id="ws-w158")

        assert wire.posts() == [], (
            f"the request left before the gate refused: {wire.calls}")
        assert caught.value.kind == "BUDGET_REFUSED"
        assert caught.value.outcome is LegOutcome.CANCELLED, (
            "nothing was sent, which is the same money fact as a cancellation")

    def test_a_refused_leg_stops_the_chain_without_claiming_an_exposure(
            self, wire, ledger, monkeypatch):
        """The chain must not dress a budget refusal up as an ambiguous charge."""
        def refuse(*_a, **_k):
            raise cost_mod.BudgetExceededError("daily budget exhausted")

        monkeypatch.setattr(cost_mod, "assert_can_spend", refuse)
        wire.always(_ok())
        router = ModelRouter()

        with pytest.raises(routing.LLMRoutingError) as caught:
            router.complete("sys", "hi", request=_req())

        assert wire.posts() == []
        assert not isinstance(caught.value, AmbiguousLegStopped)
        assert "budget gate refused this leg, so nothing was sent" in str(caught.value)
        assert "BUDGET_REFUSED" in str(caught.value)
        assert router.chain_log("ws-w158")[0]["stop_reason"] == "budget_refused"

    def test_a_rejection_voids_the_reservation_and_writes_no_second_row(
            self, wire, ledger):
        wire.always(_rejected())
        with pytest.raises(llm_mod.LLMCompletionError) as caught:
            llm_mod.complete("sys", "a question", workspace_id="ws-w158")
        assert caught.value.kind == "EXHAUSTED_ON_PROVEN_SAFE_FAILURES"
        assert ledger["events"] == ["authorize", "void"], ledger["events"]

    def test_a_success_settles_the_reservation_instead_of_writing_a_second_row(
            self, wire, ledger):
        """One completion, one ledger row: settle, never settle + track."""
        wire.always(_ok())
        result = llm_mod.complete("sys", "a question", workspace_id="ws-w158")
        assert result.text == "a finished answer"
        assert ledger["events"] == ["authorize", _settle_event(SETTLED_USD)], (
            ledger["events"])
        assert not any(e.startswith("track:") for e in ledger["events"])

    def test_an_unknown_exposure_keeps_its_reservation_and_is_alarmed(self, wire,
                                                                     ledger,
                                                                     paid_log):
        """The money may be gone: the row stays, and the incident is loud."""
        import httpx

        wire.always(httpx.ReadTimeout("lost"))
        with pytest.raises(llm_mod.LLMCompletionError) as caught:
            llm_mod.complete("sys", "a question", workspace_id="ws-w158")

        assert caught.value.outcome is LegOutcome.SUBMISSION_UNKNOWN
        assert ledger["events"] == ["authorize"], ledger["events"]
        errors = [e for e in paid_log["events"] if e["level"] == "error"]
        assert errors and "UNKNOWN" in errors[0]["message"]

    def test_the_ambiguous_record_does_not_tell_an_operator_to_go_look_it_up(
            self, wire, ledger):
        """"RECONCILE" would mean fetch the job by its id. There is no id."""
        import httpx

        wire.always(httpx.ReadTimeout("lost"))
        with pytest.raises(llm_mod.LLMCompletionError) as caught:
            llm_mod.complete("sys", "a question", workspace_id="ws-w158")

        record = caught.value.submission
        assert record.reconciliation is Reconciliation.MANUAL_OVERRIDE, (
            "a chat completion cannot be reconciled; the only true remedy is "
            "an explicit operator decision about the work")
        assert record.reconciliation is not Reconciliation.RECONCILE
        assert verdict_for(record) is RetryVerdict.RECONCILE
        assert record.may_resubmit is False
        assert record.state is SubmissionState.SUBMISSION_UNKNOWN

    def test_a_cancelled_leg_is_known_safe_and_bills_nothing(self, wire, ledger):
        wire.always(_ok())
        with pytest.raises(llm_paid.LLMCompletionCancelled) as caught:
            llm_paid.run_completion(
                transport=wire.post, url=CHAT_URL, api_key="k",
                candidates=llm_paid.build_candidates("m", {"messages": []}),
                workspace_id="ws-w158",
                should_cancel=lambda: True)
        assert wire.posts() == []
        assert caught.value.outcome is LegOutcome.CANCELLED
        assert ledger["events"] == ["authorize", "void"], ledger["events"]

    def test_a_cancellation_is_not_silently_treated_as_a_safe_retry(self, wire):
        """A cancelled leg must stop, not quietly become candidate two."""
        wire.always(_ok())
        candidates = llm_paid.build_candidates(
            "m1", {"messages": []}, fallback_model="m2")
        with pytest.raises(llm_paid.LLMCompletionCancelled):
            llm_paid.run_completion(
                transport=wire.post, url=CHAT_URL, api_key="k",
                candidates=candidates, workspace_id="ws-w158",
                should_cancel=lambda: True)
        assert wire.posts() == []

    def test_an_empty_workspace_id_is_REFUSED_before_the_request(self, wire,
                                                              ledger):
        """Work 15.9 closed the escape hatch this test used to bless.

        It previously asserted that an empty workspace ran "unenforced" and fell
        back to a plain booking row. That is a billable POST with no budget owner
        and no reservation -- spend nobody is accountable for. A billable leg
        with no owner is now refused BEFORE the request leaves.

        ``wire.calls == []`` is the assertion that matters: the money must not
        move at all.
        """
        from app.services.paid_provider import OwnerlessSpendRefused

        wire.always(_ok())
        with pytest.raises(llm_mod.LLMCompletionError) as caught:
            llm_mod.complete("sys", "a question", workspace_id="")
        assert "no budget owner" in str(caught.value) or \
            "workspace_id is empty" in str(caught.value), caught.value
        assert wire.calls == [], (
            "an ownerless billable request reached the provider")
        assert not any(e == "authorize" for e in ledger["events"])

        authorization = llm_paid.SpendAuthorization(workspace_id="",
                                                    amount_usd=0.5)
        with pytest.raises(OwnerlessSpendRefused):
            authorization.authorize()
        assert authorization.reservation is None

    def test_a_ledger_outage_is_not_a_budget_verdict(self, wire, ledger,
                                                      monkeypatch):
        """A broken ledger must not read as "there is money"."""
        def explode(*_a, **_k):
            raise RuntimeError("no such table: cost_entries")

        monkeypatch.setattr(cost_mod, "assert_can_spend", explode)
        wire.always(_ok())
        authorization = llm_paid.SpendAuthorization(workspace_id="ws-w158",
                                                    amount_usd=0.25)
        authorization.authorize()
        assert authorization.enforced is False
        assert authorization.reservation is None


# ===========================================================================
# §5 the DecisionEngine's router integration was a silent no-op
# ===========================================================================


class TestDecisionProviderRoutes:
    def test_the_router_is_constructed_with_a_registry_not_a_workspace_id(self):
        """The defect: ``ModelRouter(self.workspace_id)`` put a str in
        ``self.registry``, ``route()`` raised ``AttributeError`` and the bare
        ``except`` returned ``""``. So this must return a real decision."""
        provider = LLMDecisionProvider(workspace_id="ws-w158")
        decision = provider._router_decision()
        assert decision is not None
        assert decision.tier in routing.TIERS
        assert decision.target is ExecutionTarget.REMOTE
        assert "tier=" in provider.route_detail

    def test_the_routers_choice_is_honoured_end_to_end(self, wire, monkeypatch):
        """The proof that routing happens: the model in the POST body is the
        router's pick, and NOT ``llm.complete``'s own default."""
        wire.always(_ok('{"index": 0, "reason": "first"}'))
        monkeypatch.setattr(
            "app.services.provider_settings.effective_llm",
            lambda *a, **k: {"model": "router-picked-model", "tiers": {}})
        provider = LLMDecisionProvider(workspace_id="ws-w158")

        result = provider.run("choose", {"a": 1, "b": 2})

        assert result.model == "router-picked-model"
        assert wire.models() == ["router-picked-model"], wire.calls
        assert "gpt-4o-mini" not in wire.models()

    def test_a_local_route_never_becomes_a_gateway_call(self, wire, monkeypatch):
        """A LOCAL target yields a refusal, not a paid remote call."""
        wire.always(_ok('{"index": 0, "reason": "first"}'))
        monkeypatch.setattr(
            "app.engine.intelligence.providers.llm_provider."
            "LLMDecisionProvider._router_decision",
            lambda self: routing.RoutingDecision(
                tier="PRIVATE", model="", remote=False, reason="pinned",
                target=ExecutionTarget.LOCAL, model_source="NO_LOCAL_PROVIDER"))
        provider = LLMDecisionProvider(workspace_id="ws-w158")

        with pytest.raises(ProviderUnavailable, match="local target"):
            provider.run("choose", {"a": 1})

        assert wire.posts() == [], wire.calls

    def test_an_unresolved_route_is_refused_rather_than_defaulted(self, wire,
                                                                  monkeypatch):
        from app.core.config import settings as cfg

        wire.always(_ok('{"index": 0, "reason": "first"}'))
        monkeypatch.setattr(
            "app.services.provider_settings.effective_llm",
            lambda *a, **k: {"model": "", "tiers": {}})
        monkeypatch.setattr(cfg, "llm_model", "")
        provider = LLMDecisionProvider(workspace_id="ws-w158")

        with pytest.raises(ProviderUnavailable, match="resolved no provider model"):
            provider.run("choose", {"a": 1})

        assert wire.posts() == [], wire.calls

    def test_an_explicit_model_argument_still_wins(self, wire, monkeypatch):
        wire.always(_ok('{"value": true, "reason": "yes"}'))
        monkeypatch.setattr(
            "app.services.provider_settings.effective_llm",
            lambda *a, **k: {"model": "router-picked-model", "tiers": {}})
        provider = LLMDecisionProvider(model="explicit-model",
                                       workspace_id="ws-w158")
        result = provider.run("boolean", {"q": "yes"})
        assert result.model == "explicit-model"
        assert wire.models() == ["explicit-model"]


# ===========================================================================
# the failure taxonomy itself
# ===========================================================================


class TestFailureTaxonomy:
    def test_the_classifier_reads_the_innermost_cause(self):
        """A wrapped read timeout must not be mistaken for a bare LLMError."""
        import httpx

        from app.providers import llm as provider_mod

        wrapped = provider_mod.LLMCompletionError(detail="x", kind="AMBIGUOUS")
        wrapped.__cause__ = httpx.ReadTimeout("lost")
        assert routing.classify_leg_failure(wrapped) is FailureClass.READ_TIMEOUT

    def test_a_bare_llm_error_is_a_pre_flight_refusal(self):
        """``llm.complete`` raises it before anything is sent."""
        from app.providers import llm as provider_mod

        assert routing.classify_leg_failure(
            provider_mod.LLMError("provider not configured")
        ) is FailureClass.PROVIDER_OUTAGE

    def test_the_safe_and_unsafe_sets_are_disjoint_and_cover_the_audit(self):
        assert not (routing.SAFE_FAILURE_CLASSES
                    & routing.MAY_HAVE_BEEN_BILLED)
        for unsafe in (FailureClass.READ_TIMEOUT, FailureClass.WRITE_TIMEOUT,
                       FailureClass.POOL_TIMEOUT, FailureClass.DROPPED_CONNECTION,
                       FailureClass.SERVER_ERROR, FailureClass.BILLED_UNUSABLE):
            assert unsafe not in routing.SAFE_FAILURE_CLASSES
            assert unsafe.may_have_been_billed
        for safe in (FailureClass.KNOWN_REJECTION, FailureClass.RATE_LIMITED,
                     FailureClass.CONNECT_FAILURE, FailureClass.PROVIDER_OUTAGE):
            assert safe.proven_safe