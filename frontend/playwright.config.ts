import { defineConfig, devices } from "@playwright/test";
import { cpSync, existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");

function localURL(value: string): URL {
  const url = new URL(value);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("E2E server URLs must be plain http://127.0.0.1:<port> origins");
  }
  return url;
}

const api = localURL(process.env.YMONEY_API ?? "http://127.0.0.1:8099");
const web = localURL(process.env.YMONEY_WEB ?? "http://127.0.0.1:4178");
const BACKEND = api.origin;
const FRONTEND = web.origin;
if (BACKEND === FRONTEND) throw new Error("E2E API and web ports must differ");
const PYTHON = resolve(process.env.YMONEY_E2E_PYTHON ?? join(
  REPO, "backend", ".venv", ...(process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"]),
));
if (!existsSync(PYTHON)) throw new Error(`E2E Python missing: ${PYTHON}; set YMONEY_E2E_PYTHON`);

// Playwright merges webServer.env with process.env. Sanitize the runner itself
// so both servers AND existing subprocess fixtures inherit only OS necessities.
const inheritedRoot = process.env.YMONEY_E2E_ROOT;
const osKeys = new Set([
  "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
  "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "CI",
]);
for (const key of Object.keys(process.env)) {
  if (!osKeys.has(key.toUpperCase())) delete process.env[key];
}

// Copy code only: never developer .env, data, tokens, logs or the virtualenv.
// Workers inherit this root; a new invocation always gets a fresh database.
const tempRoot = join(tmpdir(), "opencode");
mkdirSync(tempRoot, { recursive: true });
const ROOT = inheritedRoot ?? mkdtempSync(join(tempRoot, "ymoney-e2e-"));
if (existsSync(join(ROOT, ".env")) || existsSync(join(ROOT, "backend", ".env"))) {
  throw new Error("E2E runtime must not contain dotenv files");
}
if (!inheritedRoot) {
  cpSync(join(REPO, "backend", "app"), join(ROOT, "backend", "app"), {
    recursive: true,
    filter: (path) => !path.split(/[\\/]/).includes("__pycache__") && !path.endsWith(".pyc"),
  });
  mkdirSync(join(ROOT, "scripts"), { recursive: true });
  cpSync(join(REPO, "scripts", "attach_workspace_member.py"), join(ROOT, "scripts", "attach_workspace_member.py"));
  // Fail closed on accidental provider calls, including DNS lookups. Loopback
  // remains available to the real API/browser; no successful app responses mock.
  writeFileSync(join(ROOT, "sitecustomize.py"), `import ipaddress, socket
def _local(host):
    if host == 'localhost': return
    try: allowed = ipaddress.ip_address(host).is_loopback
    except ValueError: allowed = False
    if not allowed: raise OSError('E2E outbound network denied: ' + str(host))
_resolve = socket.getaddrinfo
def _guard_resolve(host, *args, **kwargs):
    _local(host)
    return _resolve(host, *args, **kwargs)
socket.getaddrinfo = _guard_resolve
_connect = socket.socket.connect
def _guard_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6): _local(address[0])
    return _connect(self, address)
socket.socket.connect = _guard_connect
_connect_ex = socket.socket.connect_ex
def _guard_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6): _local(address[0])
    return _connect_ex(self, address)
socket.socket.connect_ex = _guard_connect_ex
`);
}
Object.assign(process.env, {
  YMONEY_E2E_ROOT: ROOT,
  YMONEY_E2E_PYTHON: PYTHON,
  YMONEY_API: BACKEND,
  YMONEY_WEB: FRONTEND,
  YMONEY_API_TARGET: BACKEND,
  PYTHONPATH: [ROOT, join(ROOT, "backend")].join(process.platform === "win32" ? ";" : ":"),
  PYTHONIOENCODING: "utf-8",
  PYTHONDONTWRITEBYTECODE: "1",
  DATABASE_URL: `sqlite:///${join(ROOT, "e2e.db").replace(/\\/g, "/")}`,
  STORAGE_ROOT: join(ROOT, "media"),
  STORAGE_STAGING_DIR: join(ROOT, "staging"),
  BACKUP_DIR: join(ROOT, "backups"),
  YMONEY_ENV: "test",
  SECRET_KEY: "isolated-e2e-not-a-deployment-secret-32chars",
  TELEGRAM_ENABLED: "false",
  GOOGLE_TRENDS_ENABLED: "false",
  CORS_ALLOWED_ORIGINS: FRONTEND,
  PUBLIC_BASE_URL: BACKEND,
});

// envDir:false prevents Vite's independent .env loading too. Use the same Node
// executable that launched Playwright (including portable release runtimes).
const previewScript = join(ROOT, "preview.mjs");
writeFileSync(previewScript, `import { preview } from ${JSON.stringify(pathToFileURL(join(HERE, "node_modules/vite/dist/node/index.js")).href)};
await preview({ root: ${JSON.stringify(HERE)}, configFile: ${JSON.stringify(join(HERE, "vite.config.ts"))}, envDir: false, preview: { port: ${Number(web.port || 80)}, strictPort: true, host: '127.0.0.1' } });
`);

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
    trace: "retain-on-failure",
    video: "off",
    screenshot: "only-on-failure",
  },
  projects: [{
    name: "desktop-1440",
    use: { ...devices["Desktop Chrome"], channel: "chrome", viewport: { width: 1440, height: 900 } },
  }],
  webServer: [{
    command: `"${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port ${api.port || 80}`,
    cwd: ROOT,
    url: `${BACKEND}/health`,
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  }, {
    command: `"${process.execPath}" "${previewScript}"`,
    cwd: ROOT,
    url: FRONTEND,
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  }],
});

export { BACKEND, FRONTEND };
