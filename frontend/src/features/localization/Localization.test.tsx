/** @vitest-environment jsdom */
/* Localization — a blocked provider must never read as available, and a paid
 * refusal must never invite a blind retry.
 *
 * Those are the two claims this screen makes that a cheaper implementation gets
 * wrong. `lipsync/base.py` computes `available` as true for BOTH `available` and
 * `degraded`, so a naive badge paints a degraded provider green. And
 * `POST /localization/run` reserves LLM budget per 20-segment batch, keeping an
 * unresolved batch as an UNKNOWN EXPOSURE on purpose — a "Retry" button there is
 * an invitation to double-charge.
 *
 * So the assertions are about HONESTY and DISTINCTNESS:
 *   - a run row renders when the read succeeds;
 *   - a failed read is an alert naming the failure, not an empty table and not 0;
 *   - an empty read and a failed read are visibly different;
 *   - a degraded / unavailable provider is not shown as available;
 *   - a refused paid mutation offers no Retry.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const wsGet = vi.fn();
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
    post: (path: string, body?: unknown) => wsPost(path, body),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
  api: vi.fn(),
}));

/* `can` / `blockedReason` live in the same module as `useSession` and both read
 * the session internally, so the mock has to stand in for all three. */
const CAPABILITIES = { known: true, list: ["content.read", "content.write"] };
let caps = CAPABILITIES;

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: caps.list,
    capabilitiesKnown: caps.known,
    switchWorkspace: () => {},
    reload: () => {},
  }),
  can: (permission: string | null) => {
    if (!permission) return true;
    if (!caps.known) return true;
    return caps.list.includes(permission);
  },
  blockedReason: (permission: string | null) => {
    if (!permission) return null;
    if (!caps.known || caps.list.includes(permission)) return null;
    return `Your workspace role does not include "${permission}". The server enforces this.`;
  },
}));

import Localization, { lipSyncTone, qcTone } from "./Localization";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
  caps = CAPABILITIES;
});

/* Typed from localization_router._dto() over models/localization.py. */
const RUN = {
  id: "loc-1",
  workspace_id: "ws-1",
  source_content_id: "content-1",
  child_content_id: "content-2",
  timeline_id: "tl-1",
  language: "es",
  locale: "es-MX",
  translation_version: 1,
  status: "READY",
  error: "",
  qc: { id: "qc-1", status: "PASS_WITH_WARNINGS", created_at: "2026-02-01T10:00:00Z", counts: { warnings: 2 } },
  stages: [{ stage: "translate", status: "ok", detail: "12 segment(s)" }],
  warnings: [],
  repairs: [],
  costs: { translation_chars: 1480, audio_files: 12 },
  created_at: "2026-02-01T09:00:00Z",
  updated_at: "2026-02-01T10:00:00Z",
};

const COST_SUMMARY = {
  last_24h_by_category: { llm: 0.31 },
  spent_last_24h_usd: 0.31,
  daily_budget_usd: 5,
  per_video_budget_usd: 0.5,
  within_budget: true,
  remaining_usd: 4.69,
};

const DUB_STATUS = {
  ffmpeg: true,
  llm: true,
  tts: true,
  tts_provider: "edge_tts",
  languages: ["de", "en", "es", "fr", "ja", "pt"],
  ready: true,
};

function route(path: string): unknown {
  if (path.startsWith("/localization")) return { total: 1, items: [RUN] };
  if (path.startsWith("/jobs")) return { items: [] };
  if (path === "/costs") return COST_SUMMARY;
  if (path === "/assets/dub/status") return DUB_STATUS;
  if (path.startsWith("/content")) return { items: [{ id: "content-1", topic: "Spring push", status: "READY" }] };
  if (path.startsWith("/lipsync/health")) return { provider: "musetalk", status: "available", available: true, detail: "" };
  if (path.startsWith("/lipsync/jobs")) return { total: 0, items: [] };
  if (path.startsWith("/dubbing/plans")) return { total: 0, items: [] };
  if (path.startsWith("/media-intel/speakers/aliases")) return { items: [], count: 0 };
  if (path.startsWith("/media-intel/speakers")) return { items: [], count: 0, anonymous: true, note: "anonymous ids" };
  return {};
}

