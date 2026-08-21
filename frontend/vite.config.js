import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The backend now serves routes under /api itself (see the APIRouter
      // mount in backend/app/main.py), so this only needs to forward the
      // request — no more stripping the /api prefix, which used to be the
      // one thing that made `/api` a dev-only fiction (see HANDOFF.md).
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
