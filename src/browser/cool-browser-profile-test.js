/**
 * Loads a document like cool-browser-test.js, but wraps the workload
 * in a Chrome DevTools Protocol Profiler session and dumps the
 * captured CPU profile to the k6 console as a chunked blob. The
 * wrapper script (`k6-flamegraph`) extracts the chunks back into a
 * .cpuprofile file.
 *
 * Marker protocol (one console.log line each):
 *   __CPUPROFILE_START__<total-bytes>
 *   __CPUPROFILE_CHUNK__<base64-chunk>
 *   ...
 *   __CPUPROFILE_END__
 *
 * Base64 keeps the chunks free of newline/log-prefix issues. The
 * chunk size below stays well under k6's default line buffer.
 */

import { browser } from 'k6/browser';
import { check, fail, sleep } from 'k6';
import encoding from 'k6/encoding';
import exec from 'k6/execution';

import { checkWopi } from '../../lib/wopi_discovery.js';
import { getNetworkBytes, setWopiClientAndFile, startCool, watchPostMessages, waitForMessage } from '../../lib/test_utils.js';
import { wopiHost, wopiUrl } from '../config.js';

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: {
        ui: {
            executor: 'shared-iterations',
            vus: 1,
            iterations: 1,
            options: {
                browser: {
                    type: 'chromium',
                },
            },
        },
    },
};

const browserOptions = {
    ignoreHTTPSErrors: true,
};

const CHUNK = 16000;

function dumpProfile(profile) {
    const text = JSON.stringify(profile);
    const b64 = encoding.b64encode(text);
    console.log(`__CPUPROFILE_START__${b64.length}`);
    for (let i = 0; i < b64.length; i += CHUNK) {
        console.log(`__CPUPROFILE_CHUNK__${b64.substring(i, i + CHUNK)}`);
    }
    console.log('__CPUPROFILE_END__');
}

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

function listAllProps(obj, label) {
    if (!obj) {
        console.log(`PROBE ${label}: <falsy ${typeof obj}>`);
        return;
    }
    const seen = new Set();
    let proto = obj;
    let depth = 0;
    while (proto && depth < 4) {
        for (const k of Object.getOwnPropertyNames(proto)) {
            if (seen.has(k)) continue;
            seen.add(k);
            // typeof on a property descriptor may throw on getters
            let t = 'unknown';
            try { t = typeof obj[k]; } catch (e) { t = `<throws ${e.message}>`; }
            console.log(`PROBE ${label}[${depth}].${k} = ${t}`);
        }
        proto = Object.getPrototypeOf(proto);
        depth++;
    }
}

async function getCDPSession(browser, context, page) {
    listAllProps(browser, 'browser');
    listAllProps(context, 'context');
    listAllProps(page, 'page');
    // k6/browser exposes newCDPSession at different places across
    // versions; probe each candidate, log what we see so a mismatch
    // is debuggable, then try in order.
    console.log(`CDP probe: typeof context.newCDPSession=${typeof context.newCDPSession} ` +
                `typeof page.context=${typeof page.context} ` +
                `typeof browser.newCDPSession=${typeof browser.newCDPSession} ` +
                `typeof page.newCDPSession=${typeof page.newCDPSession}`);
    const candidates = [
        ['context.newCDPSession(page)',
            () => context.newCDPSession && context.newCDPSession(page)],
        ['context.newCDPSession()',
            () => context.newCDPSession && context.newCDPSession()],
        ['page.context().newCDPSession(page)',
            () => page.context && page.context().newCDPSession && page.context().newCDPSession(page)],
        ['browser.newCDPSession(page)',
            () => browser.newCDPSession && browser.newCDPSession(page)],
        ['page.newCDPSession()',
            () => page.newCDPSession && page.newCDPSession()],
    ];
    for (const [label, make] of candidates) {
        try {
            const cdp = await make();
            if (cdp) {
                console.log(`CDP: using ${label}`);
                return cdp;
            }
        } catch (e) {
            console.log(`CDP: ${label} threw: ${e}`);
        }
    }
    return null;
}

export default async function () {
    const context = await browser.newContext(browserOptions);
    const page = await context.newPage();
    const vuId = exec.vu.idInTest;

    const cdp = await getCDPSession(browser, context, page);
    if (!cdp) {
        console.log('CDP newCDPSession not available in this k6/browser; skipping browser profile capture');
    } else {
        await cdp.send('Profiler.enable');
        await cdp.send('Profiler.start');
    }

    try {
        console.log(`START_TIME: ${Date.now()} vu=${vuId}`);
        await page.goto(wopiHost.toString());
        await setWopiClientAndFile(page, wopiUrl.toString(), 2);
        await watchPostMessages(page, ['App_LoadingStatus']);
        await startCool(page);

        let ready = false;
        do {
            const message = await waitForMessage(page, 'App_LoadingStatus');
            if (message) {
                if (message.Values && message.Values.Status === 'Document_Loaded')
                    console.log(`TIMING: ${JSON.stringify({...message.Values, Vu: vuId})}`);
                ready = (message.Values.Status === 'Frame_Ready');
            }
        } while (!ready);
        check(ready, {
            'Got Frame Ready': (ready) => ready,
        });

        if (cdp) {
            const { profile } = await cdp.send('Profiler.stop');
            dumpProfile(profile);
        }
    } catch (error) {
        fail(`Browser profile iteration failed: ${error}`);
    } finally {
        const bytes = await getNetworkBytes(page);
        if (bytes) console.log(`network bytes: sent=${bytes.sent}, received=${bytes.received} vu=${vuId}`);
        await page.close();
    }

    sleep(1);
}
