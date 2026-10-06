/* Permission refusal vs generic failure (Work 16.5.3 §12/§16).
 *
 * §16 is explicit that "403 != empty state", and §12 that a denied action must
 * be communicated as a permission problem rather than a failure. Before this,
 * every denied read on all twenty-two routes fell through to `ErrorState`,
 * which:
 *
 *   - painted the DANGER tokens, telling the operator something was broken when
 *     the server had answered correctly;
 *   - rendered a RETRY button, which for a 403 can never succeed -- the one
 *     control guaranteed not to help.
 *
 * `PermissionAwareError` fixes that at the shared boundary, so all twenty-two
 * routes are covered by one change. These tests pin the behaviour and, just as
 * importantly, pin that a genuine 500 is STILL treated as retryable.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen } from "@testing-library/react";

import { PermissionAwareError } from "../design-system/primitives";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");
const PRIMITIVES = readFileSync(join(SRC, "design-system", "primitives.tsx"), "utf-8");
const CSS = readFileSync(join(SRC, "design-system", "styles.css"), "utf-8");

/*
 * THE REAL MESSAGES, NOT PLAUSIBLE-LOOKING ONES.
 *
 * This file previously asserted against `"Request failed with status 403:
 * insufficient role"` -- an axios-shaped string. This codebase's `ApiError` builds
 * its message from the server's `detail` ALONE (`lib/api.ts:83`), so it has never
 * produced that prefix and never carried a status in the text at all.
 *
 * That made the whole file quietly wrong: `isPermissionDenial`'s `/\b40[13]\b/`
 * branch was unreachable, the suite passed only via a wording fallback that
 * matched one of seven real 403 strings, and the most common one
 * ("not a workspace member") was never tested. The strings below are the ones
 * `scripts/denial_vocabulary.py` extracts from the backend source.
 */
const DENIAL = "not a workspace member";
const ROLE_DENIAL = "insufficient role";
const PROJECT_DENIAL = "insufficient project role";
const ADMIN_DENIAL = "overriding a QC FAIL requires the admin role";
const KEY_DENIAL = "API key not valid for this workspace";
const UNAUTHENTICATED = "not authenticated";
const SERVER_ERROR = "internal error";
const OFFLINE = "Network request failed";

/** Every distinct 403/401 detail the backend emits, as of Work 16.5.7. */
const ALL_DENIAL_DETAILS = [
  DENIAL,
  ROLE_DENIAL,
  PROJECT_DENIAL,
  ADMIN_DENIAL,
  KEY_DENIAL,
  "browser intelligence is disabled for this workspace",
  "LOCAL_ONLY privacy mode blocks remote browser fetch",
  UNAUTHENTICATED,
  "invalid or revoked API key",
  "invalid refresh token",
];

