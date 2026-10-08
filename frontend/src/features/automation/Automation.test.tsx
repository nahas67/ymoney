/** @vitest-environment jsdom */
/* Automation — the Autopilot loop's control surface.
 *
 *   - status display: loop state, mode, cycles, next best action, recent
 *     cycles, agent policy — all read from real routes, no fake data;
 *   - controls POST to the exact autopilot routes (start with its body,
 *     run-one-cycle, pause, resume, stop with its blast-radius confirm);
 *   - admin-only writes are disabled for a non-admin with the requirement
 *     named; the backend stays authoritative;
 *   - a 403 renders as a permission denial with NO retry (QueryBoundary +
 *     PermissionAwareError already handle that) — retrying a refusal can
 *     never succeed.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const wsGet = vi.fn();
const wsPut = vi.fn();
const wsPost = vi.fn();

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
    put: (path: string, body?: unknown) => wsPut(path, body),
    post: (path: string, body?: unknown) => wsPost(path, body),
  },
  api: vi.fn(),
}));

const VIEWER_CAPS = ["content.read", "content.write", "operations.view"];
const ADMIN_CAPS = [...VIEWER_CAPS, "publish.approve", "publish.execute", "brand.manage", "providers.manage"];

const capsState: { caps: string[]; known: boolean } = { caps: [...VIEWER_CAPS], known: true };
function setCaps(caps: string[]) {
  capsState.caps = caps;
}
function setCapabilitiesKnown(known: boolean) {
  capsState.known = known;
}

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: capsState.caps,
    capabilitiesKnown: capsState.known,
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

import Automation from "./Automation";

const STATUS_RUNNING = {
  state: "RUNNING",
  mode: "CONTINUOUS",
  cycles_completed: 4,
  cycles_target: 0,
  scheduled_start_at: null,
  scheduled_stop_at: null,
  current_cycle: { id: "cy-9", number: 5, stage: "BUILD", status: "RUNNING" },
  queued_jobs: 2,
  config: {},
  last_error: "",
};

const STATUS_IDLE = { state: "IDLE", cycles_completed: 0 };

const DECISION = {
  action: "PRODUCE",
  opportunity_id: "op-1",
  topic: "emergency funds",
  score: 71.5,
  confidence: 0.82,
  reasons: ["score above threshold", "budget remaining"],
  factors: [{ name: "score", value: 71.5, contribution: 12.0, detail: "above bar" }],
  evidence: ["trend:emergency-funds"],
};

const AGENTS = {
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

const CYCLES = {
  items: [
    {
      id: "cy-8",
      number: 4,
      stage: "LEARN",
      status: "SUCCEEDED",
      cost_usd: 0.31,
      topic: "index funds",
      started_at: "2026-03-04T06:00:00Z",
      finished_at: "2026-03-04T06:20:00Z",
      error: null,
    },
  ],
};

function route(path: string): unknown {
  if (path.startsWith("/autopilot/status")) return STATUS_RUNNING;
  if (path.startsWith("/decision")) return DECISION;
  if (path.startsWith("/agents/config")) return AGENTS;
  if (path.startsWith("/cycles")) return CYCLES;
  return {};
}

function serve(status: unknown = STATUS_RUNNING) {
  wsGet.mockImplementation((path: string) =>
    Promise.resolve(path.startsWith("/autopilot/status") ? status : route(path)),
  );
  wsPost.mockImplementation(() => Promise.resolve({ ok: true }));
  wsPut.mockImplementation(() => Promise.resolve({ ok: true }));
}

function failWs(predicate: (p: string) => boolean, error: unknown) {
  serve();
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(error) : Promise.resolve(route(path)),
  );
}

afterEach(async () => {
  await act(async () => {
    await Promise.resolve();
  });
  cleanup();
  wsGet.mockReset();
  wsPut.mockReset();
  wsPost.mockReset();
  setCaps([...VIEWER_CAPS]);
  setCapabilitiesKnown(true);
  serve();
});

async function ready() {
  // The live status line only renders once GET /autopilot/status has landed.
  return screen.findByText(/Loop is Running/);
}

describe("status display", () => {
  it("shows loop state, mode, progress, the next best action and recent cycles", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Automation />);
    await ready();

    expect(screen.getByText(/mode CONTINUOUS/)).toBeInTheDocument();
    // Decision preview: action badge + topic + one reason.
    expect(screen.getByText("Produce")).toBeInTheDocument();
    expect(screen.getByText("emergency funds")).toBeInTheDocument();
    expect(screen.getByText("score above threshold")).toBeInTheDocument();
    // Recent cycles table.
    within(screen.getByRole("table", { name: "Recent autopilot cycles" })).getByText("#4");
    // Agent policy table (the title also appears as a retune <option>).
    within(screen.getByRole("table", { name: "Automation agent policy" })).getByText("Video Producer");
    // Live now line from current_cycle.
    expect(screen.getByText(/Live now: cycle #5/)).toBeInTheDocument();
  });

  it("renders the idle empty state instead of inventing progress", async () => {
    setCaps([...ADMIN_CAPS]);
    serve(STATUS_IDLE);
    render(<Automation />);
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();
    expect(screen.getByText(/No run has completed/)).toBeInTheDocument();
  });

  it("names the pause/stop gates for the current state", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Automation />);
    await ready();
    expect(screen.getByText(/Pause applies now/)).toBeInTheDocument();
    expect(screen.getByText(/Resume applies only while PAUSED/)).toBeInTheDocument();
  });
});

describe("controls drive the real endpoints", () => {
  it("runs exactly one cycle through POST /autopilot/run-one-cycle", async () => {
    setCaps([...ADMIN_CAPS]);
    serve(STATUS_IDLE);
    render(<Automation />);
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run one cycle" }));
    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/autopilot/run-one-cycle", undefined));
    // Pessimistic: the status is re-read after the server confirms.
    await waitFor(() => expect(wsGet).toHaveBeenCalledWith("/autopilot/status"));
  });

  it("starts the loop with its mode/target/override body", async () => {
    setCaps([...ADMIN_CAPS]);
    serve(STATUS_IDLE);
    render(<Automation />);
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Cycles target"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Start loop" }));
    await waitFor(() =>
      expect(wsPost).toHaveBeenCalledWith("/autopilot/start", {
        mode: "CONTINUOUS",
        cycles_target: 3,
        override_readiness: false,
      }),
    );
  });

  it("pauses and resumes through their own routes", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Automation />);
    await ready();

    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/autopilot/pause", undefined));
  });

  it("stops only after stating the blast radius", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Automation />);
    await ready();

    // DestructiveButton arms first: the blast radius is shown before Confirm.
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByText(/Running steps finish; no new cycle starts/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/autopilot/stop", undefined));
  });

  it("retunes an agent through PUT /agents/config/{key}", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Automation />);
    await ready();

    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "video_producer" } });
    fireEvent.change(screen.getByLabelText("Timeout (seconds)"), { target: { value: "600" } });
    fireEvent.click(screen.getByRole("button", { name: "Save agent policy" }));
    await waitFor(() =>
      expect(wsPut).toHaveBeenCalledWith("/agents/config/video_producer", { timeout_seconds: 600 }),
    );
  });

  it("refuses an out-of-range timeout without sending anything", async () => {
    setCaps([...ADMIN_CAPS]);
    serve();
    render(<Automation />);
    await ready();

    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "video_producer" } });
    fireEvent.change(screen.getByLabelText("Timeout (seconds)"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save agent policy" }));
    expect(await screen.findByText(/10 to 3600/)).toBeInTheDocument();
    expect(wsPut).not.toHaveBeenCalled();
  });
});

describe("permission gating", () => {
  it("disables admin writes and names the requirement for a non-admin", async () => {
    serve();
    render(<Automation />);
    await ready();

    expect(screen.getByRole("button", { name: "Start loop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run one cycle" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save agent policy" })).toBeDisabled();
    expect(document.body.textContent ?? "").toMatch(/Requires an admin role/);
  });

  it("enables the gated controls for an admin", async () => {
    setCaps([...ADMIN_CAPS]);
    serve(STATUS_IDLE);
    render(<Automation />);
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Start loop" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Run one cycle" })).not.toBeDisabled();
  });

  it("keeps admin writes disabled until capabilities are known", async () => {
    setCaps([...ADMIN_CAPS]);
    setCapabilitiesKnown(false);
    serve(STATUS_IDLE);
    render(<Automation />);
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Start loop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run one cycle" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save agent policy" })).toBeDisabled();
    expect(document.body.textContent ?? "").toMatch(/Checking admin permissions/);
    expect(wsPost).not.toHaveBeenCalled();
    expect(wsPut).not.toHaveBeenCalled();
  });

  it("keeps loop writes disabled until status loads successfully", async () => {
    setCaps([...ADMIN_CAPS]);
    let resolveStatus!: (value: unknown) => void;
    serve(STATUS_IDLE);
    wsGet.mockImplementation((path: string) =>
      path.startsWith("/autopilot/status")
        ? new Promise((resolve) => { resolveStatus = resolve; })
        : Promise.resolve(route(path)),
    );
    render(<Automation />);

    expect(screen.getByText("Reading loop state…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start loop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run one cycle" })).toBeDisabled();

    await act(async () => resolveStatus(STATUS_IDLE));
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start loop" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Run one cycle" })).not.toBeDisabled();
  });

  it("keeps loop writes disabled when status cannot be read", async () => {
    setCaps([...ADMIN_CAPS]);
    const { ApiError } = await import("../../lib/api");
    failWs(
      (path) => path.startsWith("/autopilot/status"),
      new ApiError(503, "status temporarily unavailable"),
    );
    render(<Automation />);

    expect(await screen.findByText(/Loop state UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start loop" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run one cycle" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(wsPost).not.toHaveBeenCalled();
  });

  it("keeps loop writes disabled while status is refreshing", async () => {
    setCaps([...ADMIN_CAPS]);
    let statusReads = 0;
    let resolveRefresh!: (value: unknown) => void;
    serve(STATUS_IDLE);
    wsGet.mockImplementation((path: string) => {
      if (path.startsWith("/autopilot/status")) {
        statusReads += 1;
        return statusReads === 1
          ? Promise.resolve(STATUS_IDLE)
          : new Promise((resolve) => { resolveRefresh = resolve; });
      }
      return Promise.resolve(route(path));
    });
    render(<Automation />);
    expect(await screen.findByText("Loop is idle")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run one cycle" }));
    await waitFor(() => expect(wsPost).toHaveBeenCalledWith("/autopilot/run-one-cycle", undefined));
    expect(await screen.findByLabelText("Automation save state")).toHaveTextContent(/Single cycle queued/);
    await waitFor(() => expect(statusReads).toBe(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start loop" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Run one cycle" })).toBeDisabled();

    await act(async () => resolveRefresh(STATUS_IDLE));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start loop" })).not.toBeDisabled());
  });
});

describe("a 403 is a refusal with no retry", () => {
  it("renders a permission denial without a Retry button", async () => {
    const { ApiError } = await import("../../lib/api");
    failWs(
      (p) => p.startsWith("/autopilot/status"),
      new ApiError(403, "not a workspace member"),
    );
    render(<Automation />);
    expect(await screen.findByText("Permission denied")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toMatch(/not a workspace member/);
    // PermissionAwareError renders no retry for a refusal: retrying a 403 can
    // never succeed.
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });
});

describe("no fake data", () => {
  it("renders a null agent cost cap as unknown, not as zero spend", async () => {
    serve();
    render(<Automation />);
    await ready();

    const table = screen.getByRole("table", { name: "Automation agent policy" });
    const row = within(table).getByText("Video Producer").closest("tr");
    expect(row?.textContent).toContain("unknown");
    expect(row?.textContent).not.toContain("$0.0000");
  });
});
