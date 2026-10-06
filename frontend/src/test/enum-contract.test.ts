/* Enum vocabulary contract (Work 16.5.1 §11, "enum vocabulary").
 *
 * The OpenAPI baseline cannot supply this: only 11 of its 139 component
 * schemas declare an enum, and NONE of them are the vocabularies this UI
 * depends on (opportunity basis, publication/variant status, job state). The
 * authoritative declarations live in the backend source -- `planning.py` and
 * `campaign.py` -- so that is where the truth is read from.
 *
 * The direction that matters is FRONTEND-INVENTED STATE. If a screen compares
 * against "AWAITING_PUBLISH" and the backend only ever says "AWAITING_HANDOFF",
 * that branch is dead code that will silently never render. That is the drift
 * this file exists to catch.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const REPO = resolve(SRC, "..", "..");
const BACKEND = join(REPO, "backend", "app");

function walk(dir: string, exts: string[]): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full, exts));
    else if (exts.some((e) => full.endsWith(e))) out.push(full);
  }
  return out;
}

/** All backend Python source, as one searchable blob. */
const BACKEND_PY = walk(BACKEND, [".py"])
  .map((f) => readFileSync(f, "utf-8"))
  .join("\n");

function backendFile(rel: string): string {
  const p = join(BACKEND, ...rel.split("/"));
  if (!existsSync(p)) throw new Error(`backend source missing: ${rel}`);
  return readFileSync(p, "utf-8");
}

