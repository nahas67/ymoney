"""``semantic_rerank`` -- an OPTIONAL semantic re-ranker (Work 15.6 §6).

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry). The donor's
``app/services/twelvelabs.py`` established three rules and they are the whole
reason this file exists:

1. **Opt in.** Semantic scoring is never on by default; a deployment that did
   not ask for it keeps the deterministic order it already had.
2. **Degrade to input order.** When the provider is unavailable, unconfigured,
   over quota or simply slower than the deadline, the caller's list comes back
   EXACTLY as it went in. Reordering is an enhancement; losing the list is a
   bug.
3. **Refuse a partial rerank.** A rerank that scores 7 of 9 candidates and
   silently keeps the other two where they were is worse than no rerank at
   all: the surviving order is an artifact of which requests failed, not a
   judgement about the content. So an incomplete score set is refused whole.

Two boundaries are load-bearing.

**It is not a media-analysis provider.** This adapter shares no code path with
face tracking, diarization, segmentation or smart reframing and declares none
of those capability kinds. It scores text against text; it never looks at
pixels or samples audio. It is registered as its own ``semantic_rerank``
chain in :mod:`app.engine.intel.registry` rather than bolted onto an existing
one, so ``resolve("reframe")`` cannot return it.

**The embed call is billable.** TwelveLabs meters input tokens, so every
request goes through :mod:`app.services.paid_jobs` exactly like a render does:
a :class:`~app.services.paid_jobs.SubmissionRecord` is opened *before* the
POST, every failure is classified rather than guessed, and an ambiguous
failure lands in ``SUBMISSION_UNKNOWN`` -- at which point this adapter stops
and degrades, because buying a second embedding to recover from a lost
response is exactly the loss the state machine exists to prevent.

No SDK is vendored. The wire format is TwelveLabs Embed API v2
(``POST /v1.3/embed-v2``, ``x-api-key``, ``input_type``/``model_name`` body)
and the similarity is plain stdlib arithmetic. YMONEY depends on httpx and
nothing new.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.engine.intel.base import (
    COMMERCIAL_UNVERIFIED,
    MODE_REMOTE,
    CancelFn,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProgressFn,
    ProviderCancelled,
    ProviderHealth,
    ProviderResult,
    ProviderTimeout,
    ResourceSpec,
    check_control,
    unverified_license,
)
from app.services.paid_jobs import (
    SubmissionRecord,
    SubmissionState,
    classify_submit_exception,
    record_submission,
)

logger = logging.getLogger("ymoney.intel")

#: TwelveLabs Embed API v2. Sync: one embedding per request, returned inline.
DEFAULT_BASE_URL = "https://api.twelvelabs.io"
EMBED_PATH = "/v1.3/embed-v2"
DEFAULT_MODEL = "marengo3.5"
SUPPORTED_MODELS: tuple[str, ...] = ("marengo3.5", "marengo3.0")
#: Matryoshka truncation; must match between the query and the candidates or
#: the vectors are not comparable at all.
DEFAULT_DIMENSION = 512
SUPPORTED_DIMENSIONS: tuple[int, ...] = (128, 256, 512)

#: Per-request budgets. Connect is short because a TCP failure is provably
#: undelivered; read is longer because embedding a long caption is real work.
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 60.0

#: Bounded fan-out. Each candidate is one billable request, so the candidate
#: count is a SPEND parameter and is capped here rather than trusted from the
#: caller. The donor's per-term lru_cache is deliberately NOT copied: caching
#: embeds to dodge the N+1 shape moves the cost somewhere less visible.
MAX_CANDIDATES = 32
#: Text longer than this is truncated before it leaves the process; the
#: provider's own ceiling is 2,000 tokens.
MAX_QUERY_CHARS = 8_000
MAX_CANDIDATE_CHARS = 4_000

#: Why a rerank was not applied. Reported verbatim; never invented.
REASON_NOT_OPTED_IN = "not opted in"
REASON_NO_QUERY = "no query text supplied"
REASON_NO_CANDIDATES = "no candidates supplied"
REASON_TOO_MANY = f"more than {MAX_CANDIDATES} candidates"
REASON_PARTIAL = "provider scored an incomplete candidate set"
REASON_NO_EMBEDDINGS = "provider returned no usable embedding"
REASON_UNAVAILABLE = "provider unavailable"
REASON_CANCELLED = "cancelled"
REASON_TIMEOUT = "deadline exceeded"

#: TwelveLabs publishes no per-candidate confidence. The only uncertainty it
#: returns is a per-DIMENSION ``embedding_uncertainty`` vector, which is not a
#: confidence for a candidate and is therefore never collapsed into one here.
CONFIDENCE_SOURCE_NONE = "provider returned no per-candidate confidence"
SCORE_SOURCE_DERIVED = "cosine_similarity(provider_embeddings)"

#: Shown when the capability is dark. The honest remediation for an OPTIONAL
#: provider is "nothing is required" -- the deterministic path already works --
#: plus where to opt in deliberately.
REMEDIATION = (
    "none required: callers keep their deterministic order. To enable semantic "
    "reranking, set TWELVELABS_API_KEY (or the workspace credential) and pass "
    "opt_in=true explicitly."
)

#: Code AND model terms are UNVERIFIED: no operator audit of TwelveLabs' model
#: card or service terms exists in docs/oss/MEDIA_INTEL_LICENSES.md yet, so
#: commercial mode refuses this provider rather than assuming clearance.
CODE_LICENSE = "UNKNOWN"
LICENSE_NOTES = (
    "no vendor code is used (httpx only); the Marengo model and the hosted "
    "service terms are unverified -- an operator audit must fill them in "
    "docs/oss/MEDIA_INTEL_LICENSES.md before commercial use"
)


@dataclass(frozen=True)
class ScoredCandidate:
    """One candidate plus whatever the provider ACTUALLY said about it."""

    #: stable caller-supplied id (asset id, clip id, source id)
    id: str
    #: the text that was scored
    text: str
    #: cosine similarity of the provider's embeddings, 0..1
    score: float
    #: how ``score`` was produced -- derived here, not returned upstream
    score_source: str = SCORE_SOURCE_DERIVED
    #: the provider's own confidence, or ``None``. Never fabricated.
    confidence: float | None = None
    #: why ``confidence`` is ``None``, so an absent value is explainable
    confidence_source: str = CONFIDENCE_SOURCE_NONE
    #: raw provider evidence, enough to audit the decision later
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "score": self.score,
            "score_source": self.score_source,
            "confidence": self.confidence,
            "confidence_source": self.confidence_source,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class RerankResult:
    """The ordered ids plus the decision that produced them.

    ``order`` always has the same length as the input. ``applied`` says whether
    a provider actually ranked it, and ``reason`` says why not -- an
    unapplied rerank is a normal outcome, not an error, so the reason is part
    of the result rather than a log line nobody reads.
    """

    order: tuple[str, ...]
    applied: bool
    reason: str
    provider: str = ""
    model: str = ""
    #: present only when ``applied`` is True
    scored: tuple[ScoredCandidate, ...] = ()
    #: billable request count actually issued (0 when nothing was sent)
    requests: int = 0

    @property
    def degraded(self) -> bool:
        return not self.applied

    def to_dict(self) -> dict:
        return {
            "order": list(self.order),
            "applied": self.applied,
            "degraded": self.degraded,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "requests": self.requests,
            "scored": [item.to_dict() for item in self.scored],
        }


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


def derive_idempotency_key(query: str, text: str) -> str:
    """Stable key over the exact pair that decides a score.

    Same query + same candidate text => same key, which is what makes a lost
    response recognizable as the SAME request on the way back in. Not sent
    upstream: TwelveLabs documents no idempotency header (see
    :data:`app.services.paid_jobs_audit.IDEMPOTENCY_UNSUPPORTED`).
    """
    canonical = json.dumps({"query": query, "text": text}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity of two equal-length vectors, clamped to 0..1.

    Returns ``0.0`` for a degenerate input (empty, mismatched length,
    non-finite) rather than raising: a bad vector is a failed score, and a
    failed score must be able to REFUSE the rerank, not explode.
    """
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right, strict=True):
        if not math.isfinite(a) or not math.isfinite(b):
            return 0.0
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    value = dot / (math.sqrt(norm_left) * math.sqrt(norm_right))
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def parse_embeddings(payload: Any) -> tuple[list[list[float]], bool]:
    """Extract ``[(embedding, uncertainty_returned), ...]`` from a response.

    Only the documented shape is read: ``data[i].embedding`` plus the optional
    ``data[i].embedding_uncertainty``. Anything else yields no embeddings,
    which the caller treats as "refuse the rerank".
    """
    if not isinstance(payload, dict):
        return [], False
    data = payload.get("data")
    if not isinstance(data, list):
        return [], False
    vectors: list[list[float]] = []
    uncertainty_seen = False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("embedding")
        if not isinstance(raw, list) or not raw:
            continue
        try:
            vector = [float(value) for value in raw]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in vector):
            continue
        if isinstance(entry.get("embedding_uncertainty"), list):
            uncertainty_seen = True
        vectors.append(vector)
    return vectors, uncertainty_seen


