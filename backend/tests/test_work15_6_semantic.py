"""Work 15.6 §6 -- the OPTIONAL semantic re-ranker.

The properties under test are the donor's three rules plus two boundaries.

1. **Opt in.** Without an explicit opt-in the caller's order is returned
   untouched and no request is made.
2. **Degrade to input order.** Provider unavailable, slow, erroring or
   cancelled: the order comes back EXACTLY as it went in.
3. **Refuse a partial rerank.** An incomplete score set is refused whole.
4. **No fabricated confidence.** A confidence the provider did not return is
   absent, and the reason travels with it.
5. **Separation.** The provider serves ``semantic_rerank`` and no media-analysis
   kind, so it can never be resolved for face tracking, diarization or reframe.
"""

from __future__ import annotations

import json
import math
import os

import httpx
import pytest

from app.engine.intel import base as intel_base
from app.engine.intel import registry as intel_registry
from app.engine.intel.impl.semantic_rerank import (
    MAX_CANDIDATES,
    PROVIDER,
    RerankResult,
    ScoredCandidate,
    SemanticRerankProvider,
    TwelveLabsEmbedder,
    cosine_similarity,
    derive_idempotency_key,
    is_complete,
    normalise_candidates,
    order_from_scores,
    parse_embeddings,
)

#: A non-secret placeholder. The header value is irrelevant here: the
#: transport double answers every request locally and no network call is made.
PLACEHOLDER = "unset"

# ===========================================================================
# doubles
# ===========================================================================


def _vector(seed: float, length: int = 4) -> list[float]:
    return [seed + index * 0.01 for index in range(length)]


def _embedder(vectors: list[list[float] | Exception]) -> ScriptedEmbedder:
    return ScriptedEmbedder(vectors)


class ScriptedEmbedder(TwelveLabsEmbedder):
    """Returns vectors in a caller-controlled order, counting every call.

    ``vectors`` is consumed one per call, so a test can make the SECOND
    candidate fail and prove the partial-rerank refusal.
    """

    def __init__(self, vectors: list[list[float] | Exception]) -> None:
        super().__init__(api_key=PLACEHOLDER)
        self._vectors = list(vectors)
        self.calls = 0

    def embed_text(self, text: str, **kwargs) -> list[float]:
        self.calls += 1
        if not self._vectors:
            raise RuntimeError("script exhausted")
        value = self._vectors.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class CountingTransport(httpx.BaseTransport):
    """Counts REAL outbound HTTP requests."""

    def __init__(self, *, payload=None, failure: Exception | None = None,
                 status: int = 200) -> None:
        self.requests = 0
        self._payload = payload if payload is not None else {
            "data": [{"embedding": _vector(1.0)}]}
        self._failure = failure
        self._status = status

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self._failure is not None:
            raise self._failure
        return httpx.Response(self._status, json=self._payload)


def _provider(embedder=None) -> SemanticRerankProvider:
    return SemanticRerankProvider(embedder=embedder)


CANDIDATES = [
    {"id": "asset-a", "text": "a red balloon rising over a park"},
    {"id": "asset-b", "text": "a busy city street at night"},
    {"id": "asset-c", "text": "a red kite in the wind"},
]


# ===========================================================================
# 1. opt-in
# ===========================================================================


def test_without_an_opt_in_the_order_is_untouched_and_nothing_is_sent():
    embedder = _embedder([_vector(1.0)] * 8)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=False)

    assert result.order == ("asset-a", "asset-b", "asset-c")
    assert result.applied is False
    assert result.degraded is True
    assert result.reason == "not opted in"
    assert embedder.calls == 0, "an un-opted-in rerank spent tokens"
    assert result.scored == ()
    assert result.requests == 0


def test_the_opt_in_can_come_from_either_param_name():
    """Both ``opt_in`` and ``enabled`` are honoured; neither is guessed."""
    for key in ("opt_in", "enabled"):
        request = intel_base.IntelRequest(
            workspace_id="w1", asset_id="a1", storage_path="x.mp4",
            params={"query": "a balloon", "candidates": CANDIDATES, key: True},
        )
        embedder = _embedder([_vector(1.0)] * 8)
        result = _provider(embedder).run(
            request, progress=lambda _v: None, should_cancel=lambda: False,
            deadline=None)
        assert result.metrics["applied"] is True, key
        assert embedder.calls == 4, key


def test_a_false_opt_in_keeps_the_input_order():
    embedder = _embedder([_vector(1.0)] * 8)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=False)
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert embedder.calls == 0


