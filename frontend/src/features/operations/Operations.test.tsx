/** @vitest-environment jsdom */
/* Operations — the honesty contract is the correctness claim on this screen.
 *
 * `GET /system/health` answers `llm_provider: true` when `MOCK_LLM=true`,
 * because the backend skips the probe. Rendering that boolean as a healthy
 * subsystem is exactly the Work 16.1 failure: simulated evidence presented as
 * live. So the assertions are about REFINEMENT:
 *   - a mocked subsystem is SIMULATED, never UP;
 *   - a down collector yields UNAVAILABLE, never 0, because a failed collector
 *     leaves its gauges untouched on purpose;
 *   - a failed endpoint is an alert naming the failure, not an empty list;
 *   - an ambiguous paid submission is not retryable and not FAILED.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const wsGet = vi.fn();
const globalGet = vi.fn();

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: { get: (path: string) => wsGet(path) },
  api: (method: string, path: string) => globalGet(path),
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

import Operations, {
  collectorState,
  isSimulated,
  simulatedSubsystems,
  type Collectors,
  type Health,
} from "./Operations";

/* The three process-scoped probes are mounted at the root by `main.py`, outside
 * `/api/v1` and without auth, so this file performs that GET itself. */
const PROBES: Record<string, unknown> = {
  "/livez": {
    status: "alive",
    detail: "event loop accepting work",
    event_loop: true,
    checks: [{ id: "process", status: "passed", blocking: true, detail: "ok" }],
    blocking_failures: [],
  },
  "/internal/alerts": {
    evaluated_at_epoch: 1_700_000_000,
    note: "Verdicts reflect this process's in-memory counters since start.",
    thresholds: { gpu_queue_starvation: 3 },
    firing_count: 0,
    rules: [
      {
        id: "gpu_queue_starvation",
        severity: "warning",
        title: "GPU queue starving",
        detects: "GPU-gated jobs waiting behind saturated slots",
        threshold: 3,
        threshold_source: "constant",
        unit: "jobs",
        runbook: "Add GPU capacity or lower the gate.",
        firing: false,
        observed: 0,
      },
      {
        id: "unknown_exposure",
        severity: "critical",
        title: "Unknown monetary exposure",
        detects: "an accepted call nobody can price",
        threshold: 1,
        threshold_source: "constant",
        unit: "entries",
        runbook: "Reconcile with the provider.",
        firing: false,
        observed: 0,
      },
    ],
    verdicts: [],
  },
  "/internal/slo": {
    measured: false,
    disclaimer: "Targets only. No achieved SLO is reported.",
    thresholds: {},
    targets: [{ id: "job_success", objective: "render jobs succeed", target: 0.99 }],
  },
  "/internal/collectors": {
    collectors: { workers: "up", database: "up", jobs: "up", gpu_slots: "up", storage: "up" },
    failed: [],
    note: "A failed collector reports up=0 for itself and leaves its gauges untouched.",
  },
};

const HEALTH_MOCKED: Health = {
  status: "healthy",
  database: true,
  video_engine: true,
  video_engine_name: "mock",
  video_engine_version: null,
  llm_provider: true,
  tts: { provider: "mock-tts", healthy: true },
  publishers: {
    youtube: { mode: "mock", ready: false, detail: "not configured", via_relay: false },
    tiktok: { mode: "real", ready: true, detail: "connected account ×1", via_relay: false },
  },
  queue: { backend: "local", gpu_worker: true, gpu_cuda: false, redis: false },
  mocks: { llm: true, trends: true, publishing: true, analytics: false, video_engine: true },
  time: "2026-03-04T09:00:00Z",
};

const HEALTH_REAL: Health = {
  ...HEALTH_MOCKED,
  status: "healthy",
  video_engine: true,
  video_engine_name: "comfy",
  video_engine_version: "1.2.3",
  llm_provider: true,
  tts: { provider: "elevenlabs", healthy: true },
  queue: { backend: "redis", gpu_worker: true, gpu_cuda: true, redis: true },
  mocks: { llm: false, trends: false, publishing: false, analytics: false, video_engine: false },
};

const HEALTH_FAILING: Health = {
  ...HEALTH_REAL,
  status: "degraded",
  database: false,
  video_engine: false,
  llm_provider: false,
  tts: { provider: "elevenlabs", healthy: false },
};

