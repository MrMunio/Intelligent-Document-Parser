import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API is proxied so the app is same-origin, exactly as behind nginx in Docker.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.VITE_API_TARGET || "http://localhost:8000", changeOrigin: true } },
  },
  build: { target: "es2022", chunkSizeWarningLimit: 1500 },
});
