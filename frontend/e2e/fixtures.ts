import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type APIRequestContext, type Page } from "@playwright/test";

/* Shared browser-suite fixtures: a real account, a real workspace, real data.
 *
 * NOTHING here is a route mock. The work order is explicit that Studio must be
 * verified against the canonical engine and that backend state must be asserted,
 * which is impossible if the network is stubbed -- a stubbed API proves only that
 * a component draws what it was handed.
 *
 * The account is created through the SAME public registration endpoint a user
 * hits, so the suite exercises the real auth path rather than a test-only
 * shortcut that could diverge from it.
 *
 * The password is GENERATED per account rather than hardcoded. A checked-in
 * credential is a liability regardless of whether it only ever reaches a
 * throwaway local database, and a unique one per run means one leaked run log
 * cannot be replayed against another.
 */

export const API = process.env.YMONEY_API ?? "http://127.0.0.1:8099";

export type Account = {
  token: string;
  workspaceId: string;
  email: string;
};

export function strongPassword(): string {
  const alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let out = "";
  for (let i = 0; i < 28; i += 1) {
    out += alphabet[Math.floor(Math.random() * alphabet.length)];
  }
  return `E2e!${out}`;
}

export async function register(request: APIRequestContext): Promise<Account> {
  const email = `e2e-${Date.now().toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 8)}@test.local`;

  const res = await request.post(`${API}/api/v1/auth/register`, {
    data: { email, password: strongPassword(), display_name: "E2E Operator" },
  });
  expect(res.status(), `registration failed: ${await res.text()}`).toBe(200);

  const body = await res.json();
  return {
    token: body.access_token,
    workspaceId: body.workspace.id,
    email,
  };
}

/** Put a real session in place before the app boots, so no screen sees "logged out". */
export async function authenticate(page: Page, account: Account): Promise<void> {
  await page.addInitScript(
    ([token, workspaceId]) => {
      window.localStorage.setItem("ym_token", token as string);
      window.localStorage.setItem("ym_workspace", workspaceId as string);
    },
    [account.token, account.workspaceId] as const,
  );
}

