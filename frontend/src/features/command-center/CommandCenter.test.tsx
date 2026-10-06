/** @vitest-environment jsdom */
/* Command Center — the three states every panel must be able to tell apart.
 *
 * These tests are about honesty, not layout: a dashboard that renders 0 for
 * "the endpoint is dead" is the failure mode this screen is built to avoid, so
 * the failing-endpoint case is asserted as strictly as the loaded one.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const wsGet = vi.fn();
const sysApi = vi.fn();

vi.mock("../../lib/api", () => ({
  // queries.ts reads ApiError to decide whether a status is ignorable, so the
  // mock has to carry it: a mock that omits an export fails at the error path.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: {
    get: (path: string) => wsGet(path),
  },
  api: (method: string, path: string) => sysApi(method, path),
  videoFileUrl: (id: string) => `/video/${id}.mp4`,
  videoThumbUrl: (id: string) => `/thumb/${id}.jpg`,
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: [],
  }),
}));

import { CommandCenter } from "./CommandCenter";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  sysApi.mockReset();
});

const EMPTY_PAYLOADS: Record<string, unknown> = {
  "/campaigns": {
    items: [
      {
        id: "c-1",
        name: "Spring push",
        goal: "ship 3 shorts",
        status: "ACTIVE",
        target_videos: 3,
        videos_per_day: 1,
        platforms: ["youtube"],
        automation_level: "AUTONOMOUS",
        budget_daily_usd: 5,
        starts_at: "2026-01-02T09:00:00Z",
        ends_at: null,
      },
    ],
  },
  "/content?limit=200": { total: 0, items: [] },
  "/calendar": { items: [] },
  "/jobs?limit=100": { items: [] },
  "/costs": {
    last_24h_by_category: {},
    spent_last_24h_usd: 0.25,
    daily_budget_usd: 20,
    per_video_budget_usd: 0.4,
    within_budget: true,
    remaining_usd: 19.75,
  },
  "/intelligence/routing/chains": { chains: [], note: "in-process and bounded" },
  "/planner/opportunities": {
    count: 0,
    opportunities: [],
    forbidden_claims: [],
    note: "the planner never asserts virality",
  },
  "/inbox/opportunities?limit=50": { items: [] },
  "/provider-maturity/incidents?limit=50": {
    items: [],
    count: 0,
    unknown_exposure_count: 0,
    states: [],
    note: "SUBMISSION_UNKNOWN is never shown as FAILED",
  },
  "/reviews": { items: [] },
  "/publishing/posts?limit=25": { items: [] },
};

function resolveAll(overrides: Record<string, unknown> = {}) {
  const payloads = { ...EMPTY_PAYLOADS, ...overrides };
  wsGet.mockImplementation((path: string) => {
    const key = Object.keys(payloads).find((k) => path.startsWith(k.split("?")[0]) && (path.includes("?") === k.includes("?")));
    const found = key ?? Object.keys(payloads).find((k) => path.startsWith(k));
    return Promise.resolve(found ? payloads[found] : {});
  });
  sysApi.mockImplementation(() =>
    Promise.resolve({
      status: "ready",
      checked_at: "2026-03-01T10:00:00Z",
      checks: [],
      blocking_failures: [],
      message: "All production dependencies verified.",
    }),
  );
}

function renderCenter() {
  return render(
    <MemoryRouter>
      <CommandCenter />
    </MemoryRouter>,
  );
}

describe("CommandCenter", () => {
  it("renders a loading skeleton while the panels are in flight", () => {
    const pending = new Promise(() => undefined);
    wsGet.mockImplementation(() => pending);
    sysApi.mockImplementation(() => pending);

    const { container } = renderCenter();

    expect(container.querySelector(".ym-skeleton")).toBeTruthy();
    expect(screen.getByText("Command Center")).toBeInTheDocument();
    expect(screen.getAllByText("Active campaigns").length).toBeGreaterThan(0);
    expect(screen.queryByText("Spring push")).toBeNull();
  });

  it("renders loaded panels and says why an empty one is empty", async () => {
    resolveAll();
    renderCenter();

    expect(await screen.findByText("Spring push")).toBeInTheDocument();
    expect(screen.getByText("Nothing scheduled")).toBeInTheDocument();
    expect(screen.getByText("No job is queued, running, waiting or retrying")).toBeInTheDocument();
    expect(screen.getByText("No paid incident is open")).toBeInTheDocument();
    expect(screen.getByText("No review is waiting on a human")).toBeInTheDocument();
    expect(screen.getByText("Nothing published yet")).toBeInTheDocument();
  });

  it("renders UNAVAILABLE, never 0, when a panel's endpoint fails", async () => {
    resolveAll();
    wsGet.mockImplementation((path: string) => {
      if (path.startsWith("/costs")) return Promise.reject(new Error("500 internal error"));
      if (path.startsWith("/provider-maturity/incidents")) return Promise.reject(new Error("503 unavailable"));
      if (path.startsWith("/jobs")) return Promise.reject(new Error("404 not found"));
      const found = Object.keys(EMPTY_PAYLOADS).find((k) => path.startsWith(k.split("?")[0]));
      return Promise.resolve(found ? EMPTY_PAYLOADS[found] : {});
    });

    renderCenter();

    // The failed panels are named, and the rest of the page still renders.
    expect(await screen.findByText(/3 panels could not load/)).toBeInTheDocument();
    expect(screen.getByText("Spring push")).toBeInTheDocument();
    expect(screen.getByText("What needs attention")).toBeInTheDocument();

    // UNAVAILABLE is shown instead of a number...
    const unavailable = screen.getAllByText("UNAVAILABLE");
    expect(unavailable.length).toBeGreaterThanOrEqual(3);

    // ...and no money figure was invented for the dead spend endpoint.
    expect(screen.queryByText(/\$0\.0000/)).toBeNull();
    expect(screen.queryByText("$19.7500")).toBeNull();

    // A failed panel shows its error, not a zero.
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
  });

  it("gives SUBMISSION_UNKNOWN the unknown tone and never the warning tone", async () => {
    resolveAll({
      "/provider-maturity/incidents?limit=50": {
        items: [
          {
            incident_id: "video_submission:v-1",
            source: "video_submission",
            provider: "heygen",
            operation: "video.render.submit",
            attempted_at: "2026-03-01T09:00:00Z",
            remote_id: "",
            state: "SUBMISSION_UNKNOWN",
            display_state: "SUBMISSION_UNKNOWN",
            exposure: "UNKNOWN_EXPOSURE",
            estimated_exposure_usd: null,
            exposure_unknown: true,
            recommended_action: "RECONCILE",
            retry_safe: false,
            may_resubmit: false,
            detail: "2xx with no body",
            note: "reconcile with the provider before doing anything else",
          },
        ],
        count: 1,
        unknown_exposure_count: 1,
        states: ["SUBMISSION_UNKNOWN"],
        note: "",
      },
    });

    renderCenter();

    await screen.findByText("heygen");
    const tiles = screen.getAllByText("SUBMISSION_UNKNOWN");
    const tile = tiles.map((el) => el.closest(".ym-stat")).find((el) => el !== null);
    expect(tile).toBeTruthy();
    expect(tile?.className).toContain("ym-tone-unknown");
    expect(tile?.className).not.toContain("ym-tone-warning");

    // The incident row carries the same tone, and offers no retry.
    const row = screen.getByText("heygen").closest("tr");
    expect(row?.querySelector(".ym-tone-unknown")).toBeTruthy();
    expect(row?.querySelector(".ym-tone-warning")).toBeNull();
    expect(row?.textContent).toContain("no retry offered");
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });
});