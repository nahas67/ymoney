"""Claude decision provider: credential-gated stub (Work 05 evaluation).

OSS evaluation (READ-ONLY review of READMEs/LICENSEs, no dependencies added):
- browser-use/claude-ultrafast — read-only browser research fallback. Requires
  Jev credentials; never for platform login where an API exists.
- jkudish/claude-mcp vs itsmostafa/typesafe-mcp — evaluate both, choose ONE.
  Dev-tooling surface; the backend keeps the native DecisionEngine interface.

Verdict recorded in docs/oss/OSS_COMPONENTS.md. Until credentials are
configured this provider reports UNAVAILABLE and the engine falls back to
deterministic — missing creds never fail startup or any decision call.
"""

from __future__ import annotations

import os

from app.engine.intelligence.providers.base import (
    ALL_KINDS,
    BaseDecisionProvider,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
)

_CANDIDATE_ENV_VARS = (
    "CLAUDE_API_KEY",
    "CLAUDE_MCP_URL",
    "CLAUDE_ULTRAFAST_URL",
    "BROWSER_USE_API_KEY",
)


class ClaudeDecisionProvider(BaseDecisionProvider):
    """Stub behind credentials. Returns UNAVAILABLE until configured."""

    name = "claude"
    capabilities = ALL_KINDS

    def __init__(self, workspace_id: str = "") -> None:
        self.workspace_id = workspace_id

    def _credential_present(self) -> bool:
        if any(os.environ.get(v) for v in _CANDIDATE_ENV_VARS):
            return True
        try:
            from app.services.provider_settings import get_credential

            for key in ("claude.api_key", "claude.mcp_url", "claude.base_url"):
                val, _src = get_credential(key)
                if val:
                    return True
        except Exception:
            pass
        return False

    def health(self) -> ProviderHealth:
        if not self._credential_present():
            return ProviderHealth(
                status="UNAVAILABLE",
                detail="no Claude/MCP credentials configured (see docs/oss/OSS_COMPONENTS.md)",
            )
        return ProviderHealth(status="DEGRADED",
                              detail="credential present; remote MCP path not yet wired")

    def run(self, kind: str, payload: dict) -> ProviderResult:
        self._require(kind)
        raise ProviderUnavailable(
            "Claude provider not wired — configure credentials and the MCP bridge first")
