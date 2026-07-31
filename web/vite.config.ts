import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/profiles": "http://127.0.0.1:18765",
      "/proxies": "http://127.0.0.1:18765",
      "/sessions": "http://127.0.0.1:18765",
      "/health": "http://127.0.0.1:18765",
      "/api": "http://127.0.0.1:18765",
    },
  },
});
