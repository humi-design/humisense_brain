import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api to the FastAPI backend so the browser only ever talks to
// one origin. That keeps the session cookie first-party and avoids CORS preflights.
//
// Hosts that may reach the dev server are opt-in. Vite rejects unknown Host headers by
// default, which prevents a DNS-rebinding page from reading the dev server's responses;
// VITE_ALLOWED_HOSTS exists for tunnelled or proxied development, and is not a production
// setting (production serves the built assets from the reverse proxy, not Vite).
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    allowedHosts: allowedHosts.length > 0 ? allowedHosts : undefined,
    proxy: {
      "/api": {
        target: process.env.VITE_BACKEND_URL ?? "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
