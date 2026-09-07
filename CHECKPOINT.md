# YMONEY — Platform Checkpoint & Audit Note

**Date:** 2026-09-08 · **Branch:** `main` · **Remote:** github.com/nahas67/ymoney (private)
**Scope:** whole-platform audit — failures, missing pieces, and the improvement backlog
that would move YMONEY from "hardened demo" toward a world-class autonomous content
operating system.

> Reading guide: Section 1 = verified health baseline. Section 2 = open failures &
> risks (severity + evidence + recommended fix). Section 3 = improvement backlog by
> domain. Section 4 = consolidated priority queue. Every claim below was verified by
> source inspection or a live command this session unless marked *(verify)*.

---

## 1. Verified baseline

| Area | State |
|---|---|
| Backend | FastAPI app, module factory `app.main:create_app`, uvicorn on 127.0.0.1:8100. Migrations runner `app/migrations/runner.py` (filename-keyed, transactional, idempotent). |
| Agents | Exactly 12 in `engine/agents/registry.py`, matching README ("12-agent crew"). Registry validates capabilities at import (fail-fast). |
| Frontend | React 19 + Vite. All 23 pages under `frontend/src/pages` are routed in `App.tsx` (25 `<Route>` incl. index + 404) — **no dead screens**. `tsc -b && vite build` green. |
| Tests | Full backend suite passed **254** on the last completed run (batch-3 gate). All routes/doc claims below from source. |
| Render | **Live end-to-end verified** this week: two real 9:16 videos through the local `ffmpeg_avatar` engine (edge-TTS narration → scenes → word-timed captions → BGM → h264+aac). Captions honored `subtitle_position` top vs bottom; BGM honored file+volume and `none`. |
| Repo hygiene | `.gitignore` covers `.env`, `*.db`, `backend/data/`, root `data/`, logs. Only `.env.example` templates are tracked. **No secrets in git** (content-level sweep done before push). |

### Recently closed (hardening batches 1–3, all regression-locked)
1. `ApiCredential` reads/writes were ignoring `workspace_id` → now tenant-scoped (explicit arg > context var > legacy global) + collapse-on-write.
2. Skipped/rejected opportunities (`skipped_reason`, `recommendation SKIP/HUMAN_REVIEW`) are now excluded from selection.
3. User registration was non-atomic (workspace bootstrap could be lost on late failure) → one transaction + rollback test.
4. FFmpeg engine lost in-flight tasks on restart → resolves against engine task list, honest failure.
5. Schedule sweep claimed completion before queue insert → claim → enqueue → `QUEUED`; provider success → `DONE`; exhausted retries → `FAILED` via dead-letter hook (was an infinite re-claim loop).
6. Wholesale publisher exceptions marked jobs terminal `FAILED` before re-raise → queue retry found nothing claimable and "completed"; now stays `RETRYING`.
7. Refresh-token rotation is a single-use atomic claim (replay → 401).
8. Workspace Safety-Center budgets are read by cost enforcement (`budget_available`/`assert_can_spend` now read `Workspace.settings_json["safety"]`, global fallback).
9. Calendar API lists in-flight/failed entries with status; UI shows status chips; reschedule doubles as operator retry.
10. DB-level credential uniqueness (migration `0008`: collapse + partial unique indexes) + race-safe `set_credential`.
11. Silent `/auth/refresh` rotation wired into frontend API layer (one refresh attempt on 401).
12. Local video engine honors `subtitle_position`, `bgm_type/file/volume`, `voice_rate/volume`; idempotency `request_hash` covers all output-affecting fields.
13. CI pipeline (GitHub Actions: backend tests + informational ruff, frontend build) — see `.github/workflows/ci.yml`.
14. Test suite can no longer hang: `pytest-timeout` (240 s/test) added with documented markers; measured full-suite time **254 passed in 289 s** with servers stopped.
15. `.env.example` deduplicated (`VIDEO_ENGINE` appears once, defaults aligned to `ffmpeg_avatar`); empty "Analytics collection" section removed.
16. Migration versioning policy documented (`versions/README.md`); runner now warns loudly on duplicate numeric prefixes (`_warn_on_colliding_sequence`).
17. Single rotated log sink (`backend/data/logs/ymoney.log`, 10 MB / 14-day retention) — root-level unbounded logs eliminated.
18. Clip repurposing surfaced: `POST /content/repurpose` + Studio "Repurpose URL" button/modal.


---

## 2. Open failures & risks

