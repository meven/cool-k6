// Minimal browser connectivity probe. Confirms whether the Chromium that
// k6 launches inside the container can reach the WOPI host and COOL over
// the host network. No WOPI flow, just two navigations with a short
// timeout so a black-holed connection fails fast instead of hanging.
import { browser } from 'k6/browser';

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: {
        ui: {
            executor: 'shared-iterations',
            vus: 1,
            iterations: 1,
            options: { browser: { type: 'chromium' } },
        },
    },
};

const targets = [
    'http://localhost:3000/',
    'https://localhost:9980/',
    'https://localhost:9980/hosting/discovery',
];

export default async function () {
    // Same context options the real browser tests use, so a self-signed
    // COOL cert does not abort the navigation.
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await context.newPage();
    for (const url of targets) {
        try {
            const resp = await page.goto(url, { timeout: 12000, waitUntil: 'load' });
            console.log(`PROBE ok   ${url} -> ${resp ? resp.status() : 'no-response'}`);
        } catch (e) {
            console.log(`PROBE FAIL ${url} -> ${e}`);
        }
    }
    await page.close();
    await context.close();
}