def normalise_candidates(raw: Any) -> list[tuple[str, str]]:
    """``[(id, text), ...]`` from the request params, order preserved.

    Duplicated ids are dropped rather than silently ranked twice: two rows
    with the same id make an order meaningless.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw or []):
        if isinstance(item, dict):
            cid = str(item.get("id") or item.get("asset_id") or "").strip()
            text = str(item.get("text") or item.get("description") or "").strip()
        else:
            cid = ""
            text = ""
        if not cid:
            cid = f"candidate-{index}"
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, text[:MAX_CANDIDATE_CHARS]))
    return out


def order_from_scores(scored: list[ScoredCandidate]) -> tuple[str, ...]:
    """Ids best-first.

    Ties break on the ORIGINAL position, so a provider that returns identical
    scores leaves the input order intact instead of reshuffling it.
    """
    indexed = list(enumerate(scored))
    indexed.sort(key=lambda pair: (-pair[1].score, pair[0]))
    return tuple(item.id for _, item in indexed)


def is_complete(scored: list[ScoredCandidate], expected: int,
                vectors: list[list[float]] | None = None) -> bool:
    """True only when EVERY input candidate was scored exactly once.

    This is the partial-rerank refusal, stated as one predicate so there is a
    single place to break when testing it. ``vectors`` is the optional raw
    evidence: an unscorable candidate is recorded as an EMPTY vector sentinel,
    so a caller that forgets to pass it still catches the count mismatch, and
    one that passes it also catches an empty vector hiding in the middle.
    """
    if expected <= 0 or len(scored) != expected:
        return False
    if vectors is not None and len(vectors) != expected:
        return False
    if vectors is not None and any(not vector for vector in vectors):
        return False
    ids = [item.id for item in scored]
    return len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------


class TwelveLabsEmbedder:
    """Thin httpx client for Embed API v2. No SDK, no vendored code."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        dimension: int = DEFAULT_DIMENSION,
        timeout: float = READ_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = str(model or DEFAULT_MODEL)
        self.dimension = int(dimension or DEFAULT_DIMENSION)
        self.timeout = float(timeout or READ_TIMEOUT_SECONDS)
        self._client = client

    def _client_scope(self):
        """A client context that closes ONLY a client we created ourselves.

        An injected client (tests, a shared pool) is reused across calls and is
        the caller's to close; closing it here would make the embedder
        single-use and silently break every later candidate.
        """
        if self._client is not None:
            return nullcontext(self._client)
        return httpx.Client(follow_redirects=False)

    def embed_text(self, text: str, *, workspace_id: str = "",
                   should_cancel: CancelFn | None = None) -> list[float]:
        """One embedding, through the paid-job contract.

        Raises ``PaidJobError`` on any ambiguity -- an ambiguous failure here
        means the request may have been billed, and the caller must degrade
        rather than buy the same embedding again.
        """
        record = SubmissionRecord(workspace_id=workspace_id, provider=self.model)
        record.idempotency_key = derive_idempotency_key("", text)
        try:
            with self._client_scope() as client:
                response = client.post(
                    f"{self.base_url}{EMBED_PATH}",
                    headers={"x-api-key": self.api_key,
                             "Content-Type": "application/json"},
                    json={
                        "model_name": self.model,
                        "input_type": "text",
                        "text": text,
                        "embedding_dimension": self.dimension,
                    },
                    timeout=(CONNECT_TIMEOUT_SECONDS, self.timeout),
                )
        except httpx.HTTPError as exc:
            # A lost response may have been billed: classify, never guess.
            classified = classify_submit_exception(
                exc, provider=self.model, remote_id=record.remote_id,
                attempt=record.attempts + 1)
            record_submission(record, classified)
            raise classified from exc

        if response.status_code >= 400:
            failure = httpx.HTTPStatusError(
                f"{response.status_code}",
                request=response.request, response=response,
            )
            classified = classify_submit_exception(
                failure, provider=self.model, remote_id=record.remote_id,
                attempt=record.attempts + 1)
            record_submission(record, classified)
            raise classified from failure

        try:
            payload = response.json()
        except ValueError as exc:
            # A 2xx we cannot read is NOT a confirmed submission: no durable
            # id, no artifact. Ambiguous by construction.
            classified = classify_submit_exception(
                exc, provider=self.model, attempt=record.attempts + 1)
            record_submission(record, classified)
            raise classified from exc

        record_submission(record, None)
        vectors, _uncertainty = parse_embeddings(payload)
        if not vectors:
            raise classify_submit_exception(
                ValueError(REASON_NO_EMBEDDINGS), provider=self.model)
        if should_cancel is not None:
            should_cancel()
        return vectors[0]


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------


