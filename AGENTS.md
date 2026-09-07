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
| **Trend Hunter** | Fetches candidates from enabled trend sources (Google Trends RSS, Reddit JSON, mock). One failing source never kills the cycle. Dedupes by workspace+topic. |
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

## Orchestration

The Supervisor role is implemented inside the autopilot orchestrator
(`engine/autopilot.py`) plus the Decision Engine (`engine/decision.py`): it computes
the NEXT BEST ACTION (PRODUCE / WAIT / SKIP / RESEARCH_MORE / HUMAN_REVIEW) per cycle
based on score, diversity, budget, capacity, risk and learned patterns; enforces
pause/stop gates between stages; applies the circuit breaker and Safety Center
auto-pause triggers; and schedules next cycles.

## Adding an agent

1. Create a class inheriting `BaseAgent` with a unique `meta.key`.
2. Register it in `engine/agents/registry.py`.
3. It automatically appears in the Agent Center API/UI with enable/disable controls.
