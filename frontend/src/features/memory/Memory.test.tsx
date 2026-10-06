/** @vitest-environment jsdom */
/* Memory — stored evidence must never render as generated interpretation.
 *
 * This screen replaces two overlapping legacy screens, so it has one correctness
 * claim above all others: a claim a system DERIVED from evidence is not the
 * evidence, and the two must not be rendered as the same kind of thing.
 *
 * Assertions:
 *   - an interpretation carries its own badge, tone and wrapper marker, and
 *     shows what it was derived from (or says UNTRACEABLE);
 *   - an evidence row and an interpretation row are distinguishable in the DOM,
 *     not only by colour;
 *   - a memory with no evidence and no source is labelled an unprovenanced
 *     claim, matching the backend's own UNVERIFIED status;
 *   - a failed read is an alert naming the failure, and never an empty store.
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
  },
  api: vi.fn(),
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: ["content.read", "content.write", "operations.view"],
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  }),
  can: () => true,
  blockedReason: () => null,
}));

import Memory, { classifyMemory } from "./Memory";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
});

/* `memory.py::_to_dict` shapes. */

function evidenceMemory() {
  return {
    id: "mem-ev",
    workspace_id: "ws-1",
    brand_id: null,
    type: "RESEARCH_FACT",
    content: "TikTok's feed ranks completion above watch time for 15s clips.",
    topic: "TikTok ranking",
    topic_key: "tiktok ranking",
    scope: "",
    platform: "tiktok",
    source_ids: ["doc-1"],
    evidence_ids: ["ev-9"],
    confidence: 0.9,
    freshness: "FRESH",
    status: "ACTIVE",
    effective_status: "FRESH",
    origin: "research.research_agent",
    content_hash: "h1",
    conflict_group: "",
    last_verified_at: "2026-01-02T09:00:00Z",
    superseded_by: null,
    use_count: 3,
    last_used_at: "2026-01-03T09:00:00Z",
    related_json: {},
    created_at: "2026-01-01T09:00:00Z",
    updated_at: "2026-01-02T09:00:00Z",
  };
}

/* A derived claim that cites its evidence. */
function derivedMemory() {
  return {
    ...evidenceMemory(),
    id: "mem-int",
    type: "CREATIVE_LESSON",
    content: "Question hooks outperformed number hooks on engagement rate.",
    topic: "",
    topic_key: "",
    platform: "",
    source_ids: [],
    evidence_ids: ["ev-1", "ev-2", "ev-3"],
    confidence: 0.35,
    origin: "work15.feedback.lesson",
    last_verified_at: null,
  };
}

/* A derived claim that cites nothing at all. */
function untraceableMemory() {
  return {
    ...derivedMemory(),
    id: "mem-int-bare",
    content: "Longer videos perform better.",
    evidence_ids: [],
    source_ids: [],
  };
}

/* No provenance: `store()` records this as UNVERIFIED. */
function bareClaim() {
  return {
    ...evidenceMemory(),
    id: "mem-bare",
    type: "BRAND_KNOWLEDGE",
    content: "The brand voice is warm and direct.",
    source_ids: [],
    evidence_ids: [],
    status: "UNVERIFIED",
    effective_status: "UNVERIFIED",
    confidence: 0.5,
  };
}

const MEMORIES = [evidenceMemory(), derivedMemory(), untraceableMemory(), bareClaim()];

const SOURCES = {
  items: [
    {
      id: "conn-1",
      kind: "rss",
      name: "Industry feed",
      status: "AVAILABLE",
      unavailable_reason: "",
      enabled: true,
      config: { feed_url: "https://example.com/feed" },
      has_credentials: false,
      implemented: true,
      requires_credentials: false,
      title: "RSS feed",
      blurb: "Synced 12 documents",
      doc_count: 12,
      last_sync_at: "2026-01-02T09:00:00Z",
      last_error: "",
      last_cursor: "c-9",
    },
    {
      id: "conn-2",
      kind: "notion",
      name: "Brand wiki",
      status: "UNAVAILABLE",
      unavailable_reason: "no credentials configured",
      enabled: true,
      config: {},
      has_credentials: false,
      implemented: true,
      requires_credentials: true,
      title: "Notion",
      blurb: "",
      doc_count: 0,
      last_sync_at: null,
      last_error: "auth refused",
      last_cursor: "",
    },
  ],
};

const DOCUMENTS = {
  items: [
    {
      id: "doc-1",
      connector_id: "conn-1",
      remote_id: "r-1",
      title: "Ranking signals, annotated",
      mime_type: "text/markdown",
      author: "research",
      state: "active",
      checksum: "abc123def456",
      remote_created_at: "2025-12-01T00:00:00Z",
      remote_updated_at: "2025-12-20T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-02T00:00:00Z",
      content: "preview",
    },
  ],
};

