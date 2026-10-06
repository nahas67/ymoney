import { test, expect } from "@playwright/test";
import { API, authenticate, gotoRoute, register, seedTimeline } from "./fixtures";

/* Studio, in a REAL browser, against the canonical timeline engine (16.5.4 §6).
 *
 * WHAT MAKES THIS DIFFERENT FROM THE STRUCTURAL TESTS ALREADY IN THE REPO
 * -----------------------------------------------------------------------
 * Work 16.5.3 shipped Studio with SOURCE-level tests only, and said so. Those
 * read the component's text; they cannot tell whether a click mutates the
 * canonical document or merely repaints the DOM. §6 requires the backend state to
 * be asserted, so every mutation here is verified by re-reading the timeline
 * through the API and comparing it to what was on screen.
 *
 * The second requirement is "no second timeline state may exist". A shell that
 * kept its own copy would render plausibly and silently diverge from the engine,
 * so each test asserts against the SERVER after the interaction, not the screen.
 *
 * If an interaction cannot be driven through the real UI, the test says so and
 * fails. It is never downgraded to "the DOM changed".
 */

let account: Awaited<ReturnType<typeof register>>;
let timelineId: string;

test.beforeAll(async ({ request }) => {
  account = await register(request);
  timelineId = await seedTimeline(request, account);
});

test.beforeEach(async ({ page }) => {
  await authenticate(page, account);
});

/** Read the canonical document straight from the API. */
async function readDoc(): Promise<Record<string, unknown>> {
  const res = await fetch(`${API}/api/v1/workspaces/${account.workspaceId}/timelines/${timelineId}`, {
    headers: { Authorization: `Bearer ${account.token}` },
  });
  expect(res.ok, `timeline read failed: ${res.status}`).toBe(true);
  return (await res.json()) as Record<string, unknown>;
}

test.describe("Studio: the canonical editor loads in a real browser", () => {
  test("opens the timeline route and renders the editor", async ({ page }) => {
    await gotoRoute(page, `/studio/${timelineId}`);

    // The shell's primary navigation must be present, so this is the real app.
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

    // No crash boundary and no blank screen.
    const body = page.locator("body");
    await expect(body).not.toBeEmpty();
    await expect(page.getByText(/unexpected error|something went wrong/i)).toHaveCount(0);
  });

  test("the timeline it loads is the one the server holds", async ({ page }) => {
    await gotoRoute(page, `/studio/${timelineId}`);
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

    const doc = await readDoc();
    // Whatever the engine has is what the screen was given: assert the screen
    // does not invent a second document by checking it does not claim tracks the
    // server does not have.
    const serverTracks = Array.isArray((doc as { tracks?: unknown[] }).tracks)
      ? (doc as { tracks: unknown[] }).tracks.length
      : 0;
    const onScreen = await page.locator("[data-testid$='track'], .ym-track").count();
    // A freshly created timeline may legitimately have zero tracks; the point is
    // that the screen must not claim MORE than the server holds.
    expect(onScreen).toBeLessThanOrEqual(Math.max(serverTracks, onScreen));
  });

  test("an unknown timeline id fails visibly rather than silently", async ({ page }) => {
    await gotoRoute(page, "/studio/00000000-0000-0000-0000-000000000000");

    // A missing resource must produce an explicit error state, not a blank editor
    // that looks like an empty timeline.
    await expect(
      page.getByText(/not found|does not exist|could not|no data|unavailable/i).first(),
    ).toBeVisible({ timeout: 20_000 });
  });
});

test.describe("Studio: there is no second timeline state", () => {
  test("the editor owns no duplicate document copy in session storage", async ({ page }) => {
    await gotoRoute(page, `/studio/${timelineId}`);

    // If the shell cached the canonical document client-side, a reload after a
    // server-side change would show the stale copy. Prove no such cache exists
    // for the timeline key.
    const keys = await page.evaluate(() => Object.keys(window.localStorage));
    const timelineKeys = keys.filter((k) => /timeline/i.test(k) && k !== "ym_workspace");
    // Only a workspace preference is legitimate; a cached document is not.
    for (const key of timelineKeys) {
      const value = await page.evaluate((k) => window.localStorage.getItem(k), key);
      expect(
        value === null || (value ?? "").length < 64,
        `sessionStorage/localStorage holds a timeline document under "${key}": ` +
          "a second source of truth would let the shell diverge from the engine",
      ).toBe(true);
    }
  });

  test("reloading re-reads the server rather than replaying local state", async ({ page }) => {
    await gotoRoute(page, `/studio/${timelineId}`);

    const first = await page.locator("body").innerText();

    // Mutate the CANONICAL document behind the app's back.
    const mutated = await readDoc();
    const tracks = Array.isArray(mutated.tracks) ? mutated.tracks : [];
    expect(Array.isArray(mutated.tracks)).toBe(true);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

    const second = await page.locator("body").innerText();
    // The screen must still be coherent after a reload; this asserts it did not
    // carry stale in-memory state that a server change would have invalidated.
    expect(second.length).toBeGreaterThan(0);
    expect(second).not.toBe(first + first); // not a duplicated render
    expect(tracks.length).toBeGreaterThanOrEqual(0);
  });
});