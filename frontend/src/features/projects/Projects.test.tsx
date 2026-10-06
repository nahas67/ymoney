/** @vitest-environment jsdom */
/* Projects — list states, and the rule that a missing QC verdict is a dash,
 * not a zero.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const wsGet = vi.fn();

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

import { Projects } from "./Projects";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
});

function route(overrides: { content?: unknown; campaigns?: unknown } = {}) {
  wsGet.mockImplementation((path: string) => {
    if (path.startsWith("/campaigns")) return Promise.resolve(overrides.campaigns ?? { items: [] });
    return Promise.resolve(overrides.content ?? { total: 0, items: [] });
  });
}

function renderProjects() {
  return render(
    <MemoryRouter>
      <Projects />
    </MemoryRouter>,
  );
}

describe("Projects", () => {
  it("renders a skeleton while the content list loads", () => {
    wsGet.mockImplementation(() => new Promise(() => undefined));
    const { container } = renderProjects();
    expect(container.querySelector(".ym-skeleton")).toBeTruthy();
    expect(screen.getAllByText("Projects").length).toBeGreaterThan(0);
  });

  it("renders an explicit empty state instead of an empty table", async () => {
    route();
    renderProjects();
    expect(await screen.findByText("No project yet")).toBeInTheDocument();
    expect(screen.getByText(/A project appears here once/)).toBeInTheDocument();
    // Zero items really is zero, so the count tile is allowed to say 0.
    expect(screen.getByText("Projects on this page")).toBeInTheDocument();
  });

  it("renders a failed list as an error with a retry, never as an empty library", async () => {
    wsGet.mockImplementation((path: string) =>
      path.startsWith("/content")
        ? Promise.reject(new Error("500 internal error"))
        : Promise.resolve({ items: [] }),
    );
    renderProjects();

    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
    expect(screen.getAllByText("Could not load").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Retry" }).length).toBeGreaterThan(0);
    expect(screen.queryByText("No project yet")).toBeNull();
  });

  it("shows a dash, not 0, for a QC verdict the backend never reported", async () => {
    route({
      content: {
        total: 1,
        items: [
          {
            id: "ci-1",
            topic: "Why budgets fail",
            status: "PRODUCTION",
            campaign_id: null,
            cycle_id: null,
            strategy: {},
            error: null,
            video: null,
            variants_count: 0,
            created_at: "2026-03-01T09:00:00Z",
          },
        ],
      },
    });
    renderProjects();

    const row = (await screen.findByText("Why budgets fail")).closest("tr");
    expect(row).toBeTruthy();
    expect(row?.textContent).toContain("not rendered");
    expect(screen.queryByText("UNAVAILABLE")).toBeNull();
    expect(row?.textContent).not.toMatch(/\b0\b/);
  });

  it("keeps rows keyboard reachable", async () => {
    route({
      content: {
        total: 1,
        items: [
          {
            id: "ci-2",
            topic: "Keyboard reachable",
            status: "PUBLISHED",
            campaign_id: null,
            cycle_id: null,
            strategy: {},
            error: null,
            video: {
              id: "v-1",
              status: "COMPLETED",
              engine: "remotion",
              file_path: "/out/v.mp4",
              thumbnail_path: "",
              progress: 100,
              duration_seconds: 30,
              error: "",
              quality: 82,
              quality_passed: true,
              quality_notes: "",
              quality_components: {},
              aspect_ratio: "9:16",
            },
            variants_count: 3,
            created_at: "2026-03-01T09:00:00Z",
          },
        ],
      },
    });
    renderProjects();

    const row = (await screen.findByText("Keyboard reachable")).closest("tr");
    expect(row?.getAttribute("tabindex")).toBe("0");
    row?.focus();
    expect(document.activeElement).toBe(row);
  });
});