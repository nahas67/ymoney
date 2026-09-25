# YMONEY

**Autonomous AI short-form content operating system.**

You configure a niche, connect platforms, and press **START**. YMONEY then runs the
content operation end-to-end:

```
START → FIND → NORMALIZE → DEDUPE → SCORE → DECIDE (NEXT BEST ACTION)
      → RESEARCH (+fact-check) → STRATEGIZE → BUILD → QUALITY CONTROL
      → PUBLISH (idempotent) → MEASURE → LEARN → repeat
```

The **Decision Engine** weighs trend velocity, audience fit, learned patterns,
content diversity, budget headroom, publishing capacity and risk — producing
PRODUCE / WAIT / SKIP / RESEARCH_MORE / HUMAN_REVIEW with a full WHY explanation
for every choice. A **Safety Center** enforces budgets, rate caps and quality
thresholds, auto-pausing autopilot with explanations when triggers fire.
**Simulation mode** runs up to 100 fully-mocked cycles before you ever go live.

MoneyPrinterTurbo is integrated as a swappable **video engine adapter**, not as the
product. Trend discovery, opportunity scoring, multi-agent production, AI quality
control, publishing, analytics and the learning loop are all YMONEY subsystems.

---

## Product surfaces

Navigation (desktop-first, responsive, light/dark):

| Area | What it does |
|---|---|
| **Command Center** | START/STOP, live pipeline state, next-best-action WHY panel, results, learning highlights |
| **Autopilot** | Stage-by-stage job map with retries, cycle history, dead letters |
| **Trend Center** | Opportunities by lifecycle (rising/emerging/peak/…) with full score breakdowns |
| **Ideas** | Idea queue → produce/archive; drafts and rejected items |
| **Content Studio** | Searchable library; per-item detail tabs incl. timeline & claims |
| **Calendar / Composer** | Drag-to-reschedule month view; platform-specific post scheduling |
| **Publishing** | Connected accounts + token health + delivery history |
| **Inbox** | Honest capability surface — lights up when read-API providers connect |
| **Analytics** | Overview / platforms / content performance / cost efficiency |
| **Intelligence** | Learned patterns, decision log, strategy recommendations |
| **Agents** | 22-agent crew: stats, enable/disable, per-agent run logs |
| **Campaigns / Brand / Assets / System Health / Settings** | Full management surfaces |

Every autonomous decision is explainable in-product (WHY panels). Mock/simulation
data is always labeled; missing capabilities say so instead of faking it.

## Quick start (development)

Prerequisites: Python 3.12+ (`uv` recommended), Node 18+, and optionally
[MoneyPrinterTurbo](./MoneyPrinterTurbo) running for real video renders.

```bash
# 1. Backend
cd backend
uv venv --python 3.12 && uv sync
# copy .env.example from the repo root to .env and adjust if needed
# defaults run fully mocked: no API keys needed, nothing touches real platforms
.venv/Scripts/python -m uvicorn app.main:app --port 8100   # Windows
#   or: .venv/bin/python -m uvicorn app.main:app --port 8100

# 2. Frontend
cd ../frontend
npm install && npm run dev        # http://localhost:5173
```

Open http://localhost:5173, register (creates your workspace), press **START**.

### Running the video engine for real renders

The default engine is **ffmpeg_avatar** — YMONEY's own fully-local renderer that
adopts MoneyPrinterTurbo's best technology natively (the MPT service is now
**optional**, only needed for its specific Pexels-material pipeline):

- **Real stock video clips** per scene (Pexels Videos API, same free key as photos), falling back to photos with Ken-Burns motion, then to a labeled placeholder
- **Word-timed captions** via faster-whisper (installed automatically) with proportional-timing fallback, burned in with styled ASS subtitles
- **BGM bed** — drop music files into `backend/data/bgm/` and they loop under narration at low volume with fade-out
- **Hardware encoder autodetect** (Intel QSV / NVIDIA NVENC / AMD AMF) with automatic libx264 fallback

MoneyPrinterTurbo stays selectable via `VIDEO_ENGINE=moneyprinterturbo` if you
prefer its material pipeline.

Start MoneyPrinterTurbo's API (default port 8080) per its own README, then set in `.env`:

```
VIDEO_ENGINE=moneyprinterturbo
MPT_BASE_URL=http://127.0.0.1:8080
```

