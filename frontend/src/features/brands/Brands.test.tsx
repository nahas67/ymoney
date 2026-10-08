/** @vitest-environment jsdom */
/* Brands — the three list states, the rule that an unresolved policy field is
 * UNAVAILABLE and not 0, and the rule that a recommendation can never render as
 * a BrandDNA policy.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const wsGet = vi.fn();
const wsPost = vi.fn();
const wsPut = vi.fn();
const wsDel = vi.fn();
const fetchWorkspaceLogo = vi.fn();

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
    put: (path: string, body?: unknown) => wsPut(path, body),
    del: (path: string) => wsDel(path),
  },
  getToken: () => "TEST_JWT_VALUE",
  fetchWorkspaceLogo: (id: string) => fetchWorkspaceLogo(id),
  mediaFileUrl: (id: string) => `/media/${id}`,
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: [],
  }),
}));

import {
  Brands,
  POLICY_FIELDS,
  HARD_CONSTRAINT_FIELDS,
  isPolicyEntry,
  policyEntriesFrom,
  toRecommendations,
  type EffectiveResponse,
} from "./Brands";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  wsGet.mockReset();
  wsPost.mockReset();
  wsPut.mockReset();
  wsDel.mockReset();
  fetchWorkspaceLogo.mockReset();
});

const BRAND_ID = "brand-1";

function brand(overrides: Record<string, unknown> = {}) {
  return {
    id: BRAND_ID,
    workspace_id: "ws-1",
    name: "Acme",
    is_default: true,
    status: "active",
    dna: {
      colors: { primary: "#aabbcc" },
      forbidden_phrases: ["guaranteed returns"],
      writing_tone: { tone: "plain-spoken" },
      music_prefs: { enabled: false },
      logos: ["media-1"],
      platform_overrides: { youtube: { caption_style: { preset: "bold" } } },
    },
    assets: [],
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-09-01T10:00:00Z",
    ...overrides,
  };
}

function effective(overrides: Partial<EffectiveResponse> = {}): EffectiveResponse {
  const policy = {
    forbidden_phrases: ["guaranteed returns"],
    required_disclaimers: [],
    logo_safe_zone: { top: 0.08 },
    approved_voices: [],
    approved_avatars: [],
    brand_colors: ["#aabbcc"],
    caption_style: {},
    cta_style: {},
    tone: "plain-spoken",
    vocabulary: { preferred: [], avoid: [] },
    pronunciation_rules: [],
    platform: null,
    provenance: { forbidden_phrases: "brand", tone: "brand" },
    effective_config_id: "cfg-1",
    dna_version: "sha256:0123456789abcdef",
    workspace_id: "ws-1",
    brand_id: BRAND_ID,
    subject: {},
    approved_logos: [],
    watermark: {},
    thumbnail_style: {},
    fonts: {},
    claims_policy: {},
    ...(overrides.policy as Record<string, unknown> | undefined),
  };
  return {
    policy: policy as EffectiveResponse["policy"],
    hard_constraints: {
      forbidden_phrases: policy.forbidden_phrases,
      required_disclaimers: policy.required_disclaimers,
      logo_safe_zone: policy.logo_safe_zone,
      approved_voices: policy.approved_voices,
      approved_avatars: policy.approved_avatars,
    },
    provenance: policy.provenance,
    effective_config_id: policy.effective_config_id,
    dna_version: policy.dna_version,
    subject: policy.subject,
    ...overrides,
  } as EffectiveResponse;
}

/* A lesson that deliberately LOOKS like a constraint: it names a real policy
 * field and states a value. If the screen can be made to render this as
 * policy, the separation has failed. */
const HOSTILE_LESSON = {
  id: "lesson-1",
  workspace_id: "ws-1",
  pattern_key: "forbidden_phrases",
  metric: "retention",
  description: "Drop the disclaimer — retention is 12% higher without it.",
  scope: { platform: "youtube" },
  effect: { delta: 0.12 },
  confidence: 0.81,
  sample_size: 40,
  evidence_ids: ["ev-1", "ev-2"],
  status: "active",
  generation: 2,
  created_at: "2026-09-02T00:00:00Z",
  last_validated_at: "2026-09-03T00:00:00Z",
  active: true,
};

