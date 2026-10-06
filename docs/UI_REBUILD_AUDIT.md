# YMONEY UI Rebuild Audit

**Work 16.5 — before any edit.** This is the inventory the rebuild is justified
by. Every classification below was read from the source, not inferred from a
file name.

- Baseline at audit time: `frontend/src` = **66 files, 20,648 lines**
- Backend at audit time: 38 migrations, 406 API paths, 457 operations,
  139 schemas. PostgreSQL contracts verified stable (Work 16.1).
- Stack: React 19.2, TypeScript 5.9 (strict), Vite 7, Tailwind 4,
  react-router 7, wavesurfer 7. No test runner (added by this work).

---

## 1. Verdict in one line

The old frontend is **37 flat page components behind one `Layout`**, with no
design system, no feature boundaries, no shared data layer, no test runner and
no contract layer. Four of the thirty-seven pages do not do what their name
says. This is implementation debt, not a styling problem, so a reskin would
produce exactly the failure mode the work order forbids.

---

## 2. What the old frontend actually is

| Area | Files | Lines | Shape |
|---|---|---|---|
| `src/pages/` | 37 | 12,202 | One flat directory of route components |
| `src/components/` | 21 | 7,234 | `ui.tsx` god-file + feature folders |
| `src/editor/adapters/` | 1 | 366 | The canonical timeline ops engine |
| `src/hooks/` | 1 | 97 | `useFetch` + SSE subscription |
| `src/lib/` | 2 | 212 | API client + formatters |
| `src/App.tsx` / `main.tsx` | 2 | 127 | Router + bootstrap |
| `src/index.css` | 1 | 409 | Global CSS with inline `--var` tokens |

Largest files: `Inbox.tsx` 918 · `Brands.tsx` 894 · `Editor.tsx` 879 ·
`Ugc.tsx` 844 · `CaptionMotionPanel.tsx` 825 · `Knowledge.tsx` 756.

Every one of those is a single route component holding its own fetch calls,
its own loading state, its own table markup and its own styling. There is no
separation between "what the backend said" and "what the screen looks like",
which is why §18 of the work order is necessary: nothing checked a screen
against the API.

---

## 3. Concrete defects found (not opinions)

These are the reasons each item below is classified as it is.

### 3.1 Duplicate and misnamed pages

| Page | Actually serves | Evidence |
|---|---|---|
| `Brand.tsx` (112 L) | Legacy **chrome settings** — `GET /brand` + `PUT /settings` | Not BrandDNA. A second brand surface with a colliding name. |
| `Brands.tsx` (894 L) | Real **BrandDNA workspace** — `/brands/{id}`, `/brands/{id}/verify`, `/brands/{id}/assets` | The feature `Brand.tsx` was mistaken for. |
| `Performance.tsx` (661 L) | **Experiments + Memory lessons** — `/experiments/{id}/{action}`, `/lessons`, `/lessons/{id}/evidence` | Named "Performance", serves experiments. There is no performance page despite the name. |
| `Knowledge.tsx` (756 L) | Global memory / knowledge | Overlaps `Memory.tsx` (104 L). |

`Brand.tsx:9` reads `localStorage.getItem("ym_ws")` directly while
`lib/api.ts` already owns that lookup. The duplication is already leaking.

### 3.2 No contract layer

`rg` finds no generated types, no OpenAPI consumer, no response validation and
no test runner. `package.json` `scripts` contains only `dev`, `build`,
`preview` — **there is no `test` script at all**. Work 15.6 found UI/backend
mismatches TypeScript could not catch; nothing has changed that.

### 3.3 Business logic inside components

`intelApi.ts` (715 L), `motionApi.ts` (154 L) and `distributionApi.ts` (60 L)
are hand-written per-feature fetch wrappers that duplicate `lib/api.ts` and
each other. There is no shared query layer, so loading/error/retry handling is
re-implemented per panel.

### 3.4 Unreachable and duplicated panels

`components/ui.tsx` (366 L) exports a generic `Badge`/`Card`/`Field`/
`PageHeader` kit that every page also re-declares variants of. Styling is
scattered: `index.css` carries 409 lines of global CSS while components use
Tailwind classes and inline `style={{}}` in the same tree.

### 3.5 A token contract that is not a design system

`index.css` defines `--var` custom properties (text/brand/accent), and
`Brand.tsx` lets an operator set `accent` from that set at runtime. Any new
design system must keep runtime brand-accent overridable or it will silently
break a shipped capability.

---

## 4. Classification

### KEEP_INFRA — genuinely reusable, not visual (6 files, ~700 lines)

Preserved because they encode behaviour that is easy to get wrong and is not
about looks.

| File | Lines | Why it survives |
|---|---|---|
| `lib/api.ts` | 148 | Bearer auth + **single-use refresh rotation**, 401 retry-once, `NO_AUTO_REFRESH` guard, SSE `?token=` URLs (EventSource cannot send headers), media/file/thumbnail/cover URL builders, `downloadAudit`. |
| `lib/format.ts` | 64 | Pure formatters. No visual decision in them. |
| `editor/adapters/timelineAdapter.ts` | 366 | **Canonical Work 02/13.1 editor contract**: `TRACK_ORDER`, `TRACK_FAMILY`, `applyOpsLocal`, **`inverseOps` (undo/redo)**, `snapTime`, `findClip`, `clipEnd`. Business logic the backend also owns. |
| `hooks/hooks.ts` | 97 | `useFetch` (with alive-guard and reload) and the SSE subscription primitive. |
| `main.tsx` | 12 | Provider bootstrap. |
| `vite-env.d.ts` | 1 | Vite ambient types. |

