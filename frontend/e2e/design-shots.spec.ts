import { expect, test } from "@playwright/test";
import {
  authenticate,
  gotoRoute,
  register,
  seedTimeline,
  type Account,
} from "./fixtures";

/* Design-concept screenshots for the from-scratch rebuild review.
 * Real Chrome, real backend, seeded workspace. Asserts render; writes PNGs
 * for human review against the approved design language. */
let account: Account;
let timelineId = "";

test.beforeAll(async ({ request }) => {
  account = await register(request);
  timelineId = await seedTimeline(request, account);
});

const SHOTS: { route: string; file: string }[] = [
  { route: "/", file: "command-center.png" },
  { route: "/studio", file: "studio.png" },
  { route: "/planner", file: "planner.png" },
  { route: "/projects", file: "projects.png" },
  { route: "/campaigns", file: "campaigns.png" },
  { route: "/analytics", file: "analytics.png" },
  { route: "/operations", file: "operations.png" },
  { route: "/settings", file: "settings.png" },
  { route: "/automation", file: "automation.png" },
];

for (const { route, file } of SHOTS) {
  test(`shot ${route}`, async ({ page }, testInfo) => {
    await authenticate(page, account);
    await page.setViewportSize({ width: 1440, height: 900 });
    await gotoRoute(page, route);
    await expect(page.locator("main h1")).toHaveCount(1);
    await expect.poll(async () => (await page.locator("main").innerText()).trim().length).toBeGreaterThan(0);
    await page.screenshot({ path: testInfo.outputPath(file), animations: "disabled" });
  });
}

test(`shot studio editor`, async ({ page }, testInfo) => {
  await authenticate(page, account);
  await page.setViewportSize({ width: 1440, height: 900 });
  await gotoRoute(page, `/studio/${timelineId}`);
  await expect(page.getByRole("button", { name: /play/i }).first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("studio-editor.png"), animations: "disabled" });
});
