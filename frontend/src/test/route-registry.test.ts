/* Route registry parity and smoke (Work 16.5.1 §3/§14, Work 16.5.2).
 *
 * §3 says: one authoritative route registry, no duplicated route definitions.
 * A rule nobody checks decays -- someone adds a nav item, points it at a path
 * with no component, and the failure only appears as a 404 at runtime. These
 * tests make that impossible.
 *
 * Both sides are read as TEXT. Importing the registry and App.tsx and comparing
 * them in memory would be circular: App.tsx imports the registry, so that
 * comparison could only ever prove the registry equals itself.
 *
 * Work 16.5.2 update: every screen is rebuilt, so `LEGACY_ROUTES` is empty and
 * that is asserted as a FAILURE signal -- a screen left behind cannot hide.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  LEGACY_ROUTES,
  NAV_ROUTES,
  REBUILT_ROUTES,
  ROUTES,
  ROUTE_GLYPH,
  ROUTE_GROUPS,
  breadcrumbsFor,
  routeAt,
  routeInventory,
  searchRoutes,
} from "../routes/registry";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const REPO = resolve(SRC, "..", "..");

const APP_SRC = readFileSync(join(SRC, "app", "App.tsx"), "utf-8");

/** Paths present in App.tsx's COMPONENTS map. */
const COMPONENT_KEYS = new Set(
  [...APP_SRC.matchAll(/^\s*"\/[^"]*":\s*</gm)].map((m) =>
    m[0].trim().replace(/^"/, "").replace(/":\s*<$/, ""),
  ),
);

/** Literal `path="..."` on a <Route>. */
function routerPaths(): string[] {
  return [...APP_SRC.matchAll(/<Route\s[^>]*?\bpath="([^"]+)"/g)].map((m) => m[1]);
}

