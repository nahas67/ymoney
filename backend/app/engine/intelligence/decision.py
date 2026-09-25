"""Typed DecisionEngine with provider fallback, modes and audit records.

Enforcement boundary (read before integrating): this engine only ADVISES.
Auth/RBAC, workspace isolation, budgets, publish authorization, compliance
gates, idempotency and DB invariants are enforced by the existing code paths
that call the engine — never by the engine itself. A PRIMARY-mode suggestion
that violates a deterministic guard (diversity overlap caps, QC thresholds,
master-first publish dependencies) is discarded by the caller; see
:mod:`app.engine.intelligence.integrations` for the advisory-only call sites.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from typing import Any

MODES = ("DISABLED", "SHADOW", "ASSISTED", "PRIMARY")
DEFAULT_MODE = "SHADOW"

PROVIDERS = ("deterministic", "local", "llm", "claude")

_INTELLIGENCE_DEFAULTS = {
    "decision_mode": DEFAULT_MODE,
    "provider_preference": "deterministic",
    "remote_allowed": False,
    "context_filtering": True,
    "browser_enabled": False,
    "allowed_domains": [],
    "routing_strategy": "cheap-first",
    "privacy_mode": "standard",
}


class DecisionMode:
    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    ASSISTED = "ASSISTED"
    PRIMARY = "PRIMARY"


@dataclass
class DecisionRecord:
    """In-memory audit record for one decision call."""

    requested_provider: str = "deterministic"
    actual_provider: str = "deterministic"
    model: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    fallback_reason: str = ""
    mode: str = DEFAULT_MODE
    output: Any = None
    shadow: bool = False
    kind: str = ""
    agreement: bool | None = None


def get_intelligence_settings(workspace_or_settings: Any) -> dict:
    """Merged intelligence settings with safe defaults.

    Accepts a Workspace row, a settings_json dict, or None. Unknown modes
    fall back to SHADOW; unknown providers fall back to deterministic.
    """
    raw: dict = {}
    try:
        if workspace_or_settings is None:
            raw = {}
        elif isinstance(workspace_or_settings, dict):
            settings_json = workspace_or_settings
            raw = settings_json.get("intelligence", {}) or {}
            if not raw and "decision_mode" in settings_json:
                raw = settings_json
        else:
            settings_json = getattr(workspace_or_settings, "settings_json", None) or {}
            raw = settings_json.get("intelligence", {}) or {}
    except Exception:
        raw = {}
    merged = dict(_INTELLIGENCE_DEFAULTS)
    if isinstance(raw, dict):
        for key in merged:
            if raw.get(key) is not None:
                merged[key] = raw[key]
    if merged["decision_mode"] not in MODES:
        merged["decision_mode"] = DEFAULT_MODE
    if merged["provider_preference"] not in PROVIDERS:
        merged["provider_preference"] = "deterministic"
    return merged


def _sanitize(value: Any, *, _depth: int = 0) -> Any:
    """Redact secrets from logged inputs. Prefers Lane B's helper when present."""
    try:
        from app.engine.intelligence.sanitize import redact_secrets as _lane_fn

        return _lane_fn(value)
    except Exception:
        pass
    return _fallback_sanitize(value, _depth=_depth)


_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|passwd| bearer |authorization)", re.I)
_SECRET_VALUE_PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{8,}"),
        re.compile(r"ym_[A-Za-z0-9_-]{8,}"),
        re.compile(r"xox[bap]-[A-Za-z0-9-]{8,}"),
    )


