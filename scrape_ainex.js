const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  
  const routes = ['/', '/crm', '/inbox', '/quotes', '/tasks', '/analytics'];
  
  for (const route of routes) {
    console.log("Scraping " + route);
    await page.goto('https://ainexstudio.web.app' + route, { waitUntil: 'networkidle' });
    const html = await page.content();
    const name = route === '/' ? 'index' : route.replace('/', '');
    fs.writeFileSync(`ainex_${name}.html`, html);
  }
  
  await browser.close();
})();
