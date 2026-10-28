import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /api, /ws and /direct go to the nginx load balancer on 8003.
// In Docker the frontend's own nginx does the same (see nginx.conf).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8003", rewrite: (path) => path.replace(/^\/api/, "") },
      "/ws": { target: "ws://localhost:8003", ws: true },
      "/direct": { target: "http://localhost:8003", ws: true },
    },
  },
});
