/** @vitest-environment jsdom */
/* Distribution — the two claims this screen can get catastrophically wrong.
 *
 *   1. THE MODES ARE NOT ONE BADGE. LIVE, MOCK, HANDOFF and UNAVAILABLE are
 *      different facts about money and reach. An operator who reads a handoff as
 *      a live post, or a mock as delivered, makes a decision on both.
 *
 *   2. AN AMBIGUOUS PAID SUBMISSION IS NOT RETRYABLE. `SUBMISSION_UNKNOWN` means
 *      the provider MAY already have accepted and billed the call. The screen
 *      must offer reconciliation, never a resubmit -- resubmitting an ambiguous
 *      submit is how one incident becomes two charges.
 *
 * A third, quieter claim: a failed read is an ALERT, never a zero. "0 live
 * publications" and "we could not ask" must never look the same.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

/* Everything the factory needs is created inside it: a factory that closes over
   a module-scope const would read it before the module body initialised it. */
vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
  api: vi.fn(),
}));

/* A viewer: the server reported the list, so affordances are enforced. */
vi.mock("../../state/session", () => {
  const caps = ["content.read", "content.write", "operations.view"];
  const session = {
    authenticated: true,
    user: { id: "u-1", email: "op@example.com" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    capabilities: caps,
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  };
  return {
    useSession: () => session,
    can: (p: string | null) => (p ? caps.includes(p) : true),
    blockedReason: (p: string | null) =>
      p && !caps.includes(p) ? `Your workspace role does not include "${p}". The server enforces this.` : null,
  };
});

import Distribution from "./Distribution";
import { api, wsApi } from "../../lib/api";

const wsGet = vi.mocked(wsApi.get);
const wsPost = vi.mocked(wsApi.post);
const wsDel = vi.mocked(wsApi.del);
const bareGet = vi.mocked(api);

const HEALTH = {
  status: "healthy",
  publishers: {
    youtube: { mode: "real", ready: true, detail: "connected account x1", via_relay: false },
    tiktok: { mode: "real", ready: true, detail: "connected account x1", via_relay: false },
    facebook: { mode: "mock", ready: false, detail: "not configured", via_relay: false },
    instagram: { mode: "mock", ready: false, detail: "not configured", via_relay: false },
  },
  mocks: { llm: false, trends: false, publishing: false, analytics: false, video_engine: false },
  time: "2026-05-01T00:00:00Z",
};

const CAPABILITIES = {
  items: [
    {
      platform: "youtube",
      capabilities: ["DIRECT_PUBLISH"],
      publish_mode: "DIRECT_PUBLISH",
      direct_publish: true,
      user_handoff: false,
      supports_inbox: true,
      supports_analytics: true,
      campaign_platforms: ["youtube_shorts"],
      media: "9:16",
      metadata_limits: "title 100",
    },
    {
      platform: "tiktok",
      capabilities: ["DIRECT_PUBLISH"],
      publish_mode: "DIRECT_PUBLISH",
      direct_publish: true,
      user_handoff: false,
      supports_inbox: true,
      supports_analytics: true,
      campaign_platforms: ["tiktok"],
      media: "9:16",
      metadata_limits: "title 150",
    },
    {
      /* The handoff truth comes from HANDOFF_PLATFORMS, not a name check. */
      platform: "snapchat",
      capabilities: ["USER_HANDOFF"],
      publish_mode: "USER_HANDOFF",
      direct_publish: false,
      user_handoff: true,
      supports_inbox: false,
      supports_analytics: false,
      campaign_platforms: ["snapchat"],
      media: "9:16",
      metadata_limits: null,
    },
  ],
};

const PLATFORMS = {
  items: [
    {
      platform: "youtube",
      media_types: ["shorts"],
      capabilities: ["DIRECT_PUBLISH"],
      publish_mode: "DIRECT_PUBLISH",
      verified_limits: ["max_duration_seconds", "aspect_ratio"],
      unverified: ["max_caption_chars"],
      verified_notes: ["Shorts quota documented."],
    },
  ],
};

const CALENDAR = {
  workspace_id: "ws-1",
  days: 30,
  entries: [
    { id: "se-1", platform: "youtube", run_at: "2026-05-02T09:00:00", status: "PENDING", content_item_id: "c-1", campaign_id: null },
  ],
  capacity: {
    declared: true,
    locale: "US",
    longform_per_week: 2,
    shorts_per_day: 3,
    ugc_per_day: 0,
    localization_per_day: 0,
    render_hours_per_day: 8,
    review_slots_per_day: 5,
    notes: "",
  },
  committed: { shorts: 4 },
  remaining: { shorts: 6 },
  plan_item_count: 7,
  note: "run times come from the platform's seed windows unless a measured engagement window exists",
};

const ACCOUNTS = {
  items: [
    {
      id: "acct-1",
      platform: "youtube",
      display_name: "Studio channel",
      external_id: "UC123…",
      status: "connected",
      token_expires_at: "2026-06-01T00:00:00Z",
    },
  ],
};

const JOBS = {
  items: [
    {
      id: "job-1",
      platform: "youtube",
      status: "PUBLISHED",
      remote_url: "https://youtube.com/watch?v=abc",
      remote_post_id: "abc",
      attempt: 1,
      error: "",
      content_item_id: "c-1",
      content_topic: "Spring push",
      scheduled_at: "2026-05-02T09:00:00Z",
      published_at: "2026-05-02T09:01:00Z",
      created_at: "2026-05-01T09:00:00Z",
    },
    {
      id: "job-2",
      platform: "tiktok",
      status: "FAILED",
      remote_url: "",
      remote_post_id: "",
      attempt: 3,
      error: "provider rejected the upload: quota exceeded",
      content_item_id: "c-2",
      content_topic: "Spring push 2",
      scheduled_at: null,
      published_at: null,
      created_at: "2026-05-01T10:00:00Z",
    },
  ],
};

/* One row per mode. The four buckets must be reachable from real payload
   fields: mock flag, registry handoff, and the remote pointer that
   `classify_publication` reads as `remote_id`. */
function posts() {
  return {
    items: [
      {
        id: "post-live",
        platform: "youtube",
        title: "Real one",
        remote_url: "https://youtube.com/watch?v=live1",
        published_at: "2026-05-01T09:00:00Z",
        is_mock: false,
        metrics: { views: 10, likes: 1, comments: 0, completion_rate: 0.42 },
      },
      {
        id: "post-mock",
        platform: "facebook",
        title: "Simulated one",
        remote_url: "",
        published_at: "2026-05-01T09:00:00Z",
        is_mock: true,
        metrics: { views: 0, likes: 0, comments: 0, completion_rate: null },
      },
      {
        id: "post-handoff",
        platform: "snapchat",
        title: "Prepared for a human",
        remote_url: "",
        published_at: null,
        is_mock: false,
        metrics: { views: 0, likes: 0, comments: 0, completion_rate: null },
      },
      {
        /* No mock flag, no remote pointer, direct-publish platform. Per
         * classify_publication that is UNAVAILABLE, not LIVE and not FAILED. */
        id: "post-unknown",
        platform: "tiktok",
        title: "Outcome unrecorded",
        remote_url: "",
        published_at: null,
        is_mock: false,
        metrics: { views: 0, likes: 0, comments: 0, completion_rate: null },
      },
    ],
  };
}

const INCIDENTS = {
  workspace_id: "ws-1",
  items: [
    {
      incident_id: "video_submission:v-9",
      source: "video_submission",
      provider: "moneyprinterturbo",
      operation: "video.render.submit",
      attempted_at: "2026-05-01T08:00:00Z",
      remote_id: "",
      state: "SUBMISSION_UNKNOWN",
      display_state: "SUBMISSION_UNKNOWN",
      exposure: "UNKNOWN_EXPOSURE",
      estimated_exposure_usd: null,
      exposure_unknown: true,
      recommended_action: "RECONCILE",
      retry_safe: false,
      may_resubmit: false,
      detail: "connection reset while awaiting the provider response",
      note: "reconcile with the provider before doing anything else",
    },
  ],
  count: 1,
  unknown_exposure_count: 1,
  states: ["PREPARED", "SUBMISSION_UNKNOWN", "SUCCEEDED", "FAILED"],
  note: "SUBMISSION_UNKNOWN means the provider MAY have accepted and billed the request.",
};

const MATURITY = {
  workspace_id: "ws-1",
  items: [
    {
      provider: "youtube",
      capability: "publishing",
      implementation_status: "IMPLEMENTED",
      contract_status: "CONTRACT_TESTED",
      live_status: "UNVERIFIED",
      commercial_status: "FREE_TIER",
      credential_status: "RESOLVED",
      health: "UNKNOWN",
      last_verified_at: "",
      credential_keys: ["youtube_client_id"],
      simulation_only: false,
      notes: "OAuth upload path exists.",
      evidence: ["backend/app/providers/publishers/youtube.py"],
      gaps: [],
      production_ready: false,
      blockers: ["live_status UNVERIFIED"],
    },
  ],
  count: 1,
  resolved_credential_states: ["RESOLVED", "NOT_CONFIGURED"],
  note: "resolved_credential_status is a state, never a value.",
  credential_summary: { RESOLVED: 1 },
};

function route(path: string): unknown {
  if (path.startsWith("/publishing/accounts")) return ACCOUNTS;
  if (path.startsWith("/publishing/jobs")) return JOBS;
  if (path.startsWith("/publishing/posts")) return posts();
  if (path.startsWith("/distribution/capabilities")) return CAPABILITIES;
  if (path.startsWith("/distribution/platforms")) return PLATFORMS;
  if (path.startsWith("/planner/calendar")) return CALENDAR;
  if (path.startsWith("/provider-maturity/incidents")) return INCIDENTS;
  if (path.startsWith("/provider-maturity")) return MATURITY;
  return {};
}

function serve(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    }
    return Promise.resolve(route(path));
  });
  bareGet.mockImplementation(() => Promise.resolve(HEALTH));
  wsPost.mockImplementation(() => Promise.resolve({}));
  wsDel.mockImplementation(() => Promise.resolve({ deleted: true }));
}

