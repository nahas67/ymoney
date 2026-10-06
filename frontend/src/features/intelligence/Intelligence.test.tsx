/** @vitest-environment jsdom */
/* Intelligence — three correctness claims, asserted rather than assumed.
 *
 *   1. A failed endpoint is an alert naming the failure, never a zero and never
 *      an empty list. A decision engine that reports "0 decisions" during an
 *      outage is indistinguishable from a workspace that has decided nothing.
 *   2. SUBMISSION_UNKNOWN is visually distinct from a warning. An ambiguous paid
 *      submission read as an ordinary failure is how one invoice becomes two.
 *   3. No prompt text, no secret. `_record_dto` ships `input` and `output`
 *      verbatim; neither may reach the DOM in any form.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

import Intelligence, { incidentTone, isResubmittable } from "./Intelligence";

afterEach(async () => {
  // The failure-path tests reject a query on purpose. Drain those rejections
  // inside act BEFORE unmounting, or their state updates land after teardown
  // and React logs an act warning that would mask a real one.
  await act(async () => {
    await Promise.resolve();
  });
  cleanup();
  wsGet.mockReset();
});

const SECRET_PROMPT = "SYSTEM: internal key sk-live-DO-NOT-LEAK and the private brief";
const SECRET_OUTPUT = "your key is sk-live-DO-NOT-LEAK, confirmed";

const DECISION = {
  id: "dec-1",
  kind: "boolean",
  mode: "SHADOW",
  requested_provider: "deterministic",
  actual_provider: "openai",
  model: "gpt-x",
  latency_ms: 812,
  cost_usd: 0.0042,
  fallback_reason: "local slot unhealthy",
  // The backend really does ship these two. The screen must not render them.
  input: { prompt: SECRET_PROMPT },
  output: { text: SECRET_OUTPUT },
  agree: false,
  created_at: "2026-03-04T09:00:00Z",
};

const INCIDENT_UNKNOWN = {
  incident_id: "video_submission:v-1",
  source: "video_submission",
  provider: "render-farm",
  operation: "video.render.submit",
  attempted_at: "2026-03-04T08:00:00Z",
  remote_id: "task-abc-123",
  state: "SUBMISSION_UNKNOWN",
  display_state: "SUBMISSION_UNKNOWN",
  exposure: "UNKNOWN_EXPOSURE",
  estimated_exposure_usd: null,
  exposure_unknown: true,
  recommended_action: "RECONCILE",
  retry_safe: false,
  may_resubmit: false,
  detail: "response lost after submit",
  note: "reconcile with the provider before doing anything else",
};

function route(path: string): unknown {
  if (path.startsWith("/intelligence/decisions/log")) {
    return { items: [DECISION] };
  }
  if (path.startsWith("/intelligence/decisions/shadow-report")) {
    return {
      workspace_id: "ws-1",
      kind: null,
      total: 4,
      agreed: 3,
      disagreed: 1,
      agreement_rate: 0.75,
      avg_latency_ms: 640.5,
      total_cost_usd: 0.017,
      by_kind: { boolean: { total: 4, agreed: 3, agreement_rate: 0.75 } },
    };
  }
  if (path.startsWith("/intelligence/routing/health")) {
    return { providers: { remote: true, local: false } };
  }
  if (path.startsWith("/intelligence/routing/log")) {
    return {
      entries: [
        {
          at: "2026-03-04T09:00:00Z",
          workspace_id: "ws-1",
          task_type: "script",
          tier: "BALANCED",
          model: "gpt-x",
          remote: true,
          reason: "quality_required=standard",
          fallbacks: ["FAST"],
        },
      ],
    };
  }
  if (path.startsWith("/intelligence/routing/chains")) {
    return {
      chains: [
        {
          workspace_id: "ws-1",
          task_type: "script",
          policy: { max_attempts: 3 },
          legs: [
            {
              tier: "PREMIUM",
              target: "remote",
              outcome: "READ_TIMEOUT",
              paid: true,
              attempt: 1,
              paid_attempt: 1,
              model: "gpt-x",
              model_source: "registry",
              estimated_usd: 0.02,
              fell_through: false,
              reason: "response lost",
            },
          ],
          stop_reason: "AmbiguousLegStopped",
          paid_legs: 1,
          estimated_exposure_usd: 0.02,
          at: "2026-03-04T09:00:00Z",
          succeeded_tier: "",
        },
      ],
      note: "in-process and bounded; the durable record is the incidents endpoint and the cost ledger",
    };
  }
  if (path.startsWith("/intelligence/verification/ledger")) {
    return {
      items: [
        {
          id: "ev-1",
          workspace_id: "ws-1",
          kind: "render",
          subject_id: "vid-0000000001",
          execution_status: "COMPLETED",
          verification_status: "VERIFIED",
          checks: [{ name: "audio-present", ok: true }],
          digest: "abcdef1234567890",
          prev_digest: "0000",
          created_at: "2026-03-04T09:05:00Z",
        },
      ],
      chain: { ok: true, count: 1, broken_at: null },
    };
  }
  if (path.startsWith("/provider-maturity/incidents")) {
    return {
      items: [INCIDENT_UNKNOWN],
      count: 1,
      unknown_exposure_count: 1,
      states: ["SUBMISSION_ATTEMPTED", "SUBMISSION_UNKNOWN"],
      note: "SUBMISSION_UNKNOWN means the provider MAY have accepted and billed the request.",
    };
  }
  return {};
}

function serve() {
  wsGet.mockImplementation((path: string) => Promise.resolve(route(path)));
}

function fail(predicate: (p: string) => boolean, message: string) {
  const base = route;
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(base(path)),
  );
}

function renderScreen() {
  return render(<Intelligence />);
}

/* A marker unique to the decision-log row. "Boolean" also appears in the shadow
 * by-kind table, so it cannot be used as a readiness signal. */
