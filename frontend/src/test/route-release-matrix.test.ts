/* Work 16.5.7 §7 -- regression guards for the route release matrix.
 *
 * The matrix is only worth anything if it cannot quietly stop covering a
 * screen.  These tests are the part that makes it un-driftable, and the one
 * that matters most is the FIRST: a route added to `routes/registry.ts` with no
 * matrix entry has to FAIL HERE.  A matrix that silently stops covering the
 * newest route is worse than no matrix, because it still looks like coverage.
 *
 * The matrix is generated data (`docs/UI_ROUTE_RELEASE_MATRIX.json`), so
 * nothing here imports a feature module and asserts it uses the routes it uses
 * -- that would be a tautology.  The registry and the artefact are compared as
 * INDEPENDENT artefacts, and the API-contract guard reads
 * `docs/UI_CONTRACT_AUDIT.json` rather than recomputing anything.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ROUTES } from "../routes/registry";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const REPO = resolve(SRC, "..", "..");

const MATRIX_PATH = join(REPO, "docs", "UI_ROUTE_RELEASE_MATRIX.json");
const AUDIT_PATH = join(REPO, "docs", "UI_CONTRACT_AUDIT.json");

type Dim = boolean | string | { endpoints?: number; declared?: number; undeclared?: string[] };

type Row = {
  path: string;
  label?: string;
  permission?: string | null;
  hidden?: boolean;
  component?: string | null;
  render?: Dim;
  loading?: Dim;
  populated?: Dim;
  empty?: Dim;
  error?: Dim;
  "403"?: Dim;
  contract?: Dim;
  responsive_1440?: Dim;
  responsive_1024?: Dim;
  responsive_768?: Dim;
  responsive_360?: Dim;
  a11y?: Dim;
  notes?: string[];
};

type Matrix = { dimensions: string[]; routeCount: number; routes: Row[] };

/** The twelve dimensions, in the order the work order states them. */
const DIMENSIONS = [
  "render",
  "loading",
  "populated",
  "empty",
  "error",
  "403",
  "contract",
  "responsive_1440",
  "responsive_1024",
  "responsive_768",
  "responsive_360",
  "a11y",
] as const;

/** Values the matrix is allowed to hold instead of a measurement. */
const HONEST = new Set(["unobserved", "not-seedable", "provider-gated", "skipped", "not-applicable"]);

function loadMatrix(): Matrix {
  expect(
    existsSync(MATRIX_PATH),
    `${MATRIX_PATH} is missing. Generate it with:\n` +
      `  cd frontend && npx playwright test route-matrix\n` +
      `  backend\\.venv\\Scripts\\python.exe scripts\\gen_route_release_matrix.py`,
  ).toBe(true);
  return JSON.parse(readFileSync(MATRIX_PATH, "utf-8")) as Matrix;
}

const matrix = loadMatrix();
const rows = matrix.routes;
const REGISTRY_PATHS = ROUTES.map((r) => r.path);

describe("§7 the matrix covers exactly the registry, with nothing extra", () => {
  it("has one entry per registry path and no duplicates", () => {
    expect(rows.length, "matrix row count").toBe(REGISTRY_PATHS.length);

    const matrixPaths = rows.map((r) => r.path);
    const duplicates = matrixPaths.filter((p, i) => matrixPaths.indexOf(p) !== i);
    expect(duplicates, "duplicate matrix paths").toEqual([]);
    expect(new Set(matrixPaths).size, "distinct matrix paths").toBe(matrixPaths.length);
  });

  it("declares the same count the registry does", () => {
    // Deliberately a literal, not `ROUTES.length`.  A guard that reads the
    // registry for its own expectation cannot fail when a route is added, which
    // is the one thing it exists to catch.
    expect(REGISTRY_PATHS.length).toBe(22);
    expect(matrix.routeCount).toBe(22);
    expect(rows.length).toBe(22);
  });

  it("has an entry for every registry path", () => {
    const matrixPaths = new Set(rows.map((r) => r.path));
    const missing = REGISTRY_PATHS.filter((p) => !matrixPaths.has(p));
    expect(
      missing,
      "registry routes with NO matrix entry -- re-run `npx playwright test route-matrix`",
    ).toEqual([]);
  });

  it("has no path the registry does not declare", () => {
    const registrySet = new Set(REGISTRY_PATHS);
    const stale = rows.map((r) => r.path).filter((p) => !registrySet.has(p));
    expect(stale, "matrix rows the registry no longer declares").toEqual([]);
  });

  it("records the registry's own label, permission and hidden flag", () => {
    const byPath = new Map(rows.map((r) => [r.path, r]));
    for (const route of ROUTES) {
      const row = byPath.get(route.path);
      expect(row, `no matrix row for ${route.path}`).toBeTruthy();
      expect(row!.label, `${route.path} label`).toBe(route.label);
      expect(row!.permission, `${route.path} permission`).toBe(route.permission);
      expect(row!.hidden, `${route.path} hidden`).toBe(route.hidden === true);
      expect(row!.component, `${route.path} has no component recorded`).toBeTruthy();
    }
  });
});