beforeEach(() => {
  serve();
});

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
  wsDel.mockReset();
  bareGet.mockReset();
});

function renderDistribution() {
  return render(<Distribution />);
}

describe("Distribution renders real data", () => {
  it("shows the delivery failures, and the connected account on the Accounts tab", async () => {
    renderDistribution();

    await screen.findByText("Failed deliveries");
    await waitFor(() => {
      expect(document.body.textContent).toContain("provider rejected the upload");
    });
    /* "Studio channel" only exists on the Accounts tab, so this also proves the
       tab switch re-targets real loaded data rather than a placeholder. */
    fireEvent.click(screen.getByRole("tab", { name: /accounts/i }));
    expect(await screen.findByText("Studio channel")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the schedule and provider maturity from their own endpoints", async () => {
    renderDistribution();
    await screen.findByText("Failed deliveries");

    fireEvent.click(screen.getByRole("tab", { name: /schedule/i }));
    expect(await screen.findByText("Entries in window")).toBeInTheDocument();
    expect(screen.getByText("2026-05-02 09:00:00")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /providers/i }));
    expect(await screen.findByText("Provider maturity")).toBeInTheDocument();
    /* The maturity record is shown as recorded: an UNVERIFIED live status, a
       blocker, and the reason the record carries. */
    expect(screen.getByText("Unverified")).toBeInTheDocument();
    expect(screen.getByText("live_status UNVERIFIED")).toBeInTheDocument();
    expect(screen.getByText("OAuth upload path exists.")).toBeInTheDocument();
    expect(screen.getByText("Production ready")).toBeInTheDocument();
  });

  it("reports a FAILED read as an alert naming the failure", async () => {
    serve({ "/publishing/jobs": new Error("delivery backend unreachable") });
    renderDistribution();

    const alerts = await screen.findAllByRole("alert");
    const text = alerts.map((a) => a.textContent ?? "").join(" | ");
    expect(text).toMatch(/delivery backend unreachable/i);
  });

  it("never turns a failed read into a zero", async () => {
    serve({ "/publishing/posts": new Error("posts backend unreachable") });
    renderDistribution();

    await screen.findAllByRole("alert");
    const body = document.body.textContent ?? "";
    // The mode tiles must refuse to report a number rather than showing 0.
    expect(body).toMatch(/unavailable/i);
    expect(body).not.toMatch(/no publication recorded/i);
  });

  it("keeps failure distinguishable from emptiness", async () => {
    /* EMPTY: the endpoint answered with no rows. No alert, an explicit empty. */
    serve({ "/publishing/posts": { items: [] } });
    const { unmount } = renderDistribution();
    expect(await screen.findByText("No publication recorded")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();

    /* FAILED: the endpoint did not answer. An alert, and no empty state. */
    serve({ "/publishing/posts": new Error("posts backend unreachable") });
    renderDistribution();
    await screen.findAllByRole("alert");
    expect(screen.queryByText("No publication recorded")).toBeNull();
  });
});