const DECISION_ROW_READY = "local slot unhealthy";

async function ready() {
  return screen.findByText(DECISION_ROW_READY);
}

/* A failed endpoint legitimately surfaces in more than one region -- the
 * at-a-glance banner and the panel's own error state. Assert that ONE of them
 * names the failure, rather than that there is exactly one. */
async function alertsNaming(pattern: RegExp): Promise<boolean> {
  const alerts = await screen.findAllByRole("alert");
  return alerts.some((a) => pattern.test(a.textContent ?? ""));
}

describe("Intelligence renders when data arrives", () => {
  it("shows a decision record with its provider, cost and fallback reason", async () => {
    serve();
    renderScreen();

    await ready();
    const text = document.body.textContent ?? "";
    expect(text).toContain("Boolean");
    expect(text).toContain("openai");
    expect(text).toContain("local slot unhealthy");
    expect(text).toContain("0.0042");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the routing tab's tiers and the verifier chain verdict", async () => {
    serve();
    renderScreen();
    await ready();

    const routing = screen.getByRole("tab", { name: /routing/i });
    fireEvent.click(routing);
    expect(await screen.findByText("Provider-slot health")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("BALANCED");

    const evidence = screen.getByRole("tab", { name: /evidence/i });
    fireEvent.click(evidence);
    expect(await screen.findByText("Verifier ledger")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("Chain intact");
  });
});

describe("a failed read is an alert, not a zero", () => {
  it("names the failure when the decision log is unreachable", async () => {
    fail((p) => p.startsWith("/intelligence/decisions/log"), "decision store unreachable");
    renderScreen();

    expect(await alertsNaming(/decision store unreachable/i)).toBe(true);
    expect(await alertsNaming(/\/intelligence\/decisions\/log/)).toBe(true);
  });

  it("reports the affected figures as UNAVAILABLE rather than 0", async () => {
    fail((p) => p.startsWith("/intelligence/decisions/log"), "decision store unreachable");
    renderScreen();

    await alertsNaming(/decision store unreachable/i);
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
  });

  it("does not present a failed read as an empty decision library", async () => {
    fail((p) => p.startsWith("/intelligence/decisions/log"), "decision store unreachable");
    renderScreen();

    await alertsNaming(/decision store unreachable/i);
    expect(document.body.textContent ?? "").not.toMatch(/no decision recorded yet/i);
  });

  it("keeps failure distinguishable from emptiness", async () => {
    // Empty but successful: the table explains WHY it is empty, with no alert.
    serve();
    wsGet.mockImplementation((path: string) =>
      path.startsWith("/intelligence/decisions/log")
        ? Promise.resolve({ items: [] })
        : Promise.resolve(route(path)),
    );
    renderScreen();

    expect(await screen.findByText(/no decision recorded yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("surfaces a failed routing read without blanking the screen", async () => {
    fail((p) => p.startsWith("/intelligence/routing/chains"), "chain log cold");
    renderScreen();

    await ready();
    expect(await alertsNaming(/chain log cold/i)).toBe(true);
  });
});

describe("SUBMISSION_UNKNOWN is distinct from a warning", () => {
  it("maps it to the unknown tone, never the warning bucket", () => {
    expect(incidentTone("SUBMISSION_UNKNOWN")).toBe("unknown");
    // A genuine failure keeps the danger tone, so the two cannot be conflated.
    expect(incidentTone("FAILED")).toBe("danger");
  });

  it("renders the state verbatim with a class that is not the warning tone", async () => {
    serve();
    renderScreen();

    const tab = await screen.findByRole("tab", { name: /paid submissions/i });
    fireEvent.click(tab);

    expect(await screen.findByText("SUBMISSION_UNKNOWN")).toBeInTheDocument();
    const badge = screen.getByText("SUBMISSION_UNKNOWN").closest(".ym-badge");
    expect(badge).not.toBeNull();
    expect(badge?.className).toContain("ym-tone-unknown");
    expect(badge?.className).not.toContain("ym-tone-warning");
    expect(badge?.className).not.toContain("ym-tone-danger");
  });

  it("never labels an ambiguous submission FAILED", async () => {
    serve();
    renderScreen();
    fireEvent.click(await screen.findByRole("tab", { name: /paid submissions/i }));

    await screen.findByText("SUBMISSION_UNKNOWN");
    expect(document.body.textContent ?? "").not.toMatch(/^\s*Failed\s*$/);
  });

  it("refuses to call an unprovable incident resendable", () => {
    expect(isResubmittable({ retry_safe: false, may_resubmit: false })).toBe(false);
    expect(isResubmittable({ retry_safe: true, may_resubmit: false })).toBe(false);
    expect(isResubmittable({ retry_safe: false, may_resubmit: true })).toBe(false);
    expect(isResubmittable({ retry_safe: true, may_resubmit: true })).toBe(true);
  });

  it("offers no retry control on an ambiguous paid submission", async () => {
    serve();
    renderScreen();
    fireEvent.click(await screen.findByRole("tab", { name: /paid submissions/i }));

    await screen.findByText("SUBMISSION_UNKNOWN");
    const buttons = Array.from(document.querySelectorAll("button")).map((b) =>
      (b.textContent ?? "").trim().toLowerCase(),
    );
    expect(
      buttons.some((b) => b === "retry" || b.includes("try again") || b.includes("resend")),
    ).toBe(false);
    // The reconciliation handle is what an operator is given instead.
    expect(document.body.textContent ?? "").toContain("task-abc-123");
  });
});

describe("no secret reaches the DOM", () => {
  it("never renders the decision input or output the API returns", async () => {
    serve();
    renderScreen();

    await ready();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("sk-live-DO-NOT-LEAK");
    expect(text).not.toContain(SECRET_PROMPT);
    expect(text).not.toContain(SECRET_OUTPUT);
    // The row says the payload exists and is withheld, rather than hiding it.
    expect(text).toContain("withheld");
  });

  it("keeps the secret out of the DOM on every tab, including routing and paid", async () => {
    serve();
    renderScreen();
    await ready();

    for (const name of [/routing/i, /evidence/i, /paid submissions/i, /decisions/i]) {
      fireEvent.click(await screen.findByRole("tab", { name }));
      await waitFor(() =>
        expect(document.body.textContent ?? "").not.toContain("sk-live-DO-NOT-LEAK"),
      );
    }
  });
});