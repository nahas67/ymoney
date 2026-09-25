# YMONEY — Completion Plan (2026-09-20)

Source: analysis of `registry.py` (22 agents), `providers/publishers/*`, `providers/analytics/*`, `providers/trends/*`, `App.tsx` (22 routes), `CHECKPOINT.md`, `findings.md`, `ARCHITECTURE.md` drift.

Verification gates for every slice (per `YMONEY_V2_ARCHITECTURE.md`): workspace isolation test, permission/safety test, idempotency test, simulation e2e, `pytest`, `tsc+vite build`, API smoke.

## Phase 0 — Docs & hygiene (S, unblocks all) — DONE 2026-09-20 direct (specialists down)
- [x] 0.1 `ARCHITECTURE.md:24` `Agents (12)` → 22 + provider boundaries refreshed (verified via grep).
- [x] 0.2 `backend/tests/_debug_out.txt`, `test_pipeline.py.tmp_note` absent (already clean); root `*.log` gitignored, single sink `backend/data/logs/ymoney.log` kept — no delete of live logs.
- [x] 0.3 Duplicate `0003_*` guard verified (`runner.py:_warn_on_colliding_sequence`, `versions/README.md:20-23`) — renumber deferred, no behavior change.
- Owner: fixer-0. Files: `*.md`, `backend/tests/_debug*`, `data/`, `*.log` only. No behavior change.

## Phase 1 — Doctor parity (P0, M) — BACKEND + UI DONE 2026-09-21 direct
- [x] 1.1 `GET /api/v1/system/doctor` + extended `run_readiness` (11 probes, latency + remediation, fail-closed). `test_doctor.py` green.
- [x] 1.2 `SystemHealth.tsx` Status tab: doctor checks (latency inline, click-to-copy remediation on failures, blocking-failed badge, readiness fallback when doctor 404s) + orphan-row badge + elevenlabs added to voice-lab providers. `tsc -b && vite build` green.
- [x] 1.1 `GET /api/v1/system/doctor` + extended `run_readiness` (11 probes: llm/video_engine/ffmpeg/yt_dlp/tts/images/storage/database/trends/publishing/public_base, each `{ok→status, latency_ms, remediation}` fail-closed). `backend/tests/test_doctor.py` 3 passed + `test_api::test_health` green.
- [ ] 1.2 `SystemHealth.tsx` + `Setup.tsx` surface: one screen, copyable remediation, `npm run doctor` equivalent.
- Owner: fixer-doctor-backend (`services/readiness.py`, `api/v1/misc.py`, tests). fixer-doctor-frontend (`SystemHealth.tsx`) — sequential after backend contract frozen.
- Verify: unit probes mocked, e2e with all-missing returns 200 + per-probe remediation.

## Phase 2 — Thumbnails v1 (P1, M) — BACKEND + UI DONE 2026-09-21 direct
- [x] 2.1 Generative variants: `POST /videos/{id}/ai-covers` + file serve + `thumbnail {ai_cover_index}` picker. `test_ai_covers.py` green.
- [x] 2.2 Studio picker: Compare-covers modal gains Frames / ✨ AI tabs — prompt input (blank = topic + hook), Generate 3, grid pick, Set as cover; failure hint points at image-provider key. `api.ts aiCoverFileUrl` helper. `tsc -b && vite build` green.
- [x] 2.1 Generative variants: `POST /videos/{id}/ai-covers {prompt?,count≤3,size}` via `providers/images.py` + `save_media` → `ai-cover-{id}-{i}.png`, `GET .../ai-covers/{i}/file` (magic-byte media type, token support), `POST .../thumbnail {ai_cover_index}` picker. Fail-closed 503 with remediation when provider unhealthy. `backend/tests/test_ai_covers.py` 2 passed; `test_covers` + `test_doctor` green (7 passed).
- [ ] 2.2 Studio picker: show 3 candidates + current ffmpeg frame, select → `thumbnail_path`, published via YT path.
- Owner: fixer-thumb-backend, designer-thumb-UI. Verify: no API key → fails closed with remediation; YT upload uses selected thumb (test).

