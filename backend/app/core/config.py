"""YMONEY runtime configuration.

All settings come from environment variables (optionally via a .env file).
Secrets are never hard-coded. See .env.example at the repository root.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent      # .../backend/app
PROJECT_ROOT = BACKEND_DIR.parent                          # .../backend


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT.parent / ".env"), str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Core ----
    ymoney_env: str = "development"
    secret_key: str = "change-me-to-a-long-random-string"
    host: str = "127.0.0.1"
    port: int = 8100
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Public base URL used for OAuth redirect URIs (must match the provider app config)
    public_base_url: str = "http://127.0.0.1:8100"

    # ---- Database ----
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'ymoney.db').as_posix()}"

    # ---- Auth ----
    access_token_expire_minutes: int = 120
    refresh_token_expire_days: int = 14

    # ---- Video engine ----
    # ffmpeg_avatar: fully-local render with stock clips, word-timed captions,
    # BGM bed (MPT technology adopted natively — MPT service now OPTIONAL).
    video_engine: str = "ffmpeg_avatar"  # ffmpeg_avatar | moneyprinterturbo | mock
    mpt_base_url: str = "http://127.0.0.1:8081"
    mpt_timeout_seconds: int = 1800
    # Honest flat per-render estimate (MPT exposes no monetary cost)
    mpt_estimated_render_cost_usd: float = 0.02

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Accept the long-form env name as an alias for MPT_BASE_URL
        import os

        alias = os.getenv("MONEYPRINTERTURBO_BASE_URL")
        if alias and not os.getenv("MPT_BASE_URL"):
            self.mpt_base_url = alias

    # ---- LLM ----
    mock_llm: bool = False  # always False; kept for legacy code that reads it
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_fallback_model: str = ""
    # Model-tier routing (optional). When set, agents pick a tier-appropriate
    # model instead of the default; falls back to llm_model when empty.
    research_model_cheap: str = ""      # discovery/scoring/metadata tasks
    research_model_reasoning: str = ""  # strategy/script/hook generation
    verification_model: str = ""        # fact-check / QC review

    # ---- Trend sources ----
    mock_trends: bool = False
    google_trends_enabled: bool = True
    google_trends_geo: str = "US"
    trend_refresh_minutes: int = 30
    # NewsData.io structured news (from the public-apis catalog). Free tier:
    # 200 credits/day. Set via env or Settings → Connections.
    newsdata_api_key: str = ""

    # ---- Publishing ----
    mock_publishing: bool = False
    upload_post_api_key: str = ""
    upload_post_username: str = ""
    youtube_api_key: str = ""            # Data API key (trending + analytics reads, ~1 unit/call)

    # ---- TTS / Images ----
    tts_provider: str = "edge"           # edge | kokoro | chatterbox | qwen3 | mock
    image_provider: str = "pexels"       # pexels | xkiro | pollinations | openai_compat | mock
    pexels_api_key: str = ""             # Pexels stock-photo API key (api.pexels.com)
    # avatar_image: static presenter image for the ffmpeg_avatar engine (path or URL)
    avatar_image: str = ""
    kokoro_base_url: str = ""            # e.g. http://127.0.0.1:8880/v1
    chatterbox_base_url: str = ""        # OpenAI-compat server for Chatterbox-Turbo (else native pip package)
    qwen_base_url: str = ""              # vLLM-Omni (or compat) server for Qwen3-TTS
    qwen_tts_instruct: str = ""          # default delivery direction, e.g. "speak cheerfully"

    # ---- Avatar (talking-head clips) ----
    avatar_backend: str = "server"       # server | sadtalker | mock
    avatar_base_url: str = ""            # generic renderer: multipart image+audio → mp4
    sadtalker_dir: str = ""              # local OpenTalker/SadTalker checkout with checkpoints

    # ---- B-roll (stock + AI scene clips) ----
    broll_ai_backend: str = "server"     # server | wan | ltx | synth
    broll_ai_base_url: str = ""          # generic renderer: {prompt,seconds,aspect} → mp4

    # ---- Analytics ----
    mock_analytics: bool = False

    # ---- Cost control ----
    daily_budget_usd: float = 5.0
    per_video_budget_usd: float = 0.5

    # ---- Autopilot defaults ----
    autopilot_max_cycles: int = 0
    autopilot_videos_per_cycle: int = 1
    quality_threshold: int = 75

    # ---- Telegram remote control (optional) ----
    # Pair via the UI (Settings → Integrations) with a one-time code; no bot
    # token is required for pairing to be *generated*, but sending/commands
    # only work once TELEGRAM_BOT_TOKEN is set and a chat is linked.
    telegram_bot_token: str = ""
    telegram_enabled: bool = True
    telegram_poll_interval_seconds: float = 3.0

    # ---- Jobs ----
    job_poll_interval_seconds: float = 1.0
    job_default_max_retries: int = 3
    job_worker_count: int = 4

    # ---- Storage (local default; S3-compatible optional) ----
    storage_backend: str = "local"  # local | s3
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_public_base_url: str = ""

    # ---- Observability ----
    sentry_dsn: str = ""

    # ---- Logging ----
    log_level: str = "INFO"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ymoney_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
