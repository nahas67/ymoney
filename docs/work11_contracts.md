# Work 11 Contracts — Collaboration + Review + Export + Enterprise Ops

Authoritative lane spec for YMONEY Work 11. Lanes MUST follow these contracts;
report deviations here in "Wave status + deltas" (append newest-first).

Baseline: verified Work 10 repo (fast 1095 + slow 11 = 1106 tests, 0 failed;
migration 0027 latest; all uncommitted — NO git commits during Work 11).
Rules: no new dependencies, no network in tests (mocks only), migrations
append-only (0028 next), no secrets, workspace isolation on every surface,
read files <=120 lines/read (worker survival), PowerShell 5.1 (trust
$LASTEXITCODE, no `&&`, no sleep-waits, gates redirect to `$env:TEMP\opencode\*.txt`).

## 0. Scope / non-goals

Build: project-level RBAC, reviews (version-bound approvals), anchored
comments, revision lifecycle, semantic version diff, append-only activity
ledger, optimistic concurrency (no silent last-write-wins), professional
export center (verified formats + honest NOT_AVAILABLE), export profiles,
project archive (MANIFEST_ONLY / PORTABLE_ARCHIVE), enterprise ops views,
retention policy, internal notifications, UI (/reviews /activity /exports +
editor panels).

Do NOT rebuild: editor core, timeline engine, campaigns, memory, knowledge
graph, source connectors, publishing, BrandDNA, analytics, learning, inbox,
DecisionEngine. Preserve Work 01–10 behavior (all regressions must stay green).

## 1. Lane ownership map

| Lane | Owns (create/edit) | Depends on |
|---|---|---|
| **W11-D** diff+conflict | `engine/timeline_diff.py` (new), `engine/timeline.py` (additive tip resolver), `api/v1/timelines.py` (PUT gate + GET diff), `pages/Editor.tsx` (base_version + conflict notice), PUT call-site updates in existing tests, `tests/test_timeline_diff.py` | — (parallel with F) |
| **W11-F** foundation | `models/collab.py` (new, 13 tables), `migrations/versions/0028_collaboration.py` (new), `models/__init__.py` exports, `services/project_auth.py` (new), `api/v1/projects.py` (new), mount line in `api/v1/__init__.py`, `tests/test_project_auth.py` + `tests/test_projects_api.py` | — (parallel with D) |
| **W11-R** reviews/comments/revisions | `engine/collab/{reviews,comments,revisions}.py` (new), `api/v1/reviews.py` + `api/v1/comments.py` (new), mount lines in `api/v1/__init__.py`, `tests/test_reviews_api.py` + `tests/test_comments_api.py` + `tests/test_revisions_api.py` | F |
| **W11-X** exports | `engine/exporter/{__init__,profiles,formats,verify,jobs}.py` (new), `api/v1/exports.py` (new), additive `check_export` in `engine/intelligence/verifier.py`, `WEBHOOK_EVENTS` additions allowed, mount line, `tests/test_exports.py` (+ slow real-media test) | F |
| **W11-L** activity/archive/retention/notifications/ops | `services/{activity,notifications,retention}.py` (new), `engine/archive.py` (new), `api/v1/{archives,ops,activity,notifications}.py` (new), `WEBHOOK_EVENTS` additions, mount lines, `tests/test_activity_ledger.py` + `tests/test_archive.py` + `tests/test_ops_retention.py` + `tests/test_notifications.py` | F |
| **W11-FE-A** pages | `pages/Reviews.tsx`, `pages/Activity.tsx`, `pages/Exports.tsx` (new), `App.tsx` routes, `Layout.tsx` NAV | R, X, L routes |
| **W11-FE-B** editor panels | `pages/Editor.tsx` (comments panel, review status, diff view, Request Review/Approve/Request Changes), small components under `components/collab/` | R, D |
| **W11-INT** integration | Postman regen, full gates, checkpoint, report | all |

Shared files (`api/v1/__init__.py`): each lane appends its own include block;
if an edit fails from a concurrent change, re-read and retry. `engine/collab/__init__.py`
must stay import-light (F creates it empty; R/L import submodules directly —
never edit it). Never edit another lane's files.

## 2. Migration 0028 — `0028_collaboration.py` (F owns)

13 tables, conventions of `0027_knowledge.py`: `def upgrade(session) -> None`,
`text("CREATE TABLE IF NOT EXISTS ...")` + guarded `CREATE INDEX IF NOT EXISTS`,
no downgrade, append-only/idempotent docstring. PK/TS mixins as in
`models/knowledge.py`. Polymorphic refs are plain strings (no FK), matching
`content.py` lineage convention. All FKs `ON DELETE CASCADE` except noted.

