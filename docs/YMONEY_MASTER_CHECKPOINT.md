# YMONEY Master Checkpoint (2026-09-28)

Source spec (frozen, do not edit for progress): `docs/YMONEY_MASTER_PLAN.md`.

Status convention: ✅ COMPLETE · 🟢 WORKING · 🟡 PARTIAL · 🔵 NEXT · 🔴 MISSING · ⚠️ BLOCKED · 🧪 NEEDS VERIFICATION · ♻️ REFACTORED · ❌ FAILED

## Gap matrix (baseline audit 2026-09-23)

| Master-spec object | YMONEY has | Verdict |
|---|---|---|
| Workspace, Campaign, Opportunity, ContentItem+Variants, Video+Variant, PublishingJob, PublishedPost, PostMetric, ScheduleEntry, LearningPattern, MemoryRecord, AgentRun, Cycle, CostEntry, EventLog | models + autopilot + 22 agents + publishers + analytics | 🟢 WORKING (extend, don't replace) |
| Canonical editorial Timeline/Track/Clip, SceneGraph, RenderManifest, OTIO interchange | only event-history `/content/{id}/timeline` + motion `data-no-timeline` | 🟡 PARTIAL after this slice (model+engine+manifest, no editor UI yet) |
| Professional multi-track editor (split/trim/keyframes/waveform/autosave) | Studio list/detail pages, no editor | 🔵 NEXT (own plan: `react-timeline-editor` + wavesurfer behind canonical Timeline) |
| LongFormVideoPipeline (outline→chapters→scene graph→voice consistency) | short-video pipeline only | 🔴 MISSING |
| MediaUnderstandingPipeline (diarization, word timestamps, face tracking, active speaker) | virality scoring + reframe heuristics | 🟡 PARTIAL (upgrade path: WhisperX/pyannote/MediaPipe behind interfaces) |
| AudioEnhancementPipeline (derived assets, A/B) | FFmpeg filters + compressor/limiter | 🟡 PARTIAL (RNNoise eval pending) |
| DecisionEngine interface (boolean/choose/rank/classify/score/verify/route) | `engine/decision.py` functions + advisory `provider_scoring.py` | 🟡 PARTIAL (interface wrap pending; Jev optional) |
| BrowserIntelligenceAgent, AI Creative Director, generative UI catalog | none | 🔴 MISSING |
| AvatarProvider (MuseTalk/LivePortrait, commercial-safe detectors) | SadTalker/Wav2Lip/server lanes | 🟡 PARTIAL (provider interface + license audit pending) |
| BrandDNA, one-input→campaign, ExperimentEngine, retention→scene mapping | per-platform SEO, scheduler, learning patterns | 🟡 PARTIAL (campaign hierarchy exists, derivatives graph pending) |
| KnowledgeGraphProvider, global memory classes, social inbox, collaboration, professional exports | memory service + semantic retrieve; webhooks/API keys | 🟡 PARTIAL |
| LinkedIn/X/Threads/Pinterest/Snapchat/Bluesky publishers | YouTube/TikTok/IG/Facebook | 🔵 NEXT (SocialPublisher adapters) |
| Source connectors (Drive/Dropbox/S3/RSS/podcast/Zoom/...) | YouTube/URL/local upload | 🔵 NEXT |

## Phase status

### 🎞 Phase 1 — Canonical Timeline Foundation
✅ Canonical timeline model (`ContentTimeline`)
✅ Video/audio/text/caption/voice/music/sfx/broll/avatar track kinds
✅ OTIO-compatible export/import (dict-level; `opentimelineio` lib binding later)
✅ Timeline persists to database (migration 0013)
✅ Existing videos importable (`timeline_from_video`, duration fallback)
✅ Shorts representable (aspect-tagged, multi-ratio check)
🟢 RenderManifest producible (stable hash)
🟢 Version history (parent-linked `save_version` = undo/redo state primitive)
✅ Existing render engine untouched (describes, never replaces)
✅ Editor load/save routes (`GET/PUT /timelines`, versions, manifest, OTIO, from-video) with cross-workspace 404 isolation tests
🔵 Browser editor UI (tracks render, split/trim, waveform — own plan; routes are ready for it)
🔵 Premiere/DaVinci/Final Cut export adapters (own plan, via OTIO lib)

### P0 hygiene
✅ Baseline audit (this file + `docs/oss/OSS_COMPONENTS.md` + `docs/superpowers/plans/2026-09-23-ymoney-master-oss.md`)
✅ Migration lock (`test_migrations_hygiene.py`: 0003 pair allowlisted, replay no-op)
♻️ Vendored `MoneyPrinterTurbo/` worktree copy deleted (HTTP adapter is the only integration; 286 unstaged deletions visible in `git status`, uncommitted)
🔵 Unique-prefix enforcement for future migrations (hygiene test fails on new collisions)

## Evidence (Work 01 + Work 02 + Work 03 + Work 04 + Work 05 + Work 06 + Work 07 + Work 08 + Work 09 + Work 10 + Work 11, 2026-09-23/29)

### Work 11 (this slice) — Collaboration + Review + Export + Enterprise Ops
- Full suite: fast lane **1283 passed, 4 skipped, 13 deselected** (586s);
  slow lane **13 passed** (431s). Total **1296 tests, 0 failed**
  (+190 vs Work 10's 1106).
- Delivered in 8 lanes, each verified standalone then integrated:
  **F** foundation — `models/collab.py` (13 tables) + migration **0028**
  (fresh replay **28 applied / 0028_collaboration / REPLAY_NOOP**, 89 tables,
  all 13 Work 11 tables present) + `services/project_auth.py` (5 roles ×
  9 capabilities, project roles only ever NARROW workspace roles, ws-admin
  bypass, foreign id → **404 never 403**) + `api/v1/projects.py`;
  **D** `engine/timeline_diff.py` + additive `tip_version` /
  `manifest_hash_of` on `engine/timeline.py` + `base_version` PUT gate
  (optimistic concurrency — no silent last-write-wins);
  **R** `engine/collab/{reviews,comments,revisions}.py` + `api/v1/{reviews,
  comments}.py` — exact-version binding, approve re-verifies and 409s with
  `stale:true`, lazy staleness refresh, full state machine, anchored
  threaded comments (anchor/mention 422s, root-only resolve), revisions
  with **no auto-transition on edit**; **32 tests**;
  **X** `engine/exporter/{profiles,formats,verify,jobs}.py` +
  `api/v1/exports.py` — 8 builtin profiles, **14-format registry** with
  honest ffmpeg/encoder probes, verified exports (sha256 + ffprobe census),
  retry/cancel; **58 tests (56 fast + 2 slow)**;
  **L** `services/{activity,notifications,retention}.py`,
  `engine/archive.py`, `api/v1/{activity,archives,ops,notifications}.py` —
  append-only ledger (no mutator route exists), retention sweep that never
  touches audit rows or live-review assets, MANIFEST_ONLY / PORTABLE_ARCHIVE
  with secrets structurally excluded, failure-isolated ops overview;
  **FE-A** `/reviews` `/activity` `/exports` + nav; **FE-B** editor
  `CommentsPanel` / `ReviewStatusBar` / `VersionCompare` / `ConflictNotice`
  (409 reload behind `ConfirmButton` — unsaved work never dropped silently).
- Parallel lanes caught cross-lane defects at integration: lane R isolated a
  silent `on_event` failure to lane L's file; lane L then found a genuine
  **workspace-isolation hole on the notification write path** (`db.get`
  without a workspace filter let a workspace-B id resolve recipients in
  workspace A, and recipient filtering checked global user existence rather
  than membership) — both reads and recipient sets are now workspace-scoped,
  with 2 regression tests. Lane X's own probe-driven tests found **7 real
  bugs**, incl. `str.isalnum()` rejecting `pcm_s16le`, `.upper()` breaking
  `SHORTS_1080x1920` and `WebM` (ffmpeg got an extensionless filename), a
  codec check that silently never ran, a SQLite write-lock deadlock held
  across `jobs.enqueue`, an invalid ASS writer, and a verification check
  reading its config before it was written.
- **OTIO interchange data-loss bug fixed** (`engine/otio_adapter.py`, found
  by lane X, verified then fixed by the orchestrator): OTIO returns
  `AnyDictionary` / `AnyVector`, which are `Mapping` / `Sequence` but never
  `dict` / `list`, so `isinstance(extra, dict)` dropped every extended clip
  field and `list(md.get("effects"))` returned C++-backed objects that
  dangled after the timeline was collected. Before: `text`, `volume`,
  `speed`, `transform` lost + `json.dumps` failed + `repr` raised
  `ValueError: Underlying C++ AnyDictionary has been destroyed`. After: all
  fields preserved, JSON-clean, no dangling refs (`_plain()` deep converter).
- Security: workspace isolation asserted on every Work 11 surface (404 never
  403 for foreign ids); RBAC matrix locked by truth-table tests; archive
  structurally excludes `ApiCredential`, `WebhookSubscription.secret_enc`,
  connector `config_json`, `.env` and settings secrets; notification
  recipients filtered by workspace membership; no secrets in any committed
  file (scan: 43 new files, all hits are test fixtures / exclusion docs).
- Contract deviations, documented not hidden: R followed the §3 matrix over
  §5 prose where they conflicted (matrix self-describes as the lock) and
  added an uncontracted ws-floor so §7's matrix is enforceable; X's
  `list_formats()` takes no `db, ws` (availability is machine capability),
  `ARCHIVE_MASTER` is source passthrough, `AUDIO_ONLY.audio_codec` is null;
  FE lanes followed the real backend over the contract on `capabilities`
  keys (`no can_submit`), revisions path, ledger row shape, export target
  types, and the 200-row activity limit.
- Gates: fast **1283/0 failed** + slow **13/0 failed**; fresh migration
  replay **28 applied / 0028_collaboration / REPLAY_NOOP**; Postman regen
  (drift test green); npm build **exit 0, 93 modules**; ruff
  `F,I,SIM,UP` clean in all Work 11 files; OpenAPI **330 paths**
  (53 new).
- Git state: uncommitted working tree (no commits made).

### Work 10 — GlobalMemory + KnowledgeGraph + Source Connectors + Knowledge Center
- Full suite: fast lane **1095 passed, 4 skipped, 11 deselected** (365s);
  slow lane **11 passed** (327s). Total **1106 tests, 0 failed**
  (+149 vs Work 09's 957).
- Section 0 first (Work 09 deferred gaps closed): `APPROVAL_REQUIRED` strict
  toggle blocks the draft-send escape hatch when enabled (audited, opt-in);
  write-time `LABEL_SET` validation in inbox classify (read-time kept);
  bootstrap-guard test added; CAS `send_claimed_at` and all inbox safety
  semantics untouched (49/49 touched-battery green).
- Delivered in parallel lanes, each verified standalone:
  **Foundation** `models/knowledge.py` (6 tables) + migration 0027 (fresh
  replay 27 applied / REPLAY_NOOP) + orchestrator-owned shared
  `engine/knowledge/{freshness,normalize}.py`;
  **A** `GlobalMemory` (12 types; provenance-first Memory→Evidence→Source;
  missing provenance = UNVERIFIED; conflicts never overwritten;
  supersession keeps history; statuses FRESH/AGING/STALE/CONFLICTED/
  SUPERSEDED/UNVERIFIED; 24 tests);
  **B** `KnowledgeGraphProvider` (13 node kinds / 10 relationships,
  relational DB first) + `MemoryRetriever` (scope+relevance+evidence+
  freshness+confidence ranking, weights sum 1.0; Work 05 DecisionEngine for
  semantic re-rank only; 30 tests + 50 decision-engine regressions green);
  **C** `engine/sources/` connector ecosystem (local/url/rss/youtube/s3
  implemented; 8 catalog-only kinds honest UNAVAILABLE; durable sync:
  idempotent in-flight dedupe, cursor resume, retry/backoff, cancellation,
  failure isolation, checksum dedupe; 28 tests + 35 regression green);
  **D** knowledge API — 12 routes dual-mounted (24 OpenAPI paths),
  roles viewer=GET / member=memory+promote / admin=sources,
  `_bootstrap_source_jobs` guarded registration; `test_knowledge_api.py`
  **17 tests** (RBAC matrix, secret redaction, sync dedupe, dual mount,
  generic-500 no-echo, enum 422s);
  **E** `context_bridge` (budget-bound injection, never whole-store),
  `community_bridge.promote_insights_to_memory`, creation memory block
  (failure-isolated; 11 tests + 83 regression green);
  **F** source→content lineage (research bundle → content → asset →
  timeline; 26 tests + 46 regression green);
  **Scheduler** `recommend_response_windows` — recommendation-first from
  measured data, action only when `schedule_automation` opt-in (10 tests);
  **Frontend** `/knowledge` (4 tabs; honest 404/shape degradation; nav ◈;
  `npm run build` exit 0).
- Security: no secrets/OAuth/unnecessary PII in memory; source configs
  never serialized (`config_json` redacted, `has_credentials` only);
  cross-workspace isolation asserted on every surface.
- Gates: fast **1095/0 failed** + slow **11/0 failed**; fresh migration
  replay **27 applied / FRESH_OK**; Postman regen **43 folders / 329
  requests** + drift test; npm build **exit 0**; ruff `F,I,SIM,UP` = 19
  repo-baseline hits, **zero in Work 10 files**.
- Gate fixes: stale Postman collection regenerated; `test_sources` job-count
  query workspace-scoped (leaked across tests in the shared session DB).
- Git state: uncommitted working tree (no commits made).

### Work 09 (this slice) — Unified Social Inbox + CommunityManagerAgent
- Full suite: fast lane **942 passed, 4 skipped, 11 deselected** (291s);
  slow lane **11 passed** (295s). Total **957 tests, 0 failed**
  (+147 vs Work 08's 810).
- Delivered in 4 parallel lanes, each independently audited before close:
  **A** platform capability registry + LinkedIn/X providers, **B** community
  sync (inbound pull → classify → dedupe), **C** CommunityManagerAgent +
  autonomy/policy gate, **D** inbox API (22 routes) + unified Inbox UI.
- Two independent audits (tests + security) ran on the landed code; every
  HIGH/BLOCKER/MEDIUM finding fixed before close:
  - approve/send/reply-`send=true` endpoints wired to the real engine
    signatures (were fail-closed 500s), draft/convert/insights likewise —
    all now strict 200 tests against real policy/engine logic.
  - time-bomb fixtures seeded at `utcnow()-2d` (30-day analytics window).
  - autonomy alias layer keeps the shipped `{classes, caps}` API schema;
    bridge-proof test round-trips `daily_cap`/`rate_per_10min`/classes via
    the real `Workspace` row.
  - inbound-comment escalation scan for `is_auto` sends only (clean outbound
    to a "refund" comment → refused `escalation_inbound:refund_request`;
    human escape hatch intact, both asserted).
  - `apply_disclaimers` inside `send_action` — operator-typed sends carry
    required disclosures; stored `final_text` == text sent to provider.
  - double-send race closed by CAS claim `send_claimed_at` (120s TTL,
    committed BEFORE the provider call, released on failure): parallel
    sender → `send_in_progress`, stale claim → retry proceeds (both tested).
  - workspace filters on `_existing_action`/`_duplicate_reason`, generic 500
    detail + `ymoney.inbox` logger (no exception echo), `_denied_reason`/
    `sent: False` on denial paths.
- Platform capability registry drives honest per-platform capabilities;
  unknown platform → honest error, never fabricated. LinkedIn + X providers
  use the official APIs, **zero new dependencies**; live tests skip honestly
  without tokens (no secrets committed).
- Job handlers execute in-process end-to-end (classify/action/sync run real
  logic with honest counts; only the network-bound provider is a double) —
  `test_community_job_handlers_execute_in_process`.
- Migrations: 0026 (community tables + `send_claimed_at`) append-only;
  fresh-DB replay **26 applied, replay no-op**, column verified; hygiene green.
- Tests: audit run 156 passed across the 11 Work 09 files; final 6-file
  battery **99 passed**; new strict coverage includes reply `send=true`
  full policy gate, send-claim CAS/expiry, viewer-403 on 7 more RBAC routes,
  campaign job workspace filter, inbound escalation + human-send disclaimers.
- Postman: regen → **42 folders / 300 requests** (+44); drift test green.
- Frontend: `npm run build` green (unified Inbox: threads, interactions,
  drafts/approve/send, insights — RBAC-aware).
- Lint: ruff `F,I,SIM,UP` clean on all touched files (repo total 19
  pre-existing hits, all in untouched baseline files).
- Skips: 4 — 2 new honest live-token skips (LinkedIn/X), 1 pre-existing env
  skip, 1 network-dependent (pollinations 5xx); none in Work 09 code paths.
- Git state: uncommitted working tree (no commits made).

### Work 08 (this slice) — Brand DNA + Creative Intelligence
- Full suite: fast lane **798 passed, 1 skipped, 11 deselected** (139s);
  slow lane **11 passed** (267s, incl. campaign E2E). Total **810 tests,
  0 failed** (+42 vs Work 07's 768).
- New tests: 14 brand core (Lane A) + 15 creative director (Lane B) +
  16 brand integration (Lane C) = **45**, each verified green standalone.
- BrandDNA precedence (every resolve snapshots an append-only
  `brand_effective_configs` row; stored DNA never mutated):
  workspace defaults → BrandDNA → campaign → content → platform → overrides.
  Snapshot commit is **savepoint-aware** (stage-commit pattern): SQLite
  write-lock cascade fixed — fast lane **507s → 139s**, no durability loss.
- LLMs never override hard brand constraints: forbidden phrases, required
  disclaimers, approved voices, caption preset enforced across strategist /
  script / voice / variant / localization / UGC paths; lessons that conflict
  with brand are dropped + audited (`brand_blocked_lessons`), never applied
  (`test_learning_cannot_override_hard_brand_rules`).
- Creative Director: NL → typed commands → preview ChangeSet (diff +
  rerender/cost estimates, approval flags) → **apply via Work 02 timeline ops
  + versioning** (409 on stale preview) → undo; audit ledger
  `creative_commands`. Schema gate rejects unknown component types (422).
- Generative UI decision: json-render evaluated (Apache-2.0, active
  vercel-labs labs product) **NOT adopted** — internal approved-component
  catalog (`APPROVED_COMPONENTS` in `engine/creative/commands.py`) + whitelist
  renderer `CommandRenderer.tsx` (no eval / no dangerouslySetInnerHTML);
  record in `docs/oss/OSS_COMPONENTS.md`.
- Migrations 0024 (brands, brand_dna, brand_assets, brand_presets,
  brand_overrides, brand_effective_configs) + 0025 (creative_commands);
  both append-only/idempotent, max was 0023.
- API: 9 `/brands` + 7 `/creative` endpoints; workspace isolation + RBAC
  tests green.
- Degradation proven: brand module missing/unavailable → generation still
  succeeds with explicit `applied_brand: False` + `degraded` lineage markers;
  brand verifier crash maps to QC warning, never FAIL.
- Postman: regen → 41 folders / 256 requests (+16); drift test green.
- Frontend: `npm run build` green (`/brands` page + BrandDNA editor +
  effective-policy/verify panels, CreativeDirector panel in Editor,
  CommandRenderer whitelist, nav/route).
- Git state: uncommitted working tree (no commits made).

### Work 07 (this slice) — Localization + Avatar + UGC
- Full suite: fast lane **756 passed, 1 skipped**; slow lane **11 passed**
  (incl. campaign E2E, 362s total; E2E standalone 40.9s). Total **767
  passed, 1 skipped, 0 failed** (+69 vs Work 06's 698).
- New tests: 13 localization + 17 lipsync + 13 dubbing-plan (30 incl. dubbing) + 17 UGC + 15 avatar.
- SQLite: UGC pipeline stage commits (hook/script/b-roll LLM cost writes no
  longer deadlock the project write transaction — was 60s+ timeouts).
- OSS: MuseTalk **MIT code + MIT weights** (commercial OK, config-gated,
  pinned `0a89dec4`); LivePortrait **code MIT but InsightFace detection
  models non-commercial-research-only → BLOCKED-for-commercial-production**
  until detector replaced (MediaPipe/OpenCV path); neither vendored, zero
  new dependencies.
- Migrations 0021 (localized_contents/glossary_terms/localization_qc) +
  0022 (dubbing_plans/lipsync_jobs) + 0023 (avatar_profiles/avatar_outputs/
  ugc_projects); all append-only.
- Postman: regen → 39 folders / 240 requests; drift test green.
- Frontend: `npm run build` green (Localization.tsx + Ugc.tsx pages).
- Git state: uncommitted working tree (no commits made).

### Work 06 — E2E green
- Full suite: fast lane **686 passed, 1 skipped, 11 deselected**; slow lane
  **11 passed**; campaign E2E **1 passed** (40.6s). Total **698 passed,
  1 skipped, 0 failed** (+50 vs Work 05).
- New tests: 8 retention/durability + 15 features/experiments + 26 learning.
- SQLite fix: campaign E2E wall time **242.28s → ~41s** (per-stage commits,
  no durability loss; resume semantics preserved).
- Lint: ruff F/I/SIM/UP clean on all touched files.
- Frontend: `npm run build` green (Performance dashboard + Intelligence panels).
- Migrations: 0019 (retention/features/observations) + 0020 (experiments).
- OSS: jev-ultrafast MIT + jkudish/jev-mcp MIT verified under correct slugs
  (earlier 404s were wrong names); both reference-only, zero new dependencies.
- Git state: uncommitted working tree (no commits made).

### Work 05 (this slice) — E2E green
- Full suite: fast lane **636 passed, 2 skipped, 11 deselected**; slow lane
  **11 passed**; campaign E2E **1 passed** (242s, timeout-mark documented).
  Total **648 passed, 2 skipped, 0 failed** (+130 vs Work 04).
- New tests: 50 decision-engine + 56 router/context + 24 browser/verifier.
- Shadow: agreement measured vs deterministic baseline, candidates run ASSISTED/persist=False.
- Lint: ruff F/I/SIM/UP clean on all touched files.
- Frontend: `npm run build` green (Intelligence dashboard: decisions, routing,
  context, browser, verification, settings).
- Migrations: 0017 (browser runs) + 0018 (decision records, evidence ledger)
  applied via conftest on fresh DB.
- OSS: jev-ultrafast MIT + jkudish/jev-mcp MIT verified (correct slugs;
  earlier 404s were wrong names), both NOT integrated — reference-only.
  winnow + fast-claude-compaction MIT, concepts adopted, packages not installed.
- Git state: uncommitted working tree (no commits made).

### Work 04 (kept) — E2E green
- Full suite: fast lane **506 passed, 2 skipped, 10 deselected**; slow lane
  **11 passed**; campaign E2E **1 passed**. Total **518 passed, 2 skipped,
  0 failed** (+34 vs Work 03).
- E2E (`test_campaign_e2e.py`, slow): 90s lavfi master → derive handler →
  **5 diverse Shorts** (3 chapters, pairwise overlap <0.8) → **20 platform
  variants** (titles differ on all 4 platforms) → **2 real MP4 renders**
  (ffprobe: ~20-30s, 1080x1920, h264+aac, non-black frames) → publishing plan
  (21 items, master-first deps, paced dates) → idempotent schedule (21
  entries, re-run reuses) → master-gated publish → MockPublisher
  publications with variant+campaign linkage → seeded metrics roll up to
  Short/Master/Campaign with platform comparison → regenerate/generate-more/
  404 isolation all green.
- Lint: ruff F/I/SIM/UP clean on all touched files.
- Frontend: `npm run build` green (Campaigns workspace + approve-via-actions fix).
- Migrations: none new in Work 04 (0016 covers campaign tables).
- Git state: uncommitted working tree (no commits made).

### Work 02 (kept)
- Backend suite at Work 02 close: **468 passed, 1 skipped, 0 failed**.
- Render fixture: lavfi A/B/audio/image → edited-timeline MP4 verified by
  ffprobe (duration, 1080×1920, A+V streams) + frame means proving
  trim/reorder/cutaway.
- Lint: ruff F/I/SIM/UP clean on new files (incl. one real F821
  undefined-name caught and fixed); B008/PLW1510 remain repo-baseline idioms.

### Work 03 (this slice) — E2E green
- Full suite: fast lane **474 passed, 1 skipped, 11 deselected**; slow lane
  **10 passed**. Total **484 passed, 1 skipped, 0 failed** (+16 vs Work 02).
- Units: 11 passed (validation, strategy/chapters/script/verify, scenes,
  assets, proxy, gates, estimate, repair-route, regenerate-guard).
- Repair/resume: 2 passed (graphic fallback + loud voice failure; chunk
  cache resume with mtime proof).
- E2E: 3-min EXPLAINER → **236s rendered**; 5-min DOCUMENTARY → **299s
  rendered** (both real MP4, ffprobe duration/streams, editor read-path,
  QC, metadata; overshoot vs target is by design — measured audio rules).
- Frontend: `npm run build` green (LongForm workspace, memo-ized editor lanes).
- Lint: ruff F/I/SIM/UP clean on all new files.
- Migrations: 0015 applied via conftest on fresh DB + replay no-op asserted.
- Git state: uncommitted working tree (no commits made).

## Remaining gaps (honest)
- No HTTP routes for timelines yet — editor UI plan adds them with workspace isolation tests.
- OTIO is dict-level compatible, not `opentimelineio`-lib validated — lib binding is a later task behind the same functions.
- License re-verification still open for every pending OSS row (checklist in `OSS_COMPONENTS.md`).
- `timestamptz` migration still deferred (needs live Postgres).
- Work 08: `GET /brands` lists active brands only — archived brands are
  reachable by direct id (PUT) but not discoverable to un-archive (no
  `include_archived` query yet).
- Work 08: Creative panel undo reloads via `onApplied` but does not navigate
  to the possibly-new timeline id returned by `POST /creative/undo/{id}`.
- Work 08: effective-config snapshot history is stored append-only but only
  the current resolved policy (`GET /brands/effective`) is exposed — no
  snapshot-diff/audit UI yet.
- Work 09: `APPROVAL_REQUIRED` accepts a member's `draft`-state send and
  human sends bypass volume caps (documented escape hatch) — no forced
  approval click for human sends (security-audit NIT-8, deferred by design).
- Work 09: the inbox classify endpoint stores engine labels without
  vocabulary re-validation at write time; gates re-filter via `labels_of` →
  `LABEL_SET`, so behavior is safe (NIT-10, consistency-only, deferred).
- Work 09: backoff-window assertion in `test_community_sync.py:409` is
  wall-clock sensitive (~10s margin vs ~1.6s runtime; pattern flag only,
  LOW-3, deferred).
- Work 09: the guarded job bootstrap `except Exception: pass`
  (`inbox.py:1475-1483`) has no direct test — probe-verified that importing
  `app.main` registers all 4 handlers (NIT-1, deferred).
- Work 10: 8 of 13 source connector kinds are catalog-only (no config →
  honest `SourceConfigError`/UNAVAILABLE); only local/url/rss/youtube/s3
  are implemented and tested with mocks — no live credential-backed sync
  has run (no secrets in repo).
- Work 10: registering a `url` connector with blank config returns
  **201 + `status=UNAVAILABLE`** (health() converts SourceError to a
  status) instead of 422 — honest-by-design but contract-ambiguous;
  clarification deferred.
- Work 10: response-windows opt-in (`schedule_automation`) is surfaced
  inside `notes`, not as a top-level boolean — the UI must read notes
  (scheduler honesty keys are otherwise top-level).
- Work 10: memory confidence labels map to fixed numbers (low→0.3,
  medium→0.6, high→0.9) — numeric confidence has three levels of
  granularity, not continuous.