describe("§16 a permission refusal is its own state", () => {
  it("renders a 403 as PERMISSION DENIED, not a load failure", () => {
    render(<PermissionAwareError message={DENIAL} />);
    expect(screen.getByText("Permission denied")).toBeTruthy();
    expect(screen.queryByText("Could not load")).toBeNull();
  });

  it("says nothing is missing, so it cannot read as an empty dataset", () => {
    render(<PermissionAwareError message={DENIAL} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/nothing is missing/i);
    expect(alert.textContent).not.toMatch(/no data|empty|none found/i);
  });

  it("shows the server's own reason verbatim", () => {
    render(<PermissionAwareError message={DENIAL} />);
    expect(screen.getByText(DENIAL)).toBeTruthy();
  });

  it("offers NO retry, because retrying a 403 cannot succeed", () => {
    const onRetry = () => {};
    render(<PermissionAwareError message={DENIAL} onRetry={onRetry} />);
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("uses the warning tokens, not the danger ones", () => {
    render(<PermissionAwareError message={DENIAL} />);
    const alert = screen.getByRole("alert");
    expect(alert.className).toContain("ym-error-state--denied");
    // Danger styling would report an outage where there is none.
    expect(CSS).toMatch(/\.ym-error-state--denied\s*\{[^}]*status-warning-border/);
  });

  it("is announced to assistive technology", () => {
    render(<PermissionAwareError message={DENIAL} />);
    expect(screen.getByRole("alert")).toBeTruthy();
  });
});

/*
 * THE REGRESSION THAT MATTERED (Work 16.5.7)
 *
 * Four of the backend's seven 403 details matched nothing, so those refusals
 * rendered as a generic failure WITH a Retry that could never succeed. Measured
 * on the live app by `e2e/route-matrix.spec.ts`: `/settings`, `/providers`,
 * `/operations` and `/campaigns/:campaignId` all swallowed refusals this way.
 */
describe("§16 every real backend denial is recognised, not just one", () => {
  it.each(ALL_DENIAL_DETAILS)("recognises %j as a refusal with no retry", (detail) => {
    render(<PermissionAwareError message={detail} onRetry={() => {}} />);
    expect(screen.queryByText("Could not load")).toBeNull();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("still offers retry for a genuine failure and for offline", () => {
    const onRetry = () => {};
    render(<PermissionAwareError message={SERVER_ERROR} onRetry={onRetry} />);
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });
});

/*
 * THE STATUS IS AUTHORITATIVE, NOT THE PROSE.
 *
 * Both directions are asserted. Without the second one, "the regex got longer"
 * would pass this file while the root cause -- a discarded `ApiError.status` --
 * stayed unfixed and the next new backend wording would break it again.
 */
describe("§16 the HTTP status decides, and it beats the wording", () => {
  it("a 403 is a refusal even when the wording looks like a crash", () => {
    render(<PermissionAwareError message="upstream connect error" status={403} onRetry={() => {}} />);
    expect(screen.getByText("Permission denied")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("a 500 is NOT a refusal even when its text contains 'forbidden'", () => {
    render(
      <PermissionAwareError
        message="upstream returned forbidden for a proxy reason"
        status={500}
        onRetry={() => {}}
      />,
    );
    expect(screen.queryByText("Permission denied")).toBeNull();
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });

  it("a 401 says 'Signed out', not 'ask a workspace admin'", () => {
    render(<PermissionAwareError message={UNAUTHENTICATED} status={401} />);
    expect(screen.getByText("Signed out")).toBeTruthy();
    // The old copy sent users with a dead session to the wrong person.
    expect(screen.queryByText(/ask a workspace admin/i)).toBeNull();
  });

  it("a 403 does say 'ask a workspace admin'", () => {
    render(<PermissionAwareError message={DENIAL} status={403} />);
    expect(screen.getByText(/ask a workspace admin/i)).toBeTruthy();
  });
});

describe("§16 a genuine failure is still retryable", () => {
  it("keeps the retry affordance for a 500", () => {
    const onRetry = () => {};
    render(<PermissionAwareError message={SERVER_ERROR} onRetry={onRetry} />);
    expect(screen.getByText("Could not load")).toBeTruthy();
    const button = screen.getByRole("button", { name: /retry/i });
    expect(button).toBeTruthy();
  });

  it("keeps the retry affordance for a network failure", () => {
    const onRetry = () => {};
    render(<PermissionAwareError message={OFFLINE} onRetry={onRetry} />);
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });

  it("treats 401 as a denial too", () => {
    render(<PermissionAwareError message="Request failed with status 401" />);
    expect(screen.getByText("Permission denied")).toBeTruthy();
  });

  it("explains a refused REFRESH while still showing the last good data", () => {
    render(<PermissionAwareError message={DENIAL} stale />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/refresh was refused, not the load/i);
  });
});

describe("§16 the shared boundary applies this to every route", () => {
  it("QueryBoundary routes its errors through PermissionAwareError", () => {
    // One change covers all twenty-two routes, because they all render through
    // QueryBoundary. If this reverts, every screen silently loses the state.
    const boundary = PRIMITIVES.slice(
      PRIMITIVES.indexOf("export function QueryBoundary"),
      PRIMITIVES.indexOf("export function QueryBoundary") + 2400,
    );
    expect(boundary).toContain("PermissionAwareError");
    expect(boundary).not.toContain("<ErrorState");
  });

  it("keeps ErrorState itself unchanged for callers that want it", () => {
    // PermissionAwareError is additive: a screen that deliberately wants the
    // generic state can still use ErrorState directly.
    expect(PRIMITIVES).toContain("export function ErrorState(");
  });
});