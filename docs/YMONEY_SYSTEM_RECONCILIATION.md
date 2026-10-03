# YMONEY System Reconciliation — Work 11.5

Date: 2026-09-30. Read-only reconciliation of Works 01–11 **and Work 12**
against the actual repository, followed by fixes for every
BLOCKER/CRITICAL/HIGH finding.

## 0. Premise correction

The Work 11.5 work order stated a baseline of **1296 tests** and "continue
after Work 11, do not begin Work 12". That premise is stale. The repository
at the start of this reconciliation was:

| Prompt claim | Verified reality |
|---|---|
| 1296 tests | **1754** (1687 fast + 67 slow), 0 failed |
| HEAD = Work 11 | `212d409`, in sync with `origin/main` |
| migrations → 0028 | **`0029_media_intelligence`** present (15 tables) |
| no `engine/intel/` | 23 files, 16 670 lines |
| no Editor panels | `frontend/src/components/intel/` present, build green |

Work 12 was already implemented, verified and reported. This pass therefore
reconciled **Works 01–12**, and the next feature phase is **Work 13**, not
Work 12. Findings are in `YMONEY_PRODUCTION_GAP_MATRIX.md`.

## 1. Method

Six read-only audit lanes ran in parallel (no lane could write code):
migrations/DB · RBAC/isolation · connectors+security · canonical
truth+CompletionVerifier · providers/autonomy/budget/memory ·
test-quality/licenses/perf/git. Every finding was then **independently
verified by the orchestrator** before being accepted, fixed, or deferred —
several lane claims were corrected during that step (see §5).

## 2. Architecture verdict

**One authoritative implementation per subsystem.** No abandoned duplicate
architecture, no dead provider module, and no stale adapter was confirmed.
The notable overlaps that *are* live:

- `engine/brand/brand_templates.py` (legacy) is still imported by 8 call
  sites alongside the canonical `engine/brand/dna.py` + `policy.py`
  (D-F9, LOW — needs an owner to facade it).
- `campaign/derive.py` and `campaign/derive_chain.py` are both imported on
  the derive path; they are not abandoned, just overlapping (LOW).

Terminal-status vocabulary is genuinely fragmented — `COMPLETE` vs
`COMPLETED` vs `READY` vs `SUCCEEDED` vs `DONE`, plus lower-case vocabularies
in the community/brand/creative/knowledge tables (D-F8, MEDIUM). Nothing
currently *misreads* a status because of it, so it is documented rather than
migrated.

## 3. Migrations & database

PostgreSQL is the production target; SQLite stays for dev/hermetic tests.

- **All 29 migrations are guarded and replay-safe** (`CREATE TABLE/INDEX IF
  NOT EXISTS`, inspector-based `ALTER` guards, dedupe-before-unique). A fresh
  replay applies **29** and the second run is a **no-op** (re-verified after
  this pass).
- **4 genuine PostgreSQL breakages fixed**: a double-quoted `DEFAULT ""`
  identifier (BLOCKER — certain failure), a derived table without an alias,
  and 13 integer defaults on `BOOLEAN` columns (no int→bool cast in PG).
- **Fresh installs mask most DDL risk** because the runner calls
  `Base.metadata.create_all` before migrations; the *upgraded*-DB path carried
  all of the real breakage. New migration **0030** re-applies, idempotently,
  every pre-0030 step an install could have missed: `request_id` backfill,
  publishing dedupe/UNIQUE, `BrandOverride` UNIQUE, the never-created
  `Opportunity` UNIQUE, the `scenes.idx → index` rename, and the missing
  `platform_variants` workspace index.
- All 102 ORM tables render cleanly under the PostgreSQL dialect (asserted by
  a new test).
- SQLite↔Postgres portability otherwise sound: `with_for_update(skip_locked)`
  is PG-gated, the GPU-slot ledger uses a UNIQUE-key CAS with savepoints
  (PG-sound), `ILIKE` is native on PG and emulated on SQLite, and no raw
  `RETURNING`/advisory-lock patterns exist.

