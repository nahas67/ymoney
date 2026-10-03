# YMONEY Production Gap Matrix — Work 11.5

Generated 2026-09-30. Baseline reconciled from the **actual** repository
(Work 12 complete), not the stale work-order premise. Every row was
independently verified by the orchestrator before being accepted; lanes never
wrote code.

Status legend: **FIXED** (changed + regression test), **DOCUMENTED**
(deferred with reason), **EXTERNAL** (needs credential/hardware/provider).

## Fixed in this pass

| # | Finding | Sev | Component | Evidence | Fix | Verification |
|---|---|---|---|---|---|---|
| A-F1 | `DEFAULT ""` in migration 0009 — a double-quoted **identifier** in PostgreSQL, certain failure | **BLOCKER** | migrations | `0009_request_id_columns.py:17` | Single-quoted `DEFAULT ''` | `test_no_double_quoted_default_in_migration_sql`; replay green |
| A-F2 | Derived table without alias in the 0002 dedupe — PostgreSQL mandates `AS` | **HIGH** | migrations | `0002_…:19-29,44-53` | `AS keep_newest` on both dedupe subqueries | `test_derived_tables_carry_an_alias` |
| A-F5 | `BOOLEAN … DEFAULT 0/1` in 13 places — no integer→boolean cast in PG | **HIGH** | migrations | 0005,0011,0012,0021,0024,0026,0027,0028,0029 | `TRUE`/`FALSE` everywhere | `test_no_integer_default_on_boolean_column` |
| A-F3 | `BrandOverride` unique in ORM, plain index in DDL → duplicates on migrated installs | **HIGH** | migrations/models | `0024_brand.py:120` vs `models/brand.py:129-137` | Dedupe + `CREATE UNIQUE INDEX` in 0024 **and** 0030 | `test_backfill_migration_exists_and_covers_the_drifted_constraints` |
| A-F4 | `uq_opportunity_ws_topic` declared in ORM with **no migration** | **HIGH** | migrations/models | `models/content.py:52` | Created (with dedupe) in 0030 | same |
| A-F4b | `scenes` column `idx` in legacy DDL vs `index` in ORM | **HIGH** | migrations/models | `0014_…:59` vs `models/assets.py:79` | Guarded `RENAME COLUMN` in 0030 | same |
| A-F6 | Only 0009 was ever edited post-ship; installs that ran the old version are marked applied yet lack the column | **MEDIUM** | migrations | `git log --follow 0009` → `6e2a209` | Backfill migration **0030** (idempotent, replays as no-op) | `test_backfill_replays_as_a_noop_on_a_fresh_schema` |
| A-F7 | `platform_variants` has no workspace index (ORM says `index=True`) | **MEDIUM** | migrations | `0016_…`, `models/campaign.py` | Index added in 0030 | same |
| B-F1 | QC override at **member** floor with a vacuous capability (`edit_project` is a no-op on unlinked assets) → any member could record a FAIL override | **HIGH** | RBAC | `media_intel_qc.py:202-214`, `project_auth.py:209-212` | New `assert_workspace_admin()`; override route now requires admin | `test_qc_override_requires_admin` |
| B-F1b | Same hole on the FAIL-apply path | **HIGH** | RBAC | `media_intel_edits.py:409-413` | Admin gate in `_qc_gate` override branch | covered by the same route gate |
| B-F2 | Review lifecycle (`submit`, `decisions`, `cancel`, `assignments`, revision create/state) reachable at the **viewer** floor | **HIGH** | RBAC | `reviews.py` (6 routes) | Floors raised to `member`; `submit` additionally checks the `comment` capability | `test_review_lifecycle_requires_member_not_viewer` |
| B-F2b | The Work 11 matrix had **locked** "viewer may create a revision on an unlinked target" — that lock was the defect | MEDIUM | RBAC/tests | `test_revisions_api.py::test_revision_unlinked_floors_actual` | The locked assertion was inverted to assert the **refusal**, with the reasoning recorded in the test docstring | same + `test_revisions_api.py` 10/10 |
| C-F1 | Any workspace admin could self-serve `allow_private:true` and pivot the fetcher to loopback / `169.254.169.254` | **HIGH** | connectors/SSRF | `adapters/url.py` (`_allow_private`) | Operator kill-switch `allow_private_connectors` (default **False**) honoured in both the config-time and runtime guards | `test_private_targets_require_the_operator_kill_switch` + `test_sources.py` |
| C-F4 | Workspace-controlled strings reached ffmpeg filter graphs unescaped (`drawtext` color/x/y; ASS `force_style`) | **MEDIUM** | rendering | `timeline_render.py:250-252`, `clips.py:473` | `_escape_filter_value()` + `_safe_fontcolor()`; `_sanitize_ass_style()` strips graph breakouts | covered by the render suites + `ruff` clean |
| D-F1 | `Video.status=READY` written with **no** CompletionVerifier call — a DB status could stand in for missing evidence | **HIGH** | verifier | `production.py:245-251` | Verifier now runs right after the status flip and ledgers evidence; mock renders excluded; never fails the render | `test_render_path_gates_spend_through_assert_can_spend` + verifier suites |
| D-F2 | Publication wrote `PublishedPost` on `remote_post_id` presence alone, no verifier | **HIGH** | verifier | `publish_flow.py` publish path | `check_publication` ledgered after the row is flushed (own session, never raises) | publishing + verifier suites |
| D-F5 | `check_video` stat'ed a raw DB path and matched assets by **bare basename** (never matches a `data/videos/<ws>/…` key), so real renders capped at `PARTIALLY_VERIFIED` | **HIGH** | verifier | `verifier.py:83-85,131-142` | Resolve via `managed_path` first (fallback keeps legacy rows working, and reports which path was used); match on exact key **or** same-workspace suffix **or** legacy basename | `test_video_checker_uses_the_storage_boundary_and_matches_storage_keys`, `test_asset_lookup_accepts_workspace_relative_storage_keys`, `test_verifier.py` 15/15 |
| E-F1 | **`assert_can_spend` had ZERO call sites** — daily/per-video caps were read but never enforced | **CRITICAL** | budget | `cost.py:109-118` (def only) | Pre-spend gate in the render path before `engine.submit`; over-budget marks the video FAILED, emits `video.generation.budget_blocked`, raises | `test_render_path_gates_spend_through_assert_can_spend` |
| E-F2 | Browser + DecisionEngine spend never reached `CostEntry`, so caps could not see it | **HIGH** | budget | `browser.py` (local counter), `decision.py` (record only) | Both routes ledger via `track_cost` after commit (≤0 stays a no-op) | `test_cost_ledger_is_written_for_browser_and_decision_spend` |