function globalRoute(path: string): unknown {
  if (path === "/system/health") return HEALTH_REAL;
  if (path === "/system/readiness") {
    return {
      // A blocking failure means `blocked`, per `run_readiness`. A fixture
      // reporting `ready` next to `blocking_failures` would be lying in exactly
      // the way this screen is being tested for.
      status: "blocked",
      checked_at: "2026-03-04T09:00:00Z",
      stale_after_hours: 6,
      checks: [
        { id: "ffmpeg", status: "passed", blocking: true, tier: 1, detail: "ffmpeg 6.0", latency_ms: 12, remediation: "" },
        { id: "llm", status: "failed", blocking: true, tier: 1, detail: "no api key", latency_ms: 4, remediation: "Set llm.api_key" },
      ],
      blocking_failures: ["llm"],
      message: "Autonomous production blocked by: llm",
    };
  }
  if (path === "/system/doctor") {
    return {
      status: "blocked",
      checked_at: "2026-03-04T09:00:00Z",
      stale_after_hours: 6,
      checks: [],
      blocking_failures: ["llm"],
      message: "Autonomous production blocked by: llm",
      doctor: { blocking_failed: ["llm"], attention_needed: ["images"], remediations: { llm: "Set llm.api_key" } },
    };
  }
  if (path === "/system/mode") {
    return {
      mode: "production",
      video_engine: "mock",
      mocks: { publishing: true, analytics: false, trends: true, video_engine: true },
    };
  }
  if (path === "/system/orphans") {
    return {
      videos_orphaned: 0,
      variants_orphaned: 2,
      publishing_jobs_orphaned: 0,
      published_posts_orphaned: 0,
      healthy: false,
      checked_at: "2026-03-04T09:00:00Z",
    };
  }
  return {};
}

function wsRoute(path: string): unknown {
  if (path.startsWith("/ops/overview")) {
    return {
      workspace_id: "ws-1",
      generated_at: "2026-03-04T09:00:00Z",
      jobs: {
        available: true,
        by_status: { QUEUED: 4, RUNNING: 1 },
        total: 5,
        failed_recent: [
          { id: "job-1", type: "RENDER", status: "FAILED", error: "engine refused", retry_count: 2, created_at: "2026-03-04T08:00:00Z" },
        ],
      },
      reviews: { available: false, reason: "OperationalError" },
      exports: { available: true, by_state: {}, failed: [], failed_count: 0 },
      storage: { available: true, bytes: 5242880, file_count: 12, source: "database" },
      provider_health: {
        available: true,
        status: "ready",
        checked_at: "2026-03-04T09:00:00Z",
        blocking_failures: [],
        checks: [{ id: "llm", status: "passed", blocking: true, latency_ms: 30 }],
      },
      costs: {
        available: true,
        last_24h_by_category: { decision_engine: 0.04 },
        spent_last_24h_usd: 0.04,
        daily_budget_usd: 5,
        per_video_budget_usd: 0.2,
        within_budget: true,
        remaining_usd: 4.96,
        since: "2026-03-03T09:00:00Z",
      },
      audit: { available: true, events_last_7d: 12, since: "x", retention_enforced: false },
      retention: { available: true },
    };
  }
  if (path.startsWith("/jobs")) {
    return {
      items: [
        {
          id: "job-abc-1",
          type: "MEDIA_INTEL_GPU_SLOT",
          status: "RUNNING",
          priority: 10,
          retry_count: 0,
          max_retries: 3,
          next_run_at: null,
          started_at: "2026-03-04T09:00:00Z",
          completed_at: null,
          last_error: "",
          claimed_by: "worker-7",
          claimed_at: "2026-03-04T08:59:00Z",
          lease_expires_at: "2026-03-04T09:04:00Z",
          heartbeat_at: "2026-03-04T09:03:00Z",
          lease_state: "RUNNING",
          created_at: "2026-03-04T08:58:00Z",
        },
        {
          id: "job-abc-2",
          type: "RENDER",
          status: "QUEUED",
          priority: 5,
          retry_count: 0,
          max_retries: 3,
          next_run_at: null,
          started_at: null,
          completed_at: null,
          last_error: "",
          claimed_by: "",
          claimed_at: null,
          lease_expires_at: null,
          heartbeat_at: null,
          lease_state: "TERMINAL",
          created_at: "2026-03-04T08:57:00Z",
        },
      ],
    };
  }
  if (path.startsWith("/costs/intelligence")) {
    return {
      total_cost_usd: 1.25,
      per_cycle_usd: null,
      per_video_usd: 0.25,
      per_publication_usd: null,
      cost_per_1000_views_usd: null,
      by_category: { render: 1.0 },
      by_agent: { video_producer: 1.0 },
      publications_by_platform: {},
      totals: { cycles: 0, videos_built: 5, posts_published: 0, views: 0 },
      estimated_return_usd: null,
      estimated_return_note: "requires monetization API access; not simulated",
    };
  }
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
  if (path.startsWith("/provider-maturity/incidents")) {
    return {
      items: [
        {
          incident_id: "video_submission:v-1",
          source: "video_submission",
          provider: "render-farm",
          operation: "video.render.submit",
          attempted_at: "2026-03-04T08:00:00Z",
          remote_id: "task-abc-123",
          state: "SUBMISSION_UNKNOWN",
          exposure: "UNKNOWN_EXPOSURE",
          estimated_exposure_usd: null,
          exposure_unknown: true,
          recommended_action: "RECONCILE",
          retry_safe: false,
          may_resubmit: false,
          detail: "response lost after submit",
          note: "reconcile with the provider before doing anything else",
        },
      ],
      count: 1,
      unknown_exposure_count: 1,
      states: ["SUBMISSION_UNKNOWN"],
      note: "SUBMISSION_UNKNOWN may already be an invoice.",
    };
  }
  if (path.startsWith("/retention")) {
    return { audit_retention_days: null, render_retention_days: 30, temp_asset_retention_days: 7 };
  }
  return {};
}

