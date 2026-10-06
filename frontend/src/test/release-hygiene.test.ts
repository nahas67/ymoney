/* Release hygiene (Work 16.5.3 §4, §14, §15).
 *
 * Three things that are easy to state and easy to quietly lose:
 *
 *   §14  dead frontend code. A module nothing imports is not "harmless", it is a
 *        second copy of a decision nobody will update.
 *   §15  design-system drift. A colour or spacing chosen inside a feature means
 *        two screens will eventually disagree about what "warning" looks like.
 *   §4   unsafe transport casts. `as unknown as T` on an API boundary converts
 *        "unvalidated" into "believed", which is precisely what the contract
 *        layer exists to prevent.
 *
 * WHY §4 HAS A CEILING INSTEAD OF A ZERO
 * --------------------------------------
 * The remaining casts are not spread across the rebuilt product code. They are
 * concentrated in modules imported ONLY by `pages/Editor.tsx` -- the CANONICAL
 * timeline engine, plus the intel/motion/collab panels it mounts, plus the
 * engine's own `editor/adapters/timelineAdapter.ts`.
 *
 * Work 16.5.1 §10 is explicit: the timeline engine is not to be rebuilt. So
 * driving these to zero here would mean rewriting the engine, which is the one
 * thing this programme forbids. Asserting zero would be a test that fails on the
 * correct codebase; asserting nothing would hide the debt.
 *
 * So the assertion is a FLOOR on the debt (it must not grow) plus a PROOF that
 * the debt lives where the documentation says it does. When the engine is
 * eventually re-typed, the floor can be lowered -- and this file is where that
 * change will be obvious.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, dirname, resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");

function walk(dir: string, exts: string[]): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p, exts));
    else if (exts.some((e) => name.endsWith(e))) out.push(p);
  }
  return out;
}

const ALL = walk(SRC, [".ts", ".tsx"]);
const PRODUCT = ALL.filter((p) => !/\.test\.[tj]sx?$/.test(p));

function rel(p: string): string {
  return relative(SRC, p).replace(/\\/g, "/");
}

function lines(p: string): string[] {
  return readFileSync(p, "utf-8").split("\n");
}

/** Every relative module specifier a file imports, statically or dynamically. */
function specifiers(p: string): string[] {
  const text = readFileSync(p, "utf-8");
  const out = [
    ...[...text.matchAll(/from\s+"([^"]+)"/g)].map((m) => m[1]),
    ...[...text.matchAll(/import\(\s*"([^"]+)"\s*\)/g)].map((m) => m[1]),
  ];
  return out;
}

function resolveSpec(spec: string, importer: string): string | null {
  if (!spec.startsWith(".")) return null;
  const base = resolve(dirname(importer), spec);
  for (const cand of [`${base}.tsx`, `${base}.ts`, base]) {
    if (existsSync(cand) && statSync(cand).isFile()) return cand;
  }
  return null;
}

/* =========================================================================
 * §14 -- dead code
 * ====================================================================== */

const ENTRYPOINTS = new Set(["main.tsx", "vite-env.d.ts", "setup.ts"]);

function orphanedModules(): string[] {
  const referenced = new Set<string>();
  for (const f of ALL) {
    for (const spec of specifiers(f)) {
      const r = resolveSpec(spec, f);
      if (r) referenced.add(resolve(r));
    }
  }
  return PRODUCT.filter(
    (p) =>
      !referenced.has(resolve(p)) &&
      !ENTRYPOINTS.has(p.split(/[\\/]/).pop() as string) &&
      !p.endsWith(".d.ts"),
  ).map(rel);
}

describe("§14 dead code", () => {
  it("has no orphaned modules", () => {
    // A module nothing imports is a second copy of a decision nobody updates.
    expect(orphanedModules(), "delete these, or wire them in").toEqual([]);
  });

  it("has no legacy product pages beyond the canonical editor and login", () => {
    const pages = readdirSync(join(SRC, "pages")).filter((f) => f.endsWith(".tsx")).sort();
    // Editor.tsx is the CANONICAL timeline engine (16.5.1 §10: not to be
    // rebuilt). Login is not a product screen. Everything else was rebuilt and
    // its old page deleted.
    expect(pages).toEqual(["Editor.tsx", "Login.tsx"]);
  });

  it("has no duplicate API client", () => {
    // `lib/api.ts` is the single transport. A second one means two places to
    // change the base URL, the auth header and the error shape.
    const clients = PRODUCT.filter((p) =>
      /(?:^|\/)(api|client|fetch)\.tsx?$/.test(rel(p)),
    ).map(rel);
    expect(clients.filter((c) => c.startsWith("features/"))).toEqual([]);
    expect(clients).toEqual(["lib/api.ts"]);
  });

  it("has no legacy bridge component in the shell", () => {
    // Every route is REBUILT, so the bridge must be unreachable dead code.
    const registry = readFileSync(join(SRC, "routes", "registry.ts"), "utf-8");
    expect(registry).not.toContain("LEGACY_BRIDGE\",");
  });
});

/* =========================================================================
 * §15 -- design system
 * ====================================================================== */

describe("§15 design system", () => {
  it("uses no off-system colour literals in rebuilt features", () => {
    // NOTE the negative lookbehind: `&#123;` is a JSX HTML entity, not a hex
    // colour. A naive `#[0-9a-f]{3,8}` match flags every `{entry_id}` in a
    // doc string and drowns the real finding.
    const COLOUR = /(?<!&)#[0-9a-fA-F]{3,8}\b|\brgba?\(/;
    const offenders: string[] = [];
    for (const p of PRODUCT.filter((f) => rel(f).startsWith("features/"))) {
      lines(p).forEach((line, i) => {
        if (COLOUR.test(line)) offenders.push(`${rel(p)}:${i + 1}: ${line.trim().slice(0, 90)}`);
      });
    }
    expect(
      offenders,
      "a colour chosen inside a feature; use the token layer instead",
    ).toEqual([]);
  });

  it("uses no inline <style> blocks in rebuilt features", () => {
    const offenders: string[] = [];
    for (const p of PRODUCT.filter((f) => rel(f).startsWith("features/"))) {
      if (/<style[\s>]/.test(readFileSync(p, "utf-8"))) offenders.push(rel(p));
    }
    expect(offenders).toEqual([]);
  });

  it("keeps one stylesheet set: tokens, system, one entry", () => {
    const css = walk(SRC, [".css"]).map(rel).sort();
    expect(css).toEqual(["design-system/styles.css", "design-system/tokens.css", "index.css"]);
  });
});

/* =========================================================================
 * §4 -- unsafe transport casts
 * ====================================================================== */

type Cast = { file: string; line: number; text: string };

function findCasts(pattern: RegExp, filter?: (f: string) => boolean): Cast[] {
  const out: Cast[] = [];
  for (const p of PRODUCT) {
    const r = rel(p);
    if (filter && !filter(r)) continue;
    lines(p).forEach((line, i) => {
      // A COMMENT may legitimately discuss the pattern. This suite is about what
      // ships, so a doc comment saying "do not use `as any` here" must not be
      // counted as a violation -- otherwise the honest documentation of a rule
      // becomes the evidence that breaks it.
      const stripped = line.replace(/\/\/.*$/, "").replace(/^\s*\*.*$/, "");
      if (pattern.test(stripped)) {
        out.push({ file: r, line: i + 1, text: line.trim().slice(0, 100) });
      }
    });
  }
  return out;
}

/** Modules reachable ONLY from the canonical engine, which §10 protects. */
const ENGINE_ONLY =
  /^(pages\/Editor\.tsx|components\/(intel|motion|collab)\/|editor\/)/;

describe("§4 unsafe transport casts", () => {
  it("rebuilt product features contain no `unknown as T` transport bypass", () => {
    // The habit this work order removes: asserting a shape the server never
    // promised. Features must narrow through a declared contract instead.
    const offenders = findCasts(
      /\bas\s+unknown\s+as\b/,
      (r) => r.startsWith("features/"),
    );
    expect(
      offenders,
      offenders.map((o) => `${o.file}:${o.line}: ${o.text}`).join("\n"),
    ).toEqual([]);
  });

  it("rebuilt product features contain no `as any`", () => {
    const offenders = findCasts(/\bas\s+any\b/, (r) => r.startsWith("features/"));
    expect(
      offenders,
      offenders.map((o) => `${o.file}:${o.line}: ${o.text}`).join("\n"),
    ).toEqual([]);
  });

  it("the remaining cast debt has not grown", () => {
    // Recorded at the Work 16.5.3 baseline. This is a FLOOR, not an ambition:
    // the debt lives in the canonical timeline engine, which §10 forbids
    // rewriting, so demanding zero here would fail on a correct codebase.
    const engine = findCasts(/\bas\s+unknown\s+as\b|\bas\s+any\b|\bany\[\]|<any>/, (r) =>
      ENGINE_ONLY.test(r),
    ).length;
    // eslint-disable-next-line no-console
    console.log(`[hygiene] cast debt inside the canonical engine surface: ${engine}`);
    expect(engine).toBeGreaterThan(0);
    expect(engine).toBeLessThanOrEqual(60);
  });

  it("proves the cast debt really is confined to the engine surface", () => {
    // If this fails, debt has leaked into rebuilt code and the ceiling above is
    // measuring the wrong thing.
    const elsewhere = findCasts(
      /\bas\s+unknown\s+as\b/,
      (r) => !ENGINE_ONLY.test(r) && !r.startsWith("test/"),
    ).map((c) => `${c.file}:${c.line}`);
    expect(elsewhere, `cast debt leaked outside the engine: ${elsewhere.join(", ")}`).toEqual(
      [],
    );
  });
});