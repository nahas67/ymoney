/** @vitest-environment jsdom */
/* Command Center — honesty first, operations-console layout second.
 *
 * The honesty assertions are the load-bearing ones: a dashboard that renders 0
 * for "the endpoint is dead" is the failure mode this screen is built to
 * avoid, so the failing-endpoint case is asserted as strictly as the loaded
 * one. The layout assertions (rail + attention queue + tabbed drill-down with
 * collapsible modules) pin the ops-console structure: attention queue first,
 * paid ambiguity ranked first, SUBMISSION_UNKNOWN never failed, unknown money
 * never $0.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    spent_last_24h_unknown_exposure_rows: 0,
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
  "/experiments": { total: 0, items: [] },
  "/knowledge/memories?limit=200": { items: [] },
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

function openTab(name: RegExp) {
  fireEvent.click(screen.getByRole("tab", { name }));
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

    // Workflows tab is the default drill-down.
    expect((await screen.findAllByText("Spring push")).length).toBeGreaterThan(0);
    expect(screen.getByText("No job is queued, running, waiting or retrying")).toBeInTheDocument();
    expect(screen.getByText("No review is waiting on a human")).toBeInTheDocument();

    openTab(/publish/i);
    expect(await screen.findByText("Nothing scheduled")).toBeInTheDocument();
    expect(screen.getByText("Nothing published yet")).toBeInTheDocument();

    openTab(/costs & risk/i);
    expect(await screen.findByText("No paid incident is open")).toBeInTheDocument();
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
    expect(screen.getAllByText("Spring push").length).toBeGreaterThan(0);
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

    // The attention queue surfaces the ambiguity first, verbatim.
    const attn = await screen.findByText(/Reconcile before acting: heygen/);
    expect(attn.closest("li")?.textContent).toContain("Paid ambiguity");

    openTab(/costs & risk/i);
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
    // No exact "Retry" control is offered anywhere for paid ambiguity
    // (section toggles mention retry_safe in their metadata, which is why the
    // match is anchored).
    expect(screen.queryByRole("button", { name: /^retry$/i })).toBeNull();
  });

  it("ranks paid ambiguity above failed work in the attention queue", async () => {
    resolveAll({
      "/provider-maturity/incidents?limit=50": {
        items: [
          {
            incident_id: "video_submission:v-9",
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
            note: "",
          },
        ],
        count: 1,
        unknown_exposure_count: 1,
        states: ["SUBMISSION_UNKNOWN"],
        note: "",
      },
      "/jobs?limit=100": {
        items: [
          {
            id: "j-dead",
            type: "video_render",
            cycle_id: null,
            status: "DEAD",
            priority: 1,
            retry_count: 3,
            max_retries: 3,
            next_run_at: null,
            started_at: "2026-03-01T08:00:00Z",
            completed_at: null,
            last_error: "worker lost",
            result: {},
            created_at: "2026-03-01T08:00:00Z",
            claimed_by: "",
            lease_state: "",
          },
        ],
      },
    });
    renderCenter();

    await screen.findByText(/Reconcile before acting: heygen/);
    const section = screen.getByText("What needs attention").closest("section")!;
    const items = within(section).getAllByRole("listitem");
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain("Paid ambiguity");
    expect(items[1].textContent).toContain("Failed");
    expect(items[1].textContent).toContain("Job dead");
  });

  it("lists upcoming publications and separates measured output from unmeasured", async () => {
    resolveAll({
      "/calendar": {
        items: [
          {
            id: "s-1",
            platform: "youtube",
            run_at: "2026-03-02T09:00:00Z",
            content_item_id: "ct-1",
            campaign_id: "c-1",
            status: "PENDING",
          },
        ],
      },
      "/publishing/posts?limit=25": {
        items: [
          {
            id: "p-1",
            platform: "youtube",
            title: "Measured short",
            remote_url: null,
            published_at: "2026-03-01T10:00:00Z",
            is_mock: false,
            metrics: { views: 120, likes: 4, comments: 1, completion_rate: 0.5 },
          },
          {
            id: "p-2",
            platform: "tiktok",
            title: "Unmeasured short",
            remote_url: null,
            published_at: "2026-03-01T11:00:00Z",
            is_mock: false,
            metrics: { views: null, likes: null, comments: null, completion_rate: null },
          },
        ],
      },
    });
    renderCenter();
    await screen.findAllByText("Spring push");

    openTab(/publish/i);
    expect(await screen.findByText("Upcoming publications")).toBeInTheDocument();
    // Platform appears in both the schedule row and the post row.
    expect(screen.getAllByText("Youtube").length).toBe(2);
    expect(screen.getByText("Measured short")).toBeInTheDocument();
    // The unmeasured post says so; it is never a zero.
    expect(screen.getByText("no metric")).toBeInTheDocument();
  });

  it("renders unpriceable spend as unknown while the budget gate stays real", async () => {
    resolveAll({
      "/costs": {
        last_24h_by_category: {},
        spent_last_24h_usd: null,
        spent_last_24h_unknown_exposure_rows: 2,
        daily_budget_usd: 20,
        per_video_budget_usd: 0.4,
        within_budget: true,
        remaining_usd: 19.75,
      },
    });
    renderCenter();
    await screen.findAllByText("Spring push");

    openTab(/costs & risk/i);
    await screen.findByText("Spend — budget status");
    // Unknown spend is UNAVAILABLE/unknown, never $0.0000...
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("$0.0000")).toBeNull();
    // ...while the conservative gate still reports the real remainder.
    expect(screen.getByText("$19.7500")).toBeInTheDocument();
  });

  it("derives campaign pulse from content rows, and says UNAVAILABLE when content is dead", async () => {
    const contentPayload = {
      total: 3,
      items: [
        {
          id: "ct-1",
          topic: "bad take",
          status: "FAILED",
          campaign_id: "c-1",
          cycle_id: null,
          strategy: {},
          error: "hook rejected",
          video: null,
          variants_count: 1,
          created_at: "2026-03-01T08:00:00Z",
        },
        {
          id: "ct-2",
          topic: "rendering take",
          status: "READY",
          campaign_id: "c-1",
          cycle_id: null,
          strategy: {},
          error: null,
          video: {
            id: "v-2",
            status: "RENDERING",
            engine: "heygen",
            file_path: "",
            thumbnail_path: "",
            progress: 40,
            duration_seconds: null,
            error: "",
            quality: null,
            quality_passed: null,
            quality_notes: "",
            quality_components: {},
            aspect_ratio: null,
          },
          variants_count: 1,
          created_at: "2026-03-01T08:00:00Z",
        },
      ],
    };
    resolveAll({ "/content?limit=200": contentPayload });
    renderCenter();
    await screen.findAllByText("Spring push");

    const pulse = screen.getByText("Campaign pulse").closest("section")!;
    const row = within(pulse).getByText("Spring push").closest("tr")!;
    // 2 items, 1 failed, 1 rendering — counted, not estimated.
    expect(row.textContent).toContain("2");
    expect(row.querySelector(".ym-tone-danger")).toBeTruthy();
  });

  it("counts performance signals from measured snapshots only — a genuine zero counts", async () => {
    resolveAll({
      "/publishing/posts?limit=25": {
        items: [
          {
            id: "p-zero",
            platform: "youtube",
            title: "Zero-view short",
            remote_url: null,
            published_at: "2026-03-01T10:00:00Z",
            is_mock: false,
            metrics: { views: 0, likes: 0, comments: 0, completion_rate: 0.2 },
          },
          {
            id: "p-hit",
            platform: "youtube",
            title: "Hit short",
            remote_url: null,
            published_at: "2026-03-01T11:00:00Z",
            is_mock: false,
            metrics: { views: 100, likes: 5, comments: 2, completion_rate: 0.6 },
          },
          {
            id: "p-none",
            platform: "tiktok",
            title: "No snapshot short",
            remote_url: null,
            published_at: "2026-03-01T12:00:00Z",
            is_mock: false,
            metrics: { views: null, likes: null, comments: null, completion_rate: null },
          },
        ],
      },
    });
    renderCenter();
    await screen.findAllByText("Spring push");

    openTab(/intelligence/i);
    expect(await screen.findByText("Performance signals")).toBeInTheDocument();
    // 0 + 100 over the 2 measured publications of 3 live: the null row adds nothing.
    expect(screen.getByText("Summed over 2 measured publications of 3 live")).toBeInTheDocument();
  });

  it("names an experiment winner only when the backend completed one, and shows derived learnings only", async () => {
    resolveAll({
      "/experiments": {
        total: 2,
        items: [
          {
            id: "e-run",
            kind: "HOOK",
            hypothesis: "Hook test running",
            platform: "youtube",
            primary_metric: "views",
            minimum_sample: 60,
            status: "RUNNING",
            confidence: "",
            result: {},
            created_at: "2026-03-01T08:00:00Z",
          },
          {
            id: "e-done",
            kind: "TITLE",
            hypothesis: "Title test done",
            platform: "youtube",
            primary_metric: "views",
            minimum_sample: 60,
            status: "COMPLETED",
            confidence: "95% CI excludes zero",
            result: { total_samples: 120, winner: "variant-b", minimum_sample: 60 },
            created_at: "2026-02-28T08:00:00Z",
          },
        ],
      },
      "/knowledge/memories?limit=200": {
        items: [
          {
            id: "m-1",
            type: "CREATIVE_LESSON",
            topic: "hooks",
            content: "Short hooks under 3 seconds retain better on measured posts.",
            confidence: 0.8,
            freshness: "FRESH",
            status: "ACTIVE",
            effective_status: "ACTIVE",
            source_ids: ["s-1"],
            evidence_ids: ["p-1"],
            created_at: "2026-03-01T09:00:00Z",
          },
          {
            id: "m-2",
            type: "RESEARCH_FACT",
            topic: "audience size",
            content: "The audience is large.",
            confidence: 0.9,
            freshness: "FRESH",
            status: "ACTIVE",
            effective_status: "ACTIVE",
            source_ids: ["s-2"],
            evidence_ids: ["s-2"],
            created_at: "2026-03-01T09:00:00Z",
          },
        ],
      },
    });
    renderCenter();
    await screen.findAllByText("Spring push");

    openTab(/intelligence/i);
    expect((await screen.findAllByText("Experiment results")).length).toBeGreaterThan(0);
    expect(screen.getByText("variant-b")).toBeInTheDocument();
    const runningRow = screen.getByText("Hook test running").closest("tr")!;
    expect(runningRow.textContent).not.toContain("variant-b");

    // Derived lessons render with verbatim status; evidence rows stay in Memory.
    expect(screen.getByText("What agents learned")).toBeInTheDocument();
    expect(screen.getByText(/Short hooks under 3 seconds/)).toBeInTheDocument();
    expect(screen.queryByText("The audience is large.")).toBeNull();
  });

  it("renders readiness probes verbatim with their remediation", async () => {
    resolveAll();
    sysApi.mockImplementation(() =>
      Promise.resolve({
        status: "degraded",
        checked_at: "2026-03-01T10:00:00Z",
        checks: [
          {
            id: "heygen",
            status: "failed",
            blocking: true,
            tier: 1,
            detail: "connection refused",
            latency_ms: 12,
            remediation: "Rotate the API key in Providers.",
          },
        ],
        blocking_failures: ["heygen"],
        message: "One production dependency is down.",
      }),
    );
    renderCenter();
    await screen.findAllByText("Spring push");

    // The rail reports the block in words, not color alone.
    expect(await screen.findByText("1 blocking — down")).toBeInTheDocument();

    openTab(/system/i);
    expect(await screen.findByText("System readiness")).toBeInTheDocument();
    // Verbatim in both the attention item and the probe row.
    expect(screen.getAllByText("Rotate the API key in Providers.").length).toBe(2);
  });

  it("discloses modules progressively and switches drill-down tabs", async () => {
    resolveAll();
    renderCenter();
    await screen.findAllByText("Spring push");

    // Queue health starts collapsed; expanding is a keyboard-operable disclosure.
    const toggle = screen.getByRole("button", { name: /queue health/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("No job recorded")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("No job recorded")).toBeInTheDocument();

    // Tabs swap the drill-down without losing the attention queue.
    openTab(/publish/i);
    expect(await screen.findByText("Upcoming publications")).toBeInTheDocument();
    expect(screen.getByText("What needs attention")).toBeInTheDocument();
    openTab(/workflows/i);
    expect((await screen.findAllByText("Active campaigns")).length).toBeGreaterThan(0);
  });
});
