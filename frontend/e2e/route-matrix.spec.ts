/* Work 16.5.7 §7 -- the MACHINE-GENERATED route release matrix.
 *
 * Twelve facts per registry route, recorded as data rather than as assertions
 * scattered across a dozen files. `scripts/gen_route_release_matrix.py` then
 * merges the static facts from `routes/registry.ts` and REJECTS the artefact if
 * the matrix and the registry have drifted apart.
 *
 * WHAT EACH DIMENSION ACTUALLY MEANS HERE
 * ---------------------------------------
 * render           nav[aria-label="Primary"] visible AND exactly one <main>
 *                  with a non-empty accessible name AND non-empty text AND no
 *                  "Page not found" in the tree.
 * loading          A `.ym-skeleton` was OBSERVED inside <main> between the
 *                  navigation committing and the data settling. A fast
 *                  in-memory query can finish before any poller samples it, so
 *                  the honest value when nothing was caught is the string
 *                  "unobserved" -- never `true`.
 * populated        The screen shows NO empty state once the seeds have put real
 *                  rows into at least one of the route's own GET endpoints --
 *                  both halves proved, the API half by reading the endpoint on
 *                  the seeded workspace AND on a virgin one, so an endpoint the
 *                  backend fills canonically does not count as seeding. If no
 *                  public create endpoint can put a row on that screen the
 *                  value is "not-seedable" (or "provider-gated" when a paid
 *                  provider is what stands in the way) with the reason in
 *                  `notes`. It is never `true` by default.
 * empty            On a VIRGIN workspace (a second, freshly registered account
 *                  with nothing seeded) at least one empty state renders AND no
 *                  error state is on screen -- a screen that paints "nothing
 *                  here" over a dead request is not an empty state.
 * error            ONE specific GET is failed with `page.route(...).abort()`
 *                  -- a deliberate fault injection of a transport failure --
 *                  and an error STATE renders inside <main>. An empty list is
 *                  not an error state, so it does not satisfy this. Up to three
 *                  of the route's endpoints are tried, workspace-scoped first,
 *                  because a route's first read is often a filter dropdown
 *                  whose failure is silent on purpose; the endpoint that
 *                  answered is named in `notes`.
 * 403              Two proofs, both required. (1) SERVER: a second account that
 *                  is not a member of this workspace really is refused 403 by
 *                  the API, asserted against the live backend. (2) UI: that
 *                  server's own refusal payload is injected on the same
 *                  endpoint and the screen renders a refusal message naming it.
 *                  NOTE, recorded per route in `notes`: the session provider
 *                  falls back to a workspace the caller CAN read, so a foreign
 *                  workspace is not reachable by navigating to it -- which is
 *                  why the refusal has to be injected at the transport.
 * contract         The GET endpoints this route's own component calls are
 *                  derived by reading its source, then resolved against the
 *                  published `src/api/openapi.json`; each must carry a 2xx
 *                  whose schema is a `$ref`. The count is recorded, not just
 *                  the verdict, so "zero endpoints" cannot pass as "clean".
 *                  This derivation also reaches call sites a call-site scanner
 *                  cannot extract -- a path built inside a ternary or a helper
 *                  function -- so it can legitimately report endpoints that
 *                  `docs/UI_CONTRACT_AUDIT.json` does not list at all.
 * responsive_*     At that width the document does not scroll horizontally and
 *                  the navigation is reachable (directly, or through the mobile
 *                  drawer). `/studio` and `/studio/:timelineId` are exempt from
 *                  the overflow assertion ONLY -- a timeline is deliberately
 *                  wider than its container -- and still must prove nav
 *                  reachability.
 * a11y             <main> exists, is unique and is named; exactly one <h1>;
 *                  nav[aria-label="Primary"] exists; every visible interactive
 *                  control resolves an accessible name.
 *
 * NOTHING HERE MOCKS APPLICATION STATE. Data is created through the public API,
 * failures are injected at the transport, and every assertion is about what the
 * app RENDERS. `page.route` is used only to fail or refuse one request, never to
 * hand the app a fabricated success.
 */

import { test, expect, type APIRequestContext, type Page } from "@playwright/test";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  API,
  authenticate,
  register,
  seedClip,
  seedTimeline,
  type Account,
} from "./fixtures";
import { ROUTES, type AppRoute } from "../src/routes/registry";

/* =========================================================================
 * Paths and artefacts
 * ====================================================================== */

/* Resolved from THIS file's location, the way `openapi-contract.test.ts` does it:
 * a relative `docs/` would depend on the process cwd, and the spec would write
 * its artefact somewhere the generator never looks. */
const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..", "src");
const REPO = resolve(HERE, "..", "..");
const OUT = join(REPO, "docs", "UI_ROUTE_RELEASE_MATRIX.json");

type Paths = Record<string, Record<string, unknown>>;
const SPEC = JSON.parse(readFileSync(join(SRC, "api", "openapi.json"), "utf-8")) as {
  paths: Paths;
};
const APP_TSX = readFileSync(join(SRC, "app", "App.tsx"), "utf-8");

const DIMENSIONS = [
  "render",
  "loading",
  "populated",
  "empty",
  "error",
  "403",
  "contract",
  "responsive_1440",
  "responsive_1024",
  "responsive_768",
  "responsive_360",
  "a11y",
] as const;