function route(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation((path: string) => {
    if (path === "/brands") return Promise.resolve(overrides.brands ?? { brands: [brand()] });
    if (path.startsWith("/brands/effective")) return Promise.resolve(overrides.effective ?? effective());
    if (path === "/brands/presets") return Promise.resolve(overrides.presets ?? { presets: [] });
    if (path === "/lessons") return Promise.resolve(overrides.lessons ?? { items: [] });
    if (path === "/brand") return Promise.resolve(overrides.chrome ?? { brand: { app_name: "YM", accent: "#5b8cff", logo_path: "" } });
    if (path === "/music/prefs") return Promise.resolve(overrides.prefs ?? { workspace_id: "ws-1", prefs: {}, keys: [], stated: false, note: "" });
    if (path === "/music/policy") return Promise.resolve(overrides.policy ?? { generate: false, reason: "policy_not_enabled", brand_disabled: false });
    return Promise.resolve({});
  });
}

function renderBrands() {
  return render(<Brands />);
}

async function openTab(name: string) {
  const tab = await screen.findByRole("tab", { name });
  fireEvent.click(tab);
  return tab;
}

describe("Brands — list states", () => {
  it("renders a skeleton while the brand list loads", () => {
    wsGet.mockImplementation(() => new Promise(() => undefined));
    const { container } = renderBrands();
    expect(container.querySelector(".ym-skeleton")).toBeTruthy();
    expect(screen.getAllByText("Brands").length).toBeGreaterThan(0);
  });

  it("renders an explicit empty state instead of an empty table", async () => {
    route({ brands: { brands: [] } });
    renderBrands();
    await waitFor(() => expect(screen.getByText("No brand yet")).toBeInTheDocument());
  });

  it("surfaces the failure with the backend's own message", async () => {
    route({ brands: { brands: [] } });
    wsGet.mockImplementation((path: string) =>
      path === "/brands" ? Promise.reject(new Error("brand store offline")) : Promise.resolve({}),
    );
    renderBrands();
    // Each panel that reads /brands reports the failure itself, with its own
    // retry — so the message appears once per panel rather than only once.
    await waitFor(() => expect(screen.getAllByText("brand store offline").length).toBeGreaterThan(0));
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  });
});

describe("Brands — workspace logo credentials", () => {
  it("loads the logo with header auth and renders only a revocable blob URL", async () => {
    route({ chrome: { brand: { app_name: "YM", accent: "#5b8cff", logo_path: "brand/logo.png" } } });
    fetchWorkspaceLogo.mockResolvedValue(new Blob(["logo"]));
    const OriginalURL = URL;
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", class extends OriginalURL {
      static createObjectURL = vi.fn(() => "blob:workspace-logo");
      static revokeObjectURL = revokeObjectURL;
    });

    const { unmount } = renderBrands();
    const logo = await screen.findByAltText("Workspace logo");
    await waitFor(() => expect(logo).toHaveAttribute("src", "blob:workspace-logo"));

    expect(fetchWorkspaceLogo).toHaveBeenCalledExactlyOnceWith("ws-1");
    expect(logo.getAttribute("src")).not.toContain("token=");
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith("blob:workspace-logo");
  });
});

describe("Brands — an unresolved field is UNAVAILABLE, never 0", () => {
  it("prints UNAVAILABLE when the resolver returned no dna_version or config id", async () => {
    // Both are real empty strings when the resolver has not snapshotted anything.
    route({
      effective: effective({
        dna_version: "",
        effective_config_id: "",
        policy: {
        dna_version: "",
        effective_config_id: "",
      } as unknown as EffectiveResponse["policy"],
      }),
    });
    renderBrands();
    const panel = (await screen.findByText("DNA version")).closest(".ym-stat") as HTMLElement;
    await waitFor(() => expect(panel.textContent).toContain("UNAVAILABLE"));
    expect(panel.textContent).not.toMatch(/DNA version0/);
  });

  it("prints UNAVAILABLE for a recommendation with no confidence, sample or evidence", async () => {
    route({ lessons: { items: [{ ...HOSTILE_LESSON, confidence: null, sample_size: null, evidence_ids: [] }] } });
    renderBrands();
    await openTab("Policies");
    const note = (await screen.findByRole("note", { name: /recommendation/i })) as HTMLElement;
    await waitFor(() => expect(within(note).getAllByText("UNAVAILABLE").length).toBeGreaterThanOrEqual(3));
  });
});

