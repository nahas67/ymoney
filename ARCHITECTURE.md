# YMONEY Architecture

## 1. System overview

```
                    ┌─────────────────────────────┐
                    │      Frontend (React/Vite)   │
                    │  dashboard · library · agents│
                    └──────────────┬──────────────┘
                                   │ REST + SSE
                    ┌──────────────▼──────────────┐
                    │       Backend API (FastAPI) │
                    │  auth · RBAC · workspaces   │
                    ├─────────────────────────────┤
                    │        Job queue (durable)  │
                    │  DB-backed, retries, DLQ    │
                    ├─────────────────────────────┤
                    │     Autopilot orchestrator  │
                    │  FIND→…→LEARN state machine │
                    └──┬────────┬────────┬────────┘
                       │        │        │
              ┌────────▼──┐ ┌───▼────┐ ┌▼─────────────┐
              │ Discovery │ │ Agents │ │ Distribution │
              │ providers │ │ (22)   │ │ publishers   │
              └───────────┘ └───┬────┘ └──────────────┘
                                │ VideoEngine interface
                     ┌──────────▼──────────┐
                     │ MoneyPrinterTurbo    │  ← swappable adapter
                     │ HTTP API             │
                     └─────────────────────┘
```

### Key principle: adapters at every boundary

- **VideoEngine** — `ffmpeg_avatar` (default local real render) + `MoneyPrinterTurboAdapter` (HTTP) + `MockVideoEngine` (sim only). Swapping
  engines requires zero changes elsewhere.
- **TrendSource** — Google Trends RSS, Reddit JSON, HackerNews Algolia, NewsData.io, CoinGecko, Dev.to, YouTube Trending, YouTube Channel RSS; registry-based (`providers/trends/__init__.py`).
- **Publisher** — YouTube Data API v3, TikTok Content Posting API, Facebook Graph Reels, Instagram Graph Reels,
  Upload-Post relay. Production-only factory (`providers/publishers/factory.py`); no mock in product.
- **AnalyticsProvider** — YouTube Data+Analytics API, TikTok Display, Meta Graph; mock only when `MOCK_ANALYTICS=true`, else `AnalyticsNotConfigured`.

## 2. Autopilot orchestration

The loop is **not** a while-loop. Each stage is a durable job in the `jobs` table;
successful stages enqueue the next. This gives:

- crash safety (orphaned jobs/cycles are recovered on restart),
- pause/stop between any two stages (`_gate` checks desired state),
- exactly-once claiming via conditional UPDATE under the DB write lock,
- dead-lettering with a circuit breaker (3 consecutive failed cycles stop the run).

State lives in two tables: `autopilot_runs` (desired state: IDLE/STARTING/RUNNING/
PAUSED/STOPPING/STOPPED) and `cycles` (per-cycle stage + status).

Restart policy: after an unclean shutdown all active runs are paused and in-flight
cycles are marked FAILED. Resuming is an explicit user action. This prevents zombie
loops from previous processes.

## 3. Decision engine & opportunity scoring

The Supervisor does not pick "the highest score". The **Decision Engine**
(`engine/decision.py`) computes the NEXT BEST ACTION per cycle against the full
workspace context:

- hard capacity gates first: daily/monthly budget, max videos/day,
  max uploads/hour, render queue depth (→ WAIT with reason),
- candidate evaluation: base score + lifecycle bonus (EMERGING/RISING topics earn
  outsized attention) + learned-pattern alignment − repetition penalty vs the last
  14 days of published content (token-Jaccard similarity),
- escalation outcomes: `PRODUCE`, `WAIT`, `SKIP`, `RESEARCH_MORE` (low scoring
  confidence), `HUMAN_REVIEW` (risk above workspace threshold).

Every decision carries a serializable WHY payload — factors with values and signed
contributions, evidence strings, confidence — stored on the cycle and content item,
and rendered in the UI's WHY panels.

Scoring (`engine/scoring.py`, v2) remains explainable and configurable per workspace.
Each component now includes source attribution (provider/heuristic/learned/policy)
and a confidence value; the overall score applies a repetition penalty from recent
topics and an explicit lifecycle adjustment (EMERGING > RISING > EVERGREEN > PEAK >
DECLINING). Trend lifecycle classification lives in `engine/trends.py` and derives
EMERGING/RISING/PEAK/DECLINING/EVERGREEN from velocity/volume/news signals with
documented heuristics.

### Content diversity

Similarity to recently published content produces a scored penalty and, past the
workspace-configurable `similarity_threshold` (default 0.55), a hard SKIP with the
offending prior topic cited. This prevents the system from endlessly reproducing its
own back catalog while still allowing confident patterns to inform strategy.

## 4. Content lifecycle

