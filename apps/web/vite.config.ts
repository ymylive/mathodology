import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "node:path";

// Vite config for the Mathodology web app.
// - Dev server on 5173.
// - `/api` is proxied to the Rust gateway so the browser can use relative URLs
//   in dev while Bearer-token auth still works.
// - Env is sourced from the MONOREPO ROOT `.env` (two levels up). Without this
//   the `VITE_DEV_AUTH_TOKEN` injected into `import.meta.env` is undefined and
//   every API call returns 401.
const ROOT = path.resolve(__dirname, "../..");

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ROOT, "");
  const gatewayHttp = env.VITE_GATEWAY_HTTP ?? "http://127.0.0.1:8080";

  return {
    envDir: ROOT,
    plugins: [vue()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "src"),
      },
    },
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: gatewayHttp,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ""),
        },
      },
    },
  };
});