**Runtime verification against a live PostgreSQL is `EXTERNAL/UNVERIFIED`.**
A local PG 18 server is installed, but an ephemeral cluster cannot start in
this environment (child processes die with `0xC0000142`), so no PG run was
faked — the PG findings are static analysis plus dialect compilation only.

## 4. RBAC / workspace isolation

189 mutating routes enumerated across 40 router modules.

- **Positive**: no route trusts a client-supplied `workspace_id`; every
  workspace floor comes from the path via `require_workspace_role`; foreign
  ids return 404; storage paths are resolved through `managed_path` /
  `validate_storage_key`.
- **One reviewed-and-kept exception**: comment routes sit at the viewer floor
  because `project_auth` documents collaboration caps (comment /
  request_revision / approve) as falling through to the route floor, and
  commenting is an explicit Work 11 design decision. Recorded as B-F4 MEDIUM
  for an owner decision rather than silently changed.
- **Two real privilege-escalation holes fixed**:
  1. A workspace **member** could record a QC override (the capability check
     is vacuous on unlinked assets, so the route floor was the only gate) —
     governance now requires **admin** via a new shared
     `assert_workspace_admin()`, and the FAIL-apply path is gated identically.
  2. The whole **review lifecycle** (submit, decisions, cancel, assignments,
     revision create/state) sat at the **viewer** floor — raised to `member`,
     with `submit` also checking the `comment` capability.
- Documented rather than changed: inbox public sends at member floor (needs a
  product decision); the repo-wide 403-vs-404 inconsistency.

## 5. Corrections made to lane claims during central verification

Verification was not rubber-stamping:

- Lane A reported `DEFAULT ""` at 0009:17 and a missing 0002 alias — both
  confirmed and fixed. Lane A also reported 13 `BOOLEAN DEFAULT 0/1` sites;
  confirmed 13, fixed all 13.
- Lane D claimed `check_video` "can never match" an asset. Confirmed the
  basename comparison was broken, but the *fix* needed care: routing strictly
  through `managed_path` broke 2 existing verifier tests whose fixtures store
  legacy rows. Final behaviour tries the boundary first, falls back for
  legacy rows, and reports which path it used.
- Lane C called `allow_private` "accepted-but-unused by design" for S3 and
  self-serve for URL. Confirmed the URL case and fixed it with an operator
  kill-switch.
- Lane E's CRITICAL (`assert_can_spend` never called) was confirmed by direct
  call-site search (1 definition, 0 calls) and is now enforced before
  billable render submission.
- Two lanes disagreed about `Pass`/placeholder counts; Lane F's triage (103
  `pass` = stubs + typed degradation guards, 7 `NotImplementedError` = all
  ABCs, **0 reachable placeholders**) was verified and accepted.
- `test_knowledge_memory.py::test_list_filters_order_and_limit_clamp` failed in
  one full-suite run and passed in isolation, in file-pair runs and in every
  later full-suite run — an intermittent, pre-existing flake whose cause is
  **not** in any W11.5 file. Contributing factor measured: this host's clock
  has ~1 ms granularity (2000 `utcnow()` samples collapse to 2 unique values),
  so the test's 205 tight-loop inserts share `created_at` values and the
  `created_at DESC, id ASC` tie-break becomes load-bearing. Left as a
  documented recommendation (give those inserts explicit timestamps) rather
  than a speculative test edit.
- A *different* intermittent red (`test_scale_e7::test_gpu_jobs_deferred_without_worker`)
  was root-caused and fixed — see §10.

## 6. CompletionVerifier coverage

Verifier checkers exist for `video`, `publication`, `campaign`, `research`,
`community_reply`, `export`. Auto-invocation previously covered **2 of 11**
DoD workflows (export, community reply).

Fixed this pass:

