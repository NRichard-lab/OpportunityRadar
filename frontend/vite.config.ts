import { copyFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const frontendRoot = fileURLToPath(new URL(".", import.meta.url));
const safePublicAssets = ["favicon.svg", "site.webmanifest"];

export default defineConfig({
  plugins: [
    react(),
    {
      name: "copy-safe-public-assets",
      writeBundle(options) {
        const outputDirectory = resolve(frontendRoot, options.dir || "dist");
        mkdirSync(outputDirectory, { recursive: true });
        for (const asset of safePublicAssets) {
          copyFileSync(resolve(frontendRoot, "public", asset), resolve(outputDirectory, asset));
        }
      },
    },
  ],
  base: process.env.VITE_BASE_PATH || "/",
  build: {
    copyPublicDir: false,
  },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
