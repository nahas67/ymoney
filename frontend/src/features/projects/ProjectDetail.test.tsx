/** @vitest-environment jsdom */
/* ProjectDetail — the tabbed workspace.
 *
 * The important assertions here are structural: Overview answers stage /
 * blockers / next actions without any other tab, the tablist is keyboard
 * operable, and a tab whose data this read model cannot reach says UNAVAILABLE
 * instead of inventing a count.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const wsGet = vi.fn();
const fetchVideoFile = vi.fn();
const fetchVideoThumbnail = vi.fn();

vi.mock("../../lib/api", () => ({
  // queries.ts reads ApiError to decide whether a status is ignorable, so the
  // mock has to carry it: a mock that omits an export fails at the error path.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: { get: (path: string) => wsGet(path) },
  api: vi.fn(),
  fetchVideoFile: (id: string) => fetchVideoFile(id),
  fetchVideoThumbnail: (id: string) => fetchVideoThumbnail(id),
  videoFileUrl: (id: string) => `/video/${id}.mp4`,
  videoThumbUrl: (id: string) => `/thumb/${id}.jpg`,
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: [],
  }),
}));

import { ProjectDetail } from "./ProjectDetail";

const CONTENT_ID = "ci-42";

const DETAIL = {
  id: CONTENT_ID,
  topic: "Why your budget breaks in month three",
  status: "FAILED",
  campaign_id: "camp-1",
  cycle_id: "cyc-1",
  strategy: { angle: "contrarian explainer", storyboard: { scenes: [{ index: 1, prompt: "a broken spreadsheet" }] } },
  error: "render engine refused the job",
  video: {
    id: "v-9",
    status: "FAILED",
    engine: "remotion",
    file_path: "",
    thumbnail_path: "",
    progress: 40,
    duration_seconds: null,
    error: "chromium exited with 1",
    quality: 41,
    quality_passed: false,
    quality_notes: "audio silent",
    quality_components: { audio: 30 },
    aspect_ratio: "9:16",
  },
  variants_count: 2,
  created_at: "2026-03-01T09:00:00Z",
  research: {
    summary: "Month three is where the retention cliff shows.",
    key_facts: ["Churn concentrates in month three"],
    claims: [{ claim: "Month three churn doubles", status: "LIKELY", confidence: 0.62, basis: "industry report" }],
    factual_confidence: 0.62,
    fact_status: "OK",
    angles: ["counterintuitive arithmetic"],
    visual_keywords: ["ledger", "crack"],
  },
  tags: ["money", "retention"],
  variants: [
    { id: "var-1", label: "A", hook: "Your budget breaks at month three", script: "Because...", predicted_score: 78.5, selected: true, metadata: {} },
  ],
};

const AUDIT = {
  content: { id: CONTENT_ID, topic: DETAIL.topic, status: "FAILED", error: "render engine refused the job", created_at: "2026-03-01T09:00:00Z" },
  decision_why: "highest measured retention",
  strategy: DETAIL.strategy,
  research: DETAIL.research,
  variants: DETAIL.variants,
  videos: [{ id: "v-9", engine: "remotion", status: "FAILED", aspect_ratio: "9:16", resolution: "1080x1920", duration_seconds: null, params: {}, error: "chromium exited with 1" }],
  quality_checks: [{ overall: 41, passed: false, notes: "audio silent", components: { audio: 30 }, created_at: "2026-03-01T09:10:00Z" }],
  publishing_jobs: [],
  published_posts: [],
  event_trail: [{ at: "2026-03-01T09:05:00Z", kind: "content.failed", level: "error", message: "render engine refused the job" }],
};

function resolver(overrides: Record<string, unknown> = {}) {
  return (path: string) => {
    // The reader asks about whichever id it was mounted with, so the mock keys
    // on the suffix rather than on one hard-coded id.
    if (path.match(/^\/content\/[^/]+$/)) {
      const id = path.split("/").pop() as string;
      return Promise.resolve(overrides.detail ?? { ...DETAIL, id });
    }
    if (path.endsWith("/timeline")) {
      return Promise.resolve(
        overrides.timeline ?? { items: [{ at: "2026-03-01T09:00:00Z", kind: "idea", label: "Idea created", detail: "topic" }] },
      );
    }
    if (path.endsWith("/lineage")) {
      const id = path.split("/").slice(-2, -1)[0];
      return Promise.resolve(
        overrides.lineage ?? { self: { id, topic: DETAIL.topic, status: "FAILED", derivation_type: null, lineage_version: 1, campaign_id: "camp-1" }, root_id: id, ancestors: [], children: [] },
      );
    }
    if (path.endsWith("/audit")) return Promise.resolve(overrides.audit ?? AUDIT);
    if (path === "/calendar") return Promise.resolve(overrides.schedule ?? { items: [] });
    return Promise.resolve({});
  };
}

function route(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation(resolver(overrides));
}

function renderDetail(contentId: string = CONTENT_ID) {
  return render(
    <MemoryRouter initialEntries={[`/projects/${contentId}`]}>
      <ProjectDetail contentId={contentId} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  wsGet.mockReset();
  fetchVideoFile.mockReset();
  fetchVideoThumbnail.mockReset();
});

describe("ProjectDetail", () => {
  it("answers stage, blockers and next actions on Overview", async () => {
    route();
    renderDetail();

    expect(await screen.findByText("Why your budget breaks in month three")).toBeInTheDocument();

    // Stage
    expect(screen.getByText("Current stage")).toBeInTheDocument();
    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0);
    expect(screen.getByText("QC rejected")).toBeInTheDocument();

    // Blockers, each one a recorded field.
    expect(screen.getByText("Blockers")).toBeInTheDocument();
    expect(screen.getByText("render engine refused the job")).toBeInTheDocument();
    expect(screen.getByText("chromium exited with 1")).toBeInTheDocument();
    expect(screen.getByText("Quality check rejected (41/100)")).toBeInTheDocument();

    // Next actions, pointing at the surface that owns them — never a paid retry.
    expect(screen.getAllByText("Next actions").length).toBeGreaterThan(0);
    expect(screen.getByText(/A resubmit is a paid action/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("exposes every lifecycle tab and switches them with the keyboard", async () => {
    route();
    renderDetail();
    await screen.findByText("Why your budget breaks in month three");

    for (const label of [
      "Overview",
      "Research",
      "Script",
      "Scenes",
      "Timeline",
      "Assets",
      "Versions",
      "Reviews",
      "Exports",
      "Publishing",
      "Performance",
      "Activity",
    ]) {
      expect(screen.getByRole("tab", { name: new RegExp(`^${label}`) })).toBeInTheDocument();
    }

    const overview = screen.getByRole("tab", { name: /^Overview/ });
    overview.focus();
    overview.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));

    await waitFor(() => expect(screen.getByRole("tab", { name: /^Research/ })).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByText("Research brief")).toBeInTheDocument();
    expect(screen.getByText("Month three churn doubles")).toBeInTheDocument();
  });

  it("reports an unreachable read model as UNAVAILABLE, not as zero", async () => {
    route();
    renderDetail();
    await screen.findByText("Why your budget breaks in month three");

    screen.getByRole("tab", { name: /^Exports/ }).click();
    await waitFor(() => expect(screen.getByText("Exports for this project")).toBeInTheDocument());
    expect(screen.getByText("Not in this screen's read model")).toBeInTheDocument();

    // The attributed-spend tile has no per-project source, so it must not be 0.
    expect(screen.getByText("Attributed spend")).toBeInTheDocument();
    const tiles = screen.getAllByText("UNAVAILABLE");
    expect(tiles.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("$0.0000")).toBeNull();
  });

  it("names the reads that failed instead of rendering a half-empty project", async () => {
    const base = resolver();
    wsGet.mockImplementation((path: string) =>
      path === `/content/${CONTENT_ID}/audit`
        ? Promise.reject(new Error("500 internal error"))
        : base(path),
    );
    renderDetail();

    await waitFor(() => expect(screen.getByText(/1 read failed/)).toBeInTheDocument());
    expect(screen.getAllByText("500 internal error").length).toBeGreaterThan(0);
    // Overview still renders from the reads that did land.
    expect(screen.getByText("Current stage")).toBeInTheDocument();
  });

  it("re-reads when the project id changes instead of keeping the old rows", async () => {
    route();
    wsGet.mockImplementation((path: string) =>
      path === "/content/ci-43" ? Promise.resolve({ ...DETAIL, id: "ci-43", topic: "Second project" }) : resolver()(path),
    );

    const view = render(
      <MemoryRouter>
        <ProjectDetail contentId="ci-42" />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Why your budget breaks in month three")).toBeInTheDocument();

    // A route change keeps this component mounted; the reader is keyed on the id
    // so the second project must be read from the API, not carried over.
    view.rerender(
      <MemoryRouter>
        <ProjectDetail contentId="ci-43" />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Second project")).toBeInTheDocument();
    expect(screen.queryByText("Why your budget breaks in month three")).toBeNull();
  });

  it("guards the screen when there is no project id", () => {
    render(
      <MemoryRouter initialEntries={["/projects"]}>
        <ProjectDetail />
      </MemoryRouter>,
    );
    expect(screen.getByText("No project id")).toBeInTheDocument();
    expect(wsGet).not.toHaveBeenCalled();
  });

  it("uses header-authenticated blobs for render playback and thumbnail URLs", async () => {
    route({ detail: { ...DETAIL, video: { ...DETAIL.video, file_path: "renders/output.mp4" } } });
    fetchVideoFile.mockResolvedValue(new Blob(["video"]));
    fetchVideoThumbnail.mockResolvedValue(new Blob(["thumbnail"]));
    const created: string[] = [];
    const revoked: string[] = [];
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL = vi.fn(() => {
        const url = `blob:project-${created.length + 1}`;
        created.push(url);
        return url;
      });
      static revokeObjectURL = vi.fn((url: string) => revoked.push(url));
    });
    const { unmount } = renderDetail();
    await screen.findByText("Why your budget breaks in month three");

    fireEvent.click(screen.getByRole("button", { name: "Open preview" }));
    await waitFor(() => expect(screen.getByTestId("project-video")).toHaveAttribute("src", "blob:project-1"));
    expect(fetchVideoFile).toHaveBeenCalledWith("v-9");

    fireEvent.click(screen.getByRole("tab", { name: /^Assets/ }));
    await waitFor(() => expect(screen.getByAltText(/Thumbnail for Why your budget/)).toHaveAttribute("src", "blob:project-2"));
    expect(fetchVideoThumbnail).toHaveBeenCalledWith("v-9");
    expect(Array.from(document.querySelectorAll("video, img")).every((node) => !node.getAttribute("src")?.includes("?token="))).toBe(true);

    unmount();
    expect(revoked).toEqual(["blob:project-1", "blob:project-2"]);
  });
});
