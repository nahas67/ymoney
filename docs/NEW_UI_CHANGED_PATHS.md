# NEW UI release candidate: changed-path reconciliation

Starting revision: `07b6d0f1e3154ef2888156879431a3fc11258fd6` (`main`). This
inventory accounts for the release candidate's paths; it is not itself a test
or release-readiness claim.

## Accounting

Current `git status --porcelain=v1 -uall` shows 117 release-candidate paths:
59 modified, 20 deleted and 38 added. The three private files under
`.planning/2026-10-07-release-closure/` are locally excluded by
`.git/info/exclude`, so they do not appear in status and are not part of the
release. The two standalone contract-metric scripts are intentionally retained
unchanged because they preserve reproducible counts not emitted by the current
generators. Worktree/index parity, the final staged inventory and the secret
scan must be repeated before commit.

## Modified tracked files

### CI, environment hygiene and backend behavior

- `.github/workflows/ci.yml` — explicit release checks, failure propagation and
  SHA-bound evidence workflow.
- `.gitignore` — ignore local environments, auth state and generated test
  artifacts without hiding release evidence.
- `backend/app/api/v1/telegram.py` — preserve the declared response contract in
  the test-safe path without sending a message.
- `backend/app/services/json_portability.py` — portable JSON predicates,
  including PostgreSQL key lookup and SQLite/PostgreSQL parity semantics.
- `backend/app/services/media_cache.py` — reject credential-shaped, malformed,
  OAuth and signed query/fragment URL keys, including nested and repeatedly
  percent-encoded metadata URLs, before persistent caching or legacy cache reads.

### Generated API contracts and audits

- `backend/app/schemas/contract_map.json`
- `backend/app/schemas/generated.py`
- `frontend/src/api/openapi.json`
- `docs/ANALYTICS_HONESTY_AUDIT.json`
- `docs/UI_CONTRACT_AUDIT.json`
- `docs/UI_CONTRACT_GENERATION.json`
- `docs/UI_ROUTE_RELEASE_MATRIX.json`

These are generator-owned artifacts and must be re-generated together and
checked for byte-stable second-pass output.

### Backend regression coverage

- `backend/tests/test_analytics_honesty.py`
- `backend/tests/test_compliance.py`
- `backend/tests/test_lipsync.py`
- `backend/tests/test_pipeline.py`
- `backend/tests/test_work15_5_media_cache.py` — cover OAuth tokens, encoded
  credential-bearing fragments, and unsafe nested or multiply encoded source
  metadata on cache writes and legacy reads.
- `backend/tests/test_work16_1_json.py`
- `backend/tests/test_work15_8_durable_and_budget.py`

### Frontend UI, contracts and tests

- `frontend/e2e/routes-a11y.spec.ts`
- `frontend/e2e/route-matrix.spec.ts` — resolve lazy screen imports and fail
  closed if endpoint discovery makes route probes vacuous.
- `frontend/index.html`
- `frontend/package-lock.json`
- `frontend/package.json`
- `frontend/playwright.config.ts`
- `frontend/src/app/App.tsx`
- `frontend/src/design-system/styles.css`
- `frontend/src/features/assets/Assets.test.tsx` — cover encoded/malformed
  credential keys, OAuth fragments, repeated userinfo and credential-free media
  preview attributes.
- `frontend/src/features/assets/Assets.tsx` — normalize credential keys, redact
  sensitive query/fragment/userinfo components, and preview via temporary blob
  URLs rather than credential-bearing media URLs.
- `frontend/src/features/brands/Brands.test.tsx` — verify logo previews use a
  header-authenticated fetch, token-free blob URL and cleanup.
- `frontend/src/features/brands/Brands.tsx` — load the workspace logo through
  authenticated blob fetch and revoke its temporary object URL.
- `frontend/src/features/command-center/CommandCenter.test.tsx`
- `frontend/src/features/command-center/CommandCenter.tsx`
- `frontend/src/features/projects/Projects.test.tsx` — assert the Projects
  screen does not mount the out-of-scope Long-Form workflow.
- `frontend/src/features/projects/Projects.tsx` — retain the existing project
  list and lazy-route export; remove the appended Long-Form workflow UI.
- `frontend/src/features/projects/ProjectDetail.test.tsx` — cover authenticated
  render and thumbnail previews with object-URL cleanup.
- `frontend/src/features/projects/ProjectDetail.tsx` — fetch render media and
  thumbnails with header auth; never place token-bearing URLs in media elements.
- `frontend/src/features/settings/Settings.test.tsx`
- `frontend/src/features/settings/Settings.tsx`
- `frontend/src/hooks/hooks.ts`
- `frontend/src/lib/api.ts` — add authenticated blob fetches for assets, video,
  thumbnails and workspace logos; keep access tokens in Authorization headers.
- `frontend/src/pages/Editor.tsx`
- `frontend/src/routes/registry.ts`
- `frontend/src/test/analytics-honesty.test.tsx`
- `frontend/src/test/release-hygiene.test.ts` — split CRLF and LF source lines
  consistently so clean Windows snapshots do not misclassify block-comment text.
- `frontend/src/test/openapi-contract.test.ts`
- `frontend/src/test/route-release-matrix.test.ts`
- `frontend/vite.config.ts`

### Contract generation and fixture tooling

- `scripts/audit_analytics_honesty.py`
- `scripts/gen_openapi.py`
- `scripts/gen_response_contracts.py`
- `scripts/gen_route_release_matrix.py` — reject route rows with zero measured
  endpoints.