```
projects(id PK, workspace_id FK ws, name, description,
         status TEXT default 'ACTIVE'   -- ACTIVE | ARCHIVED
         created_by FK users, archived_at NULL,
         created_at, updated_at)
project_members(id PK, project_id FK, user_id FK users, role TEXT,
         -- OWNER | ADMIN | EDITOR | REVIEWER | VIEWER
         created_at; UNIQUE(project_id, user_id))
project_targets(id PK, project_id FK, target_type TEXT, target_id TEXT,
         created_at; UNIQUE(project_id, target_type, target_id))
   -- target_type: content | campaign | timeline | localization | ugc_asset
reviews(id PK, workspace_id FK, project_id FK NULL, target_type TEXT,
         target_id TEXT, title, state TEXT default 'DRAFT',
         -- DRAFT | IN_REVIEW | CHANGES_REQUESTED | APPROVED | REJECTED | CANCELLED
         bound_version TEXT NULL,       -- exact version under review (timeline version str)
         bound_manifest_hash TEXT NULL, -- render_manifest hash of that version
         stale BOOLEAN default false, stale_detected_at NULL,
         created_by FK, created_at, updated_at, closed_at NULL)
   -- approval binds (bound_version, bound_manifest_hash); staleness = tip hash moved
review_assignments(id PK, review_id FK, user_id FK, assigned_by FK, created_at)
review_decisions(id PK, review_id FK, user_id FK, decision TEXT,
         -- APPROVE | REJECT | REQUEST_CHANGES
         bound_version TEXT NULL, bound_manifest_hash TEXT NULL,
         body TEXT, created_at)          -- append-only history
revision_requests(id PK, workspace_id FK, project_id FK NULL, review_id FK NULL,
         target_type, target_id, state TEXT default 'OPEN',
         -- OPEN | ADDRESSED | DISMISSED  (explicit transitions only)
         items_json,                     -- [{kind, description, anchor}]
         created_by, created_at, resolved_at NULL, resolved_by NULL)
comments(id PK, workspace_id FK, project_id FK NULL, parent_id FK comments NULL,
         target_type TEXT, target_id TEXT, anchor_json, body TEXT,
         author_id FK, mentions_json, version_ref TEXT NULL,
         resolved_at NULL, resolved_by NULL, created_at, updated_at)
   -- target_type: timeline | timestamp | time_range | scene | timeline_item | caption | asset
   -- anchor_json: {t_start?, t_end?, scene_id?, item_id?, caption_id?, asset_id?}
   -- version_ref = timeline version at creation (context only; comments never mutate content)
export_profiles(id PK, workspace_id FK NULL for builtin, name, preset TEXT,
         -- YOUTUBE_4K | YOUTUBE_1080P | SHORTS_1080x1920 | INSTAGRAM_REEL |
         --    TIKTOK | ARCHIVE_MASTER | AUDIO_ONLY | CAPTIONS_ONLY
         config_json, is_builtin BOOLEAN default false,
         created_by NULL, created_at, updated_at)
export_jobs(id PK, workspace_id FK, profile_id FK SET NULL, format TEXT,
         -- MP4 | MOV | WebM | MP3 | WAV | SRT | VTT | ASS | TXT | JSON | OTIO | FCPXML
         target_type TEXT, target_id TEXT, state TEXT default 'QUEUED',
         -- QUEUED | RUNNING | COMPLETE | FAILED | CANCELLED
         progress INT default 0, artifact_asset_id FK SET NULL, checksum TEXT,
         verification_json, error TEXT NULL, job_id TEXT NULL,   -- jobs.enqueue id
         attempt INT default 0, created_by, created_at, started_at NULL, finished_at NULL)
project_archives(id PK, workspace_id FK, project_id FK, mode TEXT,
         -- MANIFEST_ONLY | PORTABLE_ARCHIVE
         state TEXT default 'QUEUED', manifest_json, artifact_path TEXT NULL,
         checksum TEXT, size_bytes INT, error TEXT NULL,
         created_by, created_at, finished_at NULL)
notifications(id PK, workspace_id FK, user_id FK, kind TEXT, payload_json,
         read_at NULL, created_at)   -- INDEX(workspace_id, user_id, read_at)
retention_policies(id PK, workspace_id FK UNIQUE, audit_retention_days INT NULL,
         render_retention_days INT NULL, temp_asset_retention_days INT NULL,
         export_retention_days INT NULL, updated_by, updated_at)
```

Indexes: every table `ix_*_ws` on workspace_id where present; project_id on
children; `(workspace_id, created_at)` on reviews/comments/notifications.

## 3. Project RBAC (F owns `services/project_auth.py`)

Workspace roles stay `{viewer:0, member:1, admin:2, owner:3}`
(`auth_service.py:148`) — one auth system, project roles only NARROW within
what workspace role allows (never expand).

Project roles order: `VIEWER<REVIEWER<EDITOR<ADMIN<OWNER` (display constants
upper-case; stored as-is).

Capabilities matrix (project role -> allowed caps):

| capability | OWNER | ADMIN | EDITOR | REVIEWER | VIEWER |
|---|---|---|---|---|---|
| view_project | ✓ | ✓ | ✓ | ✓ | ✓ |
| edit_project | ✓ | ✓ | | | |
| edit_timeline | ✓ | ✓ | ✓ | | |
| comment | ✓ | ✓ | ✓ | ✓ | |
| request_revision | ✓ | ✓ | | ✓ | |
| approve | ✓ | ✓ | | ✓ | |
| export | ✓ | ✓ | ✓ | | |
| publish | ✓ | ✓ | | | |
| manage_collaborators | ✓ | | | | |

