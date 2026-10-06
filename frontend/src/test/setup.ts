import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

/* jsdom does not implement matchMedia, and `prefers-reduced-motion` is a
 * token the stylesheet keys off. Without this the query in `useNotices` style
 * hooks throws inside every render.
 */
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

/* React 19 calls this during render for components using `useId` inside a
 * document fragment. jsdom provides it, but be explicit so a future jsdom bump
 * does not surface as an unexplained test failure.
 */
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

afterEach(() => {
  cleanup();
  // jsdom exposes localStorage, but the shim is not guaranteed to implement
  // clear(); guard rather than failing every suite on teardown.
  try {
    window.localStorage?.clear?.();
  } catch {
    /* no storage in this environment */
  }
});