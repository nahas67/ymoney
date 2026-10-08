/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";

const authStorage = new Map<string, string>();

function clearAuthStorage() {
  authStorage.clear();
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
  clearAuthStorage();
});

describe("authenticated media fetch", () => {
  it("sends the access token in Authorization, never in the media URL", async () => {
    vi.resetModules();
    clearAuthStorage();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => authStorage.get(key) ?? null,
      setItem: (key: string, value: string) => authStorage.set(key, value),
      removeItem: (key: string) => authStorage.delete(key),
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["media"]),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchMediaFile, setAuth, setWorkspace } = await import("./api");
    setAuth("synthetic-access-token");
    setWorkspace("workspace-1");

    await fetchMediaFile("asset-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/workspaces/workspace-1/assets/media/asset-1/file",
      { headers: { Authorization: "Bearer synthetic-access-token" } },
    );
    expect(fetchMock.mock.calls[0][0]).not.toContain("token=");
  });

  it("fetches rendered video files through a token-free URL with header auth", async () => {
    vi.resetModules();
    clearAuthStorage();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => authStorage.get(key) ?? null,
      setItem: (key: string, value: string) => authStorage.set(key, value),
      removeItem: (key: string) => authStorage.delete(key),
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["video"]),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchVideoFile, setAuth, setWorkspace } = await import("./api");
    setAuth("synthetic-access-token");
    setWorkspace("workspace-1");

    await fetchVideoFile("video-9");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/workspaces/workspace-1/videos/video-9/file",
      { headers: { Authorization: "Bearer synthetic-access-token" } },
    );
    expect(fetchMock.mock.calls[0][0]).not.toContain("token=");
  });

  it("fetches video thumbnails through a token-free URL with header auth", async () => {
    vi.resetModules();
    clearAuthStorage();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => authStorage.get(key) ?? null,
      setItem: (key: string, value: string) => authStorage.set(key, value),
      removeItem: (key: string) => authStorage.delete(key),
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["thumbnail"]),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchVideoThumbnail, setAuth, setWorkspace } = await import("./api");
    setAuth("synthetic-access-token");
    setWorkspace("workspace-1");

    await fetchVideoThumbnail("video-9");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/workspaces/workspace-1/videos/video-9/thumbnail",
      { headers: { Authorization: "Bearer synthetic-access-token" } },
    );
    expect(fetchMock.mock.calls[0][0]).not.toContain("token=");
  });

  it("fetches workspace logos through a token-free URL with header auth", async () => {
    vi.resetModules();
    clearAuthStorage();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => authStorage.get(key) ?? null,
      setItem: (key: string, value: string) => authStorage.set(key, value),
      removeItem: (key: string) => authStorage.delete(key),
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["logo"]),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { fetchWorkspaceLogo, setAuth } = await import("./api");
    setAuth("synthetic-access-token");

    await fetchWorkspaceLogo("workspace-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/workspaces/workspace-1/brand/logo/file",
      { headers: { Authorization: "Bearer synthetic-access-token" } },
    );
    expect(fetchMock.mock.calls[0][0]).not.toContain("token=");
  });
});