describe("the four publication modes stay separate", () => {
  it("labels all four buckets even when one is empty", async () => {
    renderDistribution();

    await screen.findByText("Failed deliveries");
    expect(screen.getByText("Live publications")).toBeInTheDocument();
    expect(screen.getByText("Mock publications")).toBeInTheDocument();
    expect(screen.getByText("Handoff publications")).toBeInTheDocument();
    expect(screen.getByText("Mode not reported")).toBeInTheDocument();
  });

  it("gives each row its own badge instead of one 'published' badge", async () => {
    renderDistribution();

    await screen.findByText("Failed deliveries");
    await waitFor(() => {
      expect(screen.getByText("Real one")).toBeInTheDocument();
    });
    expect(screen.getByText("Real one")).toBeInTheDocument();
    expect(screen.getByText("Simulated one")).toBeInTheDocument();
    expect(screen.getByText("Prepared for a human")).toBeInTheDocument();
    expect(screen.getByText("Outcome unrecorded")).toBeInTheDocument();

    // LIVE / MOCK / HANDOFF each carry their own word; the undecidable row says
    // so in words rather than borrowing one of the three.
    expect(screen.getByTitle("Publication mode: LIVE")).toBeInTheDocument();
    expect(screen.getByTitle("Publication mode: MOCK")).toBeInTheDocument();
    expect(screen.getByTitle("Publication mode: HANDOFF")).toBeInTheDocument();
    expect(screen.getAllByText("MODE UNAVAILABLE").length).toBeGreaterThan(0);
  });

  it("counts each mode into its own bucket", async () => {
    renderDistribution();
    await screen.findByText("Failed deliveries");

    const live = screen.getByText("Live publications").closest(".ym-stat");
    const mock = screen.getByText("Mock publications").closest(".ym-stat");
    const handoff = screen.getByText("Handoff publications").closest(".ym-stat");
    const unknown = screen.getByText("Mode not reported").closest(".ym-stat");

    expect(live?.textContent).toContain("1");
    expect(mock?.textContent).toContain("1");
    expect(handoff?.textContent).toContain("1");
    expect(unknown?.textContent).toContain("1");
  });

  it("does not claim LIVE for a handoff platform that returned no remote id", async () => {
    serve({ "/publishing/posts": { items: [posts().items[2]] } });
    renderDistribution();

    await screen.findByText("Failed deliveries");
    await waitFor(() => {
      expect(screen.getByText("Prepared for a human")).toBeInTheDocument();
    });
    expect(screen.getByTitle("Publication mode: HANDOFF")).toBeInTheDocument();
    expect(screen.queryByTitle("Publication mode: LIVE")).toBeNull();
  });
});

