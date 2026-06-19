const { chromium } = require("@playwright/test").chromium ? require("@playwright/test") : require("playwright");
const path = require("path");
const fs = require("fs");

const SCREENSHOT_DIR = "D:\\playwright-verify";
if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const results = [];

  // ─────────────────────────────────────────────────────────────────────────────
  // TEST 1: Itinerary query
  // ─────────────────────────────────────────────────────────────────────────────
  console.log("TEST 1: Itinerary query...");
  await page.goto("http://localhost:3000", { waitUntil: "networkidle" });
  await page.screenshot({ path: `${SCREENSHOT_DIR}/01-home.png` });

  // Open concierge
  await page.click('button[aria-label="Open AI concierge"]');
  await page.waitForSelector("input[placeholder*='concierge']", { timeout: 5000 });
  await page.screenshot({ path: `${SCREENSHOT_DIR}/02-concierge-open.png` });

  // Type and send itinerary query
  const input = page.locator("input[placeholder*='concierge']");
  await input.fill("Plan a 4-night Los Angeles trip — one stay near the beach and one near downtown, budget $1200 total.");
  await page.keyboard.press("Enter");

  // Wait up to 90 seconds for stream to complete (no animate-spin left)
  console.log("  Waiting for stream to complete...");
  try {
    await page.waitForFunction(
      () => document.querySelectorAll(".animate-spin").length === 0,
      { timeout: 90000 }
    );
    console.log("  Stream done - no spinning elements remain");
  } catch (e) {
    console.log("  TIMEOUT waiting for spinner to stop");
  }

  await page.screenshot({ path: `${SCREENSHOT_DIR}/03-itinerary-result.png` });

  // Check for stay cards (look for check_in/check_out dates or "Nights" text)
  const stayCards = await page.locator("text=/Nights \\d/").count();
  const totalCostEl = await page.locator("text=/\\$[\\d,]+/").count();
  const budgetBadge = await page.locator("text=/Within budget|Over budget|No budget set/").count();
  const spinners = await page.locator(".animate-spin").count();
  
  // Get actual text content for evidence
  const pageText = await page.evaluate(() => {
    const panel = document.querySelector('[class*="fixed inset-y-0"]');
    return panel ? panel.innerText.slice(0, 1500) : "panel not found";
  });
  
  results.push({
    test: "TEST 1 - Itinerary cards",
    stayCards,
    totalCostEl,
    budgetBadge,
    spinnersLeft: spinners,
    pass: stayCards > 0 && budgetBadge > 0 && spinners === 0,
    pageText: pageText.slice(0, 800)
  });

  // ─────────────────────────────────────────────────────────────────────────────
  // TEST 1b: Swap-out test
  // ─────────────────────────────────────────────────────────────────────────────
  console.log("TEST 1b: Swap-out...");
  // Find and click first "swap-outs available" expand button
  const swapExpandBtn = page.locator("button", { hasText: /swap-out/ }).first();
  const swapExpandVisible = await swapExpandBtn.isVisible().catch(() => false);
  
  let swapResult = { pass: false, reason: "No swap-out button found" };
  if (swapExpandVisible) {
    // Get total before swap
    const totalBefore = await page.evaluate(() => {
      const match = document.body.innerText.match(/\$[\d,]+/g);
      return match ? match[0] : null;
    });
    
    await swapExpandBtn.click();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/04-swap-expanded.png` });
    
    // Click "Use this" button  
    const useThisBtn = page.locator("button", { hasText: "Use this" }).first();
    const useThisVisible = await useThisBtn.isVisible().catch(() => false);
    if (useThisVisible) {
      await useThisBtn.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${SCREENSHOT_DIR}/05-after-swap.png` });
      
      const selectedBtn = await page.locator("text=Selected").count();
      const wasChanged = selectedBtn > 0;
      swapResult = { pass: wasChanged, reason: wasChanged ? "Selected button visible after swap" : "Selected button not found", totalBefore };
    } else {
      swapResult = { pass: false, reason: "Use this button not visible after expand" };
    }
  }
  results.push({ test: "TEST 1b - Swap-out", ...swapResult });

  // ─────────────────────────────────────────────────────────────────────────────
  // TEST 2: Plain search query
  // ─────────────────────────────────────────────────────────────────────────────
  console.log("TEST 2: Plain query...");
  const input2 = page.locator("input[placeholder*='concierge']");
  await input2.fill("pet friendly place in Lisbon with a balcony");
  await page.keyboard.press("Enter");

  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/06-plain-query-started.png` });

  try {
    await page.waitForFunction(
      () => document.querySelectorAll(".animate-spin").length === 0,
      { timeout: 45000 }
    );
  } catch (e) {
    console.log("  TIMEOUT waiting for plain query spinner");
  }

  await page.screenshot({ path: `${SCREENSHOT_DIR}/07-plain-query-result.png` });
  
  const spinners2 = await page.locator(".animate-spin").count();
  const assistantBubbles = await page.locator('[class*="bg-gray-50"][class*="rounded-2xl"]').count();
  const pageText2 = await page.evaluate(() => {
    const panel = document.querySelector('[class*="fixed inset-y-0"]');
    return panel ? panel.innerText.slice(0, 1000) : "";
  });
  
  results.push({
    test: "TEST 2 - Plain query",
    spinnersLeft: spinners2,
    assistantBubbles,
    pass: spinners2 === 0 && assistantBubbles > 0,
    excerpt: pageText2.slice(0, 400)
  });

  // ─────────────────────────────────────────────────────────────────────────────
  // TEST 3: NL search bar regression
  // ─────────────────────────────────────────────────────────────────────────────
  console.log("TEST 3: NL search bar regression...");
  // Close concierge panel
  const closeBtn = page.locator('button[aria-label="Close concierge"]');
  if (await closeBtn.isVisible().catch(() => false)) await closeBtn.click();
  await page.waitForTimeout(500);

  // Find NL search input
  await page.screenshot({ path: `${SCREENSHOT_DIR}/08-main-page.png` });
  
  const nlInput = page.locator('input[placeholder*="Search"]').first();
  const nlVisible = await nlInput.isVisible().catch(() => false);
  
  let test3Result = { pass: false, reason: "NL search input not found" };
  if (nlVisible) {
    await nlInput.fill("beachfront apartment");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(3000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/09-nl-search-result.png` });
    
    // Check for filter chips or results
    const chips = await page.locator('[class*="chip"], [class*="Chip"], [class*="filter-tag"]').count();
    const resultCards = await page.locator('article').count();
    test3Result = { 
      pass: resultCards > 0 || chips > 0,
      reason: `chips=${chips}, resultCards=${resultCards}`,
      chips, resultCards
    };
  }
  results.push({ test: "TEST 3 - NL search regression", ...test3Result });

  // Print results
  console.log("\n═══════════════════════════════════════════");
  console.log("VERIFICATION RESULTS");
  console.log("═══════════════════════════════════════════");
  results.forEach(r => {
    console.log(`\n${r.pass ? "PASS" : "FAIL"} | ${r.test}`);
    const { test, pass, ...rest } = r;
    Object.entries(rest).forEach(([k, v]) => console.log(`  ${k}: ${JSON.stringify(v)}`));
  });
  console.log("\nScreenshots in:", SCREENSHOT_DIR);

  await browser.close();
})().catch(err => { console.error("SCRIPT ERROR:", err); process.exit(1); });

