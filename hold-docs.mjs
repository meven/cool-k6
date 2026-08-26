// Open several documents at once and hold them open, so the memory a kit shares
// with its parent can be told apart from what each document adds.
import puppeteer from 'puppeteer';
const host = process.env.WOPI_HOST || 'https://localhost:3000/';
const url  = process.env.WOPI_URL  || 'https://localhost:9980/';
const ids  = (process.env.FILE_IDS || '12').split(',');
const holdMs = Number(process.env.HOLD_MS || 60000);

const browser = await puppeteer.launch({headless: 'new', executablePath: '/usr/bin/chromium',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--ignore-certificate-errors']});
for (const id of ids) {
  const page = await browser.newPage();
  await page.goto(host, {waitUntil: 'domcontentloaded'});
  await page.waitForSelector('#collabora-online-server');
  await page.$eval('#collabora-online-server', (el, v) => {
    el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }, url);
  await page.$eval('#wopi-file-id', (el, v) => {
    el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }, id);
  await page.evaluate(() => {
    window._m = [];
    window.addEventListener('message', e => {
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg && msg.MessageId === 'App_LoadingStatus') window._m.push(msg);
    });
  });
  await page.click('#load-button');
  const t0 = Date.now();
  for (;;) {
    const ready = await page.evaluate(() => window._m.some(m =>
      m.Values && m.Values.Status === 'Frame_Ready'));
    if (ready) break;
    if (Date.now() - t0 > 180000) throw new Error(`timeout on file ${id}`);
    await new Promise(r => setTimeout(r, 200));
  }
  console.log(`file ${id} ready in ${Date.now() - t0}ms`);
}
console.log('all open, holding');
await new Promise(r => setTimeout(r, holdMs));
await browser.close();
console.log('closed');
