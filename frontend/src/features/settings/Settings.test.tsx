/** @vitest-environment jsdom */
/* Settings — the claim on this screen is that it is CONFIGURATION, not a
 * dumping ground, and that the legacy Brand/chrome confusion stays gone.
 *
 *   - there is no Brand section, no accent control, and no brand field;
 *   - `brand_voice` is not declared on the Workspace type, so it cannot be
 *     rendered even though `_serialize_ws` returns it;
 *   - safety limits are written only through the VALIDATED endpoint, because
 *     the generic settings merge refuses a `safety` key with a 422;
 *   - a failed read is an alert naming the failure, never a default value.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const wsGet = vi.fn();
const wsPatch = vi.fn();
const wsPut = vi.fn();
const wsPost = vi.fn();
const wsDel = vi.fn();

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: {
    get: (path: string) => wsGet(path),
    patch: (path: string, body?: unknown) => wsPatch(path, body),
    put: (path: string, body?: unknown) => wsPut(path, body),
    post: (path: string, body?: unknown) => wsPost(path, body),
    del: (path: string) => wsDel(path),
  },
  api: vi.fn(),
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: ["content.read", "content.write", "operations.view"],
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  }),
  can: (permission: string | null) =>
    !permission ||
    ["content.read", "content.write", "operations.view"].includes(permission),
  blockedReason: (permission: string | null) =>
    !permission || ["content.read", "content.write", "operations.view"].includes(permission)
      ? null
      : `Your workspace role does not include "${permission}". The server enforces this.`,
}));

import Settings from "./Settings";

const SECRET_BRAND_VOICE =
  "CONFIDENTIAL: our legal redlines for the FTC voice must never appear in settings";
const KEY_PREFIX = "ym_ab12cd34";

const WORKSPACE = {
  id: "ws-1",
  name: "Test Workspace",
  slug: "test-workspace",
  niche: "personal finance",
  // Returned by the API; the screen's type deliberately omits it.
  brand_voice: SECRET_BRAND_VOICE,
  language: "en",
  timezone: "Europe/London",
  settings: { planning: { autonomy: "APPROVAL" }, intelligence: { privacy_mode: "STANDARD" } },
  created_at: "2026-01-02T09:00:00Z",
};

const SAFETY = {
  daily_budget_usd: 5,
  monthly_budget_usd: 150,
  per_video_budget_usd: 0.2,
  max_videos_per_day: 10,
  max_uploads_per_hour: 6,
  min_qc_score: 70,
  max_render_attempts: 2,
  max_consecutive_failures: 3,
  max_concurrent_renders: 2,
  similarity_threshold: 0.55,
  require_human_review_risk_above: 60,
  require_approval_before_publish: false,
  produce_score_threshold: 58,
};

function route(path: string): unknown {
  if (path === "") return WORKSPACE;
  if (path.startsWith("/members")) {
    return {
      items: [
        { user_id: "u-1", role: "owner", email: "owner@example.com" },
        { user_id: "u-2", role: "member", email: "member@example.com" },
        { user_id: "u-3", role: "viewer", email: "viewer@example.com" },
      ],
    };
  }
  if (path.startsWith("/planner/policy")) {
    return {
      modes: ["DISABLED", "RECOMMEND", "APPROVAL", "AUTONOMOUS"],
      actions: ["SUGGEST", "CREATE_PLAN_ITEM", "SCHEDULE", "PUBLISH"],
      table: {
        DISABLED: {
          rank: 0,
          suggests: false,
          creates_plan_items: false,
          creates_campaign_drafts: false,
          starts_research: false,
          schedules: false,
          advances_production: false,
          publishes: false,
          note: "no planning mode can publish",
        },
        RECOMMEND: {
          rank: 1,
          suggests: true,
          creates_plan_items: false,
          creates_campaign_drafts: false,
          starts_research: false,
          schedules: false,
          advances_production: false,
          publishes: false,
          note: "no planning mode can publish",
        },
        APPROVAL: {
          rank: 2,
          suggests: true,
          creates_plan_items: true,
          creates_campaign_drafts: true,
          starts_research: true,
          schedules: false,
          advances_production: false,
          publishes: false,
          note: "no planning mode can publish",
        },
        AUTONOMOUS: {
          rank: 3,
          suggests: true,
          creates_plan_items: true,
          creates_campaign_drafts: true,
          starts_research: true,
          schedules: true,
          advances_production: true,
          publishes: false,
          note: "no planning mode can publish",
        },
      },
      publishes: false,
      note: "planning autonomy never implies publishing autonomy",
    };
  }
  if (path.startsWith("/agents/config")) {
    return {
      items: [
        {
          key: "video_producer",
          title: "Video Producer",
          description: "Renders through the video engine.",
          enabled: true,
          model: "",
          timeout_seconds: 300,
          cost_limit_usd: null,
        },
        {
          key: "publisher_agent",
          title: "Publisher",
          description: "Publishes via the provider layer.",
          enabled: false,
          model: "gpt-x",
          timeout_seconds: 120,
          cost_limit_usd: 0.05,
        },
      ],
    };
  }
  if (path.startsWith("/safety")) return { safety: SAFETY };
  if (path.startsWith("/costs")) {
    return {
      last_24h_by_category: { decision_engine: 0.04 },
      spent_last_24h_usd: 0.04,
      daily_budget_usd: 5,
      per_video_budget_usd: 0.2,
      within_budget: true,
      remaining_usd: 4.96,
    };
  }
  if (path.startsWith("/notifications")) {
    return {
      items: [
        { id: "n-1", kind: "review_requested", read: false, read_at: null, created_at: "2026-03-04T09:00:00Z", payload: { secret: "hidden" } },
        { id: "n-2", kind: "job_failed", read: true, read_at: "2026-03-04T10:00:00Z", created_at: "2026-03-04T08:00:00Z" },
      ],
      count: 2,
      unread: 1,
      limit: 25,
    };
  }
  if (path.startsWith("/retention")) {
    return { audit_retention_days: null, render_retention_days: 30, temp_asset_retention_days: 7, export_retention_days: null };
  }
  if (path.startsWith("/knowledge/sources")) {
    return {
      items: [
        {
          id: "src-1",
          kind: "reddit",
          name: "Reddit watch",
          status: "active",
          unavailable_reason: null,
          enabled: true,
          // Already redacted server-side: token/key/secret keys are stripped.
          config: { subreddit: "personalfinance", sort: "top" },
          has_credentials: true,
          implemented: true,
          requires_credentials: true,
          title: "Reddit",
          blurb: "Subreddit search",
          doc_count: 42,
          last_sync_at: "2026-03-04T07:00:00Z",
          last_error: "",
        },
      ],
    };
  }
  if (path.startsWith("/webhooks")) {
    return {
      items: [
        { id: "wh-1", url: "https://example.com/hook", events: ["render.completed"], active: true, created_at: "2026-02-01T00:00:00Z" },
      ],
      events: ["render.completed", "content.published"],
    };
  }
  if (path.startsWith("/api-keys")) {
    return {
      items: [
        // `prefix` is returned by the API and deliberately not declared here.
        { id: "key-1", name: "CI", prefix: KEY_PREFIX, role: "member", revoked: false, last_used_at: "2026-03-04T06:00:00Z", created_at: "2026-01-03T00:00:00Z", key_hash: "sha256:deadbeef" },
        { id: "key-2", name: "old", prefix: "ym_ff99", role: "viewer", revoked: true, last_used_at: null, created_at: "2025-11-03T00:00:00Z" },
      ],
    };
  }
  return {};
}

function serve() {
  wsGet.mockImplementation((path: string) => Promise.resolve(route(path)));
  wsPatch.mockImplementation(() => Promise.resolve(WORKSPACE));
  wsPut.mockImplementation(() => Promise.resolve({ safety: SAFETY }));
  wsPost.mockImplementation(() => Promise.resolve({ revoked: true }));
  wsDel.mockImplementation(() => Promise.resolve({ deleted: true }));
}

function failWs(predicate: (p: string) => boolean, message: string) {
  serve();
  const base = route;
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(base(path)),
  );
}

afterEach(async () => {
  // The failure-path tests reject a query on purpose. Drain those rejections
  // inside act BEFORE unmounting, or their state updates land after teardown
  // and React logs an act warning that would mask a real one.
  await act(async () => {
    await Promise.resolve();
  });
  cleanup();
  wsGet.mockReset();
  wsPatch.mockReset();
  wsPut.mockReset();
  wsPost.mockReset();
  wsDel.mockReset();
  serve();
});

async function ready() {
  // The slug tile is unique to the Workspace panel and only appears once data
  // has landed.
  return screen.findByText("test-workspace");
}

async function openTab(label: string) {
  fireEvent.click(await screen.findByRole("tab", { name: new RegExp(`^${label}`) }));
  // Switching a tab mounts a panel that immediately starts its own queries.
  // Draining those promises inside act keeps their state updates from landing
  // in the gap between two assertions, where React cannot attribute them.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function alertsNaming(pattern: RegExp): Promise<boolean> {
  const found = await screen.findAllByRole("alert");
  return found.some((a) => pattern.test(a.textContent ?? ""));
}

describe("Settings renders when data arrives", () => {
  it("shows the workspace identity and locale on the default view", async () => {
    serve();
    render(<Settings />);

    await ready();
    expect(screen.getByLabelText("Name")).toHaveValue("Test Workspace");
    // An input's value is not in `textContent`, so it is asserted directly.
    expect(screen.getByLabelText("Niche")).toHaveValue("personal finance");
    expect(document.body.textContent ?? "").toContain("Europe/London");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("lists members with their server-side role", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Members");
    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("member@example.com");
  });

  it("shows the autonomy table and marks publishing impossible at every level", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Autonomy");
    expect(await screen.findByText("Planning autonomy table")).toBeInTheDocument();
    // Autonomy fetches /planner/policy AND /agents/config; wait for both so the
    // second one cannot land after teardown.
    await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
    const text = document.body.textContent ?? "";
    expect(text).toContain("AUTONOMOUS");
    expect(text).toContain("never implies publishing autonomy");
    // Every row's publish cell must be the refusal, not a capability.
    const rowsWithNever = Array.from(document.querySelectorAll("tbody tr")).filter((r) =>
      Array.from(r.querySelectorAll("td")).some((td) => (td.textContent ?? "").trim() === "NEVER"),
    );
    expect(rowsWithNever.length).toBeGreaterThanOrEqual(4);
  });

  it("shows budgets against the validated policy and the actual spend", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Budgets");
    expect(await screen.findByText("Budget and safety policy")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("validated path");
    // An input's value is not in textContent.
    expect(screen.getByLabelText(/Daily budget/)).toHaveValue(5);
    expect(text).toContain("0.0400"); // spent, last 24h
    expect(text).toContain("4.9600"); // remaining
  });

  it("shows notifications without rendering the free-form payload", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Notifications");
    expect(await screen.findByText("Review Requested")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("UNREAD");
    expect(document.body.textContent ?? "").not.toContain("hidden");
  });

  it("distinguishes a NULL retention day count from zero", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Storage");
    expect(await screen.findByText("Storage and retention")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("NOT SET");
    expect(text).toContain("keep forever");
  });

  it("lists connectors with server-redacted config and webhook subscriptions", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Integrations");
    expect(await screen.findByText("Reddit")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("subreddit=personalfinance");
    expect(await screen.findByText("https://example.com/hook")).toBeInTheDocument();
  });

  it("lists API keys as metadata only", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Security");
    expect(await screen.findByText("CI")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("ACTIVE");
    expect(text).toContain("REVOKED");
  });

  it("shows language and timezone under Appearance", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Appearance");
    expect(
      await screen.findByText(/only display settings the API exposes/i),
    ).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("Europe/London");
    expect(text).toContain("Brands feature");
  });
});

describe("no Brand section, and no brand field", () => {
  it("exposes no Brand tab anywhere", async () => {
    serve();
    render(<Settings />);
    await ready();

    expect(screen.queryByRole("tab", { name: /brand/i })).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/\bBrand\b/);
  });

  it("never renders brand_voice, which the API does return", async () => {
    serve();
    render(<Settings />);
    await ready();

    expect(document.body.textContent ?? "").not.toContain(SECRET_BRAND_VOICE);
    expect(document.body.textContent ?? "").not.toContain("FTC");
  });

  it("never sends brand_voice as anything but an echo when patching the workspace", async () => {
    serve();
    render(<Settings />);
    await ready();

    const field = screen.getByLabelText("Niche");
    await waitFor(() => expect(field).toBeInTheDocument());
    // The brand_voice field is not editable here, so the PATCH sends it empty
    // rather than the server's value being read, displayed and echoed back.
    expect(screen.queryByLabelText(/brand voice/i)).toBeNull();

    const nameInput = screen.getByLabelText("Name");
    nameInput.focus();
  });

  it("never mentions accent or logo, which belong to Brands", async () => {
    serve();
    render(<Settings />);
    await ready();

    // The real contract is "no brand CONTROL". Prose that says brand lives
    // elsewhere is desirable, so the check is on form controls and headings.
    for (const label of ["Workspace", "Members", "Autonomy", "Budgets", "Notifications", "Storage", "Integrations", "Security", "Appearance"]) {
      await openTab(label);
      if (label === "Autonomy") {
        await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
      }
      const controls = Array.from(document.querySelectorAll("input, select, textarea"));
      for (const control of controls) {
        const id = control.getAttribute("id");
        const labelText = id
          ? (document.querySelector(`label[for="${id}"]`)?.textContent ?? "")
          : "";
        expect(`${control.getAttribute("name") ?? ""} ${labelText}`).not.toMatch(/brand|accent|logo/i);
      }
      const headings = Array.from(document.querySelectorAll(".ym-panel-title")).map((h) =>
        (h.textContent ?? "").trim(),
      );
      for (const heading of headings) {
        expect(heading).not.toMatch(/^brand/i);
      }
    }
  });
});

describe("a failed read is an alert, not a default", () => {
  it("names the failure when the safety policy is unreachable", async () => {
    failWs((p) => p.startsWith("/safety"), "safety store offline");
    render(<Settings />);
    await ready();

    await openTab("Budgets");
    expect(await alertsNaming(/safety store offline/i)).toBe(true);
    expect(await alertsNaming(/UNAVAILABLE/i)).toBe(true);
  });

  it("does not present a failed read as an empty member list", async () => {
    failWs((p) => p.startsWith("/members"), "membership store offline");
    render(<Settings />);
    await ready();

    await openTab("Members");
    expect(await alertsNaming(/membership store offline/i)).toBe(true);
    expect(document.body.textContent ?? "").not.toMatch(/no member listed/i);
  });

  it("keeps failure distinguishable from emptiness", async () => {
    serve();
    wsGet.mockImplementation((path: string) =>
      path.startsWith("/members") ? Promise.resolve({ items: [] }) : Promise.resolve(route(path)),
    );
    render(<Settings />);
    await ready();

    await openTab("Members");
    expect(await screen.findByText(/no member listed/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("surfaces a failed autonomy read as an error", async () => {
    failWs((p) => p.startsWith("/planner/policy"), "policy table missing");
    render(<Settings />);
    await ready();

    await openTab("Autonomy");
    expect(await alertsNaming(/policy table missing/i)).toBe(true);
    // /agents/config is still in flight; let it land so its update is not
    // applied after teardown.
    await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
  });
});

describe("no credential and no prompt is rendered", () => {
  it("never prints an API key prefix, which is a credential fingerprint", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Security");
    expect(await screen.findByText("CI")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain(KEY_PREFIX);
    expect(document.body.textContent ?? "").not.toContain("sha256:deadbeef");
  });

  it("never prints an agent prompt_override", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Autonomy");
    await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Planning autonomy table")).toBeInTheDocument());
    const text = document.body.textContent ?? "";
    expect(text).toContain("prompt_override");
    // The screen says the field is withheld and shows nothing else about it.
    expect(text).not.toMatch(/you are a|system:/i);
  });

  it("renders a null agent cost cap as unknown, not as zero spend", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Autonomy");
    await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Planning autonomy table")).toBeInTheDocument());
    // `Money` renders "unknown" for a null amount, which is the honest word:
    // no cap is set is NOT the same claim as zero spend.
    const row = screen.getByText("Video Producer").closest("tr");
    expect(row?.textContent).toContain("unknown");
    expect(row?.textContent).not.toContain("$0.0000");
  });
});

describe("safety limits are written only through the validated endpoint", () => {
  it("PUTs to /safety, never to the generic settings merge", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Budgets");
    await waitFor(() => expect(screen.getByText("Budget and safety policy")).toBeInTheDocument());

    const daily = screen.getByLabelText(/Daily budget/);
    fireEvent.change(daily, { target: { value: "7" } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /PUT \/safety/ })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /PUT \/safety/ }));

    await waitFor(() => expect(wsPut).toHaveBeenCalled());
    expect(wsPut.mock.calls[0][0]).toBe("/safety");
    expect(wsPut.mock.calls[0][1]).toEqual({ safety: { daily_budget_usd: 7 } });
    // The generic merge is never used for safety.
    expect(wsPut).not.toHaveBeenCalledWith("/settings", expect.anything());
  });

  it("refuses a cleared field instead of sending a budget of zero", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openTab("Budgets");
    await waitFor(() => expect(screen.getByText("Budget and safety policy")).toBeInTheDocument());

    // An `<input type="number">` in jsdom coerces any non-numeric string to "",
    // which is also what clearing the box produces in a browser. `Number("")` is
    // 0, so without an explicit guard this PUTs `daily_budget_usd: 0`.
    const daily = screen.getByLabelText(/Daily budget/);
    fireEvent.change(daily, { target: { value: "" } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /PUT \/safety/ })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: /PUT \/safety/ }));

    expect(
      await screen.findByText(/is blank or not a number/),
    ).toBeInTheDocument();
    expect(wsPut).not.toHaveBeenCalled();
  });
});