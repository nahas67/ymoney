/** @vitest-environment jsdom */
/* Assets — the three list states, the rule that an unreported metric is
 * UNAVAILABLE and not 0, and the rule that a provenance URL never renders its
 * credential.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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
  mediaFileUrl: (id: string) => `/api/v1/workspaces/ws-1/assets/media/${id}/file?token=TEST_JWT_VALUE`,
  videoFileUrl: (id: string) => `/api/v1/workspaces/ws-1/videos/${id}/file?token=TEST_JWT_VALUE`,
  videoThumbUrl: (id: string) => `/api/v1/workspaces/ws-1/videos/${id}/thumbnail`,
  getToken: () => "TEST_JWT_VALUE",
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: [],
  }),
}));

import { Assets, redactProvenance, isSignedProvenance } from "./Assets";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
});

type Row = Record<string, unknown>;

/* A provider-signed URL, assembled from parts so the fixture is obviously a
 * fixture and never a real credential pasted into the repo. */
const ACCESS_KEY_ID = ["AKIA", "IOSF", "ODNN", "7EXAMPLE"].join("");
const SIGNATURE = ["dead", "beef", "cafe", "0123", "4567", "89ab", "cdef"].join("");
const SIGNED_STORAGE_KEY = [
  "https://cdn.example.com/renders/clip.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256",
  `X-Amz-Credential=${ACCESS_KEY_ID}%2F20260901%2Fus-east-1%2Fs3%2Faws4_request`,
  "X-Amz-Date=20260901T000000Z",
  "X-Amz-Expires=900",
  `X-Amz-Signature=${SIGNATURE}`,
].join("&");

function asset(overrides: Row = {}): Row {
  return {
    id: "a-0000000-1111-2222-3333-444444444444",
    workspace_id: "ws-1",
    type: "video",
    origin: "render",
    provider: "heygen",
    storage_key: "renders/clip.mp4",
    mime_type: "video/mp4",
    duration_seconds: 12.5,
    width: 1080,
    height: 1920,
    checksum: "abc123",
    created_at: "2026-09-01T10:00:00Z",
    ...overrides,
  };
}

function route(overrides: Record<string, unknown> = {}) {
  wsGet.mockImplementation((path: string) => {
    if (path.startsWith("/assets/media")) return Promise.resolve(overrides.media ?? { total: 0, items: [] });
    if (path === "/assets") {
      return Promise.resolve(overrides.renders ?? { items: [], capabilities: { upload: true, note: "" } });
    }
    if (path === "/avatars") return Promise.resolve(overrides.avatars ?? { total: 0, items: [] });
    if (path === "/music/policy") {
      return Promise.resolve(
        overrides.music ?? {
          workspace_id: "ws-1",
          generate: false,
          reason: "policy_not_enabled",
          brand_disabled: false,
          provider_key: "",
          configured: false,
          forbidden_genres: [],
          prefs: {},
        },
      );
    }
    return Promise.resolve({});
  });
}

function renderAssets() {
  return render(<Assets />);
}