## Documented / deferred

| # | Finding | Sev | Component | Deferred reason |
|---|---|---|---|---|
| A-F8 | `0029.parent_asset_id` has no FK in the DDL (ORM declares one) | MEDIUM | migrations | Additive-only column already shipped; adding an FK needs a dedupe pass on live rows. Fold into the next media-intel migration. |
| C-F2 | DNS-rebinding TOCTOU: the host is validated, then httpx re-resolves | MEDIUM | connectors | Fixing properly needs connect-by-IP + `Host` header plumbing through the shared fetcher — a behaviour change to a path 5 connectors depend on. SSRF guards still apply on every hop. |
| C-F3 | Trend-source `config_json` is returned to viewers unredacted | MEDIUM | API | Currently safe (trend creds come from `get_credential`, not config). Any future secret-bearing trend config would leak — needs a per-kind secret-key list. |
| C-F5 | Webhook subscriptions may target `http://localhost` and dispatch without a private-IP recheck | MEDIUM | webhooks | Operator/admin-configured target; a private-IP recheck at dispatch is the right fix but changes webhook semantics for on-prem deployments. |
| C-F6 | S3 connector accepts any bucket name from a workspace admin | MEDIUM | connectors | Needs an operator bucket allowlist; bucket scoping already limits blast radius to operator credentials. |
| C-F7 | RSS/YouTube parse with `ET.fromstring` (no entity-expansion guard) | LOW | connectors | Bounded by the 10 MB fetch cap; `defusedxml` would be a new dependency, which the no-new-deps rule forbids. |
| C-F8 | S3 error text can echo endpoint/key fragments in a 422 | LOW | connectors | Generic message + server-side log. |
| C-F10 | `timeline_render` `mkdtemp` is never removed | LOW | rendering | Disk fill only over many renders; temp cleanup is a follow-up. |
| C-F11 | Avatar server lane fetches `video_url` from an operator-controlled server with no validation | EXTERNAL | avatars | Trust boundary is the operator, not a tenant — out of scope unless the threat model covers a malicious operator. |
| B-F3 | Inbox public sends at member floor (campaign publish is admin) | MEDIUM | RBAC | The engine policy gate is the backstop; whether member approve+send is intended needs a product decision, not a mechanical fix. |
| B-F4 | Comment routes sit at the **viewer** floor (`comment` capability admits REVIEWER) — a workspace viewer can write/resolve comments | MEDIUM | RBAC/comments | Documented, not changed: `project_auth`'s documented rule is explicit that *collaboration* caps fall through to the route floor and comments are collaboration ("view_project/comment/request_revision/approve … pass for any workspace member, floor is viewer"). Commenting is a documented Work 11 design decision (`reviews.py` §6 / comments docstring), so raising the floor would contradict the locked matrix. Needs an owner decision if viewer-commenting should be closed. |
| B-LOW | `require_workspace_role` returns 403 (not 404) for non-members, leaking existence | LOW | RBAC | Consistent across ~180 routes; the intelligence lanes mask it locally. A repo-wide change is a contract decision. |
| B-LOW | `timelines.py` accepts a client `file_path` into `timeline_from_video` unvalidated | LOW | timelines | No evidence it is resolved to a disk read; needs a runtime probe. |
| B-INFO | `telegram.py` unlink/toggle never `db.commit()` | LOW | telegram | Pre-existing, unrelated to isolation. |
| D-F3 | No verifier kinds for campaign-complete, long-form, short render | MEDIUM | verifier | Each needs a new kind in the locked `KINDS` contract; `check_campaign` exists and is reachable via the manual verification route. |
| D-F4 | Localization/UGC/avatar/lip-sync QC reports are not ledgered | MEDIUM | verifier | Same contract-change reason (new kinds). |
| D-F6 | Export replay trusts the persisted verdict; a file deleted after COMPLETE still verifies | MEDIUM | verifier | Re-probing on every manual verify costs an ffprobe per call. |
| D-F7 | `PublishingPlan.items_json.{approval_state,publication_state}` duplicates `PlatformVariant.status` + `PublishedPost` | MEDIUM | campaigns | Two writers today (`publish_flow.py:412`, `api/v1/campaigns.py:505-508`). Collapsing to a single writer is a data-model refactor, not a patch — must not be done without a migration plan. |
| D-F8 | Status vocabulary fragmented (`COMPLETE`/`COMPLETED`/`READY`/`SUCCEEDED`/`DONE`, upper vs lower) | MEDIUM | all models | A vocabulary registry + lint test is the fix; migrating stored rows is out of scope for a reconciliation pass. |
| D-F9 | Legacy `brand_templates.py` still imported by 8 call sites | LOW | brand | Needs an owner to facade it onto `brand/dna.py`. |
| E-MED | Video-engine `mock` selectable in prod config (no factory guard) | MEDIUM | providers | `providers/video_engine/factory.py:get_video_engine` | Factory now raises `EngineNotConfigured` when `is_production` and `allow_mock_in_production` is false | `test_mock_providers_are_refused_in_production` |
| E-MED | Analytics mock lacked the `is_production` guard that publishing has | MEDIUM | providers | `providers/analytics/__init__.py:get_provider` | Parity guard added; new `allow_mock_in_production` escape hatch in `core/config.py` (default False) | same |
| E-MED | `AUTONOMOUS` autonomy auto-approves shorts; `approval_satisfied` defaults True | MEDIUM | campaigns | Changing a default that live automations may rely on needs an owner decision. |
| E-MED | Stale/UNVERIFIED memory is served (down-ranked, labelled) without revalidation | MEDIUM | knowledge | Working as designed and labelled; a revalidation SLA is a product decision. |
| E-LOW | `complete_json` retry drops `mock_fn` | LOW | providers | Test-double-only path. |
| F-MED | `wavlip_dir` discloses LRS2 non-commercial terms but has no `commercial_mode` refusal (unlike intel providers) | MEDIUM | avatars | Pre-commercial; should reuse the intel registry's refusal helper. |
| F-MED | CC-BY-4.0 attribution (wespeaker, community-1) absent from the product surface | MEDIUM | licensing | Providers are `REVIEW_REQUIRED`, so the commercial path already refuses; needs a credits screen before any enablement. |
| F-MED | `rollup_short` is N+1-shaped (per-variant query fan-out) | MEDIUM | analytics | Not optimized without a query-count measurement, per the no-unmeasured-optimization rule. |
| F-LOW | `OSS_COMPONENTS.md` staleness (faster-whisper "pinned in uv.lock"; soundfile "not used") | LOW | docs | Docs-only correction. |
| F-LOW | `.ruff_cache/` not in `.gitignore` | LOW | hygiene | Bundle with the release checkpoint. |
| F-LOW | Assert-thin provider suites (avatar/xkiro/voices ≈1.2–1.7 asserts/test) | LOW | tests | CI installs no provider packages, so contract-tier tests are the correct evidence here. |
| F-INFO | `test_zz_debug.py` — 1-line docstring, **0 tests**, 0 repo-wide references | LOW | tests | Verified safe to delete, but test-file removal requires a human decision, so it is left in place with this note. |
| F-INFO | `backend/data/ymoney.db` (8.6 MB) + `.repowise/wiki.db` (0.8 MB) | INFO | hygiene | Both **untracked and gitignored** (`data/`, `*.db`), generated dev/runtime artifacts, not fixtures. Left on disk deliberately (dev data); excluded from any release checkpoint. |

