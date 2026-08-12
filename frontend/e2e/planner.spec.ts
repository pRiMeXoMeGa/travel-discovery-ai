import { test, expect } from "@playwright/test";

/**
 * WS3 LangGraph planner: the human-in-the-loop interrupt/resume flow.
 *
 * Tagged @llm — each run spends Gemini quota and drives the real graph against
 * the restored corpus.
 */

const CONCIERGE_OPEN = 'button[aria-label="Open AI concierge"]';
const PLAN_MODE = 'button:has-text("Plan a trip")';
const INPUT = 'input[placeholder*="Describe the trip"]';

test.describe("planner interrupt/resume @llm", () => {
  test("suspends for approval, then finalises on approve", async ({ page }) => {
    await page.goto("/");
    await page.locator(CONCIERGE_OPEN).click();

    // Switching mode is what routes to the graph rather than the concierge.
    await page.locator(PLAN_MODE).click();

    const input = page.locator(INPUT);
    await expect(input).toBeVisible();
    await input.fill("Plan a 4-night Lisbon trip for a couple, two stays, budget $900 total.");
    await input.press("Enter");

    // The interrupt is the point of the feature: the run must STOP and ask.
    const review = page.getByText(/Approve this plan\?/i);
    await expect(review).toBeVisible({ timeout: 120_000 });

    const panel = page.locator('[class*="fixed inset-y-0"]').first();
    await expect(panel).toContainText(/stay/i);

    // Approving resumes the SAME thread server-side and finalises it.
    await page.getByRole("button", { name: "Approve", exact: true }).click();

    await expect(page.getByText(/Approve this plan\?/i)).toHaveCount(0, {
      timeout: 120_000,
    });
    await expect(panel).toContainText(/confirmed|itinerary/i);
  });

  test("adjust sends feedback and comes back for another decision", async ({ page }) => {
    await page.goto("/");
    await page.locator(CONCIERGE_OPEN).click();
    await page.locator(PLAN_MODE).click();

    const input = page.locator(INPUT);
    await input.fill("Plan a 3-night Lisbon trip for a couple, budget $900 total.");
    await input.press("Enter");

    await expect(page.getByText(/Approve this plan\?/i)).toBeVisible({ timeout: 120_000 });

    await page.getByRole("button", { name: "Adjust" }).click();
    await page.locator('input[placeholder*="What should change"]').fill("somewhere quieter");
    await page.getByRole("button", { name: "Replan" }).click();

    // Replanning returns to the checkpoint rather than committing silently.
    await expect(page.getByText(/Approve this plan\?/i)).toBeVisible({ timeout: 120_000 });
  });

  test("ask mode is unaffected by the planner work", async ({ page }) => {
    await page.goto("/");
    await page.locator(CONCIERGE_OPEN).click();

    // Default mode is still the concierge; no review card should ever appear.
    const input = page.locator('input[placeholder="Ask the concierge..."]');
    await expect(input).toBeVisible();
    await input.fill("a quiet flat in Lisbon");
    await input.press("Enter");

    await page.waitForFunction(
      () => document.querySelectorAll(".animate-spin").length === 0,
      undefined,
      { timeout: 120_000 }
    );
    await expect(page.getByText(/Approve this plan\?/i)).toHaveCount(0);
  });
});