### F1 — Full suite is slow and server contention pushes it over budget  ·  P2 · mostly fixed
**Evidence:** measured 254 passed in **289 s** with servers stopped. With the dev backend
running, the same run exceeded a 300 s budget (contention on SQLite/ports), which looked
like a hang.
**Fix (applied):** `pytest-timeout` 240 s/test turns any true deadlock into a visible
failure; CI runs the suite with servers down. Remaining: cut wall-clock time (the 5 min
suite is the slowest CI step) — profile slowest tests, split network/live tests behind a
marker.

### F2 — Duplicate migration sequence `0003`  ·  P2
**Evidence:** `migrations/versions/0003_video_progress.py` and
`0003_video_thumbnails.py` both exist. The runner keys on the *full filename* and sorts
lexically, so **both currently apply** — but the sequence number is ambiguous and any
future migration ordered "after 0003" has no guaranteed position between them.
**Fix:** rename to `0003_video_progress.py` + `0004_video_thumbnails.py` and renumber
`0004→0005 … 0008→0009`, or (safer for existing DBs) keep files, add a README in
`versions/` explaining the two-part 0003, and adopt a sequence check in the runner that
fails on duplicate numeric prefixes.

### F3 — `.env.example` is internally inconsistent  ·  P2
**Evidence:** `VIDEO_ENGINE` appears twice (lines 27 and 66). Many keys in the template
(`GOOGLE_TRENDS_ENABLED`, `MPT_*`, `NEWSDATA_API_KEY`, `KOKORO_BASE_URL`, `OPENAI_*`,
`YOUTUBE_CLIENT_*`, budget keys) are **not declared** on `core/config.py` settings and
are read ad-hoc via `os.environ` inside providers — an untyped, undocumented env surface.
**Fix:** single canonical section per engine in the template (local `ffmpeg_avatar`
default vs MPT), declare remaining keys on `Settings`, or centralize an `env()` accessor
with names documented in one place.

### F4 — No CI  ·  P2
**Evidence:** no `.github/` (no workflows). Regression lock currently depends on a human
running the suite locally.
**Fix:** GitHub Actions: `backend` job (`uv sync` → `ruff check` → `pytest tests`) and
`frontend` job (`npm ci` → `npm run build`), plus a scheduled full-run. This is the
single highest-leverage reliability add (see I1).

### F5 — Ruff baseline debt  ·  P2 (style/debt, not correctness)
**Evidence:** `ruff check app tests` reports ~500 pre-existing findings. Correctness work
has stayed green, but the signal-to-noise blocks adopting lint as a gate.
**Fix:** one cleanup PR per area (unused imports/vars first — those can hide bugs), then
enable `ruff` in CI with `--output-format concise` on new code only.

### F6 — Unstructured log accumulation at repo root  ·  P3
**Evidence:** `backend-dev.log`, `backend-server.log`, `frontend.log`, `frontend-dev.log`,
`mpt.log`, `server.log`, `server.err.log` accumulate with no rotation.
**Fix:** loguru rotation (size/time) in `config.py`, single log location under
`backend/data/logs/` (already gitignored), keep root clean.

### F7 — Clip repurposing is dead capability  ·  P3 → fixed
**Evidence:** `app/providers/clips.py` (`ClipRepurposer`, yt-dlp + ffmpeg long-form→clips)
was referenced nowhere — no API route, no agent, no UI.
**Fix (applied):** `POST /workspaces/{id}/content/repurpose` route now acquires + cuts
via `ClipRepurposer`, creates `ContentItem` drafts (status IDEA) with clip metadata
in `strategy_json` and tags. Studio page gains a "Repurpose URL" button + modal
(URL, clip length, max clips, vertical toggle). Content items appear in the library
immediately after cutting.

