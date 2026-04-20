import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Vite config for the Mathodology web app.
// - Dev server on 5173.
// - `/api` is proxied to the Rust gateway so the browser can use relative URLs
//   in dev while Bearer-token auth still works.
// - Env prefix left at Vite default (`VITE_`).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const gatewayHttp = env.VITE_GATEWAY_HTTP ?? "http://127.0.0.1:8080";

  return {
    plugins: [vue(), tailwindcss()],
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
