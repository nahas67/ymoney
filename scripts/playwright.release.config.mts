/* CI/local release override: no edits to the product's developer launcher. */
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO = fileURLToPath(new URL("..", import.meta.url));
const output = process.env.YMONEY_RELEASE_OUTPUT;
const python = process.env.YMONEY_RELEASE_PYTHON;
const chromeOS = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  ["PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"].includes(key.toUpperCase()),
));
if (!output || !python) throw new Error("Use run_release_e2e.py to isolate the release backend");
// Capture evidence location before the product's isolated launcher sanitizes
// process.env. Preserve its runtime root for the suite's Python fixtures too.
const { default: base } = await import("../frontend/playwright.config.ts");
// Playwright reloads this config in its workers. Carry only evidence/toolchain
// coordinates forward after the base launcher has cleared inherited secrets.
Object.assign(process.env, chromeOS, { YMONEY_RELEASE_OUTPUT: output, YMONEY_RELEASE_PYTHON: python });

export default {
  ...base,
  testDir: resolve(REPO, "frontend/e2e"),
  outputDir: resolve(output, "test-results"),
  reporter: [
    ["list"],
    ["junit", { outputFile: resolve(output, "playwright.xml") }],
    ["html", { outputFolder: resolve(output, "html"), open: "never" }],
  ],
};
