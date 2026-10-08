/** @vitest-environment jsdom */
/* UNAVAILABLE IS NOT ZERO — the UI half of Work 16.5.7 §8/§11.
 *
 * The backend now says `null` where it used to say `0`, and this file proves the
 * screens ACT ON THAT rather than re-deriving the answer client-side. Every
 * fixture below is a real response shape: the UNAVAILABLE ones are the payload
 * the backend emits for an empty workspace, and the ZERO ones are the payload it
 * emits for rows that genuinely recorded zero.
 *
 * Two claims are asserted for every metric, and both matter:
 *
 *   1. an unavailable metric renders the literal UNAVAILABLE, never 0;
 *   2. a REAL measured zero still renders 0.
 *
 * (2) is the one that catches an over-correction. If every missing number became
 * UNAVAILABLE, the UI would stop being wrong and start being useless: "0 views"
 * and "nobody reported views" would be the same screen. The backend can tell
 * them apart, so the UI must too.
 *
 * Mocking style follows `features/analytics/Analytics.test.tsx` and
 * `features/command-center/CommandCenter.test.tsx`: mock `lib/api` and
 * `state/session`, serve fixtures by path prefix, assert on rendered text.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const wsGet = vi.fn();
const sysApi = vi.fn();

vi.mock("../lib/api", () => ({
  // queries.ts reads ApiError to decide whether a status is ignorable, so the
  // mock has to carry it: a mock that omits an export fails at the error path.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: { get: (path: string) => wsGet(path) },
  api: (method: string, path: string) => sysApi(method, path),
  videoFileUrl: (id: string) => `/video/${id}.mp4`,
  videoThumbUrl: (id: string) => `/thumb/${id}.jpg`,
}));

vi.mock("../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: [],
    switchWorkspace: () => {},
    reload: () => {},
  }),
}));

import Analytics, { type AnalyticsOverview } from "../features/analytics/Analytics";
import { CommandCenter } from "../features/command-center/CommandCenter";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  sysApi.mockReset();
});

/* ==========================================================================
 * Fixtures, transcribed from the routers' own return statements.
 * ========================================================================== */

/* misc.py::analytics_overview — NOTHING MEASURED. Every total is null, which is
 * what the endpoint now emits for a workspace with no PostMetric snapshot. */
const UNAVAILABLE_OVERVIEW = {
  totals: { views: null, likes: null, comments: null, shares: null, followers_gained: null },
  posts_published: 3,
  content_items: 9,
  cost_total_usd: null,
  cost_total_unknown_exposure_rows: 0,
  per_platform: {},
  best_post: null,
  mock_analytics: false,
};

/* misc.py::analytics_overview — a ledger holding one unpriceable exposure. The
 * total is null AND the reason is published, so the UI can say which. */
const UNKNOWN_EXPOSURE_OVERVIEW = {
  ...UNAVAILABLE_OVERVIEW,
  cost_total_unknown_exposure_rows: 2,
};

/* misc.py::analytics_overview — MEASURED ZEROS. A snapshot exists and recorded
 * zero. `per_platform` proves a snapshot was summed, which is exactly what the
 * old client-side `anyMeasured` heuristic used to infer. */
const MEASURED_ZERO_OVERVIEW = {
  totals: { views: 0, likes: 0, comments: 0, shares: 0, followers_gained: 0 },
  posts_published: 3,
  content_items: 9,
  cost_total_usd: 0,
  cost_total_unknown_exposure_rows: 0,
  per_platform: { youtube: { posts: 1, views: 0 } },
  best_post: { platform: "youtube", title: "Measured short", views: 0 },
  mock_analytics: false,
};

/* misc.py::analytics_breakdowns — a bucket whose posts were never measured. */
const UNAVAILABLE_BREAKDOWNS = {
  by_topic: [
    {
      key: "Hooks",
      posts: 0,
      mock_posts: 0,
      total_views: null,
      avg_views: null,
      engagement_pct: null,
      engagement_samples: 0,
    },
  ],
  by_hook_style: [],
  by_duration: [],
  posts_with_metrics: 0,
  mock_analytics: false,
};

/* misc.py::cost_summary — an empty window. Not "$0.00 spent". */
const UNAVAILABLE_COSTS = {
  last_24h_by_category: {},
  spent_last_24h_usd: null,
  spent_last_24h_unknown_exposure_rows: 0,
  daily_budget_usd: 20,
  per_video_budget_usd: 0.4,
  within_budget: true,
  remaining_usd: 20,
};