- **video** — `Video.status=READY` was written with no verification, so a DB
  status could stand in for missing evidence. The verifier now runs right
  after the status flip and appends evidence (mock renders excluded; a
  verifier failure never fails the render).
- **publication** — `PublishedPost` was created on `remote_post_id` presence
  alone. `check_publication` now runs after the row is flushed.
- Chasing the resulting E2E failure exposed a **pre-existing HIGH** on the same
  path: `_mark_variant` opened a nested committing session after the publish
  transaction had already flushed, so `platform_variants.status` was silently
  never set to `PUBLISHED` (and each publish burned a 5 s busy-timeout). Fixed,
  and the success path now asserts it — which the suite had never done.

Still uncovered and documented (each needs a new kind in the locked verifier
contract): campaign-complete, long-form, short render, localization, UGC,
avatar/lip-sync, connector sync, review/approval.

## 7. Providers, autonomy, budget

- **No silent mock-to-production path.** Every mock requires explicit
  operator opt-in and is labelled (`is_mock`, `mock://`, `[MOCK]`). The
  publishing factory was already production-blocked; this pass added the
  **missing parity guards** to the video-engine factory and the analytics
  factory, so `video_engine='mock'` / `mock_analytics` now raise in
  production unless the operator sets the new `allow_mock_in_production`
  escape hatch.
- **Budget enforcement was the CRITICAL**: `assert_can_spend` had zero call
  sites, so caps were read but never enforced. Now gated before render
  submission, and browser + DecisionEngine spend is ledgered so the caps can
  see it.
- Autonomy gates (Creative Director, inbox auto-send, community auto-actions,
  learning hooks, repair loops) were audited: hard constraints are checked
  ahead of AI decisions, and no provider fallback weakens a safety gate.
- Documented: the video-engine mock lacks a factory-level `is_production`
  guard, analytics lacks publishing's parity guard, and `AUTONOMOUS` autonomy
  auto-approves shorts.

## 8. Memory & knowledge

Provenance, freshness bands (FRESH/AGING/STALE), conflict preservation
(`CONFLICTED`, side-by-side), explicit supersession, workspace-filtered
retrieval, and ContextBudgetManager integration with `memory:<id>` citations
are all present and correct. Stale/unverified memory **is** served but
down-ranked and labelled, and anonymous AI content can enter as `UNVERIFIED` —
documented as MEDIUM (product decision), not a defect.

## 9. OSS / model licenses

`OSS_COMPONENTS.md` and `MEDIA_INTEL_LICENSES.md` reconciled against actual
imports: no ML package appears in `uv.lock` (every heavy import is a lazy
`find_spec` guard), so CI cannot silently satisfy a provider. **LivePortrait /
InsightFace: zero references repo-wide** — the commercial block is enforced by
absence. Documented MEDIUMs: the `wavlip_dir` LRS2 non-commercial lane has
disclosure but no `commercial_mode` refusal, and CC-BY-4.0 attribution for
`wespeaker` / `community-1` has no product surface yet (both providers are
`REVIEW_REQUIRED`, so the commercial path already refuses).

## 10. Repository hygiene

- 38 uncommitted entries before this pass (11 modified, 27 untracked Work 12).
- Both runtime DBs (`backend/data/ymoney.db`, `.repowise/wiki.db`) are
  **untracked and gitignored** — generated, not fixtures; deliberately left on
  disk (dev data) and excluded from any release checkpoint.
- `test_zz_debug.py` verified as a 1-line docstring with 0 tests and 0
  references — safe to delete, but test-file removal is a human decision, so
  it is documented instead.
- Measured performance (no optimizations proposed without measurement): app
  import 4.5 s, exporter.formats 2.9 s (OTIO), campaign derive 2.3 s,
  200-clip timeline validate 0.003 s, OTIO roundtrip 0.07 s, provider registry
  resolve 0.15 s → 0.0003 s cached. One N+1 shape flagged in
  `analytics.rollup_short` (deferred pending a query-count measurement).
