"""ModelRouter + ModelCapabilityRegistry (Work 05, Lane B).

Provider-independent routing: agents request a *capability tier* for a task,
never a vendor model name. Concrete model names resolve at routing time from
workspace overrides, then the configured LLM provider settings, then process
environment — in that order. No vendor model literals live in this module or
in any agent.

Tiers: FAST / BALANCED / HIGH_QUALITY / PREMIUM (remote) and LOCAL_ONLY /
PRIVATE (never remote). PRIVATE and LOCAL_ONLY requests, or a workspace
privacy mode of ``private`` / ``local_only``, force local-only candidates;
forcing a remote pathway under those constraints raises
:class:`PrivacyRefusal` instead of silently using a remote model.

Health is tracked per abstract provider slot (``remote`` / ``local``). A
reported failure flips the slot unhealthy so subsequent routes avoid it; every
decision records the selected tier, the resolved model, the ordered fallback
chain, and the reason, and is appended to an in-memory ring observable via
:func:`routing_log`.

**Work 15.8 -- the chain is a money decision, and it now says so.** The audit
(``docs/MODEL_ROUTER_EXECUTION_AUDIT.md``) measured three defects in
:meth:`ModelRouter.complete`:

1. it caught bare ``Exception`` and advanced, so a possibly-billed leg and a
   clean 4xx were indistinguishable -- a six-tier chain could therefore become
   six paid POSTs on failures that prove nothing was billed;
2. :meth:`ModelCapabilityRegistry` never carried an *execution target*, so a
   local tier resolved to the literal string ``"local"`` which was then POSTed
   to whatever ``llm.base_url`` pointed at, and an unresolvable remote tier
   fell through to ``llm.py``'s process-wide default model, discarding the
   cost tier that was actually selected (PREMIUM is 8.0x baseline);
3. no leg was budget-gated.

The fix is three small objects: :class:`ExecutionTarget` (where a leg actually
runs), :class:`FailureClass` (what a leg's failure *proves*), and
:class:`FallbackPolicy` (how many paid legs and how much additional money this
chain is allowed). The rule the whole module now states once:

    fall through only after a PROVEN safe failure
        -- a supported 4xx, a connect failure, or a gateway outage;
    a read timeout, a dropped connection, a write/pool timeout, a 5xx, or a
    billed-but-unusable 2xx ->  SUBMISSION_UNKNOWN  ->  STOP the chain.

Intentional fallback survives: outage, 4xx and connect failure still walk to
the next tier, bounded by the policy's caps. Every chain writes an effective
decision -- the policy, each leg's target and model, each outcome, and why the
chain stopped -- to the ring behind :func:`chain_log`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from app.engine.intelligence.sanitize import redact_secrets

TIERS = ("FAST", "BALANCED", "HIGH_QUALITY", "PREMIUM", "LOCAL_ONLY", "PRIVATE")
REMOTE_TIERS = ("FAST", "BALANCED", "HIGH_QUALITY", "PREMIUM")
LOCAL_TIERS = ("LOCAL_ONLY", "PRIVATE")

REMOTE_SLOT = "remote"
LOCAL_SLOT = "local"

_ROUTING_LOG_MAX = 200

_DEFAULT_INTELLIGENCE_SETTINGS = {
    "decision_mode": "auto",
    "provider_preference": "",
    "remote_allowed": True,
    "context_filtering": True,
    "browser_enabled": False,
    "allowed_domains": [],
    "routing_strategy": "balanced",  # cost | quality | latency | balanced
    "privacy_mode": "standard",  # standard | private | local_only
    "disabled_tiers": [],
    "models": {},  # tier (lowercase) -> explicit model name override
    "fallback_policy": {},  # Work 15.8 §9; see FallbackPolicy
}


class LLMRoutingError(Exception):
    """Raised when no routed model can serve a request."""


class PrivacyRefusal(LLMRoutingError):
    """Raised when a request would force a remote pathway under a local-only
    privacy constraint. Never silently fall back to remote."""


class LocalExecutionRefused(PrivacyRefusal):
    """A local tier was pointed at something that is not a local provider.

    A workspace ``models.local_only = "<remote-model>"`` override is the
    reachable case: it used to be returned before any locality check, so a
    local-only tier could POST a remote model name at a gateway. That defeats
    ``privacy_mode`` completely, so the override is REJECTED here rather than
    honoured and hoped about.
    """


class LocalModelUnavailable(LLMRoutingError):
    """A LOCAL tier was routed to, and no local provider can serve it.

    Never answered by quietly calling the remote gateway instead. The policy
    decides whether the chain falls through; a paid fallback needs money
    authority, exactly as any other paid leg does.
    """


class UnresolvedModel(LLMRoutingError):
    """A REMOTE tier resolved to no concrete provider model name.

    Before this, the empty string was passed to ``llm.complete`` as
    ``model=None``, which substituted the process-wide default model. A
    PREMIUM tier (8.0x baseline) would then quietly run as the cheapest
    configured model: the operator selected a cost tier and paid for a
    different one. The mismatch is now an error carrying the tier's cost
    multiplier, and no vendor name appears in this module by construction.
    """


class AmbiguousLegStopped(LLMRoutingError):
    """The chain stopped because a leg may already have been billed.

    A subclass of :class:`LLMRoutingError` so existing ``except`` handlers keep
    working, with the money facts attached so a caller does not have to parse a
    message to learn that this was not a provider outage.
    """

    def __init__(self, message: str, *, tier: str = "",
                 failure_class: FailureClass | None = None,
                 submission_id: str = "") -> None:
        super().__init__(message)
        self.tier = tier
        self.failure_class = failure_class
        self.submission_id = submission_id
        self.may_incur_second_charge = True


def get_intelligence_settings(ws_settings: dict | None) -> dict:
    """Merged ``settings_json["intelligence"]`` with safe defaults."""
    merged = dict(_DEFAULT_INTELLIGENCE_SETTINGS)
    if isinstance(ws_settings, dict):
        raw = ws_settings.get("intelligence")
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in merged and value is not None:
                    merged[key] = value
    if not isinstance(merged.get("allowed_domains"), list):
        merged["allowed_domains"] = []
    if not isinstance(merged.get("disabled_tiers"), list):
        merged["disabled_tiers"] = []
    if not isinstance(merged.get("models"), dict):
        merged["models"] = {}
    if not isinstance(merged.get("fallback_policy"), dict):
        merged["fallback_policy"] = {}
    return merged


# ---------------------------------------------------------------------------
# Work 15.8 §3: where does this leg actually RUN?
# ---------------------------------------------------------------------------


class ExecutionTarget(StrEnum):
    """The execution site of one routed leg.

    Named after the health slots the registry already tracks
    (:data:`REMOTE_SLOT` / :data:`LOCAL_SLOT`) so there is exactly one
    vocabulary in this module. ``AUTO`` means "whatever the capability entry
    says", and is what a request carries unless it pins a target.

    The distinction is load-bearing. Before this, a local tier produced the
    string ``"local"``, which was truthy, so ``model=model or None`` kept it
    and ``llm.py`` POSTed ``{"model": "local"}`` to the configured gateway. A
    target makes the two cases impossible to confuse: a REMOTE leg must carry a
    concrete provider model name, and a LOCAL leg must name a registered local
    provider.
    """

    LOCAL = LOCAL_SLOT
    REMOTE = REMOTE_SLOT
    AUTO = "auto"

    @property
    def is_paid(self) -> bool:
        """Whether reaching this target can incur a billable request."""
        return self is ExecutionTarget.REMOTE


#: Identifiers that are routing vocabulary, not provider model names. None of
#: these may ever appear in a remote request body; ``"local"`` is the one that
#: actually escaped before Work 15.8.
NON_PROVIDER_MODEL_IDENTIFIERS = frozenset({
    "", "local", "local_only", "local-only", "private", "auto", "none", "null",
})


# ---------------------------------------------------------------------------
# Work 15.8 §2 + §9: what a failure PROVES, and what this chain may spend
# ---------------------------------------------------------------------------


class FailureClass(StrEnum):
    """What one failed leg's error proves about the money.

    The old loop asked "did it raise?" and answered "walk to the next tier".
    That question has one safe answer and a dozen unsafe ones, so this is the
    question instead. Anything not provably undelivered is treated as
    possibly-billed, and the chain stops.
    """

    #: The provider refused the request itself: a 4xx. No task was created.
    KNOWN_REJECTION = "KNOWN_REJECTION"
    #: 429. Still a definitive refusal -- nothing was billed -- but a caller
    #: that wants to distinguish "wrong request" from "come back later" can.
    RATE_LIMITED = "RATE_LIMITED"
    #: No socket was ever established (connect timeout / DNS / refused).
    CONNECT_FAILURE = "CONNECT_FAILURE"
    #: 502/503/504: the gateway is not serving. A gateway that answers
    #: "unavailable" has not metered a completion.
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    #: 500 and every other 5xx. The request may have been processed and
    #: billed before the error was raised, so it is NOT a safe fall-through.
    SERVER_ERROR = "SERVER_ERROR"
    #: Delivered, response lost. The provider may have generated and billed it.
    READ_TIMEOUT = "READ_TIMEOUT"
    #: The write itself timed out: we do not know how much of the request was
    #: received, so we do not know whether it was billed.
    WRITE_TIMEOUT = "WRITE_TIMEOUT"
    #: We never got a connection out of the pool. A request we could not send
    #: is not billed, but the pool is exhausted, so retrying immediately is
    #: wrong too -- it is classed unsafe-by-default rather than as a connect
    #: failure, and an operator can add it to the safe set deliberately.
    POOL_TIMEOUT = "POOL_TIMEOUT"
    #: Peer closed / protocol error after the request was sent.
    DROPPED_CONNECTION = "DROPPED_CONNECTION"
    #: 2xx, so it WAS metered, and the body is not content we can use.
    BILLED_UNUSABLE = "BILLED_UNUSABLE"
    #: The generic name for "sent, outcome unknown", used when the cause is
    #: not one of the sharper classes above.
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    #: A LOCAL tier with no registered local provider. Never answered with a
    #: paid remote call.
    LOCAL_UNAVAILABLE = "LOCAL_UNAVAILABLE"
    #: The pre-spend budget gate refused the leg. Nothing was sent.
    BUDGET_REFUSED = "BUDGET_REFUSED"
    #: Cancelled before the request left. The outcome is known and nothing
    #: was billed.
    CANCELLED = "CANCELLED"
    #: Anything this module cannot place. Unsafe by default: an unclassified
    #: error is not evidence of safety.
    UNCLASSIFIED = "UNCLASSIFIED"

    @property
    def proven_safe(self) -> bool:
        """True only when the failure proves NOTHING was billed."""
        return self in SAFE_FAILURE_CLASSES

    @property
    def may_have_been_billed(self) -> bool:
        """True when the leg may already have cost money.

        This is the set that needs an approver to fall through, whatever it
        is called: a lost response, a 5xx, or a completion that was metered
        and unusable.
        """
        return self in MAY_HAVE_BEEN_BILLED


#: The classes that PROVE nothing was billed, and therefore the only ones a
#: chain may fall through on without asking anyone. Everything else is a money
#: decision.
#:
#: ``LOCAL_UNAVAILABLE`` is here because a local runner costs nothing -- there
#: is no charge to double. Whether the chain may then reach a PAID leg is a
#: separate question, answered by ``FallbackPolicy.allow_local_to_remote``.
#:
#: ``CANCELLED`` is deliberately NOT here even though it also proves nothing
#: was billed: a cancelled leg means the caller wanted to stop, so continuing
#: the chain would ignore the cancellation rather than respect the money rule.
SAFE_FAILURE_CLASSES = frozenset({
    FailureClass.KNOWN_REJECTION,
    FailureClass.RATE_LIMITED,
    FailureClass.CONNECT_FAILURE,
    FailureClass.PROVIDER_OUTAGE,
    FailureClass.LOCAL_UNAVAILABLE,
})

#: Classes where the leg may already have been billed. Advancing past one of
#: these is the "second charge" the policy has to authorise by name.
MAY_HAVE_BEEN_BILLED = frozenset({
    FailureClass.SERVER_ERROR,
    FailureClass.READ_TIMEOUT,
    FailureClass.WRITE_TIMEOUT,
    FailureClass.POOL_TIMEOUT,
    FailureClass.DROPPED_CONNECTION,
    FailureClass.BILLED_UNUSABLE,
    FailureClass.SUBMISSION_UNKNOWN,
    FailureClass.UNCLASSIFIED,
})


def classify_leg_failure(exc: BaseException) -> FailureClass:
    """Name what one failed leg's error PROVES.

    Deliberately fail-closed: an exception this function cannot place comes back
    :attr:`FailureClass.UNCLASSIFIED`, which is not safe to fall through. The
    old behaviour -- "it raised, try the next tier" -- is exactly the behaviour
    that turned one ambiguous completion into a chain of them.

    The wrapped chain is walked because the billable lane wraps the raw
    transport error: ``llm.complete`` raises ``LLMCompletionError`` whose cause
    is ``LLMCompletionUnknown`` whose cause is the ``httpx`` error. Only the
    innermost cause distinguishes a read timeout from a connect failure, and
    that difference decides whether the money is gone.
    """

    from app.engine.intelligence import llm_paid

    fallback = FailureClass.UNCLASSIFIED
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        found = _classify_one(current, llm_paid)
        if found is not None:
            if found is not FailureClass.SUBMISSION_UNKNOWN:
                return found
            # Keep walking: a sharper cause may still be attached.
            fallback = found
        current = current.__cause__ or current.__context__
    return fallback


def _classify_one(exc: BaseException, llm_paid_mod) -> FailureClass | None:
    """One link of the wrapped chain. ``None`` = "no opinion, keep walking"."""
    import httpx

    # The billable lane classifies first and says so in ``kind``. Dispatching on
    # that (rather than on the class) is what lets a BUDGET_REFUSED -- which
    # bills nothing -- be told apart from an AMBIGUOUS completion.
    if isinstance(exc, llm_paid_mod.LLMCompletionPaidError):
        by_kind = {
            "BILLED_BUT_UNUSABLE": FailureClass.BILLED_UNUSABLE,
            "EXHAUSTED_ON_PROVEN_SAFE_FAILURES": FailureClass.KNOWN_REJECTION,
            "BUDGET_REFUSED": FailureClass.BUDGET_REFUSED,
            "CANCELLED": FailureClass.CANCELLED,
        }
        found = by_kind.get(str(getattr(exc, "kind", "")))
        if found is not None:
            return found
        # AMBIGUOUS: keep walking, because only the cause distinguishes a read
        # timeout from a connect failure, and that decides the money.
        return None

    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is not None:
        status = int(status)
        if 400 <= status < 500:
            return (FailureClass.RATE_LIMITED if status == 429
                    else FailureClass.KNOWN_REJECTION)
        if status in (502, 503, 504):
            return FailureClass.PROVIDER_OUTAGE
        if status >= 500:
            return FailureClass.SERVER_ERROR

    # Order matters: the read/write/pool timeouts are all TimeoutException.
    if isinstance(exc, httpx.ConnectTimeout):
        return FailureClass.CONNECT_FAILURE
    if isinstance(exc, (httpx.ConnectError, ConnectionRefusedError)):
        return FailureClass.CONNECT_FAILURE
    if isinstance(exc, httpx.ReadTimeout):
        return FailureClass.READ_TIMEOUT
    if isinstance(exc, httpx.WriteTimeout):
        return FailureClass.WRITE_TIMEOUT
    if isinstance(exc, httpx.PoolTimeout):
        return FailureClass.POOL_TIMEOUT
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError,
                        httpx.WriteError, httpx.CloseError)):
        return FailureClass.DROPPED_CONNECTION

    # A bare LLMError is a PRE-FLIGHT refusal. llm.complete raises it when no
    # credentials are configured, before any request leaves, and every error
    # that happened on the wire arrives as an LLMCompletionError instead. So
    # this is the one shape that provably never reached a gateway.
    try:
        from app.providers import llm as llm_mod
    except Exception:  # noqa: BLE001 - the module may legitimately be absent
        return None
    if type(exc) is llm_mod.LLMError:
        return FailureClass.PROVIDER_OUTAGE
    return None


@dataclass(frozen=True)
class FallbackPolicy:
    """How much money this one chain may risk, and on whose authority.

    The chain-level sibling of
    :class:`app.engine.intelligence.llm_paid.FallbackPolicy`, which governs a
    single completion. This one governs the *chain*: how many legs, how many of
    them paid, and how much additional money on top of the selected tier.

    The defaults are the point. ``max_paid_attempts=2`` says a six-tier chain
    is not six paid POSTs: the selected tier, plus exactly one paid fallback,
    and that fallback only after a failure that PROVES nothing was billed.
    Everything else needs an explicit policy with a named approver.

    Two independent bounds, because they answer different questions:

    * ``max_attempts`` -- how many legs may run at all (local legs are free but
      still cost wall-clock and can loop);
    * ``max_paid_attempts`` -- how many of them may reach a metered gateway;
    * ``additional_budget`` -- a hard USD ceiling on the estimated exposure of
      the FALLBACK legs, on top of the selected tier's own attempt. ``None``
      (the default) means "no money bound configured", i.e. the count caps
      govern. A number turns the money question into an explicit one.

    ``allow_after_unknown`` is ``False`` by default and cannot be true without
    an ``approver``: falling through after a possibly-billed leg is spending
    money a second time, and an unattributed second charge cannot be explained
    three weeks later.
    """

    allowed: bool = True
    max_attempts: int = 4
    max_paid_attempts: int = 2
    additional_budget: float | None = None
    safe_failure_classes: tuple[FailureClass, ...] = tuple(SAFE_FAILURE_CLASSES)
    allow_after_unknown: bool = False
    allow_local_to_remote: bool = False
    approver: str = ""
    authority: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_paid_attempts < 0:
            raise ValueError("max_paid_attempts cannot be negative")
        if self.additional_budget is not None and self.additional_budget < 0:
            raise ValueError("additional_budget cannot be negative")
        resolved: list[FailureClass] = []
        for raw in self.safe_failure_classes:
            try:
                resolved.append(FailureClass(str(raw)))
            except ValueError as exc:
                raise ValueError(
                    f"unknown failure class {raw!r}; the vocabulary is "
                    f"{sorted(str(c) for c in FailureClass)}") from exc
        unsafe = sorted(str(c) for c in resolved if c not in SAFE_FAILURE_CLASSES)
        if unsafe:
            raise ValueError(
                f"these failure classes do not prove anything was undelivered, "
                f"so they cannot be listed as safe to fall through: {unsafe}. "
                f"Use allow_after_unknown with a named approver instead.")
        object.__setattr__(
            self, "safe_failure_classes",
            tuple(sorted(set(resolved), key=str)))
        if self.allow_after_unknown and not self.approver:
            raise ValueError(
                "allow_after_unknown=True requires approver: a policy that "
                "permits a second charge after a possibly-billed leg must say "
                "who authorised the extra exposure")
        if self.allow_local_to_remote and not self.approver:
            raise ValueError(
                "allow_local_to_remote=True requires approver: moving work "
                "from a local tier to a metered gateway must say who agreed to "
                "send it")

    def may_fall_through(self, failure: FailureClass) -> bool:
        """Whether this failure class may advance the chain."""
        return FailureClass(str(failure)) in self.safe_failure_classes

    def permits_uncertain_spend(self, failure: FailureClass) -> bool:
        """Whether an explicitly authorised chain may pass a possibly-billed leg.

        Separate from :meth:`may_fall_through` on purpose: this is the second
        charge, and it is what the approver's name is for.
        """
        return self.allow_after_unknown and FailureClass(
            str(failure)).may_have_been_billed

    def authorises_amount(self, remaining: float) -> bool:
        """Whether ``remaining`` USD of estimated exposure is still authorised."""
        if self.additional_budget is None:
            return True
        return remaining <= self.additional_budget

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "max_attempts": self.max_attempts,
            "max_paid_attempts": self.max_paid_attempts,
            "additional_budget": self.additional_budget,
            "safe_failure_classes": [str(c) for c in self.safe_failure_classes],
            "allow_after_unknown": self.allow_after_unknown,
            "allow_local_to_remote": self.allow_local_to_remote,
            "approver": self.approver,
            "authority": self.authority,
            "note": self.note,
        }


#: The policy a chain gets when nobody configured one. Conservative on purpose:
#: one paid fallback, and only after a failure that proves nothing was billed.
DEFAULT_FALLBACK_POLICY = FallbackPolicy()

_FALLBACK_POLICY_FIELDS = frozenset(
    {"allowed", "max_attempts", "max_paid_attempts", "additional_budget",
     "safe_failure_classes", "allow_after_unknown", "allow_local_to_remote",
     "approver", "authority", "note"})


def resolve_fallback_policy(ws_settings: dict | None,
                            override: FallbackPolicy | None = None
                            ) -> FallbackPolicy:
    """The policy in force: the caller's, else the workspace's, else the default.

    A workspace may tighten or loosen the chain, and loosening it is a money
    decision -- so ``allow_after_unknown`` and ``approver`` come from the
    workspace record together, and a workspace that grants itself money
    authority without naming anyone is refused with a routing error rather than
    honoured.
    """
    if override is not None:
        return override
    settings = get_intelligence_settings(ws_settings)
    raw = settings.get("fallback_policy") or {}
    if not isinstance(raw, dict) or not raw:
        return DEFAULT_FALLBACK_POLICY
    unknown = sorted(set(raw) - _FALLBACK_POLICY_FIELDS)
    if unknown:
        raise LLMRoutingError(
            f"unknown fallback_policy field(s): {unknown}; the accepted fields "
            f"are {sorted(_FALLBACK_POLICY_FIELDS)}")
    changes = dict(raw)
    if "safe_failure_classes" in changes:
        changes["safe_failure_classes"] = tuple(changes["safe_failure_classes"])
    try:
        return replace(DEFAULT_FALLBACK_POLICY, **changes)
    except (TypeError, ValueError) as exc:
        raise LLMRoutingError(
            f"workspace fallback_policy is not usable: {exc}") from exc


@dataclass(frozen=True)
class ChainLeg:
    """One leg of one chain, as decided. Written down, never re-derived."""

    tier: str
    target: ExecutionTarget
    outcome: str
    paid: bool
    attempt: int
    paid_attempt: int
    model: str = ""
    model_source: str = ""
    estimated_usd: float = 0.0
    fell_through: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChainRecord:
    """The effective decision for one chain, kept for audit.

    The routing log says which tier was PICKED; this says what the chain then
    COST -- every leg, its execution target, its model, its outcome, and the
    reason it stopped. Without it, "six tiers, six attempts" and "six tiers,
    one attempt" are indistinguishable after the fact.
    """

    workspace_id: str
    task_type: str
    policy: dict
    legs: list
    stop_reason: str
    paid_legs: int
    estimated_exposure_usd: float
    at: str = ""
    succeeded_tier: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelCapability:
    """Code-defined capability entry for one routing tier.

    ``tier`` is an abstract id (FAST, BALANCED, ...), never a vendor model
    name. ``provider`` is an abstract health slot (``remote`` / ``local``).
    """

    tier: str
    description: str
    context_window_tokens: int
    supports_tools: bool
    supports_vision: bool
    supports_reasoning: bool
    relative_cost: float  # 1.0 = baseline balanced cost
    latency_class: str  # low | medium | high
    quality_class: str  # standard | high | top
    remote: bool
    provider: str = REMOTE_SLOT
    enabled: bool = True


def _default_capabilities() -> list[ModelCapability]:
    return [
        ModelCapability(
            tier="FAST",
            description="Cheap, low-latency tier for discovery and metadata tasks.",
            context_window_tokens=32000,
            supports_tools=False,
            supports_vision=False,
            supports_reasoning=False,
            relative_cost=0.3,
            latency_class="low",
            quality_class="standard",
            remote=True,
            provider=REMOTE_SLOT,
        ),
        ModelCapability(
            tier="BALANCED",
            description="Default tier for general research and drafting work.",
            context_window_tokens=64000,
            supports_tools=True,
            supports_vision=False,
            supports_reasoning=False,
            relative_cost=1.0,
            latency_class="medium",
            quality_class="standard",
            remote=True,
            provider=REMOTE_SLOT,
        ),
        ModelCapability(
            tier="HIGH_QUALITY",
            description="Reasoning tier for strategy, scripts, and verification.",
            context_window_tokens=128000,
            supports_tools=True,
            supports_vision=True,
            supports_reasoning=True,
            relative_cost=3.0,
            latency_class="high",
            quality_class="high",
            remote=True,
            provider=REMOTE_SLOT,
        ),
        ModelCapability(
            tier="PREMIUM",
            description="Top-quality tier for final verification passes.",
            context_window_tokens=200000,
            supports_tools=True,
            supports_vision=True,
            supports_reasoning=True,
            relative_cost=8.0,
            latency_class="high",
            quality_class="top",
            remote=True,
            provider=REMOTE_SLOT,
        ),
        ModelCapability(
            tier="LOCAL_ONLY",
            description="On-device/offline tier. Never leaves the machine.",
            context_window_tokens=8000,
            supports_tools=False,
            supports_vision=False,
            supports_reasoning=False,
            relative_cost=0.0,
            latency_class="medium",
            quality_class="standard",
            remote=False,
            provider=LOCAL_SLOT,
        ),
        ModelCapability(
            tier="PRIVATE",
            description="Strict privacy tier: local execution, no telemetry.",
            context_window_tokens=8000,
            supports_tools=False,
            supports_vision=False,
            supports_reasoning=False,
            relative_cost=0.0,
            latency_class="medium",
            quality_class="standard",
            remote=False,
            provider=LOCAL_SLOT,
        ),
    ]


class ModelCapabilityRegistry:
    """Capability entries, local providers, per-slot health, workspace overrides."""

    def __init__(self, capabilities: list[ModelCapability] | None = None):
        self._entries: dict[str, ModelCapability] = {
            c.tier: c for c in (capabilities or _default_capabilities())
        }
        self._health: dict[str, bool] = {REMOTE_SLOT: True, LOCAL_SLOT: True}
        self._local_runners: dict[str, Callable[[str, str], str]] = {}

    def get(self, tier: str) -> ModelCapability | None:
        return self._entries.get((tier or "").upper())

    def all(self) -> list[ModelCapability]:
        return list(self._entries.values())

    def set_enabled(self, tier: str, enabled: bool) -> None:
        entry = self.get(tier)
        if entry is not None:
            entry.enabled = enabled

    # -- local providers --------------------------------------------------
    def register_local_model(self, name: str,
                             runner: Callable[[str, str], str] | None = None
                             ) -> None:
        """Register a LOCAL model, optionally with the callable that runs it.

        The registration is what makes a local alias mean something. Before
        this, the identifier ``"local"`` existed only as a string the resolver
        invented, with no provider behind it -- which is how it ended up in a
        request body aimed at a gateway. A local alias resolves ONLY if it is
        registered here; anything else is not a local provider and is refused.
        """
        clean = (name or "").strip()
        if not clean:
            raise ValueError("a local model needs a name")
        if runner is not None:
            self._local_runners[clean] = runner

    def local_models(self) -> tuple[str, ...]:
        """Every registered local model name, sorted."""
        return tuple(sorted(self._local_runners))

    def local_runner(self, name: str) -> Callable[[str, str], str] | None:
        """The callable that executes ``name`` locally, or ``None``.

        ``None`` is the honest answer whenever nothing is registered: YMONEY
        ships no local inference runtime, so a LOCAL tier has no way to be
        served unless an operator registered one.
        """
        return self._local_runners.get((name or "").strip())

    def is_local_model(self, name: str) -> bool:
        return (name or "").strip() in self._local_runners

    # -- health ---------------------------------------------------------
    def is_healthy(self, provider: str) -> bool:
        return self._health.get(provider, True)

    def report_failure(self, provider: str) -> None:
        self._health[provider] = False

    def report_success(self, provider: str) -> None:
        self._health[provider] = True

    def health(self) -> dict[str, bool]:
        return dict(self._health)


@dataclass
class RouteRequest:
    task_type: str = "general"
    modality: str = "text"  # text | vision | audio
    complexity: float = 0.5  # 0..1
    context_tokens: int = 0
    latency_sensitive: bool = False
    quality_required: str = "standard"  # standard | high | top
    budget_usd: float | None = None
    tier: str | None = None  # explicit tier pin
    require_remote: bool = False
    workspace_id: str = ""
    workspace_settings: dict | None = None
    #: Where the work may run. ``AUTO`` follows the capability entry's own
    #: ``remote`` flag; pinning LOCAL or REMOTE makes a mismatch an error
    #: rather than a silent re-route.
    target: ExecutionTarget = ExecutionTarget.AUTO

    @property
    def requires_remote(self) -> bool:
        return self.target is ExecutionTarget.REMOTE


@dataclass
class RoutingDecision:
    tier: str
    model: str
    remote: bool
    reason: str
    fallbacks: list[str] = field(default_factory=list)
    provider_health: dict = field(default_factory=dict)
    latency_class: str = ""
    quality_class: str = ""
    workspace_id: str = ""
    task_type: str = ""
    #: Where this decision runs. Never inferred by the caller from ``remote``.
    target: ExecutionTarget = ExecutionTarget.AUTO
    #: Which rule produced ``model``. ``UNRESOLVED`` and ``NO_LOCAL_PROVIDER``
    #: are the two that matter: they mean the model name is not a provider
    #: identifier and must not be sent anywhere.
    model_source: str = ""
    #: The capability tier's own cost multiple (1.0 = baseline). Carried so a
    #: caller can see what a mismatch would actually cost before it happens.
    relative_cost: float = 0.0

    @property
    def resolved(self) -> bool:
        """Whether ``model`` is a concrete provider identifier we may call."""
        return bool(self.model) and self.model_source not in (
            "UNRESOLVED", "NO_LOCAL_PROVIDER")

    def to_dict(self) -> dict:
        return asdict(self)


_LATENCY_RANK = {"low": 0, "medium": 1, "high": 2}
_QUALITY_RANK = {"standard": 0, "high": 1, "top": 2}


class ModelRouter:
    """Routes tasks to capability tiers; wraps the LLM provider (no fork)."""

    def __init__(self, registry: ModelCapabilityRegistry | None = None):
        self.registry = registry or ModelCapabilityRegistry()
        self._log: deque = deque(maxlen=_ROUTING_LOG_MAX)
        self._chain_log: deque = deque(maxlen=_ROUTING_LOG_MAX)

    # -- routing ----------------------------------------------------------
    def route(self, request: RouteRequest) -> RoutingDecision:
        settings = get_intelligence_settings(request.workspace_settings)
        privacy_mode = str(settings.get("privacy_mode") or "standard").lower()
        local_only = privacy_mode in ("private", "local_only")

        wanted_tier = (request.tier or "").upper() or None
        if wanted_tier and wanted_tier not in TIERS:
            raise LLMRoutingError(f"unknown tier {request.tier!r}")

        if request.require_remote and (local_only or wanted_tier in LOCAL_TIERS):
            raise PrivacyRefusal(
                "remote execution refused: workspace privacy mode "
                f"{privacy_mode!r} forbids remote pathways"
            )
        if wanted_tier in REMOTE_TIERS and local_only:
            raise PrivacyRefusal(
                f"remote tier {wanted_tier} refused under privacy mode {privacy_mode!r}"
            )
        if request.require_remote and not settings.get("remote_allowed", True):
            raise PrivacyRefusal("remote execution refused: remote_allowed is false")

        # An explicit target is a constraint, not a hint. A REMOTE target under
        # a local-only workspace is the same refusal as require_remote.
        requested_target = ExecutionTarget(str(request.target or ExecutionTarget.AUTO))
        if requested_target is ExecutionTarget.REMOTE and not (
                bool(settings.get("remote_allowed", True)) and not local_only):
            raise PrivacyRefusal(
                "REMOTE execution target refused: workspace policy forbids "
                "remote pathways")
        if requested_target is ExecutionTarget.LOCAL and request.require_remote:
            raise PrivacyRefusal(
                "contradictory request: target=LOCAL together with require_remote")
        if requested_target is ExecutionTarget.REMOTE and wanted_tier in LOCAL_TIERS:
            raise PrivacyRefusal(
                f"contradictory request: target=REMOTE cannot be served by the "
                f"local tier {wanted_tier}")
        if requested_target is ExecutionTarget.LOCAL and wanted_tier in REMOTE_TIERS:
            raise PrivacyRefusal(
                f"contradictory request: target=LOCAL cannot be served by the "
                f"remote tier {wanted_tier}")

        remote_allowed = bool(settings.get("remote_allowed", True)) and not local_only

        strategy = str(settings.get("routing_strategy") or "balanced").lower()
        preference = str(settings.get("provider_preference") or "").upper() or None
        if preference and preference not in TIERS:
            preference = None
        disabled = {(t or "").upper() for t in settings.get("disabled_tiers") or []}

        candidates = [c for c in self.registry.all() if c.enabled and c.tier not in disabled]
        if not remote_allowed:
            candidates = [c for c in candidates if not c.remote]
        if request.modality == "vision":
            vision = [c for c in candidates if c.supports_vision]
            if vision:
                candidates = vision
            elif remote_allowed:
                pass  # best-effort: keep text tiers rather than fail
            else:
                raise PrivacyRefusal(
                    "vision capability requires a remote tier, refused under local-only privacy"
                )
        if not candidates:
            raise LLMRoutingError("no routing candidates available")

        ordered = self._order(
            candidates, request, strategy, preference, wanted_tier, remote_allowed
        )
        if requested_target is not ExecutionTarget.AUTO:
            pinned_local = (requested_target is ExecutionTarget.LOCAL)
            matching = [c for c in ordered if c.remote is not pinned_local]
            if not matching:
                raise PrivacyRefusal(
                    f"no {requested_target} tier is available under this "
                    f"workspace's routing constraints")
            ordered = matching
        healthy = [c for c in ordered if self.registry.is_healthy(c.provider)]
        degraded = not healthy
        picked = healthy[0] if healthy else ordered[0]
        fallbacks = [c.tier for c in ordered if c.tier != picked.tier]

        model, source = self._resolve_model(picked.tier, settings)
        reason = self._reason(picked, request, strategy, fallbacks, degraded, preference)

        decision = RoutingDecision(
            tier=picked.tier,
            model=model,
            remote=picked.remote,
            reason=reason,
            fallbacks=fallbacks,
            provider_health=self.registry.health(),
            latency_class=picked.latency_class,
            quality_class=picked.quality_class,
            workspace_id=request.workspace_id,
            task_type=request.task_type,
            target=self.target_for(picked),
            model_source=source,
            relative_cost=float(picked.relative_cost),
        )
        self._record(request, decision)
        return decision

    def target_for(self, entry: ModelCapability) -> ExecutionTarget:
        """Where a capability entry actually runs.

        One place, so ``route()``, :meth:`complete` and the audit trail cannot
        disagree about whether a tier is local.
        """
        return ExecutionTarget.REMOTE if entry.remote else ExecutionTarget.LOCAL

    def _order(
        self,
        candidates: list[ModelCapability],
        request: RouteRequest,
        strategy: str,
        preference: str | None,
        wanted_tier: str | None,
        remote_allowed: bool,  # noqa: FBT001 — internal helper flag
    ) -> list[ModelCapability]:
        pinned = wanted_tier or preference
        prefer_local = not remote_allowed or (wanted_tier in LOCAL_TIERS)

        def locality(entry: ModelCapability) -> int:
            # Remote tiers serve by default; local tiers are last-resort
            # fallbacks unless privacy forces local execution (or a local
            # tier was explicitly requested).
            if prefer_local:
                return 0 if not entry.remote else 1
            return 0 if entry.remote else 1

        def score(entry: ModelCapability) -> tuple:
            # Lower tuple wins. Pin first, then locality, then strategy.
            pin = 0 if (pinned and entry.tier == pinned) else 1
            loc = locality(entry)
            if strategy == "cost":
                return (pin, loc, entry.relative_cost, _QUALITY_RANK[entry.quality_class])
            if strategy == "quality":
                return (
                    pin,
                    loc,
                    -_QUALITY_RANK[entry.quality_class],
                    entry.relative_cost,
                )
            if strategy == "latency":
                return (pin, loc, _LATENCY_RANK[entry.latency_class], entry.relative_cost)
            # balanced: task signals decide the price/quality trade-off.
            quality_need = _QUALITY_RANK.get(request.quality_required, 0)
            complexity = max(0.0, min(1.0, request.complexity))
            if request.latency_sensitive and complexity < 0.6 and quality_need == 0:
                return (pin, loc, _LATENCY_RANK[entry.latency_class], entry.relative_cost)
            if request.budget_usd is not None and request.budget_usd <= 0.5:
                return (pin, loc, entry.relative_cost, _LATENCY_RANK[entry.latency_class])
            if complexity >= 0.75 or quality_need >= 2:
                return (
                    pin,
                    loc,
                    -_QUALITY_RANK[entry.quality_class],
                    entry.relative_cost,
                )
            if complexity <= 0.3 and quality_need == 0:
                return (pin, loc, entry.relative_cost, _LATENCY_RANK[entry.latency_class])
            # default middle: prefer BALANCED, then cost, then quality.
            middle = 0 if entry.tier == "BALANCED" else 1
            return (pin, loc, middle, entry.relative_cost)
        # Stable sort keeps registry definition order inside equal scores.
        return sorted(candidates, key=score)

    def _resolve_model(self, tier: str, settings: dict) -> tuple[str, str]:
        """Concrete model name for a tier, plus which rule produced it.

        Never a hardcoded vendor literal, and never an invented one either.
        The previous version returned the literal string ``"local"`` for a local
        tier and ``""`` for a remote one; both were then handed to
        ``llm.complete(model=model or None)``, which turned ``"local"`` into a
        request body aimed at whatever gateway was configured and ``""`` into
        the process default. So:

        * a LOCAL tier resolves ONLY to a registered local model, and a
          workspace override naming anything else is REFUSED rather than
          honoured (:class:`LocalExecutionRefusal`);
        * a REMOTE tier that resolves to nothing returns ``("UNRESOLVED")``
          with an empty name, which :meth:`complete` refuses to call, instead of
          silently running a PREMIUM request as the cheapest configured model.
        """
        entry = self.registry.get(tier)
        local = (entry is not None and not entry.remote) or tier in LOCAL_TIERS
        override = str((settings.get("models") or {}).get(tier.lower()) or "")

        if local:
            if not override:
                return "", "NO_LOCAL_PROVIDER"
            if not self.registry.is_local_model(override):
                raise LocalExecutionRefused(
                    f"local tier {tier} was overridden with {override!r}, which "
                    f"is not a registered local provider; registered local "
                    f"models: {list(self.registry.local_models()) or 'none'}. "
                    f"A local tier never resolves to a remote model name.")
            return override, "LOCAL_REGISTRY"

        if override:
            return override, "WORKSPACE_OVERRIDE"
        try:
            from app.services.provider_settings import effective_llm

            eff = effective_llm()
            tiers = eff.get("tiers") or {}
            mapping = {
                "FAST": tiers.get("cheap") or tiers.get("default") or eff.get("model"),
                "BALANCED": tiers.get("default") or eff.get("model"),
                "HIGH_QUALITY": tiers.get("reasoning") or tiers.get("default") or eff.get("model"),
                "PREMIUM": (
                    tiers.get("verification")
                    or tiers.get("reasoning")
                    or tiers.get("default")
                    or eff.get("model")
                ),
            }
            name = mapping.get(tier)
            if name:
                return str(name), "WORKSPACE_TIER"
        except Exception:
            pass
        try:
            from app.core.config import settings as env_settings

            if getattr(env_settings, "llm_model", ""):
                return str(env_settings.llm_model), "ENV_DEFAULT"
        except Exception:
            pass
        return "", "UNRESOLVED"

    def _assert_remote_model(self, tier: str, model: str, source: str) -> str:
        """Refuse to send anything to a gateway that is not a provider id.

        Two distinct mistakes are caught here, and both used to reach the wire:
        the empty name (which ``llm.complete`` replaced with its own default,
        discarding the selected cost tier) and the string ``"local"`` (which is
        truthy and therefore survived ``model or None``).
        """
        clean = (model or "").strip()
        if not clean or source == "UNRESOLVED":
            entry = self.registry.get(tier)
            multiple = entry.relative_cost if entry is not None else 0.0
            raise UnresolvedModel(
                f"tier {tier} resolved to no provider model name, so calling it "
                f"would silently substitute the process default and discard the "
                f"selected cost tier ({multiple:.1f}x baseline). Configure a "
                f"model for {tier} or refuse the call.")
        if clean.lower() in NON_PROVIDER_MODEL_IDENTIFIERS:
            raise UnresolvedModel(
                f"refusing to send model={clean!r} to a remote gateway: "
                f"{clean!r} is routing vocabulary, not a provider model name")
        return clean

    def _run_local(self, tier: str, model: str, system: str,
                   user: str) -> str:
        """Execute a LOCAL leg through its registered runner.

        There is no fallback to the gateway here, on purpose. "Local tier
        unavailable" and "call the paid remote tier instead" are different
        decisions, and only the policy may make the second one.
        """
        runner = self.registry.local_runner(model)
        if runner is None:
            raise LocalModelUnavailable(
                f"local tier {tier} resolved to {model!r}, which has no "
                f"registered local runner; registered local models: "
                f"{list(self.registry.local_models()) or 'none'}")
        return runner(system, user)

    @staticmethod
    def _estimate_leg(model: str, system: str, user: str,
                      max_tokens: int) -> float:
        """Pre-spend estimate for one leg. A bound, never a measurement.

        Lazy import so this module keeps working without the billable lane.
        """
        try:
            from app.engine.intelligence import llm_paid
        except Exception:  # noqa: BLE001 - the estimate must never break routing
            return 0.0
        return llm_paid.estimate_request_cost(
            model, {"messages": [{"content": system}, {"content": user}]},
            max_tokens)

    def _reason(
        self,
        picked: ModelCapability,
        request: RouteRequest,
        strategy: str,
        skipped: list[str],
        degraded: bool,  # noqa: FBT001
        preference: str | None,
    ) -> str:
        bits = [
            f"task={request.task_type}",
            f"complexity={request.complexity:.2f}",
            f"strategy={strategy}",
        ]
        if request.tier:
            bits.append(f"requested_tier={picked.tier}")
        elif preference:
            bits.append(f"workspace_preference={preference}")
        else:
            bits.append(f"selected_tier={picked.tier}")
        bits.append(f"quality={picked.quality_class}/latency={picked.latency_class}")
        bits.append("local_execution" if not picked.remote else "remote_execution")
        if request.budget_usd is not None:
            bits.append(f"budget_usd={request.budget_usd}")
        if skipped:
            bits.append(f"fallbacks={','.join(skipped)}")
        if degraded:
            bits.append("degraded=all_providers_unhealthy_best_effort")
        return "; ".join(bits)

    def _record(self, request: RouteRequest, decision: RoutingDecision) -> None:
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "workspace_id": request.workspace_id,
            "task_type": request.task_type,
            "tier": decision.tier,
            "model": decision.model,
            "remote": decision.remote,
            "reason": decision.reason,
            "fallbacks": list(decision.fallbacks),
        }
        # The log is persisted in-memory and served via API: scrub it so a
        # task description carrying credentials can never leak through.
        self._log.append(redact_secrets(entry))

    # -- health + log ------------------------------------------------------
    def report_failure(self, provider: str) -> None:
        self.registry.report_failure(provider)

    def report_success(self, provider: str) -> None:
        self.registry.report_success(provider)

    def health(self) -> dict[str, bool]:
        return self.registry.health()

    def log(self, workspace_id: str | None = None) -> list[dict]:
        entries = list(self._log)
        if workspace_id:
            entries = [e for e in entries if e.get("workspace_id") == workspace_id]
        return [redact_secrets(dict(e)) for e in entries]

    # -- provider wrapper (wraps app.providers.llm, never forks it) ---------
    def complete(
        self,
        system: str,
        user: str,
        *,
        request: RouteRequest,
        workspace_id: str = "",
        temperature: float = 0.8,
        max_tokens: int = 1500,
        json_mode: bool = False,
        mock_fn=None,
        policy: FallbackPolicy | None = None,
    ):
        """Route once, then walk the chain under an explicit money policy.

        Provider failures flip the slot unhealthy (dynamic reroute for later
        calls). Beyond that, the chain advances ONLY after a failure that
        PROVES nothing was billed -- a supported 4xx, a connect failure or a
        gateway outage -- and only while the policy still has an attempt, a
        paid attempt, and (when configured) a dollar of headroom left.

        Anything else stops the chain with
        :class:`AmbiguousLegStopped`, because the leg may already have been
        billed and a chat completion has no remote id to reconcile against.

        Raises :class:`LLMRoutingError` when every candidate fails.
        """
        from app.providers import llm as llm_mod

        request.workspace_id = request.workspace_id or workspace_id
        # The tenant the request was routed FOR is the tenant the completion is
        # billed to. Forwarding the bare ``workspace_id`` argument instead would
        # silently book the spend against "" and skip the budget gate, which is
        # the exact defect this work set out to close.
        bill_to = request.workspace_id
        settings = get_intelligence_settings(request.workspace_settings)
        decision = self.route(request)
        effective = resolve_fallback_policy(request.workspace_settings, policy)
        chain = [decision.tier, *decision.fallbacks]

        legs: list[ChainLeg] = []
        errors: list[str] = []
        attempts = 0
        paid_attempts = 0
        exposed = 0.0
        stop_reason = "chain_exhausted"
        previous_target: ExecutionTarget | None = None

        for position, tier_id in enumerate(chain):
            entry = self.registry.get(tier_id)
            if entry is None or not entry.enabled:
                legs.append(ChainLeg(
                    tier=tier_id, target=ExecutionTarget.AUTO, outcome="skipped",
                    paid=False, attempt=attempts, paid_attempt=paid_attempts,
                    reason="tier unknown or disabled"))
                continue
            target = self.target_for(entry)
            if (target.is_paid and previous_target is ExecutionTarget.LOCAL
                    and not effective.allow_local_to_remote):
                # A LOCAL tier that could not run must not quietly become a
                # metered remote call. That is a separate decision, with its
                # own authority, because it moves data across a boundary the
                # caller chose on locality grounds.
                legs.append(ChainLeg(
                    tier=tier_id, target=target, outcome="skipped", paid=True,
                    attempt=attempts, paid_attempt=paid_attempts,
                    reason=("a LOCAL leg's failure may not become a paid remote "
                            "call unless the policy authorises it")))
                continue
            if target.is_paid and not self._remote_allowed_for(request):
                legs.append(ChainLeg(
                    tier=tier_id, target=target, outcome="skipped", paid=True,
                    attempt=attempts, paid_attempt=paid_attempts,
                    reason="remote execution not permitted by workspace policy"))
                continue
            try:
                model, source = self._resolve_model(tier_id, settings)
            except PrivacyRefusal:
                # An override that would break the local-only promise is not a
                # chain event to walk past; it is a refusal.
                raise

            if target.is_paid:
                model = self._assert_remote_model(tier_id, model, source)
                estimated = self._estimate_leg(model, system, user, max_tokens)
            else:
                estimated = 0.0

            # -- the two caps, plus the optional money cap -------------------
            if position > 0 or attempts > 0:
                if not effective.allowed:
                    legs.append(ChainLeg(
                        tier=tier_id, target=target, outcome="refused", paid=target.is_paid,
                        attempt=attempts, paid_attempt=paid_attempts, model=model,
                        model_source=source, estimated_usd=estimated,
                        reason="policy does not allow a second leg"))
                    break
                if target.is_paid and paid_attempts >= effective.max_paid_attempts:
                    legs.append(ChainLeg(
                        tier=tier_id, target=target, outcome="refused", paid=True,
                        attempt=attempts, paid_attempt=paid_attempts, model=model,
                        model_source=source, estimated_usd=estimated,
                        reason=(f"paid attempt cap reached "
                                f"({effective.max_paid_attempts}); the chain may "
                                f"not buy a third generation")))
                    continue
                if target.is_paid and not effective.authorises_amount(
                        effective.additional_budget - exposed
                        if effective.additional_budget is not None else 0.0):
                    legs.append(ChainLeg(
                        tier=tier_id, target=target, outcome="refused", paid=True,
                        attempt=attempts, paid_attempt=paid_attempts, model=model,
                        model_source=source, estimated_usd=estimated,
                        reason=(f"estimated ${estimated:.4f} exceeds the "
                                f"remaining additional budget authorised by "
                                f"{effective.approver or '<unnamed>'} "
                                f"(${exposed:.4f} of "
                                f"${effective.additional_budget} already at "
                                f"risk)")))
                    continue
            if attempts >= effective.max_attempts:
                stop_reason = "attempt_cap_reached"
                legs.append(ChainLeg(
                    tier=tier_id, target=target, outcome="refused", paid=target.is_paid,
                    attempt=attempts, paid_attempt=paid_attempts, model=model,
                    model_source=source, estimated_usd=estimated,
                    reason=f"attempt cap reached ({effective.max_attempts})"))
                break

            attempts += 1
            previous_target = target
            paid_attempt = 0
            if target.is_paid:
                paid_attempts += 1
                paid_attempt = paid_attempts
            try:
                if target.is_paid:
                    result = llm_mod.complete(
                        system,
                        user,
                        workspace_id=bill_to,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_mode=json_mode,
                        mock_fn=mock_fn,
                    )
                else:
                    result = self._run_local(tier_id, model, system, user)
            except Exception as exc:  # noqa: BLE001 - classified, never guessed
                failure = classify_leg_failure(exc)
                if not target.is_paid and failure is FailureClass.UNCLASSIFIED:
                    # A local leg failed in a way only the local runner can
                    # describe. Calling it "local unavailable" is the honest
                    # label: it cost nothing, and it must not be mistaken for a
                    # gateway problem.
                    failure = FailureClass.LOCAL_UNAVAILABLE
                if target.is_paid and failure.may_have_been_billed:
                    exposed += estimated
                self.report_failure(entry.provider)
                errors.append(f"{tier_id}: {failure}: {type(exc).__name__}: {exc}")
                permitted = (effective.may_fall_through(failure)
                             or effective.permits_uncertain_spend(failure))
                if effective.may_fall_through(failure):
                    why = "proven safe to fall through"
                else:
                    who = "authorised" if permitted else "refused"
                    why = f"not proven safe; policy {who} advancing"
                if failure is FailureClass.BUDGET_REFUSED:
                    # Nothing was sent, so nothing may have been billed. This
                    # is NOT an ambiguous exposure and must not be raised as
                    # one; walking to a cheaper tier under the same exhausted
                    # cap would be the multiplication this policy exists to
                    # stop.
                    stop_reason = "budget_refused"
                    legs.append(ChainLeg(
                        tier=tier_id, target=target, outcome=str(failure),
                        paid=target.is_paid, attempt=attempts,
                        paid_attempt=paid_attempt, model=model,
                        model_source=source, estimated_usd=estimated,
                        fell_through=False,
                        reason=f"{failure}: {redact_secrets(str(exc))}"))
                    self._record_chain(request, effective, legs, stop_reason,
                                       paid_attempts, exposed, "")
                    raise LLMRoutingError(
                        f"chain stopped at tier {tier_id}: the pre-spend budget "
                        f"gate refused this leg, so nothing was sent: {exc}"
                    ) from exc
                legs.append(ChainLeg(
                    tier=tier_id, target=target, outcome=str(failure),
                    paid=target.is_paid, attempt=attempts,
                    paid_attempt=paid_attempt, model=model,
                    model_source=source, estimated_usd=estimated,
                    fell_through=permitted,
                    reason=f"{failure} is {why}"))
                if permitted:
                    continue
                stop_reason = f"unsafe_failure:{failure}"
                self._record_chain(request, effective, legs, stop_reason,
                                   paid_attempts, exposed, "")
                raise AmbiguousLegStopped(
                    f"chain stopped at tier {tier_id}: {failure}. "
                    + (f"{len(errors)} leg(s) failed; the last one may already "
                       f"have been billed."
                       if failure.may_have_been_billed
                       else f"legs failed: {'; '.join(errors)}"),
                    tier=tier_id, failure_class=failure,
                    submission_id=_submission_id(exc),
                ) from exc
            self.report_success(entry.provider)
            legs.append(ChainLeg(
                tier=tier_id, target=target, outcome="succeeded", paid=target.is_paid,
                attempt=attempts, paid_attempt=paid_attempt, model=model,
                model_source=source, estimated_usd=estimated,
                reason="completed"))
            self._record_chain(request, effective, legs, "succeeded",
                               paid_attempts, exposed, tier_id)
            return result

        stop_reason = stop_reason if stop_reason != "chain_exhausted" else "no_leg_ran"
        self._record_chain(request, effective, legs, stop_reason, paid_attempts,
                           exposed, "")
        raise LLMRoutingError(f"all routed models failed: {'; '.join(errors)}")

    def _record_chain(self, request: RouteRequest, policy: FallbackPolicy,
                      legs: list, stop_reason: str, paid_legs: int,
                      exposed: float, succeeded_tier: str) -> None:
        """Write down what this chain cost and why it stopped.

        The routing log answers "what was picked". This answers "how many paid
        legs actually left, and what authorised each one", which is the question
        a bill arrives asking.
        """
        record = ChainRecord(
            workspace_id=request.workspace_id,
            task_type=request.task_type,
            policy=policy.to_dict(),
            legs=[leg.to_dict() for leg in legs],
            stop_reason=stop_reason,
            paid_legs=paid_legs,
            estimated_exposure_usd=round(float(exposed), 6),
            at=datetime.now(UTC).isoformat(),
            succeeded_tier=succeeded_tier,
        )
        self._chain_log.append(redact_secrets(record.to_dict()))

    def chain_log(self, workspace_id: str | None = None) -> list[dict]:
        """Every chain this router walked, newest last."""
        entries = list(self._chain_log)
        if workspace_id:
            entries = [e for e in entries if e.get("workspace_id") == workspace_id]
        return [redact_secrets(dict(e)) for e in entries]

    def _remote_allowed_for(self, request: RouteRequest) -> bool:
        settings = get_intelligence_settings(request.workspace_settings)
        privacy_mode = str(settings.get("privacy_mode") or "standard").lower()
        return bool(settings.get("remote_allowed", True)) and privacy_mode == "standard"


_DEFAULT_ROUTER = ModelRouter()


def default_router() -> ModelRouter:
    return _DEFAULT_ROUTER


def routing_log(workspace_id: str | None = None) -> list[dict]:
    return _DEFAULT_ROUTER.log(workspace_id)


def chain_log(workspace_id: str | None = None) -> list[dict]:
    """Every chain the default router walked, with its effective policy."""
    return _DEFAULT_ROUTER.chain_log(workspace_id)


def _submission_id(exc: BaseException) -> str:
    """The paid-submission id behind a chain failure, when there is one.

    Walks the same wrapped chain the classifier walks, because the leg error
    the router sees is the outermost wrapper. An operator handed this id can
    find the row that says whether the money is gone.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        record = getattr(current, "submission", None)
        if record is not None and getattr(record, "submission_id", ""):
            return str(record.submission_id)
        current = current.__cause__ or current.__context__
    return ""


def router_health() -> dict[str, bool]:
    return _DEFAULT_ROUTER.health()
