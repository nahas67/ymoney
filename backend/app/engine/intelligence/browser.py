"""Research-FIRST browser agent (Lane C, Work 05).

Never publishes. Typed actions (navigate/click/type/select/scroll/extract/
back/wait) execute against a pluggable ``BrowserBackend``. The shipped
``RecordingBackend`` is deterministic and scripted (used by tests); the
production seam is a backend implementing ``BrowserBackend`` over a real
browser driver (reference: claude-ultrafast evaluated via webfetch README —
no dependency, no import, no network path in this module).

Guards:
- domain allow/deny lists (deny wins; default-deny unless allowlisted),
- max_steps / timeout / cancellation token / cost limit,
- login, form_submit and purchase default to BLOCKED (attempts raise),
- pre-action state/target revalidation (stale -> abort with reason).

Evidence feeds the existing ResearchAgent brief shape
(``evidence_to_research_brief``) — no second research system.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse

try:  # Lane B's shared sanitizer; local fallback only when it is absent.
    from app.engine.intelligence.sanitize import redact_secrets as _shared_sanitize
except Exception:  # noqa: BLE001 — Lane B file may not exist yet
    _shared_sanitize = None  # type: ignore[assignment]

_FALLBACK_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9-_]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)(['\"]?)[A-Za-z0-9\-._~+/=]{8,}\2"),
)


def _sanitize(text: str) -> str:
    redacted = text or ""
    if _shared_sanitize is not None:
        try:
            out = _shared_sanitize(redacted)
            if isinstance(out, str):
                redacted = out
        except Exception:
            pass
    # Chain the minimal local patterns so obvious secret shapes are redacted
    # regardless of the shared sanitizer's coverage (Lane B wins where set).
    for pat in _FALLBACK_PATTERNS:
        redacted = pat.sub("[REDACTED]", redacted)
    return redacted


ACTION_KINDS = (
    "navigate", "click", "type", "select", "scroll", "extract", "back", "wait",
)

# Actions that are blocked by default; attempts raise BlockedActionError.
BLOCKED_BY_DEFAULT = ("login", "form_submit", "purchase")


class BrowserError(Exception):
    """Base browser-agent error."""


class BlockedActionError(BrowserError):
    """Raised when a blocked action (login/form_submit/purchase) is attempted."""


class StaleStateError(BrowserError):
    """Raised when pre-action state revalidation fails — abort with reason."""


class DomainDeniedError(BrowserError):
    """Raised when navigation targets a non-allowlisted or denied domain."""


class BudgetExceededError(BrowserError):
    """Raised when step/timeout/cost caps are hit."""


class CancelledError(BrowserError):
    """Raised when the cancellation token fires mid-run."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _domain_allowed(host: str, allowed: list[str], denied: list[str]) -> bool:
    host = (host or "").lower()
    for rule in denied or []:
        rule = rule.lower().strip()
        if rule and (host == rule or host.endswith("." + rule)):
            return False  # deny wins
    for rule in allowed or []:
        rule = rule.lower().strip()
        if rule and (host == rule or host.endswith("." + rule)):
            return True
    return False  # default-deny unless allowlisted


@dataclass
class BrowserAction:
    kind: str  # one of ACTION_KINDS (or a blocked kind -> raises)
    target: str = ""  # element id for click/type/select; url for navigate
    text: str = ""  # typed input / select value
    expected_state_hash: str | None = None  # stale revalidation token


@dataclass
class BrowserObservation:
    url: str = ""
    title: str = ""
    visible_text: str = ""
    interactive_elements: list[dict] = field(default_factory=list)
    structured_data: dict = field(default_factory=dict)
    state_hash: str = ""

    def compute_hash(self) -> str:
        payload = f"{self.url}|{self.title}|{self.visible_text}|{len(self.interactive_elements)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class BrowserPolicy:
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    max_steps: int = 20
    timeout_seconds: float = 120.0
    cost_limit_usd: float = 1.0
    step_cost_usd: float = 0.001
    blocked_actions: tuple[str, ...] = BLOCKED_BY_DEFAULT

    def check_domain(self, url: str) -> None:
        if not _domain_allowed(_host(url), self.allowed_domains, self.denied_domains):
            raise DomainDeniedError(f"domain not allowlisted (default-deny): {_host(url)}")

    def check_action_kind(self, kind: str) -> None:
        if kind in (self.blocked_actions or ()):
            raise BlockedActionError(f"action '{kind}' is blocked by policy")
        if kind not in ACTION_KINDS:
            raise BrowserError(f"unknown action kind: {kind}")


