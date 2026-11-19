import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// base: './' -> relative assets, so ONE build serves at the hostname root AND
// under /marquee/ behind the Homepage proxy. Never bake an absolute prefix.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { proxy: { "/api": "http://localhost:8410", "/auth": "http://localhost:8410",
                     "/healthz": "http://localhost:8410", "/ws": { target: "ws://localhost:8410", ws: true } } },
});