def _api_key(workspace_id: str = "") -> str:
    """Workspace credential first, then the process-level fallback.

    Mirrors the music provider's resolution order. The key is used only as a
    request header and never enters a log line, a cache key or a result.
    """
    try:
        from app.services.provider_settings import get_credential

        value, _source = get_credential("intel.twelvelabs_api_key", workspace_id or None)
        if value:
            return str(value).strip()
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not crash the probe
        logger.warning("semantic_rerank: credential lookup failed (%s)",
                       type(exc).__name__)
    return str(os.environ.get("TWELVELABS_API_KEY") or "").strip()


class SemanticRerankProvider(MediaIntelProvider):
    """Optional semantic re-ranking of candidate texts against a query.

    Available only when an API key is configured -- it is an enhancement, so a
    deployment without credentials must resolve to nothing and keep its
    existing deterministic ordering. Every failure mode ends in
    :meth:`rerank` returning the INPUT ORDER with a reason, never in an
    exception the caller has to remember to catch.
    """

    key = "semantic_rerank"
    kind = "semantic_rerank"
    #: No extra chains. ``kind`` alone serves the single ``semantic_rerank``
    #: chain, which is exactly the separation required: this provider can
    #: never be resolved for face_tracking / diarization / reframe.
    kinds = ()

    def __init__(self, embedder: TwelveLabsEmbedder | None = None) -> None:
        self._embedder = embedder

    # -- availability ----------------------------------------------------

    def _client_for(self, workspace_id: str = "") -> TwelveLabsEmbedder | None:
        """The configured embedder, or ``None`` when there is no key."""
        if self._embedder is not None:
            return self._embedder
        key = _api_key(workspace_id)
        if not key:
            return None
        return TwelveLabsEmbedder(api_key=key)

    def health(self) -> ProviderHealth:
        """Configured, not probed. Never raises; dark states carry a reason.

        No network call: a probe would spend tokens to answer a question the
        credential already answers.
        """
        try:
            configured = bool(_api_key())
        except Exception as exc:  # noqa: BLE001 - a probe must not raise
            return ProviderHealth(
                available=False,
                reason=f"credential probe failed: {type(exc).__name__}",
                mode=MODE_REMOTE,
            )
        if not configured:
            return ProviderHealth(
                available=False,
                reason=(
                    "semantic reranking is optional and no TwelveLabs API key is "
                    "configured; the deterministic order is kept"
                ),
                mode=MODE_REMOTE,
                detail={
                    "optional": True,
                    "degrades_to": "input order",
                    # An unavailable verdict must tell the operator what to do,
                    # and for an OPTIONAL provider the honest answer is "nothing".
                    "remediation": REMEDIATION,
                },
            )
        return ProviderHealth(
            available=True,
            reason="",
            version=DEFAULT_MODEL,
            mode=MODE_REMOTE,
            detail={"optional": True, "max_candidates": MAX_CANDIDATES},
        )

    def capabilities(self) -> dict:
        """What this provider actually does, and what it refuses to claim."""
        health = self.health()
        return {
            "operations": ["rerank_candidates"],
            "use_cases": [
                "scene_broll_relevance",
                "clip_semantic_description",
                "best_clip_for_concept",
                "search_term_rerank",
                "asset_candidate_ranking",
            ],
            "models": list(SUPPORTED_MODELS),
            "default_model": DEFAULT_MODEL,
            "dimensions": list(SUPPORTED_DIMENSIONS),
            "max_candidates": MAX_CANDIDATES,
            "max_query_chars": MAX_QUERY_CHARS,
            "optional": True,
            "opt_in_required": True,
            "degrades_to": "input order",
            "refuses_partial_rerank": True,
            "billable": True,
            "confidence": "not published by the provider; never synthesised",
            # Honest negatives: this adapter is not a media analyser.
            "not_supported": [
                "face_tracking", "diarization", "segmentation", "reframe",
                "speech_activity", "active_speaker",
            ],
            "available": health.available,
        }

    def resource_requirements(self) -> ResourceSpec:
        """Remote inference: no GPU, no VRAM, but one billable call per text."""
        return ResourceSpec(
            gpu=False,
            vram_mb=0,
            ram_mb=0,
            model_bytes=0,
            notes=(
                "remote HTTP per candidate (billable, capped at "
                f"{MAX_CANDIDATES}); no local model, no GPU admission"
            ),
        )

    def license_info(self) -> LicenseInfo:
        """UNVERIFIED until an operator audits the model + service terms."""
        return unverified_license(
            code_license=CODE_LICENSE,
            code_license_url="",
            model_license="SEE_MODEL_CARD",
            notes=LICENSE_NOTES,
            model_gated=True,
        )

    # -- work ------------------------------------------------------------

    def rerank(
        self,
        query: str,
        candidates: Any,
        *,
        opt_in: bool = False,
        workspace_id: str = "",
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
        deadline: float | None = None,
        client: TwelveLabsEmbedder | None = None,
    ) -> RerankResult:
        """Re-rank candidate texts against ``query``. Never raises for provider
        problems and never reorders partially.

        The three donor rules are the structure of this method: the opt-in check
        comes first, every degradation returns the input order verbatim, and
        :func:`is_complete` gates the single place where an order is changed.
        """
        pairs = normalise_candidates(candidates)
        input_order = tuple(cid for cid, _text in pairs)
        query_text = str(query or "").strip()[:MAX_QUERY_CHARS]

        if not opt_in:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_NOT_OPTED_IN)
        if not query_text:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_NO_QUERY)
        if not pairs:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_NO_CANDIDATES)
        if len(pairs) > MAX_CANDIDATES:
            # Refuse rather than truncate: a shortened candidate set silently
            # drops content the caller asked to rank.
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_TOO_MANY)

        embedder = client or self._client_for(workspace_id)
        if embedder is None:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_UNAVAILABLE)

        try:
            check_control(should_cancel, deadline)
        except ProviderCancelled:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_CANCELLED)
        except ProviderTimeout:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_TIMEOUT)

        # 1 request for the query, then one per candidate. A failure anywhere
        # below returns the input order; none of these paths is retried.
        requests = 0
        vectors: list[list[float]] = []
        try:
            query_vector = embedder.embed_text(
                query_text, workspace_id=workspace_id, should_cancel=should_cancel)
            requests += 1
        except ProviderCancelled:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_CANCELLED)
        except ProviderTimeout:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades
            # Includes PaidSubmissionUnconfirmed: an ambiguous submit may have
            # been billed, so the correct response is to STOP and keep the
            # caller's order, never to buy the embeddings again.
            logger.info("semantic_rerank: degraded (%s)", type(exc).__name__)
            return RerankResult(order=input_order, applied=False,
                                reason=f"provider failure: {type(exc).__name__}",
                                provider=self.key, model=embedder.model,
                                requests=requests)

        # Per-candidate failures are collected rather than raised, so the ONE
        # refusal gate below decides the outcome. A candidate that fails is a
        # partial rerank; reporting it as anything else would let a caller
        # treat a half-ranked list as a complete one.
        try:
            for index, (_cid, text) in enumerate(pairs):
                check_control(should_cancel, deadline)
                try:
                    vectors.append(embedder.embed_text(
                        text, workspace_id=workspace_id,
                        should_cancel=should_cancel))
                except (ProviderCancelled, ProviderTimeout):
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad candidate
                    logger.info("semantic_rerank: candidate %d unscored (%s)",
                                index, type(exc).__name__)
                    vectors.append([])   # sentinel: an UNSCORABLE candidate
                requests += 1
                if progress is not None:
                    progress(0.5 * (index + 1) / len(pairs))
        except ProviderCancelled:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_CANCELLED,
                                provider=self.key, model=embedder.model,
                                requests=requests)
        except ProviderTimeout:
            return RerankResult(order=input_order, applied=False,
                                reason=REASON_TIMEOUT,
                                provider=self.key, model=embedder.model,
                                requests=requests)

        # THE refusal, before any scoring: an incomplete candidate set is not
        # reordered, because the resulting order would be an artifact of which
        # requests failed rather than a judgement about the content.
        if not is_complete(
            [ScoredCandidate(id=cid, text=text, score=0.0)
             for cid, text in pairs], len(pairs), vectors):
            return RerankResult(order=input_order, applied=False, reason=REASON_PARTIAL,
                                provider=self.key, model=embedder.model,
                                requests=requests)
        scored = self._score(query_vector, pairs, vectors, embedder)
        if progress is not None:
            progress(1.0)
        return RerankResult(order=order_from_scores(scored), applied=True, reason="",
                            provider=self.key, model=embedder.model,
                            scored=tuple(scored), requests=requests)

    def _score(
        self,
        query_vector: list[float],
        pairs: list[tuple[str, str]],
        vectors: list[list[float]],
        embedder: TwelveLabsEmbedder,
    ) -> list[ScoredCandidate]:
        """One ScoredCandidate per candidate, with honest evidence.

        ``confidence`` is ``None`` unless the provider published a scalar one.
        TwelveLabs publishes none, so it is always ``None`` -- and the reason
        travels with the value so nobody reads the absence as an oversight.
        """
        scored: list[ScoredCandidate] = []
        for (cid, text), vector in zip(pairs, vectors, strict=False):
            scored.append(ScoredCandidate(
                id=cid,
                text=text,
                score=cosine_similarity(query_vector, vector),
                score_source=SCORE_SOURCE_DERIVED,
                confidence=None,
                confidence_source=CONFIDENCE_SOURCE_NONE,
                evidence={
                    "provider": "twelvelabs",
                    "model": embedder.model,
                    "dimension": embedder.dimension,
                    "source_asset": cid,
                    "idempotency_key": derive_idempotency_key("", text),
                    "text_chars": len(text),
                },
            ))
        return scored

    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Registry entry point. Returns the order; never raises for provider
        problems.

        The parameters are the rerank inputs: ``query``, ``candidates``
        (``[{id, text}]``) and ``opt_in``. A run that degrades is reported as
        a successful run with ``applied: false`` and a reason -- the caller's
        content is intact, which is the whole contract of the degradation.
        """
        params: dict[str, Any] = dict(request.params or {})
        opt_in = bool(params.get("opt_in", params.get("enabled", False)))
        result = self.rerank(
            str(params.get("query") or ""),
            params.get("candidates"),
            opt_in=opt_in,
            workspace_id=request.workspace_id,
            progress=progress,
            should_cancel=should_cancel,
            deadline=deadline,
        )
        payload = result.to_dict()
        warnings = [f"semantic rerank not applied: {result.reason}"] if result.degraded else []
        progress(1.0)
        return ProviderResult(
            ok=True,
            artifacts={"semantic_rerank": payload},
            metrics={
                "order": list(result.order),
                "applied": result.applied,
                "degraded": result.degraded,
                "reason": result.reason,
                "requests": result.requests,
                "billable": result.requests > 0,
            },
            warnings=warnings,
        )

    def cost(self, spec: ResourceSpec) -> dict:
        """Cost is the vendor's, per input token; YMONEY never invents a rate."""
        return {
            "gpu_ms": 0,
            "cpu_ms": 0,
            "cost_micros": 0,
            "billed": False,
            "notes": (
                "remote metered API; the per-request cost is the vendor's and is "
                "not estimated here -- see the paid-job record for what was sent"
            ),
        }