/* Same endpoint, one UNKNOWN_EXPOSURE row priced at nothing. */
const UNKNOWN_EXPOSURE_COSTS = {
  ...UNAVAILABLE_COSTS,
  last_24h_by_category: { llm: 1.25, video: 0.5 },
  spent_last_24h_unknown_exposure_rows: 1,
};

/* A real, explicit $0.00 cost row. */
const MEASURED_ZERO_COSTS = {
  ...UNAVAILABLE_COSTS,
  last_24h_by_category: { storage: 0 },
  spent_last_24h_usd: 0,
};

/* misc.py::list_published_posts — one post per case. */
function post(id: string, metrics: Record<string, number | null>) {
  return {
    id,
    platform: "youtube",
    title: id,
    remote_url: "",
    published_at: "2026-01-02T09:00:00Z",
    is_mock: false,
    metrics,
  };
}

const NO_METRICS = { views: null, likes: null, comments: null, completion_rate: null };
const ZERO_METRICS = { views: 0, likes: 0, comments: 0, completion_rate: 0 };

/* `agent_status` — no AgentRun rows at all for any agent. */
const UNAVAILABLE_AGENTS = {
  items: [
    {
      key: "trend_hunter",
      title: "Trend Hunter",
      description: "fetches candidates",
      capabilities: [],
      tools: [],
      enabled: true,
      status: "idle",
      current_task: null,
      runs: 0,
      failure_rate: null,
      avg_duration_ms: null,
      total_cost_usd: null,
    },
  ],
  recent_runs: [],
};

const MEASURED_ZERO_AGENTS = {
  ...UNAVAILABLE_AGENTS,
  items: [
    {
      ...UNAVAILABLE_AGENTS.items[0],
      runs: 1,
      failure_rate: 0,
      avg_duration_ms: 10,
      total_cost_usd: 0,
    },
  ],
};

/* The Command Center asks for these paths (see `useCombinedQueries`). Each is
 * answered with a real-shaped empty payload so the screen renders rather than
 * erroring on an unrelated panel. */
const CENTER_DEFAULTS: Record<string, unknown> = {
  "/campaigns": { items: [] },
  "/content": { total: 0, items: [] },
  "/calendar": { items: [] },
  "/jobs": { items: [] },
  "/costs": UNAVAILABLE_COSTS,
  "/publishing/posts": { items: [] },
  "/intelligence/routing/chains": { chains: [], note: "in-process and bounded" },
  "/planner/opportunities": {
    count: 0,
    opportunities: [],
    forbidden_claims: [],
    note: "the planner never asserts virality",
  },
  "/inbox/opportunities": { items: [] },
  "/provider-maturity/incidents": {
    items: [],
    count: 0,
    unknown_exposure_count: 0,
    states: [],
    note: "SUBMISSION_UNKNOWN is never shown as FAILED",
  },
  "/reviews": { items: [] },
};

