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
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy },
  preview: { proxy },
});