class BrowserBackend(Protocol):
    """Pluggable browser seam. Production backends implement this over a
    real driver (e.g. claude-ultrafast); tests use RecordingBackend."""

    def open(self, url: str) -> BrowserObservation:
        ...  # pragma: no cover

    def act(self, action: BrowserAction, current: BrowserObservation) -> BrowserObservation:
        ...  # pragma: no cover

    def close(self) -> None:
        ...  # pragma: no cover


class RecordingBackend:
    """Deterministic scripted backend for tests and offline runs.

    ``pages`` maps url -> {title, text, elements, data}. Unknown urls yield
    an empty deterministic page (never network, never an exception).
    """

    def __init__(self, pages: dict[str, dict] | None = None):
        self.pages = dict(pages or {})
        self.calls: list[dict] = []
        self.closed = False

    def _observe(self, url: str) -> BrowserObservation:
        page = self.pages.get(url, {})
        obs = BrowserObservation(
            url=url,
            title=str(page.get("title", url)),
            visible_text=_sanitize(str(page.get("text", ""))),
            interactive_elements=list(page.get("elements", [])),
            structured_data=dict(page.get("data", {})),
        )
        obs.state_hash = obs.compute_hash()
        return obs

    def open(self, url: str) -> BrowserObservation:
        self.calls.append({"kind": "navigate", "url": url})
        return self._observe(url)

    def act(self, action: BrowserAction, current: BrowserObservation) -> BrowserObservation:
        self.calls.append({"kind": action.kind, "target": action.target})
        if action.kind == "navigate":
            return self._observe(action.target)
        if action.kind == "back":
            return self._observe(current.url)
        # click/type/select/scroll/extract/wait re-observe current page;
        # extract merges the page's structured data into the observation.
        obs = self._observe(current.url)
        if action.kind == "extract" and action.target:
            data = dict(obs.structured_data)
            data.setdefault("extracts", []).append(action.target)
            obs.structured_data = data
            obs.state_hash = obs.compute_hash()
        return obs

    def close(self) -> None:
        self.closed = True


class CancellationToken:
    """Cooperative cancellation checked before every browser step."""

    def __init__(self):
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise CancelledError("browser run cancelled")


@dataclass
class BrowserEvidence:
    url: str = ""
    title: str = ""
    retrieved_at: str = ""
    extracts: list[dict] = field(default_factory=list)
    action_trace: list[dict] = field(default_factory=list)