def _fallback_sanitize(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "[REDACTED]"
            else:
                out[k] = _fallback_sanitize(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_fallback_sanitize(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_VALUE_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        if len(redacted) > 2000:
            redacted = redacted[:2000] + "[truncated]"
        return redacted
    return value


def _outputs_agree(kind: str, baseline: Any, candidate: Any) -> bool:
    try:
        if isinstance(baseline, dict) and isinstance(candidate, dict):
            decision_keys = ("value", "choice", "index", "label", "winner", "route", "status")
            shared = [k for k in decision_keys if k in baseline or k in candidate]
            # The decision itself must match; numeric scores use
            # provider-specific scales and are advisory only.
            if shared and any(baseline.get(k) != candidate.get(k) for k in shared):
                return False
            if "order" in baseline or "order" in candidate:
                return list(baseline.get("order") or []) == list(candidate.get("order") or [])
            if not shared:
                if "score" in baseline and "score" in candidate:
                    try:
                        return abs(float(baseline["score"]) - float(candidate["score"])) < 5.0
                    except (TypeError, ValueError):
                        return False
                return True
            return True
        return baseline == candidate
    except Exception:
        return False


class DecisionEngine:
    """Typed decision facade: boolean/choose/rank/rerank/score/classify/compare/route/verify."""

    def __init__(
        self,
        workspace_id: str = "",
        *,
        mode: str = DEFAULT_MODE,
        provider_preference: str = "deterministic",
        persist: bool = True,
        settings: dict | None = None,
    ) -> None:
        if settings:
            merged = get_intelligence_settings(settings)
            mode = settings.get("decision_mode", mode) if "decision_mode" in settings else merged["decision_mode"]
            provider_preference = merged["provider_preference"]
        self.workspace_id = workspace_id
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.provider_preference = provider_preference if provider_preference in PROVIDERS else "deterministic"
        self.persist = persist
        self._records: list[DecisionRecord] = []

    # -- provider resolution ---------------------------------------------

    def _provider(self, name: str):
        from app.engine.intelligence.providers import (
            ClaudeDecisionProvider,
            DeterministicProvider,
            LLMDecisionProvider,
            LocalHeuristicProvider,
        )

        if name == "local":
            return LocalHeuristicProvider()
        if name == "llm":
            return LLMDecisionProvider(workspace_id=self.workspace_id)
        if name == "claude":
            return ClaudeDecisionProvider(workspace_id=self.workspace_id)
        return DeterministicProvider()

    def _chain(self, requested: str) -> list[str]:
        ordered = [requested] if requested in PROVIDERS else []
        for fallback in ("local", "deterministic"):
            if fallback not in ordered:
                ordered.append(fallback)
        return ordered

    # -- core -------------------------------------------------------------

    def decide(self, kind: str, payload: dict, *,
               provider: str = "", save: bool | None = None) -> tuple[Any, DecisionRecord]:
        """Run one decision; returns (output, record). Never raises for provider issues."""
        from app.engine.intelligence.providers.base import ProviderUnavailable

        requested = provider or self.provider_preference
        clean = _sanitize(dict(payload or {}))
        if self.mode == DecisionMode.DISABLED:
            return self._finish(kind, clean, requested, "deterministic", {}, save)

        output: Any = None
        actual = "deterministic"
        model = ""
        cost = 0.0
        latency_ms = 0
        fallback_reason = ""
        for name in self._chain(requested):
            candidate = self._provider(name)
            try:
                health = candidate.health()
            except Exception:
                health = None
            if health is not None and getattr(health, "status", "") == "UNAVAILABLE":
                if name == requested:
                    fallback_reason = f"{name}: {getattr(health, 'detail', 'unavailable')}"
                continue
            try:
                started = time.monotonic()
                result = candidate.run(kind, dict(payload or {}))
                latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
                output = result.output
                actual = candidate.name
                model = result.model
                cost = float(result.cost_usd or 0.0)
                break
            except ProviderUnavailable as exc:
                if name == requested:
                    fallback_reason = str(exc)[:300]
                continue
            except Exception as exc:  # noqa: BLE001 - providers must never break callers
                if name == requested:
                    fallback_reason = f"{type(exc).__name__}: {exc}"[:300]
                continue
        if output is None:
            # Last resort: deterministic directly (always available).
            try:
                result = self._provider("deterministic").run(kind, dict(payload or {}))
                output = result.output
                model = result.model
                if not fallback_reason:
                    fallback_reason = "all providers unavailable; deterministic fallback"
            except Exception as exc:  # noqa: BLE001 - absolute last resort
                output = {"error": f"decision failed: {type(exc).__name__}"}
                fallback_reason = str(exc)[:300]
        return self._finish(kind, clean, requested, actual, {
            "output": output, "model": model, "cost": cost,
            "latency_ms": latency_ms, "fallback_reason": fallback_reason,
        }, save)

    def _finish(self, kind: str, clean: dict, requested: str, actual: str,
                result: dict, save: bool | None) -> tuple[Any, DecisionRecord]:
        output = result.get("output")
        if output is None and actual == "deterministic" and self.mode == DecisionMode.DISABLED:
            from app.engine.intelligence.providers import DeterministicProvider

            started = time.monotonic()
            res = DeterministicProvider().run(kind, dict(clean))
            output = res.output
            result = {"output": output, "model": res.model, "cost": 0.0,
                      "latency_ms": int((time.monotonic() - started) * 1000),
                      "fallback_reason": ""}
        shadow = self.mode == DecisionMode.SHADOW and actual != "deterministic"
        agreement: bool | None = None
        if shadow:
            try:
                from app.engine.intelligence.providers import DeterministicProvider

                baseline = DeterministicProvider().run(kind, dict(clean)).output
                agreement = _outputs_agree(kind, baseline, output)
            except Exception:
                agreement = None
        record = DecisionRecord(
            requested_provider=requested,
            actual_provider=actual,
            model=result.get("model", ""),
            latency_ms=int(result.get("latency_ms", 0) or 0),
            cost_usd=float(result.get("cost", 0.0) or 0.0),
            fallback_reason=str(result.get("fallback_reason", "") or ""),
            mode=self.mode,
            output=output,
            shadow=shadow,
            kind=kind,
            agreement=agreement,
        )
        self._records.append(record)
        if (save if save is not None else self.persist) and self.workspace_id:
            with contextlib.suppress(Exception):
                self._persist(kind, clean, record)
        return output, record

    def _persist(self, kind: str, clean: dict, record: DecisionRecord) -> None:
        from app.db import session_scope
        from app.models.intelligence import DecisionRecordRow

        with session_scope() as s:
            s.add(DecisionRecordRow(
                workspace_id=self.workspace_id,
                kind=kind,
                mode=record.mode,
                requested_provider=record.requested_provider,
                actual_provider=record.actual_provider,
                model=record.model,
                latency_ms=record.latency_ms,
                cost_usd=record.cost_usd,
                fallback_reason=record.fallback_reason[:500],
                input_json=clean,
                output_json=_sanitize(record.output) if isinstance(record.output, dict) else {"value": str(record.output)[:2000]},
                agree=record.agreement,
                shadow_json={"shadow": record.shadow},
            ))
            s.flush()

    # -- typed primitives --------------------------------------------------

    def boolean(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("boolean", payload, **kw)

    def choose(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("choose", payload, **kw)

    def rank(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("rank", payload, **kw)

    def rerank(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("rerank", payload, **kw)

    def score(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("score", payload, **kw)

    def classify(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("classify", payload, **kw)

    def compare(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("compare", payload, **kw)

    def route(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("route", payload, **kw)

    def verify(self, payload: dict, **kw) -> tuple[Any, DecisionRecord]:
        return self.decide("verify", payload, **kw)

    # -- batch variants -----------------------------------------------------

    def _batch(self, kind: str, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        outputs: list[Any] = []
        records: list[DecisionRecord] = []
        for item in items or []:
            out, rec = self.decide(kind, item, **kw)
            outputs.append(out)
            records.append(rec)
        return outputs, records

    def boolean_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("boolean", items, **kw)

    def choose_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("choose", items, **kw)

    def rank_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("rank", items, **kw)

    def rerank_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("rerank", items, **kw)

    def score_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("score", items, **kw)

    def classify_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("classify", items, **kw)

    def compare_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("compare", items, **kw)

    def route_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("route", items, **kw)

    def verify_batch(self, items: list[dict], **kw) -> tuple[list[Any], list[DecisionRecord]]:
        return self._batch("verify", items, **kw)

    @property
    def records(self) -> list[DecisionRecord]:
        return list(self._records)


__all__ = [
    "DEFAULT_MODE",
    "DecisionEngine",
    "DecisionMode",
    "DecisionRecord",
    "MODES",
    "PROVIDERS",
    "get_intelligence_settings",
]