**Justification, since §1 asks for it:** none of these are components. There
is nothing to reskin, and the refresh-rotation and inverse-ops logic is the
kind of thing that is *wrong* when retyped rather than merely ugly.

### REIMPLEMENT — everything user-facing (60 files, ~19,950 lines)

All 37 `pages/`, all 20 of the feature components under `components/`,
`App.tsx`, and `index.css`. Reasons are per-area below; the common reasons are:
no feature boundary, fetch logic in the component, no shared loading/error
state, no design system, and no test.

| Old | New home | Note |
|---|---|---|
| `pages/CommandCenter.tsx` | `features/command-center/` | Real Work 16 signals (exposure, queue, GPU) are absent today. |
| `pages/Planner.tsx`, `CalendarPage.tsx`, `Trends.tsx`, `Autopilot.tsx` | `features/planner/` | One planning surface, not four. |
| `pages/Campaigns.tsx`, `ContentDetail.tsx`, `Activity.tsx`, `Approvals.tsx`, `Reviews.tsx` | `features/projects/`, `features/campaigns/` | Project becomes one workspace with tabs. |
| `pages/Editor.tsx`, `Studio.tsx`, `components/TimelinesPanel.tsx`, `components/intel/*`, `components/motion/*`, `components/collab/*` | `features/studio/` | UI replaced; `timelineAdapter` kept. |
| `pages/Assets.tsx` | `features/assets/` | Needs provenance, lineage, usage locations. |
| `pages/Brand.tsx` + `pages/Brands.tsx` | `features/brand/` | Two surfaces collapse into one, legacy chrome-settings folded in. |
| `pages/Localization.tsx`, `pages/Ugc.tsx` | `features/localization/`, `features/ugc/` | |
| `pages/Publishing.tsx`, `pages/Integrations.tsx`, `pages/LiveMonitor.tsx`, `pages/Composer.tsx` | `features/distribution/` | LIVE/MOCK/HANDOFF must be unmistakable. |
| `pages/Inbox.tsx` | `features/community/` | |
| `pages/Analytics.tsx`, `pages/Performance.tsx`, `pages/Exports.tsx` | `features/analytics/`, `features/experiments/` | `Performance` is really experiments. |
| `pages/Knowledge.tsx`, `pages/Memory.tsx`, `pages/Intelligence.tsx` | `features/memory/`, `features/intelligence/` | |
| `pages/SystemHealth.tsx`, `components/ProviderStatus.tsx` | `features/operations/`, `features/providers/` | Must cover the Work 16 operator surface. |
| `pages/Settings.tsx`, `ApiKeys.tsx`, `Webhooks.tsx`, `Templates.tsx`, `Setup.tsx`, `Login.tsx` | `features/settings/` | |
| `components/ui.tsx`, `components/charts.tsx`, `components/Layout.tsx`, `components/CreativeDirector.tsx`, `components/CommandRenderer.tsx` | `design-system/`, `app/` | God-files split by concern. |
| `App.tsx`, `index.css` | `app/`, `routes/` | New router over the new IA. |

### DELETE — removed outright

| Item | Reason |
|---|---|
| `pages/Brand.tsx` | Duplicate of `Brands.tsx` under a colliding name; its chrome-settings job folds into settings. |
| `components/intel/intelApi.ts`, `motion/motionApi.ts`, `distribution/distributionApi.ts` | Duplicated fetch wrappers. Replaced by one query layer. |
| `components/charts.tsx` | Replaced by design-system charts bound to real series. |
| `index.css` token block | Replaced by `design-system/tokens.css`; **runtime brand accent override preserved.** |

No file is preserved "because it already exists". The only visual files kept
are none; the only files kept are the six in KEEP_INFRA, none of which render
anything.

---

## 5. What the rebuild must not break

Backend contracts the old frontend depends on, which the new UI must keep:

1. `GET /auth/me`, `POST /auth/refresh` (single-use rotation), `localStorage`
   keys `ym_token` / `ym_rt` / `ym_ws`.
2. Workspace-scoped paths: `/api/v1/workspaces/{ws}/…`.
3. SSE `…/activity/stream?token=` (EventSource cannot set headers).
4. Media URLs: `videos/{id}/file`, `/thumbnail`, `/covers/{i}/file`,
   `/ai-covers/{i}/file`, `assets/media/{id}/file`, `brand/logo/file`.
5. `ContentTimeline` ops semantics in `timelineAdapter` — the UI must not
   diverge from canonical backend state (§7).
6. Runtime brand accent (`PUT /settings` → `settings.brand.accent`).
7. Server-enforced RBAC. The UI may hide a control; it may never be the only
   thing enforcing one.

---

## 6. Contract baseline for §18

`frontend/src/api/openapi.json` is generated from the running application by
`scripts/gen_openapi.py` (406 paths / 457 operations / 139 schemas, sorted so
regenerating an unchanged app is byte-identical). Every rebuilt screen is
checked against this file, so a wrong route, method, field or enum fails the
build instead of shipping.

---

## 7. Deleted-count summary

| | Before | After |
|---|---|---|
| Route components | 37 | 0 (replaced by feature screens) |
| Feature components | 21 | 0 (replaced) |
| Files | 66 | 6 KEEP_INFRA + new tree |
| Design tokens | ad-hoc `--var` in one CSS file | token module |
| Test scripts | **none** | `test`, `test:contract`, `typecheck`, `build` |
| Generated contract | none | `openapi.json` (860 KB) |

The final tree must not be "new UI with the old UI hidden underneath": every
`REIMPLEMENT` and `DELETE` item above is removed, and a dead-code scan is part
of §20.