class BrowserIntelligenceAgent:
    """Research-FIRST browsing agent: navigate + extract, never publish."""

    def __init__(self, policy: BrowserPolicy | None = None,
                 backend: BrowserBackend | None = None):
        self.policy = policy or BrowserPolicy()
        self.backend = backend or RecordingBackend()

    # -- single action with full guard chain --------------------------------
    def run_action(self, action: BrowserAction,
                   current: BrowserObservation | None) -> BrowserObservation:
        self.policy.check_action_kind(action.kind)
        if action.kind == "navigate":
            self.policy.check_domain(action.target)
        if (current is not None and action.expected_state_hash is not None
                and action.expected_state_hash != current.state_hash):
            raise StaleStateError(
                f"stale state: expected {action.expected_state_hash} "
                f"but page is {current.state_hash} — aborting"
            )
        if action.kind == "navigate":
            return self.backend.open(action.target)
        if current is None:
            raise BrowserError("no current observation for non-navigate action")
        return self.backend.act(action, current)

    # -- goal run ------------------------------------------------------------
    def run(self, goal: str, start_urls: list[str],
            cancel: CancellationToken | None = None) -> dict:
        policy = self.policy
        for url in start_urls:
            policy.check_domain(url)  # fail fast before spending budget
        deadline = time.monotonic() + policy.timeout_seconds
        cost = 0.0
        steps: list[dict] = []
        evidence = BrowserEvidence(retrieved_at=_utcnow_iso())
        current: BrowserObservation | None = None
        status = "COMPLETED"
        stop_reason = ""
        try:
            for url in start_urls:
                if cancel is not None:
                    cancel.check()
                if len(steps) >= policy.max_steps:
                    raise BudgetExceededError(f"max_steps exceeded ({policy.max_steps})")
                if time.monotonic() >= deadline:
                    raise BudgetExceededError("timeout exceeded")
                if cost + policy.step_cost_usd > policy.cost_limit_usd:
                    raise BudgetExceededError("cost limit exceeded")
                nav = BrowserAction(kind="navigate", target=url,
                                    expected_state_hash=current.state_hash if current else None)
                current = self.run_action(nav, current)
                cost += policy.step_cost_usd
                steps.append({"kind": "navigate", "url": url,
                              "state_hash": current.state_hash})
                if cancel is not None:
                    cancel.check()
                if len(steps) >= policy.max_steps:
                    raise BudgetExceededError(f"max_steps exceeded ({policy.max_steps})")
                ext = BrowserAction(kind="extract", target="main",
                                    expected_state_hash=current.state_hash)
                current = self.run_action(ext, current)
                cost += policy.step_cost_usd
                steps.append({"kind": "extract", "url": url,
                              "state_hash": current.state_hash})
                evidence.extracts.append({
                    "url": current.url,
                    "title": current.title,
                    "text": current.visible_text[:2000],
                    "structured_data": current.structured_data,
                })
            evidence.url = evidence.extracts[0]["url"] if evidence.extracts else ""
            evidence.title = evidence.extracts[0]["title"] if evidence.extracts else ""
        except (BudgetExceededError, CancelledError, StaleStateError,
                DomainDeniedError, BlockedActionError) as exc:
            status = "CANCELLED" if isinstance(exc, CancelledError) else "ABORTED"
            stop_reason = str(exc)
        finally:
            with contextlib.suppress(Exception):
                self.backend.close()
        evidence.action_trace = steps
        return {
            "goal": goal,
            "status": status,
            "stop_reason": stop_reason,
            "steps": steps,
            "cost_usd": round(cost, 6),
            "evidence": {
                "url": evidence.url,
                "title": evidence.title,
                "retrieved_at": evidence.retrieved_at,
                "extracts": evidence.extracts,
                "action_trace": evidence.action_trace,
            },
        }


def evidence_to_research_brief(evidence: dict, topic: str) -> dict:
    """Fold browser evidence into the existing ResearchAgent brief shape.

    Same keys as ResearchAgent.research output (summary/key_facts/angles/
    visual_keywords/cautions/claims/factual_confidence/fact_status) plus a
    ``sources`` provenance list. No second research system.
    """
    extracts = evidence.get("extracts", []) or []
    sources = [
        {"url": e.get("url", ""), "title": e.get("title", ""),
         "retrieved_at": evidence.get("retrieved_at", "")}
        for e in extracts
    ]
    key_facts = []
    for e in extracts[:5]:
        text = (e.get("text") or "").strip()
        if text:
            key_facts.append(f"[{e.get('title', e.get('url', ''))[:60]}] {text[:220]}")
    claims = [{
        "claim": f[:400],
        "status": "LIKELY",
        "confidence": 0.6,
        "basis": f"browser extract: {(extracts[i].get('url', ''))[:120]}",
    } for i, f in enumerate(key_facts[:5])]
    n = len(claims)
    factual_confidence = round(0.7 * 0.6 if n else 0.4, 2)
    fact_status = "INSUFFICIENT" if n == 0 else ("OK" if n >= 2 else "INSUFFICIENT")
    summary = f"Browser research on '{topic[:120]}': {n} sourced fact(s) from {len(sources)} page(s)."
    return {
        "summary": summary[:300],
        "key_facts": key_facts[:5],
        "angles": [f"Explain {topic[:80]} with sourced evidence"],
        "visual_keywords": [topic[:40] or "research"],
        "cautions": ["browser-sourced; verify before publishing"] if n else ["no sources retrieved"],
        "claims": claims,
        "factual_confidence": factual_confidence,
        "fact_status": fact_status,
        "sources": sources,
    }


def default_intelligence_settings() -> dict:
    """Defaults for Workspace.settings_json['intelligence'] (shared contract)."""
    return {
        "decision_mode": "auto",
        "provider_preference": "local",
        "remote_allowed": False,
        "context_filtering": True,
        "browser_enabled": True,
        "allowed_domains": [],
        "routing_strategy": "cheap_first",
        "privacy_mode": "STANDARD",
    }


def resolve_policy(settings: dict | None) -> BrowserPolicy:
    intel = dict(settings or {}).get("intelligence", {}) if settings else {}
    merged = {**default_intelligence_settings(), **intel}
    return BrowserPolicy(allowed_domains=list(merged.get("allowed_domains") or []))