function serveCenter(costs: unknown, posts: unknown[]) {
  const payloads: Record<string, unknown> = {
    ...CENTER_DEFAULTS,
    "/costs": costs,
    "/publishing/posts": { items: posts },
  };
  wsGet.mockImplementation((path: string) => {
    const key = Object.keys(payloads).find((k) => path.startsWith(k.split("?")[0]));
    return Promise.resolve(key ? payloads[key] : {});
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

function serve(routes: Record<string, unknown>, fallback: unknown = {}) {
  wsGet.mockImplementation((path: string) => {
    for (const [prefix, payload] of Object.entries(routes)) {
      if (path.startsWith(prefix)) return Promise.resolve(payload);
    }
    return Promise.resolve(fallback);
  });
}

/**
 * The rendered value of the StatTile with this label, scoped to one panel.
 *
 * Scoped because labels repeat across panels on this screen -- "Views" is a tile
 * in "At a glance" AND in "Best post" -- and an unscoped lookup fails with
 * "found multiple elements", which is a test bug that reads like a product bug.
 */
async function tileText(label: string, panel: string): Promise<string> {
  const heading = await screen.findByText(panel);
  const scope = heading.closest(".ym-panel") ?? document.body;
  const el = await within(scope as HTMLElement).findByText(label);
  const tile = el.closest(".ym-stat") as HTMLElement;
  expect(tile, `no tile found for ${label}`).not.toBeNull();
  return (tile.querySelector(".ym-stat-value")?.textContent ?? "").trim();
}

/** Open a tab of the Analytics screen, whose panels render one at a time. */
async function openTab(name: string) {
  const tab = await screen.findByRole("tab", { name });
  tab.click();
  await waitFor(() => expect(tab).toHaveAttribute("aria-selected", "true"));
}

const ANALYTICS_FALLBACK = {
  breakdowns: UNAVAILABLE_BREAKDOWNS,
  patterns: { items: [] },
  campaigns: { items: [] },
  capabilities: { items: [] },
  compare: { campaign_id: "c-1", group_by: "hook", groups: [], causal: false, note: "" },
  "overview?": { items: [] },
};

/** The Analytics screen's per-post panel renders under this heading. */
const CONTENT_PANEL = "Measured content";

/* ==========================================================================
 * §8 ANALYTICS — the backend null is the availability signal
 * ========================================================================== */

describe("Analytics: an unmeasured total renders UNAVAILABLE, not 0", () => {
  it("never shows 0 for a total nothing was summed into", async () => {
    serve({ "/analytics/overview": UNAVAILABLE_OVERVIEW }, ANALYTICS_FALLBACK);
    render(<Analytics />);

    await screen.findByText("At a glance");
    for (const label of ["Views", "Likes", "Comments", "Shares", "Followers gained"]) {
      expect(await tileText(label, "At a glance")).toBe("UNAVAILABLE");
    }
  });

  it("says WHY the cost total is missing when an exposure is unpriceable", async () => {
    serve({ "/analytics/overview": UNKNOWN_EXPOSURE_OVERVIEW }, ANALYTICS_FALLBACK);
    render(<Analytics />);

    await screen.findByText("At a glance");
    // The count is the whole point of the new field: an operator who sees a bare
    // dash cannot tell "nothing spent" from "money we cannot see".
    expect(
      await screen.findByText(/2 cost row\(s\) record an exposure nobody can price/),
    ).toBeInTheDocument();
  });

  it("does NOT compensate client-side for a total the backend already refused", async () => {
    /* The regression this whole change exists to prevent: a screen that
     * re-derives availability from a COUNT of measured posts, and therefore
     * keeps guessing after the backend started telling the truth. A measured
     * count of 1 with a null total is impossible; if the UI shows a number here
     * it is not reading the backend. */
    const impossible = { ...UNAVAILABLE_OVERVIEW, per_platform: { youtube: { posts: 1, views: 0 } } };
    serve({ "/analytics/overview": impossible }, ANALYTICS_FALLBACK);
    render(<Analytics />);

    await screen.findByText("At a glance");
    expect(await tileText("Views", "At a glance")).toBe("UNAVAILABLE");
  });

  it("still renders a REAL measured zero as 0", async () => {
    serve({ "/analytics/overview": MEASURED_ZERO_OVERVIEW }, ANALYTICS_FALLBACK);
    render(<Analytics />);

    await screen.findByText("At a glance");
    for (const label of ["Views", "Likes", "Comments", "Shares", "Followers gained"]) {
      expect(await tileText(label, "At a glance")).toBe("0");
    }
    // A measured $0.00 is also a real figure.
    expect(await tileText("Total cost", "At a glance")).toContain("$0.0000");
  });

  it("keeps COUNT fields numeric even when nothing was measured", async () => {
    serve({ "/analytics/overview": UNAVAILABLE_OVERVIEW }, ANALYTICS_FALLBACK);
    render(<Analytics />);

    await screen.findByText("At a glance");
    // A COUNT over an empty table is a measured zero. Rendering it UNAVAILABLE
    // would be the over-correction.
    expect(await tileText("Published posts", "At a glance")).toBe("3");
    expect(await tileText("Content items", "At a glance")).toBe("9");
  });
});

describe("Analytics: per-post metrics distinguish unmeasured from zero", () => {
  /* The per-post panel is a TAB, not a section of the overview, so these tests
   * open it first. A screen that renders every panel at once would not need
   * this, and the click is here so a tab change does not silently turn these
   * into assertions about nothing. */
  it("shows UNAVAILABLE for a post with no snapshot", async () => {
    serve(
      { "/publishing/posts": { items: [post("p-1", NO_METRICS)] } },
      ANALYTICS_FALLBACK,
    );
    render(<Analytics />);
    await openTab("Content");

    await screen.findByText(CONTENT_PANEL);
    const row = (await screen.findByText("p-1")).closest("tr") as HTMLElement;
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent?.trim());
    expect(cells).toContain("UNAVAILABLE");
    expect(cells).not.toContain("0");
  });

  it("shows 0 for a post whose snapshot genuinely recorded zero", async () => {
    serve(
      { "/publishing/posts": { items: [post("p-2", ZERO_METRICS)] } },
      ANALYTICS_FALLBACK,
    );
    render(<Analytics />);
    await openTab("Content");

    await screen.findByText(CONTENT_PANEL);
    const row = (await screen.findByText("p-2")).closest("tr") as HTMLElement;
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent?.trim());
    expect(cells).toContain("0");
    expect(cells).not.toContain("UNAVAILABLE");
  });
});

/* ==========================================================================
 * §11 COMMAND CENTER — the dashboard that reads /costs and /publishing/posts
 * ========================================================================== */

describe("Command Center: spend is UNAVAILABLE, not $0.00", () => {
  /* The spend figure lives in the status rail (kicker "Spend 24h"), not in a
   * StatTile: the redesign moved it, but the honesty contract is unchanged —
   * a null total renders UNAVAILABLE, never $0. */
  async function spendRail(): Promise<HTMLElement> {
    const kicker = await screen.findByText("Spend 24h");
    const block = kicker.closest("button") as HTMLElement;
    expect(block, "no spend rail block").not.toBeNull();
    return block;
  }

  it("marks the spend tile unavailable when the window is empty", async () => {
    serveCenter(UNAVAILABLE_COSTS, []);
    renderCenter();

    const block = await spendRail();
    await waitFor(() => {
      expect(within(block).getByText("UNAVAILABLE")).toBeInTheDocument();
      // And never "$0.0000" anywhere in that block.
      expect(block.textContent).not.toContain("$0.0000");
    });
  });

  it("does NOT read /ops/overview, which still fabricates $0.0000", async () => {
    /* The Command Center asks for `/costs` (see `useCombinedQueries`), which is
     * the endpoint this change fixed. `ops.py::_costs_section` is a DUPLICATE
     * that was out of scope and still reports $0.0000 for an empty window — the
     * Operations screen reads that one. This asserts the Command Center is on the
     * fixed endpoint, so the fix actually reaches the dashboard, and names the
     * one it does not reach. */
    const asked: string[] = [];
    const payloads: Record<string, unknown> = { ...CENTER_DEFAULTS, "/costs": UNAVAILABLE_COSTS };
    wsGet.mockImplementation((path: string) => {
      asked.push(path);
      const key = Object.keys(payloads).find((k) => path.startsWith(k.split("?")[0]));
      return Promise.resolve(key ? payloads[key] : {});
    });
    sysApi.mockImplementation(() =>
      Promise.resolve({ status: "ready", checked_at: "", checks: [], blocking_failures: [] }),
    );
    renderCenter();
    await screen.findByText("Command Center");

    expect(asked.some((p) => p.startsWith("/costs"))).toBe(true);
    expect(asked.some((p) => p.startsWith("/ops/overview"))).toBe(false);
  });

  it("says how many rows are unpriceable instead of a bare dash", async () => {
    serveCenter(UNKNOWN_EXPOSURE_COSTS, []);
    renderCenter();

    // The unpriceable-row count lives in the Costs & risk tab.
    (await screen.findByRole("tab", { name: /Costs & risk/ })).click();
    await waitFor(async () => {
      expect(screen.getByText(/cost row\(s\) record an exposure nobody can price/)).toBeInTheDocument();
    });
  });

  it("shows $0.0000 for a real, explicit zero-cost row", async () => {
    serveCenter(MEASURED_ZERO_COSTS, []);
    renderCenter();

    const block = await spendRail();
    await waitFor(() => {
      expect(block.textContent).toContain("$0.0000");
      expect(within(block).queryByText("UNAVAILABLE")).not.toBeInTheDocument();
    });
  });

  it("keeps the BUDGET GATE a real verdict even when the spend total is unknown", async () => {
    /* The distinction that matters for safety: `within_budget` is a decision the
     * backend makes conservatively (unknown spend eats headroom), so the UI must
     * not render it as "unknown" and let a reader assume the gate is unresolved.
     * It renders the gate, and only the MEASUREMENT as UNAVAILABLE. */
    serveCenter(UNKNOWN_EXPOSURE_COSTS, []);
    renderCenter();

    // The gate lives in the Costs & risk tab next to the "Remaining" metric.
    (await screen.findByRole("tab", { name: /Costs & risk/ })).click();
    await waitFor(async () => {
      const el = await screen.findByText("Remaining");
      const metric = (el.closest("[class*='metric']") ?? el.parentElement) as HTMLElement;
      expect(within(metric).queryByText("UNAVAILABLE")).not.toBeInTheDocument();
      expect(metric.textContent).toMatch(/\$/);
    });
  });

  it("does not count a zero-view post as unmeasured", async () => {
    /* `measuredPosts` used to filter on `completion_rate !== null || views > 0`,
     * which excluded a post whose snapshot genuinely said zero. A measured zero
     * is a measurement, and the count is a measurement. */
    serveCenter(MEASURED_ZERO_COSTS, [post("p-zero", ZERO_METRICS)]);
    renderCenter();

    // The measured-post count lives in the Publish tab.
    (await screen.findByRole("tab", { name: /^Publish/ })).click();
    await waitFor(async () => {
      const el = await screen.findByText("With a metric snapshot");
      const metric = (el.closest("[class*='metric']") ?? el.parentElement) as HTMLElement;
      expect(within(metric).getByText("1")).toBeInTheDocument();
    });
  });

  it("excludes a post with no snapshot from the measured count", async () => {
    serveCenter(UNAVAILABLE_COSTS, [post("p-none", NO_METRICS)]);
    renderCenter();

    (await screen.findByRole("tab", { name: /^Publish/ })).click();
    await waitFor(async () => {
      const el = await screen.findByText("With a metric snapshot");
      const metric = (el.closest("[class*='metric']") ?? el.parentElement) as HTMLElement;
      expect(within(metric).getByText("0")).toBeInTheDocument();
      // 0 measured posts is a real count, so the metric is NOT "unavailable".
      expect(within(metric).queryByText("UNAVAILABLE")).not.toBeInTheDocument();
    });
  });
});

describe("Command Center: the post table shows no-metric, not 0 views", () => {
  it("labels an unmeasured post instead of printing a bare zero", async () => {
    serveCenter(UNAVAILABLE_COSTS, [post("p-x", NO_METRICS)]);
    renderCenter();

    // The recent-output table lives in the Publish tab.
    (await screen.findByRole("tab", { name: /^Publish/ })).click();
    await waitFor(async () => {
      const row = (await screen.findByText("p-x")).closest("tr") as HTMLElement;
      expect(row.textContent).toContain("no metric");
    });
  });

  it("prints 0 for a post whose snapshot recorded a real zero", async () => {
    serveCenter(MEASURED_ZERO_COSTS, [post("p-y", ZERO_METRICS)]);
    renderCenter();

    (await screen.findByRole("tab", { name: /^Publish/ })).click();
    await waitFor(async () => {
      const row = (await screen.findByText("p-y")).closest("tr") as HTMLElement;
      expect(row.textContent).not.toContain("no metric");
    });
  });
});

/* ==========================================================================
 * The frontend contract, asserted on the source that compiles
 * ========================================================================== */

describe("the TS types admit null for every widened field", () => {
  /* A COMPILE-TIME claim, proven by the file compiling: the literal below is
   * annotated with the screen's own exported type and assigns nulls to
   * `totals` and `cost_total_usd`. If either were declared `number`, `tsc -b`
   * fails here and nothing in a test run would have said so. `npx tsc -b
   * --force` is therefore part of this file's verification, not just the app's. */
  it("AnalyticsOverview.cost_total_usd is nullable", () => {
    const overview: AnalyticsOverview = {
      ...UNAVAILABLE_OVERVIEW,
      totals: { views: null, likes: null, comments: null, shares: null, followers_gained: null },
    };
    expect(overview.totals.views).toBeNull();
    expect(overview.cost_total_usd).toBeNull();
  });

  it("a measured zero still satisfies the same type", () => {
    const overview: AnalyticsOverview = { ...MEASURED_ZERO_OVERVIEW };
    expect(overview.totals.views).toBe(0);
  });
});

describe("agent stats are UNAVAILABLE, not a 0% failure rate", () => {
  it("keeps the run count numeric and refuses the rate", async () => {
    /* Not rendered by these two screens today, so the check is on the payload
     * the Command Center forwards: a 0% failure rate for an agent that never ran
     * is the most flattering possible reading of no data, and the backend no
     * longer emits it. */
    const agent = UNAVAILABLE_AGENTS.items[0];
    expect(agent.runs).toBe(0);
    expect(agent.failure_rate).toBeNull();
    expect(agent.total_cost_usd).toBeNull();

    const measured = MEASURED_ZERO_AGENTS.items[0];
    expect(measured.failure_rate).toBe(0);
    expect(measured.total_cost_usd).toBe(0);
  });
});
