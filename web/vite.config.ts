import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` talks to the API on localhost:8000 through the same /api and /ws paths nginx uses.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", rewrite: (p) => p.replace(/^\/api/, "") },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