/** Studio keeps a deliberately wide timeline. Overflow is exempt ONLY there. */
const HSCROLL_EXEMPT = new Set(["/studio", "/studio/:timelineId"]);

/** `true` / `false`, or an honest string naming why it could not be proved. */
type Dim = boolean | "unobserved" | "not-seedable" | "provider-gated";

type ContractFact = {
  endpoints: number;
  declared: number;
  ok: boolean;
  undeclared: string[];
};

type RouteFacts = {
  path: string;
  label: string;
  permission: string | null;
  hidden: boolean;
  component: string | null;
  render: boolean;
  loading: Dim;
  populated: Dim;
  empty: Dim;
  error: Dim;
  "403": Dim;
  contract: ContractFact;
  responsive_1440: boolean;
  responsive_1024: boolean;
  responsive_768: boolean;
  responsive_360: boolean;
  a11y: boolean;
  notes: string[];
};

/* =========================================================================
 * Static derivation: route -> component source -> GET endpoints
 * ====================================================================== */

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function skipBalanced(src: string, i: number, open: string, close: string): number {
  let depth = 0;
  for (let j = i; j < src.length; j += 1) {
    const c = src[j];
    if (c === open) depth += 1;
    else if (c === close) {
      depth -= 1;
      if (depth === 0) return j + 1;
    } else if (c === '"' || c === "'" || c === "`") {
      const q = c;
      j += 1;
      while (j < src.length && src[j] !== q) {
        if (src[j] === "\\") j += 1;
        j += 1;
      }
    }
  }
  return src.length;
}

/**
 * First argument of every call to `callee`, as a literal or as an identifier.
 *
 * Handles the generic form (`useWsQuery<T>("...")`) and skips the callee's own
 * declaration and its import specifier, neither of which is a call site.
 */
function callSites(src: string, callee: string): { arg: string; literal: boolean }[] {
  const out: { arg: string; literal: boolean }[] = [];
  const re = new RegExp(`\\b${callee}\\b`, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    if (/\bfunction\s+$/.test(src.slice(Math.max(0, m.index - 12), m.index))) continue;
    let i = m.index + m[0].length;
    while (i < src.length && /\s/.test(src[i])) i += 1;
    if (src[i] === "<") i = skipBalanced(src, i, "<", ">");
    while (i < src.length && /\s/.test(src[i])) i += 1;
    if (src[i] !== "(") continue;
    i += 1;
    while (i < src.length && /\s/.test(src[i])) i += 1;
    const c = src[i];
    if (c === '"' || c === "'" || c === "`") {
      const end = src.indexOf(c, i + 1);
      if (end < 0) continue;
      out.push({ arg: src.slice(i + 1, end), literal: true });
      re.lastIndex = end;
    } else {
      const idm = /^[A-Za-z_$][\w$]*/.exec(src.slice(i));
      if (idm) {
        out.push({ arg: idm[0], literal: false });
        re.lastIndex = i + idm[0].length;
      }
    }
  }
  return out;
}

/**
 * A UI path -> an OpenAPI-shaped path.
 *
 * The query string is dropped (a `?` inside `${ ... }` is a ternary, not a
 * query) and every `${ ... }` becomes one path parameter, so an interpolated
 * segment can match a literal-looking one at resolution time.
 */
function normaliseUiPath(raw: string): string {
  let depth = 0;
  let cut = raw.length;
  for (let i = 0; i < raw.length; i += 1) {
    if (raw.startsWith("${", i)) {
      depth += 1;
      i += 1;
      continue;
    }
    if (raw[i] === "}" && depth > 0) {
      depth -= 1;
      continue;
    }
    if (raw[i] === "?" && depth === 0) {
      cut = i;
      break;
    }
  }
  const head = raw.slice(0, cut).replace(/\$\{[^}]*\}/g, "{param}");
  const trimmed = head.replace(/\/+$/, "");
  return trimmed.length ? trimmed : "/";
}

function resolveSpecPath(candidate: string): string | null {
  const paths = SPEC.paths;
  if (paths[candidate]?.get) return candidate;
  const segs = candidate
    .split("/")
    .map((s) => (s.startsWith("{") ? "[^/]+" : escapeRe(s)))
    .join("/");
  const hits = Object.keys(paths).filter((p) => new RegExp(`^${segs}$`).test(p));
  return hits.find((p) => paths[p].get) ?? hits[0] ?? null;
}