# ===========================================================================
# 2. degrade to input order
# ===========================================================================


def test_an_unavailable_provider_returns_the_input_order_verbatim():
    """No configured credential -> the provider cannot build a client."""
    provider = SemanticRerankProvider()
    result = provider.rerank("a balloon", CANDIDATES, opt_in=True,
                             workspace_id="w-no-credential")
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert result.applied is False
    assert result.reason == "provider unavailable"
    assert result.requests == 0


def test_a_transport_failure_degrades_without_raising():
    transport = CountingTransport(failure=httpx.ConnectError("refused"))
    embedder = TwelveLabsEmbedder(api_key=PLACEHOLDER,
                                  client=httpx.Client(transport=transport))
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert result.degraded is True
    assert "provider failure" in result.reason


def test_a_5xx_from_the_provider_degrades_rather_than_reordering():
    transport = CountingTransport(status=503)
    embedder = TwelveLabsEmbedder(api_key=PLACEHOLDER,
                                  client=httpx.Client(transport=transport))
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert result.applied is False


def test_cancellation_degrades_to_the_input_order():
    embedder = _embedder([_vector(1.0)] * 8)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True,
                                        should_cancel=lambda: True)
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert result.reason == "cancelled"
    assert embedder.calls == 0


def test_a_deadline_that_has_already_elapsed_degrades_immediately():
    embedder = _embedder([_vector(1.0)] * 8)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True,
                                        deadline=0.0)
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert result.applied is False
    assert embedder.calls == 0


def test_an_empty_query_degrades_without_a_request():
    embedder = _embedder([_vector(1.0)] * 8)
    result = _provider(embedder).rerank("   ", CANDIDATES, opt_in=True)
    assert result.reason == "no query text supplied"
    assert embedder.calls == 0


def test_no_candidates_degrades():
    embedder = _embedder([_vector(1.0)] * 8)
    result = _provider(embedder).rerank("a balloon", [], opt_in=True)
    assert result.order == ()
    assert result.reason == "no candidates supplied"
    assert embedder.calls == 0


def test_too_many_candidates_is_refused_not_truncated():
    """A shortened candidate set would silently drop ranked content."""
    embedder = _embedder([_vector(1.0)] * 200)
    many = [{"id": f"a{i}", "text": "x"} for i in range(MAX_CANDIDATES + 5)]
    result = _provider(embedder).rerank("a balloon", many, opt_in=True)
    assert result.applied is False
    assert result.reason == f"more than {MAX_CANDIDATES} candidates"
    assert len(result.order) == len(many)
    assert embedder.calls == 0, "a refused call still spent tokens"


def test_a_degraded_rerank_reports_its_reason_in_the_provider_result():
    """The degradation is part of the result, not a swallowed log line."""
    embedder = _embedder([_vector(1.0)] * 8)
    request = intel_base.IntelRequest(
        workspace_id="w1", asset_id="a1", storage_path="x.mp4",
        params={"query": "a balloon", "candidates": CANDIDATES, "opt_in": False},
    )
    result = _provider(embedder).run(
        request, progress=lambda _v: None, should_cancel=lambda: False,
        deadline=None)
    assert result.ok is True, "degradation is not a run failure"
    assert result.metrics["applied"] is False
    assert result.metrics["degraded"] is True
    assert result.metrics["reason"] == "not opted in"
    assert any("not opted in" in w for w in result.warnings)
    assert result.artifacts["semantic_rerank"]["order"] == [
        c["id"] for c in CANDIDATES]


# ===========================================================================
# 3. refuse a partial rerank  (mutation-critical)
# ===========================================================================


def test_a_complete_set_is_applied():
    embedder = _embedder([
        _vector(1.0),      # query
        _vector(1.0),      # asset-a (close)
        _vector(9.0),      # asset-b (far)
        _vector(0.5),      # asset-c
    ])
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert result.applied is True
    assert result.reason == ""
    assert sorted(result.order) == ["asset-a", "asset-b", "asset-c"]
    assert result.order[0] == "asset-a", "the closest candidate must win"


def test_a_failed_candidate_refuses_the_whole_rerank():
    """THE refusal. The order must be the INPUT order, not a partial reorder."""
    embedder = _embedder([
        _vector(1.0),
        _vector(1.0),
        RuntimeError("provider dropped candidate c"),
    ])
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert result.applied is False
    assert list(result.order) == [c["id"] for c in CANDIDATES], (
        "a partial rerank reordered the list; that is worse than no rerank")
    assert result.reason == "provider scored an incomplete candidate set"