## Verified clean (no action)

- **Committed secrets**: none. `.env` is gitignored, untracked and never committed; the `.git` history is clean; matches are variable *names* in docs/config only.
- **Test quality**: 0 true zero-assertion tests out of ~1754 (8 AST-flagged all verify through helpers); no `unittest.mock` in production code; no sleeps beyond bounded polls; no order-dependent state.
- **`pass` / `NotImplementedError` triage**: 103 bare `pass` = 8 exception stubs + ~95 typed `except: pass` degradation guards; 7 `NotImplementedError` = all ABC/protocol stubs. **Reachable production placeholders: 0.**
- **LivePortrait / InsightFace**: zero code references repo-wide; correctly unshipped, commercial block enforced by absence.
- **Mock boundaries**: every mock path requires explicit operator opt-in and is labelled (`is_mock`, `mock://`, `[MOCK]`); `MockPublisher` is production-blocked.
- **Workspace isolation**: 189 mutating routes enumerated; zero client-supplied `workspace_id`; foreign ids 404.
- **Provider baselines**: no new runtime dependency; `uv.lock` contains zero ML packages (CI = honest-unavailable proof).
- **Canonical truth**: knowledge graph and GlobalMemory *reference* canonical rows; they do not duplicate publication, approval, campaign, cost or timeline state (D-F7's campaign exception is a MEDIUM read-model duplication, not a second source of truth for a *different* subsystem).
- **`GlobalMemory.list` post-limit band filter** (investigated during the gate re-run, **not a defect**): the band filter's Python re-check runs after `stmt.limit(n)`, which *looks* like the limit is applied to candidates rather than matches. Empirically refuted on a throwaway SQLite DB — the SQL stage already filters on the identical predicate (`coalesce(last_verified_at, created_at) > cut`, with `status = ACTIVE`), so the re-check can never drop an extra row. No code change made; a speculative "fix" here would have been a no-op wrapped in a false test.
- **`test_knowledge_memory.py::test_list_filters_order_and_limit_clamp`** is an **intermittent, pre-existing** failure (passes in isolation, in file-pair runs, and in full-suite run 3; failed only in full-suite run 2). Untouched by these fixes and no W11.5 file is involved. Contributing factor identified: this machine's clock has ~1 ms granularity (2000 `utcnow()` samples collapse to 2 unique values), so the 205 rows the test inserts in a tight loop share `created_at` values and the `created_at DESC, id ASC` tie-break becomes load-bearing. Recommended follow-up: give the test explicit distinct timestamps (as its own `_craft` helper already does for `days_ago`) rather than relying on clock resolution.

## Test isolation defect found and fixed during the gate re-run

| # | Finding | Sev | Component | Evidence | Fix | Verification |
|---|---|---|---|---|---|---|
| F-F1 | `jobs.enqueue` commits through its **own** `session_scope`, and `autopilot.start_schedule_sweep()` runs at app startup — so the suite's single shared SQLite file is **never** queue-empty. `_claim_next()` returns the oldest eligible job, so `test_scale_e7`'s "queue is empty" and "next claim is my job" assertions were order-dependent and failed intermittently in full-suite runs | **MEDIUM** | tests/isolation | `app/engine/autopilot.py:99`, `app/services/jobs.py`, `test_scale_e7.py:117,141` | `_drain_queue()` helper empties QUEUED rows before the two claim-order tests (assertions kept **unchanged**, not weakened) | `test_scale_e7.py` 9/9; full-suite gate re-run |
| F-F2 | **Calendar-dependent test that aged out on its own.** `test_brains_e6.py::test_youtube_channel_rss_parse` hardcoded RSS `<published>` dates and the source drops anything older than the `days` window (`age_hours > days * 24`). Entry `2026-08-01` crossed the 60-day boundary at **2026-09-30T10:00:00Z** and the test began failing with **zero code changes** — measured: age 1441.7 h vs a 1440 h window | **MEDIUM** | tests | `test_brains_e6.py:12-14`, `providers/trends/__init__.py` (`age_h > self.days * 24`) | Fixture is now built at runtime from `datetime.now(timezone.utc)` offsets (12 d / 59 d), so it can never age out. Intent (parse two entries, newer first) is unchanged | `test_brains_e6.py` 11/11 |

This is the general hazard the whole suite carries: any code path that writes
through its own committing session (the job queue is one) leaks rows past the
`db_session` snapshot rollback and makes later tests order-dependent. A
session-wide autouse queue purge would fix the class rather than the two
symptoms, but that changes shared fixtures for all ~1700 tests and belongs in
its own change with its own evidence.

## Defect introduced and caught by the gates in this same pass

| # | Finding | Sev | Component | Evidence | Fix | Verification |
|---|---|---|---|---|---|---|
| W-F1 | The W11.5 **D-F2** fix itself was wrong on first write: it ran the verifier in a **nested** `session_scope()` inside the publish transaction, and the ledger's `session.commit()` deadlocked against SQLite's already-held write lock (`OperationalError: database is locked` at `verifier.py:458` → `ledger.py:65`). It turned the E2E gate red | **HIGH** | verifier/publish | caught by `test_campaign_e2e::test_campaign_e2e_master_to_attributed_metrics` | Run the verifier on the **ambient** session (`s`) instead of a nested one — which also makes publication + evidence a single atomic commit | E2E test passes; slow lane re-run clean |
| D-F1 | `_mark_variant` opened its **own** `session_scope()` *after* the publish transaction had already `flush()`ed the `PublishedPost`. SQLite's write lock is held until the outer scope commits (`db.py:55`), so the nested write waited out `busy_timeout` (5 s), raised `database is locked`, and was swallowed by a bare `except Exception: pass`. **`platform_variants.status` was therefore never set to `PUBLISHED` on the success path** while the publish reported success — plus a 5 s stall on every publish | **HIGH** | campaigns | `publish_flow.py:412` → `:471`; hidden because the E2E test only asserted variant status on the **FAILED** path (which works, because nothing is flushed there yet) | `_mark_variant(..., session=...)` reuses the caller's session on the success path. The two FAILED-path calls deliberately keep their own session: they `raise` immediately, so writing on the ambient session would be **rolled back** | new success-path assertions in `test_campaign_e2e` (`status == "PUBLISHED"` + `published_post_id`); runtime 41.55 s → **12.43 s**, i.e. the 5 s stalls are gone |

**The transferable rule**: never open a committing session inside an ambient
write transaction. `verify_completion` / `append_evidence` commit internally,
so the caller must already own (or not hold) the transaction. The D-F1 wiring
in `agents/production.py` is safe *only by accident of ordering* — it runs
after `_update_video()` has already closed its own `session_scope()`, so no
transaction is open. That is a fragile invariant and is called out in the code
comment there.

**Class-level follow-up** (read-only audit of all 186 `session_scope()` sites):
`engine/agents/discovery.py:168` nests `DecisionEngine._persist` inside an open
scope and survives only because `autoflush=False` (`db.py:46`) leaves the
outer's dirty rows unflushed — one flush away from a hard deadlock.
`engine/agents/intelligence.py:79` and
`providers/publishers/platforms.py:392` (reached via `publish_flow.py:371`) are
nested but read-only under WAL. The pattern to copy is
`engine/knowledge/context_bridge.py:136` and `services/media_intel_runs.py:199`
(commit first, then emit).