/** The registry path -> the feature component that renders it. */
function componentFor(routePath: string): { name: string; file: string } | null {
  const imports = new Map<string, string>();
  for (const m of APP_TSX.matchAll(/import\s+([A-Za-z0-9_]+)\s+from\s+"([^"]+)"/g)) {
    imports.set(m[1]!, m[2]!);
  }
  const entry = new RegExp(`"${escapeRe(routePath)}"\\s*:\\s*<([A-Za-z0-9_]+)`).exec(APP_TSX);
  if (!entry) return null;
  const name = entry[1]!;
  const rel = imports.get(name);
  if (!rel) return null;
  const base = join(SRC, rel.replace(/^\.\.\//, ""));
  // The import statement omits the extension; the file on disk has one.
  for (const candidate of [`${base}.tsx`, `${base}.ts`, base]) {
    if (existsSync(candidate)) return { name, file: candidate };
  }
  return { name, file: base };
}

const PATH_LITERAL = /[`"'](\/[^`"'\s]*)[`"']/g;

/**
 * A path literal passed to a call as an identifier (`useWsQuery(listPath)`).
 *
 * The value is computed by a `useMemo`, so the declaration of that identifier is
 * the only place the path can appear; a fixed window after it is searched.
 */
function pathFromIdentifier(src: string, ident: string): string | null {
  const decl = new RegExp(`\\b(?:const|let|var)\\s+${escapeRe(ident)}\\b`).exec(src);
  if (!decl) return null;
  for (const m of src.slice(decl.index, decl.index + 600).matchAll(PATH_LITERAL)) {
    const candidate = normaliseUiPath(m[1]!);
    if (candidate.length > 1) return candidate;
  }
  return null;
}

/**
 * A 2xx on this endpoint whose schema is a real `$ref`.
 *
 * The `$ref` sits under the media type's `schema`, not on the media type
 * itself; looking in the wrong place declares every endpoint undocumented, and
 * a confident wrong answer is worse than an honest gap.
 */
function declared2xx(specPath: string): boolean {
  const op = SPEC.paths[specPath]?.get as
    | { responses?: Record<string, { content?: Record<string, unknown> }> }
    | undefined;
  if (!op) return false;
  for (const [code, response] of Object.entries(op.responses ?? {})) {
    if (!code.startsWith("2")) continue;
    for (const media of Object.values(response.content ?? {})) {
      const ref = (media as { $ref?: unknown })?.$ref;
      const nested = (media as { schema?: { $ref?: unknown } })?.schema?.$ref;
      for (const candidate of [ref, nested]) {
        if (typeof candidate === "string" && candidate.startsWith("#/components/schemas/")) {
          return true;
        }
      }
    }
  }
  return false;
}

const derived = new Map<
  string,
  { paths: string[]; contract: ContractFact; notes: string[] }
>();

/** GET endpoints this route's own component calls, plus the contract verdict. */
function derive(routePath: string): { paths: string[]; contract: ContractFact; notes: string[] } {
  const hit = derived.get(routePath);
  if (hit) return hit;

  const notes: string[] = [];
  const target = componentFor(routePath);
  if (!target) {
    notes.push("no component entry in App.tsx COMPONENTS for this path");
    const out = { paths: [] as string[], contract: { endpoints: 0, declared: 0, ok: false, undeclared: [] as string[] }, notes };
    derived.set(routePath, out);
    return out;
  }

  const src = readFileSync(target.file, "utf-8");
  const candidates: string[] = [];
  for (const site of [...callSites(src, "useWsQuery"), ...callSites(src, "wsApi.get")]) {
    const raw = site.literal ? site.arg : pathFromIdentifier(src, site.arg);
    if (!raw) {
      if (!site.literal) notes.push(`could not resolve the argument "${site.arg}"`);
      continue;
    }
    candidates.push(`/api/v1/workspaces/{workspace_id}${normaliseUiPath(raw)}`);
  }
  for (const m of src.matchAll(/api(?:<[^(]*>)?\s*\(\s*"GET"\s*,\s*[`"']([^`"']+)[`"']/g)) {
    candidates.push(`/api/v1${normaliseUiPath(m[1]!)}`);
  }

  const paths = new Set<string>();
  for (const c of candidates) {
    const hitPath = resolveSpecPath(c);
    if (hitPath) paths.add(hitPath);
    else notes.push(`no spec path for ${c}`);
  }

  const list = [...paths].sort();
  const undeclared = list.filter((p) => !declared2xx(p));
  const out = {
    paths: list,
    contract: {
      endpoints: list.length,
      declared: list.length - undeclared.length,
      ok: list.length > 0 && undeclared.length === 0,
      undeclared,
    },
    notes,
  };
  derived.set(routePath, out);
  return out;
}

/* =========================================================================
 * Shared context: a seeded workspace, a virgin one, and an outsider
 * ====================================================================== */

type Seeded = { name: string; path: string; body: Record<string, unknown> };

/**
 * Real rows, created through the public API.
 *
 * Every seed is optional: one the backend rejects is recorded as not-applied
 * rather than fatal, so a single tightened validator cannot make the whole
 * matrix unreadable -- and the route it was meant for then reports
 * "not-seedable" with the reason, which is the honest outcome.
 */
const SEEDS: Seeded[] = [
  { name: "brand", path: "/brands", body: { name: "MX Brand", is_default: true, dna: {} } },
  {
    name: "campaign",
    path: "/campaigns",
    body: {
      name: "MX Campaign",
      goal: "route matrix seed",
      target_videos: 3,
      videos_per_day: 1,
      platforms: ["tiktok"],
    },
  },
  {
    /* The engine, not the schema, validates this one: every arm needs a
     * `variant_ref`, control and variant refs must differ, `primary_metric`
     * must name a real PostMetric column, and `minimum_sample` must be >= 2. */
    name: "experiment",
    path: "/experiments",
    body: {
      kind: "HOOK",
      hypothesis: "MX hypothesis for the route matrix",
      control: { variant_ref: "mx-control" },
      variants: [{ variant_ref: "mx-variant", descriptor: "MX variant arm" }],
      platform: "tiktok",
      primary_metric: "views",
      minimum_sample: 10,
    },
  },
  {
    /* `type` must be a member of `app.engine.knowledge.memory.TYPES`. */
    name: "knowledge-memory",
    path: "/knowledge/memories",
    body: {
      type: "RESEARCH_FACT",
      content: "MX memory written by the route release matrix",
      confidence: 0.8,
      scope: "global",
      topic: "route-matrix",
      origin: "e2e",
    },
  },
  {
    /* `kind` must be a key of the backend connector catalog; an unknown kind is
     * refused with 422 rather than registered as an unusable connector. */
    name: "knowledge-source",
    path: "/knowledge/sources",
    body: { kind: "local", name: "MX Source", config: {} },
  },
  {
    name: "webhook",
    path: "/webhooks",
    body: { url: "https://example.invalid/hook", events: ["campaign.created"] },
  },
  { name: "api-key", path: "/api-keys", body: { name: "MX Key", role: "member" } },
  {
    name: "glossary",
    path: "/localization/glossary",
    body: { term: "MX term", replacement: "MX replacement", target_languages: ["es"] },
  },
  {
    name: "media",
    path: "/assets/media",
    body: {
      type: "video",
      origin: "upload",
      provider: "e2e",
      storage_key: "e2e/mx-clip.mp4",
      mime_type: "video/mp4",
      duration_seconds: 4,
      width: 1080,
      height: 1920,
    },
  },
];

type Ctx = {
  seeded: Account;
  virgin: Account;
  outsider: Account;
  timelineId: string;
  campaignId: string | null;
  seedsApplied: string[];
  seedsFailed: { name: string; status: number; detail: string }[];
};

let ctx: Ctx | null = null;

/**
 * Substitute a spec path for a concrete request URL.
 *
 * Real ids are used where one exists, so a probe of
 * `/campaigns/{campaign_id}` on the SEEDED workspace asks about the campaign the
 * seed actually created. Substituting a placeholder everywhere would report a
 * 404 as "empty" and quietly understate what a route can show.
 */
function concreteUrl(specPath: string, account: Account): string {
  const seeded = ctx && account.workspaceId === ctx.seeded.workspaceId ? ctx : null;
  const path = specPath.replace(/\{([a-zA-Z_]+)\}/g, (_match, name: string) => {
    if (name === "workspace_id") return account.workspaceId;
    if (name === "campaign_id") return seeded?.campaignId ?? "missing-id";
    if (name === "timeline_id") return seeded?.timelineId ?? "missing-id";
    return "missing-id";
  });
  return `${API}${path}`;
}

/** The longest collection anywhere in a response, at any depth. */
function maxCollectionLength(value: unknown, depth = 0): number {
  if (depth > 6) return 0;
  if (Array.isArray(value)) {
    let best = value.length;
    for (const v of value.slice(0, 5)) best = Math.max(best, maxCollectionLength(v, depth + 1));
    return best;
  }
  if (value && typeof value === "object") {
    let best = 0;
    for (const v of Object.values(value as Record<string, unknown>)) {
      best = Math.max(best, maxCollectionLength(v, depth + 1));
    }
    return best;
  }
  return 0;
}

type EndpointState = "filled" | "empty" | "refused" | "unreachable";

/**
 * Read one endpoint and say what it holds.
 *
 * `unreachable` is deliberately distinct from `empty`. A dropped connection is
 * not evidence that a collection is empty, and folding the two together is how
 * a transport blip turns into a "no rows" claim that later reads as coverage.
 */
/**
 * Bounded on purpose. `/api/v1/system/readiness` runs eleven live probes --
 * providers, storage, ffmpeg -- so it can take seconds or drop the connection;
 * an unbounded wait here would hang the sweep on one screen.
 */
const PROBE_TIMEOUT_MS = 6000;

async function probeOne(
  request: APIRequestContext,
  account: Account,
  specPath: string,
): Promise<EndpointState> {
  const url = concreteUrl(specPath, account);
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const res = await request.get(url, {
        headers: { Authorization: `Bearer ${account.token}` },
        timeout: PROBE_TIMEOUT_MS,
      });
      if (!res.ok()) return "refused";
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      return maxCollectionLength(body) > 0 ? "filled" : "empty";
    } catch {
      // Retry: a serial suite against one uvicorn drops the occasional
      // keep-alive. Exhausted retries mean "unknown", never "empty".
      if (attempt === 2) return "unreachable";
    }
  }
  return "unreachable";
}

/**
 * Endpoints the collection probe deliberately does NOT read.
 *
 * `/api/v1/system/*` are root-internal liveness/readiness probes: they run live
 * provider and storage checks, hold the single uvicorn worker for seconds and
 * return a report rather than a collection. Probing them twice per route adds
 * tens of seconds and tells us nothing about populated/empty. They stay in
 * `contract`, where declaring them is exactly the point.
 */
function probeExcluded(specPath: string): boolean {
  return specPath.startsWith("/api/v1/system/");
}

/**
 * Endpoint groups whose rows can only exist because a provider, a paid plan or
 * real hardware did something first.
 *
 * A route whose data lives only behind these cannot be populated from a
 * throwaway account, and reporting that as "not-seedable" would read as "nobody
 * tried" rather than "the world has to move first". One list, one reason per
 * group, so a stale entry is visible as a stale entry.
 */
const PROVIDER_OWNED: { pattern: RegExp; why: string }[] = [
  { pattern: /\/publishing\/(accounts|posts|jobs)/, why: "a real social account must be connected and OAuth'd" },
  { pattern: /\/avatars/, why: "an avatar profile needs a provider render" },
  { pattern: /\/lipsync/, why: "lip-sync needs a lip-sync worker" },
  { pattern: /\/assets\/dub|\/dubbing\/plans/, why: "dubbing needs a paid TTS provider" },
  { pattern: /\/media-intel\//, why: "media-intel runs need real media through a worker" },
  { pattern: /\/intelligence\/(browser\/|decisions\/)/, why: "decision records and browser runs need real routed work" },
  { pattern: /\/timelines\/\{timeline_id\}\/render/, why: "a render needs a video engine" },
  { pattern: /\/assets\/(broll\/generate|images\/generate|motion)/, why: "generation needs a paid media provider" },
  { pattern: /\/inbox\/(actions|opportunities|insights|interactions|conversations)/, why: "inbox rows come from syncing a real social platform" },
  { pattern: /^\/api\/v1\/opportunities/, why: "opportunities need a live trend source" },
  { pattern: /\/planner\/plans/, why: "a plan needs a content item to schedule" },
];

function providerGate(paths: string[]): string | null {
  const gated = PROVIDER_OWNED.filter((g) => paths.some((p) => g.pattern.test(p)));
  if (gated.length === 0) return null;
  const hits = paths.filter((p) => gated.some((g) => g.pattern.test(p)));
  return `${hits.length} of this route's endpoints can only be filled by real work -- ${gated
    .map((g) => g.why)
    .join("; ")}: ${hits.join(", ")}`;
}

async function probeCollections(
  request: APIRequestContext,
  account: Account,
  specPaths: string[],
): Promise<{ filled: string[]; empty: string[]; unreachable: string[]; skipped: string[] }> {
  const filled: string[] = [];
  const empty: string[] = [];
  const unreachable: string[] = [];
  const skipped: string[] = [];
  for (const p of specPaths) {
    if (probeExcluded(p)) {
      skipped.push(p);
      continue;
    }
    const state = await probeOne(request, account, p);
    if (state === "filled") filled.push(p);
    else if (state === "empty") empty.push(p);
    else if (state === "unreachable") unreachable.push(p);
    else empty.push(`${p} (refused)`);
  }
  return { filled, empty, unreachable, skipped };
}

/* =========================================================================
 * Browser probes
 * ====================================================================== */

/**
 * Wait for the shell, reloading once if the session boot did not survive.
 *
 * The app lands on the login screen whenever `/auth/me` fails, and against a
 * single-worker uvicorn under a serial sweep that happens. One reload is the
 * honest retry for a transport blip; a second failure is a real failure and is
 * reported as one rather than swallowed.
 */
async function openShell(page: Page): Promise<void> {
  const nav = page.getByRole("navigation", { name: "Primary" });
  const attempts = [15_000, 25_000];
  for (const [i, timeout] of attempts.entries()) {
    if (i > 0) await page.reload({ waitUntil: "domcontentloaded" });
    const ok = await nav
      .waitFor({ state: "visible", timeout })
      .then(() => true)
      .catch(() => false);
    if (ok) return;
  }
  await expect(nav).toBeVisible();
}

async function open(page: Page, url: string): Promise<void> {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await openShell(page);
  /* Let the page's own reads finish before the next phase starts. Without this
   * the phases overlap: a screen's ten queries plus the next phase's API probes
   * are in flight together, and the backend's ten-connection pool starves --
   * which shows up as a login screen twenty routes later, not as a pool error. */
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => {});
  await page.waitForTimeout(200);
}

async function checkRender(page: Page): Promise<boolean> {
  if (!(await page.getByRole("navigation", { name: "Primary" }).isVisible().catch(() => false))) {
    return false;
  }
  const mains = page.locator("main");
  if ((await mains.count()) !== 1) return false;
  const name = (await mains.getAttribute("aria-label").catch(() => null)) ?? "";
  if (name.trim().length === 0) return false;
  if (((await mains.innerText().catch(() => "")).trim()).length === 0) return false;
  return (await page.getByText(/page not found/i).count()) === 0;
}

async function checkA11y(page: Page): Promise<{ ok: boolean; why: string[] }> {
  const why: string[] = [];
  const r = await page.evaluate(() => {
    const mains = Array.from(document.querySelectorAll("main"));
    const navs = document.querySelectorAll('nav[aria-label="Primary"]').length;
    const h1s = document.querySelectorAll("h1").length;
    const mainName = (mains[0]?.getAttribute("aria-label") ?? "").trim();

    const accName = (el: Element): string => {
      const aria = (el.getAttribute("aria-label") ?? "").trim();
      if (aria) return aria;
      const lb = el.getAttribute("aria-labelledby");
      if (lb) {
        const t = lb.split(/\s+/).map((id) => document.getElementById(id)?.textContent ?? "").join(" ").trim();
        if (t) return t;
      }
      if (
        el instanceof HTMLInputElement ||
        el instanceof HTMLSelectElement ||
        el instanceof HTMLTextAreaElement
      ) {
        if (el.id) {
          const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (l?.textContent?.trim()) return l.textContent.trim();
        }
        const wrap = el.closest("label");
        if (wrap?.textContent?.trim()) return wrap.textContent.trim();
        const ph = (el.getAttribute("placeholder") ?? "").trim();
        if (ph) return ph;
        const ti = (el.getAttribute("title") ?? "").trim();
        if (ti) return ti;
        const type = el.getAttribute("type");
        if (type === "submit") return "Submit";
        if (type === "button" || type === "reset") return "Button";
        return "";
      }
      const ti = (el.getAttribute("title") ?? "").trim();
      if (ti) return ti;
      const txt = (el.textContent ?? "").trim();
      if (txt) return txt;
      const img = el.querySelector("img[alt]") as HTMLImageElement | null;
      if (img?.alt?.trim()) return (img.getAttribute("alt") ?? "").trim();
      const svg = el.querySelector("svg title");
      if (svg?.textContent?.trim()) return svg.textContent!.trim();
      return "";
    };

    const controls = Array.from(
      document.querySelectorAll(
        'button, a[href], input, select, textarea, [role="button"], [role="link"]',
      ),
    ).filter((el) => {
      const box = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return box.width > 0 && box.height > 0 && st.visibility !== "hidden" && st.display !== "none";
    });
    return {
      mainCount: mains.length,
      mainName,
      navs,
      h1s,
      unnamed: controls.filter((el) => accName(el).length === 0).map((el) => el.outerHTML.slice(0, 100)),
    };
  });

  if (r.mainCount !== 1) why.push(`${r.mainCount} <main> elements`);
  if (r.mainName.length === 0) why.push("<main> has no accessible name");
  if (r.navs !== 1) why.push(`${r.navs} nav[aria-label=Primary]`);
  if (r.h1s !== 1) why.push(`${r.h1s} <h1>`);
  if (r.unnamed.length > 0) why.push(`${r.unnamed.length} unnamed controls, e.g. ${r.unnamed[0]}`);
  return { ok: why.length === 0, why };
}

/** `true` only when a skeleton was actually SEEN; otherwise the honest string. */
async function observeLoading(page: Page, url: string): Promise<Dim> {
  await page.goto(url, { waitUntil: "commit" });
  const seen = await page
    .waitForSelector("main .ym-skeleton", { state: "attached", timeout: 4000 })
    .then(() => true)
    .catch(() => false);
  return seen ? true : "unobserved";
}

async function navReachable(page: Page): Promise<boolean> {
  const nav = page.locator('nav[aria-label="Primary"]');
  if (await nav.isVisible().catch(() => false)) return true;
  const toggle = page.locator(".ym-nav-toggle");
  if (!(await toggle.isVisible().catch(() => false))) return false;
  await toggle.click({ timeout: 5000 }).catch(() => {});
  const opened = await nav.isVisible().catch(() => false);
  if (opened) await toggle.click({ timeout: 5000 }).catch(() => {});
  return opened;
}

async function responsive(page: Page, url: string, width: number, exempt: boolean): Promise<boolean> {
  await page.setViewportSize({ width, height: 900 });
  await open(page, url);
  const nav = await navReachable(page);
  const m = await page.evaluate(() => ({
    s: document.documentElement.scrollWidth,
    c: document.documentElement.clientWidth,
  }));
  return nav && (exempt || m.s - m.c <= 2);
}

/**
 * EXACTLY one endpoint, matched as a regular expression rather than a glob.
 *
 * A glob for `/workspaces/{workspace_id}/assets` would also swallow
 * `/assets/media`, `/assets/dub/status` and every other asset subtree, so the
 * "one failed request" this dimension is built on would quietly become a dozen.
 * The trailing `(?:\?|$)` is what keeps the match to one path.
 */
function specPathToMatcher(specPath: string): RegExp {
  const body = specPath
    .split("/")
    .map((s) => (s.startsWith("{") ? "[^/]+" : escapeRe(s)))
    .join("\\/");
  return new RegExp(`${body}(?:\\?|$)`);
}

/** Detail routes need a real id; where none can exist, a placeholder is used. */
function concreteRouteUrl(route: AppRoute, c: Ctx): string {
  if (route.path === "/studio/:timelineId") return `/studio/${c.timelineId}`;
  if (route.path === "/campaigns/:campaignId") {
    return c.campaignId ? `/campaigns/${c.campaignId}` : "/campaigns/missing-id";
  }
  return route.path.replace(/:[A-Za-z]+/g, "missing-id");
}

/* =========================================================================
 * The sweep
 * ====================================================================== */

const records: RouteFacts[] = [];

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  test.setTimeout(240_000);
  const seeded = await register(request);
  const virgin = await register(request);
  const outsider = await register(request);

  const timelineId = await seedTimeline(request, seeded);
  await seedClip(request, seeded, timelineId, "MX Clip");

  const seedsApplied: string[] = ["timeline", "clip"];
  const seedsFailed: { name: string; status: number; detail: string }[] = [];
  let campaignId: string | null = null;
  for (const seed of SEEDS) {
    const res = await request.post(`${API}/api/v1/workspaces/${seeded.workspaceId}${seed.path}`, {
      headers: { Authorization: `Bearer ${seeded.token}` },
      data: seed.body,
    });
    if (!res.ok()) {
      seedsFailed.push({ name: seed.name, status: res.status(), detail: (await res.text()).slice(0, 160) });
      continue;
    }
    seedsApplied.push(seed.name);
    if (seed.name === "campaign") {
      try {
        const body = (await res.json()) as { campaign?: { id?: string }; id?: string };
        campaignId = body.campaign?.id ?? body.id ?? null;
      } catch {
        campaignId = null;
      }
    }
  }

  ctx = { seeded, virgin, outsider, timelineId, campaignId, seedsApplied, seedsFailed };
});

test.afterAll(() => {
  writeFileSync(
    OUT,
    `${JSON.stringify(
      {
        schema: "ymoney.route-release-matrix/1",
        generatedBy: "frontend/e2e/route-matrix.spec.ts",
        sources: {
          registry: "frontend/src/routes/registry.ts",
          openapi: "frontend/src/api/openapi.json",
        },
        dimensions: [...DIMENSIONS],
        routeCount: records.length,
        seeds: { applied: ctx?.seedsApplied ?? [], failed: ctx?.seedsFailed ?? [] },
        routes: [...records].sort((a, b) => a.path.localeCompare(b.path)),
      },
      null,
      2,
    )}\n`,
    "utf-8",
  );
});

for (const route of ROUTES) {
  test(`${route.path} -- twelve release facts`, async ({ page, request }) => {
    test.setTimeout(300_000);
    if (!ctx) throw new Error("shared context was not built");
    const c = ctx;
    const notes: string[] = [];
    const url = concreteRouteUrl(route, c);
    if (url !== route.path) notes.push(`exercised at ${url} (detail route)`);

    const { paths, contract, notes: derivedNotes } = derive(route.path);
    notes.push(...derivedNotes);

    /* ---- loading ---------------------------------------------------- */
    await authenticate(page, c.seeded);
    await page.setViewportSize({ width: 1440, height: 900 });
    const loading = await observeLoading(page, url);
    await openShell(page);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => {});
    await page.waitForTimeout(400);

    /* ---- render + a11y + populated, on the seeded workspace -------- */
    const render = await checkRender(page);
    const a11y = await checkA11y(page);
    for (const why of a11y.why) notes.push(`a11y: ${why}`);

    const emptyAfter = await page.locator(".ym-empty").count();

    /* Both API probes first, so "the seeds reached this route" can be separated
     * from "the backend ships canonical rows anyway". */
    const seededProbe = await probeCollections(request, c.seeded, paths);
    const virginProbe = await probeCollections(request, c.virgin, paths);
    const seededOnly = seededProbe.filled.filter((p) => !virginProbe.filled.includes(p));

    let populated: Dim;
    const gate = providerGate(paths);
    if (paths.length === 0) {
      populated = "not-seedable";
      notes.push("no resolvable GET endpoint: populated cannot be established");
    } else if (seededOnly.length === 0) {
      populated = gate ? "provider-gated" : "not-seedable";
      notes.push(
        gate
          ? `no seed reached this route -- ${gate}`
          : `no seed reached this route (nothing gained rows: ${seededProbe.empty.join(", ")})`,
      );
    } else if (emptyAfter > 0) {
      populated = gate ? "provider-gated" : "not-seedable";
      notes.push(
        `${seededOnly.join(", ")} gained rows from the seeds but the screen still shows ${emptyAfter} empty state(s)`,
      );
      if (gate) notes.push(`the remaining panels are behind: ${gate}`);
    } else {
      populated = true;
    }

    /* ---- empty, on a virgin workspace ------------------------------ */
    await authenticate(page, c.virgin);
    await open(page, url);
    await page.waitForTimeout(400);
    const emptyBefore = await page.locator(".ym-empty").count();
    const virginErrors = await page.locator("main .ym-error-state").count();
    /* An empty state only counts if nothing is failing: a screen that paints
     * "nothing here" over a dead request is not an empty state. */
    const empty: boolean = emptyBefore > 0 && virginErrors === 0;
    if (emptyBefore === 0) {
      notes.push("no empty state rendered on a virgin workspace (the screen shows figures, not an empty state)");
    }
    if (virginErrors > 0) {
      notes.push(`${virginErrors} error state(s) on a virgin workspace: not an empty state`);
    }
    if (virginProbe.filled.length > 0) {
      notes.push(`a virgin workspace already holds rows in ${virginProbe.filled.join(", ")} (canonical data)`);
    }
    for (const [who, probe] of [
      ["seeded", seededProbe],
      ["virgin", virginProbe],
    ] as const) {
      if (probe.unreachable.length > 0) {
        notes.push(`${probe.unreachable.length} endpoint(s) unreachable on the ${who} workspace: ${probe.unreachable.join(", ")}`);
      }
    }
    if (seededProbe.skipped.length > 0) {
      notes.push(`collection probe skipped the root-internal probe(s) ${seededProbe.skipped.join(", ")}`);
    }

    /* ---- error: fail ONE endpoint at the transport ------------------ */
    /* Workspace-scoped endpoints first: a global probe such as
     * `/system/readiness` is not membership-scoped, so it could not demonstrate
     * anything about a 403. Up to three candidates are tried because a route's
     * FIRST read is often a filter dropdown, whose failure is deliberately
     * silent -- which is a true statement about that panel and no statement at
     * all about the screen. The endpoint that answered is named in `notes`. */
    const scoped = paths.filter((p) => p.includes("/workspaces/{workspace_id}"));
    const candidates = [...scoped, ...paths.filter((p) => !scoped.includes(p))].slice(0, 3);
    const alertSel = "main .ym-error-state, main .ym-error, main [role='alert']";

    let error: Dim = false;
    if (candidates.length === 0) {
      error = "not-seedable";
      notes.push("no endpoint to fail: an error state cannot be provoked");
    } else {
      const silent: string[] = [];
      for (const candidate of candidates) {
        await authenticate(page, c.seeded);
        const matcher = specPathToMatcher(candidate);
        await page.route(matcher, (r) => r.abort("failed"));
        await open(page, url);
        const visible = await page
          .locator(alertSel)
          .first()
          .waitFor({ state: "visible", timeout: 8000 })
          .then(() => true)
          .catch(() => false);
        await page.unroute(matcher);
        if (visible) {
          error = true;
          notes.push(`an error state was rendered by failing ${candidate}`);
          break;
        }
        silent.push(candidate);
      }
      if (error !== true) {
        notes.push(`failing ${silent.join(", ")} rendered no error state inside <main>`);
      }
    }

    /* ---- 403: the server refuses, and the screen says so ----------- */
    let refused403: Dim = false;
    if (candidates.length === 0) {
      refused403 = "not-seedable";
      notes.push("no endpoint to refuse: a 403 cannot be provoked");
    } else {
      const statuses: string[] = [];
      for (const candidate of candidates) {
        /* The SEEDED workspace's id with the OUTSIDER's token: that is the
         * request a non-member actually makes. Probing the outsider's own
         * workspace would answer 200 and quietly "prove" a refusal that never
         * happened. */
        let status = 0;
        let detail = "";
        try {
          const probe = await request.get(concreteUrl(candidate, { ...c.seeded, token: c.outsider.token }), {
            headers: { Authorization: `Bearer ${c.outsider.token}` },
            timeout: PROBE_TIMEOUT_MS,
          });
          status = probe.status();
          if (status === 403) detail = (await probe.text()).slice(0, 200);
        } catch {
          status = 0;
        }
        statuses.push(`${candidate} -> HTTP ${status || "no response"}`);
        if (status !== 403) continue;

        await authenticate(page, c.seeded);
        const matcher = specPathToMatcher(candidate);
        await page.route(matcher, (r) =>
          r.fulfill({
            status: 403,
            contentType: "application/json",
            body: JSON.stringify({ detail: detail || "not a workspace member" }),
          }),
        );
        await open(page, url);
        const alert = page.locator(alertSel).first();
        const uiVisible = await alert
          .waitFor({ state: "visible", timeout: 8000 })
          .then(() => true)
          .catch(() => false);
        const uiText = uiVisible ? await alert.innerText() : "";
        const denialStyled = await page.locator("main .ym-error-state--denied").count();
        await page.unroute(matcher);

        if (uiVisible && /not a workspace member|forbidden|insufficient role|403/i.test(uiText)) {
          refused403 = true;
          notes.push(
            denialStyled > 0
              ? `refusal rendered through the styled denial state (${candidate})`
              : `refusal rendered for ${candidate}, but NOT through PermissionAwareError's denial state: isPermissionDenial() does not match the backend's own detail, so a 403 offers a Retry button that can never succeed`,
          );
          break;
        }
        notes.push(`a real 403 on ${candidate} rendered no refusal message in <main>`);
      }
      if (refused403 !== true) {
        notes.push(`non-member request status: ${statuses.join("; ")}`);
      }
    }

    /* ---- responsive -------------------------------------------------- */
    await authenticate(page, c.seeded);
    const exempt = HSCROLL_EXEMPT.has(route.path);
    const responsive_1440 = await responsive(page, url, 1440, exempt);
    const responsive_1024 = await responsive(page, url, 1024, exempt);
    const responsive_768 = await responsive(page, url, 768, exempt);
    const responsive_360 = await responsive(page, url, 360, exempt);
    if (exempt) {
      notes.push("horizontal-scroll assertion exempt: a timeline is deliberately wider than its container");
    }

    records.push({
      path: route.path,
      label: route.label,
      permission: route.permission,
      hidden: route.hidden === true,
      component: componentFor(route.path)?.name ?? null,
      render,
      loading,
      populated,
      empty,
      error,
      "403": refused403,
      contract,
      responsive_1440,
      responsive_1024,
      responsive_768,
      responsive_360,
      a11y: a11y.ok,
      notes,
    });

    /* Only the invariants the work order states outright are asserted here.
     * Everything else is RECORDED: a dimension that cannot be established
     * honestly is written as an honest string, not failed. */
    expect(render, `${route.path} did not render the shell`).toBe(true);
    expect(a11y.ok, `${route.path} accessibility: ${a11y.why.join("; ")}`).toBe(true);
  });
}
