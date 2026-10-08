import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/* The API proxy is shared by `server` (dev) and `preview` (the built bundle the
 * browser suite drives).
 *
 * Before this they were not shared, and `vite preview` served the app with NO
 * proxy at all -- so every browser request to `/api/v1/...` 404'd against the
 * static server and the suite would have "passed" against a blank API. That is
 * the worst shape a browser test can have: green, and blind.
 *
 * The target is overridable because the hardcoded port was itself a hazard: the
 * browser suite has to be able to run against its own backend without colliding
 * with a developer's running server.
 */
const API_TARGET = process.env.YMONEY_API_TARGET ?? "http://127.0.0.1:8100";

const proxy = {
  "/api": {
    target: API_TARGET,
    changeOrigin: true,
  },
  // Root-mounted process probes (`api/internalOps.ts`). The backend mounts
  // `internal_ops_router` on the APP ROOT -- not under `/api/v1` -- because
  // they carry no workspace scope. Without these entries the browser asks the
  // static server for `/livez` and gets a 404, so Operations shows "Could not
  // load" with a Retry on every load while every other panel is green. That
  // is exactly the failure this comment exists to prevent from recurring:
  // a probe path that is correct in the client but unreachable through the
  // proxy reads as a backend outage.
  "/livez": {
    target: API_TARGET,
    changeOrigin: true,
  },
  "/internal": {
    target: API_TARGET,
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy },
  preview: { proxy },
});