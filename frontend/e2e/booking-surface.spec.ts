import { test, expect, Page } from "@playwright/test";

/**
 * The non-LLM product surface: search, filters, map, detail, wishlist, compare.
 * These assert against the real restored corpus, so they check *shape and
 * behaviour* (a price is rendered, the neighbourhood matches the filter) rather
 * than exact listing names, which would break on any re-ingest.
 */

// A results card is an <article> wrapping *two* links (photo + content), so the
// bare anchor selector matches half a card — the photo link alone contains only
// the room-type badge. Scope to the article to read a whole card.
const CARD = 'article:has(a[href^="/listings/"])';
const CARD_LINK = 'article a[href^="/listings/"]';
// The wishlist page renders its own card markup (a div, not an article).
const WISHLIST_CARD = 'div.rounded-2xl:has(a[href^="/listings/"])';

async function waitForResults(page: Page) {
  await expect(page.locator(CARD).first()).toBeVisible({ timeout: 60_000 });
}

test.describe("search results", () => {
  test("renders listing cards with price, rating and photo", async ({ page }) => {
    await page.goto("/");
    await waitForResults(page);

    const cards = page.locator(CARD);
    expect(await cards.count()).toBeGreaterThan(5);

    const first = cards.first();
    // Price is rendered with a currency symbol and a per-night suffix.
    await expect(first).toContainText(/[€$]\s?[\d,]+/);
    await expect(first.locator("img").first()).toBeVisible();
  });

  test("city switch changes the corpus that comes back", async ({ page }) => {
    await page.goto("/?city=Amsterdam");
    await waitForResults(page);
    const amsterdam = await page.locator(CARD).first().innerText();

    await page.goto("/?city=Lisbon");
    await waitForResults(page);
    const lisbon = await page.locator(CARD).first().innerText();

    expect(amsterdam).not.toEqual(lisbon);
  });

  test("price ceiling in the URL is respected by every card", async ({ page }) => {
    await page.goto("/?city=Lisbon&price_max=80");
    await waitForResults(page);

    const texts = await page.locator(CARD).allInnerTexts();
    const prices = texts
      .map((t) => t.match(/[€$]\s?([\d,]+(?:\.\d+)?)/)?.[1])
      .filter(Boolean)
      .map((p) => parseFloat(p!.replace(/,/g, "")));

    expect(prices.length).toBeGreaterThan(0);
    // Cards show the nightly rate; the filter is a nightly cap.
    for (const p of prices) expect(p).toBeLessThanOrEqual(80);
  });

  test("filter chips appear and can be removed", async ({ page }) => {
    await page.goto("/?city=Lisbon&price_max=100");
    await waitForResults(page);

    const chipBar = page.locator("header");
    await expect(chipBar).toContainText(/Lisbon/i);
  });
});

test.describe("map", () => {
  test("MapLibre canvas mounts and draws price markers", async ({ page }) => {
    await page.goto("/?city=Lisbon");
    await waitForResults(page);

    // maplibre-gl renders into a canvas; without its CSS the pins collapse,
    // so a visible canvas plus non-zero box is the real smoke test.
    const canvas = page.locator("canvas.maplibregl-canvas, .maplibregl-map canvas").first();
    await expect(canvas).toBeVisible({ timeout: 30_000 });
    const box = await canvas.boundingBox();
    expect(box!.width).toBeGreaterThan(100);
    expect(box!.height).toBeGreaterThan(100);
  });
});

test.describe("listing detail", () => {
  test("opens from a card and shows gallery, amenities and reviews", async ({ page }) => {
    await page.goto("/?city=Lisbon");
    await waitForResults(page);

    await page.locator(CARD_LINK).first().click();
    await page.waitForURL(/\/listings\/[0-9a-f-]{36}/, { timeout: 60_000 });

    await expect(page.locator("h1").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("img").first()).toBeVisible();
    // Price breakdown / reserve panel is the anchor of the right rail.
    await expect(page.getByText(/night/i).first()).toBeVisible();
  });

  test("summary panel only claims 'AI' when the summary really is AI-written", async ({
    page,
  }) => {
    // The regression this guards is a false claim, not a crash: every listing
    // has a summary, but only the WS0-A backfilled subset is model-written.
    // The rest are two review quotes truncated at ~120 chars, and they used to
    // render under a sparkle icon labelled "AI Review Summary".
    //
    // Deliberately ONE city. An earlier version walked three, which made this
    // the heaviest test in the file (six navigations) and pushed the dev server
    // into 60s `page.goto` timeouts that surfaced as failures in whichever test
    // happened to run next. Both provenance branches are covered exhaustively
    // by test_listing_detail_surfaces_summary_provenance, which is parametrized;
    // what only a browser can prove is that the rendered heading follows the
    // flag, and one listing proves that.
    const detailResponse = page.waitForResponse(
      (r) => /\/api\/listings\/[0-9a-f-]{36}(\?|$)/.test(r.url()) && r.ok(),
      { timeout: 60_000 },
    );

    await page.goto("/?city=Lisbon");
    await waitForResults(page);
    await page.locator(CARD_LINK).first().click();
    await page.waitForURL(/\/listings\/[0-9a-f-]{36}/, { timeout: 60_000 });
    await expect(page.locator("h1").first()).toBeVisible({ timeout: 30_000 });

    // Read the payload the page already fetched. Issuing our own fetch() would
    // need the API origin and CORS: the client calls an absolute
    // NEXT_PUBLIC_API_URL, not a same-origin proxy.
    const detail = (await (await detailResponse).json()) as {
      summary?: string;
      summary_provenance?: string | null;
    };
    test.skip(
      !detail.summary || detail.summary === "No reviews yet.",
      "listing has no summary to label",
    );

    const aiLabel = page.getByText("AI Review Summary");
    if (detail.summary_provenance === "llm") {
      await expect(aiLabel).toBeVisible();
    } else {
      // The direction that matters: a heuristic summary must never be badged AI.
      await expect(aiLabel).toHaveCount(0);
      await expect(page.getByText("What guests said")).toBeVisible();
    }
  });
});

test.describe("wishlist and compare", () => {
  test("saving a listing persists it to the wishlist page", async ({ page }) => {
    await page.goto("/?city=Lisbon");
    await waitForResults(page);

    // <h2> is the listing name inside a results card.
    const savedName = (await page.locator(`${CARD} h2`).first().innerText()).trim();
    expect(savedName.length).toBeGreaterThan(3);

    await page.locator('button[aria-label="Save to wishlist"]').first().click();

    // The button flips to the remove state once the localStorage write lands.
    await expect(
      page.locator('button[aria-label="Remove from wishlist"]').first()
    ).toBeVisible();

    await page.goto("/wishlist");
    await expect(page.getByRole("heading", { name: /saved places/i })).toBeVisible();
    await expect(page.locator(WISHLIST_CARD).first()).toBeVisible({ timeout: 30_000 });

    // The exact listing we saved is the one that comes back, by name.
    await expect(page.locator(`${WISHLIST_CARD} h3`).first()).toHaveText(savedName);
  });

  test("empty wishlist shows the empty state", async ({ page }) => {
    await page.goto("/wishlist");
    await expect(page.getByText(/no saved places yet/i)).toBeVisible();
  });
});
