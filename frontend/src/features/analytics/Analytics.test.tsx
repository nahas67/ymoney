/** @vitest-environment jsdom */
/* Analytics — UNAVAILABLE IS NOT ZERO.
 *
 * This is the one screen where a fabricated number does real damage: an operator
 * reading "0 views" concludes the content failed, when the truth is that no
 * provider ever reported a figure. The backend hands this screen zeros as
 * *initialisers* in four separate places (see the header comment in Analytics.tsx),
 * so the assertions here are about the difference between a measurement and a
 * placeholder:
 *
 *   - a post with no metric snapshot renders UNAVAILABLE, not 0;
 *   - a channel whose totals were never summed renders UNAVAILABLE, not 0;
 *   - a failed endpoint is an alert naming the failure, not an empty list and
 *     not a 0;
 *   - a failure never reads as emptiness.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const wsGet = vi.fn();

vi.mock("../../lib/api", () => ({
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
    capabilities: ["content.read", "operations.view"],
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  }),
}));

import Analytics from "./Analytics";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
});

/* Fixtures transcribed from the routers' own return statements. */

/* misc.py::analytics_overview — measured: one youtube post with a snapshot. */
const MEASURED_OVERVIEW = {
  totals: { views: 4200, likes: 300, comments: 40, shares: 12, followers_gained: 5 },
  posts_published: 3,
  content_items: 9,
  cost_total_usd: 1.25,
  per_platform: { youtube: { posts: 1, views: 4200 } },
  best_post: { platform: "youtube", title: "Measured short", views: 4200 },
  mock_analytics: false,
};

/* misc.py::analytics_overview — posts exist, but NOTHING has a snapshot.
 *
 * UPDATED by Work 16.5.7 §8. This fixture used to carry `totals` full of ZEROS
 * with a comment saying they were the initialiser; the backend now sends `null`
 * for a total nothing was summed into, so the fixture sends null. The screen no
 * longer has to infer "unmeasured" from a COUNT of measured posts -- it reads the
 * backend's own statement. `cost_total_usd: null` for the same reason: an empty
 * ledger is not "$0.00 spent". */
const UNMEASURED_OVERVIEW = {
  totals: { views: null, likes: null, comments: null, shares: null, followers_gained: null },
  posts_published: 3,
  content_items: 9,
  cost_total_usd: null,
  cost_total_unknown_exposure_rows: 0,
  per_platform: {},
  best_post: null,
  mock_analytics: false,
};

const BREAKDOWNS = {
  by_topic: [
    { key: "Hooks", posts: 4, mock_posts: 0, total_views: 900, avg_views: 225, engagement_pct: 3.5, engagement_samples: 4 },
    /* Work 16.5.7 §8: a bucket with no MEASURED post reports null for its total
     * and averages. Was `0` / `0`, which is the fabrication this whole change
     * removes. */
    { key: "Empty bucket", posts: 0, mock_posts: 0, total_views: null, avg_views: null, engagement_pct: null, engagement_samples: 0 },
  ],
  by_hook_style: [],
  by_duration: [],
  posts_with_metrics: 4,
  mock_analytics: false,
};

const PATTERNS = {
  items: [
    {
      pattern_key: "question_hook",
      description: "Question hooks led on engagement rate.",
      improvement_pct: 18.2,
      confidence: "medium",
      sample_size: 12,
      active: true,
      updated_at: "2026-01-02T09:00:00Z",
    },
    {
      pattern_key: "no_data_hook",
      description: "A pattern with no measured improvement recorded.",
      improvement_pct: null,
      confidence: "",
      sample_size: 0,
      active: false,
      updated_at: "2026-01-02T09:00:00Z",
    },
  ],
};

/* misc.py::list_published_posts
 *
 * UPDATED by Work 16.5.7 §8. `measured: false` used to mean "the backend sent
 * views: 0 with completion_rate: null, and the UI had to read one field to learn
 * the other was a filler". Both are null now, so `completion_rate` is no longer
 * the only per-post tell — and a post whose snapshot genuinely recorded zero
 * views is a DIFFERENT row from one that was never measured. */
