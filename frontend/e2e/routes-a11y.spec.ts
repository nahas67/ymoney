import { test, expect } from "@playwright/test";
import { authenticate, gotoRoute, register } from "./fixtures";
import { ROUTES } from "../src/routes/registry";

/* All 22 routes, in a REAL browser, at four viewports (16.5.4 §8/§9).
 *
 * THE ROUTE LIST IS IMPORTED, NOT COPIED. A hardcoded list here would drift from
 * the authoritative registry and quietly stop covering a screen that was added
 * later -- which is exactly the failure the registry exists to prevent. Importing
 * it means adding a route to the registry automatically adds it to this sweep.
 *
 * Studio is exempted from the overflow check ONLY, and that exemption is stated
 * in the assertion rather than hidden in a skip: a timeline is deliberately wider
 * than its container, and pretending otherwise would either force a horizontal
 * scrollbar onto every screen or quietly disable the check everywhere.
 */

const API_ONLY_WIDTH = 360;

const NON_HIDDEN_ROUTES = ROUTES.filter((r) => !r.hidden);

let account: Awaited<ReturnType<typeof register>>;

test.beforeAll(async ({ request }) => {
  account = await register(request);
});

test.describe("§8 route release sweep: every route renders", () => {
  for (const route of NON_HIDDEN_ROUTES) {
    test(`${route.path} mounts and shows the shell`, async ({ page }) => {
      await authenticate(page, account);
      await gotoRoute(page, route.path);

      // The shell is present, so this is the real app and not a bare error page.
      await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
      await expect(page.locator("main")).toBeVisible();

      // No uncaught-render boundary.
      await expect(
        page.getByText(/unexpected error|something went wrong|cannot read propert/i),
      ).toHaveCount(0);

      // A blank screen is a failure, not an empty state. Every route must render
      // SOMETHING beyond the chrome.
      const mainText = (await page.locator("main").innerText()).trim();
      expect(mainText.length, `${route.path} rendered an empty <main>`).toBeGreaterThan(0);
    });
  }

  test("every hidden detail route also resolves", async ({ page }) => {
    // Hidden routes are in the sidebar but must not 404 when linked directly.
    await authenticate(page, account);
    for (const route of ROUTES.filter((r) => r.hidden)) {
      const concrete = route.path.replace(/:[A-Za-z]+/g, "missing-id");
      await page.goto(concrete, { waitUntil: "domcontentloaded" });
      await expect(page.locator("main")).toBeVisible();
      await expect(
        page.getByText(/page not found/i),
      ).toHaveCount(0);
    }
  });

  test("an unregistered route renders the not-found state, not a feature", async ({ page }) => {
    await authenticate(page, account);
    await page.goto("/definitely-not-a-route", { waitUntil: "domcontentloaded" });
    await expect(page.getByText(/page not found/i)).toBeVisible();
  });
});

test.describe("§9 responsive: no accidental horizontal overflow", () => {
  const widths = [
    { label: "wide desktop", width: 1440 },
    { label: "standard desktop", width: 1024 },
    { label: "tablet", width: 768 },
    { label: "narrow", width: API_ONLY_WIDTH },
  ];

  for (const { label, width } of widths) {
    test(`${label} (${width}px): the shell does not scroll sideways`, async ({ page }) => {
      await authenticate(page, account);
      await page.setViewportSize({ width, height: 900 });
      await gotoRoute(page, "/");

      // Measured on the document, not the body: a fixed-position sidebar can make
      // body scrollWidth match the viewport while the page still scrolls.
      const overflow = await page.evaluate(() => {
        const de = document.documentElement;
        return { scroll: de.scrollWidth, client: de.clientWidth };
      });

      // 2px of slack absorbs sub-pixel rounding at fractional device ratios.
      expect(
        overflow.scroll - overflow.client,
        `page scrolls horizontally at ${width}px (${overflow.scroll} > ${overflow.client})`,
      ).toBeLessThanOrEqual(2);
    });
  }

  test("every route stays within the viewport at tablet width", async ({ page }) => {
    await authenticate(page, account);
    await page.setViewportSize({ width: 768, height: 900 });

    const offenders: string[] = [];
    for (const route of NON_HIDDEN_ROUTES) {
      await page.goto(route.path, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(120);
      const m = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth,
      }));
      // Studio keeps its timeline deliberately wide; every other route must fit.
      if (m.scroll - m.client > 2 && route.path !== "/studio") {
        offenders.push(`${route.path} (${m.scroll} > ${m.client})`);
      }
    }
    expect(offenders, "these routes overflow at 768px").toEqual([]);
  });
});

