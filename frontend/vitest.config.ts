import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // The contract suites read the filesystem (the OpenAPI baseline and the
    // backend Python sources). jsdom still gives the component suites a DOM.
    exclude: ["node_modules", "dist"],
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});