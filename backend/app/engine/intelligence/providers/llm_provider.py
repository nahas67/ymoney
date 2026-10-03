"""LLM decision provider: remote model via existing llm.py + Lane B ModelRouter.

Both dependencies are imported lazily so this module (and the whole
DecisionEngine) keeps working when Lane B is absent or no LLM credentials
exist — the provider simply reports UNAVAILABLE and the engine falls back
to deterministic. Missing credentials never raise at startup.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from app.engine.intelligence.providers.base import (
    ALL_KINDS,
    BaseDecisionProvider,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
)
from app.engine.intelligence.router import ExecutionTarget

_KIND_PROMPTS = {
    "boolean": "Answer with JSON {\"value\": <true|false>, \"reason\": string}.",
    "choose": "Answer with JSON {\"index\": <int>, \"reason\": string}.",
    "rank": "Answer with JSON {\"order\": [<int>...], \"reason\": string}.",
    "rerank": "Answer with JSON {\"order\": [<int>...], \"reason\": string}.",
    "score": "Answer with JSON {\"score\": <0-100>, \"reason\": string}.",
    "classify": "Answer with JSON {\"label\": string, \"reason\": string}.",
    "compare": "Answer with JSON {\"winner\": \"a\"|\"b\", \"reason\": string}.",
    "route": "Answer with JSON {\"route\": string, \"reason\": string}.",
    "verify": "Answer with JSON {\"status\": \"SUPPORTED\"|\"PARTIAL\"|\"UNVERIFIED\", \"reason\": string}.",
}


class LLMDecisionProvider(BaseDecisionProvider):
    name = "llm"
    capabilities = ALL_KINDS

    def __init__(self, model: str = "", workspace_id: str = "") -> None:
        self.model = model
        self.workspace_id = workspace_id
        #: Why the router's answer was or was not usable. Kept so the DecisionEngine
        #: can say "the router picked X" instead of silently falling back.
        self.route_detail: str = ""

    def _router_decision(self):
        """Ask the ModelRouter for a decision, or ``None`` when it cannot answer.

        Work 15.8 §5. This used to be a silent no-op:

            router = router_cls(self.workspace_id)   # a STRING, not a registry

        ``ModelRouter.__init__`` takes a ``ModelCapabilityRegistry``, so the
        workspace id landed in ``self.registry``, ``route()`` raised
        ``AttributeError``, the bare ``except`` swallowed it, and
        ``_router_model()`` returned ``""`` on every call. The router has never
        actually chosen anything from here; every decision ran on whatever
        ``llm.complete`` defaulted to.

        So the router is now constructed properly, asked a real
        :class:`~app.engine.intelligence.router.RouteRequest`, and its answer is
        used -- with the chosen tier, target and model recorded in
        :attr:`route_detail` instead of being thrown away.
        """
        try:
            from app.engine.intelligence.router import (
                ModelCapabilityRegistry,
                ModelRouter,
                RouteRequest,
            )
        except Exception as exc:  # noqa: BLE001 - optional dependency
            logger.debug("llm decision provider: router module absent: {}", exc)
            return None
        try:
            router = ModelRouter(ModelCapabilityRegistry())
            decision = router.route(RouteRequest(
                task_type="decision",
                # A decision primitive is a small, structured, latency-bound
                # call -- that is what FAST is for, and asking for it is what
                # makes the router's choice observable.
                latency_sensitive=True,
                complexity=0.2,
                quality_required="standard",
                workspace_id=self.workspace_id,
            ))
        except Exception as exc:  # noqa: BLE001 - routing must not break a decision
            logger.debug("llm decision provider: router refused a decision: {}",
                         exc)
            self.route_detail = f"router_refused:{type(exc).__name__}"
            return None
        self.route_detail = (
            f"tier={decision.tier} target={decision.target} "
            f"model={decision.model or '<none>'} source={decision.model_source} "
            f"cost={decision.relative_cost:.1f}x")
        return decision

    def _router_model(self) -> str:
        """The concrete REMOTE model the router chose, or ``""``.

        A LOCAL target yields ``""`` on purpose: this provider talks to the
        configured gateway, and handing a local alias to a gateway is the leak
        Work 15.8 §3 closed. ``run`` then reports the decision unavailable
        rather than sending a non-provider identifier over the wire.
        """
        decision = self._router_decision()
        if decision is None:
            return ""
        if decision.target is not ExecutionTarget.REMOTE:
            logger.debug("llm decision provider: router chose a {} target; "
                         "no gateway call is made for it", decision.target)
            return ""
        if not decision.resolved:
            logger.warning(
                "llm decision provider: router chose tier {} but resolved no "
                "provider model ({}); refusing to let the provider substitute "
                "its own default for a {}x cost tier",
                decision.tier, decision.model_source, decision.relative_cost)
            return ""
        return decision.model

    def health(self) -> ProviderHealth:
        try:
            from app.providers import llm as llm_mod
        except Exception:
            return ProviderHealth(status="UNAVAILABLE", detail="llm provider module missing")
        try:
            if not llm_mod.llm_available():
                return ProviderHealth(status="UNAVAILABLE", detail="no LLM credentials configured")
        except Exception as exc:
            return ProviderHealth(status="UNAVAILABLE", detail=f"availability check failed: {exc}")
        return ProviderHealth(status="AVAILABLE", detail="LLM endpoint configured")

    def run(self, kind: str, payload: dict) -> ProviderResult:
        self._require(kind)
        try:
            from app.providers import llm as llm_mod
        except Exception as exc:
            raise ProviderUnavailable(f"llm module missing: {exc}") from exc
        try:
            available = llm_mod.llm_available()
        except Exception:
            available = False
        if not available:
            raise ProviderUnavailable("no LLM credentials configured")
        import json as _json

        model = self.model
        if not model:
            decision = self._router_decision()
            if decision is not None:
                if decision.target is not ExecutionTarget.REMOTE:
                    raise ProviderUnavailable(
                        f"the router routed this decision to a "
                        f"{decision.target} target and this provider only "
                        f"serves the configured gateway")
                if not decision.resolved:
                    raise ProviderUnavailable(
                        f"the router chose tier {decision.tier} "
                        f"({decision.relative_cost:.1f}x baseline) but resolved "
                        f"no provider model ({decision.model_source}); refusing "
                        f"to substitute the provider default")
                model = decision.model
        system = (
            "You are a precise decision assistant for a video-content OS. "
            "Reply with JSON only. " + _KIND_PROMPTS[kind]
        )
        started = time.monotonic()
        try:
            parsed: Any = llm_mod.complete_json(
                system=system,
                user=_json.dumps({"kind": kind, "input": payload}, default=str)[:6000],
                workspace_id=self.workspace_id,
                tier="cheap",
                temperature=0.2,
                max_tokens=400,
                # The router's choice is explicit, so llm.complete cannot
                # substitute a default behind our back. ``tier`` stays as the
                # fallback for the case where no decision was reachable.
                **({"model": model} if model else {}),
            )
        except Exception as exc:
            raise ProviderUnavailable(f"LLM call failed: {type(exc).__name__}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(parsed, dict):
            raise ProviderUnavailable("LLM returned non-JSON output")
        return ProviderResult(output=parsed, model=model or "llm-default",
                              latency_ms=latency_ms)
