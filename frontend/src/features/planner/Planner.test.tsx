/** @vitest-environment jsdom */
/* Planner and Editorial Calendar — the three states that must stay distinct.
 *
 * The planner is where the product makes its honesty claims: OBSERVED is
 * evidence, INFERRED is derivation, RECOMMENDED is a suggestion with no
 * measurement behind it. A screen that renders those identically, or that shows
 * a count of 0 when the endpoint is simply dead, destroys the only signal an
 * operator has.
 *
 * So the assertion that matters is the DISTINCTION between:
 *   - the endpoint failed        -> an alert naming the failure
 *   - the endpoint returned empty -> no alert, an empty state
 * A test that only ever renders the happy path cannot tell those apart, which is
 * precisely the bug this file exists to catch.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const wsGet = vi.fn();

vi.mock("../../lib/api", () => ({
  // queries.ts imports ApiError to decide whether a status is ignorable, so a
  // mock that omits it fails at the error path instead of the test path.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: { get: (path: string) => wsGet(path) },
  api: vi.fn(),
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: [],
    switchWorkspace: () => {},
    reload: () => {},
  }),
}));

import Planner from "./Planner";
import CalendarView from "./CalendarView";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
});

/** Plausible empty payloads. Unknown paths resolve to `{}` so an unmocked read
 *  cannot crash a screen on `payload.items.length`. */
function route(path: string): unknown {
  if (path.startsWith("/planner/opportunities")) return { items: [] };
  if (path.startsWith("/opportunities")) return { items: [] };
  if (path.startsWith("/planner/signals")) return { items: [] };
  if (path.startsWith("/planner/calendar")) {
    return { items: [], committed: {}, remaining: {}, capacity: { declared: false } };
  }
  if (path.startsWith("/planner/plans")) return { items: [] };
  if (path.startsWith("/planner/policy")) return {};
  if (path.startsWith("/inbox/autonomy")) return {};
  if (path === "/calendar") return { items: [] };
  if (path.startsWith("/calendar/best-times")) return { measured: false, items: [] };
  if (path.startsWith("/calendar/response-windows")) return { items: [] };
  if (path.startsWith("/campaigns")) return { items: [] };
  return {};
}

function serveEmpty() {
  wsGet.mockImplementation((path: string) => Promise.resolve(route(path)));
}

function failPaths(predicate: (p: string) => boolean, message: string) {
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(route(path)),
  );
}

function renderPlanner() {
  return render(
    <MemoryRouter>
      <Planner />
    </MemoryRouter>,
  );
}

function renderCalendar() {
  return render(
    <MemoryRouter>
      <CalendarView />
    </MemoryRouter>,
  );
}

describe("Planner renders its three states apart", () => {
  it("renders without crashing when every endpoint returns empty", async () => {
    serveEmpty();
    renderPlanner();
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
    // Settled: no alert, and the planner's own chrome is on screen.
    await waitFor(() => {
      expect(document.body.textContent?.length ?? 0).toBeGreaterThan(0);
    });
  });

  it("surfaces a failed opportunities read as an alert, not as an empty list", async () => {
    failPaths((p) => p.startsWith("/planner/opportunities"), "planner backend unavailable");
    renderPlanner();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/planner backend unavailable/i);
  });

  it("keeps failure distinguishable from emptiness", async () => {
    // Empty first: no alert anywhere.
    serveEmpty();
    const { unmount } = renderPlanner();
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    unmount();
    cleanup();

    // Then failed: an alert appears. Same screen, different truth.
    failPaths((p) => p.startsWith("/planner/opportunities"), "planner backend unavailable");
    renderPlanner();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});

describe("Editorial Calendar renders its three states apart", () => {
  it("renders without crashing when the schedule is empty", async () => {
    serveEmpty();
    renderCalendar();
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
  });

  it("reports a failed calendar read as an alert", async () => {
    failPaths((p) => p === "/calendar", "calendar backend unavailable");
    renderCalendar();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/calendar backend unavailable/i);
  });

  it("does not fabricate an empty calendar when the schedule read fails", async () => {
    failPaths((p) => p === "/calendar", "calendar backend unavailable");
    renderCalendar();
    await screen.findByRole("alert");
    // "No entries scheduled" would be a lie: the schedule was never read.
    expect(document.body.textContent).not.toMatch(/no entries scheduled/i);
  });
});