import { copyFileSync, cpSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = join(__dirname, "..");
const dist = join(webRoot, "dist");
const target = join(webRoot, "..", "src", "web_dist");

if (!existsSync(join(dist, "index.html"))) {
  console.error("web/dist/index.html missing — run vite build first");
  process.exit(1);
}

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(dist, target, { recursive: true });
console.log(`Copied ${dist} -> ${target}`);
