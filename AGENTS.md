# YMONEY Agents

Agents are modular classes under `backend/app/engine/agents/`. Each agent:
- has an identity (`AgentMeta`: key, title, description),
- records every execution in `agent_runs` (duration, cost, output summary, errors),
- emits events to the live activity feed,
- can be disabled per workspace (`PUT /api/v1/workspaces/{id}/agents/config/{key}`).

Users never operate agents manually; they observe and can disable/re-enable them.

## Catalog

| Agent | Responsibility |
|---|---|
| **Trend Hunter** | Fetches candidates from enabled trend sources (Google Trends RSS, Reddit JSON, Hacker News, NewsData.io, CoinGecko, Dev.to, YouTube Trending/Channel, Bilibili, mock). One failing source never kills the cycle. Dedupes by workspace+topic. |
| **Trend Analyst** | Scores all unscored opportunities with explainable scoring v2: per-component score/weight/reason/source/confidence, repetition penalty vs recent content, and lifecycle classification (EMERGING/RISING/PEAK/DECLINING/EVERGREEN). |
| **Research Agent** | Produces a compact research brief: summary, key facts, angles, visual keywords, cautions — plus tracked claims with VERIFIED/LIKELY/UNCERTAIN/CONFLICTING status and an aggregate factual confidence. Conflicting or insufficient fact-confidence surfaces to QC notes. |
| **Content Strategist** | Decides angle, audience, format, duration (clamped 20–90s), hook type, tone, CTA, platforms, aspect ratio. |
| **Script Agent** | Writes retention-optimized scripts (~2.6 words/second) honoring strategy + research. Generates 3–5 variations. |
| **Hook Optimizer** | Ranks variants by predicted hook strength; boosts patterns the Learning Agent flagged as confident. Best variant is selected for production. |
| **Video Producer** | Submits renders through the VideoEngine adapter, polls with cancellation checks, downloads finished files, estimates render cost. |
| **Quality Agent** | Scores hook/story/retention/pacing/audio/captions/caption-readability/visual relevance/originality/accuracy/safety/brand-consistency/platform-fit. Heuristic baseline blended 40/60 with LLM review when available; factual confidence from research folds into accuracy. Workspace `min_qc_score` enforced. Below threshold → regenerate (max 2 attempts) → fail cycle. |
| **SEO Agent** | Per-platform titles/descriptions/hashtags/keywords within platform limits. Never copies identical metadata across platforms. |
| **Publisher Agent** | Publishes via provider layer to connected accounts; falls back to labeled mock publishing when `MOCK_PUBLISHING=true` or no account is connected. |
| **Analytics Agent** | Collects metric snapshots for published posts; a single platform outage never breaks the cycle. |
| **Learning Agent** | Extracts performance patterns vs channel median with confidence + sample size; updates existing patterns via exponential moving average. |
| **Link Miner** | Mines ranked viral moments (score + hook + reason) from long-form URLs/files via transcript + scene + LLM/heuristic rank. |
| **Repurpose Editor** | Assembles ranked moments into captioned vertical shorts (face-tracked reframe, caption presets). |
| **Motion Designer** | Renders kinetic HyperFrames cards (hook/stat/CTA/lower-third); fails closed without CLI + Chrome. |
| **Dubbing Localizer** | Translates + re-voices videos (SRT → TTS → time-fit → bilingual portrait assembly). |
| **Voice Designer** | Casts/clones/directs narration voices, multi-voice dialogue assembly, emotion control. |
| **Avatar Director** | Directs talking-head clips (photo + audio, or script voiced first) via server/SadTalker lanes. |
| **B-roll Researcher** | Plans per-scene visuals and fetches stock / generates AI B-roll clips. |
| **Compliance Officer** | Pre-publish gate: spec preflight, disclosure presence, reused-content risk → HUMAN_REVIEW. |
| **Competitor Analyst** | Scans the channel watchlist; persists rival videos and raises trend-jack alerts. |
| **Scheduler** | Auto-fills the calendar at best measured hours within daily caps; idempotent. |

## Orchestration

The Supervisor role is implemented inside the autopilot orchestrator
(`engine/autopilot.py`) plus the Decision Engine (`engine/decision.py`): it computes
the NEXT BEST ACTION (PRODUCE / WAIT / SKIP / RESEARCH_MORE / HUMAN_REVIEW) per cycle
based on score, diversity, budget, capacity, risk and learned patterns; enforces
pause/stop gates between stages; applies the circuit breaker and Safety Center
auto-pause triggers; and schedules next cycles.

## Editorial planning (`engine/planning/`)

The Planner turns signals into scheduled work. It is a *proposal* layer that
references canonical objects — it never owns execution state.

- `signals.py` — `TrendSignal`: an observation with its evidence. A single
  observation has **no** velocity; the rate is only computed between two real
  observations. Re-ingesting the same source item refreshes rather than
  duplicating, so a re-syncing source cannot manufacture recurrence. Evidence
  verification is **derived**, not caller-asserted: only self-resolving sources
  (research, connectors, official platform APIs) with evidence ids count as
  verified, and an operator's word never does.
- `opportunities.py` — `ContentOpportunity` with a `basis` of `OBSERVED` /
  `INFERRED` / `RECOMMENDED`. A factor with no data contributes 0 and says so;
  `RECOMMENDED` is capped so a suggestion cannot outrank measured demand. No
  virality, revenue, or success-probability field exists.
- `dedup.py` — `NEW` / `RELATED` / `DUPLICATE` / `SATURATED`, with an explicit
  series exception that lifts a block while recording why.
- `capacity.py` — capacity columns are **rates** (`shorts_per_day`) scaled to
  the horizon, compared against a committed count. An unset pool is unbounded,
  not zero. The ledger is locale-scoped, so one market's limits are not spent
  on another's work.
- `calendar.py` — placement only. It writes the existing `ScheduleEntry` store
  and never enqueues a job. `run_at` is stored in **UTC** (the Scheduler reads
  that naive column as UTC), keyed on `ScheduleEntry.plan_item_id` for
  idempotency. Timing comes from the platform's seed window unless measured
  data exists; no statistical optimum is claimed without it.
- `autonomy.py` — `DISABLED` / `RECOMMEND` / `APPROVAL` / `AUTONOMOUS`, gated
  through `assert_may_advance`. **Planning autonomy is not publishing
  autonomy**: `PUBLISH` is refused at every mode, unconditionally.
- `engine.py` — `ContentPlanningEngine`. Consults GlobalMemory first; a topic
  memory already covers is `SETTLED` and is not re-researched.
- `orchestration.py` — trend → campaign, idempotent and resumable. Produces a
  campaign DRAFT; creating a draft is not approval. Every stage goes through
  the full gate, so an AUTONOMOUS allowlist naming only `SCHEDULE` runs nothing
  else.
- `feedback.py` — measured outcomes, and lessons that need `MIN_SAMPLE` items
  and a real effect size. Only the **latest** `PostMetric` snapshot per post is
  counted (they are cumulative). Reports `effect_size`, never a "confidence"
  derived from a rate delta.

Outbound fetches go through `core/netguard.py` + `core/fetch.py`: hostname
resolution, a deny-by-default `is_global` gate, per-hop redirect revalidation,
and address pinning against DNS rebinding. This covers fetches **YMONEY**
performs; a URL handed to a platform to fetch is out of scope and is checked
separately by that provider.

## Adding an agent

1. Create a class inheriting `BaseAgent` with a unique `meta.key`.
2. Register it in `engine/agents/registry.py`.
3. It automatically appears in the Agent Center API/UI with enable/disable controls.