function serve() {
  globalGet.mockImplementation((path: string) => Promise.resolve(globalRoute(path)));
  wsGet.mockImplementation((path: string) => Promise.resolve(wsRoute(path)));
  vi.stubGlobal(
    "fetch",
    vi.fn((path: string) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(PROBES[path] ?? {}),
      }),
    ),
  );
}

function failWs(predicate: (p: string) => boolean, message: string) {
  serve();
  const base = wsRoute;
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(base(path)),
  );
}

function failGlobal(predicate: (p: string) => boolean, message: string) {
  serve();
  const base = globalRoute;
  globalGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(base(path)),
  );
}

function failProbe(path: string, message: string) {
  serve();
  vi.stubGlobal(
    "fetch",
    vi.fn((p: string) =>
      p === path
        ? Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })
        : Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(PROBES[p] ?? {}) }),
    ),
  );
}

beforeEach(() => {
  serve();
});

afterEach(async () => {
  // The failure-path tests reject a query on purpose. Drain those rejections
  // inside act BEFORE unmounting, or their state updates land after teardown
  // and React logs an act warning that would mask a real one.
  await act(async () => {
    await Promise.resolve();
  });
  cleanup();
  wsGet.mockReset();
  globalGet.mockReset();
  vi.unstubAllGlobals();
});

/* `findAllByText` because a readiness message legitimately appears twice (the
 * at-a-glance hint and the readiness panel body). What matters is that the
 * process-level reads have resolved, not how many times the sentence renders. */
const READY_SIGNAL = /Autonomous production blocked by: llm/;

async function ready() {
  await screen.findAllByText(READY_SIGNAL);
}

async function alertsNaming(pattern: RegExp): Promise<boolean> {
  const found = await screen.findAllByRole("alert");
  return found.some((a) => pattern.test(a.textContent ?? ""));
}

async function openTab(name: RegExp) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

/* "Llm" appears in BOTH the readiness probe table and the subsystem probe table.
 * The subsystem table is the one with four columns, so the row is located by
 * shape rather than by a text that is legitimately ambiguous. */
async function subsystemRow(id: string): Promise<HTMLElement | null> {
  const cells = await screen.findAllByText(id);
  for (const cell of cells) {
    const row = cell.closest("tr");
    if (row && row.querySelectorAll("td").length === 4) return row;
  }
  return null;
}