### F8 — `run_at` timezone handling is implicit  ·  P3 → mostly fixed
**Evidence:** `ScheduleEntry.run_at` was stored as a naive datetime and serialized as
`isoformat() + "Z"` (forced UTC label) in `content.py` (calendar routes).
**Fix (applied):** the API now rejects naive and past `run_at` datetimes (422);
`ScheduleBody.content_item_id` is required (no content-less entries); and
`add_schedule` validates the content reference exists in the workspace.
Frontend updated to require content selection before submit.
Remaining: the DB column is still naive (SQLite doesn't store tzinfo); the forced
`"Z"` serialization on read remains, which is fine for a UTC-only backend. A future
Postgres migration could store `timestamptz` explicitly.

### F9 — Test-artifact cruft  ·  P4
**Evidence:** `backend/tests/_debug_out.txt`, `backend/tests/test_pipeline.py.tmp_note`,
root `data/` (389 K stray render output outside the ignored dir? — `data/` IS ignored,
but nothing should write there; point writers at `backend/data/`).
**Fix:** delete artifacts; audit which process wrote root `data/`.

---

## 3. Improvement backlog — "if we add this, it improves"

### I1 Reliability / engineering (highest leverage)
1. **CI pipeline** (test + lint + build) — regression lock becomes automatic. *P1.*
2. **`pytest-timeout` + unit/integration/live split** — eliminates F1 class hangs.
3. **Postgres-ready production path** — `DATABASE_URL` is already configurable and the
   migrations runner claims SQLite/Postgres parity; add a documented
   `docker-compose` Postgres profile + backup story for SQLite in dev.
4. **Correlation IDs + structured logging** — `X-Request-ID` header → agent-run →
   job logs; makes the "why did cycle 42 fail" question answerable in seconds.
5. **DB orphan sweep** — scheduled job flagging `PublishedPost`/`Video`/`PublishingJob`
   rows with dangling parent references (schema has no FK cascade audit yet).

### I2 Product surface — what makes it a *world-class automation platform*
1. **Real session-based publishing (TikTok/IG/YT)** — browser-session publisher
   adapter (pattern proven by AutoSocial Studio) to publish without platform API
   approval, alongside the existing API/mock publishers, with honest per-account
   labeling. *Biggest capability unlock.*
2. **Clip repurposing workflow** (fix F7) — operator-supplied long-form URL →
   even segments → drafts in Ideas queue → normal QC/publish path.
3. **Partial-publish visibility** — when A published and B retries, show that split in
   the Publishing/Calendar UI instead of only in job rows.
4. **Campaign-level steering** — Campaigns page exists; deepen to auto-scheduling slots,
   per-campaign budget caps and progress-to-goal (folds into decision engine input).
5. **Per-platform post templates** (AutoSocial sidecar idea) — reusable caption/SEO
   shells so repeat formats don't get rewritten from scratch.
6. **Cost ledger UI** — `CostEntry` rows exist and are enforced; a per-cycle cost
   breakdown page (est. vs actual, per stage) would make the budget story visible.
7. **Notifications** — Telegram bot exists; add opt-in alerts for `QC_FAILED`,
   publish `FAILED`, budget-pause and autopilot stop events.
8. **"Doctor" parity check** — run all readiness probes from one command/UI with
   actionable output (AutoSocial's `npm run doctor` equivalent), incl. yt-dlp,
   ffmpeg, engine reachability, token expiry.

### I3 Security & data
1. Re-verify the **silent-refresh path in a browser** with a genuinely expired access
   token (implemented, not yet live-tested).
2. Red-team the **session/role model** (owner/admin/viewer per workspace) — confirm
   every mutating route checks workspace role, not just authentication.
3. Secret-rotation UX — encrypted `ApiCredential` rows exist; add an "expired/rotated"
   health surface in Settings (currently corrupt keys silently fall back to env).

### I4 Docs sync
- README product-surface table should mention the repurposing feature only once it is
  surfaced (F7) — never describe nonexistent functionality.
- `API.md` is close to current; re-generate the auth/calendar sections to include the
  `QUEUED/FAILED` schedule lifecycle and `/calendar` retry semantics added in batch 2.

---

## 4. Priority queue (next actions, in order)

| # | Action | Fixes | Effort |
|---|---|---|---|
| # | Action | Fixes | Effort | Status |
|---|---|---|---|---|
| 1 | Add GitHub Actions CI (backend tests+lint, frontend build) | F4, guards F1 | S | ✅ done |
| 2 | `pytest-timeout` + test split; document run order | F1 | S | ✅ done (timeout; split pending) |
| 3 | Fix `.env.example` (duplicate + untyped keys) | F3 | S | ✅ duplicate fixed; untyped-keys centralization pending |
| 4 | Renumber/annotate duplicate `0003` migrations + runner guard | F2 | S | ✅ annotated + guard; renumber deferred (needs schema_migrations rewrite) |
| 5 | Enforce tz-aware `run_at` + 4xx validation on calendar create | F8 | M | ✅ done |
| 6 | Surface clip repurposing as a workflow (or retire it) | F7, I2-2 | M | ✅ done |
| 7 | Ruff cleanup PRs (unused imports first) → enable lint gate | F5 | M | ⬜ |
| 8 | Partial-publish status in Publishing/Calendar UI | I2-3 | M | ⬜ |
| 9 | Correlation IDs + structured logging | I1-4 | M | ⬜ file sink added; request-ID pending |
| 10 | Log rotation + single log dir | F6 | S | ✅ done |

---

*This checkpoint is a living document — re-scan after each batch and move verified fixes
from §2 into §1.*
