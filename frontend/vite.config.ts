import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the API runs on :8000; the dev server forwards /api to it so the browser
// talks to one origin and the event stream needs no CORS setup.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } },
  },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
