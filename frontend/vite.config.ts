import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "./", // ✅ REQUIRED for S3
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/healthz": {
        // target: "http://3.87.88.133:8000",
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});