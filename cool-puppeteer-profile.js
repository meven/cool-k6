#!/usr/bin/env node
/**
 * Puppeteer-driven browser CPU profile capture for cool-k6.
 *
 * Replicates the cool-browser-profile-test.js flow but uses Puppeteer's
 * native CDP session so the V8 Profiler API actually works (k6/browser
 * v2 abstracts away the CDP layer with no escape hatch). Writes the
 * captured profile to $OUT_PATH.
 *
 * Env:
 *   WOPI_HOST     - URL of the WOPI demo host page (default https://localhost:3000/)
 *   WOPI_URL      - URL of the COOL server         (default https://localhost:9980/)
 *   OUT_PATH      - where to write the .cpuprofile (default ./browser.cpuprofile)
 *   FILE_ID       - WOPI file id to load           (default 2)
 *   EXECUTABLE    - path to chromium               (default: Puppeteer's bundled one)
 *   PUPPETEER_HEADLESS - "false" to disable headless mode (default headless 'new')
 *   FRAME_READY_TIMEOUT_MS - default 60000
 *   TYPE_REPEATS     - how many times to repeat the sample sentence (default 0
 *                      = skip typing; typical value: 5-20)
 *   TYPE_DELAY_MS    - per-keystroke delay (default 30)
 *   TYPE_SETTLE_MS   - extra wait after typing finishes (default 1500)
 *   TYPE_TEXT        - sentence to type (default "The quick brown fox jumps
 *                      over the lazy dog. ")
 */
import fs from 'node:fs';
import path from 'node:path';
import puppeteer from 'puppeteer';

const wopiHost = process.env.WOPI_HOST || 'https://localhost:3000/';
const wopiUrl  = process.env.WOPI_URL  || 'https://localhost:9980/';
const outPath  = process.env.OUT_PATH  || 'browser.cpuprofile';
const fileId   = process.env.FILE_ID   || '2';
const execPath = process.env.EXECUTABLE || undefined;
const headless = process.env.PUPPETEER_HEADLESS === 'false' ? false : 'new';
const frameReadyTimeoutMs = Number(process.env.FRAME_READY_TIMEOUT_MS || 60000);
const typeRepeats = Number(process.env.TYPE_REPEATS || 0);
const typeDelayMs = Number(process.env.TYPE_DELAY_MS || 30);
const typeSettleMs = Number(process.env.TYPE_SETTLE_MS || 1500);
const typeText = process.env.TYPE_TEXT
    || 'The quick brown fox jumps over the lazy dog. ';

async function main() {
    const launchOpts = {
        headless,
        executablePath: execPath,
        args: [
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--ignore-certificate-errors',
        ],
    };

    const browser = await puppeteer.launch(launchOpts);
    let exitCode = 0;
    try {
        const page = await browser.newPage();
        const cdp = await page.createCDPSession();
        await cdp.send('Profiler.enable');

        console.log(`START_TIME: ${Date.now()} vu=1`);
        await page.goto(wopiHost, { waitUntil: 'domcontentloaded' });

        // Set the WOPI client URL and file id, then load.
        await page.waitForSelector('#collabora-online-server');
        await page.$eval('#collabora-online-server',
            (el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
            },
            wopiUrl);
        await page.$eval('#wopi-file-id',
            (el, v) => {
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
            },
            fileId);

        // Buffer App_LoadingStatus postMessages for later polling.
        await page.evaluate(() => {
            window._k6cool = { messages: [], documentLoadedValues: null };
            window.addEventListener('message', (event) => {
                let msg;
                try { msg = JSON.parse(event.data); } catch { return; }
                if (msg && msg.MessageId === 'App_LoadingStatus') {
                    window._k6cool.messages.push(msg);
                    if (msg.Values && msg.Values.Status === 'Document_Loaded') {
                        window._k6cool.documentLoadedValues = msg.Values;
                    }
                }
            });
        });

        await cdp.send('Profiler.start');
        const profileStart = Date.now();

        await page.click('#load-button');

        // Wait for Frame_Ready.
        const t0 = Date.now();
        let ready = false;
        while (!ready) {
            ready = await page.evaluate(() => {
                const i = window._k6cool.messages.findIndex(m =>
                    m.MessageId === 'App_LoadingStatus' &&
                    m.Values && m.Values.Status === 'Frame_Ready');
                if (i >= 0) {
                    window._k6cool.messages.splice(i, 1);
                    return true;
                }
                return false;
            });
            if (Date.now() - t0 > frameReadyTimeoutMs) {
                throw new Error(
                    `timed out waiting for Frame_Ready after ${frameReadyTimeoutMs}ms`);
            }
            if (!ready) await new Promise(r => setTimeout(r, 100));
        }
        const elapsed = Date.now() - profileStart;
        console.log(`Frame_Ready in ${elapsed}ms`);

        // Emit the Document_Loaded timing values on a stable marker
        // line so external tooling can scrape them.
        const timing = await page.evaluate(() => window._k6cool.documentLoadedValues);
        if (timing) console.log(`TIMING: ${JSON.stringify({...timing, Vu: 1})}`);

        if (typeRepeats > 0) {
            // Find the editor iframe and click into its canvas area.
            // The WOPI demo wraps Cool in an iframe with id
            // collabora-online-viewer.
            const iframeHandle = await page.waitForSelector(
                '#collabora-online-viewer', { timeout: 5000 });
            const box = await iframeHandle.boundingBox();
            if (!box) throw new Error('iframe has no bounding box');
            // Click ~60% down the iframe so the cursor lands somewhere in
            // the editable canvas rather than the toolbar.
            await page.mouse.click(
                box.x + box.width / 2,
                box.y + box.height * 0.6,
            );
            await new Promise(r => setTimeout(r, 500));

            console.log(`typing ${typeRepeats}x "${typeText}" (delay=${typeDelayMs}ms)...`);
            const typeStart = Date.now();
            for (let i = 0; i < typeRepeats; i++) {
                await page.keyboard.type(typeText, { delay: typeDelayMs });
            }
            console.log(`typing finished in ${Date.now() - typeStart}ms`);

            // Let any pending paint / save / collaboration traffic settle
            // before stopping the profiler.
            await new Promise(r => setTimeout(r, typeSettleMs));
        }

        const { profile } = await cdp.send('Profiler.stop');
        fs.writeFileSync(outPath, JSON.stringify(profile));
        const size = fs.statSync(outPath).size;
        console.log(`wrote CPU profile -> ${path.resolve(outPath)} (${size} bytes)`);
    } catch (err) {
        console.error(err);
        exitCode = 1;
    } finally {
        try {
            const pages = await browser.pages();
            const top = pages[0];
            const target = top && top.frames().find(f =>
                (f.url() || '').includes('/browser/'));
            if (target) {
                const bytes = await target.evaluate(() => {
                    const s = window.app && window.app.socket;
                    if (!s) return null;
                    return {
                        sent: typeof s.getBytesSent === 'function' ? s.getBytesSent() : null,
                        received: typeof s.getBytesReceived === 'function' ? s.getBytesReceived() : null,
                    };
                });
                if (bytes) console.log(`network bytes: sent=${bytes.sent}, received=${bytes.received} vu=1`);
            }
        } catch (e) {
            console.error(`failed to read network bytes: ${e}`);
        }
        await browser.close();
    }
    process.exit(exitCode);
}

main();