describe("route registry is the single source of truth", () => {
  it("declares every route exactly once", () => {
    const paths = ROUTES.map((r) => r.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("has no route outside a declared group", () => {
    const groupIds = new Set(ROUTE_GROUPS.map((g) => g.id));
    for (const r of ROUTES) expect(groupIds.has(r.group)).toBe(true);
  });

  it("every visible route has a glyph, and every glyph maps to a real route", () => {
    for (const r of NAV_ROUTES) {
      expect(ROUTE_GLYPH[r.path], `no glyph for ${r.path}`).toBeDefined();
    }
    for (const glyphPath of Object.keys(ROUTE_GLYPH)) {
      expect(
        ROUTES.some((r) => r.path === glyphPath),
        `glyph declared for ${glyphPath}, which is not a registered route`,
      ).toBe(true);
    }
  });

  it("records a state for every route, and it is one of the two honest values", () => {
    for (const r of ROUTES) {
      expect(["REBUILT", "LEGACY_BRIDGE"]).toContain(r.state);
    }
  });

  it("routes are GENERATED from the registry, not listed by hand", () => {
    // A hand-written list beside the registry is the duplication §3 forbids.
    expect(APP_SRC).toContain("ROUTES.map");
    const literalCount = routerPaths().filter((p) => p !== "*").length;
    // Only the two legacy deep-link redirects should be literal.
    expect(literalCount).toBeLessThanOrEqual(2);
  });

  it("every registry route has a component", () => {
    const missing = ROUTES.map((r) => r.path).filter((p) => !COMPONENT_KEYS.has(p));
    expect(missing, "registry routes with no component would render a bridge").toEqual([]);
  });

  it("has no component for a path that is not registered", () => {
    const orphans = [...COMPONENT_KEYS].filter(
      (p) => !ROUTES.some((r) => r.path === p),
    );
    expect(orphans, "components wired to unregistered paths").toEqual([]);
  });

  it("never imports a legacy page as a product screen", () => {
    // Both forms: a static `from "../pages/X"` and the lazy
    // `import("../pages/Login")`. Only Login may remain -- it is not a product
    // screen, and an unauthenticated visitor should not download the app.
    const staticImports = [
      ...APP_SRC.matchAll(/from\s+"[^"]*\/pages\/([A-Za-z]+)"/g),
    ].map((m) => m[1]);
    const dynamicImports = [
      ...APP_SRC.matchAll(/import\(\s*"[^"]*\/pages\/([A-Za-z]+)"\s*\)/g),
    ].map((m) => m[1]);
    expect(
      [...staticImports, ...dynamicImports].sort(),
      "legacy pages must not be product screens",
    ).toEqual(["Login"]);
  });

  it("keeps pre-rebuild deep links working rather than 404ing", () => {
    expect(APP_SRC).toContain('path="/editor/:timelineId"');
    expect(APP_SRC).toContain("<Navigate");
  });
});

describe("rebuilt vs legacy-bridged", () => {
  it("Work 16.5.2 rebuilt every product screen", () => {
    // This is what makes "still a bridge" a FAILURE rather than a quiet
    // default. Replacing a screen must flip its state here.
    expect(
      LEGACY_ROUTES.map((r) => r.path),
      "still marked legacy; the replacement should flip these to REBUILT",
    ).toEqual([]);
    expect(REBUILT_ROUTES.length).toBe(ROUTES.length);
  });

  it("covers the whole target information architecture", () => {
    const paths = ROUTES.map((r) => r.path);
    for (const p of [
      "/", "/planner", "/calendar",
      "/projects", "/studio", "/campaigns",
      "/assets", "/brands",
      "/localization", "/ugc", "/distribution", "/community",
      "/analytics", "/experiments", "/memory", "/intelligence",
      "/operations", "/providers", "/settings",
    ]) {
      expect(paths, `${p} missing from the registry`).toContain(p);
    }
  });

  it("records only capabilities the backend actually declares", () => {
    // A typo'd capability would silently hide a control for everyone, and no
    // type would catch it. Read the backend vocabulary rather than trusting a
    // hand-copied list.
    const capsPy = readFileSync(
      join(REPO, "backend", "app", "services", "capabilities.py"),
      "utf-8",
    );
    const declared = new Set(
      [...capsPy.matchAll(/"([a-z]+\.[a-z_]+)":\s*ROLE_/g)].map((m) => m[1]),
    );
    expect(declared.size).toBeGreaterThanOrEqual(8);

    const unknown = ROUTES.map((r) => r.permission)
      .filter((p): p is string => Boolean(p))
      .filter((p) => !declared.has(p));
    expect(
      [...new Set(unknown)],
      "routes reference a capability the backend does not declare",
    ).toEqual([]);
  });

  it("gates the money-touching surfaces on the paid capabilities", () => {
    // A generic retry is forbidden where money or reach is at stake, so those
    // screens must be the ones gated on publish/provider capabilities.
    expect(ROUTES.find((r) => r.path === "/distribution")?.permission).toBe(
      "publish.approve",
    );
    expect(ROUTES.find((r) => r.path === "/providers")?.permission).toBe(
      "providers.manage",
    );
  });

  it("reports a truthful inventory", () => {
    const inv = routeInventory();
    expect(inv.total).toBe(ROUTES.length);
    expect(inv.rebuilt).toBe(REBUILT_ROUTES.length);
    expect(inv.legacy).toBe(LEGACY_ROUTES.length);
    expect(inv.rebuilt + inv.legacy).toBe(inv.total);
  });
});

describe("route smoke", () => {
  it("resolves every registered path back to itself", () => {
    for (const r of ROUTES) {
      const concrete = r.path.replace(/:[A-Za-z]+/g, "x");
      expect(routeAt(concrete), `${r.path} does not resolve to a route`).toBeDefined();
    }
  });

  it("resolves a nested path to the most specific route, not the list", () => {
    expect(routeAt("/projects/abc")?.path).toBe("/projects/:contentId");
    expect(routeAt("/projects")?.path).toBe("/projects");
    expect(routeAt("/campaigns/c1")?.path).toBe("/campaigns/:campaignId");
    expect(routeAt("/studio/t1")?.path).toBe("/studio/:timelineId");
    expect(routeAt("/")?.path).toBe("/");
  });

  it("resolves unknown paths to nothing rather than to a wrong page", () => {
    // A catch-all must not masquerade as a real feature.
    expect(routeAt("/definitely-not-a-route")).toBeUndefined();
  });

  it("builds a breadcrumb trail that ends on the current page", () => {
    const crumbs = breadcrumbsFor("/projects/abc");
    expect(crumbs[0].label).toBe("Command Center");
    expect(crumbs[crumbs.length - 1].label).toBeTruthy();
  });

  it("has a palette search that finds by intent, not only by label", () => {
    expect(searchRoutes("timeline").map((r) => r.path)).toContain("/studio");
    expect(searchRoutes("queue").map((r) => r.path)).toContain("/operations");
    expect(searchRoutes("maturity").map((r) => r.path)).toContain("/providers");
    expect(searchRoutes("").length).toBe(NAV_ROUTES.length);
    expect(searchRoutes("zzz-not-a-thing")).toEqual([]);
  });

  it("every nav route carries a permission field, even when null", () => {
    for (const r of NAV_ROUTES) {
      expect("permission" in r, `${r.path} has no permission field`).toBe(true);
    }
  });

  it("still marks bridged routes in the palette if any reappear", () => {
    // Vacuously true today. It stays so that reintroducing a bridge is visible
    // in the palette rather than silently indistinguishable from a rebuild.
    for (const r of NAV_ROUTES.filter((x) => x.state === "LEGACY_BRIDGE")) {
      expect(r.description).toBeTruthy();
    }
  });
});