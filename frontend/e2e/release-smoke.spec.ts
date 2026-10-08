import { expect, test } from "@playwright/test";
import { API, authenticate, gotoRoute, register, type Account } from "./fixtures";

// Only release gaps: existing Studio specs prove canonical edits/autosave and
// critical-e2e proves role refusals/toasts. No provider start/render/publish here.
let account: Account;
test.beforeEach(async ({ request, page }) => {
  account = await register(request);
  await authenticate(page, account);
});

test("Settings: vertical sections, search, focus, dirty/save and canonical reload", async ({ page, request }) => {
  await gotoRoute(page, "/settings");
  const sections = page.getByRole("navigation", { name: "Settings sections" });
  await expect(sections).toBeVisible();
  const navBox = await sections.boundingBox();
  const panelBox = await page.locator(".ymset-layout > section").boundingBox();
  expect(navBox).not.toBeNull();
  expect(panelBox).not.toBeNull();
  expect(navBox!.x + navBox!.width).toBeLessThanOrEqual(panelBox!.x);

  await page.getByLabel("Search settings").fill("webhooks");
  await expect(sections.getByRole("button")).toHaveCount(1);
  await sections.getByRole("button", { name: "Webhooks", exact: true }).click();
  await expect(page.locator("#ymset-heading")).toBeFocused();
  await page.getByLabel("Search settings").fill("");
  await sections.getByRole("button", { name: "Workspace", exact: true }).click();
  await expect(page.getByLabel("Name", { exact: true })).toBeVisible();
  const name = `Release workspace ${Date.now()}`;
  await page.getByLabel("Name", { exact: true }).fill(name);
  await expect(page.getByRole("status", { name: "Save state" })).toContainText("Unsaved changes");
  const saved = page.waitForResponse((r) => r.request().method() === "PATCH" && r.url().endsWith(`/workspaces/${account.workspaceId}`));
  await page.getByRole("button", { name: "Save workspace", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await expect(page.getByRole("status", { name: "Save state" })).toContainText("All changes saved");
  const read = await request.get(`${API}/api/v1/workspaces/${account.workspaceId}`, {
    headers: { Authorization: `Bearer ${account.token}` },
  });
  expect(read.status()).toBe(200);
  expect((await read.json()).name).toBe(name);
  await page.reload();
  await expect(page.getByLabel("Name", { exact: true })).toHaveValue(name);
  await expect(page.getByRole("button", { name: "Save workspace", exact: true })).toBeDisabled();
});

test("Command Center: console stays clean and drill-down tabs remain operable", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await gotoRoute(page, "/");
  await expect(page.getByRole("heading", { name: "What needs attention" })).toBeVisible();
  // The unmocked readiness endpoint runs real local capability probes; a slow
  // first probe can outlast the default 10s assertion timeout without being a
  // stuck loading state. Keep the longer wait scoped to this readiness panel.
  await expect(page.locator(".ops-attn .ym-skeleton")).toHaveCount(0, { timeout: 30_000 });
  const tabs = page.getByRole("tab");
  expect(await tabs.count()).toBeGreaterThan(1);
  for (const tab of await tabs.all()) {
    await tab.click();
    await expect(tab).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("tabpanel")).toBeVisible();
  }
  expect(errors, "browser console / uncaught exceptions").toEqual([]);
});

test("Automation: loading resolves from real backend; idle gates and policy are authoritative", async ({ page, request }) => {
  const statusURL = `${API}/api/v1/workspaces/${account.workspaceId}/autopilot/status`;
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`**/api/v1/workspaces/${account.workspaceId}/autopilot/status`, async (route) => {
    await held;
    await route.continue();
  });
  try {
    await gotoRoute(page, "/automation");
    await expect(page.locator("main .ym-skeleton").first()).toBeVisible();
  } finally {
    release();
  }
  const headers = { Authorization: `Bearer ${account.token}` };
  const status = await request.get(statusURL, { headers });
  expect(status.status()).toBe(200);
  expect((await status.json()).state).toBe("IDLE");
  await expect(page.getByRole("button", { name: "Start loop", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeDisabled();
  const configURL = `${API}/api/v1/workspaces/${account.workspaceId}/agents/config`;
  const config = await request.get(configURL, { headers });
  expect(config.status()).toBe(200);
  const agent = (await config.json()).items[0] as { key: string; title: string; enabled: boolean };
  expect(agent).toBeTruthy();
  const row = page.getByRole("row").filter({ hasText: agent.title });
  const toggle = row.getByRole("checkbox");
  await expect(toggle).toBeChecked({ checked: agent.enabled });
  // Toggle hides its native checkbox (width/height 0) behind a visible label.
  // Click the user-facing control rather than the intentionally hidden input.
  await row.locator("label.ym-toggle").click();
  await expect.poll(async () => {
    const read = await request.get(configURL, { headers });
    expect(read.status()).toBe(200);
    return (await read.json()).items.find((item: { key: string }) => item.key === agent.key).enabled;
  }).toBe(!agent.enabled);
  await page.reload();
  await expect(row.getByRole("checkbox")).toBeChecked({ checked: !agent.enabled });
});

test("Shell: sidebar hover/focus, dialogs, mobile drawer and reduced motion", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await gotoRoute(page, "/");
  const settings = page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Settings", exact: true });
  await settings.hover();
  await settings.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(settings).toBeFocused();
  expect(await settings.evaluate((node) => {
    const style = getComputedStyle(node);
    return style.boxShadow !== "none" || (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0);
  })).toBe(true);
  expect(await settings.evaluate((node) => Math.max(...getComputedStyle(node).transitionDuration.split(",").map((s) => parseFloat(s))))).toBeLessThanOrEqual(0.001);
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(page.locator(".ym-shell")).toHaveClass(/ym-shell--collapsed/);
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await page.getByRole("button", { name: "Open command palette" }).click();
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Search", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toHaveCount(0);
  await page.getByRole("button", { name: /^Notifications/ }).click();
  await expect(page.getByRole("dialog", { name: "Notifications" })).toBeVisible();
  await page.getByRole("dialog", { name: "Notifications" }).getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Notifications" })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open navigation", exact: true }).click();
  await expect(page.getByRole("button", { name: "Close navigation", exact: true })).toHaveAttribute("aria-expanded", "true");
  await settings.click();
  await expect(page.getByRole("heading", { name: "Settings", exact: true, level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open navigation", exact: true })).toHaveAttribute("aria-expanded", "false");
});

test("Permission denial: a foreign workspace write is refused and cannot change canonical state", async ({ request }) => {
  const outsider = await register(request);
  const url = `${API}/api/v1/workspaces/${account.workspaceId}`;
  const owner = { headers: { Authorization: `Bearer ${account.token}` } };
  const before = await request.get(url, owner);
  expect(before.status()).toBe(200);
  const canonical = await before.json();
  const denied = await request.patch(url, {
    headers: { Authorization: `Bearer ${outsider.token}` },
    data: { name: "Forbidden release mutation", niche: "", brand_voice: "" },
  });
  expect(denied.status()).toBe(403);
  const after = await request.get(url, owner);
  expect(after.status()).toBe(200);
  expect((await after.json()).name).toBe(canonical.name);
});