describe("Assets — list states", () => {
  it("renders a skeleton while the media library loads", () => {
    wsGet.mockImplementation(() => new Promise(() => undefined));
    const { container } = renderAssets();
    expect(container.querySelector(".ym-skeleton")).toBeTruthy();
    expect(screen.getAllByText("Assets").length).toBeGreaterThan(0);
  });

  it("renders an explicit empty state instead of an empty table", async () => {
    route();
    renderAssets();
    await waitFor(() => expect(screen.getByText("No media asset registered")).toBeInTheDocument());
  });

  it("surfaces the failure with the backend's own message, not a silent zero", async () => {
    route();
    wsGet.mockImplementation((path: string) =>
      path.startsWith("/assets/media")
        ? Promise.reject(new Error("asset store unreachable"))
        : Promise.resolve({}),
    );
    renderAssets();
    await waitFor(() => expect(screen.getByText("asset store unreachable")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
});

describe("Assets — a missing field is UNAVAILABLE, never 0", () => {
  it("prints UNAVAILABLE for an unset music provider instead of a blank 0", async () => {
    // /music/policy genuinely returns provider_key "" and forbidden_genres []
    // when nobody opted in. That is "not stated", not "zero genres allowed".
    route();
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Music" });
    const panel = heading.closest("section") as HTMLElement;

    const providerTile = within(panel).getByText("Provider").closest(".ym-stat");
    await waitFor(() => expect(providerTile?.textContent).toContain("UNAVAILABLE"));

    const genresTile = within(panel).getByText("Forbidden genres").closest(".ym-stat");
    expect(genresTile?.textContent).toContain("UNAVAILABLE");
    expect(genresTile?.textContent).not.toMatch(/Forbidden genres0/);
  });

  it("reports an unreported dimension as not reported rather than 0×0", async () => {
    route({
      media: {
        total: 1,
        items: [asset({ width: null, height: null, type: "image", mime_type: "image/png", origin: "upload" })],
      },
    });
    renderAssets();
    await waitFor(() => expect(screen.getAllByText("not reported").length).toBeGreaterThan(0));
    expect(screen.queryByText("0×0")).toBeNull();
  });

  it("counts only assets that actually reported a dimension", async () => {
    route({
      media: {
        total: 2,
        items: [
          asset(),
          asset({ id: "b-1", width: null, height: null, type: "image", mime_type: "image/png", duration_seconds: null }),
        ],
      },
    });
    renderAssets();
    await waitFor(() => expect(screen.getByText("With dimensions").closest(".ym-stat")?.textContent).toContain("1"));
  });
});

describe("Assets — generated vs source is read from origin", () => {
  it("labels a rendered asset Generated and an uploaded one Source", async () => {
    route({
      media: {
        total: 2,
        items: [
          asset({ id: "r-1", origin: "render", type: "video" }),
          asset({ id: "u-1", origin: "upload", type: "image", mime_type: "image/png" }),
        ],
      },
    });
    renderAssets();
    await waitFor(() => expect(screen.getAllByText("Generated").length).toBeGreaterThan(0));
    expect(screen.getAllByText("Source").length).toBeGreaterThan(0);
  });
});

describe("Assets — provenance redaction", () => {
  it("redactProvenance strips the whole query string when a signed key is present", () => {
    const red = redactProvenance(SIGNED_STORAGE_KEY);
    expect(red.redacted).toBe(true);
    expect(red.display).toBe("https://cdn.example.com/renders/clip.mp4…redacted");
    expect(red.display).not.toContain(SIGNATURE);
    expect(red.display).not.toContain(ACCESS_KEY_ID);
    expect(red.removedKeys).toEqual(
      expect.arrayContaining(["X-Amz-Signature", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires"]),
    );
  });

  it("redactProvenance keeps a clean URL intact and strips userinfo", () => {
    expect(redactProvenance("https://cdn.example.com/a.mp4?w=640")).toEqual({
      display: "https://cdn.example.com/a.mp4?w=640",
      removedKeys: [],
      redacted: false,
    });
    const userinfo = redactProvenance("https://user:hunter2@cdn.example.com/a.mp4");
    expect(userinfo.redacted).toBe(true);
    expect(userinfo.display).toBe("https://cdn.example.com/a.mp4…redacted");
  });

  it("isSignedProvenance recognises a bearer token in the query", () => {
    expect(isSignedProvenance("https://x.test/v?token=abc")).toBe(true);
    expect(isSignedProvenance("https://x.test/v?download_token=abc")).toBe(true);
    expect(isSignedProvenance("https://x.test/v?page=2")).toBe(false);
    expect(isSignedProvenance("")).toBe(false);
  });

  it("renders a signed provenance value without ever putting the secret in the DOM", async () => {
    route({ media: { total: 1, items: [asset({ storage_key: SIGNED_STORAGE_KEY })] } });
    const { container } = renderAssets();
    await waitFor(() => expect(container.querySelector("[data-provenance='redacted']")).toBeTruthy());

    const cell = container.querySelector("[data-provenance='redacted']") as HTMLElement;
    expect(cell.textContent).toContain("…redacted");
    expect(cell.textContent).toContain("credential removed");
    expect(cell.textContent).not.toContain(SIGNATURE);
    expect(cell.textContent).not.toContain(ACCESS_KEY_ID);
    expect(cell.getAttribute("title")).toBeNull();

    for (const node of Array.from(container.querySelectorAll("[data-provenance]"))) {
      for (const attr of Array.from(node.attributes)) {
        expect(attr.value).not.toContain(SIGNATURE);
        expect(attr.value).not.toContain("X-Amz-Signature");
      }
    }
  });

  it("redacts a stock provider preview URL, which is where signed URLs really live", async () => {
    route();
    wsPost.mockResolvedValue({
      items: [
        {
          video_id: "12345",
          preview: SIGNED_STORAGE_KEY,
          duration: 6.2,
          author: "Someone",
          page_url: "https://www.pexels.com/video/12345/",
        },
      ],
    });
    const { container } = renderAssets();
    const input = (await screen.findByPlaceholderText(/city night traffic/i)) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "city night" } });
    const form = input.closest("form") as HTMLFormElement;
    fireEvent.submit(form);

    await waitFor(() => expect(container.querySelector("[data-provenance='redacted']")).toBeTruthy());
    const redacted = container.querySelector("[data-provenance='redacted']") as HTMLElement;
    expect(redacted.textContent).not.toContain(SIGNATURE);
    expect(redacted.textContent).toContain("https://cdn.example.com/renders/clip.mp4…redacted");
  });
});
