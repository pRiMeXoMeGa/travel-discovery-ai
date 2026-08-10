import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config for the booking surface.
 *
 * Assumes the compose stack is already up (`docker compose up -d`), because the
 * suite needs the real backend on :8000 with the restored corpus behind it —
 * these tests assert on real Inside Airbnb data, not fixtures. No `webServer`
 * block for that reason: starting a bare `next dev` here would give a frontend
 * with nothing to talk to.
 *
 *   npm run test:e2e            # everything
 *   npm run test:e2e -- --grep-invert @llm    # skip the quota-spending specs
 */
const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  // The concierge streams a multi-agent run over SSE; 30s is not enough.
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  // Agent runs hit a rate-limited free-tier LLM. Parallel workers turn that
  // into 429s that look like product failures, so keep it serial.
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // First paint compiles the route in `next dev`, which can take ~15s.
    navigationTimeout: 60_000,
    actionTimeout: 20_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