- **Test isolation**: the gate re-runs surfaced a real order-dependency.
  `jobs.enqueue` commits through its own session and app startup enqueues a
  `system.schedule_sweep` job, so the suite's single shared SQLite file is
  never queue-empty; `_claim_next()` returns the oldest eligible job, which
  made two `test_scale_e7` assertions intermittently red. Fixed with a
  `_drain_queue()` helper that leaves the assertions untouched. The general
  hazard — any write through its own committing session leaking past the
  `db_session` rollback — remains and wants a session-wide fix with its own
  evidence.
- One suspected memory-listing truncation was investigated and **refuted**
  (`GlobalMemory.list` re-checks the freshness band after `stmt.limit()`; the
  SQL pre-filter uses the identical predicate, confirmed on a throwaway DB).
  No code and no test were written for it.

## 11. Gate results (final, post-fix)

| # | Gate | Result |
|---|---|---|
| 1 | Fast suite (`-m "not slow"`) | **1701 passed, 4 skipped, 67 deselected, 0 failed** (444.87 s, exit 0) |
| 2 | Slow suite (`-m slow`) | **67 passed, 1705 deselected, 0 failed** (349.51 s, exit 0) |
| 3 | E2E (campaign + longform, slow-marked) | included in lane 2 — all pass |
| 4 | Migration replay (fresh DB) | 30 applied; second run `REPLAY_NOOP`; 104 tables; 0 missing Work 12 tables |
| 5 | Postman drift | regenerated: 60 folders / 414 requests; `test_postman` + `test_migrations_hygiene` **3/3** |
| 6 | OpenAPI | import OK, **365 paths** |
| 7 | Backend compile | `from app.main import app` OK |
| 8 | Scoped Ruff (`F,I,SIM,UP`) | **19 findings, all pre-existing, 0 in any W11.5 file** (repo baseline is 19) |
| 9 | Frontend type-check | `tsc -b --force` **exit 0** |
| 10 | Frontend build | **exit 0** (2.95 s) |
| 11 | Security / RBAC battery | `test_security` + `test_capability_permissions` + reviews/revisions — all pass (30/30 on reviews+revisions) |
| 12 | Connector-security battery | `test_sources` **28/28** |
| 13 | CompletionVerifier battery | `test_verifier` **15/15** |

Combined named batteries (security + RBAC + E2E + verifier + connectors +
W11.5 + scale + brains): **89 passed**.

Test-count delta vs the pre-pass baseline (1687 fast-passed): **+14**, exactly
the new `test_reconciliation_115.py` battery. No pre-existing test was
removed or had its assertions weakened; the two suite-stability fixes
(`test_scale_e7`, `test_brains_e6`) changed *inputs*, not what is asserted.

## 12. Git policy

**No commits were made.** All fixes live in the working tree at
`212d409` (in sync with `origin/main`, 0 ahead / 0 behind).

- **37 modified** (tracked): 29 are Work 11.5 files, 8 are pre-existing
  Work 12 edits (`api/v1/__init__.py`, `main.py`, `models/__init__.py`,
  `models/assets.py`, `services/readiness.py`, `services/webhooks.py`,
  `docs/ymoney-postman.json` — regenerated by the drift gate —
  `frontend/src/pages/Editor.tsx`).
- **31 untracked**: 4 are Work 11.5 (`migrations/versions/0030_reconciliation_backfill.py`,
  `tests/test_reconciliation_115.py`, `docs/YMONEY_SYSTEM_RECONCILIATION.md`,
  `docs/YMONEY_PRODUCTION_GAP_MATRIX.md`), 27 are Work 12.
  Two of the Work 12 untracked files were also **edited** by Work 11.5 for the
  B-F1 admin gate: `api/v1/media_intel_qc.py`, `api/v1/media_intel_edits.py`.
- **0 deleted.** `test_zz_debug.py` was verified safe to remove (0 collected
  tests, 0 references) but is **left in place** — test-file removal is a human
  decision and the harness correctly refused it.