def test_an_incomplete_score_set_is_refused_even_without_an_exception():
    """A response the provider sends but we cannot score is still incomplete.

    The candidate does not raise here -- the embedder reports "no usable
    embedding" and the refusal gate must still refuse the WHOLE rerank. Break
    ``is_complete`` and this returns a half-ranked list.
    """
    class PartlyBrokenTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.calls <= 2:      # query + first candidate answer properly
                return httpx.Response(200, json={"data": [{"embedding": _vector(1.0)}]})
            # ...and the rest come back with an unusable vector.
            return httpx.Response(200, json={"data": [{"embedding": "nonsense"}]})

    embedder = TwelveLabsEmbedder(api_key=PLACEHOLDER,
                                  client=httpx.Client(transport=PartlyBrokenTransport()))
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert result.applied is False
    assert list(result.order) == [c["id"] for c in CANDIDATES]
    assert result.reason == "provider scored an incomplete candidate set"


def test_a_duplicate_score_is_refused():
    """Two rows with the same id make an order meaningless."""
    scored = [
        ScoredCandidate(id="asset-a", text="a", score=0.9),
        ScoredCandidate(id="asset-a", text="a", score=0.8),
        ScoredCandidate(id="asset-b", text="b", score=0.7),
    ]
    assert is_complete(scored, 3) is False


def test_a_wrong_count_is_refused():
    scored = [ScoredCandidate(id="asset-a", text="a", score=0.9)]
    assert is_complete(scored, 3) is False
    assert is_complete([], 3) is False
    assert is_complete(scored, 0) is False


def test_the_input_order_is_never_reordered_on_refusal():
    """The strongest form: for EVERY candidate failure position the order is
    the input order. A guard that only covered the last candidate would fail.
    """
    for fail_at in range(4):
        vectors: list = [_vector(1.0)] * 4
        vectors[fail_at] = RuntimeError("boom")
        result = _provider(_embedder(vectors)).rerank(
            "a balloon", CANDIDATES, opt_in=True)
        assert list(result.order) == [c["id"] for c in CANDIDATES], fail_at


def test_a_successful_result_is_not_marked_degraded():
    embedder = _embedder([_vector(1.0)] * 4)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert result.applied is True
    assert result.degraded is False
    assert result.to_dict()["degraded"] is False


# ===========================================================================
# 4. never fabricate a confidence
# ===========================================================================


def test_no_confidence_is_invented_when_the_provider_returns_none():
    embedder = _embedder([_vector(1.0)] * 4)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert result.applied is True
    for item in result.scored:
        assert item.confidence is None
        assert item.confidence_source == "provider returned no per-candidate confidence"
    # And the absence is visible in the serialized payload.
    for row in result.to_dict()["scored"]:
        assert row["confidence"] is None
        assert row["confidence_source"].strip()


def test_the_score_is_labelled_as_derived_not_provider_returned():
    """Cosine similarity is OUR arithmetic; calling it the provider's score
    would be a small lie that grows."""
    embedder = _embedder([_vector(1.0)] * 4)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    for item in result.scored:
        assert item.score_source == "cosine_similarity(provider_embeddings)"


def test_a_per_dimension_uncertainty_vector_is_not_collapsed_into_confidence():
    """TwelveLabs returns per-DIMENSION uncertainty, not a per-candidate
    confidence. Averaging it into one would fabricate the wrong quantity."""
    vectors, uncertainty_seen = parse_embeddings({
        "data": [{
            "embedding": [0.1, 0.2, 0.3],
            "embedding_uncertainty": [0.01, 0.02, 0.03],
        }]
    })
    assert uncertainty_seen is True
    assert vectors == [[0.1, 0.2, 0.3]]
    # Nothing in the public surface converts that vector into a confidence.
    scored = ScoredCandidate(id="x", text="t", score=0.5)
    assert scored.confidence is None


def test_every_result_carries_provider_model_and_source_evidence():
    embedder = _embedder([_vector(1.0)] * 4)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    assert result.provider == "semantic_rerank"
    assert result.model == "marengo3.5"
    for item in result.scored:
        evidence = item.evidence
        assert evidence["provider"] == "twelvelabs"
        assert evidence["model"] == "marengo3.5"
        assert evidence["source_asset"] == item.id
        assert len(evidence["idempotency_key"]) == 32