`IDEA → RESEARCHING → STRATEGY → SCRIPTING → SCRIPT_READY → PRODUCTION → QC →
APPROVED → SCHEDULED? → PUBLISHED → ANALYZING → LEARNED`

Transitions are validated in code (`CONTENT_TRANSITIONS`); illegal moves raise.
QC rejection routes back to `SCRIPT_READY` for regeneration (max 2 attempts).
Same-state re-entry is allowed for idempotent job retries.

## 5. Learning loop

After each cycle the Learning Agent compares top-performing posts against the channel
median on observable features (title style, numbering). Patterns store observed
improvement %, confidence (low <10 samples, medium <30, high ≥30) and sample size.
Correlation is explicitly not treated as causation. Confident patterns feed back into
scoring (`historical_performance`) and hook ranking bonuses.

## 6. Cost control & Safety Center

Every LLM/video/publish call records a `CostEntry`. Before production the Decision
Engine checks daily and monthly budgets plus per-video caps; exhausted budget halts
production gracefully with an event, never mid-render.

The **Safety Center** (workspace `settings.safety`, editable in Settings → Safety
Center) centralizes limits with safe defaults:

```
daily_budget_usd, monthly_budget_usd, per_video_budget_usd,
max_videos_per_day, max_uploads_per_hour, min_qc_score,
max_render_attempts, max_consecutive_failures,
similarity_threshold, require_human_review_risk_above
```

Automatic triggers pause autopilot with an explanation: repeated cycle failures,
abnormal publishing rate, budget exhaustion. All triggers emit `safety.autopause`
events visible on the dashboard.

## 7. Publishing idempotency

`published_posts` and `publishing_jobs` carry unique indexes on `(video_id, platform)`.
The upload stage checks what is already published before calling any provider,
upserts job rows (attempt counter increments), and reuses post rows — a retried or
resumed upload can never duplicate content on a real platform. Verified by dedicated
tests (`test_chaos.py`).

## 8. Simulation mode

`POST /workspaces/{id}/simulation {cycles}` runs the full pipeline with the run-level
flag `simulation=true`: PublisherAgent forces mock publishing regardless of global
flags, so no real account is ever touched. `GET /simulation/report` aggregates
decisions, produced videos, failures, retries, agent utilization and estimated cost.
Simulation output is always labeled SIMULATION.

## Video engine integration (MoneyPrinterTurbo)

Boundary rule: **YMONEY knows WHAT/WHY/WHEN/FOR-WHOM/HOW-TO-JUDGE; MoneyPrinterTurbo
knows HOW to render.** The only file that understands MPT's API shape is
`providers/video_engine/mpt.py`.

- **Normalized request** (`RenderRequest`): subject/script/keywords/aspect/voice/
  subtitles/bgm/clip_duration/count + `request_hash()` for idempotency. Domain fields
  (workspace/content/campaign) never leak into engine payloads (tested).
- **Normalized states**: `queued|processing|complete|failed|not_found|cancelled`;
  raw MPT states (-1/1/4) are mapped inside the adapter only.
- **Typed errors**: `VideoEngineUnavailable` (retryable: connect/timeout/429/5xx) vs
  `VideoEngineRequestInvalid` (permanent: 400/401/403/404). Server paths and secrets
  never surface in messages.
- **Durable render**: the BUILD stage persists a `videos` row (status RENDERING,
  request_hash, then engine_task_id) BEFORE polling; progress is written to the row and
  emitted as `video.generation.progress` events at 25% steps. On retry/restart the
  producer reattaches to the stored engine task, or ADOPTS an orphaned engine task by
  workspace-scoped subject match when the crash happened between submit and persist.
  Duplicate submission is impossible (request-hash + variant selection checks).
- **Storage boundary** (`services/storage.py`): completed renders move engine → YMONEY
  storage with ffprobe metadata (duration/width/height/size). Local provider today;
  S3-compatible providers slot in behind the same interface.
- **Backpressure**: `safety.max_concurrent_renders` (default 2); excess builds requeue.
- **Runtime configuration**: Settings → Video Engine edits base URL/timeout via the
  encrypted credential store (env fallback), applied immediately; health/version
  (from OpenAPI)/capabilities are displayed live.
- **QC-driven regeneration**: rejection stores a concrete instruction derived from the
  weakest QC dimensions, flips to the next-best script variant (labeled `r{n}.`),
  injects the instruction into regenerated scripts, and respects
  `safety.max_render_attempts`.
- **Audit**: generation created/completed/failed recorded in `audit_logs`
  with actor AI_AGENT.

## TTS (narration) providers

`providers/tts.py` abstracts narration voice generation behind `BaseTTSProvider`:

- **edge** (default) — Microsoft Edge neural voices over the public edge-tts
  protocol. Free, no key, no local model.