## Phase 3 — Approval queue (P1, M) — CORE DONE 2026-09-20 direct (no new status; QC=pending, APPROVED=released)
- [x] 3.1 Hold path verified: QC-passed + `require_approval_before_publish` → APPROVED + `review.required` event (`autopilot.py:828-833`); compliance `require_human` → APPROVED + hold (`autopilot.py:992-1002`); Telegram push already wired (`events.py:75-77` → `telegram_service.on_event`, `review.required` in notify kinds + 👀). Toggle already exposed (`safety.py:43,187`, `decision.py:161`). `POST /content/{id}/actions {approve}` enqueues upload idempotently (`content.py:412-434`, tested in `test_compliance`).
- [x] 3.2 Bug fix: `reject` passed schema validation but hit `KeyError` → 500. Now `reject` → FAILED + reason in `error` + `content.rejected` audit event (`content.py`). `backend/tests/test_approval.py` 2 passed; existing approve test green.
- Decision: no `APPROVAL_PENDING` status/migration — QC serves as pending, APPROVED as released; avoids state-machine migration for identical semantics.
- Owner: fixer-approval. Files: `models/content.py`, `api/v1/content.py`, `engine/autopilot.py`, `services/telegram_service.py`. Verify: isolation + idempotency + simulation e2e.

## Phase 4 — Public API + webhooks (P1, S-M) — 4.1a KEYS DONE 2026-09-20 direct, webhooks + Postman pending
- [x] 4.1a Scoped API keys: `WorkspaceApiKey` (workspace FK CASCADE, prefix, sha256 hash-only via `security.hash_token`, role viewer|member|admin, revoked, last_used_at) + migration `0011` + `api/v1/api_keys.py` (mint admin/list viewer/revoke admin over JWT, `GET .../me` key-only proof via `Bearer ym_...`/`X-API-Key`, hmac compare, cross-workspace 403). Plaintext shown once; hashes never leave the server. `backend/tests/test_api_keys.py` 1 passed.
- [x] 4.1b Webhooks: `WebhookSubscription` (workspace FK CASCADE, AES-encrypted `whsec_...` secret shown once, event allowlist, active) + migration `0012` + `api/v1/webhooks.py` (subscribe admin/list viewer/delete admin/`/{id}/test` ping) + `services/webhooks.py` (`webhook.dispatch` job: HMAC-SHA256 `X-YM-Signature`, 15s timeout, 429/5xx/network raise for backoff ×5 then DEAD, 4xx terminal) + fan-out in `events.record_event` (allowlisted kinds only, best-effort, idempotent `wh-{event}-{sub}`). Catalog: cycle.completed/failed, publish.done/failed/skipped, quality.failed/passed, review.required, budget.exceeded/warning, safety.autopause (+ webhook.test ping). `backend/tests/test_webhooks.py` 2 passed.
- [x] 4.2 Postman: `scripts/gen_postman.py` builds `docs/ymoney-postman.json` from live `/openapi.json` (27 folders, 141 requests, collection bearer `{{jwt}}` + `X-API-Key` variant for `/me`, variables baseUrl/workspaceId/jwt/apiKey). `backend/tests/test_postman.py` asserts coverage + committed-file sync.
- Owner: done direct. Verify: `pytest tests/test_postman.py -q` green.
- Owner: fixer-api. Verify: key leak test, cross-workspace denial, webhook retry test.

## Phase 5 — Voice ElevenLabs (P2, M) — DONE 2026-09-21 direct
- [x] 5.1 `ElevenLabsTTSProvider` (`providers/tts.py`: text-to-speech + voices + user-health, `xi-api-key`, default Rachel voice_id, speed clamped 0.7–1.2, fail-closed TTSError without key) wired in factory (`elevenlabs|eleven|xi|11labs`) + `tts_provider_status` (generic path) + `Settings.elevenlabs_api_key` + `tts.elevenlabs_api_key` credential (Settings → Connections) + `ELEVENLABS_API_KEY` template. Per-content voice/provider override already existed (`VoiceDesigner.design/design_batch`); now usable with any voice_id. Paid usage estimated at $0.20/1k chars (`EST_USD_PER_CHAR`, labeled estimate) recorded via `track_cost` in `design` + `design_batch` (was 0.0). `backend/tests/test_voices_elevenlabs.py` 7 passed; `test_voices_e2` green (21 total).
- Owner: fixer-voice. Files: `providers/tts.py`, `engine/agents/voice.py`, Settings UI strings only.

