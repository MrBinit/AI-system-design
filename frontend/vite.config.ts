import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const localApiTarget = String(env.VITE_DEV_API_TARGET ?? "http://localhost:8000")
    .trim()
    .replace(/\/+$/, "");

  return {
    base: "./", // required for S3 static hosting path resolution
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: localApiTarget,
          changeOrigin: true,
        },
        "/healthz": {
          target: localApiTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