- **kokoro** — fully local Kokoro-82M (Apache-2 weights) behind any
  OpenAI-compatible `/audio/speech` server. Configure `KOKORO_BASE_URL` env or
  `tts.kokoro_base_url` under Settings → Connections.
- **elevenlabs** — cloud neural voices + voice library (paid key via `tts.elevenlabs_api_key` / `ELEVENLABS_API_KEY`); per-content `voice` override takes any voice_id; cost estimated per character so budgets stay honest.
- **mock** — deterministic labeled silence for simulation/CI (`TTS_RESULT.is_mock`).

Selection: `TTS_PROVIDER=edge|kokoro|mock`. MoneyPrinterTurbo renders accept a
`kokoro:<voice>` voice name (e.g. `kokoro:af_heart`) which routes to the same
local server inside the engine (`[kokoro] base_url` in its config.toml).

## Social listening & clip repurposing

- **Hacker News source** (`hacker_news` kind in `providers/trends`): keyless
  social listening over the official Algolia search API — engagement velocity
  (points + comments per hour) ranks stories in the workspace niche. Register it
  per workspace via Settings → Trends (kinds: `google_trends | reddit | hacker_news`).
- **Clip repurposing** (`providers/clips.py` + `POST /assets/repurpose`): cuts an
  operator-supplied long-form source (URL via optional yt-dlp, or local file) into
  deterministic vertical 1080x1920 segments with FFmpeg. Outputs land inside the
  workspace storage boundary; capability probes are exposed at
  `GET /assets/repurpose/status` and failures report exact remediation.

## Image providers & the FFmpeg Avatar engine

`providers/images.py` abstracts scene-image generation behind `BaseImageProvider`:

- **pollinations** (default) — free keyless generation via the public
  pollinations.ai endpoint, with automatic retry/backoff for transient 5xx.
- **openai_compat** — any OpenAI-compatible `/images/generations` endpoint
  (LocalAI, ComfyUI bridges, or a vendor). Configure `image.openai_base_url`,
  `image.openai_api_key`, `image.openai_model` under Settings → Connections.
- **mock** — deterministic labeled placeholder gradients for simulation/CI.

Selection: `IMAGE_PROVIDER=pollinations|openai_compat|mock`.

The **FFmpeg Avatar engine** (`VIDEO_ENGINE=ffmpeg_avatar`) is a fully local REAL
render path: TTS narration (via the TTS layer) + AI scene images (via the image
layer) + Ken-Burns motion + crossfades + burned captions → H.264/AAC mp4 in
9:16 / 16:9 / 1:1. It implements the standard `BaseVideoEngine` interface, so the
rest of YMONEY (production agent, QC, publishing, UI) requires zero changes.
Image generation is also exposed directly at `POST /assets/images/generate` and
`POST /connections/images/test` (Settings → Images has a live preview).

## Agent observability (step tracing)

Every `BaseAgent.execute()` records an ordered **step trace** alongside the run:
agents call `self.step("name", "detail")` / `self.step_done("ok", "…")` around
sub-steps (LLM calls, verification passes, vision analysis). Steps are persisted
on `agent_runs.steps_json` (migration 0006), exposed through the agent detail API,
and rendered as an expandable per-run timeline in Agent Center. A step left open
when a run ends is recorded as `interrupted` — never silently dropped.

## Learned-pattern influence (WHY panel)

The decision engine applies **active learned patterns** as named, per-pattern
factors instead of an anonymous aggregate boost. Each relevant pattern contributes
`min(observed_improvement_pct, 15%) × 0.3` (capped at +8 total), and the WHY panel
shows pattern key, observed improvement, confidence, and sample size. Format
patterns (hook style, duration, title style) apply broadly; domain patterns apply
only when their keywords match the topic — so a money-pattern never boosts a
quantum-computing topic.

## Trend Center (operator UX)

## Scheduled publishing executor (Calendar that works)

The Calendar was write-only: `POST /calendar` created PENDING entries nothing ever executed. Now a periodic **schedule sweep** (armed at startup, re-arms itself every 60s as an idempotent `system.schedule_sweep` job) claims due entries and publishes them:

- **Claim-then-work**: each due entry is marked `DONE` inside the sweep transaction *before* dispatch, so a sweep retry can never double-dispatch; downstream idempotency is still enforced by the unique `(video_id, platform)` indexes and a `sched-{entry_id}` job idempotency key.
- **Platform targeting**: the enqueued `cycle.upload` carries `platforms_override` so only the entry's platform publishes (upload handler honors the override).
- **Cycle-optional**: scheduled publishes have no owning cycle; on success the handler collects metrics immediately instead of chaining the cycle MEASURE/LEARN stages, and failure paths skip cycle finalization.
- **Honest cancellation**: entries without a renderable video (e.g. Composer entries with no content attached) are CANCELLED with a warning event — never silently dropped or fake-published.
- **Concurrency note**: the sweep enqueues jobs and emits events strictly *after* its DB session closes — nested `session_scope` with pending writes deadlocks SQLite (two real defects found and fixed via tests).

