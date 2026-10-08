/** @vitest-environment jsdom */
/* Settings — vertical settings architecture.
 *
 *   - a fixed vertical sidebar (nav landmark + group headings), not tabs;
 *   - search filters sections/controls; a mobile select replaces the sidebar;
 *   - dirty edits mark the nav and the header save-state (Saved / Saving /
 *     Unsaved changes / Save failed), announced through a live region;
 *   - admin-only writes show their permission requirement and are disabled
 *     without it; the backend stays authoritative;
 *   - a 403 renders as a permission refusal with NO retry (QueryBoundary +
 *     PermissionAwareError already handle that);
 *   - the legacy guarantees hold: no Brand section, no brand_voice render,
 *     safety only through PUT /safety, no credential or prompt rendering.
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

const VIEWER_CAPS = ["content.read", "content.write", "operations.view"];
const ADMIN_CAPS = [...VIEWER_CAPS, "publish.approve", "publish.execute", "brand.manage", "providers.manage"];

const capsState: { caps: string[] } = { caps: [...VIEWER_CAPS] };
function setCaps(caps: string[]) {
  capsState.caps = caps;
}

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: capsState.caps,
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  }),
  can: (permission: string | null) =>
    !permission || capsState.caps.includes(permission),
  blockedReason: (permission: string | null) =>
    !permission || capsState.caps.includes(permission)
      ? null
      : `Your workspace role does not include "${permission}". The server enforces this.`,
}));

import Settings from "./Settings";

const SECRET_BRAND_VOICE =
  "CONFIDENTIAL: our legal redlines for the FTC voice must never appear in settings";
// Fixture stand-ins for values the screen must never render (key fingerprint,
// redacted connection fingerprint, shown-once mint artifacts, pairing code).
// Neutral wording: they assert ABSENCE or one-time display, never real auth.
const FP_A = "fp-alpha-fixture";
const FP_B = "fp-beta-fixture";
const MASK_A = "mask-alpha-fixture";
const ONCE_A = "once-alpha-fixture";
const ONCE_B = "once-beta-fixture";
const PAIR_A = "pair-alpha-fixture";

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

const RETENTION = {
  audit_retention_days: null,
  render_retention_days: 30,
  temp_asset_retention_days: 7,
  export_retention_days: null,
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
          rank: 0, suggests: false, creates_plan_items: false, creates_campaign_drafts: false,
          starts_research: false, schedules: false, advances_production: false,
          publishes: false, note: "no planning mode can publish",
        },
        RECOMMEND: {
          rank: 1, suggests: true, creates_plan_items: false, creates_campaign_drafts: false,
          starts_research: false, schedules: false, advances_production: false,
          publishes: false, note: "no planning mode can publish",
        },
        APPROVAL: {
          rank: 2, suggests: true, creates_plan_items: true, creates_campaign_drafts: true,
          starts_research: true, schedules: false, advances_production: false,
          publishes: false, note: "no planning mode can publish",
        },
        AUTONOMOUS: {
          rank: 3, suggests: true, creates_plan_items: true, creates_campaign_drafts: true,
          starts_research: true, schedules: true, advances_production: true,
          publishes: false, note: "no planning mode can publish",
        },
      },
      publishes: false,
      note: "planning autonomy never implies publishing autonomy",
    };
  }
  if (path.startsWith("/planner/calendar")) {
    return {
      entries: [],
      capacity: {
        declared: true, locale: "", longform_per_week: 1, shorts_per_day: 2,
        ugc_per_day: 0, localization_per_day: 0, render_hours_per_day: 1,
        review_slots_per_day: 2, notes: "",
      },
      committed: {},
      remaining: { shorts: 5 },
    };
  }
  if (path.startsWith("/agents/config")) {
    return {
      items: [
        {
          key: "video_producer", title: "Video Producer",
          description: "Renders through the video engine.",
          enabled: true, model: "", timeout_seconds: 300, cost_limit_usd: null,
        },
        {
          key: "publisher_agent", title: "Publisher",
          description: "Publishes via the provider layer.",
          enabled: false, model: "gpt-x", timeout_seconds: 120, cost_limit_usd: 0.05,
        },
      ],
    };
  }
  if (path.startsWith("/safety")) return { safety: SAFETY };
  if (path.startsWith("/costs/intelligence")) {
    return {
      total_cost_usd: 1.2, per_cycle_usd: 0.3, per_video_usd: 0.2,
      per_publication_usd: 0.4, cost_per_1000_views_usd: null,
      by_category: { decision_engine: 0.5 }, by_agent: { video_producer: 0.7 },
      publications_by_platform: { youtube: 2 },
      totals: { cycles: 4, videos_built: 6, posts_published: 3, views: 0 },
      estimated_return_usd: null,
    };
  }
  if (path.startsWith("/costs")) {
    return {
      last_24h_by_category: { decision_engine: 0.04 },
      spent_last_24h_usd: 0.04,
      spent_last_24h_unknown_exposure_rows: 0,
      daily_budget_usd: 5,
      per_video_budget_usd: 0.2,
      within_budget: true,
      remaining_usd: 4.96,
    };
  }
  if (path.startsWith("/publishing/accounts")) {
    return { items: [] };
  }
  if (path.startsWith("/calendar")) {
    return {
      items: [
        { id: "se-1", platform: "youtube", run_at: "2026-04-01T09:00:00Z", content_item_id: "ci-1", campaign_id: "c-1", status: "PENDING" },
      ],
    };
  }
  if (path.startsWith("/connections/tts")) {
    return { provider: "kokoro", healthy: true, voices: [], error: null };
  }
  if (path.startsWith("/connections/images")) {
    return { provider: "openai", healthy: true, error: null };
  }
  if (path.startsWith("/connections/video-engine")) {
    return {
      engine: "hyperframes", base_url: "http://localhost:9000", timeout_seconds: 600,
      sources: ["local"], healthy: true, version: "1.2.3", capabilities: ["subtitles"],
    };
  }
  if (path.startsWith("/connections")) {
    return {
      items: [
        // `masked` is returned by the API and deliberately not declared.
        { key: "llm.api_key", label: "LLM API key", secret: true, hint: "", configured: true, source: "workspace", masked: MASK_A },
        { key: "llm.model", label: "LLM model", secret: false, hint: "", configured: true, source: "env", masked: "gpt-x" },
      ],
    };
  }
  if (path.startsWith("/music/providers")) {
    return { items: [{ key: "m1", available: true, detail: "" }], available: ["m1"] };
  }
  if (path.startsWith("/music/policy")) {
    return {
      generate: false, reason: "nobody opted in", brand_disabled: false,
      provider_key: "", configured: false, forbidden_genres: [], prefs: {},
    };
  }
  if (path.startsWith("/retention")) return RETENTION;
  if (path.startsWith("/assets")) return { items: [] };
  if (path.startsWith("/notifications")) {
    return {
      items: [
        { id: "n-1", kind: "review_requested", read: false, read_at: null, created_at: "2026-03-04T09:00:00Z", payload: { note: "hidden" } },
        { id: "n-2", kind: "job_failed", read: true, read_at: "2026-03-04T10:00:00Z", created_at: "2026-03-04T08:00:00Z" },
      ],
      count: 2,
      unread: 1,
      limit: 25,
    };
  }
  if (path.startsWith("/telegram/status")) {
    return {
      bot_configured: true, token_source: "env", linked: true,
      links: [{ id: "l-1", chat_id: "123", chat_title: "Ops", active: true, linked_at: "2026-03-01T00:00:00Z" }],
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
  if (path.startsWith("/knowledge/sources")) {
    return {
      items: [
        {
          id: "src-1", kind: "reddit", name: "Reddit watch", status: "active",
          unavailable_reason: null, enabled: true,
          config: { subreddit: "personalfinance", sort: "top" },
          has_credentials: true, implemented: true, requires_credentials: true,
          title: "Reddit", blurb: "Subreddit search", doc_count: 42,
          last_sync_at: "2026-03-04T07:00:00Z", last_error: "",
        },
      ],
    };
  }
  if (path.startsWith("/trend-sources")) {
    return {
      items: [
        { id: "ts-1", kind: "google_trends", name: "Trends RSS", enabled: true, priority: 50 },
      ],
    };
  }
  if (path.startsWith("/api-keys")) {
    return {
      items: [
        // `prefix` is returned by the API and deliberately not declared here.
        { id: "key-1", name: "CI", prefix: FP_A, role: "member", revoked: false, last_used_at: "2026-03-04T06:00:00Z", created_at: "2026-01-03T00:00:00Z" },
        { id: "key-2", name: "old", prefix: FP_B, role: "viewer", revoked: true, last_used_at: null, created_at: "2025-11-03T00:00:00Z" },
      ],
    };
  }
  if (path.startsWith("/decision")) {
    return { action: "PRODUCE", reasons: ["score above threshold"], blockers: [] };
  }
  return {};
}

function serve() {
  wsGet.mockImplementation((path: string) => Promise.resolve(route(path)));
  wsPatch.mockImplementation(() => Promise.resolve(WORKSPACE));
  wsPut.mockImplementation((path: string) => {
    if (path === "/safety") return Promise.resolve({ safety: SAFETY });
    if (path === "/retention") return Promise.resolve(RETENTION);
    if (path === "/music/policy") return Promise.resolve(route("/music/policy"));
    if (path === "/connections/video-engine") return Promise.resolve(route("/connections/video-engine"));
    return Promise.resolve({ ok: true });
  });
  wsPost.mockImplementation((path: string) => {
    if (path === "/api-keys") return Promise.resolve({ id: "key-9", api_key: ONCE_A, name: "CI" });
    if (path === "/webhooks") return Promise.resolve({ id: "wh-9", secret: ONCE_B });
    if (path === "/telegram/pairing-code") return Promise.resolve({ code: PAIR_A, expires_in_seconds: 900 });
    if (path === "/telegram/test") return Promise.resolve({ sent: 2 });
    if (path === "/telegram/links/l-1/toggle") return Promise.resolve({ id: "l-1", active: false });
    if (path === "/connections/test-llm") return Promise.resolve({ ok: true, mode: "real", detail: "connected (3 models visible)" });
    if (path === "/connections/test-publishing") return Promise.resolve({ ok: true, detail: "relay ok" });
    if (path.endsWith("/sync")) return Promise.resolve({ job_id: "job-1", queued: true });
    if (path === "/planner/capacity") return Promise.resolve({ workspace_id: "ws-1" });
    if (path === "/publishing/accounts") return Promise.resolve({ id: "acc-1" });
    return Promise.resolve({ ok: true });
  });
  wsDel.mockImplementation(() => Promise.resolve({ deleted: true }));
}

function failWs(predicate: (p: string) => boolean, error: unknown) {
  serve();
  const base = route;
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(error) : Promise.resolve(base(path)),
  );
}

afterEach(async () => {
  await act(async () => {
    await Promise.resolve();
  });
  cleanup();
  wsGet.mockReset();
  wsPatch.mockReset();
  wsPut.mockReset();
  wsPost.mockReset();
  wsDel.mockReset();
  setCaps([...VIEWER_CAPS]);
  serve();
});

async function ready() {
  // The slug stat is unique to the Workspace section and only appears once
  // data has landed.
  return screen.findByText("test-workspace");
}

function saveState() {
  return screen.getByRole("status", { name: "Save state" });
}

async function openSection(label: string) {
  fireEvent.click(screen.getByRole("button", { name: label }));
  // Switching a section mounts a panel that immediately starts its own
  // queries. Draining those promises inside act keeps their state updates
  // from landing in the gap between two assertions.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function alertsNaming(pattern: RegExp): Promise<boolean> {
  const found = await screen.findAllByRole("alert");
  return found.some((a) => pattern.test(a.textContent ?? ""));
}

describe("vertical nav, not tabs", () => {
  it("renders a sidebar nav with category groups and no tablist", async () => {
    serve();
    render(<Settings />);
    await ready();

    expect(screen.getByRole("navigation", { name: "Settings sections" })).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
    for (const group of ["General", "Team & Access", "Autonomy", "Budgets & Costs", "Publishing", "AI & Generation", "Storage & Media", "Notifications", "Integrations"]) {
      expect(screen.getByText(group)).toBeInTheDocument();
    }
    // The active section button carries the current state; the default is Workspace.
    expect(screen.getByRole("button", { name: "Workspace" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByLabelText("Name")).toHaveValue("Test Workspace");
  });

  it("moves between sections through the nav", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("Members");
    expect(await screen.findByText("owner@example.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Members" })).toHaveAttribute("aria-current", "true");
    // The previous panel unmounts: its controls leave with it.
    expect(screen.queryByLabelText("Name")).toBeNull();
  });

  it("exposes the same sections through the mobile select", async () => {
    serve();
    render(<Settings />);
    await ready();

    const select = screen.getByLabelText("Settings section") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "telegram" } });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(await screen.findByText("Bot configured")).toBeInTheDocument();
  });
});

describe("settings search", () => {
  it("filters sections by name, blurb and control keywords", async () => {
    serve();
    render(<Settings />);
    await ready();

    fireEvent.change(screen.getByLabelText("Search settings"), { target: { value: "telegram" } });
    expect(await screen.findByText(/1 section matches/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Telegram" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Workspace" })).toBeNull();

    // Control keywords match too: "pairing" is a Telegram keyword.
    fireEvent.change(screen.getByLabelText("Search settings"), { target: { value: "pairing" } });
    expect(screen.getByRole("button", { name: "Telegram" })).toBeInTheDocument();
  });

  it("says so when nothing matches and recovers on clear", async () => {
    serve();
    render(<Settings />);
    await ready();

    fireEvent.change(screen.getByLabelText("Search settings"), { target: { value: "zzz-no-such-setting" } });
    expect(await screen.findByText("No section matches.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search settings"), { target: { value: "" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Workspace" })).toBeInTheDocument());
  });
});

describe("dirty state and save state", () => {
  it("discards a section draft and its dirty bookkeeping on navigation", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();
    fireEvent.change(screen.getByLabelText("Niche"), { target: { value: "discard me" } });
    expect(saveState()).toHaveTextContent("Unsaved changes");
    await openSection("Members");
    expect(saveState()).toHaveTextContent("No unsaved changes");
    expect(document.querySelector(".ymset-dirtydot")).toBeNull();
    await openSection("Workspace");
    expect(screen.getByLabelText("Niche")).toHaveValue("personal finance");
  });

  it.each(["resolve", "reject"])("settles an in-flight save after leaving its section (%s)", async (outcome) => {
    setCaps([...ADMIN_CAPS]);
    serve();
    let resolve!: (value: unknown) => void;
    let reject!: (error: Error) => void;
    wsPatch.mockImplementation(() => new Promise((yes, no) => { resolve = yes; reject = no; }));
    render(<Settings />);
    await ready();
    fireEvent.change(screen.getByLabelText("Niche"), { target: { value: "save me" } });
    fireEvent.click(screen.getByRole("button", { name: "Save workspace" }));
    expect(saveState()).toHaveTextContent("Saving");
    await openSection("Members");
    expect(saveState()).toHaveTextContent("Saving");
    await act(async () => {
      if (outcome === "resolve") resolve(WORKSPACE);
      else reject(new Error("save refused after navigation"));
    });
    expect(saveState()).not.toHaveTextContent("Saving");
    expect(saveState()).not.toHaveTextContent("Unsaved changes");
    expect(document.querySelector(".ymset-dirtydot")).toBeNull();
    if (outcome === "reject") expect(saveState()).toHaveTextContent("save refused after navigation");
  });

  it("marks edits unsaved in the nav and the live region, then saved", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    expect(saveState().textContent ?? "").toMatch(/No unsaved changes/);

    fireEvent.change(screen.getByLabelText("Niche"), { target: { value: "travel hacking" } });
    await waitFor(() => expect(saveState().textContent ?? "").toMatch(/Unsaved changes/));
    expect(document.querySelector(".ymset-dirtydot")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Save workspace" }));
    await waitFor(() => expect(wsPatch).toHaveBeenCalled());
    expect(wsPatch.mock.calls[0][0]).toBe("");
    expect(wsPatch.mock.calls[0][1]).toEqual({ name: "Test Workspace", niche: "travel hacking", brand_voice: "" });
    await waitFor(() => expect(saveState().textContent ?? "").toMatch(/All changes saved/));
    expect(document.querySelector(".ymset-dirtydot")).toBeNull();
  });

  it("tracks a safety draft as unsaved until the validated save lands", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Budgets & Safety");
    const daily = screen.getByLabelText(/Daily budget/);
    fireEvent.change(daily, { target: { value: "7" } });
    await waitFor(() => expect(saveState().textContent ?? "").toMatch(/Unsaved changes/));

    fireEvent.click(screen.getByRole("button", { name: "Save safety policy" }));
    await waitFor(() => expect(wsPut).toHaveBeenCalled());
    expect(wsPut.mock.calls[0][0]).toBe("/safety");
    expect(wsPut.mock.calls[0][1]).toEqual({ safety: { daily_budget_usd: 7 } });
    await waitFor(() => expect(saveState().textContent ?? "").toMatch(/All changes saved/));
  });
});

describe("permission gating", () => {
  it("disables admin writes and names the requirement for a non-admin", async () => {
    serve();
    render(<Settings />);
    await ready();

    expect(screen.getByRole("button", { name: "Save workspace" })).toBeDisabled();
    expect(document.body.textContent ?? "").toMatch(/Requires an admin role/);

    await openSection("Budgets & Safety");
    expect(screen.getByRole("button", { name: "Save safety policy" })).toBeDisabled();
  });

  it("enables admin writes for an admin", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    fireEvent.change(screen.getByLabelText("Niche"), { target: { value: "x" } });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save workspace" })).not.toBeDisabled(),
    );
  });

  it("gates the admin-only connections read behind the providers capability", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("Connections & Keys");
    // The query never fires without the capability; the refusal is stated in
    // prose instead of as a failed read.
    expect(await screen.findByText(/Credential status needs an admin role/)).toBeInTheDocument();
    expect(wsGet).not.toHaveBeenCalledWith("/connections");
  });
});

describe("a 403 is a refusal with no retry", () => {
  it("renders a permission denial without a Retry button", async () => {
    const { ApiError } = await import("../../lib/api");
    failWs(
      (p) => p.startsWith("/members"),
      new ApiError(403, "not a workspace member"),
    );
    render(<Settings />);
    await ready();

    await openSection("Members");
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toMatch(/not a workspace member/);
    // PermissionAwareError renders no retry for a refusal: retrying a 403 can
    // never succeed.
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });
});

describe("a failed read is an alert, not a default", () => {
  it("names the failure when the safety policy is unreachable", async () => {
    failWs((p) => p.startsWith("/safety"), new Error("safety store offline"));
    render(<Settings />);
    await ready();

    await openSection("Budgets & Safety");
    expect(await alertsNaming(/safety store offline/i)).toBe(true);
  });

  it("does not present a failed read as an empty member list", async () => {
    failWs((p) => p.startsWith("/members"), new Error("membership store offline"));
    render(<Settings />);
    await ready();

    await openSection("Members");
    expect(await alertsNaming(/membership store offline/i)).toBe(true);
    expect(document.body.textContent ?? "").not.toMatch(/no member listed/i);
  });
});

describe("no Brand section, and no brand field", () => {
  it("exposes no brand control anywhere", async () => {
    serve();
    render(<Settings />);
    await ready();

    expect(screen.queryByRole("button", { name: /brand/i })).toBeNull();
    for (const section of ["Workspace", "Locale & Display", "Members", "API Keys", "Autonomy Policy", "Agents", "Capacity", "Budgets & Safety", "Cost Analytics", "Accounts", "Scheduling", "Providers", "Music", "Retention & Assets", "Inbox", "Telegram", "Connections & Keys", "Webhooks", "Knowledge Sources", "Trend Sources"]) {
      await openSection(section);
      const controls = Array.from(document.querySelectorAll("input, select, textarea"));
      for (const control of controls) {
        const id = control.getAttribute("id");
        const labelText = id
          ? (document.querySelector(`label[for="${id}"]`)?.textContent ?? "")
          : "";
        expect(`${control.getAttribute("name") ?? ""} ${labelText}`).not.toMatch(/brand|accent|logo/i);
      }
    }
  });

  it("never renders brand_voice, which the API does return", async () => {
    serve();
    render(<Settings />);
    await ready();

    expect(document.body.textContent ?? "").not.toContain(SECRET_BRAND_VOICE);
    expect(document.body.textContent ?? "").not.toContain("FTC");
  });
});

describe("no credential and no prompt is rendered", () => {
  it("never prints an API key prefix, which is a credential fingerprint", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("API Keys");
    expect(await screen.findByText("CI")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain(FP_A);
    expect(document.body.textContent ?? "").not.toContain(FP_B);
  });

  it("never prints connection masked values", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Connections & Keys");
    expect(await screen.findByText("LLM API key")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain(MASK_A);
  });

  it("withholds the agent prompt_override", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("Agents");
    await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
    const text = document.body.textContent ?? "";
    expect(text).toContain("prompt_override");
    expect(text).not.toMatch(/you are a|system:/i);
  });

  it("renders a null agent cost cap as unknown, not as zero spend", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("Agents");
    await waitFor(() => expect(screen.getByText("Video Producer")).toBeInTheDocument());
    const row = screen.getByText("Video Producer").closest("tr");
    expect(row?.textContent).toContain("unknown");
    expect(row?.textContent).not.toContain("$0.0000");
  });
});

describe("safety limits are written only through the validated endpoint", () => {
  it("PUTs to /safety, never to the generic settings merge", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Budgets & Safety");
    const daily = screen.getByLabelText(/Daily budget/);
    fireEvent.change(daily, { target: { value: "7" } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save safety policy" })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save safety policy" }));

    await waitFor(() => expect(wsPut).toHaveBeenCalled());
    expect(wsPut.mock.calls[0][0]).toBe("/safety");
    expect(wsPut.mock.calls[0][1]).toEqual({ safety: { daily_budget_usd: 7 } });
    expect(wsPut).not.toHaveBeenCalledWith("/settings", expect.anything());
  });

  it("refuses a cleared field instead of sending a budget of zero", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Budgets & Safety");
    const daily = screen.getByLabelText(/Daily budget/);
    fireEvent.change(daily, { target: { value: "" } });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save safety policy" })).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save safety policy" }));

    expect(await screen.findByText(/is blank or not a number/)).toBeInTheDocument();
    expect(wsPut).not.toHaveBeenCalled();
  });
});

describe("new coverage the old tabs omitted", () => {
  it("shows the telegram status, links and pairing flow", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Telegram");
    expect(await screen.findByText("Bot configured")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("Ops");

    fireEvent.click(screen.getByRole("button", { name: "Generate pairing code" }));
    expect(await screen.findByText(PAIR_A)).toBeInTheDocument();
    expect(wsPost).toHaveBeenCalledWith("/telegram/pairing-code", undefined);
  });

  it("writes a credential through PUT /connections", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Connections & Keys");
    expect(await screen.findByText("LLM API key")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "llm.api_key" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: " replacement-value " } });
    fireEvent.click(screen.getByRole("button", { name: "Save credential" }));

    await waitFor(() => expect(wsPut).toHaveBeenCalled());
    const call = wsPut.mock.calls.find((c) => c[0] === "/connections");
    expect(call?.[1]).toEqual({ key: "llm.api_key", value: " replacement-value " });
  });

  it("shows the webhook signing artifact exactly once at subscribe time", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Webhooks");
    expect(await screen.findByText("https://example.com/hook")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://ops.example.com/hook" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "render.completed" }));
    fireEvent.click(screen.getByRole("button", { name: "Subscribe" }));

    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/webhooks", {
      url: "https://ops.example.com/hook",
      events: ["render.completed"],
    }));
    expect(await screen.findByText(ONCE_B)).toBeInTheDocument();
  });

  it("shows the minted API key artifact exactly once at mint time", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("API Keys");
    expect(await screen.findByText("CI")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "CI runner" } });
    fireEvent.click(screen.getByRole("button", { name: "Mint key" }));

    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/api-keys", { name: "CI runner", role: "member" }));
    expect(await screen.findByText(ONCE_A)).toBeInTheDocument();
  });

  it("distinguishes a NULL retention day count from zero and saves edits", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Retention & Assets");
    expect(await screen.findByLabelText("Render retention (days)")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("NOT SET");
    expect(document.body.textContent ?? "").toContain("keep forever");

    fireEvent.change(screen.getByLabelText("Render retention (days)"), { target: { value: "45" } });
    fireEvent.click(screen.getByRole("button", { name: "Save retention" }));
    await waitFor(() => expect(wsPut).toHaveBeenCalledWith("/retention", { render_retention_days: 45 }));
  });

  it("marks a notification read through the scoped write path", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("Inbox");
    expect(await screen.findByText("Review Requested")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/notifications/n-1/read", undefined));
  });

  it("preserves all untouched stored rates and notes when declaring one capacity rate", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    wsGet.mockImplementation((path: string) => Promise.resolve(path.startsWith("/planner/calendar") ? {
      entries: [], committed: {}, remaining: {},
      capacity: { declared: true, locale: "", longform_per_week: 4, shorts_per_day: 2,
        ugc_per_day: 5, localization_per_day: 6, render_hours_per_day: 7,
        review_slots_per_day: 8, notes: "Keep the existing review allocation" },
    } : route(path)));
    render(<Settings />);
    await ready();

    await openSection("Capacity");
    expect(await screen.findByLabelText("Shorts per day")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Shorts per day"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Declare capacity" }));
    await waitFor(() =>
      expect(wsPost).toHaveBeenCalledWith("/planner/capacity", {
        locale: "", longform_per_week: 4, shorts_per_day: 3, ugc_per_day: 5,
        localization_per_day: 6, render_hours_per_day: 7, review_slots_per_day: 8,
        notes: "Keep the existing review allocation",
      }),
    );
  });

  it("refuses a capacity partial update without a complete selected-locale record", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();
    await openSection("Capacity");
    fireEvent.change(screen.getByLabelText("Locale"), { target: { value: "fr" } });
    fireEvent.change(screen.getByLabelText("Shorts per day"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Declare capacity" }));
    await waitFor(() => expect(document.body.textContent).toMatch(/capacity.*locale.*unavailable/i));
    expect(wsPost).not.toHaveBeenCalled();
  });

  it("clears a capacity pool only with an explicit zero, preserving blank edits", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();
    await openSection("Capacity");
    fireEvent.change(screen.getByLabelText("Longform per week"), { target: { value: "9" } });
    fireEvent.change(screen.getByLabelText("Longform per week"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Shorts per day"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Declare capacity" }));
    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/planner/capacity", {
      locale: "", longform_per_week: 1, shorts_per_day: 0, ugc_per_day: 0,
      localization_per_day: 0, render_hours_per_day: 1, review_slots_per_day: 2, notes: "",
    }));
  });

  it("saves the music opt-in through PUT /music/policy", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Settings />);
    await ready();

    await openSection("Music");
    expect(await screen.findByText("not opted in")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Opt in to generated music"));
    fireEvent.click(screen.getByRole("button", { name: "Save music policy" }));
    await waitFor(() =>
      expect(wsPut).toHaveBeenCalledWith("/music/policy", { generate: true }),
    );
  });

  it("marks publishing impossible at every autonomy level", async () => {
    serve();
    render(<Settings />);
    await ready();

    await openSection("Autonomy Policy");
    expect(await screen.findByText("Planning autonomy table")).toBeInTheDocument();
    expect(await screen.findByText("Produce")).toBeInTheDocument();
    const rowsWithNever = Array.from(document.querySelectorAll("tbody tr")).filter((r) =>
      Array.from(r.querySelectorAll("td")).some((td) => (td.textContent ?? "").trim() === "NEVER"),
    );
    expect(rowsWithNever.length).toBeGreaterThanOrEqual(4);
  });
});
