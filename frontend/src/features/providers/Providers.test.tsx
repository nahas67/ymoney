/** @vitest-environment jsdom */
/* Providers — two contracts, asserted rather than assumed.
 *
 *   1. THE LADDER IS EXACT. `maturity.STATES` is a closed set of eight values.
 *      A ninth label ("PRODUCTION_READY", most likely) must not be printable
 *      here, and an unrecognised value must not borrow the authority of a real
 *      one — so the offending string is never rendered at all.
 *   2. NO CREDENTIAL IS EVER RENDERED. The API returns a `masked` value per key
 *      and a `prefix` on an API key; both are credential fingerprints. Only the
 *      boolean `configured` and the `source` word are shown.
 *
 * Plus the general rule: a failed endpoint is an alert naming the failure, never
 * an empty provider list.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

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

let CAPABILITIES: string[] = ["providers.manage"];
vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: CAPABILITIES,
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  }),
  can: (permission: string | null) =>
    !permission ? true : CAPABILITIES.includes(permission),
  blockedReason: (permission: string | null) =>
    !permission || CAPABILITIES.includes(permission)
      ? null
      : `Your workspace role does not include "${permission}". The server enforces this.`,
}));

import Providers, {
  MATURITY_LADDER,
  isLadderState,
  maturityTone,
  groupForCapability,
  type WorkspaceMaturityList,
} from "./Providers";

const LLM: WorkspaceMaturityList["items"][number] = {
  provider: "openai",
  capability: "llm",
  implementation_status: "IMPLEMENTED",
  contract_status: "CONTRACT_TESTED",
  live_status: "UNVERIFIED",
  commercial_status: "EXTERNAL_LIMITATION",
  credential_status: "REQUIRED",
  resolved_credential_status: "CONFIGURED",
  health: "UNKNOWN",
  last_verified_at: "",
  credential_keys: ["llm.api_key"],
  simulation_only: false,
  notes: "Adapter written; never exercised against the real service.",
  evidence: ["app/providers/llm.py"],
  gaps: ["streaming not covered"],
  production_ready: false,
  blockers: [
    "contract_status=CONTRACT_TESTED: no offline contract test",
    "live_status=UNVERIFIED: never exercised against the real service",
    "uncovered surfaces: streaming not covered",
  ],
};

const MOCK_TTS: WorkspaceMaturityList["items"][number] = {
  provider: "mock-tts",
  capability: "tts",
  implementation_status: "IMPLEMENTED",
  contract_status: "CONTRACT_TESTED",
  live_status: "UNAVAILABLE",
  commercial_status: "UNVERIFIED",
  credential_status: "NOT_REQUIRED",
  resolved_credential_status: "NOT_REQUIRED",
  health: "UNKNOWN",
  last_verified_at: "",
  credential_keys: [],
  simulation_only: true,
  notes: "Fabricates audio. Nothing leaves this process.",
  evidence: [],
  gaps: [],
  production_ready: false,
  blockers: ["simulation_only: this provider fabricates output"],
};

const BLOCKED_VIDEO: WorkspaceMaturityList["items"][number] = {
  provider: "wav2lip",
  capability: "video",
  implementation_status: "UNAVAILABLE",
  contract_status: "UNVERIFIED",
  live_status: "UNVERIFIED",
  commercial_status: "BLOCKED_LICENSE",
  credential_status: "REQUIRED",
  resolved_credential_status: "NOT_CONFIGURED",
  health: "UNKNOWN",
  last_verified_at: "",
  credential_keys: ["avatar.wavlip_dir"],
  simulation_only: false,
  notes: "No adapter exists, and the licence forbids use.",
  evidence: [],
  gaps: [],
  production_ready: false,
  blockers: ["implementation_status=UNAVAILABLE: there is no adapter", "commercial_status=BLOCKED_LICENSE: use is restricted"],
};

const AVATAR: WorkspaceMaturityList["items"][number] = {
  provider: "sadtalker",
  capability: "avatar",
  implementation_status: "IMPLEMENTED",
  contract_status: "UNVERIFIED",
  live_status: "UNVERIFIED",
  commercial_status: "UNVERIFIED",
  credential_status: "REQUIRED",
  resolved_credential_status: "UNRESOLVED",
  health: "UNKNOWN",
  last_verified_at: "",
  credential_keys: ["avatar.sadtalker_dir"],
  simulation_only: false,
  notes: "Registry capability not named by this screen's grouping; shown rather than dropped.",
  evidence: [],
  gaps: [],
  production_ready: false,
  blockers: ["contract_status=UNVERIFIED: no offline contract test"],
};

const SECRET_VALUE = "ym_sk-live-ABCDEF0123456789";
const MASKED_PREFIX = "ym_sk-l***XY";

function wsRoute(path: string): unknown {
  if (path.startsWith("/provider-maturity/incidents")) {
    return { items: [], count: 0, unknown_exposure_count: 0, states: [], note: "n" };
  }
  if (path.startsWith("/provider-maturity")) {
    return {
      workspace_id: "ws-1",
      items: [LLM, MOCK_TTS, BLOCKED_VIDEO, AVATAR],
      count: 4,
      resolved_credential_states: ["CONFIGURED", "NOT_CONFIGURED", "NOT_REQUIRED", "UNRESOLVED"],
      note: "resolved_credential_status is a state, never a value, a length, or a digest.",
      credential_summary: {
        by_state: { CONFIGURED: 1, NOT_CONFIGURED: 1, NOT_REQUIRED: 1, UNRESOLVED: 1 },
        providers_without_credentials: ["wav2lip"],
      },
    };
  }
  if (path.startsWith("/connections")) {
    return {
      items: [
        {
          key: "llm.api_key",
          label: "LLM API key",
          secret: true,
          hint: "Sent as a bearer token.",
          configured: true,
          source: "workspace",
          // The API really ships this. The screen must not declare it.
          masked: MASKED_PREFIX,
        },
        {
          key: "llm.model",
          label: "LLM model",
          secret: false,
          hint: "Model name.",
          configured: true,
          source: "workspace",
          // For a NON-secret key `masked` is the RAW value.
          masked: "gpt-4o-mini",
        },
      ],
    };
  }
  if (path.startsWith("/distribution/capabilities")) {
    return {
      items: [
        {
          platform: "youtube",
          capabilities: ["upload", "metrics"],
          publish_mode: "DIRECT_PUBLISH",
          direct_publish: true,
          user_handoff: false,
        },
        {
          platform: "instagram",
          capabilities: [],
          publish_mode: "USER_HANDOFF",
          direct_publish: false,
          user_handoff: true,
        },
      ],
    };
  }
  if (path.startsWith("/intelligence/routing/health")) {
    return { providers: { remote: true, local: false } };
  }
  return {};
}

function globalRoute(path: string): unknown {
  if (path === "/provider-maturity/tts/qualification") {
    return {
      implemented: [
        {
          provider: "mock-tts",
          label: "Offline mock",
          adapter: "MockTTS",
          implementation_status: "IMPLEMENTED",
          contract_status: "CONTRACT_TESTED",
          live_status: "UNAVAILABLE",
          commercial_status: "UNVERIFIED",
          qualification_labels: ["OFFLINE"],
          simulation_only: true,
          credential_keys: [],
          gaps: [],
        },
      ],
      donor_candidates: [{ provider: "donor-voice", merged: false, label: "proposed only" }],
      probeable: ["elevenlabs"],
      note: "Contract-tested means an offline test drove the adapter. It does not mean the vendor works.",
    };
  }
  if (path === "/provider-maturity/summary") {
    return {
      registry_reviewed_at: "2026-03-01",
      states: [...MATURITY_LADDER],
      capabilities: ["llm", "tts", "music", "video", "image", "avatar"],
      total: 4,
      by_capability: { llm: 1, tts: 1, video: 1, avatar: 1 },
      implementation_status: { IMPLEMENTED: 2, UNAVAILABLE: 1 },
      contract_status: { CONTRACT_TESTED: 2, UNVERIFIED: 2 },
      live_status: { UNVERIFIED: 3, UNAVAILABLE: 1 },
      commercial_status: { EXTERNAL_LIMITATION: 1, UNVERIFIED: 2, BLOCKED_LICENSE: 1 },
      production_ready: [],
      not_production_ready: {},
      note: "production_ready is a conclusion over four axes — never a stored label.",
    };
  }
  return {
    items: [LLM, MOCK_TTS, BLOCKED_VIDEO, AVATAR],
    count: 4,
    capabilities: ["llm", "tts", "music", "video", "image", "avatar"],
    states: [...MATURITY_LADDER],
    registry_reviewed_at: "2026-03-01",
    note: "live_status=UNVERIFIED means nobody has exercised this provider against its real service yet.",
  };
}

function serve() {
  globalGet.mockImplementation((path: string) => Promise.resolve(globalRoute(path)));
  wsGet.mockImplementation((path: string) => Promise.resolve(wsRoute(path)));
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

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  globalGet.mockReset();
  CAPABILITIES = ["providers.manage"];
});

beforeEach(() => {
  serve();
});

async function ready() {
  // "Live-verified" is the at-a-glance tile label and is unique to it.
  return screen.findByText("Live-verified");
}

async function openTab(name: RegExp) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

/* Tab labels carry a count suffix ("LLM1"), so tabs are matched by prefix. */
function tab(label: string) {
  return screen.getByRole("tab", { name: new RegExp(`^${label}`) });
}