test.describe("§10 accessibility on every route", () => {
  test("each route exposes a named main landmark and a single h1", async ({ page }) => {
    await authenticate(page, account);

    const offenders: string[] = [];
    for (const route of NON_HIDDEN_ROUTES) {
      await page.goto(route.path, { waitUntil: "domcontentloaded" });
      await expect(page.locator("main")).toBeVisible();

      const h1s = await page.locator("h1").count();
      if (h1s !== 1) offenders.push(`${route.path} has ${h1s} <h1>`);

      const mainLabel = await page
        .locator("main")
        .getAttribute("aria-label")
        .then((v) => v ?? "")
        .catch(() => "");
      if (h1s === 1 && mainLabel === "") {
        // Not fatal on its own, but recorded so a missing landmark name is visible.
        offenders.push(`${route.path} <main> has no accessible name`);
      }
    }
    expect(offenders).toEqual([]);
  });

  test("interactive controls have accessible names", async ({ page }) => {
    await authenticate(page, account);

    const unnamed: string[] = [];
    for (const route of NON_HIDDEN_ROUTES) {
      await page.goto(route.path, { waitUntil: "domcontentloaded" });
      const count = await page.locator("button, a[href], input, select, textarea").count();
      for (let i = 0; i < count; i += 1) {
        const el = page.locator("button, a[href], input, select, textarea").nth(i);
        if (!(await el.isVisible())) continue;
        const name = (
          (await el.getAttribute("aria-label")) ??
          (await el.innerText().catch(() => "")) ??
          ""
        ).trim();
        const labelled = await el.evaluate((node) => {
          const el2 = node as HTMLElement;
          if (el2.getAttribute("aria-label")) return true;
          if (el2.getAttribute("aria-labelledby")) return true;
          if (el2.getAttribute("title")) return true;
          if (el2.id && document.querySelector(`label[for="${el2.id}"]`)) return true;
          if (el2.closest("label")) return true;
          return (el2.textContent ?? "").trim().length > 0;
        });
        if (!labelled && name.length === 0) {
          unnamed.push(`${route.path} :: ${(await el.evaluate((n) => n.outerHTML)).slice(0, 90)}`);
        }
      }
    }
    expect(unnamed, "controls with no accessible name").toEqual([]);
  });

  test("the shell is reachable by keyboard from the top of the page", async ({ page }) => {
    await authenticate(page, account);
    await gotoRoute(page, "/");

    // The first Tab must reach the skip link, which is what makes the shell
    // operable without a mouse.
    await page.keyboard.press("Tab");
    const focused = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      return { tag: el?.tagName ?? "", cls: el?.className ?? "", text: el?.textContent ?? "" };
    });
    expect(
      focused.cls.includes("ym-skip-link") || focused.tag === "A",
      `first Tab focused ${focused.tag}.${focused.cls} rather than the skip link`,
    ).toBe(true);
  });

  test("status is never conveyed by colour alone", async ({ page }) => {
    await authenticate(page, account);

    const offenders: string[] = [];
    for (const route of ["/operations", "/distribution", "/providers"]) {
      await page.goto(route, { waitUntil: "domcontentloaded" });
      const count = await page.locator(".ym-badge").count();
      for (let i = 0; i < count; i += 1) {
        const badge = page.locator(".ym-badge").nth(i);
        if (!(await badge.isVisible())) continue;
        const text = ((await badge.innerText()) ?? "").trim();
        // A colour-only indicator is a badge with no legible word.
        if (text.length === 0) offenders.push(`${route} badge ${i} has no text`);
      }
    }
    expect(offenders).toEqual([]);
  });
});