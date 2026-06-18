const { chromium } = require("@playwright/test");
const fs = require("fs");
const SCREENSHOT_DIR = "D:\\playwright-verify";
if (!fs.existsSync(SCREENSHOT_DIR)) fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();

  await page.goto("http://localhost:3000", { waitUntil: "networkidle" });
  await page.screenshot({ path: `${SCREENSHOT_DIR}/t3-home.png` });
  
  // Dump all input elements
  const inputs = await page.evaluate(() => {
    return Array.from(document.querySelectorAll("input, textarea")).map(el => ({
      tag: el.tagName,
      placeholder: el.getAttribute("placeholder"),
      type: el.getAttribute("type"),
      class: el.className.slice(0, 60)
    }));
  });
  console.log("All inputs on main page:", JSON.stringify(inputs, null, 2));

  await browser.close();
})().catch(err => { console.error("ERROR:", err); process.exit(1); });
