"""LLM decision provider: remote model via existing llm.py + Lane B ModelRouter.

Both dependencies are imported lazily so this module (and the whole
DecisionEngine) keeps working when Lane B is absent or no LLM credentials
exist — the provider simply reports UNAVAILABLE and the engine falls back
to deterministic. Missing credentials never raise at startup.
"""

from __future__ import annotations

import time
from typing import Any

from app.engine.intelligence.providers.base import (
    ALL_KINDS,
    BaseDecisionProvider,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
)

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

    def _router_model(self) -> str:
        """Ask Lane B's ModelRouter for a model; empty string when absent."""
        for module_name in (
            "app.engine.intelligence.model_router",
            "app.engine.intelligence.router",
            "app.engine.intelligence.routing",
        ):
            try:
                module = __import__(module_name, fromlist=["x"])
                router_cls = getattr(module, "ModelRouter", None)
                if router_cls is not None:
                    router = router_cls(self.workspace_id) if self.workspace_id else router_cls()
                    pick = getattr(router, "pick", None) or getattr(router, "route", None)
                    if callable(pick):
                        try:
                            chosen = pick("decision") or pick()
                        except TypeError:
                            chosen = pick()
                        if isinstance(chosen, dict):
                            return str(chosen.get("model", "") or "")
                        if chosen:
                            return str(chosen)
            except Exception:
                continue
        return ""

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

        model = self.model or self._router_model()
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
                **({"model": model} if model else {}),
            )
        except Exception as exc:
            raise ProviderUnavailable(f"LLM call failed: {type(exc).__name__}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(parsed, dict):
            raise ProviderUnavailable("LLM returned non-JSON output")
        return ProviderResult(output=parsed, model=model or "llm-default",
                              latency_ms=latency_ms)