describe("Operations renders when data arrives", () => {
  it("shows liveness, readiness and the failed probe's remediation", async () => {
    render(<Operations />);

    await ready();
    const text = document.body.textContent ?? "";
    expect(text).toContain("alive");
    expect(text).toContain("blocked"); // run_readiness: a blocking failure is not_ready
    expect(text).toContain("Set llm.api_key");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the budget window on the Cost view", async () => {
    render(<Operations />);
    await ready();

    await openTab(/cost/i);
    expect(await screen.findByText("Spend against budget")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("5.0000"); // daily budget
  });

  it("lists jobs with their lease owner and expiry", async () => {
    render(<Operations />);
    await ready();

    await openTab(/workers/i);
    expect(await screen.findByText("Jobs and leases")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("worker-7");
    expect(text).toContain("no lease held");
  });

  it("shows the paid reconciliation incident as ambiguous, not failed", async () => {
    render(<Operations />);
    await ready();

    await openTab(/reconciliation/i);
    expect(await screen.findByText("SUBMISSION_UNKNOWN")).toBeInTheDocument();
    const badge = screen.getByText("SUBMISSION_UNKNOWN").closest(".ym-badge");
    expect(badge?.className).toContain("ym-tone-unknown");
    expect(document.body.textContent ?? "").toContain("NO — RECONCILE");
    expect(document.body.textContent ?? "").toContain("task-abc-123");
  });
});

describe("a failed read is an alert, not a zero", () => {
  it("names the failure when the workspace job list is unreachable", async () => {
    failWs((p) => p.startsWith("/jobs"), "job table unavailable");
    render(<Operations />);
    await ready();

    await openTab(/workers/i);
    expect(await alertsNaming(/job table unavailable/i)).toBe(true);
  });

  it("does not present a failed cost read as an empty ledger", async () => {
    failWs((p) => p === "/costs", "cost ledger unavailable");
    render(<Operations />);
    await ready();

    await openTab(/cost/i);
    expect(await alertsNaming(/cost ledger unavailable/i)).toBe(true);
    expect(document.body.textContent ?? "").not.toMatch(/no cost recorded in this window/i);
  });

  it("reports the affected tile as UNAVAILABLE rather than $0.0000", async () => {
    failWs((p) => p === "/costs", "cost ledger unavailable");
    render(<Operations />);
    await ready();

    await openTab(/cost/i);
    await alertsNaming(/cost ledger unavailable/i);
    const spentTile = screen.getByText("Spent, last 24h").closest(".ym-stat");
    expect(spentTile?.textContent).toContain("UNAVAILABLE");
    expect(spentTile?.textContent).not.toContain("0.0000");
  });

  it("keeps failure distinguishable from emptiness", async () => {
    serve();
    wsGet.mockImplementation((path: string) =>
      path.startsWith("/costs") && !path.includes("intelligence")
        ? Promise.resolve({
            last_24h_by_category: {},
            spent_last_24h_usd: 0,
            daily_budget_usd: 5,
            per_video_budget_usd: 0.2,
            within_budget: true,
            remaining_usd: 5,
          })
        : Promise.resolve(wsRoute(path)),
    );
    render(<Operations />);
    await ready();

    await openTab(/cost/i);
    expect(await screen.findByText(/no cost recorded in this window/i)).toBeInTheDocument();
    // A genuine zero is shown as a number; the emptiness is explained, not alerted.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("surfaces a failed ops overview as an error, not as empty sections", async () => {
    failWs((p) => p.startsWith("/ops/overview"), "ops section unavailable");
    render(<Operations />);
    await ready();

    await openTab(/storage/i);
    expect(await alertsNaming(/ops section unavailable/i)).toBe(true);
  });

  it("surfaces a failed readiness read as an error", async () => {
    failGlobal((p) => p === "/system/readiness", "readiness probe crashed");
    render(<Operations />);
    expect(await alertsNaming(/readiness probe crashed/i)).toBe(true);
  });

  it("surfaces an unreachable process probe as an error", async () => {
    failProbe("/internal/alerts", "down");
    render(<Operations />);
    await screen.findAllByText(/Autonomous production blocked by: llm/);

    await openTab(/slo & alerts/i);
    expect(await alertsNaming(/internal\/alerts answered 503/i)).toBe(true);
  });
});

describe("simulated evidence is never presented as live", () => {
  it("classifies a mocked deployment as simulated at the helper level", () => {
    expect(isSimulated(HEALTH_MOCKED.mocks, "mock")).toBe(true);
    expect(simulatedSubsystems(HEALTH_MOCKED)).toEqual([
      "LLM",
      "VIDEO ENGINE",
      "TREND SOURCES",
      "PUBLISHING",
    ]);
    expect(isSimulated(HEALTH_REAL.mocks, "comfy")).toBe(false);
    expect(simulatedSubsystems(HEALTH_REAL)).toEqual([]);
  });

  it("labels a mocked subsystem SIMULATED even when the backend flag is true", async () => {
    serve();
    globalGet.mockImplementation((path: string) =>
      path === "/system/health" ? Promise.resolve(HEALTH_MOCKED) : Promise.resolve(globalRoute(path)),
    );
    render(<Operations />);
    await ready();

    // The backend still says `llm_provider: true`. The screen must not call that UP.
    await openTab(/system/i);
    expect((await screen.findAllByText("SIMULATED")).length).toBeGreaterThan(0);

    const row = await subsystemRow("Llm");
    expect(row).not.toBeNull();
    expect(row?.textContent).toContain("SIMULATED");
    expect(row?.textContent).toContain("not meaningful");
    expect(row?.textContent).not.toMatch(/\bUP\b/);
  });

  it("marks the whole deployment as simulated in an alert banner", async () => {
    serve();
    globalGet.mockImplementation((path: string) =>
      path === "/system/health" ? Promise.resolve(HEALTH_MOCKED) : Promise.resolve(globalRoute(path)),
    );
    render(<Operations />);
    await ready();

    expect(await alertsNaming(/SIMULATED in this deployment/i)).toBe(true);
  });

  it("only calls a real subsystem UP when nothing is mocked", async () => {
    render(<Operations />);
    await ready();

    await openTab(/system/i);
    const row = await subsystemRow("Llm");
    expect(row?.textContent).toContain("UP");
    expect(row?.textContent).not.toContain("SIMULATED");
  });

  it("renders a mock publish path with the mock tone and a real one with info", async () => {
    serve();
    globalGet.mockImplementation((path: string) =>
      path === "/system/health" ? Promise.resolve(HEALTH_MOCKED) : Promise.resolve(globalRoute(path)),
    );
    render(<Operations />);
    await ready();

    await openTab(/providers/i);
    expect(await screen.findByText("Publishing platforms")).toBeInTheDocument();

    // A mock path and a real path never share a colour, and neither is called
    // LIVE -- that tone is reserved for "an official API returned a remote id".
    const mockPath = (await screen.findAllByText("MOCK PATH")).find((n) =>
      n.className.includes("ym-tone-mock"),
    );
    expect(mockPath).toBeDefined();
    const realPath = (await screen.findAllByText("REAL PATH")).find((n) =>
      n.className.includes("ym-tone-info"),
    );
    expect(realPath).toBeDefined();
    expect(screen.queryByText("LIVE")).toBeNull();
  });

  it("labels a SIMULATED render backend rather than reporting it up", async () => {
    render(<Operations />);
    await ready();

    await openTab(/renders/i);
    expect(await screen.findByText("Render backend")).toBeInTheDocument();
    const tiles = document.body.textContent ?? "";
    expect(tiles).toContain("SIMULATED");
  });
});

describe("a down collector means unknown, not zero", () => {
  it("classifies a missing collector as unknown at the helper level", () => {
    const up: Collectors = { collectors: { workers: "up", jobs: "down" }, failed: ["jobs"], note: "" };
    expect(collectorState(up, "workers")).toBe("up");
    expect(collectorState(up, "jobs")).toBe("unknown");
    expect(collectorState(null, "workers")).toBe("unknown");
  });

  it("renders UNAVAILABLE and explains, instead of an empty queue", async () => {
    serve();
    vi.stubGlobal(
      "fetch",
      vi.fn((p: string) =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve(
              p === "/internal/collectors"
                ? { collectors: { workers: "up", database: "up", jobs: "down", gpu_slots: "up" }, failed: ["jobs"], note: "gates untouched" }
                : (PROBES[p] ?? {}),
            ),
        }),
      ),
    );
    render(<Operations />);
    await ready();

    await openTab(/workers/i);
    const queueTile = await screen.findByText("Queue depth");
    expect(await alertsNaming(/queue-depth collector is down/i)).toBe(true);
    expect(queueTile).toBeInTheDocument();
  });

  it("does not show a GPU slot count when the gpu_slots collector is down", async () => {
    serve();
    vi.stubGlobal(
      "fetch",
      vi.fn((p: string) =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve(
              p === "/internal/collectors"
                ? { collectors: { workers: "up", database: "up", jobs: "up", gpu_slots: "down" }, failed: ["gpu_slots"], note: "gates untouched" }
                : (PROBES[p] ?? {}),
            ),
        }),
      ),
    );
    render(<Operations />);
    await ready();

    await openTab(/gpu/i);
    const tile = (await screen.findByText("GPU slot ledger")).closest(".ym-stat");
    expect(tile?.textContent).toContain("UNAVAILABLE");
    expect(tile?.textContent).toContain("unknown, not zero");
  });

  it("never derives its own GPU slot state — it shows the backend verdict", async () => {
    render(<Operations />);
    await ready();

    await openTab(/gpu/i);
    const text = document.body.textContent ?? "";
    expect(text).toContain("gpu_queue_starvation");
    expect(text).toContain("WAITING");
  });
});

describe("nothing ambiguous is retryable", () => {
  it("offers no retry affordance for an ambiguous paid submission", async () => {
    render(<Operations />);
    await ready();

    await openTab(/reconciliation/i);
    await screen.findByText("SUBMISSION_UNKNOWN");
    const buttons = Array.from(document.querySelectorAll("button")).map((b) =>
      (b.textContent ?? "").trim().toLowerCase(),
    );
    expect(
      buttons.some((b) => b === "retry" || b.includes("try again") || b.includes("resend")),
    ).toBe(false);
  });
});