function post(id: string, measured: boolean) {
  return {
    id,
    platform: "youtube",
    title: id === "p-measured" ? "Measured short" : "Unmeasured short",
    remote_url: "",
    published_at: "2026-01-02T09:00:00Z",
    is_mock: false,
    metrics: measured
      ? { views: 4200, likes: 300, comments: 40, completion_rate: 0.42 }
      : { views: null, likes: null, comments: null, completion_rate: null },
  };
}

const CAMPAIGNS = {
  items: [{ id: "camp-1", name: "Spring push", status: "RUNNING" }],
};

const CAPABILITIES = {
  items: [
    {
      platform: "youtube",
      capabilities: ["DIRECT_PUBLISH"],
      publish_mode: "DIRECT_PUBLISH",
      supports_analytics: true,
      campaign_platforms: ["youtube_shorts"],
    },
    {
      platform: "snapchat",
      capabilities: ["USER_HANDOFF"],
      publish_mode: "USER_HANDOFF",
      supports_analytics: false,
      campaign_platforms: ["snapchat"],
    },
  ],
};

function baseRoute(path: string): unknown {
  if (path.startsWith("/analytics/overview")) return MEASURED_OVERVIEW;
  if (path.startsWith("/analytics/breakdowns")) return BREAKDOWNS;
  if (path.startsWith("/analytics/patterns")) return PATTERNS;
  if (path.startsWith("/publishing/posts")) return { items: [post("p-measured", true)] };
  if (path.startsWith("/campaigns")) return CAMPAIGNS;
  if (path.startsWith("/distribution/capabilities")) return CAPABILITIES;
  return {};
}

function serve(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation((path: string) => {
    for (const [prefix, payload] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return Promise.resolve(payload);
    }
    return Promise.resolve(baseRoute(path));
  });
}

function fail(predicate: (p: string) => boolean, message: string) {
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(baseRoute(path)),
  );
}

function renderAnalytics() {
  return render(<Analytics />);
}

