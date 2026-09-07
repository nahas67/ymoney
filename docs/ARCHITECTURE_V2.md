# YMONEY Architecture V2 — Production-Only Redesign

## 1. Inspection summary

### YMONEY (current)
- FastAPI control plane: workspaces/RBAC, durable job queue (atomic claim,
  retries/backoff, dead-letter, cancellation), autopilot orchestrator chaining
  FIND→SCORE→SELECT→RESEARCH→BUILD→VERIFY→UPLOAD→MEASURE→LEARN
- 12 agents, explainable scoring, diversity engine, fact-check claims, safety
  center, learning loop (EMA/confidence/sample-size), cost intelligence
- MoneyPrinterTurbo HTTP adapter (durable render rows, orphan adoption,
  progress events, ffprobe metadata, thumbnails), storage boundary
- Providers: Google Trends RSS, Reddit JSON, YouTube/TikTok/Facebook/Upload-Post
  publishers, OpenAI-compatible LLM with tier routing
- React 19 SPA (19 routes), SSE activity, command palette, topbar controls
- **Legacy fakery still present**: MockVideoEngine, MockPublisher,
  MockAnalyticsProvider, MockTrendSource, Simulation run-mode

### ZIP: youtube-automation-agent-master (Node.js)
| Capability | Implementation | Verdict |
|---|---|---|
| ProductionReadinessService | Probes AI/video/ffmpeg/storage/youtube creds; blocks actions with 409 + blocking failures; stale after 24h | **ADOPT** (Python port, gates autopilot START) |
| GenerationRecoveryService | Checkpoint/resume table per production | **ALREADY EQUIVALENT** (durable Video rows + reattach/adopt) |
| ProvenanceService | Sources + claims + synthetic-media disclosure per production | **PARTIAL ADOPT** — we already track claims; add explicit `contains_synthetic_media` on publish payloads (Upload-Post already sends it) |
| SceneRepairService / SceneRetentionEngine | Scene-graph editing | **NOT INTEGRATED** — MPT does not expose a scene graph; would require engine-side scenes. Roadmap, honestly marked N/A today |
| ShortsRepurposingService | Long-form → shorts segments | **NOT APPLICABLE** — pipeline produces shorts only; no long-form source exists |
| CredentialManager | Encrypted store | **ALREADY EQUIVALENT** (api_credentials AES-GCM) |
| GrowthExperimentService / DiscoverabilityService / AudienceEngagement | Experiments, SEO audits, comment ops | **ROADMAP** — require platform read APIs not present; will not fake |
| AutonomousChannelOperator | Loop driver | **ALREADY SUPERIOR** (autopilot + decision engine) |
| Vanilla dashboard / Express API / SQLite db.js | App shell | **REJECTED** — YMONEY is the product (Rule #3/#67) |

## 2. Target architecture (unchanged layering, hardened)

Control plane (YMONEY) vs execution engines (MPT/LLM/TTS/platforms) — every
external capability behind an adapter. New in V2:

1. **Production-only policy**: zero mock/demo/simulation code paths in the
   product. Missing dependency ⇒ explicit state
   (`NOT CONFIGURED` / `UNAVAILABLE` / `AUTH REQUIRED`) with remediation.
2. **Readiness gate**: autopilot cannot START unless blocking checks pass
   (LLM auth, video engine healthy, ffmpeg present, storage writable).
   Override is possible but audited.
3. **Publishing fail-closed**: no account ⇒ `BLOCKED (AUTH REQUIRED)`; upload
   confirmation failure ⇒ `UPLOAD STATE UNKNOWN` + reconciliation required
   (never auto-republish).
4. **Operator authority**: dead-letter requeue endpoint, stage re-run via
   existing cycle actions, credential rotate/clear, all audited.

## 3. Removals (this change)
- `MockVideoEngine`, `MockPublisher`, `MockAnalyticsProvider`,
  `MockTrendSource` removed from product registries/factories (test doubles
  live only inside tests/ as injected fakes).
- `/simulation` endpoints, run-config `simulation` flag, Simulation UI.
- `MOCK_*` env switches; `/system/mode` replaced by `/system/readiness`
  (PRODUCTION READY / BLOCKED with reasons).
- Frontend Simulation button/badges; mode pill becomes readiness pill.

## 4. Deferred (honest, not built)
Scene repair studio, Shorts repurposing, engagement inbox replies,
growth experiments, discoverability audits — require capabilities MPT/platform
APIs do not currently expose. They remain roadmap items and are surfaced as
such in-app where relevant (never faked).