/** Wait for the app shell, which every route renders inside. */
export async function gotoRoute(page: Page, path: string): Promise<void> {
  await page.goto(path, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible({
    timeout: 20_000,
  });
}

/** Create a timeline through the real API so Studio has something canonical to load. */
export async function seedTimeline(
  request: APIRequestContext,
  account: Account,
): Promise<string> {
  const res = await request.post(
    `${API}/api/v1/workspaces/${account.workspaceId}/timelines`,
    {
      headers: { Authorization: `Bearer ${account.token}` },
      data: { name: "E2E Timeline", fps: 30, duration_seconds: 30, aspect_ratio: "9:16" },
    },
  );
  expect(res.ok(), `timeline create failed: ${await res.text()}`).toBe(true);
  const body = await res.json();
  return body.timeline?.id ?? body.id;
}

/** Read the canonical document straight from the API. */
export async function readTimeline(
  request: APIRequestContext,
  account: Account,
  timelineId: string,
): Promise<Record<string, unknown>> {
  const res = await request.get(
    `${API}/api/v1/workspaces/${account.workspaceId}/timelines/${timelineId}`,
    { headers: { Authorization: `Bearer ${account.token}` } },
  );
  const text = await res.text();
  expect(
    res.ok(),
    `timeline read failed: ${res.status()} for ${timelineId}: ${text.slice(0, 300)}`,
  ).toBe(true);
  return JSON.parse(text) as Record<string, unknown>;
}

/** Add a REAL clip, so the editor has something to select, move, trim and split.
 *
 * Written through `POST /timelines/{id}/operations` -- the same canonical op
 * channel the editor itself uses -- NOT by patching the database. A clip injected
 * at the ORM layer could carry a shape the engine would never produce, and every
 * interaction test would then be asserting against a fiction.
 *
 * `add_item` is the op that creates a clip on a track; the vocabulary is
 * `app.engine.timeline_ops.OP_TYPES`.
 */
export async function seedClip(
  request: APIRequestContext,
  account: Account,
  timelineId: string,
  label = "E2E Clip",
): Promise<{ clipId: string; track: string; start: number; label: string }> {
  const current = await readTimeline(request, account, timelineId);
  const version = Number(current.version ?? 1);
  const clipId = `e2e-clip-${Date.now().toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 6)}`;
  const track = "video";
  // A UNIQUE name per clip. Every clip on a shared timeline was called
  // "E2E Clip", so `getByTitle(/^E2E Clip/)` matched the first one on the track --
  // an earlier test's clip -- and each interaction then asserted against the
  // wrong object. Returning the label lets a test address exactly its own clip.
  const name = `${label} ${clipId.slice(-4)}`;

  // Place the clip in FREE space, not at a hardcoded 1.0s.
  //
  // The engine refuses an overlapping clip ("overlaps previous clip", 422), so a
  // fixed start means only the FIRST seed on a shared timeline succeeds and every
  // later one fails for a reason that has nothing to do with the interaction
  // under test. Starting after the last clip's end keeps each seed independent.
  const existing = clipsOn(current, track);
  const end = existing.reduce(
    (max, c) => Math.max(max, Number(c.start ?? 0) + Number(c.duration ?? 0)),
    0,
  );
  const start = end + 1;

  const res = await request.post(
    `${API}/api/v1/workspaces/${account.workspaceId}/timelines/${timelineId}/operations`,
    {
      headers: { Authorization: `Bearer ${account.token}` },
      data: {
        base_version: version,
        operations: [
          {
            type: "add_item",
            track,
            clip: {
              id: clipId,
              name,
              start,
              duration: 4.0,
              source: { asset_ref: "e2e://clip" },
            },
          },
        ],
      },
    },
  );
  expect(res.ok(), `clip seed failed: ${await res.text()}`).toBe(true);
  return { clipId, track, start, label: name };
}

/** The clips the canonical document holds on a track. */
export function clipsOn(
  doc: Record<string, unknown>,
  trackKind: string,
): Record<string, unknown>[] {
  const tracks = Array.isArray(doc.tracks) ? (doc.tracks as Record<string, unknown>[]) : [];
  const track = tracks.find((t) => t.kind === trackKind);
  const clips = track && Array.isArray(track.clips) ? track.clips : [];
  return clips as Record<string, unknown>;
}

/** The canonical document's version, which every op batch must quote. */
export function timelineVersion(doc: Record<string, unknown>): number {
  return Number(doc.version ?? 1);
}

/** Apply a batch of canonical timeline ops and return the server's reply.
 *
 * Used both to ARRANGE a fixture and to MUTATE a timeline behind the editor's
 * back. The second use is deliberate: forcing a version conflict means writing
 * an op out-of-band so the editor's in-memory version is genuinely stale, which
 * is the only honest way to exercise the 409 path. A test that manufactured the
 * conflict in the frontend would only prove the frontend can display a string it
 * was handed.
 */
export async function applyOps(
  request: APIRequestContext,
  account: Account,
  timelineId: string,
  operations: Record<string, unknown>[],
  baseVersion?: number,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const version =
    baseVersion ?? timelineVersion(await readTimeline(request, account, timelineId));
  const res = await request.post(
    `${API}/api/v1/workspaces/${account.workspaceId}/timelines/${timelineId}/operations`,
    {
      headers: { Authorization: `Bearer ${account.token}` },
      data: { base_version: version, operations },
    },
  );
  const text = await res.text();
  let body: Record<string, unknown> = {};
  try {
    body = JSON.parse(text) as Record<string, unknown>;
  } catch {
    body = { raw: text };
  }
  return { status: res.status(), body };
}

/** Add a clip on an ARBITRARY track through the canonical op channel.
 *
 * `seedClip` pins `track: "video"`, and the caption/motion panel only mounts for
 * a selection on the `caption` track. Testing keyframes, effects and captions
 * through the real UI therefore requires a caption-track clip, which is what this
 * exists for -- still created by the engine, never by patching the database.
 */
export async function seedClipOnTrack(
  request: APIRequestContext,
  account: Account,
  timelineId: string,
  track: string,
  label: string,
  duration = 4.0,
  atStart?: number,
): Promise<{ clipId: string; track: string; start: number; label: string }> {
  const clipId = `e2e-${track}-${Date.now().toString(36)}-${Math.random()
    .toString(36)
    .slice(2, 6)}`;
  const name = `${label} ${clipId.slice(-4)}`;
  // Free space again -- see `seedClip`. The engine refuses overlaps.
  const existing = clipsOn(await readTimeline(request, account, timelineId), track);
  const end = existing.reduce(
    (max, c) => Math.max(max, Number(c.start ?? 0) + Number(c.duration ?? 0)),
    0,
  );
  // `atStart` exists for ADJACENCY: `find_adjacent_pair` refuses any pair with a gap
  // over 0.05s, so a transition test must place the second clip exactly where the
  // first one ends rather than in the free-space slot above.
  const start = atStart ?? end + 1;
  const { status, body } = await applyOps(request, account, timelineId, [
    {
      type: "add_item",
      track,
      clip: {
        id: clipId,
        name,
        start,
        duration,
        source: { asset_ref: `e2e://${track}` },
      },
    },
  ]);
  expect(status, `clip seed failed: ${JSON.stringify(body)}`).toBe(200);
  return { clipId, track, start, label: name };
}

/** One clip on a track, by id. Fails loudly rather than returning undefined. */
export function clipById(
  doc: Record<string, unknown>,
  trackKind: string,
  clipId: string,
): Record<string, unknown> {
  const found = clipsOn(doc, trackKind).find((c) => c.id === clipId);
  expect(found, `no clip ${clipId} on track ${trackKind}`).toBeTruthy();
  return found as Record<string, unknown>;
}

/** Repository root, resolved the same way the contract suite does.
 *
 * `__dirname` is unavailable in an ES module (this file is bundled as one), so
 * the root is derived from `import.meta.url`.
 *
 * The `../../` must be resolved BY THE URL PARSER, not by string concatenation.
 * `fileURLToPath` only percent-decodes -- it does NOT collapse `..`. So
 * `fileURLToPath(new URL(".", import.meta.url) + "/../..")` silently yields
 * `...\ymoney\frontend\`, not the repo root, and every caller then looks for
 * `backend\.venv\Scripts\python.exe` one directory too low. That bug surfaced as
 * a `spawnSync` with `status: null` and no stdout or stderr at all, because the
 * spawn never happened.
 */
export const REPO_ROOT = fileURLToPath(new URL("../../", import.meta.url));

/**
 * Put a second user into an EXISTING workspace at a role.
 *
 * There is no API for this: registration always creates a NEW workspace owned by
 * the new user, so a second registered user can only ever prove cross-workspace
 * isolation. The role is one column -- `workspace_members.role` -- and every
 * existing backend test writes it directly. This shells out to the same helper so
 * the browser suite can exercise the viewer/member/admin ladder inside ONE
 * workspace instead of pretending three separate workspaces prove it.
 */
export async function attachToWorkspace(
  workspaceId: string,
  email: string,
  role: "viewer" | "member" | "admin" | "owner",
): Promise<void> {
  const script = join(REPO_ROOT, "scripts", "attach_workspace_member.py");
  const python = join(REPO_ROOT, "backend", ".venv", "Scripts", "python.exe");
  const proc = spawnSync(python, [script, workspaceId, email, role], {
    encoding: "utf-8",
    // MUST match the API server's cwd, which `playwright.config.ts` pins to the
    // repo root (`cwd: REPO`). The default DATABASE_URL is a RELATIVE sqlite
    // path, so a helper running from `frontend/` opens a different -- and empty
    // -- database and reports `no such table: users`, which looks like a broken
    // schema rather than a wrong working directory.
    cwd: REPO_ROOT,
  });
  expect(
    proc.status,
    `attaching ${email} as ${role} failed.\n` +
      `  python : ${python}\n` +
      `  script : ${script}\n` +
      `  status : ${proc.status}\n` +
      // `proc.error` is the ONLY signal when the spawn itself failed: in that case
      // status is null and both output streams are undefined, so without this the
      // failure reads as a bare "undefinedundefined".
      `  error  : ${proc.error?.message ?? "none"}\n` +
      `  stdout : ${proc.stdout || "(empty)"}\n` +
      `  stderr : ${proc.stderr || "(empty)"}`,
  ).toBe(0);
}
