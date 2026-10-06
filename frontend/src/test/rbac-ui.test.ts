/* RBAC / capability tests (Work 16.5.2 §2).
 *
 * Work 16.5.1 established that `/auth/me` reported NO role and NO capabilities,
 * and that the UI therefore had to fail open because there was nothing truthful
 * to gate on. That gap is now closed at the source:
 *
 *   backend/app/services/capabilities.py  -- the canonical table
 *   backend/app/api/v1/auth.py            -- /auth/me reports it per workspace
 *   frontend/src/state/session.tsx        -- can() / blockedReason() consume it
 *
 * These tests pin the NEW contract. The critical property is the one that is easy
 * to get wrong in a way that either locks users out or pretends to protect them:
 *
 *   capabilities REPORTED  -> enforce (an empty list is a real answer)
 *   capabilities ABSENT     -> allow   (older backend; the server still 403s)
 *
 * A capability is AFFORDANCE. It is never authorization, and no test here may be
 * read as evidence that possessing one grants access.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const REPO = resolve(SRC, "..", "..");

const SESSION = readFileSync(join(SRC, "state", "session.tsx"), "utf-8");
const SHELL = readFileSync(join(SRC, "app", "AppShell.tsx"), "utf-8");
const CAPS_PY = readFileSync(
  join(REPO, "backend", "app", "services", "capabilities.py"),
  "utf-8",
);
const AUTH_PY = readFileSync(join(REPO, "backend", "app", "api", "v1", "auth.py"), "utf-8");

const REQUESTED_VOCABULARY = [
  "content.read",
  "content.write",
  "publish.approve",
  "publish.execute",
  "providers.manage",
  "operations.view",
  "brand.manage",
  "reviews.approve",
];

describe("RBAC: the backend publishes a canonical capability contract", () => {
  it("declares the agreed vocabulary", () => {
    for (const cap of REQUESTED_VOCABULARY) {
      expect(CAPS_PY, `${cap} is not declared in capabilities.py`).toContain(`"${cap}"`);
    }
  });

  it("each capability carries a minimum role", () => {
    expect(CAPS_PY).toMatch(/_MINIMUM_ROLE:\s*dict\[str,\s*str\]/);
    for (const role of ["ROLE_VIEWER", "ROLE_MEMBER", "ROLE_ADMIN", "ROLE_OWNER"]) {
      expect(CAPS_PY).toContain(`${role} =`);
    }
  });

  it("marks the paid capability, so no generic retry can be built on it", () => {
    expect(CAPS_PY).toContain("PAID_CAPABILITIES");
    expect(CAPS_PY).toContain('"publish.execute"');
  });

  it("fails CLOSED on an unknown role", () => {
    expect(CAPS_PY).toMatch(/ROLE_ORDER\.get\(str\(role\)\.strip\(\),\s*-1\)/);
  });

  it("/auth/me reports role and capabilities per workspace", () => {
    expect(AUTH_PY).toContain("response_model=MeResponse");
    expect(AUTH_PY).toContain("capabilities_for_role");
    // Per workspace, not only a union: acting inside one workspace needs that
    // workspace's permissions.
    expect(AUTH_PY).toMatch(/workspaces\s*=\s*\[/);
    expect(AUTH_PY).toContain("role=str(role)");
  });

  it("keeps the payload additive so existing callers survive", () => {
    // Editor.tsx / Exports.tsx / Reviews.tsx read id and is_superuser.
    for (const key of ["id", "email", "display_name", "is_superuser", "workspaces"]) {
      expect(CAPS_PY, `MeResponse lost ${key}`).toContain(key);
    }
  });
});

describe("RBAC: the client enforces what the server reported", () => {
  it("distinguishes 'none' from 'not reported'", () => {
    // The single most important line in the client. Collapsing these two cases
    // either hides every control (older backend) or shows every control to a
    // viewer (a viewer really does get an empty list).
    expect(SESSION).toContain("capabilitiesKnown");
    expect(SESSION).toMatch(
      /const capabilitiesKnown = Array\.isArray\(caps\)/,
    );
  });

  it("enforces when the server reported, and allows when it did not", () => {
    expect(SESSION).toMatch(
      /if \(!capabilitiesKnown\) return true;\s*\n\s*return capabilities\.includes\(permission\);/,
    );
  });

  it("can() is documented as affordance, never authorization", () => {
    expect(SESSION).toMatch(/NEVER authorization/);
    // Both branches must be explained where the decision is made, so the next
    // reader does not "simplify" the distinction away.
    expect(SESSION).toMatch(/older backend/i);
    expect(SESSION).toMatch(/403/);
  });

  it("offers an explanation for a blocked action", () => {
    expect(SESSION).toContain("blockedReason");
    expect(SESSION).toMatch(/does not include/);
  });

  it("does not branch on the raw role string anywhere in the client", () => {
    // The client must branch on capabilities, not on role names, or adding a role
    // silently changes behaviour in the UI but not on the server.
    expect(SESSION).not.toMatch(/\.role\s*===\s*"/);
    expect(SESSION).not.toMatch(/role\s*===\s*"(owner|admin|member|viewer)"/i);
  });
});

describe("RBAC: gating is affordance only", () => {
  it("never filters navigation by capability", () => {
    // Hiding a nav entry would make a permissioned area look like a missing
    // feature. Routes stay; the server answers.
    expect(SHELL).not.toMatch(/\.filter\([^)]*capabilit/);
    expect(SHELL).not.toMatch(/capabilities\.includes/);
  });

  it("never treats a hidden control as the enforcement point", () => {
    // If any code claimed a capability grant authorises a request, this fails.
    expect(SESSION).not.toMatch(/capabilit\w*\s*\)?\s*(\/\/|\*)\s*(authoris|authoriz|grant|enforce)/i);
  });
});
