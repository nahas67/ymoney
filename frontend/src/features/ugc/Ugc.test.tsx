/** @vitest-environment jsdom */
/* UGC — the preset vocabulary belongs to the backend, and a FAIL is a gate, not
 * a warning.
 *
 * Two claims carry this screen. First, there is NO preset table in the client:
 * `engine/ugc/pipeline.py` owns `UGC_PRESETS` / `PRESET_DEFAULTS` and
 * `create_project` answers 422 for anything else, so a locally hardcoded list
 * would be a second business rule that silently drifts. Second, a QC FAIL must
 * stop the render — `UGCBlockedError` becomes 409 — so the control is disabled
 * and the refusal is explained rather than left to be discovered.
 *
 * So the assertions are:
 *   - a project row and its preset render when the reads succeed;
 *   - a failed read is an alert naming the failure, not an empty table and not 0;
 *   - an empty read stays visibly different from a failed one;
 *   - every preset the backend serves is offered, and none is invented;
 *   - a QC-FAIL project offers no render;
 *   - a refused paid run offers no retry.
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

import Ugc, { readStages, ugcQcTone } from "./Ugc";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
  caps = CAPABILITIES;
});

/* ugc_router.list_presets: `{"key": key, **PRESET_DEFAULTS[key]}`. */
const PRESETS = {
  total: 2,
  workspace_id: "ws-1",
  items: [
    { key: "PRODUCT_DEMO", hook_type: "curiosity_gap", duration_seconds: 30, format: "hands-on product demo over product shots" },
    { key: "TALKING_HEAD", hook_type: "question", duration_seconds: 30, format: "direct-to-camera narration" },
  ],
};

/* ugc_router._project_dto(UgcProjectRow). */
const PROJECT = {
  id: "ugc-1",
  workspace_id: "ws-1",
  preset: "PRODUCT_DEMO",
  status: "READY",
  brief: { topic: "YMoney expense cards", audience: "freelancers", product_assets: ["asset-1"] },
  timeline_id: "tl-1",
  render_asset_ref: "",
  qc: {
    status: "PASS",
    checks: {
      hook_timing: { status: "pass", detail: "hook within 3.5s" },
      product_assets: { status: "pass", detail: "1 declared reference resolved" },
    },
    preset: "PRODUCT_DEMO",
    report_type: "ugc",
  },
  lineage: {
    preset: "PRODUCT_DEMO",
    topic: "YMoney expense cards",
    audience: "freelancers",
    hook: "Nobody told freelancers this",
    script: "Nobody told freelancers this. YMoney expense cards fix it.",
    script_source: "llm",
    voice: [{ asset_id: "voice-1", duration: 3.2, words: 9 }],
    music: { track: "bed", generated: false, reason: "music generation is opt-in and no policy enables it" },
    product_assets: { resolved: [{ asset_id: "asset-1", ref: "asset-1", storage_key: "uploads/a.png", type: "image", role: "product_image" }], unresolved: [] },
    broll_plan: [{ index: 0, query: "expense cards" }],
    cta: { text: "Try YMoney", spoken: true },
    timeline_id: "tl-1",
    manifest_hash: "abcdef0123456789",
    generated_version: 1,
  },
  created_at: "2026-03-01T09:00:00Z",
  updated_at: "2026-03-01T09:05:00Z",
};

const PROJECT_DETAIL = {
  ...PROJECT,
  product_assets: {
    resolved: [{ asset_id: "asset-1", ref: "asset-1", storage_key: "uploads/a.png", type: "image", role: "product_image" }],
    unresolved: [],
  },
  open_in_editor: true,
};

const AVATARS = {
  total: 1,
  items: [
    {
      id: "av-1",
      workspace_id: "ws-1",
      name: "Brand spokesperson",
      profile: {
        name: "Brand spokesperson",
        source_asset_ref: "portrait-1",
        voice_ref: "",
        expression_preset: "neutral",
        motion_preset: "subtle",
        framing: "medium_closeup",
        background: "studio",
        language: "en",
        brand_association: "YMoney",
        provider: "sadtalker",
        status: "active",
      },
      source_asset_ref: "portrait-1",
      consent_state: "pending",
      consent: {
        state: "pending",
        source: "",
        granted_by: "",
        granted_at: "",
        authorization_evidence: {},
        statement: "",
      },
      provider: "sadtalker",
      status: "active",
      created_at: "2026-03-01T08:00:00Z",
    },
  ],
};