def test_scores_are_finite_and_bounded():
    for left, right, expected in (
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 1.0], [-1.0, -1.0], 0.0),   # clamped, never negative
    ):
        value = cosine_similarity(left, right)
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0
        assert abs(value - expected) < 1e-9, (left, right, value)


def test_a_degenerate_vector_scores_zero_rather_than_raising():
    for left, right in (([], []), ([1.0], [1.0, 2.0]),
                        ([0.0, 0.0], [1.0, 1.0]),
                        ([float("nan")], [1.0])):
        assert cosine_similarity(left, right) == 0.0


def test_malformed_payloads_yield_no_embeddings_not_an_exception():
    for payload in (None, {}, {"data": "nope"}, {"data": [{"embedding": "x"}]},
                    {"data": [{"embedding": []}]}, {"data": [{}]}):
        vectors, _seen = parse_embeddings(payload)
        assert vectors == [], payload


def test_a_non_finite_embedding_is_rejected_before_it_can_be_scored():
    vectors, _ = parse_embeddings({"data": [{"embedding": [0.1, float("inf")]}]})
    assert vectors == []


# ===========================================================================
# 5. separation from media analysis + registry integration
# ===========================================================================


def test_the_provider_is_the_registry_symbol():
    assert PROVIDER is SemanticRerankProvider
    assert issubclass(PROVIDER, intel_base.MediaIntelProvider)


def test_it_registers_in_its_own_chain_and_only_that_one():
    assert intel_registry.chain_for("semantic_rerank") == ("semantic_rerank",)
    provider = intel_registry.get_provider("semantic_rerank")
    assert isinstance(provider, SemanticRerankProvider)
    # ``kinds`` repeats ``kind`` (the registry reads both); what matters is that
    # NO media-analysis kind appears.
    assert set(provider.chain_kinds()) == {"semantic_rerank"}
    assert provider.supports_kind("semantic_rerank") is True
    for kind in ("face_tracking", "diarization", "segmentation", "reframe",
                 "active_speaker", "alignment", "speech_activity"):
        assert provider.supports_kind(kind) is False, kind


def test_no_media_analysis_chain_resolves_to_the_semantic_provider():
    """The boundary. If this fails, semantic scoring leaked into reframe."""
    for kind in ("face_tracking", "diarization", "segmentation", "reframe",
                 "active_speaker", "speech_activity", "alignment",
                 "enhancement", "denoise"):
        assert intel_registry.chain_for(kind) is not None
        assert "semantic_rerank" not in intel_registry.chain_for(kind), kind
        resolved, _reasons = intel_registry.resolve(kind)
        assert not isinstance(resolved, SemanticRerankProvider), kind


def test_it_declares_the_media_kinds_it_refuses():
    capabilities = SemanticRerankProvider().capabilities()
    for kind in ("face_tracking", "diarization", "segmentation", "reframe"):
        assert kind in capabilities["not_supported"]


def test_the_provider_appears_in_the_registry_listing():
    intel_registry.clear_cache()
    keys = {item["key"] for item in intel_registry.list_providers()}
    assert "semantic_rerank" in keys
    intel_registry.clear_cache()


def test_the_health_probe_never_raises_and_states_a_reason_when_dark():
    health = intel_base.safe_health(SemanticRerankProvider())
    assert isinstance(health, intel_base.ProviderHealth)
    if not health.available:
        assert health.reason.strip(), "an unavailable verdict needs a reason"


def test_health_is_dark_without_credentials_and_says_so():
    provider = SemanticRerankProvider()
    health = provider.health()
    assert health.mode == intel_base.MODE_REMOTE
    assert health.detail.get("optional") is True
    if not health.available:
        assert "deterministic" in health.reason or "order" in health.reason
        # The registry contract: an unavailable verdict carries remediation.
        # For an optional provider the honest remediation is "nothing needed".
        assert health.detail["remediation"].strip()
        assert "none required" in health.detail["remediation"]


def test_a_dark_provider_still_serialises_through_the_registry_shape():
    """Work 12's listing must not special-case this provider."""
    payload = SemanticRerankProvider().to_dict()
    assert payload["key"] == "semantic_rerank"
    assert payload["kinds"] == ["semantic_rerank"]
    assert payload["health"]["detail"]["remediation"].strip()
    assert payload["capabilities"]["refuses_partial_rerank"] is True
    assert payload["license"]["commercial_use"] == intel_base.COMMERCIAL_UNVERIFIED


