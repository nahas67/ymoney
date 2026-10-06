"""YMONEY runtime configuration.

All settings come from environment variables (optionally via a .env file).
Secrets are never hard-coded. See .env.example at the repository root.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
    # Pool bounds (Work 16 §7). SQLite serialises on its own file lock and
    # ignores max_overflow; Postgres gets a real pool. `db_pool_size +
    # db_max_overflow` is the hard ceiling on concurrent connections, so these
    # two must stay below the server's max_connections with headroom for the
    # migration runner and any operator session.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # How long a caller waits for a pooled connection before being told the
    # database is saturated. Long enough to ride out a render's transaction,
    # short enough to fail before a request times out upstream.
    db_pool_timeout: int = 30
    # Retire connections proactively; below typical cloud LB / pgbouncer idle
    # timeouts, which kill a socket without telling either end.
    db_pool_recycle: int = 1800

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
    tts_provider: str = "edge"           # edge | kokoro | chatterbox | qwen3 | elevenlabs | mock
    image_provider: str = "pexels"       # pexels | xkiro | pollinations | openai_compat | mock
    pexels_api_key: str = ""             # Pexels stock-photo API key (api.pexels.com)
    # avatar_image: static presenter image for the ffmpeg_avatar engine (path or URL)
    avatar_image: str = ""
    kokoro_base_url: str = ""            # e.g. http://127.0.0.1:8880/v1
    chatterbox_base_url: str = ""        # OpenAI-compat server for Chatterbox-Turbo (else native pip package)
    qwen_base_url: str = ""              # vLLM-Omni (or compat) server for Qwen3-TTS
    qwen_tts_instruct: str = ""          # default delivery direction, e.g. "speak cheerfully"
    elevenlabs_api_key: str = ""         # ElevenLabs cloud TTS key (or tts.elevenlabs_api_key credential)

    # ---- Avatar (talking-head clips) ----
    avatar_backend: str = "server"       # server | sadtalker | wavlip | mock
    avatar_base_url: str = ""            # generic renderer: multipart image+audio → mp4
    sadtalker_dir: str = ""              # local OpenTalker/SadTalker checkout with checkpoints
    wavlip_dir: str = ""                 # local Wav2Lip checkout (non-commercial LRS2 weights!)

    # ---- B-roll (stock + AI scene clips) ----
    broll_ai_backend: str = "server"     # server | wan | ltx | synth
    broll_ai_base_url: str = ""          # generic renderer: {prompt,seconds,aspect} → mp4

    # ---- Analytics ----
    mock_analytics: bool = False

    # ---- Cost control ----
    daily_budget_usd: float = 5.0
    per_video_budget_usd: float = 0.5
    # ---- Work 16 §11: budget ROLLUPS (optional, cross-category) ----
    # The per-category and per-call caps above answer "may this CATEGORY afford
    # this?"; these answer "may this DEPLOYMENT afford this?" over every
    # category at once. 0.0 means NOT CONFIGURED -- never "free" -- which is why
    # an unset rollup leaves a deployment behaving exactly as it did before
    # Work 16 §11. A per-workspace ceiling lives in the database
    # (`budget_rollup_limits`, migration 0037); these two are the
    # deployment-wide pair, read only when no system row exists.
    budget_rollup_system_daily_total_cap_usd: float = 0.0
    budget_rollup_system_monthly_total_cap_usd: float = 0.0

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
    # Work 12 media intelligence. `max_concurrent_gpu_jobs` gates the
    # DB-backed GPU slot ledger (CPU-only providers bypass it);
    # `commercial_mode` makes the provider registry refuse any adapter
    # whose license audit did not clear it (see docs/oss/MEDIA_INTEL_LICENSES.md).
    max_concurrent_gpu_jobs: int = 1
    commercial_mode: bool = False
    # Work 11.5 (C-F1): operator kill-switch for private-target source
    # connectors. Connector configs may request allow_private, but the fetch
    # is refused unless the operator enables this. Default False.
    allow_private_connectors: bool = False
    # Work 11.5 (E-MED): a deliberate escape hatch for a staging deployment
    # that intentionally renders with the labeled mock engine. The factory
    # refuses video_engine='mock' in production unless this is set.
    allow_mock_in_production: bool = False
    job_queue: str = "local"             # local | redis (redis dispatch, DB fallback)
    redis_url: str = "redis://localhost:6379/0"
    gpu_worker: bool = False             # this process claims GPU-gated jobs
    gpu_engines: str = "wan,ltx"         # engine names treated as GPU-native

    # ---- Work 16 §2: leases. A live worker renews; only an EXPIRED lease may
    # be reclaimed, so the TTL only has to outlast a missed heartbeat, not a
    # job. `job_lease_seconds_by_workload` raises it for the long classes (a
    # 30-minute render is heartbeat-renewed every third of its TTL, so its own
    # duration does not enter into it). Format: "RENDER=900,GPU=1800".
    job_lease_seconds: float = 120.0
    job_lease_seconds_by_workload: str = ""
    job_reclaim_interval_seconds: float = 15.0
    # ---- Work 16 §3: worker pools. Empty means "every worker takes SMALL
    # jobs", which is the pre-16 behaviour: one pool, no starvation to fix.
    # Format: "SMALL=4,RENDER=2,GPU=1".
    job_worker_pools: str = ""
    # ---- Graceful shutdown. In-flight work is allowed to finish; past this
    # deadline the remaining leases are simply left to expire and be reclaimed,
    # which is safer than cancelling a paid render mid-flight.
    job_drain_timeout_seconds: float = 30.0
    # ---- Worker identity in `jobs.claimed_by`. Empty means "derive one":
    # host + pid + a per-process suffix, so two processes on one host are still
    # distinguishable and a restart is visible as a new identity.
    job_worker_identity: str = ""

    # ---- Storage (local default; S3-compatible optional) ----
    storage_backend: str = "local"  # local | s3
    # Where LOCAL storage keeps canonical media. Must be an absolute path on a
    # durable mount. It used to be a module constant, `Path("data/videos")`,
    # resolved against the process working directory -- which in the container
    # is `/app`, inside the image's writable layer. Nothing mounted `/data` to
    # the backend service at all, so every render was destroyed by the next
    # `docker compose up --build`. Set this to a mounted volume (or use the
    # s3 backend) and the media survives a container recreate.
    storage_root: str = ""
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_public_base_url: str = ""
    # Work 16 §5. Bytes > this size are refused by the streaming writer rather
    # than buffered: an upload is a stream, and a 4 GB "buffer" is an OOM.
    storage_stream_chunk_bytes: int = 4 * 1024 * 1024
    # Staging root for in-flight writes. Deliberately OUTSIDE the managed media
    # tree so a temp file can never be resolved by `storage.managed_path`.
    storage_staging_dir: str = "data/storage_staging"
    # How long an unfinished PENDING object may sit before cleanup removes it.
    storage_pending_ttl_seconds: float = 3600.0

    # ---- Work 16 §4: GPU admission. A device is a ROW with finite VRAM, and
    # admission is a conditional UPDATE against it -- see gpu_scheduler.py for
    # why a count is not enough. `gpu_worker` still gates whether this process
    # claims GPU work at all; these govern how much it may hold.
    gpu_scheduler_enabled: bool = True
    # Seconds a GPU slot may be held before it is presumed abandoned. Tied to
    # the job lease so one crash-recovery rule covers both.
    gpu_slot_lease_seconds: float = 120.0
    # Bounded admission wait. Never unbounded: a queue that waits forever is a
    # queue that never says no.
    gpu_admission_timeout_seconds: float = 900.0
    # Fallback is opt-in twice: the CALLER must declare the work CPU-capable
    # and the operator must allow it here. Default off.
    gpu_cpu_fallback_enabled: bool = False
    # Devices this process will admit onto, comma-separated. Empty means "every
    # registered device".
    gpu_device_keys: str = ""

    # ---- Observability ----
    sentry_dsn: str = ""
    # Work 16 §8. The metric registry, the structured JSON log sink and the
    # trace recorder are dependency-free and always available; these switches
    # only control whether their sinks are attached at startup.
    observability_enabled: bool = True
    # True renders each log record as one JSON object (redacted); False keeps
    # loguru's default text format. Redaction is applied either way once a
    # structured sink is installed.
    observability_json_logs: bool = True
    observability_service_name: str = "ymoney"
    # Completed spans retained in memory for GET /internal/traces. Bounded on
    # purpose: an unbounded trace buffer only leaks under load.
    observability_max_spans: int = 2000
    # Per-metric label-set cap. Bounds memory against a hostile or buggy caller
    # that would otherwise mint unbounded series; drops are counted in
    # ymoney_metrics_series_overflow_total.
    observability_max_series_per_metric: int = 512

    # ---- Work 16 §10: SLO / alert thresholds. ONE source of truth ----
    # There are exactly TWO kinds of number here, and conflating them is the
    # mistake this block exists to prevent:
    #
    #   `alert_*` are COMPARED. A rule in `services/observability/slo.py`
    #     evaluates a live metric against them on every `GET /internal/alerts`,
    #     so changing one changes a verdict. They are resolved at call time,
    #     never captured at import, so a retune takes effect without a
    #     restart-free lie and a test can drive them.
    #
    #   `slo_*` are DECLARED. `slo_catalog()` reports `measured: false` because
    #     the registry is in-process and has no history, so there is nothing to
    #     compare these against. They are the number the PUBLISHED objective
    #     states. Changing one changes the objective text on
    #     `GET /internal/slo` -- and nothing else, because an unmeasured
    #     objective has no evaluator to retune. That is the honest ceiling, and
    #     it is why there is no `slo_api_latency_p95_seconds` field: the
    #     "p95 <= 1.0s" objective is wording, and inventing a knob for wording
    #     would be configurability with no behaviour behind it.
    #
    # The bounds below are the point, not decoration. Each one rejects a value
    # that would turn the rule into something other than what it documents, and
    # each is rejected AT STARTUP: `Settings()` is constructed at import of this
    # module, so a bad value raises `ValidationError` before a request can be
    # served, rather than silently coercing into a threshold nobody chose.
    #
    #   ALERT_PUBLISH_FAILURE_STREAK=0  -> `worst >= 0` is true for a perfectly
    #       healthy system: the rule would page on ZERO failures.
    #   ALERT_QUEUE_STALL_SECONDS=0    -> any queued job whose last start was in
    #       the same instant reads as a stall.
    #   SLO_API_AVAILABILITY_TARGET=0  -> declares an objective no system can
    #       meet, i.e. a permanently red dashboard.
    # The ceilings exist so a typo (`1e9`, a stray zero) cannot silently switch
    # paging off: "no alert" and "a badly configured alert" must look different.
    alert_queue_stall_seconds: float = Field(default=900.0, gt=0.0, le=86_400.0)
    # 0 is legal and means the strict policy "any unknown exposure is an
    # incident"; negative is nonsense because exposure cannot be negative.
    alert_unknown_exposure_usd: float = Field(default=1.0, ge=0.0, le=10_000.0)
    alert_publish_failure_streak: int = Field(default=3, ge=1, le=100)

    # Bounds: a rate is a fraction of 1, so `0.0 <= rate <= 1.0`; an
    # availability target is a fraction of 1 and must be above zero; a latency
    # and a queue depth are positive quantities bounded by a day / a very large
    # backlog so neither can be set to "never".
    slo_api_availability_target: float = Field(default=0.995, gt=0.0, le=1.0)
    slo_job_start_latency_seconds: float = Field(default=60.0, gt=0.0, le=86_400.0)
    slo_queue_backlog_max: int = Field(default=25, ge=1, le=1_000_000)
    slo_publish_failure_rate_max: float = Field(default=0.02, ge=0.0, le=1.0)
    slo_render_failure_rate_max: float = Field(default=0.05, ge=0.0, le=1.0)
    slo_unknown_exposure_max_usd: float = Field(default=5.0, gt=0.0, le=10_000.0)

    # ---- Logging ----
    log_level: str = "INFO"

    # ---- Work 16 §12: backup / restore ----
    # Where `python -m app.scripts.backup_restore` writes backups and drills by
    # default. A path, not a remote: the object-storage BYTES are deliberately
    # NOT in a backup (only the `storage_objects` inventory + checksums), so the
    # backup's size is dominated by the logical dump and a local directory is
    # the honest default. Off-host copy is a deployment decision, recorded in
    # docs/BACKUP_RESTORE_RUNBOOK.md.
    backup_dir: str = str(PROJECT_ROOT / "data" / "backups")
    # Docker container that holds pg_dump/pg_restore when the tools are not
    # installed on the host. Empty means "use the host's client tools", which is
    # correct for a managed PostgreSQL reached over the network.
    backup_pg_container: str = ""
    # Age of the OLDEST backup after which a backup is considered overdue. The
    # backup interval is cron/systemd, not here -- a library that also scheduled
    # itself would double-take on every cron tick. What this is for is the one
    # question an operator asks after an incident: "how stale is the thing I am
    # about to restore?", which is exactly the RPO the schedule implies.
    backup_max_age_hours: float = 26.0
    # A restored database whose canonical digests do not match the manifest is a
    # FAILED restore even though pg_restore exited 0. Kept as a setting so a
    # deployment can record its own tolerance without editing code; the value is
    # the number of FAILED checks tolerated, and it is 0 by design.
    backup_tolerate_failed_checks: int = 0

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
