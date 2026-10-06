import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/* Absolute paths, resolved from THIS file's location.
 *
 * The first attempt used relative paths with a `cwd`, and Playwright's spawned
 * shell could not find them ("The system cannot find the path specified") --
 * the browser never launched and the failure looked like a harness problem.
 * Resolving against the config file removes the ambiguity entirely.
 */
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const PYTHON = resolve(REPO, "backend", ".venv", "Scripts", "python.exe");

/* Real-browser verification for Work 16.5.4 §6-§10.
 *
 * WHY THE INSTALLED CHROME, NOT A DOWNLOADED BROWSER
 * --------------------------------------------------
 * `channel: "chrome"` drives the Chrome already on this machine, so the suite
 * needs no `playwright install` step and no ~150 MB download. That matters here:
 * a browser download is exactly the kind of step that fails on a locked-down or
 * intermittently-restarted environment, and a suite that cannot run is worse
 * than no suite because it reads as coverage.
 *
 * The servers are started by Playwright's `webServer`, not assumed to be running.
 * A browser suite that silently attaches to whatever happens to be on :8000 is
 * not reproducible.
 */
const BACKEND = process.env.YMONEY_API ?? "http://127.0.0.1:8099";
const FRONTEND = process.env.YMONEY_WEB ?? "http://127.0.0.1:4178";

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],

  use: {
    baseURL: FRONTEND,
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "desktop-1440",
      use: { ...devices["Desktop Chrome"], channel: "chrome", viewport: { width: 1440, height: 900 } },
    },
  ],

  webServer: [
    {
      // The API, on a dedicated port so a developer's own :8000 is never touched.
      command: `"${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port 8099 --app-dir "${resolve(REPO, "backend")}"`,
      cwd: REPO,
      url: `${BACKEND}/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
      // The frontend proxies /api to this target, so it must agree with it.
      env: { YMONEY_API_TARGET: BACKEND },
    },
    {
      command: `npx vite preview --port 4178 --strictPort --host 127.0.0.1`,
      cwd: HERE,
      url: FRONTEND,
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
      env: { YMONEY_API_TARGET: BACKEND },
    },
  ],
});

export { BACKEND, FRONTEND };