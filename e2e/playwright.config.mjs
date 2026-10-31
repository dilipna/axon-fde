import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
if (process.env.GITHUB_ACTIONS !== "true")
  throw Error("Browser execution is restricted to GitHub runners.");
const origin = process.env.AXON_E2E_ORIGIN || "http://127.0.0.1:3290/";
if (
  !["http://127.0.0.1:3290/", "https://dilipna.github.io/axon-fde/"].includes(
    origin,
  )
)
  throw Error("Unexpected Axon verification origin");
export default defineConfig({
  testDir: "./tests",
  outputDir: "../e2e-results/artifacts",
  workers: 1,
  retries: 0,
  timeout: 30000,
  globalTimeout: 600000,
  reporter: [["list"], ["json", { outputFile: "../e2e-results/results.json" }]],
  use: {
    baseURL: origin,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: origin.startsWith("http://127.0.0.1")
    ? {
        cwd: fileURLToPath(new URL("../site", import.meta.url)),
        command: "python -m http.server 3290 --bind 127.0.0.1",
        url: origin,
        reuseExistingServer: false,
        timeout: 30000,
      }
    : undefined,
});