/** `NAME = "VALUE"` assignments. */
function pyConstants(src: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of src.matchAll(/^([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"/gm)) out[m[1]] = m[2];
  return out;
}

/** `NAME = ("A", "B", ...)` tuples. */
function pyTuples(src: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const m of src.matchAll(/^([A-Z][A-Z0-9_]*)\s*=\s*\(([^)]*)\)/gm)) {
    out[m[1]] = [...m[2].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  }
  return out;
}

/* =========================================================================
 * The opportunity basis vocabulary -- the planner's central correctness claim
 * ====================================================================== */

describe("enum contract: opportunity basis", () => {
  const planning = pyConstants(backendFile("models/planning.py"));
  const BACKEND_BASIS = ["OBSERVED", "INFERRED", "RECOMMENDED"].filter((b) =>
    planning[b] !== undefined,
  );

  it("the backend declares exactly the three bases the planner documents", () => {
    // Not an equality assertion against a hardcoded list -- if the backend ever
    // drops RECOMMENDED, this fails rather than the UI quietly losing a lane.
    expect(planning.OBSERVED).toBe("OBSERVED");
    expect(planning.INFERRED).toBe("INFERRED");
    expect(planning.RECOMMENDED).toBe("RECOMMENDED");
    expect(BACKEND_BASIS).toEqual(["OBSERVED", "INFERRED", "RECOMMENDED"]);
  });

  it("the planner renders all three, and renders no fourth", () => {
    const planner = readFileSync(join(SRC, "features", "planner", "Planner.tsx"), "utf-8");

    for (const basis of BACKEND_BASIS) {
      expect(
        planner.includes(`"${basis}"`),
        `Planner.tsx never mentions the backend basis ${basis}`,
      ).toBe(true);
    }

    // Any other ALL-CAPS token used in a basis comparison must be real.
    const basisComparisons = [
      ...planner.matchAll(/basis\s*===\s*"([A-Z_]+)"/g),
      ...planner.matchAll(/BASIS_MEANING\[\s*"([A-Z_]+)"\s*\]/g),
    ].map((m) => m[1]);
    for (const token of new Set(basisComparisons)) {
      expect(
        BACKEND_BASIS.includes(token),
        `Planner.tsx compares basis against "${token}", which the backend never declares`,
      ).toBe(true);
    }
  });

  it("freshness states in the UI match the backend's planning vocabulary", () => {
    const planning = pyConstants(backendFile("models/planning.py"));
    const planner = readFileSync(join(SRC, "features", "planner", "Planner.tsx"), "utf-8");
    const FRESHNESS = ["FRESH", "AGING", "STALE"];
    for (const f of FRESHNESS) expect(planning[f]).toBe(f);
    for (const f of FRESHNESS) {
      if (planner.includes(`"${f}"`)) {
        expect(planning[f], `UI uses ${f} but the backend dropped it`).toBe(f);
      }
    }
  });
});

/* =========================================================================
 * Publication / variant status -- the LIVE / MOCK / HANDOFF requirement
 * ====================================================================== */

describe("enum contract: campaign variant status", () => {
  const campaignPy = backendFile("models/campaign.py");
  const variants = pyTuples(campaignPy).VARIANT_STATUSES ?? [];
  const plans = pyTuples(campaignPy).PLAN_STATUSES ?? [];

  it("reads a non-trivial variant vocabulary from the backend", () => {
    expect(variants.length).toBeGreaterThanOrEqual(5);
    expect(plans.length).toBeGreaterThanOrEqual(4);
  });

  it("AWAITING_HANDOFF exists in the backend and is not PUBLISHED", () => {
    // Work 14: media prepared, a human still has to publish. Deliberately not
    // PUBLISHED, so nothing downstream mistakes a handoff for a live post.
    expect(variants).toContain("AWAITING_HANDOFF");
    expect(variants).not.toContain("PUBLISHED_HANDOFF");
  });

  it("no rebuilt screen compares against a variant status the backend lacks", () => {
    const files = [
      ...walk(join(SRC, "features"), [".tsx"]),
      ...walk(join(SRC, "design-system"), [".tsx", ".ts"]),
    ];
    const vocabulary = new Set([...variants, ...plans]);

    const offenders: string[] = [];
    for (const f of files) {
      const src = readFileSync(f, "utf-8");
      const rel = f.replace(`${SRC}\\`, "");
      for (const m of src.matchAll(
        /(?:status|state)\s*(?:===|!==|\.includes\()\s*\(?\s*"([A-Z][A-Z0-9_]{2,})"/g,
      )) {
        const token = m[1];
        if (!vocabulary.has(token) && !BACKEND_PY.includes(token)) {
          offenders.push(`${rel}: "${token}"`);
        }
      }
    }
    expect(offenders, "frontend states with no backend declaration").toEqual([]);
  });
});

/* =========================================================================
 * Broad sweep: no invented ALL-CAPS state tokens
 * ====================================================================== */

describe("enum contract: no invented state tokens", () => {
  const files = [
    ...walk(join(SRC, "features"), [".tsx"]),
    ...walk(join(SRC, "design-system"), [".tsx", ".ts"]),
    ...walk(join(SRC, "app"), [".tsx"]),
  ].filter((f) => !/\.test\.(ts|tsx)$/.test(f));

  /** Tokens used in a comparison or a status array, which imply a real state. */
  const STATE_TOKENS =
    /(?:\.includes\(\s*|\s(?:===|!==)\s*|\bcase\s)\s*"([A-Z][A-Z0-9_]{2,})"/g;

  const collected = files.flatMap((f) => {
    const src = readFileSync(f, "utf-8");
    const rel = f.replace(`${SRC}\\`, "");
    return [...src.matchAll(STATE_TOKENS)].map((m) => ({
      rel,
      token: m[1],
    }));
  });

  it("the sweep actually inspected the rebuilt screens", () => {
    expect(files.length).toBeGreaterThan(8);
    expect(collected.length).toBeGreaterThan(5);
  });

  it("every state token compared against exists in the backend source", () => {
    // These are UI-internal vocabularies, deliberately not backend state.
    // Declared here with their reason so the allowlist cannot silently grow
    // into a hole -- a new entry is a visible act, not an oversight.
    const FRONTEND_INTERNAL: Record<string, string> = {
      SUCCESS: "design-system tone for a provider check that passed",
      HEALTHY: "design-system tone for a healthy provider; not a backend state",
      LEGACY_BRIDGE: "route-registry RouteState; Work 16.5.2 left no bridges",
      INTERPRETATION:
        "Memory's own evidence-vs-derivation classification, computed by " +
        "classifyMemory() from backend memory types; the backend stores no such field",
    };

    const unknown = [
      ...new Map(
        collected
          .filter((c) => !BACKEND_PY.includes(c.token))
          .map((c) => [c.token, c.rel]),
      ),
    ];

    // eslint-disable-next-line no-console
    console.log(
      `[enum] ${collected.length} state-token comparisons inspected; ` +
        `${unknown.length} not in backend/app (${unknown
          .map(([t]) => (FRONTEND_INTERNAL[t] ? `${t}=frontend-internal` : t))
          .join(", ") || "none"})`,
    );

    const unjustified = unknown
      .filter(([t]) => !(t in FRONTEND_INTERNAL))
      .map(([t, r]) => `${t} in ${r}`);

    expect(
      unjustified,
      "these ALL-CAPS tokens are compared against but declared neither in " +
        "backend/app nor in the documented frontend-internal allowlist",
    ).toEqual([]);
  });
});