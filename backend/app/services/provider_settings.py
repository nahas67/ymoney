"""Provider credential management.

Credentials for external providers (LLM, Google OAuth client, Upload-Post) can
be configured at runtime through Settings → Connections. They are stored
AES-256-GCM encrypted in the api_credentials table and NEVER returned to the
client in plaintext — only masked previews and source attribution.

Resolution order: database (encrypted) -> environment -> None.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import settings as env_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.db import session_scope
from app.models import ApiCredential

# provider.key -> (description, env fallback attr or None)
# Provider calls run in queued worker threads as well as request threads. Keep
# the active workspace in a context variable so low-level providers can resolve
# tenant credentials without threading workspace IDs through every third-party
# interface. Explicit workspace_id arguments always take precedence.
_active_workspace: ContextVar[str | None] = ContextVar("ymoney_workspace_id", default=None)


def workspace_context(workspace_id: str | None):
    """Set the workspace used by credential lookups for the current context."""
    return _active_workspace.set(workspace_id or None)


def reset_workspace_context(token) -> None:
    _active_workspace.reset(token)


@contextmanager
def workspace_scope(workspace_id: str | None):
    """Temporarily scope provider resolution to a workspace."""
    token = workspace_context(workspace_id)
    try:
        yield
    finally:
        reset_workspace_context(token)


def current_workspace_id() -> str | None:
    return _active_workspace.get()


REGISTRY: dict[str, dict] = {
    "llm.api_key": {"label": "LLM API key", "secret": True, "env": "openai_api_key"},
    "llm.base_url": {"label": "LLM base URL", "secret": False, "env": "openai_base_url"},
    "llm.model": {"label": "LLM model", "secret": False, "env": "llm_model"},
    "google.client_id": {"label": "Google OAuth client ID", "secret": False, "env": None},
    "google.client_secret": {"label": "Google OAuth client secret", "secret": True, "env": None},
    "tiktok.client_key": {"label": "TikTok app client key", "secret": False, "env": None},
    "tiktok.client_secret": {"label": "TikTok app client secret", "secret": True, "env": None},
    "meta.app_id": {"label": "Meta app ID (Facebook/Instagram)", "secret": False, "env": None},
    "meta.app_secret": {"label": "Meta app secret", "secret": True, "env": None},
    "upload_post.api_key": {"label": "Upload-Post API key", "secret": True, "env": "upload_post_api_key"},
    "upload_post.username": {"label": "Upload-Post profile username", "secret": False, "env": "upload_post_username"},
    "engine.base_url": {"label": "Video engine base URL", "secret": False, "env": "mpt_base_url"},
    "engine.timeout_seconds": {"label": "Video engine timeout (s)", "secret": False, "env": "mpt_timeout_seconds"},
    "tts.kokoro_base_url": {"label": "Kokoro TTS server URL", "secret": False, "env": "kokoro_base_url"},
    "tts.kokoro_api_key": {"label": "Kokoro TTS API key (optional)", "secret": True, "env": None},
    "tts.provider": {"label": "TTS provider (edge|kokoro|chatterbox|qwen3|mock)", "secret": False, "env": "tts_provider"},
    "tts.chatterbox_base_url": {"label": "Chatterbox server URL (else native pip package)", "secret": False, "env": "chatterbox_base_url"},
    "tts.qwen_base_url": {"label": "Qwen3-TTS server URL (vLLM-Omni)", "secret": False, "env": "qwen_base_url"},
    "tts.qwen_instruct": {"label": "Qwen3 delivery direction (e.g. speak cheerfully)", "secret": False, "env": "qwen_tts_instruct"},
    "tts.qwen_api_key": {"label": "Qwen3-TTS API key (optional)", "secret": True, "env": None},
    "avatar.backend": {"label": "Avatar backend (server|sadtalker|mock)", "secret": False, "env": "avatar_backend"},
    "avatar.base_url": {"label": "Avatar renderer URL (multipart image+audio → mp4)", "secret": False, "env": "avatar_base_url"},
    "avatar.sadtalker_dir": {"label": "SadTalker checkout dir (with checkpoints)", "secret": False, "env": "sadtalker_dir"},
    "broll.ai_backend": {"label": "AI B-roll backend (server|wan|ltx|synth)", "secret": False, "env": "broll_ai_backend"},
    "broll.ai_base_url": {"label": "AI B-roll renderer URL ({prompt,seconds,aspect} → mp4)", "secret": False, "env": "broll_ai_base_url"},
    "image.openai_base_url": {"label": "OpenAI-compatible image API base URL", "secret": False, "env": None},
    "image.openai_api_key": {"label": "OpenAI-compatible image API key", "secret": True, "env": None},
    "image.openai_model": {"label": "Image model name (optional)", "secret": False, "env": None},
    "pexels.api_key": {"label": "Pexels API key (stock photos)", "secret": True, "env": "PEXELS_API_KEY"},
    "newsdata.api_key": {"label": "NewsData.io API key (news trend source)", "secret": True, "env": "NEWSDATA_API_KEY"},
    "coingecko.api_key": {"label": "CoinGecko demo key (optional, higher rate limits)", "secret": True, "env": "COINGECKO_API_KEY"},
    "telegram.bot_token": {"label": "Telegram bot token (from @BotFather)", "secret": True, "env": "telegram_bot_token"},
    # Model-tier routing
    "llm.model_cheap": {"label": "Cheap-tier model (discovery/metadata)", "secret": False, "env": "research_model_cheap"},
    "llm.model_reasoning": {"label": "Reasoning-tier model (strategy/scripts)", "secret": False, "env": "research_model_reasoning"},
    "llm.model_verification": {"label": "Verification-tier model (QC/fact-check)", "secret": False, "env": "verification_model"},
}


def get_credential(key: str, workspace_id: str | None = None) -> tuple[str | None, str]:
    """Return ``(value, source)`` for one workspace without cross-tenant reads.

    Workspace credentials are preferred over legacy/global rows. A global row
    (``workspace_id IS NULL``) is only used when no workspace-specific row
    exists; this preserves environment/admin configuration while preventing a
    credential saved in workspace A from being returned to workspace B.
    """
    spec = REGISTRY.get(key)
    if not spec:
        raise KeyError(f"unknown credential key {key}")
    workspace_id = workspace_id or current_workspace_id()
    with session_scope() as s:
        row = None
        if workspace_id:
            rows = s.scalars(
                select(ApiCredential)
                .where(
                    ApiCredential.provider == key,
                    ApiCredential.workspace_id == workspace_id,
                )
                .order_by(ApiCredential.updated_at.desc(), ApiCredential.created_at.desc())
            ).all()
            row = rows[0] if rows else None
        if row is None:
            rows = s.scalars(
                select(ApiCredential)
                .where(
                    ApiCredential.provider == key,
                    ApiCredential.workspace_id.is_(None),
                )
                .order_by(ApiCredential.updated_at.desc(), ApiCredential.created_at.desc())
            ).all()
            row = rows[0] if rows else None
        if row and row.value_enc:
            try:
                return decrypt_secret(row.value_enc), "db"
            except ValueError:
                pass  # corrupt/rotated key — fall through to env
    env_attr = spec.get("env")
    if env_attr:
        val = getattr(env_settings, env_attr, "") or ""
        if val:
            return val, "env"
    return None, "none"


def set_credential(key: str, value: str | None, workspace_id: str | None = None) -> None:
    """Store/clear an encrypted credential for one workspace.

    ``workspace_id=None`` intentionally targets the legacy global/system
    scope, used by tests and process-wide Telegram configuration. Workspace
    APIs must pass their authorized workspace explicitly.
    """
    if key not in REGISTRY:
        raise KeyError(f"unknown credential key {key}")
    workspace_id = workspace_id or current_workspace_id()

    def _apply(s) -> None:
        rows = s.scalars(
            select(ApiCredential)
            .where(
                ApiCredential.provider == key,
                ApiCredential.workspace_id == workspace_id,
            )
            .order_by(ApiCredential.updated_at.desc(), ApiCredential.created_at.desc())
        ).all()
        row = rows[0] if rows else None
        # Migration 0008 plus this collapse keep one row per (provider, scope);
        # older rows are removed whenever a credential is touched so reads are
        # deterministic even before the migration runs.
        for duplicate in rows[1:]:
            s.delete(duplicate)
        if not value:
            if row:
                s.delete(row)
            return
        enc = encrypt_secret(value)
        if row:
            row.value_enc = enc
        else:
            s.add(
                ApiCredential(
                    workspace_id=workspace_id,
                    provider=key,
                    name=REGISTRY[key]["label"],
                    value_enc=enc,
                )
            )

    try:
        with session_scope() as s:
            _apply(s)
    except IntegrityError:
        # Two writers raced the select-then-insert. The unique indexes from
        # migration 0008 let exactly one commit; the loser re-reads the winner's
        # row and updates it in place instead of failing the request.
        with session_scope() as s:
            _apply(s)


def effective_llm(workspace_id: str | None = None) -> dict:
    """Effective LLM configuration honoring DB overrides over env."""
    key, key_src = get_credential("llm.api_key", workspace_id)
    base, base_src = get_credential("llm.base_url", workspace_id)
    model, model_src = get_credential("llm.model", workspace_id)
    cheap, _ = get_credential("llm.model_cheap", workspace_id)
    reasoning, _ = get_credential("llm.model_reasoning", workspace_id)
    verification, _ = get_credential("llm.model_verification", workspace_id)
    return {
        "api_key": key,
        "base_url": base,
        "model": model,
        "configured": bool(key),
        "mock": env_settings.mock_llm,
        "sources": {"api_key": key_src, "base_url": base_src, "model": model_src},
        "tiers": {
            # tier -> model to use; empty string means "use default model"
            "cheap": cheap or "",
            "reasoning": reasoning or "",
            "verification": verification or "",
            "default": model or "",
        },
    }


def google_oauth_client(workspace_id: str | None = None) -> tuple[str | None, str | None]:
    cid, _ = get_credential("google.client_id", workspace_id)
    secret, _ = get_credential("google.client_secret", workspace_id)
    return cid, secret


def upload_post_config(workspace_id: str | None = None) -> tuple[str | None, str | None]:
    key, _ = get_credential("upload_post.api_key", workspace_id)
    user, _ = get_credential("upload_post.username", workspace_id)
    # fall back to env-backed settings values
    return key or (env_settings.upload_post_api_key or None), user or (
        env_settings.upload_post_username or None
    )


def mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "••••"
    return value[:4] + "••••" + value[-4:]