Rules (single function — in-handler, for polymorphic targets):
```
assert_capability(db, ws, user, *, capability,
                  project_id=None, target_type=None, target_id=None) -> None
```
- Workspace admin+ (role order >= admin) -> always allowed (governance).
- Resolve project: explicit project_id, or via `project_targets` lookup for
  (target_type, target_id). Target/project in another workspace -> 404 short
  detail. Project not found -> 404.
- Target NOT linked to any project -> fallback: collaboration caps
  (comment/request_revision/approve/view_project) allowed for any ws member;
  mutation caps (edit_*/publish/export/manage_collaborators) require the
  existing workspace floor (route's own `require_workspace_role`).
- Target linked -> user must be in project_members with the cap (matrix above)
  AND pass the route's workspace floor; missing membership -> 403 short detail.
- Thin FastAPI dependency for project-scoped routes:
  `require_project_capability(project_id_param: str, capability: str)` —
  resolves ws from path, applies same rules.
- Last-owner protection: cannot remove/demote the final OWNER; transfer makes
  old owner ADMIN. ws admin can always repair.

Workspace floors stay on routes via existing `require_workspace_role`:
collaboration routes = "viewer" floor (any member); timeline/content mutation
routes keep their existing member/admin gates unchanged.

## 4. Projects API (F owns `api/v1/projects.py`)

All under dual-mount convention (canonical `/workspaces/{workspace_id}/...`
only — flat mount only if trivial to add; R/X/L also canonical-only, see delta).

- `POST /workspaces/{ws}/projects` (ws member+) -> 201 project dict; creator
  becomes project OWNER.
- `GET  /workspaces/{ws}/projects` (viewer+) -> `{"items":[...]}`.
- `GET  /workspaces/{ws}/projects/{project_id}` (view_project) -> project +
  `members`, `targets` counts.
- `PATCH /workspaces/{ws}/projects/{project_id}` (edit_project) -> update
  name/description; status change NOT here (archive is lane L).
- `POST /workspaces/{ws}/projects/{project_id}/members` {user_id, role}
  (manage_collaborators; user must be ws member else 404) -> 201.
- `DELETE /workspaces/{ws}/projects/{project_id}/members/{user_id}`
  (manage_collaborators OR self-leave; last-owner -> 409).
- `GET/POST/DELETE .../targets` — add/remove linked targets
  (edit_project; target must exist in workspace else 404; allowed target_types:
  content|campaign|timeline|localization|ugc_asset).
- `POST .../transfer` {to_user_id} (OWNER; target must be current project
  OWNER or ADMIN? -> target must be a project member with OWNER/ADMIN;
  old owner -> ADMIN; audited) -> 200.
Error hygiene: generic 500 `{"detail":"internal error"}`, logger
`ymoney.collab`, no exception echo (repo convention).

## 5. Reviews (R owns)

Engine `engine/collab/reviews.py`:
```
create_review(db, ws, *, target_type, target_id, title, project_id=None,
              requested_by, reviewers: list[str]) -> dict
submit_for_review(db, ws, review_id) -> dict        # DRAFT -> IN_REVIEW
record_decision(db, ws, review_id, *, user, decision, body="") -> dict
cancel_review(db, ws, review_id, *, user) -> dict
list_reviews(db, ws, *, state=None, target_type=None, target_id=None, project_id=None) -> list
refresh_staleness(db, ws, review_id) -> dict         # recompute stale flag
```
- target_types: `project | content | timeline_version | campaign |
  localization | ugc_asset`.
- **Exact-version binding**: when target is `timeline_version` (or `content`
  whose current timeline is the subject), creation captures
  `bound_version = tip_version(...)` (resolver D adds to engine/timeline.py;
  if D not yet landed when R starts, F's contract note: R imports it lazily
  and D lands first in the same wave — coordinate by reading the file) and
  `bound_manifest_hash = manifest_hash_of(doc)`. Non-versioned targets bind
  version as NULL and staleness uses their row's `updated_at` only for
  information (stale flag only enforced for versioned targets).
- **Approve re-verifies binding**: `record_decision(APPROVE)` recomputes current
  tip hash; if != bound -> 409 `{"error":"review target changed since review
  was requested — re-request on the current version", "stale": true}` and
  review state -> CHANGES_REQUESTED? NO — stays IN_REVIEW with stale=true;
  approver must create a NEW review on the new version (or R may add a
  `reopen` path; minimal: 409 + stale). Decision rows store their own
  bound_version/bound_hash at decision time (append-only history).
- **Staleness on edit**: `refresh_staleness` computes tip != bound -> stale=true,
  stale_detected_at=now. It runs (a) lazily on every review GET/list, (b)
  explicitly when timeline PUT/ops bumps a version IF cheap hook exists —
  minimum requirement is (a): tests edit the timeline then GET the review and
  assert `stale: true` AND that a previously-APPROVED review no longer counts
  as valid (`stale: true` + response includes `approval_valid: false`).
  Approved state itself is NOT deleted (history), but any consumer must check
  `approval_valid`.
- State machine: DRAFT -> IN_REVIEW -> (APPROVED | REJECTED |
  CHANGES_REQUESTED) ; IN_REVIEW -> CANCELLED; CHANGES_REQUESTED -> IN_REVIEW
  (re-request after edits, rebinds version). APPROVED/REJECTED/CANCELLED are
  terminal. Illegal transitions -> 409 short detail. All transitions append a
  `record_event` (lane L provides `services/activity.py`; until it exists R
  may emit via `record_event` directly with kinds listed in §9).
- `ReviewAssignment`: created with review (reviewers list) or
  `POST /reviews/{id}/assignments` (request_revision/manage_collaborators cap).

Reviews API (`api/v1/reviews.py`, canonical dual endpoints):
- `POST /workspaces/{ws}/reviews` (floor viewer + project cap `approve`? NO —
  creating a review = who? content editors request review: floor viewer +
  project cap `edit_timeline` when project-scoped else ws member) — spec in
  route docstrings; tests lock the matrix:
  - viewer (no membership) POST review -> 403; member -> 201;
  - project-scoped: member with VIEWER project role -> 403; REVIEWER -> 201;
  - viewer GET reviews -> 200.
- `GET /workspaces/{ws}/reviews` (viewer) filters: state, target_type,
  target_id, project_id -> `{"items":[...]}`.
- `GET /workspaces/{ws}/reviews/{id}` (viewer) -> review + decisions +
  assignments + `stale`/`approval_valid`.
- `POST .../reviews/{id}/submit | /decisions | /cancel` —
  decisions body {decision: APPROVE|REJECT|REQUEST_CHANGES, body}:
  APPROVE/REJECT need cap `approve`; REQUEST_CHANGES needs
  `request_revision`; cancel needs creator or manage_collaborators.
- `POST .../reviews/{id}/assignments` {user_id} (request_revision cap).
- REQUEST_CHANGES auto-creates a RevisionRequest (items from body if given).

## 6. Comments (R owns)

Engine `engine/collab/comments.py`:
```
add_comment(db, ws, *, target_type, target_id, body, author,
            anchor: dict | None = None, parent_id=None,
            mentions: list[str] | None = None, project_id=None) -> dict
list_comments(db, ws, *, target_type, target_id, include_resolved=False) -> list[dict]
resolve_comment(db, ws, comment_id, *, user) -> dict     # sets resolved_at
reopen_comment(db, ws, comment_id, *, user) -> dict
```
- Targets: `timeline | timestamp | time_range | scene | timeline_item |
  caption | asset` (+ free anchor fields). `time_range`/`timestamp` anchors
  REQUIRE t_start (+t_end for range) in seconds (float >= 0, end > start);
  validation -> 422.
- Thread replies via parent_id (same target required else 422; max depth
  enforced or flat replies — flat thread under root is acceptable, report
  choice). resolve/reopen on ROOT comments only (reply resolve -> 404/422,
  report).
- mentions: list of user_ids validated as ws members (unknown -> 422), stored
  in mentions_json; emits notification (lane L) when notifications table ready
  (guard import — R may call `services.notifications` if exists, else skip
  with TODO note; final integration adds it. PREFER: R imports lazily with
  try/except ImportError and L fills it in the same wave? NO — R runs before L
  lands. Decision: R emits `record_event` kind `comment.mentioned`; L's
  notifications service subscribes to kinds by writing rows when IT creates
  events — notifications for mentions are created by R calling
  `services.notifications.notify(...)` guarded `try: from app.services
  import notifications ... except ImportError: pass`? Fragile. CLEANER: L owns
  notification creation for ALL kinds, triggered inside `services/activity.py`
  emit helper; R calls the SAME helper `activity.emit(ws, kind, actor, ...)`
  — but L lands in the same wave as R... Both depend on F only. R and L run
  PARALLEL. R needs L's emit helper. Contract: **`services/activity.py`
  (L) is a WAVE-1.5 dependency for R**: R may write its routes first using
  direct `record_event(...)` calls with the kinds in §9, and L provides
  `services/activity.py` with `emit(ws_id, kind, *, actor, target=None,
  version=None, message, data=None)` — integration pass (INT lane) refactors R's
  direct calls to `activity.emit` IF both landed differently. Simplest: BOTH
  lanes use `record_event` directly with §9 kinds and structured data_json
  {actor, target, version, project_id}; L builds the QUERY/ledger API over
  events; notifications are written by `services/notifications.py::on_event(...)`
  called from... events fan-out (webhooks) doesn't hit DB notifications.
  DECISION (final): `record_event` is the single emission primitive (both
  lanes); `services/notifications.py::on_event(db, ws, kind, data) -> int`
  is called by L's `notify_for_event` wrapper; R calls
  `app.services.notifications.notify_for_event(...)` IF IMPORTABLE (lazy
  try/except with pass) — L lands notification writes for review/export kinds
  it owns; integration test asserts notifications exist for: review assigned
  (R path), mention (R path), changes requested (R path), approval (R path),
  export completed/failed (X path) — X has same lazy-import option. INT lane
  closes any gap. Report this wiring honestly.
- Comments NEVER mutate content: test asserts tracks_json bytes unchanged
  after add/resolve.
- API (`api/v1/comments.py`): `POST /workspaces/{ws}/comments` (floor viewer
  + cap `comment` when project-scoped), `GET .../comments?target_type=&target_id=`
  (viewer, workspace-filtered, cross-ws target -> empty/404 consistent with
  repo: 404 on foreign target), `POST .../comments/{id}/resolve | /reopen`
  (cap `comment`, author or EDITOR+).

## 7. Revisions (R owns)

Engine `engine/collab/revisions.py`:
```
create_revisions(db, ws, *, items, target_type, target_id,
                 review_id=None, project_id=None, created_by) -> list[dict]
set_state(db, ws, rev_id, *, state, user) -> dict   # OPEN->ADDRESSED | OPEN->DISMISSED | ADDRESSED->OPEN(reopen)
list_revisions(db, ws, *, state=None, target_id=None) -> list[dict]
```
- items: `[{kind: trim|timing|asset_swap|text|voice|caption|brand|other,
  description: str, anchor: dict}]` — validate kind enum + non-empty
  description -> 422.
- **NO auto-transition**: timeline edits never flip state (test: edit timeline
  version, revision stays OPEN — "do not infer fixed because timeline
  changed"). Explicit set_state only; ADDRESSED records resolved_by/at.
- API: `POST/GET /workspaces/{ws}/revisions` + `POST .../revisions/{id}/state`
  (OPEN->ADDRESSED requires `request_revision` cap i.e. reviewer side;
  DISMISSED allowed for creator/EDITOR+; report exact matrix in tests).

## 8. VersionDiff (D owns) — see D brief

Engine: `diff_timeline_docs(before, after) -> {...}` structured diff
(added/removed/modified clips with trim/timing/asset/text/voice/metadata
changes, track adds/removes, metadata_changes, summary) +
`diff_brand_snapshots(before_dna, after_dna) -> {changed, added, removed}`.
Route: `GET /workspaces/{ws}/timelines/{id}/diff?from_version=&to_version=`
(viewer) -> `{from:{version,manifest_hash}, to:{...}, diff, brand_diff}`.
PUT gains REQUIRED `base_version` (422 missing, 409 stale — same detail shape
as operations endpoint) + version bump on success. Canonical
`tip_version(session, timeline_id)` + `manifest_hash_of(doc)` added to
engine/timeline.py (R and review binding REUSE these).

## 9. Activity ledger (L owns query + docs; ALL lanes emit)

Reuse `EventLog` (`models/ops.py:126`, append-only by convention) via
`record_event(workspace_id, kind, message, level, source, data)`
(`services/events.py:27`). Actor/target/version ride in `data_json`:
`{"actor": user_id, "target": {"type","id"}, "version": ..., "project_id": ...}`.

Required kinds (Work 11): `PROJECT_CREATED`, `TIMELINE_EDITED`,
`VERSION_CREATED`, `COMMENT_ADDED`, `REVIEW_REQUESTED`, `CHANGES_REQUESTED`,
`APPROVED`, `EXPORT_CREATED`, `PUBLISHED` — plus internal: `ARCHIVE_CREATED`,
`RETENTION_SWEEP`, `REVIEW_ASSIGNED`, `REVISION_REQUESTED`,
`EXPORT_COMPLETED`, `EXPORT_FAILED`, `PROJECT_TRANSFERRED`,
`RETENTION_POLICY_UPDATED`, `NOTIFICATION_*` not needed.
Convention: prefix new kinds as `work11.<KIND>`? NO — spec says bare names;
emit bare (PROJECT_CREATED etc.) AND add each to `WEBHOOK_EVENTS`
(`services/webhooks.py:27-49`) — whitelist silently drops unknown kinds.
Ledger API: `GET /workspaces/{ws}/activity?kind=&project_id=&target_type=
&target_id=&since=&limit=` (viewer) -> `{"items":[...]}` newest-first,
workspace-filtered. **Read-only**: no POST/PUT/DELETE routes exist for events
(append-only test: attempt via any exposed route -> 404/405; two emits ->
monotonically growing rows; no mutator in `api/`).

## 10. Enterprise ops / retention / notifications (L owns)

- `GET /workspaces/{ws}/ops/overview` (admin) -> aggregates:
  `jobs {by_status, failed_recent}`, `reviews {open, stale_approvals}`,
  `exports {by_state, failed}`, `storage {bytes, file_count}` (sum
  MediaAsset.file_size + walk STORAGE_ROOT fallback), `provider_health`
  (reuse `services/readiness.py` probes / system health internals — no new
  stack), `costs` (reuse costs service summary), `audit {events_last_7d}`.
  Every section failure-isolated (try/except -> `{"available": false}`).
- `GET/PUT /workspaces/{ws}/retention` (admin): GET -> policy row (defaults:
  all NULL = keep forever); PUT validates non-negative ints, max 3650 days ->
  422 else 200 + `RETENTION_POLICY_UPDATED` event.
- Retention sweep: job type `RETENTION_SWEEP` registered via guarded bootstrap
  (mirror `_bootstrap_source_jobs` / knowledge.py pattern; register idempotently
  in ops module import or route module). Sweep logic:
  `services/retention.py::sweep(db, ws) -> dict` deletes ONLY:
  render MediaAssets older than render_retention_days, export artifacts older
  than export_retention_days, temp assets older than temp_asset_retention_days.
  GUARDS: never delete an asset referenced by a live review/comment/lineage
  parent (query review targets + content lineage + project_targets); NEVER
  delete events/audit/review_decisions/comments — audit_retention_days, when
  set, is enforced by EXPIRY MARKING only (`EventLog.data_json.expired=true`?
  NO — keep simple: audit rows are never hard-deleted by sweep; the policy
  value is stored and reported, deletion of audit data requires an explicit
  future policy action — document honestly in report + test: sweep with
  audit_retention_days=0 still returns events_untouched > 0). Returns
  {"deleted": n, "skipped": {reason: n}, "events_untouched": n} + emits
  RETENTION_SWEEP event.
- Notifications: `services/notifications.py`:
  `notify(db, ws, user_id, kind, payload) -> dict`,
  `on_event(db, ws, kind, data) -> int` (maps event kinds -> recipient sets:
  review assigned/changes/approval -> review creator + assignees + target
  owner; mention -> mentioned user; export completed/failed -> job creator),
  `list_notifications(db, ws, user, *, unread_only, limit)`,
  `mark_read(db, ws, user, id | all)`.
  API: `GET /workspaces/{ws}/notifications` (own rows only; unread_only param),
  `POST /workspaces/{ws}/notifications/{id}/read`,
  `POST /workspaces/{ws}/notifications/read-all`.
  Kinds: `review.assigned`, `comment.mention`, `review.changes_requested`,
  `review.approved`, `export.completed`, `export.failed`.
- Archive (engine/archive.py):
  `create_archive(db, ws, project_id, *, mode, user) -> dict` and
  `get_archive(...)` / `download_bytes(...)`.
  MANIFEST_ONLY -> single JSON document. PORTABLE_ARCHIVE -> stdlib zipfile
  bytes: `manifest.json`, `project.json`, `timelines/*.json` (full docs +
  version list), `captions/*.srt|vtt`, `scripts/*.json` (ContentItem research/
  script fields), `research/*.json`, `branddna.json` (effective snapshot),
  `assets.json` (manifest: id, name, path-relative?, size, checksum, mime),
  `lineage.json` (content lineage chains), `exports.json` (export metadata).
  Media binaries: manifest-only entries (path/size/checksum) — NOT copied
  (honest, documented). EXCLUDE ALWAYS: any `ApiCredential`,
  `WebhookSubscription.secret_enc`, connector `config_json`, `.env` files,
  provider tokens, settings secrets, assets NOT under the project's linked
  targets (workspace-isolation: unrelated workspace assets never included).
  `project.status -> ARCHIVED` after archive (POST
  `/workspaces/{ws}/projects/{id}/archive` {mode} -> 201 archive row;
  `GET .../archives` + `GET .../archives/{id}/download` (FileResponse-like
  bytes; viewer of project), archived project still readable but edit_project
  -> 409 until unarchived `POST .../projects/{id}/unarchive` (admin)).

## 11. Export center (X owns)

Engine `engine/exporter/`:
- `profiles.py`: BUILTIN_PROFILES dict with 8 presets -> config_json:
  `{width, height, fps, video_codec, bitrate_kbps, audio_codec,
  audio_bitrate_kbps, audio_channels, captions: {enabled, formats[]},
  watermark: {enabled, text?, asset_id?}, color: {matrix, transfer} |
  null}`.
  Presets: YOUTUBE_4K 3840x2160/30/h264; YOUTUBE_1080P 1920x1080/30/h264;
  SHORTS_1080x1920 1080x1920/30/h264; INSTAGRAM_REEL 1080x1920/30/h264;
  TIKTOK 1080x1920/30/h264; ARCHIVE_MASTER 1920x1080/30 high-bitrate (or
  source-res — pick source passthrough, document); AUDIO_ONLY (audio only,
  no video stream — width/height null); CAPTIONS_ONLY (no media, captions
  only). Validation: unknown preset -> 422; invalid dims (<=0), fps outside
  1..120, unknown codec for format -> 422; AUDIO_ONLY/CAPTIONS_ONLY formats
  constrained (AUDIO_ONLY -> MP3|WAV, CAPTIONS_ONLY -> SRT|VTT|ASS|TXT).
  Brand/export policy defaults: watermark text falls back to BrandDNA brand
  name when watermark.enabled and no explicit text (best-effort, degrade
  silently if brand module unavailable).
  `seed_builtins(db, ws)` idempotent insert of builtin profiles per workspace
  (or global `workspace_id NULL` rows — DECISION: global rows workspace_id
  NULL + workspace may clone; simpler for tests: seed lazily on first
  GET /profiles if none).
- `formats.py`: FORMAT_REGISTRY: each format -> `{available(), export(...),
  verify(...)}`. Text: SRT (caption track -> cues, roundtrip via existing
  `parse_srt` providers/dubbing.py:172), VTT (generate + parse-back validator),
  ASS (generate + parse-back header/cue validation), TXT (transcript from
  text/caption/voice clips), JSON (timeline doc + project metadata envelope),
  OTIO (existing `engine/otio_adapter.py` export_timeline + roundtrip
  `:103/:135`). Media: MP4/WebM/MOV/MP3/WAV via ffmpeg subprocess (already
  present — renders run) with PER-FORMAT capability probe (encoder presence,
  e.g. libvpx for WebM) -> if ffmpeg/encoder missing: `available()=False`,
  state NOT_AVAILABLE with reason (honest). NLE: FCPXML via existing adapter
  gate; Premiere/Resolve -> NOT_AVAILABLE (keep `otio_adapter.export_formats()`
  honesty shape `:113-132`) — NEVER emit a fake xmeml/XML.
  `list_formats(db, ws, *, target_type=None) -> [{"format","available",
  "reason"?}]` — the ONLY source the API exposes.
- `verify.py`: `verify_export(db, ws, job) -> dict` -> runs:
  (1) file exists + size>0; (2) media formats: ffprobe via existing
  `probe_metadata` (`services/storage.py:96`) — expected streams (video
  formats: v+a; audio: a only), duration tolerance vs source (<=2% or <=0.5s),
  resolution matches profile when profile has dims; (3) checksum sha256
  recorded; (4) text formats: parse/roundtrip success; (5) job row persisted.
  Verdict: `{complete: bool, checks: [{name, passed, detail}], checksum,
  probe: {...}}`. Export is COMPLETE only if ALL critical checks pass; else
  FAILED with failed check names in error. ALSO wire CompletionVerifier:
  additive `check_export(...)` + append `"export"` to `KINDS`
  (`engine/intelligence/verifier.py:24`) so exports append ledger evidence.
- `jobs.py`: `run_export(db, ws, job_id) -> dict` executor (loads target,
  runs format exporter, verifies, persists state/artifact as MediaAsset
  origin="export" where media file; text formats store artifact as MediaAsset
  with text path under STORAGE_ROOT/ws or return inline content for small
  text? DECISION: all artifacts land as files under STORAGE_ROOT/<ws>/exports/
  + MediaAsset row (origin="export"), download route streams file).
  `enqueue_export(db, ws, *, profile_id, format, target_type, target_id, user)
  -> {job_id, export_id, queued}` using `jobs.enqueue(type="EXPORT_BUILD", ...)`
  (services/jobs.py:67, idempotency_key) + guarded handler registration
  `_bootstrap_export_jobs()` mirroring `_bootstrap_source_jobs`
  (knowledge.py:546). Retry: `POST /exports/{id}/retry` (FAILED/CANCELLED only
  -> re-enqueue, attempt+1, new job_id; max attempt 5 -> 409). Cancel:
  `POST /exports/{id}/cancel` -> `jobs.cancel_job` (jobs.py:135) + state
  CANCELLED (queued/running only; else 409).
- Export API (`api/v1/exports.py`, admin/member floors as noted):
  - `GET /workspaces/{ws}/exports/formats` (viewer) -> list_formats (honesty).
  - `GET /workspaces/{ws}/exports/profiles` (viewer) + `POST` (admin) +
    `PUT /profiles/{id}` (admin; is_builtin profiles: config overrides allowed?
    DECISION: builtin editable per workspace copy only -> POST "clone from
    preset"; PUT on is_builtin global row -> 409. Report actual.)
  - `POST /workspaces/{ws}/exports` (member + `export` cap when project-
    scoped) {profile_id, format, target_type, target_id} -> 201
    {export_id, job_id, queued:true} — 422 if format unavailable
    (NOT_AVAILABLE reason in detail) or profile/format mismatch (e.g. MP4 +
    CAPTIONS_ONLY).
  - `GET /workspaces/{ws}/exports` (viewer) + `GET /{export_id}` (state,
    progress, verification, failure reason) + `POST /{id}/retry|cancel`
    (member) + `GET /{id}/download` (viewer; FileResponse; text + media).
  - Response shapes locked for FE: export row = `{id, format, profile:
    {name, preset}, target: {type, id}, state, progress, verification:
    {complete, checks} | null, artifact: {url, size, checksum} | null,
    error: str | null, attempt, created_at, finished_at}`.
- Real media export test: SLOW-marked, lavfi -> render a tiny MP4 (existing
  pattern test_work02_api render / campaign E2E) -> export MP4 (re-encode or
  copy-with-verify), assert ffprobe duration/resolution/streams + checksum +
  COMPLETE. Plus WebM/MOV/MP3/WAV: at least ONE more real format (WebM or
  WAV) asserted if encoder present, else assert honest NOT_AVAILABLE path
  (probe-driven test: run capability probe, branch assertions — never fake).
  Subtitle exports: SRT+VTT+ASS generation from seeded caption track +
  parse-back roundtrip assertions. OTIO: export + `roundtrip_serialized`.

## 12. Frontend (FE-A, FE-B)

- `/reviews` (FE-A): list (state filters, badges), detail (target, bound
  version, staleness banner "Target changed since review — approval stale",
  decisions timeline, assignments, Approve / Request Changes / Reject buttons
  RBAC-aware — hide when no cap per GET review `capabilities` field R must
  include: `{"can_approve": bool, "can_request_changes": bool, ...}` computed
  via project_auth), revisions section.
- `/activity` (FE-A): ledger feed (kind filter, actor, target, version,
  timestamp, request id), read-only, pagination/limit.
- `/exports` (FE-A): formats grid (available/NOT_AVAILABLE + reason),
  profiles table (8 presets + config), new export form (target+profile+format
  with format/availability guard), jobs list (format, preset, progress,
  verification verdict, download link, failure reason, retry/cancel buttons).
- Editor (FE-B): Comments panel (target = current timeline; anchors: playhead
  timestamp/time_range/scene/timeline_item; threads, resolve/reopen, mentions
  input), Review status bar (current version, active review state, staleness
  warning, Request Review button, Approve/Request Changes when allowed),
  Version comparison (pick two versions -> renders diff from GET diff route),
  conflict notice on 409 (D adds the save-side; FE-B ensures reload path
  usable). Follow Editor.tsx panel conventions (showVersions toggle pattern,
  subcomponent + toolbar button; PageHeader/Card/Tabs/Badge/toast from
  components/ui.tsx; useFetch + wsApi).
- Nav (FE-A): add ◎ Reviews, ≡ Activity, ⇩ Exports to Layout NAV (grouped
  sensibly; keys/desc per existing entries).

## 13. Test matrix (Work 11 required tests -> owner)

| Required test | File | Lane |
|---|---|---|
| review exact-version binding | test_reviews_api.py | R |
| approval stale after edit | test_reviews_api.py | R |
| revision request lifecycle | test_revisions_api.py | R |
| comment timestamp/range anchoring | test_comments_api.py | R |
| comment workspace isolation | test_comments_api.py | R |
| review RBAC | test_reviews_api.py | R |
| semantic version diff | test_timeline_diff.py | D |
| stale-edit conflict | test_timeline_diff.py | D |
| activity append-only | test_activity_ledger.py | L |
| export profile validation | test_exports.py | X |
| real media export verification (slow) | test_exports.py | X |
| subtitle export | test_exports.py | X |
| OTIO export | test_exports.py | X |
| unsupported NLE honesty | test_exports.py | X |
| archive excludes secrets | test_archive.py | L |
| archive workspace isolation | test_archive.py | L |
| export retry/cancel | test_exports.py | X |
| notification events | test_notifications.py | L |
| project RBAC matrix | test_project_auth.py + test_projects_api.py | F |

## 14. Gates (sequential, `$env:TEMP\opencode\*.txt`, trust $LASTEXITCODE)

1. New-test batteries per lane (standalone).
2. Fast: `-m "not slow"`; Slow: `-m slow`.
3. ruff `--select F,I,SIM,UP backend scripts` (19 pre-existing repo hits
   expected; zero in Work 11 files).
4. `scripts/gen_postman.py` + `backend/tests/test_postman.py`.
5. `npm run build` (frontend touched).
6. Fresh migration replay (28 applied / REPLAY_NOOP) + hygiene.
7. App import + OpenAPI route probe (`app.openapi()["paths"]`).

## Wave status + deltas (newest first)

- **2026-09-29 W11 CLOSED** — all 8 lanes verified (D, F, R, X, L,
  FE-A, FE-B, FE-A2 + main-thread integration). Gates: fast **1283
  passed / 0 failed** (508s), slow **13 passed / 0 failed** (409s),
  replay **28 applied / REPLAY_NOOP**, ruff `F,I,SIM,UP` = 19 repo
  baseline / **0 in Work 11 files**, Postman regen **52 folders /
  377 requests** + drift test, npm build **exit 0**, OpenAPI **330
  paths** (+53). Tests: **190** across the 11 new files (project_auth
  8, projects_api 12, reviews 20, comments 12, revisions 10,
  timeline_diff 14, exports 58 [2 slow], activity_ledger 10, archive 8,
  ops_retention 21, notifications 17).
  Contract deltas (documented, not hidden): PUT `/timelines/{id}` now
  *requires* `base_version` (422 missing / 409 stale, ops-endpoint
  detail shape) with an in-place version bump — copy-on-write
  dispatch rejected as inconsistent with the frozen ops endpoint; R
  followed §3 capability matrix over §5 prose where they conflicted;
  X `list_formats()` takes no `db, ws` (machine capability);
  FE capability keys mirror the real backend (`no can_submit`).
  Incidents: out-of-lane `otio_adapter.py` edit (22:07) broke the
  OTIO roundtrip mid-gate — root-caused (`AnyVector` is a `Sequence`,
  not `list`/`tuple`) and fixed; a workspace-isolation hole on the
  notification write path was found cross-lane and fixed with 2
  regression tests; stale Postman collection regenerated.
- **2026-09-29 W11-D dispatched** (diff + PUT conflict + Editor notice).
- **2026-09-29 contracts written** (this file). Deltas: none yet.