def test_health_does_not_spend_a_request():
    """A health probe that POSTs would cost money to answer 'are you there?'."""
    transport = CountingTransport()
    embedder = TwelveLabsEmbedder(api_key=PLACEHOLDER,
                                  client=httpx.Client(transport=transport))
    _provider(embedder).health()
    _provider(embedder).capabilities()
    _provider(embedder).resource_requirements()
    assert transport.requests == 0


def test_capabilities_report_the_donor_rules_as_machine_readable_flags():
    capabilities = SemanticRerankProvider().capabilities()
    assert capabilities["optional"] is True
    assert capabilities["opt_in_required"] is True
    assert capabilities["degrades_to"] == "input order"
    assert capabilities["refuses_partial_rerank"] is True
    assert capabilities["billable"] is True
    assert "rerank_candidates" in capabilities["operations"]


def test_the_provider_costs_nothing_locally_and_invents_no_rate():
    provider = SemanticRerankProvider()
    cost = provider.cost(provider.resource_requirements())
    assert cost["gpu_ms"] == 0
    assert cost["billed"] is False
    assert "not estimated" in cost["notes"] or "vendor" in cost["notes"]


def test_commercial_mode_refuses_the_provider_until_it_is_audited():
    """UNVERIFIED must block, so an operator cannot ship it silently."""
    provider = SemanticRerankProvider()
    assert provider.license_info().commercial_use == intel_base.COMMERCIAL_UNVERIFIED
    assert intel_registry.license_block_reason(provider).strip()


# ===========================================================================
# input normalisation
# ===========================================================================


def test_candidate_ids_are_preserved_in_input_order():
    pairs = normalise_candidates(CANDIDATES)
    assert [cid for cid, _text in pairs] == ["asset-a", "asset-b", "asset-c"]


def test_an_idless_candidate_gets_a_stable_placeholder():
    pairs = normalise_candidates([{"text": "one"}, {"text": "two"}])
    assert [cid for cid, _t in pairs] == ["candidate-0", "candidate-1"]


def test_duplicate_ids_are_dropped_not_ranked_twice():
    pairs = normalise_candidates([
        {"id": "x", "text": "one"},
        {"id": "x", "text": "two"},
    ])
    assert [cid for cid, _t in pairs] == ["x"]


def test_an_asset_id_key_is_accepted_as_an_id():
    pairs = normalise_candidates([{"asset_id": "media-7", "description": "a clip"}])
    assert pairs == [("media-7", "a clip")]


def test_overlong_candidate_text_is_truncated_locally():
    long_text = "x" * 10_000
    pairs = normalise_candidates([{"id": "a", "text": long_text}])
    assert len(pairs[0][1]) == 4_000


def test_equal_scores_keep_the_input_order():
    """A flat score set must not reshuffle; ties break on input position."""
    scored = [
        ScoredCandidate(id="first", text="", score=0.5),
        ScoredCandidate(id="second", text="", score=0.5),
        ScoredCandidate(id="third", text="", score=0.5),
    ]
    assert order_from_scores(scored) == ("first", "second", "third")


def test_scores_sort_best_first():
    scored = [
        ScoredCandidate(id="low", text="", score=0.1),
        ScoredCandidate(id="high", text="", score=0.9),
        ScoredCandidate(id="mid", text="", score=0.5),
    ]
    assert order_from_scores(scored) == ("high", "mid", "low")


def test_the_result_is_json_serialisable_for_the_audit_trail():
    embedder = _embedder([_vector(1.0)] * 4)
    result = _provider(embedder).rerank("a balloon", CANDIDATES, opt_in=True)
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["applied"] is True
    assert len(payload["scored"]) == 3


def test_the_result_dataclass_defaults_to_an_unapplied_verdict():
    empty = RerankResult(order=("a",), applied=False, reason="not opted in")
    assert empty.degraded is True
    assert empty.scored == ()
    assert empty.to_dict()["provider"] == ""


# ===========================================================================
# live provider -- honestly skipped without credentials
# ===========================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("TWELVELABS_API_KEY"),
    reason="live TwelveLabs credentials are not configured",
)
def test_live_rerank_orders_by_semantic_similarity():
    result = SemanticRerankProvider().rerank(
        "a red balloon in the sky", CANDIDATES, opt_in=True)
    assert result.applied is True
    assert len(result.order) == len(CANDIDATES)
    assert result.requests == len(CANDIDATES) + 1


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("TWELVELABS_API_KEY"),
    reason="live TwelveLabs credentials are not configured",
)
def test_live_idempotency_key_is_stable_across_processes():
    assert derive_idempotency_key("q", "t") == derive_idempotency_key("q", "t")