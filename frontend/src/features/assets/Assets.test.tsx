/** @vitest-environment jsdom */
/* Assets — the three list states, the rule that an unreported metric is
 * UNAVAILABLE and not 0, and the rule that a provenance URL never renders its
 * credential.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const wsGet = vi.fn();
const wsPost = vi.fn();
const fetchMediaFile = vi.fn();
const sessionState: { capabilities: string[]; capabilitiesKnown?: boolean } = {
  capabilities: [],
  capabilitiesKnown: undefined,
};

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
  fetchMediaFile: (id: string) => fetchMediaFile(id),
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
    capabilities: sessionState.capabilities,
    capabilitiesKnown: sessionState.capabilitiesKnown,
  }),
}));

import { Assets, redactProvenance, isSignedProvenance } from "./Assets";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  wsGet.mockReset();
  wsPost.mockReset();
  fetchMediaFile.mockReset();
  sessionState.capabilities = [];
  sessionState.capabilitiesKnown = undefined;
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
    if (path === "/exports/formats") {
      return Promise.resolve(
        overrides.formats ?? {
          items: [
            { format: "SRT", available: true, reason: null, kind: "subtitle", media_type: "text/plain", suffix: ".srt" },
            { format: "MP4", available: false, reason: "ffmpeg not installed", kind: "video", media_type: "video/mp4", suffix: ".mp4" },
          ],
        },
      );
    }
    if (path === "/exports/profiles") {
      return Promise.resolve(
        overrides.profiles ?? {
          items: [{ id: "p-1", workspace_id: "ws-1", name: "Default", preset: "SRT", config: {}, is_builtin: true, created_at: null, updated_at: null }],
        },
      );
    }
    if (path === "/exports") return Promise.resolve(overrides.exports ?? { items: [] });
    if (path.startsWith("/exports/")) return Promise.resolve(overrides.exportDetail ?? exportRow());
    if (path === "/voice-preview/providers") {
      return Promise.resolve(
        overrides.voiceProviders ?? {
          items: [
            { provider: "edge", label: "Edge", offerable: true, available: true, reason: "ok", message: "configured and ready to preview", qualification_labels: [], missing_credentials: [], simulation_only: false, live_verified: false, contract_tested: false },
            { provider: "elevenlabs", label: "ElevenLabs", offerable: true, available: false, reason: "not_configured", message: "set ELEVENLABS_API_KEY under Settings", qualification_labels: [], missing_credentials: ["ELEVENLABS_API_KEY"], simulation_only: false, live_verified: false, contract_tested: false },
          ],
          offerable: ["edge"],
          unavailable: [],
          default_provider: "edge",
          max_chars: 600,
          max_per_window: 20,
          window_seconds: 60,
        },
      );
    }
    if (path.startsWith("/voice-preview/providers/")) {
      return Promise.resolve(
        overrides.voices ?? {
          provider: "edge",
          language: "",
          items: [{ id: "voice-a", gender: "feminine", locale: "en-US" }],
          count: 1,
          empty_reason: "",
          cache: "none",
        },
      );
    }
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

function exportRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "e-1",
    format: "SRT",
    profile: { name: "Default", preset: "SRT" },
    target: { type: "timeline", id: "tl-1" },
    state: "DONE",
    progress: 100,
    verification: { complete: true, checks: ["size"] },
    artifact: { url: "/api/v1/workspaces/ws-1/exports/e-1/download", size: 2048, checksum: "abc123" },
    error: null,
    attempt: 1,
    created_at: "2026-09-01T10:00:00Z",
    finished_at: "2026-09-01T10:01:00Z",
    ...overrides,
  };
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

  it("redacts percent-encoded credential query keys without exposing their values", () => {
    const url = "https://cdn.example.com/renders/clip.mp4?%74oken=1&w=640";
    const red = redactProvenance(url);

    expect(red).toEqual({
      display: "https://cdn.example.com/renders/clip.mp4…redacted",
      removedKeys: ["%74oken"],
      redacted: true,
    });
    expect(isSignedProvenance(url)).toBe(true);
  });

  it("redacts credential-shaped keys and malformed encoded keys fail closed", () => {
    for (const key of [
      "client_secret", "secret", "password", "session_id", "my_client_secret",
      "clientSecret", "accessToken", "oauthToken", "sessionId", "authToken", "%63lientSecret",
    ]) {
      const url = `https://cdn.example.com/a.mp4?${key}=private-value`;
      const red = redactProvenance(url);
      expect(isSignedProvenance(url)).toBe(true);
      expect(red.display).toBe("https://cdn.example.com/a.mp4…redacted");
      expect(red.display).not.toContain("private-value");
    }

    const malformedCases = [
      "https://cdn.example.com/a.mp4?%74oken%=malformed-secret",
      "https://cdn.example.com/a.mp4?%FFtoken=invalid-utf8-secret",
      "https://cdn.example.com/a.mp4#/%3F%FFtoken=invalid-fragment-secret",
    ];
    for (const url of malformedCases) {
      const red = redactProvenance(url);
      expect(isSignedProvenance(url)).toBe(true);
      expect(red.display).toBe("https://cdn.example.com/a.mp4…redacted");
      expect(red.display).not.toMatch(/malformed-secret|invalid-utf8-secret|invalid-fragment-secret/);
    }
  });

  it("matches form-decoded and whitespace-padded query keys", () => {
    for (const key of ["+token", "%20token"]) {
      const url = `https://cdn.example.com/renders/clip.mp4?${key}=1&w=640`;
      const red = redactProvenance(url);

      expect(isSignedProvenance(url)).toBe(true);
      expect(red.redacted).toBe(true);
      expect(red.display).toBe("https://cdn.example.com/renders/clip.mp4…redacted");
      expect(red.display).not.toContain("=1");
    }
  });

  it("redacts credential parameters in URL fragments", () => {
    const url = "https://cdn.example.com/renders/clip.mp4#access_token=1&token_type=bearer";
    const red = redactProvenance(url);

    expect(isSignedProvenance(url)).toBe(true);
    expect(red).toEqual({
      display: "https://cdn.example.com/renders/clip.mp4…redacted",
      removedKeys: ["access_token", "token_type"],
      redacted: true,
    });
    expect(red.display).not.toContain("access_token");
    expect(red.display).not.toContain("token_type");
    expect(red.display).not.toContain("=1");
  });

  it("does not ignore a credential before a later query-like fragment suffix", () => {
    const url = "https://cdn.example.com/a.mp4#access_token=1?route=preview";
    const red = redactProvenance(url);

    expect(isSignedProvenance(url)).toBe(true);
    expect(red.display).toBe("https://cdn.example.com/a.mp4…redacted");
    expect(red.display).not.toContain("access_token");
    expect(red.display).not.toContain("=1");
  });

  it("detects encoded query separators in a fragment route", () => {
    const url = "https://cdn.example.com/a.mp4#/preview%3Faccess_token=1";
    const red = redactProvenance(url);

    expect(isSignedProvenance(url)).toBe(true);
    expect(red.display).toBe("https://cdn.example.com/a.mp4…redacted");
    expect(red.display).not.toContain("access_token");
    expect(red.display).not.toContain("=1");
  });

  it("redacts OAuth refresh and OAuth tokens in queries and fragments", () => {
    const cases = [
      { url: "https://cdn.example.com/a.mp4?refresh_token=1", key: "refresh_token" },
      { url: "https://cdn.example.com/a.mp4#oauth_token=2", key: "oauth_token" },
    ];

    for (const { url, key } of cases) {
      const red = redactProvenance(url);
      expect(isSignedProvenance(url)).toBe(true);
      expect(red.display).toBe("https://cdn.example.com/a.mp4…redacted");
      expect(red.removedKeys).toContain(key);
      expect(red.display).not.toContain("=1");
      expect(red.display).not.toContain("=2");
    }
  });

  it("removes userinfo through the final authority separator", () => {
    const url = "https://user:1@attacker@cdn.example.com/a.mp4";
    const red = redactProvenance(url);

    expect(isSignedProvenance(url)).toBe(true);
    expect(red.display).toBe("https://cdn.example.com/a.mp4…redacted");
    expect(red.display).not.toContain("attacker");
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

  it("keeps OAuth query and fragment credentials out of provenance DOM attributes", async () => {
    route({
      media: {
        total: 2,
        items: [
          asset({ id: "oauth-query", storage_key: "https://cdn.example.com/a.mp4?refresh_token=1" }),
          asset({ id: "oauth-fragment", storage_key: "https://cdn.example.com/b.mp4#oauth_token=2" }),
        ],
      },
    });
    const { container } = renderAssets();

    await waitFor(() =>
      expect(container.querySelectorAll("[data-provenance='redacted']")).toHaveLength(2),
    );
    for (const node of Array.from(container.querySelectorAll("[data-provenance]"))) {
      expect(node.textContent).not.toMatch(/refresh_token|oauth_token|=1|=2/);
      for (const attr of Array.from(node.attributes)) {
        expect(attr.value).not.toMatch(/refresh_token|oauth_token|=1|=2/);
      }
    }
  });

  it("fetches preview media with Authorization instead of placing its token in src", async () => {
    route({
      media: { total: 1, items: [asset({ id: "preview-asset", mime_type: "image/png" })] },
    });
    const blob = new Blob(["image-data"], { type: "image/png" });
    const create = vi.fn(() => "blob:asset-preview");
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL = create;
      static revokeObjectURL = revoke;
    });
    fetchMediaFile.mockResolvedValue(blob);
    const { container } = renderAssets();

    fireEvent.click(await screen.findByRole("button", { name: "Grid" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Preview$/ }));
    const preview = await screen.findByAltText("Video asset preview-");
    expect(preview).toHaveAttribute("src", "blob:asset-preview");
    expect(fetchMediaFile).toHaveBeenCalledWith("preview-asset");
    for (const media of Array.from(container.querySelectorAll("img,video,audio"))) {
      expect(media.getAttribute("src")).not.toContain("token=");
    }

    cleanup();
    expect(revoke).toHaveBeenCalledWith("blob:asset-preview");
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

describe("Assets — exports", () => {
  async function selectExport() {
    const heading = await screen.findByRole("heading", { name: "Export queue" });
    const panel = heading.closest("section") as HTMLElement;
    const format = await within(panel).findByText("SRT");
    fireEvent.click(format.closest("tr") as HTMLElement);
    return (await screen.findByRole("heading", { name: "Export detail" })).closest("section") as HTMLElement;
  }

  it("downloads an artifact with bearer auth and revokes the temporary blob URL", async () => {
    route({ exports: { items: [exportRow()] } });
    const blob = new Blob(["subtitle bytes"], { type: "text/plain" });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => blob });
    vi.stubGlobal("fetch", fetchMock);
    const create = vi.fn(() => "blob:export-1");
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
    const downloads: { href: string; filename: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push({ href: this.href, filename: this.download });
    });
    renderAssets();
    const detail = await selectExport();
    fireEvent.click(within(detail).getByRole("button", { name: "Download artifact" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/workspaces/ws-1/exports/e-1/download", {
      method: "GET", headers: { Authorization: "Bearer TEST_JWT_VALUE" },
    }));
    await waitFor(() => expect(downloads).toEqual([{ href: "blob:export-1", filename: "export-e-1.srt" }]));
    expect(create).toHaveBeenCalledWith(blob);
    // Revocation may be scheduled for the next task so the browser can start the download.
    await act(async () => { await new Promise((done) => setTimeout(done, 0)); });
    expect(revoke).toHaveBeenCalledWith("blob:export-1");
    expect(document.querySelector("a[download]" )).toBeNull();
  });

  it("keeps artifact downloads available at the viewer API floor", async () => {
    sessionState.capabilitiesKnown = true;
    sessionState.capabilities = ["content.read"];
    route({ exports: { items: [exportRow()] } });
    renderAssets();
    const detail = await selectExport();
    expect(within(detail).getByRole("button", { name: "Download artifact" })).toBeEnabled();
    expect(within(detail).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("reports an artifact download refusal without clicking a download link", async () => {
    route({ exports: { items: [exportRow()] } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401, statusText: "Unauthorized", json: async () => ({ detail: "invalid or expired token" }) }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderAssets();
    const detail = await selectExport();
    fireEvent.click(within(detail).getByRole("button", { name: "Download artifact" }));
    expect(await within(detail).findByRole("alert")).toHaveTextContent("invalid or expired token");
    expect(click).not.toHaveBeenCalled();
  });

  it("renders probed formats with the probe's own unavailability reason", async () => {
    route();
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Exports" });
    const panel = heading.closest("section") as HTMLElement;
    expect(within(panel).getByText("SRT")).toBeInTheDocument();
    expect(within(panel).getByText(/ffmpeg not installed/)).toBeInTheDocument();
    expect(wsGet).toHaveBeenCalledWith("/exports/formats");
    expect(wsGet).toHaveBeenCalledWith("/exports/profiles");
  });

  it("renders an explicit empty queue instead of an empty table", async () => {
    route();
    renderAssets();
    expect(await screen.findByText("No export queued")).toBeInTheDocument();
  });

  it("queues an export with profile, probed format and target", async () => {
    route();
    wsPost.mockResolvedValue({ export_id: "e-9" });
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Queue an export" });
    const panel = heading.closest("section") as HTMLElement;
    fireEvent.change(await within(panel).findByLabelText("Profile"), { target: { value: "p-1" } });
    fireEvent.change(within(panel).getByLabelText("Format"), { target: { value: "SRT" } });
    fireEvent.change(within(panel).getByLabelText("Target id"), { target: { value: "tl-1" } });
    const form = within(panel).getByLabelText("Target id").closest("form") as HTMLFormElement;
    fireEvent.submit(form);
    await waitFor(() =>
      expect(wsPost).toHaveBeenCalledWith("/exports", {
        profile_id: "p-1",
        format: "SRT",
        target_type: "timeline",
        target_id: "tl-1",
      }),
    );
  });

  it("selecting a queued export shows verification and the artifact", async () => {
    route({ exports: { items: [exportRow()] } });
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Export queue" });
    const panel = heading.closest("section") as HTMLElement;
    const row = within(panel).getByText("SRT").closest("tr") as HTMLElement;
    fireEvent.click(row);

    const detailHeading = await screen.findByRole("heading", { name: "Export detail" });
    const detailPanel = detailHeading.closest("section") as HTMLElement;
    expect(within(detailPanel).getByText("verification complete")).toBeInTheDocument();
    expect(within(detailPanel).getByText(/1 checks recorded/)).toBeInTheDocument();
    expect(within(detailPanel).getByRole("button", { name: "Download artifact" })).toBeInTheDocument();
    // DONE is neither FAILED/CANCELLED nor QUEUED/RUNNING, so both stay disabled.
    expect(within(detailPanel).getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(within(detailPanel).getByRole("button", { name: "Cancel" })).toBeDisabled();
  });

  it("a viewer sees the gating notice instead of the queue form", async () => {
    sessionState.capabilities = [];
    sessionState.capabilitiesKnown = true;
    route();
    renderAssets();
    await waitFor(() =>
      expect(screen.getByText("Members and above can queue exports")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Queue export" })).toBeNull();
  });
});

describe("Assets — voice preview", () => {
  it("loads and submits voices for the displayed default provider without a provider click", async () => {
    route();
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 503, statusText: "Unavailable", json: async () => ({ detail: "provider unavailable" }) });
    vi.stubGlobal("fetch", fetchMock);
    renderAssets();
    const voice = await screen.findByLabelText("Voice");
    expect(screen.getByRole("button", { name: "Edge" })).toHaveAttribute("aria-pressed", "true");
    expect(wsGet).toHaveBeenCalledWith("/voice-preview/providers/edge/voices");
    fireEvent.change(voice, { target: { value: "voice-a" } });
    fireEvent.change(screen.getByLabelText("Sample text"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview voice" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/workspaces/ws-1/voice-preview", expect.objectContaining({
      body: JSON.stringify({ text: "Hello", voice: "voice-a", provider: "edge" }),
    })));
  });

  it("revokes each owned preview sample on replacement and the last one on unmount", async () => {
    route();
    const create = vi.fn().mockReturnValueOnce("blob:sample-1").mockReturnValueOnce("blob:sample-2");
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(["audio"]), headers: { get: () => null } }));
    const { unmount } = renderAssets();
    fireEvent.change(await screen.findByLabelText("Sample text"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview voice" }));
    await waitFor(() => expect(screen.getByLabelText("Voice preview sample")).toHaveAttribute("src", "blob:sample-1"));
    expect(revoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Preview voice" }));
    await waitFor(() => expect(screen.getByLabelText("Voice preview sample")).toHaveAttribute("src", "blob:sample-2"));
    expect(revoke.mock.calls).toEqual([["blob:sample-1"]]);
    unmount();
    expect(revoke.mock.calls).toEqual([["blob:sample-1"], ["blob:sample-2"]]);
  });

  it("revokes the first ready preview immediately when leaving the page", async () => {
    route();
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = () => "blob:first-sample"; static revokeObjectURL = revoke; });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(["audio"]), headers: { get: () => null } }));
    const { unmount } = renderAssets();
    fireEvent.change(await screen.findByLabelText("Sample text"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview voice" }));
    await screen.findByLabelText("Voice preview sample");
    unmount();
    expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:first-sample");
  });

  it("does not create an object URL when a pending preview finishes after unmount", async () => {
    route();
    let resolveBlob!: (blob: Blob) => void;
    const blobPromise = new Promise<Blob>((resolve) => { resolveBlob = resolve; });
    const blob = vi.fn(() => blobPromise);
    const create = vi.fn(() => "blob:late-sample");
    const revoke = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob, headers: { get: () => null } });
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderAssets();
    fireEvent.change(await screen.findByLabelText("Sample text"), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview voice" }));
    await waitFor(() => expect(blob).toHaveBeenCalledOnce());

    unmount();
    await act(async () => { resolveBlob(new Blob(["late audio"])); await blobPromise; });

    expect(create).not.toHaveBeenCalled();
    expect(revoke).not.toHaveBeenCalled();
  });

  function stubPreview(status: number, detail: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status,
        statusText: status === 429 ? "Too Many Requests" : "Payment Required",
        json: async () => ({ detail }),
        headers: { get: () => null },
        blob: async () => new Blob([]),
      }),
    );
  }

  it("discloses the cost and the cache rule before any spend", async () => {
    route();
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Voice preview" });
    const panel = heading.closest("section") as HTMLElement;
    expect(await within(panel).findByRole("button", { name: "Edge" })).toBeInTheDocument();
    expect(within(panel).getByText(/set ELEVENLABS_API_KEY/)).toBeInTheDocument();
    expect(within(panel).getByText(/estimated cost/)).toBeInTheDocument();
    expect(within(panel).getByText(/served from cache is free/)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("renders a 429 as a rate limit, never a generic error", async () => {
    route();
    stubPreview(429, {
      reason: "preview_budget_exceeded",
      message: "at most 20 voice previews per 60s per workspace",
    });
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Voice preview" });
    const panel = heading.closest("section") as HTMLElement;
    fireEvent.change(await within(panel).findByLabelText("Sample text"), {
      target: { value: "Hello world" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: "Preview voice" }));
    await waitFor(() => expect(within(panel).getByRole("alert")).toHaveTextContent(/Rate limited/));
    expect(within(panel).getByRole("alert")).toHaveTextContent(/20 previews/);
    vi.unstubAllGlobals();
  });

  it("renders a 402 as budget exhaustion with no retry promise", async () => {
    route();
    stubPreview(402, {
      reason: "budget_exhausted",
      message: "workspace daily cap reached",
    });
    renderAssets();
    const heading = await screen.findByRole("heading", { name: "Voice preview" });
    const panel = heading.closest("section") as HTMLElement;
    fireEvent.change(await within(panel).findByLabelText("Sample text"), {
      target: { value: "Hello world" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: "Preview voice" }));
    await waitFor(() => expect(within(panel).getByRole("alert")).toHaveTextContent(/Budget exhausted/));
    vi.unstubAllGlobals();
  });

  it("a viewer sees the gating notice instead of the preview form", async () => {
    sessionState.capabilities = [];
    sessionState.capabilitiesKnown = true;
    route();
    renderAssets();
    await waitFor(() =>
      expect(screen.getByText("Members and above can preview voices")).toBeInTheDocument(),
    );
    vi.unstubAllGlobals();
  });
});