const AVATAR_HEALTH = {
  provider: "sadtalker",
  ready: true,
  detail: "",
  consent_required: true,
  consent_states: ["authorized", "pending", "revoked"],
  health: {},
  capabilities: { lanes: "server" },
  workspace_id: "ws-1",
};

function route(path: string): unknown {
  if (path.startsWith("/ugc/presets")) return PRESETS;
  if (path === "/ugc/projects") return { total: 1, items: [PROJECT] };
  if (path.startsWith("/ugc/projects/")) return PROJECT_DETAIL;
  if (path.startsWith("/avatars/health")) return AVATAR_HEALTH;
  if (path === "/avatars") return AVATARS;
  return {};
}

/**
 * Longest-prefix wins, so an override for `/ugc/projects` does not swallow the
 * `/ugc/projects/ugc-1` detail read the way a naive `startsWith` would.
 */
function serve(overrides: Record<string, unknown> = {}) {
  const keys = Object.keys(overrides).sort((a, b) => b.length - a.length);
  wsGet.mockImplementation((path: string) => {
    for (const prefix of keys) {
      if (path.startsWith(prefix)) {
        const value = overrides[prefix];
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

function renderUgc() {
  return render(<Ugc />);
}

describe("UGC projects list states", () => {
  it("renders a project row when the list arrives", async () => {
    serve();
    renderUgc();

    expect(await screen.findByText(/YMoney expense cards/)).toBeInTheDocument();
    expect(screen.getByText("Product Demo")).toBeInTheDocument();
    expect(screen.getByText("Pass")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reports a failed project read as an alert naming the failure, not as zero projects", async () => {
    fail((p) => p === "/ugc/projects", "ugc backend unavailable");
    renderUgc();

    // Two surfaces report it (the at-a-glance summary and the table), and every
    // one of them names the failure rather than showing a count.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.length).toBeGreaterThan(0);
    for (const alert of alerts) expect(alert).toHaveTextContent(/ugc backend unavailable/i);
    expect(document.body.textContent).toMatch(/UNAVAILABLE/);
  });

  it("does not present a failed read as an empty project list", async () => {
    fail((p) => p === "/ugc/projects", "ugc backend unavailable");
    renderUgc();

    await screen.findAllByRole("alert");
    expect(document.body.textContent).not.toMatch(/no ugc project yet/i);
  });

  it("keeps a failure distinguishable from a genuinely empty list", async () => {
    serve({ "/ugc/projects": { total: 0, items: [] } });
    renderUgc();

    expect(await screen.findByText(/no ugc project yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows UNAVAILABLE for an unreadable count rather than reporting 0", async () => {
    fail((p) => p === "/avatars", "avatar store offline");
    renderUgc();

    await screen.findAllByRole("alert");
    expect(document.body.textContent).toMatch(/UNAVAILABLE/);
    expect(document.body.textContent).toMatch(/avatar store offline/i);
  });
});

describe("the preset vocabulary is the backend's, never the client's", () => {
  it("offers every preset the backend serves and no others", async () => {
    serve();
    renderUgc();
    await screen.findByText(/YMoney expense cards/);

    const body = document.body.textContent ?? "";
    for (const p of PRESETS.items) expect(body).toContain(p.key);
    // Presets that exist in pipeline.py but were NOT served by this backend must
    // not appear: inventing them would produce a 422 on create.
    for (const absent of ["UNBOXING", "REACTION", "BEFORE_AFTER", "FOUNDER_STYLE", "TESTIMONIAL", "REVIEW", "PROBLEM_SOLUTION"]) {
      expect(body).not.toContain(absent);
    }
  });

  it("blocks creation and says why when the preset read fails, rather than falling back to a guess", async () => {
    fail((p) => p === "/ugc/presets", "preset service offline");
    renderUgc();
    await screen.findByText(/YMoney expense cards/);

    await userEvent.click(screen.getAllByRole("button", { name: /new project/i })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/preset service offline/i);
    // No fabricated option is offered.
    expect(within(dialog).queryByRole("option", { name: /product demo/i })).toBeNull();
    expect(within(dialog).getByRole("button", { name: /POST \/ugc\/projects/i })).toBeDisabled();
  });

  it("reports a missing PRESET_DEFAULTS duration as UNAVAILABLE, not as zero seconds", async () => {
    serve({
      "/ugc/presets": { total: 1, workspace_id: "ws-1", items: [{ key: "REACTION", hook_type: "bold_claim" }] },
      "/ugc/projects": { total: 0, items: [] },
    });
    renderUgc();

    expect(await screen.findByText("REACTION")).toBeInTheDocument();
    expect(screen.getByTitle(/no duration for this preset/i)).toHaveTextContent("UNAVAILABLE");
  });
});

describe("a QC FAIL is a gate, not a warning", () => {
  const BLOCKED = {
    ...PROJECT,
    status: "BLOCKED",
    qc: {
      status: "FAIL",
      checks: { product_assets: { status: "fail", detail: "1 declared reference did not resolve" } },
      preset: "PRODUCT_DEMO",
      report_type: "ugc",
    },
  };

  it("renders a FAIL verdict as danger and never as a pass", () => {
    expect(ugcQcTone("FAIL")).toBe("danger");
    expect(ugcQcTone("PASS")).toBe("success");
    expect(ugcQcTone("PASS_WITH_WARNINGS")).toBe("warning");
    expect(ugcQcTone("REVIEW_REQUIRED")).toBe("unknown");
  });

  it("offers no render control for a project QC blocked", async () => {
    serve({
      "/ugc/projects": { total: 1, items: [BLOCKED] },
      "/ugc/projects/ugc-1": {
        ...BLOCKED,
        product_assets: { resolved: [], unresolved: [{ entry: "asset-9", reason: "not a workspace MediaAsset" }] },
        open_in_editor: false,
      },
    });
    renderUgc();
    await userEvent.click(await screen.findByText(/YMoney expense cards/));

    await screen.findByText("Workflow");
    const renderButton = await screen.findByRole("button", { name: /POST \/ugc\/projects\/.*\/render/i });
    expect(renderButton).toBeDisabled();
    // The refusal is explained, and the route's own status is named.
    expect(document.body.textContent).toMatch(/409/);
  });

  it("reports a refused render with no retry, because a render may already have spent time", async () => {
    wsPost.mockRejectedValue(new Error("rendered output failed QC (FAIL) — output retained but the project stays BLOCKED"));
    serve();
    renderUgc();
    await userEvent.click(await screen.findByText(/YMoney expense cards/));

    await screen.findByRole("heading", { name: "Render" });
    await userEvent.click(screen.getByRole("button", { name: /POST \/ugc\/projects\/.*\/render/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/stays BLOCKED/i);
    expect(within(alert).queryByRole("button", { name: /retry/i })).toBeNull();
    expect(wsPost).toHaveBeenCalledTimes(1);
  });
});

describe("the workflow strip reports absence, never a guess", () => {
  it("marks an unrecorded stage as absent rather than done", () => {
    const stages = readStages({}, { status: "", checks: {} }, { requested: "", resolved: false, consent: null });
    expect(stages).toHaveLength(12);
    // A DRAFT's lineage is empty, so eleven stages are simply absent.
    expect(stages.filter((s) => s.id !== "presenter").every((s) => s.state === "absent")).toBe(true);
    // Talent is the one exception: "no presenter requested" is a settled DECISION
    // recorded by stage_presenter, not a stage that failed to write.
    const talent = stages.find((s) => s.id === "presenter");
    expect(talent?.state).toBe("done");
    expect(talent?.detail).toMatch(/no presenter requested/i);
  });

  it("treats a declined music bed as a recorded decision, not a missing stage", () => {
    // stage_music writes {generated: false, reason: …} when policy declines.
    const stages = readStages(
      { music: { track: "bed", generated: false, reason: "opt-in only" } },
      { status: "PASS", checks: {} },
      { requested: "", resolved: false, consent: null },
    );
    const music = stages.find((s) => s.id === "music");
    expect(music?.state).toBe("warn");
    expect(music?.detail).toMatch(/opt-in only/);
  });

  it("fails the talent stage when the requested profile is not authorized", () => {
    // stage_presenter calls require_authorized BEFORE voice/avatar work.
    const stages = readStages(
      { script: "s" },
      { status: "", checks: {} },
      { requested: "av-1", resolved: true, consent: "pending" },
    );
    const talent = stages.find((s) => s.id === "presenter");
    expect(talent?.state).toBe("failed");
    expect(talent?.detail).toMatch(/require_authorized/i);
  });

  it("warns rather than inventing when the requested profile does not exist here", () => {
    const stages = readStages(
      { script: "s" },
      { status: "", checks: {} },
      { requested: "av-missing", resolved: false, consent: null },
    );
    expect(stages.find((s) => s.id === "presenter")?.state).toBe("warn");
  });
});

describe("consent and provider maturity are shown as the backend states them", () => {
  it("shows a pending avatar as pending, with its evidence column empty rather than invented", async () => {
    serve();
    renderUgc();
    await userEvent.click(await screen.findByRole("tab", { name: /talent/i }));

    expect(await screen.findByText("Brand spokesperson")).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.getByText(/none recorded/i)).toBeInTheDocument();
    // An unauthorized profile offers authorization, never a render.
    expect(screen.getByRole("button", { name: /authorize…/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^render…$/i })).toBeNull();
  });

  it("renders a not-ready avatar backend as not ready, with its remediation", async () => {
    serve({
      "/avatars/health": { ...AVATAR_HEALTH, ready: false, detail: "no SadTalker on this host" },
    });
    renderUgc();
    await userEvent.click(await screen.findByRole("tab", { name: /talent/i }));

    await screen.findByText("Backend ready");
    expect(screen.getByText("NOT READY")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/no SadTalker on this host/i);
    expect(screen.queryByText("READY")).toBeNull();
  });

  it("renders an absent ready flag as UNAVAILABLE rather than not-ready", async () => {
    serve({ "/avatars/health": { provider: "sadtalker", consent_states: ["authorized"] } });
    renderUgc();
    await userEvent.click(await screen.findByRole("tab", { name: /talent/i }));

    await screen.findByText("Backend ready");
    expect(screen.getAllByText("UNAVAILABLE").length).toBeGreaterThan(0);
    expect(screen.queryByText("NOT READY")).toBeNull();
    expect(screen.queryByText("READY")).toBeNull();
  });
});

describe("a refused paid run offers no generic retry", () => {
  it("reports the refusal once and never offers to send the pipeline again", async () => {
    serve();
    wsPost.mockRejectedValue(new Error("voice stage produced no audio — configure TTS or supply brief.script narration"));
    renderUgc();
    await screen.findByText(/YMoney expense cards/);

    await userEvent.click(screen.getAllByRole("button", { name: /new project/i })[0]);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByRole("option", { name: /product demo/i })).toBeInTheDocument());

    await userEvent.selectOptions(within(dialog).getByLabelText(/preset/i), "PRODUCT_DEMO");
    await userEvent.type(within(dialog).getByLabelText(/topic \/ product name/i), "YMoney expense cards");
    await userEvent.click(within(dialog).getByRole("button", { name: /POST \/ugc\/projects/i }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/voice stage produced no audio/i);
    expect(within(alert).queryByRole("button", { name: /retry/i })).toBeNull();
    expect(wsPost).toHaveBeenCalledTimes(1);
  });
});

describe("capability gating is an affordance, and says why", () => {
  it("hides the create controls and explains the block when content.write is absent", async () => {
    caps = { known: true, list: ["content.read"] };
    serve();
    renderUgc();

    await screen.findByText(/YMoney expense cards/);
    expect(screen.queryByRole("button", { name: /new project/i })).toBeNull();
    expect(screen.getAllByText("BLOCKED").length).toBeGreaterThan(0);
    expect(document.body.textContent).toMatch(/does not include "content\.write"/);
  });

  it("keeps the read surfaces intact for a viewer", async () => {
    caps = { known: true, list: ["content.read"] };
    serve();
    renderUgc();

    await screen.findByText(/YMoney expense cards/);
    expect(screen.getAllByText("Registered presets").length).toBeGreaterThan(0);
    expect(screen.getByText("At a glance")).toBeInTheDocument();
  });
});