## Phase 6 — Engineering debt (background) — sweep + postgres docs + lint batch1 DONE 2026-09-21 direct
- [x] 6.3a Orphan sweep: `GET /system/orphans` (counts-only, fail-closed). Finding: Video/Variant/Job FKs enforced — only `PublishedPost.video_id` can dangle. `test_orphans.py` green.
- [x] 6.3b Postgres docs verified present (`DEPLOYMENT.md:19-52`); `run_at timestamptz` code migration deferred.
- [x] 6.2 batch 1 (safe auto-fixes only): `ruff --select I001,RUF100,UP034,UP037,RUF022 --fix` → 627→605 across 23 files. Postmortem (honest): a scoped `--select` makes RUF100 evaluate noqa usefulness against the subset only, so it stripped 5 load-bearing guards — restored `main.py` webhooks side-effect import, `conftest` metadata import, `test_pipeline` handler import, `llm.py` + `storage.py` BLE001 intent comments — and correctly removed one dead import (`autopilot.new_uuid`). Lesson recorded: RUF100 only with the full default selection. Triaged remaining 45 (F401/F841/SIM117/F811): safe F401 removals (avatar `os`/logger, clips `field`, motion `json`/`STORAGE_ROOT`, trends `timezone`×2, content `timezone`, tests `os`/`datetime`/unused imports) queued for batch 2; F841/SIM117 need per-site review; `safety.py` F811 duplicate endpoint defs need human review (routes still register — behavior kept).
- [x] 6.1 test split DONE: full-suite `--durations=15` run (380 passed, 269.89s) → top-8 slowest marked `@pytest.mark.slow` (compliance preflights 60.6s+10.0s, motion status 46.3s, vision_qc 31.7s, api full-cycle 30.9s, repurpose 6.5s, dubbing 4.6s, phase_d preregistration 4.2s ≈ 195s) → default `addopts` excludes slow (fast lane: 372 passed in 68.53s) → CI split into parallel `backend` / `backend-slow` jobs. Fixed missing top-level `pytest` import in `test_vision_qc.py` found by the split. Follow-ups (not marking away): `motion status_contract` 46s probe timeouts and compliance preflight render time are the real optimization targets.
- [x] 6.2 batch 2 F401 DONE: reviewed `--diff` preview first, applied 13 deletions + 2 manual (function-level `timezone` ruff skips); F401 now **zero** (627→**584** total). Caught + removed my own unused `utcnow` import in `webhooks.py`. Verified: trends neighbors + new tests 25 passed; reorder check 5 passed.
- [x] 6.2 batch 2 F841/SIM117 DONE: all 14 dead locals + 6 nested-with merges reviewed per-site — pure deletions (`job_ids`, `score`, `niche`, `n_dub`, `extra_note`, `vf`×2, `duration_ms`, test probes) and side-effect-preserving bare calls (`_get_cycle` existence guard, `run_single_cycle`, `real_submit`); `test_publishing_path` via ruff safe-fix. One intentional keep: `autopilot.py:202` lock+session nesting (merging reindents ~60 lines of the hottest function for zero behavior change). Verified: 125 passed across 16 files. Baseline 584→**564** (F401 zero, F841 zero, SIM117×1 intentional).
- [x] `safety.py` F811 DONE (human review): lines 165-302 were a byte-identical duplicate of lines 27-158 (bad merge — verified 0-diff via script; serving copy determined by `api/v1/__init__.py` imports). Deleted the duplicate (307→168 lines). Verified: live `/openapi.json` safety/decision/costs routes byte-identical before/after (4 paths), F811 clean, compileall green, decision tests pass.
- Remaining: `timestamptz` migration (Postgres-only; SQLite naive + UTC-only backend is documented behavior).