describe("§7 every route carries all twelve dimensions", () => {
  it("declares the twelve dimensions in the artefact header", () => {
    expect(matrix.dimensions).toEqual([...DIMENSIONS]);
  });

  for (const route of ROUTES) {
    it(`${route.path} has all twelve dimensions, none null or pending`, () => {
      const row = rows.find((r) => r.path === route.path);
      expect(row, `no matrix row for ${route.path}`).toBeTruthy();

      for (const dim of DIMENSIONS) {
        const value = (row as unknown as Record<string, unknown>)[dim];
        expect(value, `${route.path}: \`${dim}\` is absent`).toBeDefined();
        expect(value, `${route.path}: \`${dim}\` is null`).not.toBeNull();
        if (dim === "contract") {
          const contract = value as { endpoints?: number; declared?: number };
          expect(typeof contract?.endpoints, `${route.path}: contract.endpoints`).toBe("number");
          expect(typeof contract?.declared, `${route.path}: contract.declared`).toBe("number");
          continue;
        }
        expect(typeof value, `${route.path}: \`${dim}\` is not a scalar`).toBe(
          typeof value === "boolean" ? "boolean" : "string",
        );
        expect(value, `${route.path}: \`${dim}\` is still "pending"`).not.toBe("pending");
        if (typeof value === "string") {
          expect(
            HONEST.has(value),
            `${route.path}: \`${dim}\` holds ${JSON.stringify(value)}, which is neither a boolean nor one of ` +
              `${[...HONEST].join(", ")}`,
          ).toBe(true);
        }
      }
    });
  }
});

describe("§7 the matrix asserts what the release actually requires", () => {
  it("every route renders the shell", () => {
    const offenders = rows.filter((r) => r.render !== true).map((r) => r.path);
    expect(offenders, "routes that did not render nav + a named <main>").toEqual([]);
  });

  it("every route is accessible at every recorded width", () => {
    // Both of these are pass/fail claims the sweep already proved in a real
    // browser, so pinning them here is free: a future edit that breaks the
    // landmark or the mobile drawer fails the unit suite, not the nightly one.
    const a11yOffenders = rows.filter((r) => r.a11y !== true).map((r) => r.path);
    expect(a11yOffenders, "routes with no named <main>, no single <h1>, or an unnamed control").toEqual([]);

    const widths = ["responsive_1440", "responsive_1024", "responsive_768", "responsive_360"];
    const offenders: string[] = [];
    for (const row of rows) {
      for (const width of widths) {
        if ((row as unknown as Record<string, unknown>)[width] !== true) {
          offenders.push(`${row.path} ${width}`);
        }
      }
    }
    expect(offenders, "widths with overflow or an unreachable navigation").toEqual([]);
  });
});

describe("§7 the endpoints behind the routes are declared responses", () => {
  it("reports no UNDECLARED_JSON for the endpoints the UI calls", () => {
    expect(
      existsSync(AUDIT_PATH),
      `${AUDIT_PATH} is missing; run \`npx vitest run src/test/openapi-contract.test.ts\` first`,
    ).toBe(true);
    const audit = JSON.parse(readFileSync(AUDIT_PATH, "utf-8")) as {
      byClass?: Record<string, number>;
    };
    const undeclared = audit.byClass?.UNDECLARED_JSON ?? 0;
    expect(
      undeclared,
      "ordinary JSON responses with no declared schema (see docs/UI_CONTRACT_AUDIT.json)",
    ).toBe(0);
  });

  it("records an endpoint count for every route, so zero cannot pass as clean", () => {
    const offenders = rows
      .filter((r) => {
        const contract = r.contract as { endpoints?: number; declared?: number } | undefined;
        return typeof contract?.endpoints !== "number" || typeof contract?.declared !== "number";
      })
      .map((r) => r.path);
    expect(offenders, "routes whose GET endpoints could not be derived or counted").toEqual([]);
  });
});