const GRAPH = {
  nodes: [
    {
      id: "node-1",
      workspace_id: "ws-1",
      node_type: "Claim",
      ref_id: "mem-ev",
      node_key: "claim~abc",
      label: "TikTok ranking claim",
      topic_key: "tiktok ranking",
      meta: {},
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-02T00:00:00Z",
    },
  ],
  edges: [
    {
      id: "edge-1",
      workspace_id: "ws-1",
      from_node_id: "node-1",
      to_node_id: "node-1",
      relationship: "CLAIM_SUPPORTED_BY",
      weight: 1,
      evidence_ids: ["ev-9"],
      meta: {},
      created_at: "2026-01-02T00:00:00Z",
    },
  ],
};

const PLANNER_MEMORY = {
  workspace_id: "ws-1",
  used_ids: ["mem-ev"],
  memory_count: 4,
  settled_topics: ["tiktok ranking"],
  needs_revalidation: [bareClaim()],
  memories: MEMORIES,
};

function baseRoute(path: string): unknown {
  if (path.startsWith("/knowledge/memories")) return { items: MEMORIES };
  if (path.startsWith("/knowledge/sources/")) return DOCUMENTS;
  if (path.startsWith("/knowledge/sources")) return SOURCES;
  if (path.startsWith("/knowledge/graph")) return GRAPH;
  if (path.startsWith("/planner/memory")) return PLANNER_MEMORY;
  return {};
}

function serve() {
  wsGet.mockImplementation((path: string) => Promise.resolve(baseRoute(path)));
}

function fail(predicate: (p: string) => boolean, message: string) {
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve(baseRoute(path)),
  );
}

function renderMemory() {
  return render(<Memory />);
}

function panelByTitle(title: string): HTMLElement {
  const heading = screen.getAllByText(title).find((n) => n.classList.contains("ym-panel-title"));
  if (!heading) throw new Error(`no panel titled "${title}"`);
  return heading.closest(".ym-panel") as HTMLElement;
}

async function openTab(name: string) {
  await userEvent.click(await screen.findByRole("tab", { name }));
}

describe("the classification is derived from backend facts", () => {
  it("treats a derived type as an interpretation even when it cites evidence", () => {
    // Citing evidence does not turn a conclusion into an observation.
    expect(classifyMemory(derivedMemory())).toBe("INTERPRETATION");
  });

  it("treats a recording type as evidence", () => {
    expect(classifyMemory(evidenceMemory())).toBe("EVIDENCE");
  });

  it("refuses to classify a type outside both vocabularies", () => {
    expect(classifyMemory({ type: "SOMETHING_NEW", source_ids: [], evidence_ids: [] })).toBe("UNCLASSIFIED");
  });

  it("falls back to provenance for an unknown type that does cite something", () => {
    expect(classifyMemory({ type: "SOMETHING_NEW", source_ids: ["doc-1"], evidence_ids: [] })).toBe("EVIDENCE");
  });
});