describe("Analytics renders when data arrives", () => {
  it("shows the measured totals and the best post", async () => {
    serve();
    renderAnalytics();

    expect(await screen.findByText("Measured short")).toBeInTheDocument();
    // 4200 appears twice on purpose: once as the channel total, once as the
    // best post's views. Both are real measurements of the same snapshot.
    expect(screen.getAllByText("4200").length).toBe(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("marks a low-sample breakdown group rather than ranking it", async () => {
    serve();
    renderAnalytics();
    expect(await screen.findByText("Empty bucket")).toBeInTheDocument();
    // by_hook_style / by_duration have no rows and must say so, not render blank.
    expect(screen.getAllByText(/no .* measured/i).length).toBeGreaterThan(0);
  });
});

describe("UNAVAILABLE is not zero", () => {
  it("renders a per-post metric as UNAVAILABLE when no snapshot exists, never 0", async () => {
    serve({ "/publishing/posts": { items: [post("p-measured", true), post("p-unmeasured", false)] } });

    renderAnalytics();
    // Move to the Content tab.
    await userEvent.click(await screen.findByRole("tab", { name: "Content" }));

    expect(await screen.findByText("Unmeasured short")).toBeInTheDocument();

    const row = screen.getByText("Unmeasured short").closest("tr");
    expect(row).not.toBeNull();
    const cells = Array.from(row?.querySelectorAll("td") ?? []).map((c) => c.textContent ?? "");
    // Every metric cell on an unmeasured post is UNAVAILABLE. The backend sent
    // null (§8), so this is a rendering of its own statement, not a guess.
    expect(cells.filter((c) => /UNAVAILABLE/.test(c)).length).toBeGreaterThanOrEqual(3);
    expect(cells.some((c) => c.trim() === "0")).toBe(false);
  });

  it("renders channel totals as UNAVAILABLE when no post was ever measured", async () => {
    serve({ "/analytics/overview": UNMEASURED_OVERVIEW });
    renderAnalytics();

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/UNAVAILABLE/);
    });
    // The backend returned totals.views === null. It must not be painted as a figure.
    const views = screen.getAllByText("UNAVAILABLE").length;
    expect(views).toBeGreaterThan(1);
    // And the reason is stated in words, not left to inference.
    expect(document.body.textContent).toMatch(/no post has a metric snapshot/i);
  });

  it("states that an unmeasured best post is not a zero-view post", async () => {
    serve({ "/analytics/overview": UNMEASURED_OVERVIEW });
    renderAnalytics();

    expect(await screen.findByText(/No measured post yet/i)).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/no measurement",? not "zero views"/i);
  });

  it("renders a pattern with no measured improvement as UNAVAILABLE, not 0%", async () => {
    serve();
    renderAnalytics();

    await screen.findByText("question_hook");
    expect(document.body.textContent).toMatch(/no_data_hook/);
    // The row exists; its improvement cell must not read "0.0%".
    const patternRow = screen.getByText("no_data_hook").closest("tr");
    const cells = Array.from(patternRow?.querySelectorAll("td") ?? []).map((c) => c.textContent ?? "");
    expect(cells.some((c) => /UNAVAILABLE/.test(c))).toBe(true);
    expect(cells.some((c) => /0\.0%/.test(c))).toBe(false);
  });

  it("labels a platform the registry says reports no analytics", async () => {
    serve();
    renderAnalytics();

    await userEvent.click(await screen.findByRole("tab", { name: "Provider metrics" }));

    // "Snapchat" also appears in the campaign-platforms cell, so assert on the
    // badge that carries the registry's own verdict.
    await waitFor(() => {
      expect(screen.getAllByText("Snapchat").length).toBeGreaterThan(0);
    });
    const badge = screen.getByTitle(
      "The registry declares no analytics for this platform, so every figure for it is UNAVAILABLE.",
    );
    expect(badge).toHaveTextContent("NO");
    expect(badge).toHaveClass("ym-tone-unknown");
  });
});

describe("a failed endpoint is an alert, never a zero and never an empty list", () => {
  it("names the failure of the overview read", async () => {
    fail((p) => p.startsWith("/analytics/overview"), "analytics backend unavailable");
    renderAnalytics();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/analytics backend unavailable/i);
  });

  it("names the failure of the publications read", async () => {
    fail((p) => p.startsWith("/publishing/posts"), "posts backend unavailable");
    renderAnalytics();

    // The publications read only happens on the Content tab.
    await userEvent.click(await screen.findByRole("tab", { name: "Content" }));

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => /posts backend unavailable/i.test(a.textContent ?? ""))).toBe(true);
  });

  it("keeps a failure distinguishable from emptiness on a list that is not the active tab", async () => {
    fail((p) => p.startsWith("/analytics/breakdowns"), "breakdowns backend unavailable");
    renderAnalytics();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/breakdowns backend unavailable/i);
    // The breakdown panels are all empty in this fixture; a failed read must not
    // read as three empty breakdowns.
    expect(document.body.textContent).not.toMatch(/no by topic measured/i);
  });

  it("keeps a failure distinguishable from emptiness", async () => {
    fail((p) => p.startsWith("/analytics/patterns"), "patterns backend unavailable");
    renderAnalytics();

    await waitFor(() => {
      expect(document.body.textContent).toMatch(/patterns backend unavailable/i);
    });
    // The empty copy for this list ("no learned pattern yet") must NOT appear.
    expect(document.body.textContent).not.toMatch(/no learned pattern yet/i);
  });

  it("does not blank the whole screen when one panel fails", async () => {
    fail((p) => p.startsWith("/distribution/capabilities"), "registry unavailable");
    renderAnalytics();

    // The channel figures still render; the registry claim is refused instead.
    expect(await screen.findByText("Measured short")).toBeInTheDocument();
  });
});
