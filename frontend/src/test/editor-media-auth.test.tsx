/** @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const wsGet = vi.fn();
const wsPost = vi.fn();
const authenticatedMedia = vi.fn();
const authenticatedVideo = vi.fn();
const mediaFileUrl = vi.fn((id: string) => `/assets/${id}/file?token=TEST_JWT_VALUE`);
const videoFileUrl = vi.fn((id: string) => `/videos/${id}/file?token=TEST_JWT_VALUE`);
const createObjectURL = vi.fn(() => `blob:editor-${createObjectURL.mock.calls.length}`);
const revokeObjectURL = vi.fn();
const anchorClick = vi.fn();
const waveCreate = vi.fn();
let pointerCaptureDescriptor: PropertyDescriptor | undefined;
let createObjectURLDescriptor: PropertyDescriptor | undefined;
let revokeObjectURLDescriptor: PropertyDescriptor | undefined;
let anchorClickDescriptor: PropertyDescriptor | undefined;

vi.mock("../lib/api", () => ({
  api: vi.fn(() => Promise.resolve({ id: "user-1" })),
  getToken: () => "TEST_JWT_VALUE",
  fetchMediaFile: (id: string) => authenticatedMedia(id),
  fetchVideoFile: (id: string) => authenticatedVideo(id),
  mediaFileUrl: (id: string) => mediaFileUrl(id),
  videoFileUrl: (id: string) => videoFileUrl(id),
  wsApi: {
    get: (path: string) => wsGet(path),
    post: (path: string, body?: unknown) => wsPost(path, body),
  },
}));

vi.mock("wavesurfer.js", () => ({ default: { create: (options: any) => waveCreate(options) } }));
vi.mock("../components/ui", () => ({
  Badge: ({ children }: any) => <span>{children}</span>,
  Card: ({ children }: any) => <div>{children}</div>,
  PageHeader: ({ title, actions }: any) => <header><h1>{title}</h1>{actions}</header>,
  toast: vi.fn(),
}));
vi.mock("../components/CreativeDirector", () => ({ default: () => null }));
vi.mock("../components/collab/CommentsPanel", () => ({ default: () => null }));
vi.mock("../components/collab/ConflictNotice", () => ({ default: () => null }));
vi.mock("../components/collab/ReviewStatusBar", () => ({ default: () => null }));
vi.mock("../components/collab/VersionCompare", () => ({ default: () => null }));
vi.mock("../components/intel/AudioIntelligencePanel", () => ({ default: () => null }));
vi.mock("../components/intel/VisualIntelligencePanel", () => ({ default: () => null }));
vi.mock("../components/motion/CaptionMotionPanel", () => ({ default: () => null }));
vi.mock("../components/motion/TransitionEditor", () => ({ default: () => null }));

import Editor from "../pages/Editor";

const TIMELINE = {
  name: "Media test",
  duration_seconds: 10,
  version: 1,
  tracks: [
    { id: "visual", kind: "broll", clips: [{ id: "visual-clip", name: "frame.png", start: 0, duration: 10, source: { asset_id: "visual-asset" } }] },
    { id: "audio", kind: "voice", clips: [{ id: "audio-clip", name: "voice.wav", start: 0, duration: 10, source: { asset_id: "audio-asset" } }] },
  ],
};

function mountEditor(timeline: any = TIMELINE) {
  wsGet.mockImplementation((path: string) => {
    if (path === "/timelines/tl-1") return Promise.resolve(timeline);
    if (path.endsWith("/versions")) return Promise.resolve({ versions: [] });
    if (path.endsWith("/scenes")) return Promise.resolve({ scenes: [] });
    if (path.startsWith("/assets/media")) return Promise.resolve({ items: [] });
    if (path === "/members") return Promise.resolve({ items: [] });
    return Promise.resolve({});
  });
  return render(
    <MemoryRouter initialEntries={["/timelines/tl-1"]}>
      <Routes><Route path="/timelines/:timelineId" element={<Editor />} /></Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  pointerCaptureDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "setPointerCapture");
  createObjectURLDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  revokeObjectURLDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  anchorClickDescriptor = Object.getOwnPropertyDescriptor(HTMLAnchorElement.prototype, "click");
  authenticatedMedia.mockReset().mockResolvedValue(new Blob(["asset-media"]));
  authenticatedVideo.mockReset().mockResolvedValue(new Blob(["video-media"]));
  waveCreate.mockReset().mockReturnValue({ on: vi.fn(), destroy: vi.fn(), setTime: vi.fn() });
  mediaFileUrl.mockClear();
  videoFileUrl.mockClear();
  wsGet.mockReset();
  wsPost.mockReset();
  createObjectURL.mockClear();
  revokeObjectURL.mockClear();
  anchorClick.mockClear();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
  Object.defineProperty(HTMLAnchorElement.prototype, "click", { configurable: true, value: anchorClick });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (pointerCaptureDescriptor) {
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", pointerCaptureDescriptor);
  } else {
    delete (HTMLElement.prototype as any).setPointerCapture;
  }
  if (createObjectURLDescriptor) Object.defineProperty(URL, "createObjectURL", createObjectURLDescriptor);
  else delete (URL as any).createObjectURL;
  if (revokeObjectURLDescriptor) Object.defineProperty(URL, "revokeObjectURL", revokeObjectURLDescriptor);
  else delete (URL as any).revokeObjectURL;
  if (anchorClickDescriptor) Object.defineProperty(HTMLAnchorElement.prototype, "click", anchorClickDescriptor);
  else delete (HTMLAnchorElement.prototype as any).click;
});

describe("Editor authenticated media", () => {
  it("loads image and audio previews as blobs and revokes their object URLs on unmount", async () => {
    const { container, unmount } = mountEditor();

    await waitFor(() => expect(container.querySelector("img[src]")).toBeInTheDocument());
    await waitFor(() => expect(container.querySelector("audio[src]")).toBeInTheDocument());

    expect(authenticatedMedia).toHaveBeenCalledWith("visual-asset");
    expect(authenticatedMedia).toHaveBeenCalledWith("audio-asset");
    expect(Array.from(container.querySelectorAll("[src]")).map((node) => node.getAttribute("src")))
      .toEqual(expect.arrayContaining(["blob:editor-1", "blob:editor-2"]));
    expect(container.innerHTML).not.toContain("TEST_JWT_VALUE");
    expect(mediaFileUrl).not.toHaveBeenCalled();

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:editor-1");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:editor-2");
  });

  it("fetches video-id clips through the authenticated video endpoint", async () => {
    const timeline = {
      ...TIMELINE,
      tracks: [{ id: "visual", kind: "video", clips: [{ id: "video-clip", name: "clip.mp4", start: 0, duration: 10, source: { video_id: "video-9" } }] }],
    };
    const { container } = mountEditor(timeline);

    await waitFor(() => expect(container.querySelector("video[src]")).toBeInTheDocument());

    expect(authenticatedVideo).toHaveBeenCalledWith("video-9");
    expect(videoFileUrl).not.toHaveBeenCalled();
    expect(container.querySelector("video")?.getAttribute("src")).toMatch(/^blob:/);
    expect(container.innerHTML).not.toContain("TEST_JWT_VALUE");
  });

  it("fetches waveform media as a blob and releases the waveform on unmount", async () => {
    const { unmount } = mountEditor();
    await screen.findByText("Media test");
    const voiceClip = screen.getByTitle(/^voice\.wav/);
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: vi.fn() });
    fireEvent.pointerDown(voiceClip, { pointerId: 1 });

    await waitFor(() => expect(waveCreate).toHaveBeenCalled());
    expect(authenticatedMedia).toHaveBeenCalledWith("audio-asset");
    const waveOptions = waveCreate.mock.calls[0][0];
    expect(waveOptions.url).toMatch(/^blob:/);
    expect(waveOptions.url).not.toContain("TEST_JWT_VALUE");

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith(waveOptions.url);
  });

  it("shows a media loading error without rendering a source URL", async () => {
    authenticatedMedia.mockRejectedValue(new Error("permission denied"));
    const timeline = {
      ...TIMELINE,
      tracks: [{ id: "visual", kind: "video", clips: [{ id: "broken", name: "broken.mp4", start: 0, duration: 10, source: { asset_id: "broken-asset" } }] }],
    };
    const { container } = mountEditor(timeline);

    expect(await screen.findByRole("alert")).toHaveTextContent("Media unavailable.");
    expect(container.querySelector("video[src], img[src]")).not.toBeInTheDocument();
    expect(container.innerHTML).not.toContain("TEST_JWT_VALUE");
  });

  it("switches video clips without retaining the previous media ref", async () => {
    const timeline = {
      ...TIMELINE,
      tracks: [{ id: "visual", kind: "video", clips: [
        { id: "first", name: "first.mp4", start: 0, duration: 1, source: { asset_id: "first-asset" } },
        { id: "second", name: "second.mp4", start: 2, duration: 2, source: { asset_id: "second-asset" } },
      ] }],
    };
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    let resolveFirst!: (blob: Blob) => void;
    authenticatedMedia.mockImplementation((id: string) => id === "first-asset"
      ? new Promise<Blob>((resolve) => { resolveFirst = resolve; })
      : Promise.resolve(new Blob(["second media"])));
    const { container } = mountEditor(timeline);
    await waitFor(() => expect(authenticatedMedia).toHaveBeenCalledWith("first-asset"));

    fireEvent.change(screen.getByLabelText("Seek"), { target: { value: "2" } });
    await waitFor(() => expect(container.querySelector("video[src]")).toHaveAttribute("src", "blob:editor-1"));
    expect(authenticatedMedia).toHaveBeenCalledWith("second-asset");
    await act(async () => {
      resolveFirst(new Blob(["stale first media"]));
      await Promise.resolve();
    });
    expect(container.querySelector("video")?.getAttribute("src")).toBe("blob:editor-1");
    expect(createObjectURL).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    expect(play.mock.contexts[0]).toBe(container.querySelector("video"));
  });

  it("downloads render output through an authenticated blob without a credential-bearing href", async () => {
    wsPost.mockResolvedValue({ asset_id: "render-1", width: 1080, height: 1920, duration_seconds: 5 });
    const { container, unmount } = mountEditor();
    await screen.findByText("Media test");

    fireEvent.click(screen.getByRole("button", { name: "Render MP4" }));
    const download = await screen.findByRole("button", { name: "Download render" });
    expect(container.querySelector("a[href*='token=']")).not.toBeInTheDocument();
    expect(download).not.toHaveAttribute("href");

    fireEvent.click(download);

    await waitFor(() => expect(anchorClick).toHaveBeenCalled());
    expect(authenticatedMedia).toHaveBeenCalledWith("render-1");
    const anchor = anchorClick.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.href).toMatch(/^blob:/);
    expect(anchor.download).toMatch(/\.mp4$/);
    expect(anchor.href).not.toContain("TEST_JWT_VALUE");

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith(anchor.href);
  });

  it("does not allocate an object URL when a media request resolves after unmount", async () => {
    let resolveMedia!: (blob: Blob) => void;
    authenticatedMedia.mockReturnValue(new Promise<Blob>((resolve) => { resolveMedia = resolve; }));
    const { unmount } = mountEditor();
    await waitFor(() => expect(authenticatedMedia).toHaveBeenCalledWith("visual-asset"));

    unmount();
    resolveMedia(new Blob(["late media"]));
    await Promise.resolve();

    expect(createObjectURL).not.toHaveBeenCalled();
  });
});