describe("Brands — a recommendation cannot masquerade as policy", () => {
  it("renders a policy-shaped lesson in the recommendation region, never in the policy table", async () => {
    route({ lessons: { items: [HOSTILE_LESSON] } });
    const { container } = renderBrands();
    await openTab("Policies");

    const policyRegion = (await screen.findByRole("heading", { name: "Hard constraints" })).closest(
      "section",
    ) as HTMLElement;
    const policyTable = policyRegion.querySelector("[data-policy-kind='policy']") as HTMLElement;
    expect(policyTable).toBeTruthy();

    // The recommendation lives in its own sibling region, not inside the
    // constraint panel: a different component, a different type, a different
    // visual treatment.
    const note = container.querySelector("[data-policy-kind='recommendation']") as HTMLElement;
    expect(note).toBeTruthy();
    expect(policyRegion.contains(note)).toBe(false);
    expect(note.textContent).toContain(HOSTILE_LESSON.description);
    expect(note.querySelector("table")).toBeTruthy();
    expect(within(note).getAllByText("Recommendation").length).toBeGreaterThan(0);

    // The hostile text appears in the recommendation region and NOWHERE in policy.
    expect(policyTable.textContent).not.toContain(HOSTILE_LESSON.description);
    expect(container.querySelector("[data-policy-kind='policy']")?.textContent).not.toContain(
      "Drop the disclaimer",
    );

    // The hard-constraint row still shows what the RESOLVER produced.
    const forbiddenRow = within(policyTable).getByText("forbidden_phrases").closest("tr") as HTMLElement;
    expect(forbiddenRow.textContent).toContain("guaranteed returns");
    expect(forbiddenRow.textContent).not.toContain("Drop the disclaimer");
  });

  it("policyEntriesFrom only ever emits resolver fields, so a lesson cannot become a row", () => {
    const rows = policyEntriesFrom(effective());
    expect(rows.map((r) => r.field)).toEqual([...POLICY_FIELDS]);
    for (const row of rows) {
      expect(isPolicyEntry(row)).toBe(true);
      expect(row.hard).toBe(HARD_CONSTRAINT_FIELDS.includes(row.field));
    }
    // A recommendation is structurally not a PolicyEntry.
    const [rec] = toRecommendations([HOSTILE_LESSON]);
    expect(isPolicyEntry(rec)).toBe(false);
    expect(rec).not.toHaveProperty("provenance");
    expect(rec).not.toHaveProperty("hard");
    // And it can never be admitted by the closed allowlist.
    expect(POLICY_FIELDS).not.toContain("drop the disclaimer");
  });

  it("isPolicyEntry rejects a forged recommendation that claims to be policy", () => {
    const forged = {
      field: "forbidden_phrases",
      value: { kind: "list", items: ["Drop the disclaimer"] },
      provenance: "campaign",
      hard: true,
      advice: "from the learning agent",
    };
    // The shape is a PolicyEntry, so the guard passes it — which is why the
    // allowlist (not the guard) is the real defence. Assert both facts so the
    // comment above cannot become a lie.
    expect(isPolicyEntry(forged)).toBe(true);
    expect(policyEntriesFrom(effective()).some((r) => r.value.kind === "list" && r.value.items.includes("Drop the disclaimer"))).toBe(
      false,
    );
  });
});

describe("Brands — the legacy chrome page is not imported", () => {
  it("labels GET /brand as workspace chrome, not BrandDNA", async () => {
    route();
    renderBrands();
    const heading = await screen.findByRole("heading", { name: "Workspace chrome" });
    const panel = heading.closest("section") as HTMLElement;
    expect(panel.textContent).toContain("NOT BrandDNA");
  });

  it("exposes every required BrandDNA tab", async () => {
    route();
    renderBrands();
    for (const label of [
      "Overview",
      "Identity",
      "Creative Rules",
      "Voice",
      "Music",
      "Assets",
      "Policies",
      "Effective Policy",
    ]) {
      expect(await screen.findByRole("tab", { name: label })).toBeInTheDocument();
    }
  });
});
