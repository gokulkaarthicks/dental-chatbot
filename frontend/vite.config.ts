import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The backend is never called directly by the browser:
//  - `npm run dev` proxies /api to the compose backend service.
//  - In Docker, nginx serves the bundle and proxies /api to the backend.
// No API keys or secrets are ever injected into the bundle.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");

  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://backend:8000",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: false,
    },
  };
});