function serve(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation((path: string) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) {
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
      }
    }
    return Promise.resolve(route(path));
  });
}

function fail(predicate: (p: string) => boolean, message: string) {
  const base = route;
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(base(path)),
  );
}

function renderLocalization() {
  return render(<Localization />);
}

describe("Localization runs list states", () => {
  it("renders a run row when the list arrives", async () => {
    serve();
    renderLocalization();

    // es-MX is the row's locale; the language column renders it beside ES.
    expect(await screen.findByText(/es-MX/)).toBeInTheDocument();
    expect(screen.getByText("Pass With Warnings")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reports a failed run read as an alert naming the failure, not as zero runs", async () => {
    fail((p) => p === "/localization", "localization backend unavailable");
    renderLocalization();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/localization backend unavailable/i);

    // And the cost/budget panel keeps its own independent read alive.
    await waitFor(() => expect(screen.getByText("$4.6900")).toBeInTheDocument());
  });

  it("does not present a failed read as an empty run list", async () => {
    fail((p) => p === "/localization", "localization backend unavailable");
    renderLocalization();

    await screen.findByRole("alert");
    expect(document.body.textContent).not.toMatch(/no localization run yet/i);
  });

  it("keeps a failure distinguishable from a genuinely empty list", async () => {
    serve({ "/localization": { total: 0, items: [] } });
    renderLocalization();

    // Empty is a settled answer and it SAYS SO — no alert anywhere.
    expect(await screen.findByText(/no localization run yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows UNAVAILABLE rather than a zero for the cost ledger when only that read fails", async () => {
    fail((p) => p === "/costs", "ledger offline");
    renderLocalization();

    await screen.findByRole("alert");
    // The run list still renders; the budget tiles refuse to invent a figure.
    expect(await screen.findByText(/es-MX/)).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/UNAVAILABLE/);
    expect(document.body.textContent).not.toMatch(/\$0\.0000/);
  });
});

describe("a blocked provider is never presented as available", () => {
  it("does not report a degraded lip-sync provider as available", () => {
    // ProviderHealth.available is TRUE for degraded; showing it as available is
    // the exact bug the screen exists to prevent.
    expect(lipSyncTone({ status: "degraded", available: true })).not.toBe("success");
    expect(lipSyncTone({ status: "degraded", available: true })).toBe("warning");
    expect(lipSyncTone({ status: "available", available: true })).toBe("success");
    expect(lipSyncTone({ status: "unavailable", available: false })).toBe("danger");
  });

  it("renders a DEGRADED provider with its degradation visible, not as ready", async () => {
    serve({
      "/lipsync/health": {
        provider: "musetalk",
        status: "degraded",
        available: true,
        detail: "GPU busy; falling back to the server lane",
      },
    });
    renderLocalization();
    await userEvent.click(await screen.findByRole("tab", { name: /lip-sync/i }));

    expect(await screen.findByText("Availability")).toBeInTheDocument();
    expect(screen.getByText("DEGRADED")).toBeInTheDocument();
    expect(screen.getByText(/GPU busy/)).toBeInTheDocument();
    expect(screen.queryByText("AVAILABLE")).toBeNull();
  });

  it("renders an unavailable provider's remediation instead of implying it works", async () => {
    serve({
      "/lipsync/health": {
        provider: "musetalk",
        status: "unavailable",
        available: false,
        detail: "no GPU on this host",
        remediation: "configure a lip-sync backend under Settings → Connections",
      },
    });
    renderLocalization();
    await userEvent.click(await screen.findByRole("tab", { name: /lip-sync/i }));

    await screen.findByText("Availability");
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
    expect(screen.getByText(/configure a lip-sync backend/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/not available/i);
  });

  it("renders a missing provider flag as UNAVAILABLE, not as false", async () => {
    // dub_status with the keys stripped: the checks were never made.
    serve({ "/assets/dub/status": { languages: ["en"] } });
    renderLocalization();
    await userEvent.click(await screen.findByRole("tab", { name: /dubbing/i }));

    await screen.findByText("Dubbing pipeline maturity");
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
    expect(screen.queryByText("NOT READY")).toBeNull();
  });

  it("never paints a PASS QC verdict as a neutral/no-information state", () => {
    // toneForStatus maps PASS to neutral; a clean report must still read clean.
    expect(qcTone("PASS")).toBe("success");
    expect(qcTone("PASS_WITH_WARNINGS")).toBe("warning");
    expect(qcTone("FAIL")).toBe("danger");
    // A human decision is neither a pass nor a failure.
    expect(qcTone("REVIEW_REQUIRED")).toBe("unknown");
  });
});

describe("a paid action offers no generic retry", () => {
  async function openRunModal() {
    renderLocalization();
    await screen.findByText(/es-MX/);
    await userEvent.click(screen.getAllByRole("button", { name: /new run/i })[0]);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("es")).toBeInTheDocument());
    return dialog;
  }

  it("reports a refused localization run without offering to send it again", async () => {
    serve();
    wsPost.mockRejectedValue(
      new Error("translation budget refused before batch 1/1: daily cap reached. Nothing was sent"),
    );
    const dialog = await openRunModal();

    await userEvent.selectOptions(within(dialog).getByLabelText(/source content/i), "content-1");
    await userEvent.click(within(dialog).getByText("es"));

    await userEvent.click(within(dialog).getByRole("button", { name: /POST \/localization\/run/i }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/daily cap reached/i);
    // The refusal explains itself and stops. There is no Retry anywhere in it.
    expect(within(alert).queryByRole("button", { name: /retry/i })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /^retry$/i })).toBeNull();
    /* And it was sent exactly once — a retry control is how that becomes two. */
    expect(wsPost).toHaveBeenCalledTimes(1);
  });

  it("reports a refused lip-sync submit with no retry, because a GPU submit may already be billed", async () => {
    serve({
      "/lipsync/health": {
        provider: "musetalk",
        status: "available",
        available: true,
        detail: "",
      },
    });
    wsPost.mockRejectedValue(new Error("lip-sync unavailable: no GPU; configure a backend"));
    renderLocalization();
    await userEvent.click(await screen.findByRole("tab", { name: /lip-sync/i }));

    await screen.findByText("Submit a lip-sync job");
    await userEvent.type(screen.getByLabelText(/video ref/i), "asset-video-1");
    await userEvent.type(screen.getByLabelText(/audio ref/i), "asset-audio-1");
    await userEvent.click(screen.getByRole("button", { name: /POST \/lipsync\/jobs/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/no GPU/i);
    expect(within(alert).queryByRole("button", { name: /retry/i })).toBeNull();
    expect(wsPost).toHaveBeenCalledTimes(1);
  });
});

describe("capability gating is an affordance, and says why", () => {
  it("hides every localization mutation and explains the block when content.write is absent", async () => {
    caps = { known: true, list: ["content.read"] };
    serve();
    renderLocalization();

    await screen.findByText(/es-MX/);
    // The header and the panel both drop the create control.
    expect(screen.queryByRole("button", { name: /new run/i })).toBeNull();
    expect(screen.getAllByText("BLOCKED").length).toBeGreaterThan(0);
    expect(document.body.textContent).toMatch(/does not include "content\.write"/);
  });

  it("keeps the read surfaces intact for a viewer: a gate is not a blank screen", async () => {
    caps = { known: true, list: ["content.read"] };
    serve();
    renderLocalization();

    await screen.findByText(/es-MX/);
    expect(screen.getAllByText("Localization runs").length).toBeGreaterThan(0);
    expect(screen.getByText("Cost and budget")).toBeInTheDocument();
  });
});
