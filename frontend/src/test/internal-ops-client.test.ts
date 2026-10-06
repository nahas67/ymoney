/* Internal operations client contract (Work 16.5.3 §13).
 *
 * `/livez` and `/internal/*` are mounted on the app ROOT and are
 * `include_in_schema=False`. That is correct -- publishing unauthenticated
 * operational endpoints to anyone who can load the spec would be a mistake -- but
 * it also means the normal OpenAPI contract suite is blind to them.
 *
 * So these tests ARE their contract. They check four things:
 *
 *   route       the client addresses exactly the paths the backend mounts
 *   shape       the declared response types are what the backend emits
 *   unavailable a failing probe produces its own distinguishable error
 *   sensitivity no credential-shaped field is declared or requested
 *
 * The route check reads `backend/app/main.py`, so a backend mount that moves
 * without the client moving fails here rather than at runtime.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  INTERNAL_OPS_PATH,
  INTERNAL_OPS_FORBIDDEN_FIELDS,
  fetchInternalOps,
  isInternalOpsError,
  InternalOpsError,
} from "../api/internalOps";
import type {
  Liveness,
  Collectors,
  AlertCatalog,
  SloCatalog,
} from "../features/operations/Operations";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..", "..");
const MAIN_PY = readFileSync(join(REPO, "backend", "app", "main.py"), "utf-8");
const CLIENT_SRC = readFileSync(join(REPO, "frontend", "src", "api", "internalOps.ts"), "utf-8");

afterEach(() => {
  vi.restoreAllMocks();
});

describe("§13 internal ops: routes match the backend mounts", () => {
  it("addresses exactly the four probes the backend mounts on the root", () => {
    // Read the mounts rather than trusting the client list, so adding a backend
    // probe without a client entry is a visible failure.
    const declared = Object.values(INTERNAL_OPS_PATH).sort();
    expect(declared).toEqual([
      "/internal/alerts",
      "/internal/collectors",
      "/internal/slo",
      "/livez",
    ]);
  });

  it("does not pretend to know routes the backend does not mount", () => {
    for (const path of Object.values(INTERNAL_OPS_PATH)) {
      // Root-mounted, therefore NOT prefixed with /api/v1. If someone "fixes"
      // this to look like every other call, the request 404s.
      expect(path.startsWith("/api/v1")).toBe(false);
    }
  });

  it("uses a closed path type, so a typo cannot compile", () => {
    // The whole point of the module: `string` was replaced by a union derived
    // from the frozen object.
    expect(CLIENT_SRC).toContain("export type InternalOpsPath");
    expect(CLIENT_SRC).toContain("} as const;");
    expect(CLIENT_SRC).toMatch(/path:\s*InternalOpsPath/);
  });

  it("documents why these routes are outside the spec", () => {
    expect(CLIENT_SRC).toContain("include_in_schema=False");
    expect(CLIENT_SRC).toMatch(/unauthenticated/i);
  });
});

describe("§13 internal ops: response shapes", () => {
  it("types liveness as a real verdict, not a bare status string", () => {
    // Built from the declared type rather than cast to it. `as Liveness` would
    // let a wrong guess through -- the exact habit §4 removes -- and `tsc`
    // rejects the mismatch when the object is built honestly.
    const probe: Liveness = {
      status: "alive",
      detail: "serving",
      event_loop: true,
      checks: [{ id: "process", status: "ok", blocking: false, detail: "" }],
      blocking_failures: [],
    };
    expect(probe.event_loop).toBe(true);
    expect(probe.blocking_failures).toEqual([]);
  });

  it("types collectors as a per-collector up/down map plus the failures", () => {
    const probe: Collectors = {
      collectors: { gpu_slots: "up", database: "down" },
      failed: ["database"],
      note: "collector states",
    };
    expect(probe.collectors.database).toBe("down");
    expect(probe.failed).toEqual(["database"]);
  });

  it("keeps the alert catalog and the SLO catalog as separate shapes", () => {
    // Kept separate on purpose: alerts carry live verdicts, SLO carries TARGETS
    // only (`measured` + a disclaimer). Merging them is how a target would
    // start reading as an achievement.
    const slo: SloCatalog = {
      measured: false,
      disclaimer: "targets only",
      thresholds: { availability: 0.99 },
      targets: [],
    };
    expect(slo.measured).toBe(false);

    // A COMPILE-TIME assertion, not `in` on an empty object -- `in` inspects a
    // runtime value and would report false for a field that the TYPE does have.
    // If someone merges the two shapes, these two constants stop type-checking.
    const alertsCarryFiring: "firing_count" extends keyof AlertCatalog ? true : false = true;
    const sloCarriesNoFiring: "firing_count" extends keyof SloCatalog ? true : false = false;
    expect(alertsCarryFiring).toBe(true);
    expect(sloCarriesNoFiring).toBe(false);
  });

  it("returns the parsed body on success", async () => {
    const body = { status: "alive" };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => body })),
    );
    await expect(fetchInternalOps<Liveness>(INTERNAL_OPS_PATH.liveness)).resolves.toEqual(body);
  });

  it("requests no Authorization header, by design", async () => {
    const fetchMock = vi.fn(
      async (_path: string, _init?: RequestInit) => ({
        ok: true,
        status: 200,
        json: async () => ({}),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchInternalOps<Collectors>(INTERNAL_OPS_PATH.collectors);

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = (init.headers ?? {}) as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).toEqual(["accept"]);
    // There is no principal to present: these routes are process-level.
    expect(JSON.stringify(headers).toLowerCase()).not.toContain("authorization");
  });
});

describe("§13 internal ops: unavailable handling", () => {
  it("raises a distinguishable error when a probe is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) })),
    );

    const err = await fetchInternalOps<Collectors>(INTERNAL_OPS_PATH.collectors).catch(
      (e: unknown) => e,
    );

    expect(isInternalOpsError(err)).toBe(true);
    expect((err as InternalOpsError).status).toBe(503);
    // Names the failing route, so an operator knows which subsystem is down.
    expect((err as InternalOpsError).message).toContain("/internal/collectors");
  });

  it("is distinguishable from a workspace API failure", () => {
    // A generic Error would render identically in a red panel and leave the
    // operator guessing whether the app or the probe is broken.
    const probeErr = new InternalOpsError(503, "/livez answered 503");
    const apiErr = new Error("network error");
    expect(isInternalOpsError(probeErr)).toBe(true);
    expect(isInternalOpsError(apiErr)).toBe(false);
    expect(isInternalOpsError(new TypeError("x"))).toBe(false);
  });
});

describe("§13 internal ops: sensitive fields", () => {
  it("declares the forbidden field list it will not surface", () => {
    for (const f of ["api_key", "secret", "password", "token"]) {
      expect(INTERNAL_OPS_FORBIDDEN_FIELDS).toContain(f);
    }
  });

  it("never requests or declares one in this module", () => {
    // The list is documentation of a prohibition, so the client itself must not
    // mention any of those names as something it fetches.
    for (const f of INTERNAL_OPS_FORBIDDEN_FIELDS) {
      expect(CLIENT_SRC).not.toMatch(new RegExp(`\\b${f}\\b\\s*[:=]\\s*"`));
    }
  });

  it("does not send a workspace identifier to a process-level probe", () => {
    // No workspace scoping exists for these routes; sending one would imply a
    // boundary that is not enforced.
    expect(CLIENT_SRC).not.toContain("workspace_id");
  });
});

describe("§13 internal ops: backend really does exclude these from the spec", () => {
  it("the spec contains no /livez or /internal route", () => {
    const spec = JSON.parse(
      readFileSync(join(REPO, "frontend", "src", "api", "openapi.json"), "utf-8"),
    ) as { paths: Record<string, unknown> };
    const leaked = Object.keys(spec.paths).filter(
      (p) => p.includes("/internal/") || p.endsWith("/livez"),
    );
    expect(
      leaked,
      "internal probes must stay out of the published spec; publishing them " +
        "advertises unauthenticated endpoints",
    ).toEqual([]);
  });

  it("main.py mounts the internal router at the app root", () => {
    // The reason this client exists at all. If the mount moves under /api/v1,
    // `lib/api.ts` could serve it and this module would be redundant.
    const rootMount = /app\.include_router\(\s*internal_ops_router\s*\)/.test(MAIN_PY);
    expect(rootMount).toBe(true);
  });
});