"""Decision provider interface: capabilities + health, never startup failure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProviderUnavailable(Exception):
    """Raised when a provider cannot serve (missing creds, offline, error)."""


@dataclass
class ProviderHealth:
    status: str  # AVAILABLE | UNAVAILABLE | DEGRADED
    detail: str = ""


@dataclass
class ProviderResult:
    output: Any
    model: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0


class BaseDecisionProvider:
    """One decision backend. Subclasses implement :meth:`run`."""

    name: str = "base"
    capabilities: frozenset[str] = frozenset()

    def health(self) -> ProviderHealth:
        return ProviderHealth(status="AVAILABLE")

    def supports(self, kind: str) -> bool:
        return kind in self.capabilities

    def run(self, kind: str, payload: dict) -> ProviderResult:
        raise NotImplementedError

    # Optional per-primitive overrides; default routes through run().
    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} name={self.name}>"

    def _require(self, kind: str) -> None:
        if not self.supports(kind):
            raise ProviderUnavailable(f"provider '{self.name}' lacks capability '{kind}'")


ALL_KINDS: frozenset[str] = frozenset({
    "boolean", "choose", "rank", "rerank", "score",
    "classify", "compare", "route", "verify",
})


@dataclass
class ShadowComparison:
    agree: bool
    baseline: Any = None
    candidate: Any = None
    note: str = ""
    extra: dict = field(default_factory=dict)
