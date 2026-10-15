import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative asset URLs: the backend injects <base href> so the app works under any BASE_PATH.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: { "/api": { target: process.env.API_URL ?? "http://localhost:8080", changeOrigin: true } },
  },
  test: { environment: "node" },
});
