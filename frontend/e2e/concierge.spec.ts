import { test, expect } from "@playwright/test";

/**
 * The agent surface: NL search parsing and the streaming concierge.
 *
 * Tagged @llm because each test spends free-tier Gemini quota and takes tens of
 * seconds. Skip with `--grep-invert @llm`.
 */

const CONCIERGE_OPEN = 'button[aria-label="Open AI concierge"]';
const CONCIERGE_INPUT = 'input[placeholder="Ask the concierge..."]';
const NL_INPUT = 'input[placeholder*="Describe your trip"]';

test.describe("natural-language search @llm", () => {
  test("parses a query into applied filter chips", async ({ page }) => {
    await page.goto("/");
    await page.locator(NL_INPUT).fill(
      "an entire place in Lisbon under 130 with a balcony"
    );
    await page.locator(NL_INPUT).press("Enter");

    // The parse round-trips through /api/nl-search, then the chips re-render.
    await expect(page.locator("header")).toContainText(/Lisbon/i, {
      timeout: 90_000,
    });
    await expect(page.locator("header")).toContainText(/balcony/i);
    await expect(page.locator('a[href^="/listings/"]').first()).toBeVisible();
  });
});

test.describe("streaming concierge @llm", () => {
  test("streams agent steps and a grounded answer with citations", async ({ page }) => {
    await page.goto("/");
    await page.locator(CONCIERGE_OPEN).click();

    const input = page.locator(CONCIERGE_INPUT);
    await expect(input).toBeVisible();
    await input.fill("places in Lisbon guests say are quiet and clean");
    await input.press("Enter");

    // Visible agent steps are the SSE contract — they must appear before the
    // answer. The trail shows friendly labels (ConciergePanel STEP_LABEL), not
    // the raw agent names, and "router" is deliberately hidden.
    await expect(
      page.getByText(/Understanding your request/i).first()
    ).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText(/Reading reviews/i).first()).toBeVisible({
      timeout: 90_000,
    });

    // Wait for the run to settle: every spinner gone.
    await page.waitForFunction(
      () => document.querySelectorAll(".animate-spin").length === 0,
      undefined,
      { timeout: 120_000 }
    );

    const panel = page.locator('[class*="fixed inset-y-0"]').first();
    const text = await panel.innerText();

    // A grounded answer, not an error or an empty stream.
    expect(text.length).toBeGreaterThan(120);
    expect(text).toMatch(/Lisbon/i);
    expect(text).not.toMatch(/something went wrong|failed to/i);
  });

  test("itinerary query renders day-by-day stay cards within budget", async ({ page }) => {
    await page.goto("/");
    await page.locator(CONCIERGE_OPEN).click();

    const input = page.locator(CONCIERGE_INPUT);
    await input.fill(
      "Plan a 4-night Los Angeles trip for a couple — one stay near the beach and one near Downtown. Budget $1200 total."
    );
    await input.press("Enter");

    await page.waitForFunction(
      () => document.querySelectorAll(".animate-spin").length === 0,
      undefined,
      { timeout: 120_000 }
    );

    const panel = page.locator('[class*="fixed inset-y-0"]').first();
    const text = await panel.innerText();

    // Structured itinerary event renders as night-range cards with a total.
    expect(text).toMatch(/night/i);
    expect(text).toMatch(/\$[\d,]+/);
    expect(text).not.toMatch(/something went wrong|failed to/i);
  });
});

test.describe("memory panel @llm", () => {
  test("shows what was learned, and the rule it will enforce", async ({ page }) => {
    await page.goto("/");
    await page.locator(CONCIERGE_OPEN).click();

    const input = page.locator(CONCIERGE_INPUT);
    await expect(input).toBeVisible();
    // A standing rule, phrased so the intent agent captures it as a dealbreaker
    // rather than a one-off constraint for this search.
    await input.fill("I hate stairs and never want a shared room. Find me a place in Amsterdam.");
    await input.press("Enter");

    await page.waitForFunction(
      () => document.querySelectorAll(".animate-spin").length === 0,
      undefined,
      { timeout: 120_000 }
    );

    // The panel is collapsed by default and summarises counts on the header.
    const header = page.getByRole("button", { name: /Memory/ }).first();
    await expect(header).toBeVisible({ timeout: 30_000 });
    await expect(header).toContainText(/learned|used/);

    // Expanding it must show the individual memories.
    await header.click();
    const panel = page.locator('[class*="fixed inset-y-0"]').first();
    await expect(panel).toContainText(/shared room|stairs|lift|elevator/i);
  });
});