describe("Memory renders when data arrives", () => {
  it("lists the memory store with its state and provenance", async () => {
    serve();
    renderMemory();

    expect(await screen.findAllByText(/TikTok's feed ranks completion/i)).not.toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the source connectors and their honest health", async () => {
    serve();
    renderMemory();
    await openTab("Evidence");

    expect(await screen.findAllByText("Industry feed")).not.toHaveLength(0);
    // A connector with no credentials says so instead of pretending to be ready.
    expect(screen.getByTitle("no credentials configured")).toHaveTextContent("Unavailable");
  });

  it("shows the ingested documents as the evidence itself", async () => {
    serve();
    renderMemory();
    await openTab("Evidence");

    expect(await screen.findByText("Ranking signals, annotated")).toBeInTheDocument();
  });

  it("shows related entities and their edges", async () => {
    serve();
    renderMemory();
    await openTab("Entities");

    expect(await screen.findByText("TikTok ranking claim")).toBeInTheDocument();
    expect(screen.getByText("Claim Supported By")).toBeInTheDocument();
  });

  it("shows what the planner already trusts", async () => {
    serve();
    renderMemory();
    await openTab("Planner view");

    expect(await screen.findByText("tiktok ranking")).toBeInTheDocument();
  });
});

describe("interpretation is visually distinct from evidence", () => {
  it("gives an interpretation its own badge, tone and marker, never the evidence one", async () => {
    serve();
    renderMemory();
    await openTab("Interpretation");

    expect(await screen.findByText(/Question hooks outperformed number hooks/i)).toBeInTheDocument();

    const kinds = Array.from(document.querySelectorAll("[data-kind]")).map((n) => n.getAttribute("data-kind"));
    // The derived row is marked as an interpretation and never as evidence.
    expect(kinds).toContain("INTERPRETATION");
    expect(kinds).not.toContain("EVIDENCE");
  });

  it("uses a different design-system tone for each family", async () => {
    serve();
    renderMemory();
    await openTab("Interpretation");
    await screen.findByText(/Question hooks outperformed number hooks/i);

    const badge = screen.getAllByText("GENERATED INTERPRETATION")[0];
    expect(badge).toHaveClass("ym-tone-info");
    expect(screen.queryByText("STORED EVIDENCE")).toBeNull();
  });

  it("shows what an interpretation was derived from", async () => {
    serve();
    renderMemory();
    await openTab("Interpretation");
    await screen.findByText(/Question hooks outperformed number hooks/i);

    const panel = panelByTitle("Generated interpretation");
    expect(panel.textContent).toMatch(/3 evidence refs/);
    expect(panel.textContent).toMatch(/Cite it as a conclusion, never as an observation/i);
  });

  it("says UNTRACEABLE when a derivation cites nothing", async () => {
    serve();
    renderMemory();
    await openTab("Interpretation");

    expect(await screen.findByText(/Longer videos perform better/i)).toBeInTheDocument();
    expect(screen.getAllByText("UNTRACEABLE").length).toBeGreaterThan(0);
  });

  it("keeps the two families in separate tabs, never in one undifferentiated list", async () => {
    serve();
    renderMemory();

    // The Memory tab shows both, but each row is individually marked.
    await screen.findAllByText(/TikTok's feed ranks completion/i);
    const kinds = Array.from(document.querySelectorAll("[data-kind]")).map((n) => n.getAttribute("data-kind"));
    expect(kinds).toContain("EVIDENCE");
    expect(kinds).toContain("INTERPRETATION");

    // The Evidence tab asserts the family in prose.
    await openTab("Evidence");
    expect((await screen.findByText(/A document here is a thing that was ingested/i))).toBeInTheDocument();
  });

  it("labels an unprovenanced row a claim rather than a finding", async () => {
    serve();
    renderMemory();
    await openTab("Evidence");

    expect(await screen.findAllByText(/The brand voice is warm and direct/i)).not.toHaveLength(0);
    const panel = panelByTitle("Unprovenanced claims");
    expect(panel.textContent).toMatch(/UNVERIFIED/);
    expect(panel.textContent).toMatch(/an unsourced claim is not a finding/i);
  });

  it("reports an unprovenanced row's provenance as none rather than 0", async () => {
    serve();
    renderMemory();

    const row = (await screen.findAllByText(/The brand voice is warm and direct/i))[0].closest("tr");
    const cells = Array.from(row?.querySelectorAll("td") ?? []).map((c) => c.textContent ?? "");
    /* Column order: Kind, Type, Memory, State, Scope, Provenance, … */
    expect(cells[5]).toBe("none");
    // The tooltip names why, and no bare "0" is painted as a count of evidence.
    expect(row?.querySelectorAll("td")[5].querySelector("[title]")?.getAttribute("title")).toMatch(
      /No evidence_ids and no source_ids/,
    );
    expect(cells.some((c) => c.trim() === "0")).toBe(false);
  });
});

describe("a failed endpoint is an alert, never an empty store", () => {
  it("names the failure of the memory list", async () => {
    fail((p) => p.startsWith("/knowledge/memories"), "knowledge backend unavailable");
    renderMemory();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/knowledge backend unavailable/i);
  });

  it("keeps the failure distinguishable from emptiness", async () => {
    fail((p) => p.startsWith("/knowledge/memories"), "knowledge backend unavailable");
    renderMemory();

    await screen.findByRole("alert");
    expect(document.body.textContent).not.toMatch(/No memory matches/i);
  });

  it("refuses to report zero memories when the read failed", async () => {
    fail((p) => p.startsWith("/knowledge/memories"), "knowledge backend unavailable");
    renderMemory();

    await screen.findByRole("alert");
    const tiles = Array.from(document.querySelectorAll(".ym-stat")).map((t) => t.textContent ?? "");
    expect(tiles.some((t) => /Memories returned\s*UNAVAILABLE/.test(t.replace(/\s+/g, " ")))).toBe(true);
  });

  it("names the failure of the graph read", async () => {
    fail((p) => p.startsWith("/knowledge/graph"), "graph backend unavailable");
    renderMemory();
    await openTab("Entities");

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => /graph backend unavailable/i.test(a.textContent ?? ""))).toBe(true);
  });

  it("does not invent a settled topic when the planner read fails", async () => {
    fail((p) => p.startsWith("/planner/memory"), "planner memory unavailable");
    renderMemory();
    await openTab("Planner view");

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => /planner memory unavailable/i.test(a.textContent ?? ""))).toBe(true);
    expect(document.body.textContent).not.toMatch(/tiktok ranking/);
  });
});

describe("Memory is one canonical surface", () => {
  it("covers provenance, freshness, conflicts, supersession and scope in the store", async () => {
    serve();
    renderMemory();
    await screen.findAllByText(/TikTok's feed ranks completion/i);

    const headers = Array.from(document.querySelectorAll("th")).map((t) => t.textContent ?? "");
    for (const column of ["Kind", "State", "Scope", "Provenance", "Freshness", "Conflict", "Superseded by"]) {
      expect(headers).toContain(column);
    }
  });

  it("never renders a derived claim with the evidence badge text", async () => {
    serve();
    renderMemory();
    await openTab("Interpretation");

    await screen.findByText(/Longer videos perform better/i);
    const interpretationBadges = Array.from(document.querySelectorAll('[data-kind="INTERPRETATION"]'));
    expect(interpretationBadges.length).toBeGreaterThan(0);
    for (const node of interpretationBadges) {
      expect(node.textContent).not.toMatch(/STORED EVIDENCE/);
    }
  });
});
