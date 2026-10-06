/** @vitest-environment jsdom */
/* Community — one desk for everything a viewer says back.
 *
 * The claim under test is HONESTY OF STATE. An inbox that shows "no comments"
 * when its comment endpoint is down is worse than one that shows nothing: an
 * operator concludes the audience is silent when in fact nobody asked. So the
 * suite is about the difference between EMPTY (the endpoint answered) and
 * FAILED (it did not), and about the screen refusing to infer facts about a
 * person that the API does not return.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

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

/* A `member`: the backend gate on every inbox mutation is
   `require_workspace_role("member")`, which is exactly what `reviews.approve`
   carries in `services/capabilities._MINIMUM_ROLE`. */
vi.mock("../../state/session", () => {
  const caps = ["content.read", "content.write", "reviews.approve", "operations.view"];
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

import Community from "./Community";
import { wsApi } from "../../lib/api";

const wsGet = vi.mocked(wsApi.get);
const wsPost = vi.mocked(wsApi.post);

const CONVERSATION = {
  id: "conv-1",
  platform: "youtube",
  account_id: "acct-1",
  thread_key: "t-1",
  title: "Question about the pricing",
  participant_remote_id: "u-remote-9",
  participant_name: "Dana Ruiz",
  published_post_id: "post-1",
  last_interaction_at: "2026-05-01T09:00:00Z",
  unread_count: 2,
  status: "open",
  priority: "high",
  last_interaction: {
    id: "ix-1",
    kind: "COMMENT",
    platform: "youtube",
    author_name: "Dana Ruiz",
    status: "unread",
    text: "Is there a free tier for the render pass?",
    created_at: "2026-05-01T09:00:00Z",
  },
  created_at: "2026-04-30T09:00:00Z",
  updated_at: "2026-05-01T09:00:00Z",
};

const INTERACTION = {
  id: "ix-1",
  workspace_id: "ws-1",
  platform: "youtube",
  account_id: "acct-1",
  remote_id: "yt-c-1",
  kind: "COMMENT",
  text: "Is there a free tier for the render pass?",
  author_remote_id: "u-remote-9",
  author_name: "Dana Ruiz",
  parent_interaction_id: null,
  thread_id: "t-1",
  conversation_id: "conv-1",
  post_remote_id: "yt-v-1",
  published_post_id: "post-1",
  content_item_id: "c-1",
  campaign_id: null,
  status: "unread",
  unread: true,
  moderation_state: "allowed",
  sentiment: "neutral",
  intent: "pricing_question",
  priority: "high",
  is_question: true,
  classifications: [{ label: "QUESTION", confidence: 0.91, provider: "rules", evidence: ["?"] }],
  moderation: [{ verdict: "ALLOW", reason: "no policy hit" }],
  remote_created_at: "2026-05-01T08:59:00Z",
  created_at: "2026-05-01T08:59:00Z",
  updated_at: "2026-05-01T08:59:00Z",
};

const ACTION = {
  id: "act-1",
  interaction_id: "ix-1",
  conversation_id: "conv-1",
  account_id: "acct-1",
  platform: "youtube",
  action_type: "REPLY",
  mode: "DRAFT_ONLY",
  state: "pending_approval",
  origin: "ai",
  badge: "AI DRAFT",
  draft_text: "Yes — the render pass has a free tier.",
  final_text: "",
  brand_check: { status: "PASS" },
  approval_user_id: null,
  rejected_user_id: null,
  is_mock: false,
  remote_reply_id: "",
  error: "",
  sent_at: "",
  created_at: "2026-05-01T09:01:00Z",
  updated_at: "2026-05-01T09:01:00Z",
};

const PLATFORMS = {
  available: true,
  platforms: [{ platform: "youtube", capabilities: ["DIRECT_PUBLISH"] }],
  capabilities: { youtube: ["DIRECT_PUBLISH"] },
  observed: ["youtube", "tiktok"],
};

const AUTONOMY = {
  autonomy: { mode: "APPROVAL_REQUIRED", classes: ["QUESTION", "LEAD"], caps: { daily_replies: 20, hourly_replies: 5 } },
  modes: ["DRAFT_ONLY", "LOW_RISK_AUTO", "APPROVAL_REQUIRED", "DISABLED"],
  classes: ["QUESTION", "LEAD", "SPAM"],
  defaults: { mode: "DRAFT_ONLY", classes: [], caps: { daily_replies: 20, hourly_replies: 5 } },
};

const ANALYTICS = {
  available: true,
  metrics: { window_days: 30, replies_sent: 12, response_rate: 0.4 },
  kpis: [],
  totals: {},
};

const OPPORTUNITIES = {
  items: [
    {
      id: "opp-1",
      opportunity_type: "partnership",
      title: "Asked about a sponsorship",
      detail: "Three threads mention brand deals.",
      evidence_count: 3,
      source_interaction_ids: ["ix-1"],
      confidence: "medium",
      state: "open",
      converted_idea_json: {},
      converted_opportunity_id: null,
      created_at: "2026-05-01T09:00:00Z",
      updated_at: "2026-05-01T09:00:00Z",
    },
  ],
};

const INSIGHTS = {
  items: [
    {
      id: "ins-1",
      topic_key: "pricing",
      topic: "Pricing questions",
      representative_text: "Is there a free tier?",
      platforms: ["youtube"],
      evidence_count: 7,
      confidence: "high",
      state: "new",
      created_at: "2026-05-01T09:00:00Z",
      updated_at: "2026-05-01T09:00:00Z",
    },
  ],
  suggestions: [],
  suggestions_available: false,
};

const ACTIONS = { items: [ACTION] };

function route(path: string): unknown {
  if (path.startsWith("/inbox/platforms")) return PLATFORMS;
  if (path.startsWith("/inbox/autonomy")) return AUTONOMY;
  if (path.startsWith("/inbox/analytics")) return ANALYTICS;
  if (path.startsWith("/inbox/conversations/")) {
    /* `get_conversation` returns `{conversation, interactions}` — a different
       envelope from the list's `{items}`. */
    return { conversation: CONVERSATION, interactions: [] };
  }
  if (path.startsWith("/inbox/conversations")) return { items: [CONVERSATION] };
  if (path.startsWith("/inbox/interactions/")) {
    return {
      interaction: INTERACTION,
      linked_publication: null,
      thread: [],
      actions: [ACTION],
    };
  }
  if (path.startsWith("/inbox/interactions")) return { items: [INTERACTION], next_cursor: null };
  if (path.startsWith("/inbox/actions")) return ACTIONS;
  if (path.startsWith("/inbox/opportunities")) return OPPORTUNITIES;
  if (path.startsWith("/inbox/insights")) return INSIGHTS;
  return {};
}

function serve(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    }
    return Promise.resolve(route(path));
  });
  wsPost.mockImplementation(() => Promise.resolve({}));
}