## Order
0 → 1 → 2+3 (parallel, no file overlap: thumb=`providers/images.py, videos`, approval=`models/content, autopilot`) → 4 → 5 → 6 background.

## Today's start (this session)
- Lane 0 docs+hygiene (fixer-0)
- Lane 1 doctor-backend contract (fixer-doctor)
- Lane 2 thumb-backend probe (read-only check: image provider → thumb candidate path, no UI yet)
- Designer/UI lanes gated until backend contracts land.

## Production fix (2026-09-22, found live): `GET /cycles` 500'd with StopIteration
when a cycle lacked a "select" summary — simplified to a direct `.get` chain
(topic None instead of crash). Regression test in `test_phase_d.py` green.

## P0 external-tech adoption (ranking/personas/webhooks/probe DONE; grounding BLOCKED)

- [x] Source quality probe (openshorts-adapted): `probe_source_quality` in `clips.py` (local ffprobe dims / yt-dlp metadata-only, never raises, fail-open with warnings incl. <720p softness note), `LinkMiner.mine` probes before acquiring + returns `source_quality`, `POST /assets/repurpose/probe` endpoint. 4 new tests green.

- [x] Link Miner ranking (openshorts-adapted): `trim_to_best` (score-desc contract kept, explicit start-time tie-break — old code relied on sort stability over pre-sorted input only), `moment_count_target` (1..12 clamp, `LINKMINER_MIN/MAX_MOMENTS` env A/B without deploy). 3 new tests in `test_repurpose_e1.py` green (13 passed with approval neighbors).
- [ ] Hook vision-grounding (openshorts `hook_grounding.py`) — BLOCKED: `llm.complete_json` is text-only and `vision.analyze` takes whole renders, not frames. Needs a multimodal LLM call (image bytes) first; scoped as its own slice when that exists. Technique notes (3 frames @1024px + transcript slice, env-gated, never-raises, before/after audit) recorded here for that slice.
- [x] Persona prompt injection (agency-agents-adapted, output shapes unchanged): Strategist + retention/packaging doctrine (3s hook, payoff pacing, pillar balance, title+thumbnail synergy); ScriptWriter + retention mechanics (no intro/dead air, 8-12s pattern interrupts, CTA ending); SEO + title-angles/hashtag-mix craft. Verified: phase_e/ab_variants/pipeline 20 passed.
- [x] ffmpeg loudness: NOT changed — engine already levels via acompressor+limiter with operator `bgm_volume`; LUFS normalization was explicitly rejected in-code (explodes on digital silence). Documented as intentional.
- [x] HookOptimizer scoring rubric (TikTok/Video-Opt signals): `command` hook type (stop/never/don't markers, 76) + number concreteness bonus (+4) + multi-marker tables; pattern lift untouched. 4 new tests + ab_variants/phase_e green (22 passed).
- [x] Per-job `webhook_url` (openshorts parity): `POST /content/repurpose {webhook_url, webhook_secret?}` — URL validated up front (422 before any work), completion enqueued as `webhook.dispatch` with inline URL + AES-encrypted secret (no subscription row), same HMAC/retry semantics, unsigned when no secret. `repurpose.completed` added to the event catalog (subscriptions + timeline). 2 new tests green (validation-first, signed + unsigned delivery).

## P1 external-tech adoption — provider scoring DONE 2026-09-21 direct

- [x] Scored provider selection (OpenMontage-adapted): `app/engine/provider_scoring.py` — static capability profiles x task context x config snapshot into ranked ProviderScores (7 dims at proven weights, per-dim reason + `explain()`), pure/deterministic, advisory-only (explicit override wins; `continuity` lets healthy incumbents win ties). Wired additively into Connections TTS/Images status (`ranked`, fail-closed). Lint: file-idiom BLE001 only. 9 tests green (ordering proofs: edge-default, cloners, budget, offline, stock, continuity, production-mock).
- [x] Doctor refinements (Agent-Reach-adapted): per-check `tier` (0 built-in / 1 configured) grouped in the UI; credential scrubbing (`://user:pass@`, `?key=` shapes) at the readiness output boundary; yt-dlp missing/broken/unreadable trichotomy with version in detail + fix commands. 3 new doctor tests green; frontend build green.
- [x] Semantic memory retrieval (Understand-Anything idea, no new deps): token-overlap scoring (`|Q∩D|/min`, stopword-aware via decision tokens — Jaccard would punish thorough records) in `memory.py` as `semantic_score` + `retrieve_semantic` (same capped targeted-access contract, carries `semantic_score`/`matched_terms`); `retrieve_for_topic` now unions substring hits and ranks by overlap (recall never shrinks, zero-overlap sinks). API `semantic` flag on both memory endpoints. 4 new tests + full phase_e green (17 passed).

## P2 external-tech adoption — community templates DONE 2026-09-21 direct

- [x] Community templates (OpenCreator-adapted): workspace-authored customs via settings (validated, attribution defaulted, optional source link) with no-collision vs built-ins; resolve/list/versions workspace-aware; POST/DELETE admin endpoints; isolation tested. 2 new tests green (10 passed with existing).
- [x] Dry-run dubbing (OpenCreator-adapted): `dry_run_dub` provider check (language/source/SRT/translation/voice/TTS/assemble, zero downloads/transcription/LLM/TTS/ffmpeg, nothing written) + `POST /assets/dub/dry-run` (viewer, always 200). 2 new tests green.
- [x] Reference-video-to-plan (OpenMontage-adapted): `StrategistAgent.plan_from_reference` — mines reference moments, shapes keeps/changes/cost/sample before production spend, observable agent run. 2 tests green.
- [x] Bilibili trend source (Agent-Reach-adapted search-API fallback): keyless `bilibili` kind (niche keyword query, `<em>` markup stripped, play/danmaku/recency velocity), fail-closed without query. 3 tests green. Docs synced (README catalog, AGENTS.md hunter row).
- [ ] `timestamptz` migration (Postgres-only; deferred — needs live Postgres to verify).

## UI v2 rewrite (2026-09-21, autonomous) — Studio Noir, dark-first

- [x] Foundation: `index.css` token revamp (all legacy classes preserved) + `ui.tsx` additions (Toasts/CopyButton/ConfirmButton/Accordion/SearchInput/Progress/Avatar) + Layout rewrite (glass chrome, workspace menu, mobile drawer, searchable palette, error toasts, 3-state theme) + theme-hook dark-first fix. Build green.
- [x] Pages batch 1: CommandCenter hero rewrite (control deck, budget bar, tiered gate, toasts).
- [x] New: Approvals queue (/approvals with inline approve/reject), Developers group (/developers/keys with once-only mint + revoke, /developers/webhooks with subscribe/ping/delete, /developers/templates with authoring + detail + delete). Build green (74 modules).
- [x] Pages batch 2: Trends (search + toasts + hover), Autopilot (cycle search + cancel toasts), Publishing (toasts + ConfirmButton disconnect + hover), Analytics (engagement bars), Memory rewrite (semantic toggle + debounced search + match badges + confirm delete + toasts), ContentDetail (all alerts → toasts + success feedback).
- [x] Pages batch 3: SystemHealth lab toasts, Intelligence (WhyPanel + cost stat cards + raw accordion), Studio (debounced search), Agents (fleet search + save toasts + hover), Calendar (toasts), Campaigns (toasts + hover), Login (glow hero), Brand (toasts), Setup (doctor endpoint + remediation rows), Integrations (toasts + ConfirmButton unlink + pairing-code copy), Assets (11 alerts → toasts + op success feedback), Settings (toasts + inline test output + elevenlabs/bilibili options + confirm remove). Zero `alert`/`confirm` left repo-wide. Inbox/Composer/Live needed no changes. Build green throughout.
- [ ] Pages batch 3: Campaigns, Brand, Assets, Settings, Setup, Integrations, Inbox, Composer, Live, Login, Memory semantic toggle.
- [ ] Delete nothing until unimported; final full build + click-through.

## Bug hunt (2026-09-22) — found live + by sweep, all fixed + tested

Fixed (behavioral):
- `GET /cycles` 500 StopIteration on cycles without select summary (found live) → safe `.get` chain + regression test.
- 7× null-unsafe `.get` chains on external/DB JSON (mpt version/tasks, oauth snippet, content decision ×2, reddit children, tiktok videos) → `or {}` hardening.
- `get_safety_settings` crashed on explicit-null/garbage settings (single point bricking every safety consumer) → default-fallback coercion + tests.
- LLM non-numeric `duration_seconds` killed strategy stage → 32 fallback + test.
- Voice batch non-numeric rate → honest TTSError + test.
- Repurpose moments: KeyError/lexicographic pre-filter bugs → parse-first validation with clear ValueError + tests (test caught the deeper bug).
- LIKE wildcards unescaped in memory/log/content search → shared `escape_like` + `escape` param.
- `motion.py` control-flow assert → MotionError; dead `or True` instagram filter removed; dead `STATUS_FLOW` removed; unused unpack → `_cfg`.
- NaN check documented (`noqa` with reason); ClassVar annotations (creation ×2, pexels); intelligence silent skips now debug-logged; security test tightened to ValueError.

Audited clean (no change, verified by read): Jaccard/division guards, avatar checkpoint gate, numeric casts, no bare excepts, naive-datetime consistency, audit logs exclude query strings (no token leak), test `_Ctx` dicts function-scoped, ISC004 concatenations intentional, E402 guards dead-correct.

Verification: 117 + 7 passed across affected files; ruff F841/F811/PLR0124/SIM222 clean on touched files.

## Bug hunt round 2 (2026-09-22) — index LOC, validation bypass

Fixed:
- Settings PUT accepted a raw `safety` subtree, bypassing SafetyBody ge/le validation (negative budgets, impossible QC bars) → 422 redirect to the validated endpoint + test. No legit callers affected (brand/templates only).
- motion.py control-flow assert → MotionError; connections empty image batch → 502 (was IndexError 500).
- Decision.first_reason property; 3 event-logging call sites hardened against future reason-less Decisions.

Audited clean: analytics/mpt/oauth index sites guarded; telegram empty-text guarded; avatar checkpoint gate covers; memory/test unpacks benign; ISC004 concats intentional; audit logs exclude query strings.

Ruff 584 → 563 (F401/F841/F811 zero; SIM117 ×1 intentional lock nesting).

## Master OSS plan P0 slice (2026-09-23) - timeline foundation DONE
- Plan: docs/superpowers/plans/2026-09-23-ymoney-master-oss.md (TDD, later phases split into own plans).
- Docs: docs/oss/OSS_COMPONENTS.md (in-use adapters + pending license checklist), docs/YMONEY_MASTER_CHECKPOINT.md (gap matrix + Phase 1 partial).
- Code: ContentTimeline model + migration 0013 + engine/timeline.py (create/validate/OTIO dict roundtrip/from_video/shorts/manifest/save_version). Render engine untouched.
- Tests: test_timeline_foundation (7) + test_migrations_hygiene (2) = 9 passed; +approval = 11; decision+api = 19 passed 1 deselected. Red-first confirmed (8 failed pre-impl). Ruff clean on touched files.
- Notes: load_migrations names lack .py (test fixed); FK needs real workspace (test fixed, constraint kept); vendored MoneyPrinterTurbo/ deletion stays uncommitted (HTTP adapter only).
- NEXT: editor UI plan (load/save routes + isolation tests), OTIO lib binding, long-form pipeline plan.

## Timeline routes slice (2026-09-23) - DONE
- Routes: app/api/v1/timelines.py (create/list/get/put/versions/manifest/otio/from-video) registered in api/v1/__init__.py. Invalid tracks -> 422, cross-workspace -> 404.
- Tests: test_timelines_api.py (4) red-first then green. Postman regen: 27/141 -> 28 folders/154 requests, test_postman green.
- Combined: postman + timelines + foundation + hygiene = 14 passed. compileall ok. B008 Depends-idiom matches repo baseline (CI lint informational); F401 fixed.
- NEXT: browser editor UI, OTIO lib binding, long-form pipeline plan.

## Work 01 (2026-09-23) - COMPLETE (partial: full editor explicitly out of scope)
- Gap matrix: docs/YMONEY_BASELINE_GAP_MATRIX.md. Arch docs: docs/architecture/CONTENT_MODEL.md + TIMELINE_ARCHITECTURE.md. Spec frozen: docs/YMONEY_MASTER_PLAN.md.
- Content graph: lineage cols (0014) + engine/content_graph.py + lineage/derive routes + content.derived event. MediaAsset + Scene models + media registry routes (key guard) + scenes routes + timeline.* / asset.registered / otio.exported events.
- OTIO: opentimelineio==0.18.1 pinned (Apache-2.0 verified), engine/otio_adapter.py sole importer, real JSON roundtrip tested. Render adapter manifest_to_render_request tested.
- Frontend: TimelinesPanel + Edit-timeline tab; npm run build green.
- Full backend: 445 passed, 1 skipped, 0 failed (421 fast + 19 heavy-fast + 5 slow). Media regression 41 passed 1 deselected.
- NEXT (Work 02): browser multi-track editor UI on these routes; OTIO NLE export adapters; long-form pipeline.

## Work 02 editor (2026-09-23) - PARTIAL (functional editor, no virtualization/proxies yet)
- Ops layer: engine/timeline_ops.py (12 typed ops, families, atomic batches) + clip schema extensions + OTIO full-clip preservation. Tests: 8 ops tests green.
- Render: providers/video_engine/timeline_render.py (layered segments, drawtext, captions, volume/tempo/fades, asset_id|video_id-only resolution) + fixture test proving trim/reorder/cutaway in pixels. Bugs fixed honestly: input-ordinal indexing, drawtext C: escaping, enable commas.
- Scene sync: engine/scene_sync.py + broll/repurpose adapters (payload timeline_id) + resync on ops commit. 5 tests green incl. real plan_scenes + agent paths.
- Routes: operations (409 stale) / versions list+restore / render->MediaAsset / export otio download + fcpxml (501 NOT_AVAILABLE if absent) / media file serve. 5 API tests green. Postman 168 requests.
- Frontend: Editor page (/editor/:timelineId) + timelineAdapter + wavesurfer 7.12.12 (BSD-3-Clause); react-timeline-editor NOT integrated (not on npm registry — documented). tsc+vite green.
- Full backend: 468 passed, 1 skipped, 0 failed. Lint F/I/SIM/UP clean on new files.
- NEXT (Work 03): long-form generation on this substrate; editor follow-ups: virtualization, proxies, NLE adapters when verified.

## Work 03 long-form (2026-09-24) - PARTIAL (engine complete+verified; campaign-derivation is Work 04)
- Models 0015: longform_projects/chapters + scenes.chapter_id/beats_json. Pipeline: durable chained stages, pending-stage resume, REVIEW/MANUAL gates, cancel, estimates, regenerate-guard, repair route.
- Stages: research(bundle+provenance)/strategy(11 formats, narrative structures)/outline(chapter budgets)/script(claim-linked, LLM-polish guarded)/verify(verdicts, risky surfaced)/scenes(types+beats as metadata)/assets(provider interface+recorded fallbacks)/voice(measured audio authoritative, pronunciation)/timeline(canonical, editor-ready)/QC(5-section report)/chunked render(retry+cache+concat)/metadata(titles/desc/yt-chapters/thumbnails).
- Bugs fixed honestly: AssetProviderError duplicate class, SQLite nested-session lock, create_empty kwarg, concat backslashes, CWD-proof absolute paths, thumbnail dict mixup, float-rounding chapter boundaries, shallow-JSON persistence rule, single-chunk cache destruction.
- E2E: 3-min EXPLAINER 236s + 5-min DOCUMENTARY 299s real MP4s (ffprobe verified, editor opens, QC, metadata). Full suite 484 passed.
- Wan2.2 evaluated (Apache-2.0 code+weights) NOT integrated (no GPU). Editor ClipBlock memoized; backend 1800-clip ops ~10ms.
- NEXT (Work 04): master->shorts campaign derivation on lineage.