- `scripts/infer_response_models.py`
- `scripts/run_backend_slices.py`
- `scripts/spec_request_builder.py`
- `scripts/ui_contract_observer.py`
- `scripts/ui_fixtures_orm.py`

### Existing documentation corrections

- `docs/MPT_TECHNOLOGY_MATRIX.md`
- `docs/oss/MONEYPRINTERTURBO_INTEGRATION.md`

## Added release tests, tooling and documentation

### Backend release tests

- `backend/tests/test_release_contract_generation.py`
- `backend/tests/test_release_docs.py`
- `backend/tests/test_release_gates.py`

### Frontend automation and browser coverage

- `frontend/e2e/design-shots.spec.ts`
- `frontend/e2e/release-smoke.spec.ts`
- `frontend/src/lib/api.test.ts` — verify media, thumbnail and logo auth use
  headers and token-free request URLs.
- `frontend/src/test/editor-media-auth.test.tsx` — guard Editor playback,
  waveform and render download URLs against query-token leakage.
- `frontend/src/features/automation/Automation.test.tsx`
- `frontend/src/features/automation/Automation.tsx`

### Release runners and evidence tooling

- `scripts/gen_new_ui_release_docs.py`
- `scripts/playwright.release.config.mts`
- `scripts/release_app.py`
- `scripts/release_pg_proof.py`
- `scripts/release_pytest.py`
- `scripts/release_support.py`
- `scripts/run_backend_gate.py`
- `scripts/run_release_e2e.py`
- `scripts/run_release_postgres.py`
- `scripts/verify_generated_artifacts.py`
- `scripts/verify_ternary_endpoints.py`
- `scripts/write_release_receipt.py`

### NEW UI release documents

- `docs/NEW_UI_CAPABILITY_MATRIX.md`
- `docs/NEW_UI_DESIGN_SYSTEM.md`
- `docs/NEW_UI_GAP_AUDIT.md`
- `docs/NEW_UI_RELEASE_EVIDENCE.json`
- `docs/NEW_UI_ROUTE_MATRIX.md`
- `docs/NEW_UI_VERIFICATION.md`
- `docs/NEW_UI_CHANGED_PATHS.md` — this reconciliation.

### Curated UI review screenshots

- `docs/shots/analytics.png`
- `docs/shots/automation.png`
- `docs/shots/campaigns.png`
- `docs/shots/command-center.png`
- `docs/shots/operations.png`
- `docs/shots/planner.png`
- `docs/shots/projects.png`
- `docs/shots/settings.png`
- `docs/shots/studio.png`
- `docs/shots/studio-editor.png`

## Deleted investigation artifacts

The following unreferenced scratch scripts are removed from the release
candidate. They are development probes, one-off mutation scripts or temporary
measurements, not runtime modules. Their relevant maintained outcomes live in
the current source, focused regression tests, contract reports or the dedicated
release gate tooling described above.

- `backend/_seed_pg.py` — scratch PostgreSQL seed/drill; not used by application
  or current release tests.
- `scripts/debug_app_routes.py` — replaced by maintained route traversal and
  contract-registry tests.
- `scripts/debug_child_models.py` — replaced by the ORM fixture seeder.
- `scripts/debug_nested_404.py` — request-shape investigation.
- `scripts/debug_registry_match.py` — replaced by maintained registry checks.
- `scripts/debug_remaining_endpoints.py` — replaced by generated contract reports.
- `scripts/debug_response_validation.py` — replaced by response-filtering tests.
- `scripts/debug_seed_bodies.py` — replaced by request-builder and fixture tools.
- `scripts/fix_caption_reachability_message.py` — one-off E2E test rewrite.
- `scripts/fix_caption_scroll_and_comment.py` — one-off UI test/comment rewrite.
- `scripts/fix_caption_test_order.py` — one-off test-order rewrite.
- `scripts/fix_studio_spec_races.py` — one-off Studio E2E rewrite.
- `scripts/move_transition_ui.py` — one-off component-move script; component is
  now mounted by `frontend/src/pages/Editor.tsx`.
- `scripts/probe_caption_clip.py` — one-off persistence probe; covered by E2E.
- `scripts/probe_gaps.py` — replaced by maintained generated gap reporting.
- `scripts/probe_inbox_send.py` — one-off observer probe; no runtime dependency.
- `scripts/probe_request_shapes.py` — replaced by the OpenAPI artifact and
  request-builder tooling.
- `scripts/probe_response_model_filtering.py` — replaced by focused filtering
  regression tests.
- `scripts/rewrite_transition_test.py` — one-off test rewrite; current E2E
  coverage remains.
- `scripts/show_remaining_gaps.py` — replaced by the generated contract report.

## Exclusions and release caveats

- The three `.planning/2026-10-07-release-closure/{task_plan.md,findings.md,
  progress.md}` files are working ledgers, not release content.
- `scripts/count_declared_responses.py` and `scripts/count_publication_mode.py`
  remain unchanged baseline utilities; their distinct measured counts are not
  duplicated in the generated contract reports.
- No local `.env`, credentials, browser storage state, database dump, cache,
  `node_modules`, `.venv`, `dist` or routine Playwright artifacts are intended
  for staging. A staged-diff and secret scan remains required.
- The release documents preserve three analytics provenance gaps, 12 known
  out-of-scope fabrications, and the limits of live-provider verification.
- Screenshots under `docs/shots/` are curated review snapshots; they are not
  generated test output.