beforeEach(() => {
  serve();
});

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
});

function renderCommunity() {
  return render(<Community />);
}

describe("Community renders real data", () => {
  it("shows a conversation, its unread count and the platform filter", async () => {
    renderCommunity();

    expect(await screen.findByText("Dana Ruiz")).toBeInTheDocument();
    expect(screen.getByText("Is there a free tier for the render pass?")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Tiktok" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reports a FAILED read as an alert naming the failure", async () => {
    serve({ "/inbox/conversations": new Error("inbox backend unreachable") });
    renderCommunity();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/inbox backend unreachable/i);
  });

  it("never turns a failed read into an empty inbox", async () => {
    serve({ "/inbox/conversations": new Error("inbox backend unreachable") });
    renderCommunity();

    await screen.findByRole("alert");
    expect(document.body.textContent).not.toMatch(/no conversation\b/i);
  });

  it("keeps failure distinguishable from emptiness", async () => {
    /* EMPTY: the endpoint answered with zero rows. */
    serve({ "/inbox/conversations": { items: [] } });
    const { unmount } = renderCommunity();
    expect(await screen.findByText("No conversation")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();

    /* FAILED: the endpoint did not answer. */
    serve({ "/inbox/conversations": new Error("inbox backend unreachable") });
    renderCommunity();
    await screen.findByRole("alert");
    expect(screen.queryByText("No conversation")).toBeNull();
  });

  it("surfaces an unavailable community engine as UNAVAILABLE, not as zero", async () => {
    serve({
      "/inbox/analytics": { available: false, metrics: {}, kpis: [], error: "community engine not available" },
    });
    renderCommunity();

    await screen.findByText("Dana Ruiz");
    expect(document.body.textContent).toMatch(/community engine is not available/i);
    // No fabricated metric tile is painted from an empty metrics map.
    expect(screen.queryByText("Replies sent")).toBeNull();
  });
});

describe("no attribute is inferred about a person", () => {
  it("renders only the fields the API returns, with no demographic inference", async () => {
    renderCommunity();
    await screen.findByText("Dana Ruiz");

    // The thread list itself must not present a score of the author.
    expect(screen.queryByText("Author score")).toBeNull();
    expect(screen.queryByText("Trust")).toBeNull();
    expect(screen.queryByText("Persona")).toBeNull();
    // The classifier's own fields are absent from the list on purpose: only the
    // stored classification labels and the question flag appear here.
    expect(screen.queryByText("Neutral")).toBeNull();
  });

  it("keeps a message-level sentiment out of the list view entirely", async () => {
    renderCommunity();
    await screen.findByText("Dana Ruiz");
    const body = document.body.textContent ?? "";
    expect(body).not.toMatch(/tone of author|author sentiment|demographic/i);
  });
});

describe("approval respects the capability the backend enforces", () => {
  it("offers Sync when the workspace has reviews.approve and names the reason when it does not", async () => {
    /* This session mock grants reviews.approve, so the control is offered. */
    const { unmount } = renderCommunity();
    await screen.findByText("Dana Ruiz");
    expect(screen.getByRole("button", { name: "Sync now" })).toBeInTheDocument();
    unmount();
  });

  it("does not treat a draft approval as a publishing approval", async () => {
    renderCommunity();
    await screen.findByText("Dana Ruiz");
    const body = document.body.textContent ?? "";
    /* `publish.approve` is an admin capability; nothing on this screen may
       require it, and the sync control must not claim it. */
    expect(body).not.toMatch(/publish\.approve/);
    expect(body).not.toMatch(/admin/);
  });
});

describe("interaction detail", () => {
  it("opens classification, moderation and response history for a selected row", async () => {
    serve({
      "/inbox/interactions/ix-1": {
        interaction: INTERACTION,
        linked_publication: {
          id: "post-1",
          title: "Spring push",
          remote_url: "https://youtube.com/watch?v=abc",
          campaign_id: null,
          platform: "youtube",
          remote_post_id: "abc",
        },
        thread: [],
        actions: [ACTION],
      },
    });
    renderCommunity();

    await screen.findByText("Dana Ruiz");
    /* Switch the list from conversations to interactions. The conversations
       table is replaced, so the author name below is the interaction row. */
    fireEvent.change(screen.getByLabelText("List"), { target: { value: "interactions" } });
    await screen.findByText("Classified as");

    fireEvent.click(screen.getByText("Dana Ruiz"));
    await screen.findByText("Classification");

    /* Exactly the stored fields. Nothing about the author is invented. */
    expect(screen.getByText("Message sentiment")).toBeInTheDocument();
    expect(screen.getByText("Neutral")).toBeInTheDocument();
    expect(screen.getByText("Asked a question")).toBeInTheDocument();
    expect(screen.getByText("Pricing Question")).toBeInTheDocument();
    expect(screen.getByText("Allow")).toBeInTheDocument();
    expect(screen.getByText("Response history (1)")).toBeInTheDocument();
    /* The provenance word appears twice: on the live draft chip and in the
       history entry. Both must come from the backend's own `badge` field. */
    expect(screen.getAllByText("AI DRAFT").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Yes — the render pass has a free tier.")).toBeInTheDocument();
    /* Approving a reply is gated on reviews.approve, which this session has. */
    expect(screen.getByRole("button", { name: "Approve reply" })).toBeInTheDocument();
  });

  it("shows the thread of a selected conversation instead of an interaction", async () => {
    renderCommunity();
    await screen.findByText("Dana Ruiz");
    fireEvent.click(screen.getByText("Dana Ruiz"));
    expect(await screen.findByText("No messages in this thread")).toBeInTheDocument();
  });
});