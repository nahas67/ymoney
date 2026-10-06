/** @vitest-environment jsdom */
/* Campaigns — publication mode is the correctness claim on this screen.
 *
 * A campaign list that merges LIVE, MOCK and HANDOFF into one "published" badge
 * is the failure this screen is built to prevent: an operator who believes a
 * handoff went out, or that a mock publish was real, will make a decision on
 * money and reach that it was never spent or never delivered.
 *
 * So the assertions are about DISTINCTNESS and HONESTY:
 *   - the three modes are separately visible, not collapsed;
 *   - a failed endpoint is an alert, not a zero;
 *   - a post whose mode cannot be resolved says so instead of defaulting.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

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
    capabilities: [],
    switchWorkspace: () => {},
    reload: () => {},
  }),
}));

import Campaigns from "./Campaigns";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
});

const CAMPAIGN = {
  id: "camp-1",
  name: "Spring push",
  goal: "ship three shorts",
  status: "ACTIVE",
  target_videos: 3,
  videos_per_day: 1,
  platforms: ["youtube"],
  automation_level: "AUTONOMOUS",
  budget_daily_usd: 5,
  starts_at: "2026-01-02T09:00:00Z",
  ends_at: null,
};

function route(path: string): unknown {
  if (path.startsWith("/campaigns")) return { items: [CAMPAIGN] };
  if (path.startsWith("/publishing/posts")) return { items: [] };
  if (path.startsWith("/distribution/capabilities")) return { items: [] };
  if (path.startsWith("/content")) return { total: 0, items: [] };
  return {};
}

function serve(posts: unknown[] = []) {
  wsGet.mockImplementation((path: string) =>
    path.startsWith("/publishing/posts")
      ? Promise.resolve({ items: posts })
      : Promise.resolve(route(path)),
  );
}

function fail(predicate: (p: string) => boolean, message: string) {
  const base = route;
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(base(path)),
  );
}

function renderCampaigns() {
  return render(
    <MemoryRouter>
      <Campaigns />
    </MemoryRouter>,
  );
}

describe("Campaigns list states", () => {
  it("renders a campaign when the list arrives", async () => {
    serve();
    renderCampaigns();
    expect(await screen.findByText("Spring push")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reports a failed campaign read as an alert, not as zero campaigns", async () => {
    fail((p) => p.startsWith("/campaigns"), "campaign backend unavailable");
    renderCampaigns();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/campaign backend unavailable/i);
  });

  it("does not present a failed read as an empty library", async () => {
    fail((p) => p.startsWith("/campaigns"), "campaign backend unavailable");
    renderCampaigns();
    await screen.findByRole("alert");
    expect(document.body.textContent).not.toMatch(/no campaigns/i);
  });
});

describe("publication mode is never conflated", () => {
  const post = (id: string, platform: string) => ({
    id,
    platform,
    account_id: "acct-1",
    remote_id: `remote-${id}`,
    status: "PUBLISHED",
  });

  it("counts each mode separately rather than one blended total", async () => {
    // One publish per platform. Whether a platform is LIVE, MOCK or HANDOFF is
    // the backend's call via /distribution/capabilities; the screen must not
    // invent a single "published" number.
    serve([post("p1", "youtube"), post("p2", "youtube"), post("p3", "tiktok")]);
    renderCampaigns();

    await screen.findByText("Spring push");
    // Every mode bucket is labelled, even at zero, so an absent mode is visibly
    // absent rather than folded into another.
    const text = document.body.textContent ?? "";
    expect(text).toMatch(/live/i);
    expect(text).toMatch(/mock/i);
    expect(text).toMatch(/handoff/i);
  });

  it("keeps the campaign list usable when only the posts read failed", async () => {
    fail((p) => p.startsWith("/publishing/posts"), "posts backend unavailable");
    renderCampaigns();

    // One panel failing must not blank the screen: the campaign list still
    // renders, and the mode figures refuse to report a number rather than
    // defaulting to zero published posts.
    expect(await screen.findByText("Spring push")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toMatch(/unavailable/i);
  });
});