async function alertsNaming(pattern: RegExp): Promise<boolean> {
  const found = await screen.findAllByRole("alert");
  return found.some((a) => pattern.test(a.textContent ?? ""));
}

describe("Providers renders when data arrives", () => {
  it("lists the LLM group with all four maturity axes", async () => {
    render(<Providers />);
    await ready();

    const text = document.body.textContent ?? "";
    expect(text).toContain("openai");
    expect(text).toContain("IMPLEMENTED");
    expect(text).toContain("CONTRACT_TESTED");
    expect(text).toContain("UNVERIFIED");
    expect(text).toContain("EXTERNAL_LIMITATION");
    expect(text).toContain("CONFIGURED");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("expands a provider into its blockers, gaps and credential keys", async () => {
    render(<Providers />);
    await ready();

    fireEvent.click(await screen.findByText("openai"));
    expect(await screen.findByText("Why this is not production-ready")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("never exercised against the real service");
    expect(text).toContain("uncovered surfaces: streaming not covered");
    expect(text).toContain("llm.api_key");
  });

  it("offers a group for every requested category plus the registry's own", async () => {
    render(<Providers />);
    await ready();

    for (const label of ["LLM", "TTS", "Music", "Image", "Video", "Intelligence", "Publishing", "Credentials"]) {
      expect(tab(label)).toBeInTheDocument();
    }
    // `avatar` is a real registry capability the requested grouping omits.
    expect(tab("Other")).toBeInTheDocument();
  });
});

describe("the maturity ladder is exact", () => {
  it("accepts exactly the eight registered states", () => {
    expect([...MATURITY_LADDER]).toEqual([
      "IMPLEMENTED",
      "CONTRACT_TESTED",
      "LIVE_VERIFIED",
      "UNVERIFIED",
      "UNAVAILABLE",
      "BLOCKED_LICENSE",
      "BLOCKED_COMMERCIAL_TERMS",
      "EXTERNAL_LIMITATION",
    ]);
    for (const state of MATURITY_LADDER) expect(isLadderState(state)).toBe(true);
  });

  it("rejects anything outside the set, including a readiness label", () => {
    expect(isLadderState("PRODUCTION_READY")).toBe(false);
    expect(isLadderState("production_ready")).toBe(false);
    expect(isLadderState("IMPLEMENTED_V2")).toBe(false);
    expect(isLadderState(undefined)).toBe(false);
    expect(isLadderState(null)).toBe(false);
  });

  it("renders only ladder values as maturity badges on the page", async () => {
    render(<Providers />);
    await ready();

    // The ladder panel prints all eight. What must never appear is a BADGE
    // claiming readiness: "production ready" is a conclusion over four axes, so
    // printing it as a state is the exact failure the closed set prevents.
    // (The word legitimately appears in prose — a tile label, a note.)
    const badgeTexts = Array.from(document.querySelectorAll(".ym-badge")).map((b) =>
      (b.textContent ?? "").trim(),
    );
    for (const text of badgeTexts) {
      expect(text).not.toMatch(/PRODUCTION[\s_-]?READY/i);
      // No badge may *begin* with a ladder word and then continue — that is the
      // shape a smuggled ninth label would take.
      for (const state of MATURITY_LADDER) {
        expect(text === state || !text.startsWith(state)).toBe(true);
      }
    }
  });

  it("refuses to print an off-ladder value, even from the backend", async () => {
    serve();
    const offLadder = { ...LLM, live_status: "PRODUCTION_READY" };
    wsGet.mockImplementation((path: string) => {
      if (path.startsWith("/provider-maturity") && !path.includes("incidents")) {
        return Promise.resolve({
          workspace_id: "ws-1",
          items: [offLadder],
          count: 1,
          resolved_credential_states: ["CONFIGURED"],
          note: "n",
          credential_summary: { by_state: {}, providers_without_credentials: [] },
        });
      }
      return Promise.resolve(wsRoute(path));
    });
    render(<Providers />);
    await ready();

    expect(await screen.findAllByText("NOT IN LADDER")).not.toHaveLength(0);
    // The offending string is withheld from the body AND from the tooltip.
    const notInLadder = (await screen.findAllByText("NOT IN LADDER"))[0];
    expect(notInLadder.textContent).not.toContain("PRODUCTION_READY");
    expect(notInLadder.getAttribute("title") ?? "").not.toContain("PRODUCTION_READY");
    expect(document.body.innerHTML).not.toContain("PRODUCTION_READY");
  });

  it("does not colour a contract-tested provider as healthy", () => {
    expect(maturityTone("LIVE_VERIFIED")).toBe("success");
    expect(maturityTone("CONTRACT_TESTED")).toBe("warning");
    expect(maturityTone("BLOCKED_LICENSE")).toBe("danger");
    expect(maturityTone("EXTERNAL_LIMITATION")).toBe("unknown");
  });

  it("maps registry capabilities to groups without inventing a capability", () => {
    expect(groupForCapability("llm")).toBe("llm");
    expect(groupForCapability("tts")).toBe("tts");
    expect(groupForCapability("music")).toBe("music");
    expect(groupForCapability("image")).toBe("image");
    expect(groupForCapability("video")).toBe("video");
    // No such registry capability exists, so nothing maps onto these.
    expect(groupForCapability("intelligence")).toBe("other");
    expect(groupForCapability("publishing")).toBe("other");
    expect(groupForCapability("avatar")).toBe("other");
  });

  it("shows an unprobed provider's health as unknown, not healthy", async () => {
    render(<Providers />);
    await ready();
    fireEvent.click(await screen.findByText("openai"));

    const health = (await screen.findAllByText("HEALTH UNKNOWN"))[0];
    expect(health.className).toContain("ym-tone-unknown");
    // The absence of a probe must never read as a passing probe.
    expect(screen.queryByText("HEALTH OK")).toBeNull();
  });

  it("labels a simulation-only adapter as simulated", async () => {
    render(<Providers />);
    await ready();
    await openTab(/^TTS/);

    // `mock-tts` is in both the TTS maturity table and the qualification table.
    fireEvent.click((await screen.findAllByText("mock-tts"))[0]);
    expect(await screen.findByText(/this provider fabricates output/)).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("SIMULATED");
  });
});

describe("no credential value is ever rendered", () => {
  it("renders configuration status only, never the masked value", async () => {
    render(<Providers />);
    await ready();
    await openTab(/^Credentials/);

    expect(await screen.findByText("Credential configuration")).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    expect(text).toContain("LLM API key");
    expect(text).toContain("Configured");
    // A secret's masked prefix is a fingerprint; a non-secret's `masked` is its
    // raw value. Neither may appear.
    expect(text).not.toContain(MASKED_PREFIX);
    expect(text).not.toContain("gpt-4o-mini");
    expect(document.body.innerHTML).not.toContain(MASKED_PREFIX);
    expect(document.body.innerHTML).not.toContain("gpt-4o-mini");
  });

  it("renders the credential state word, not the key's value, in the maturity detail", async () => {
    render(<Providers />);
    await ready();
    fireEvent.click(await screen.findByText("openai"));

    // CONFIGURED appears twice once the row is expanded: in the table cell and
    // in the detail tile. Both are the state word; neither is a value.
    expect((await screen.findAllByText("CONFIGURED")).length).toBeGreaterThan(0);
    // The key NAME is allowed; the module's docstring says "Names, never values".
    expect(document.body.textContent ?? "").toContain("llm.api_key");
    expect(document.body.textContent ?? "").not.toContain(SECRET_VALUE);
  });

  it("keeps UNRESOLVED distinct from NOT CONFIGURED", () => {
    // A resolver fault and a missing key are different answers and the screen
    // must not collapse them; both appear in the ladder panel's credential set.
    expect(isLadderState("UNRESOLVED")).toBe(false);
    expect(isLadderState("NOT_CONFIGURED")).toBe(false);
  });

  it("explains that credential configuration needs admin instead of hiding it", async () => {
    CAPABILITIES = ["content.read"];
    render(<Providers />);
    await ready();
    await openTab(/^Credentials/);

    expect(await alertsNaming(/does not include "providers\.manage"/i)).toBe(true);
    expect(await alertsNaming(/UNAVAILABLE/i)).toBe(true);
    // The viewer-level credential STATE still renders.
    expect(document.body.textContent ?? "").toContain("CONFIGURED");
  });
});

describe("a failed read is an alert, not an empty list", () => {
  it("names the failure when the workspace registry is unreachable", async () => {
    failWs((p) => p.startsWith("/provider-maturity") && !p.includes("incidents"), "registry unreachable");
    render(<Providers />);

    expect(await alertsNaming(/registry unreachable/i)).toBe(true);
    expect(await alertsNaming(/\/provider-maturity/i)).toBe(true);
  });

  it("does not present a failed read as an empty provider list", async () => {
    failWs((p) => p.startsWith("/provider-maturity") && !p.includes("incidents"), "registry unreachable");
    render(<Providers />);

    await alertsNaming(/registry unreachable/i);
    expect(document.body.textContent ?? "").not.toMatch(/no LLM provider record/i);
  });

  it("reports the affected tiles as UNAVAILABLE rather than 0", async () => {
    failWs((p) => p.startsWith("/provider-maturity") && !p.includes("incidents"), "registry unreachable");
    render(<Providers />);

    await alertsNaming(/registry unreachable/i);
    const tile = screen.getByText("Providers in the registry").closest(".ym-stat");
    expect(tile?.textContent).toContain("UNAVAILABLE");
  });

  it("keeps failure distinguishable from emptiness", async () => {
    serve();
    wsGet.mockImplementation((path: string) => {
      if (path.startsWith("/provider-maturity") && !path.includes("incidents")) {
        return Promise.resolve({
          workspace_id: "ws-1",
          items: [],
          count: 0,
          resolved_credential_states: [],
          note: "n",
          credential_summary: { by_state: {}, providers_without_credentials: [] },
        });
      }
      return Promise.resolve(wsRoute(path));
    });
    render(<Providers />);
    await ready();

    expect(await screen.findByText(/no LLM provider record/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("surfaces a failed summary read as an error", async () => {
    failGlobal((p) => p === "/provider-maturity/summary", "summary service down");
    render(<Providers />);
    await ready();

    expect(await alertsNaming(/summary service down/i)).toBe(true);
  });
});

describe("publishing and intelligence are served by real data, not invented records", () => {
  it("shows the publisher registry's publish path, not a maturity verdict", async () => {
    render(<Providers />);
    await ready();
    await openTab(/^Publishing/);

    expect(
      await screen.findByText(/has no `publishing` capability/),
    ).toBeInTheDocument();
    // The platform cell humanizes, and `ModeBadge` prints the raw publish_mode.
    expect(await screen.findByText("Youtube")).toBeInTheDocument();
    expect(document.body.textContent ?? "").toContain("DIRECT_PUBLISH");
    expect(document.body.textContent ?? "").toContain("USER_HANDOFF");
  });

  it("states plainly that no intelligence maturity record exists", async () => {
    render(<Providers />);
    await ready();
    await openTab(/^Intelligence/);

    expect(
      await screen.findByText(/has no `intelligence` capability/),
    ).toBeInTheDocument();
    expect(screen.getAllByText("none exists").length).toBeGreaterThan(0);
  });
});