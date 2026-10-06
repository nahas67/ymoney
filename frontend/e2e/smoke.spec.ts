import { test, expect } from "@playwright/test";

/* Can this machine actually drive a browser? Prove it before writing a suite
 * against it. If Chrome cannot launch, every later failure would be ambiguous
 * between "the app is broken" and "the harness never ran".
 */
test("a real browser launches and renders", async ({ page }) => {
  await page.setContent("<h1 id='x'>browser works</h1>");
  await expect(page.locator("#x")).toHaveText("browser works");

  const ua = await page.evaluate(() => navigator.userAgent);
  expect(ua).toContain("Chrome");

  // A second viewport, so a responsive check has something to resize.
  await page.setViewportSize({ width: 375, height: 700 });
  expect(await page.evaluate(() => window.innerWidth)).toBe(375);
});