## Persistent memory (Memory Center)

- **Model** (`memory_records`): typed records — `short_term | episodic | semantic | strategic | preference` — with content, source agent, confidence (0–1), importance (0–1), topic scope, related-entity JSON, and optional `expires_at`. Workspace-scoped and indexed for targeted retrieval.
- **Service** (`app/services/memory.py`): `store()` validates type and clamps confidence/importance; `retrieve()` is **targeted by design** — filters by type/scope/keyword, ordered by importance then recency, hard-capped at 50 so no caller can dump the store. Expired records are never returned and are purged lazily on write.
- **Learning loop integration**: every learned pattern the Learning Agent upserts is mirrored into semantic memory (`Pattern [key]: description — +X% vs channel median (n=…, confidence)`), so future decision/research contexts can retrieve it by scope. Mirroring failures never break the learning loop.
- **API**: `GET /workspaces/{id}/memory` (filtered list), `POST …/memory/store`, `POST …/memory/retrieve` (requires ≥1 filter — dump-all is rejected with 422), `DELETE …/memory/{id}` (admin). All workspace-isolated and tested for cross-workspace leakage.
- **UI**: Memory Center page (Understand → Memory) with type tabs, keyword search, confidence/source metadata, and delete. The Learning Agent's semantic memories appear here as they form.
- **Feedback loop closed**: `retrieve_for_topic()` (full-topic match → keyword fallback, shared matching semantics) feeds the decision engine — topic-relevant semantic memories add an inspectable `memory_context` WHY factor capped at +4.5 — and grounds the Research Agent's LLM prompt with prior knowledge labeled "verify independently". Memory is context, never a dependency: any failure degrades to empty context.
- **Style grounding**: `style_context()` (preference + strategic only, semantic excluded) injects a "Workspace style guidance (must be honored)" block into the Strategist and Scriptwriter system prompts — a user's "faceless documentary only" preference demonstrably shapes generated strategy and scripts, not just decisions.

## Phase D surfaces

- **Cycle execution drawer** (Autopilot page): every cycle row is clickable and opens `GET /api/v1/workspaces/{id}/cycles/{cycle_id}/detail` — per-stage jobs (status, duration, error, attempt), the agent runs behind each stage, and each run's step trace. One view answers "what happened in this cycle, in order."
- **Analytics breakdowns**: `GET /api/v1/workspaces/{id}/analytics/breakdowns` aggregates post metrics by topic, hook style, and duration bucket (with median baselines) plus a `mock_analytics` honesty flag. Surfaced as the Breakdowns/Patterns tabs in Analytics.
- **Publishing job hardening**: the autopilot pre-registers `QUEUED` `publishing_jobs` rows before calling any publisher; combined with the unique `(video_id, platform)` index, a retried or concurrent stage can never double-publish — retries update the same row (`attempt` increments). Publishing UI now shows status, attempt count, and remote post IDs per row.

The Trend Center exposes discovery metadata (`velocity`, `source_url`) from the
opportunities API. Cards show a lifecycle badge, score bar, velocity indicator,
and source; the detail panel links to the original evidence. Keyboard navigation:
↑/↓ to move, Enter to open.

## 9. Data model (SQLite default / Postgres-compatible)

Core tables: `users`, `workspaces`, `workspace_members`, `social_accounts`,
`api_credentials`, `audit_logs`, `campaigns`, `trend_sources`, `opportunities`
(unique per workspace+topic), `content_items`, `video_variants`, `videos`,
`quality_checks`, `publishing_jobs`, `published_posts`, `post_metrics`,
`schedule_entries`, `jobs`, `agent_configs`, `agent_runs`, `autopilot_runs`,
`cycles`, `events`, `cost_entries`, `learning_patterns`, `system_logs`.

Migrations run through a minimal ordered runner (`app/migrations/runner.py`) that is
transactional per migration; base tables come from SQLAlchemy metadata.

## 8. Frontend

React 19 + Vite + Tailwind v4. Dashboard centers on START/STOP with a live pipeline
visualizer; SSE powers the activity feed; polling refreshes stats. All controls hit
real endpoints — there are no fake statistics or placeholder actions.

## 9. Scaling path

The reference deployment is single-process (API + workers in one uvicorn process),
which is sufficient for several concurrent cycles. To scale out: swap `services/jobs.py`
for a Redis/Celery backend behind the same `enqueue`/handler interface, move SQLite to
Postgres via `DATABASE_URL`, and run MPT renders on GPU workers behind its Redis task
manager.