describe("an ambiguous paid submission is reconcilable, never retryable", () => {
  it("reports SUBMISSION_UNKNOWN with no generic retry affordance", async () => {
    renderDistribution();

    await screen.findByText("Failed deliveries");
    await waitFor(() => {
      expect(screen.getByText("Submission Unknown")).toBeInTheDocument();
    });

    // The state travels verbatim and is never rendered as a failure. Scoped to
    // the incident table: a delivery job elsewhere on this tab IS FAILED, and
    // conflating the two vocabularies is the bug being guarded.
    const incidentTable = screen.getByText("State (verbatim)").closest("table");
    expect(incidentTable?.textContent ?? "").toContain("Submission Unknown");
    expect(incidentTable?.textContent ?? "").not.toContain("Failed");
    expect(screen.getByText("Reconcile")).toBeInTheDocument();
    expect(screen.getByText("Resubmit NOT safe")).toBeInTheDocument();

    // The load-bearing assertion: NO control anywhere offers a retry of a paid
    // or ambiguous action. (The only Retry this screen can produce is
    // QueryBoundary's re-read of a GET, and every read here succeeds.)
    const retryish = screen
      .queryAllByRole("button")
      .filter((b) => /retry|resend|resubmit|send again|try again/i.test(b.textContent ?? ""));
    expect(retryish.map((b) => b.textContent)).toEqual([]);
    expect(screen.queryAllByRole("link", { name: /retry/i })).toHaveLength(0);
  });

  it("still offers the backend's own verdict when a resubmit IS proven safe", async () => {
    serve({
      "/provider-maturity/incidents": {
        ...INCIDENTS,
        items: [{ ...INCIDENTS.items[0], state: "FAILED", retry_safe: true, recommended_action: "RESUBMIT" }],
        count: 1,
        unknown_exposure_count: 0,
      },
    });
    renderDistribution();

    await screen.findByText("Failed deliveries");
    await waitFor(() => {
      expect(screen.getByText("Resubmit proven safe")).toBeInTheDocument();
    });
    expect(screen.getByText("Resubmit")).toBeInTheDocument();
    // Still no button: the backend reports safety, the operator decides out of band.
    const retryish = screen
      .queryAllByRole("button")
      .filter((b) => /retry|resend|resubmit/i.test(b.textContent ?? ""));
    expect(retryish.map((b) => b.textContent)).toEqual([]);
  });
});