Without it, keep `VIDEO_ENGINE=mock` — the pipeline runs end-to-end with clearly
labeled simulated artifacts.

### Mock flags

| Flag | Effect |
|---|---|
| `MOCK_LLM=true` | Deterministic offline scripts/research/QC (no LLM spend) |
| `MOCK_TRENDS=true` | Fixed dev topics instead of live sources |
| `MOCK_PUBLISHING=true` | Publishing simulated locally — **never posts to real platforms** |
| `MOCK_ANALYTICS=true` | Simulated metric growth |

Every mock artifact is labeled (`is_mock`, `MOCK` badges) so simulated results can
never be mistaken for production data.

### Trend sources (public-apis catalog)

Discovery pulls candidates from pluggable sources: Google Trends RSS, Reddit,
Hacker News (all keyless), **NewsData.io** — a structured breaking-news API
from the [public-apis catalog](https://github.com/public-apis/public-apis) with
categories, keywords, sentiment, and publisher-authority metadata (free tier:
200 credits/day, 10 articles/request; add the key under **Settings →
Connections & Keys** and it joins discovery automatically) — plus two more
keyless catalog picks: **CoinGecko** (trending crypto coins, real market
attention signals), **Dev.to** (practitioner tech articles with reaction
velocity), **YouTube Trending / Channel RSS** (chart + watchlist tracking) and
**Bilibili** (keyless video search by niche keyword). Configure per-workspace via the trend-sources API (`q`, `language`,
`categories`, `timeframe` for newsdata; `tag`, `days` for devto).

### Live Monitor (real-time graphs + agent work graph)

**Live Monitor** (`/live`) shows the system's pulse in real time — per-minute agent
activity and error charts (last 60 min), plus the **agent work graph**: the 22-agent
FIND→…→LEARN pipeline rendered as a live DAG where each node reflects its agent's
actual state (busy / idle / error / disabled), run counts, failure rates and average
duration, polled every 4 seconds with a freeze toggle.

Powered by two viewer-scoped endpoints: `GET …/live/metrics` (per-minute buckets of
agent runs, failures, cost and error events) and `GET …/live/agents/graph` (pipeline
DAG with live per-node stats).

### Telegram remote control (control YMONEY from your phone)

1. Create a bot with **@BotFather** in Telegram (`/newbot`) and copy the token.
2. Paste the token in **Settings → Connections** (`telegram.bot_token`, encrypted at
   rest) or set `TELEGRAM_BOT_TOKEN` in `.env`.
3. Open **Integrations → Telegram**, generate a pairing code, and send
   `/start <code>` to your bot in Telegram. That chat is now linked.
4. Control the pipeline from your phone:

| Command | Effect |
|---|---|
| `/status` | Autopilot state, current stage, cycle #, today's spend |
| `/run` | Start the autopilot |
| `/stop` | Stop safely (running steps finish) |
| `/pause` / `/resume` | Pause before next stage / resume |
| `/cycle` | Run exactly one FIND→…→LEARN cycle |
| `/cost` | Today's spend vs daily budget |

Alerts are pushed automatically for cycle completions/failures, published posts,
quality reviews needing attention, safety auto-pauses, and budget warnings.
The poller runs inside the backend process (long-polling, no webhook needed).

## Tests

```bash
cd backend
.venv/Scripts/python -m pytest tests -q
```

Covers scoring calibration, lifecycle state machine, security primitives, cost/budget
enforcement, provider adapters, the full FIND→…→LEARN pipeline through the real job
system, and API integration including workspace isolation.

## Documentation

- [ARCHITECTURE.md](./ARCHITECTURE.md) — system design & data model
- [AGENTS.md](./AGENTS.md) — agent catalog & orchestration
- [API.md](./API.md) — HTTP API reference
- [DEPLOYMENT.md](./DEPLOYMENT.md) — Docker & production deployment
- [SECURITY.md](./SECURITY.md) — security model
- [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) — common issues

## Platform policy

YMONEY only uses official APIs/public feeds (YouTube Data API, TikTok Content Posting
API, Google Trends RSS, Reddit JSON API). No scraping that evades restrictions.
Autonomy has guardrails: quality thresholds, budget circuit breakers, risk penalties,
and human override at every stage.
