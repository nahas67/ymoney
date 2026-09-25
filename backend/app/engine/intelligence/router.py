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
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

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
}


class LLMRoutingError(Exception):
    """Raised when no routed model can serve a request."""


class PrivacyRefusal(LLMRoutingError):
    """Raised when a request would force a remote pathway under a local-only
    privacy constraint. Never silently fall back to remote."""


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
    return merged


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
    """Capability entries plus per-slot health and workspace overrides."""

    def __init__(self, capabilities: list[ModelCapability] | None = None):
        self._entries: dict[str, ModelCapability] = {
            c.tier: c for c in (capabilities or _default_capabilities())
        }
        self._health: dict[str, bool] = {REMOTE_SLOT: True, LOCAL_SLOT: True}

    def get(self, tier: str) -> ModelCapability | None:
        return self._entries.get((tier or "").upper())

    def all(self) -> list[ModelCapability]:
        return list(self._entries.values())

    def set_enabled(self, tier: str, enabled: bool) -> None:
        entry = self.get(tier)
        if entry is not None:
            entry.enabled = enabled

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

    def to_dict(self) -> dict:
        return asdict(self)


_LATENCY_RANK = {"low": 0, "medium": 1, "high": 2}
_QUALITY_RANK = {"standard": 0, "high": 1, "top": 2}


class ModelRouter:
    """Routes tasks to capability tiers; wraps the LLM provider (no fork)."""

    def __init__(self, registry: ModelCapabilityRegistry | None = None):
        self.registry = registry or ModelCapabilityRegistry()
        self._log: deque = deque(maxlen=_ROUTING_LOG_MAX)

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
        healthy = [c for c in ordered if self.registry.is_healthy(c.provider)]
        degraded = not healthy
        picked = healthy[0] if healthy else ordered[0]
        fallbacks = [c.tier for c in ordered if c.tier != picked.tier]

        model = self._resolve_model(picked.tier, settings)
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
        )
        self._record(request, decision)
        return decision

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

    def _resolve_model(self, tier: str, settings: dict) -> str:
        """Concrete model name for a tier. Never a hardcoded vendor literal."""
        override = (settings.get("models") or {}).get(tier.lower())
        if override:
            return str(override)
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
                return str(name)
        except Exception:
            pass
        try:
            from app.core.config import settings as env_settings

            if tier in REMOTE_TIERS and getattr(env_settings, "llm_model", ""):
                return str(env_settings.llm_model)
        except Exception:
            pass
        return "local" if tier in LOCAL_TIERS else ""

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
    ):
        """Route once, then try the selected tier and its fallbacks in order.

        Provider failures flip the slot unhealthy (dynamic reroute for later
        calls) before the next fallback is attempted. Raises
        :class:`LLMRoutingError` when every candidate fails.
        """
        from app.providers import llm as llm_mod

        request.workspace_id = request.workspace_id or workspace_id
        decision = self.route(request)
        chain = [decision.tier, *decision.fallbacks]
        errors: list[str] = []
        for tier_id in chain:
            entry = self.registry.get(tier_id)
            if entry is None or not entry.enabled:
                continue
            if entry.remote and not self._remote_allowed_for(request):
                continue
            model = self._resolve_model(
                tier_id, get_intelligence_settings(request.workspace_settings)
            )
            try:
                result = llm_mod.complete(
                    system,
                    user,
                    workspace_id=workspace_id,
                    model=model or None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    mock_fn=mock_fn,
                )
                self.report_success(entry.provider)
                return result
            except Exception as exc:  # noqa: BLE001 - fallback chain must survive
                self.report_failure(entry.provider)
                errors.append(f"{tier_id}: {type(exc).__name__}: {exc}")
        raise LLMRoutingError(f"all routed models failed: {'; '.join(errors)}")

    def _remote_allowed_for(self, request: RouteRequest) -> bool:
        settings = get_intelligence_settings(request.workspace_settings)
        privacy_mode = str(settings.get("privacy_mode") or "standard").lower()
        return bool(settings.get("remote_allowed", True)) and privacy_mode == "standard"


_DEFAULT_ROUTER = ModelRouter()


def default_router() -> ModelRouter:
    return _DEFAULT_ROUTER


def routing_log(workspace_id: str | None = None) -> list[dict]:
    return _DEFAULT_ROUTER.log(workspace_id)


def router_health() -> dict[str, bool]:
    return _DEFAULT_ROUTER.health()