#: the registry imports this symbol (contracts 1.1)
PROVIDER = SemanticRerankProvider


def provider() -> SemanticRerankProvider:
    """Build the provider (the registry instantiates with no arguments)."""
    return SemanticRerankProvider()


#: The credential key this provider resolves. Named here so the audit and the
#: tests can assert the provider and the settings table agree.
CREDENTIAL_KEY = "intel.twelvelabs_api_key"

#: Re-exported so a caller can recognise the ambiguous case without importing
#: paid_jobs itself.
AMBIGUOUS_STATE = SubmissionState.SUBMISSION_UNKNOWN

COMMERCIAL_VERDICT = COMMERCIAL_UNVERIFIED

__all__ = [
    "AMBIGUOUS_STATE",
    "COMMERCIAL_VERDICT",
    "CREDENTIAL_KEY",
    "REMEDIATION",
    "DEFAULT_BASE_URL",
    "DEFAULT_DIMENSION",
    "DEFAULT_MODEL",
    "EMBED_PATH",
    "MAX_CANDIDATES",
    "PROVIDER",
    "RerankResult",
    "ScoredCandidate",
    "SemanticRerankProvider",
    "SUPPORTED_DIMENSIONS",
    "SUPPORTED_MODELS",
    "TwelveLabsEmbedder",
    "cosine_similarity",
    "derive_idempotency_key",
    "is_complete",
    "normalise_candidates",
    "order_from_scores",
    "parse_embeddings",
    "provider",
]