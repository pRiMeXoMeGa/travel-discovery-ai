const { chromium } = require("@playwright/test");
const fs = require("fs");
const SCREENSHOT_DIR = "D:\\playwright-verify";
if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();

  await page.goto("http://localhost:3000", { waitUntil: "networkidle" });
  
  const nlInput = page.locator('input[placeholder*="Describe your trip"]');
  const visible = await nlInput.isVisible().catch(() => false);
  console.log("NL input visible:", visible);

  if (visible) {
    await nlInput.fill("beachfront apartment");
    await page.screenshot({ path: `${SCREENSHOT_DIR}/t3-before-enter.png` });
    await page.keyboard.press("Enter");
    await page.waitForTimeout(4000);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/t3-after-enter.png` });
    
    // Check for chips (FilterChips component)
    const chipText = await page.evaluate(() => {
      const chips = document.querySelectorAll('[class*="rounded-full"][class*="border"]');
      return Array.from(chips).map(c => c.innerText.slice(0, 40));
    });
    console.log("Chip-like elements:", chipText);
    
    const articleCount = await page.locator("article").count();
    console.log("Article count:", articleCount);
    
    // Get page text snapshot
    const mainText = await page.evaluate(() => {
      return document.body.innerText.slice(0, 800);
    });
    console.log("Page text:", mainText);
    
    const pass = articleCount > 0 || chipText.length > 0;
    console.log("\nTEST 3:", pass ? "PASS" : "FAIL", "| articles:", articleCount, "chips:", chipText.length);
  } else {
    console.log("TEST 3: FAIL - NL input not found");
  }

  await browser.close();
})().catch(err => { console.error("ERROR:", err); process.exit(1); });
