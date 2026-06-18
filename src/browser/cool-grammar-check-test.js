/**
 * Loads writer-large-edit.fodt (831 English paragraphs) and keeps the
 * document open long enough for the GrammarCheckingIterator background
 * thread (engine/linguistic/source/gciterator.cxx) to grind through
 * its queue. Use with k6-flamegraph to confirm the
 * GetGrammarChecker per-paragraph hot path on the kit side.
 *
 * Drives the same setup as cool-browser-profile-test.js and reuses
 * its CDP-based browser CPU profile capture (falls back silently if
 * the k6/browser image doesn't expose newCDPSession).
 *
 * Tunable via env vars:
 *   COOL_K6_FILE_ID    - WOPI file id to open (default 3, which maps
 *                        to writer-large-edit.fodt in files.json)
 *   COOL_K6_DWELL_SEC  - seconds to keep the document open after
 *                        Frame_Ready, to let the grammar thread
 *                        iterate (default 30)
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

const FILE_ID = __ENV.COOL_K6_FILE_ID || '3';
const DWELL_SEC = parseInt(__ENV.COOL_K6_DWELL_SEC || '30', 10);

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

async function getCDPSession(browser, context, page) {
    const candidates = [
        () => context.newCDPSession && context.newCDPSession(page),
        () => page.context && page.context().newCDPSession && page.context().newCDPSession(page),
        () => browser.newCDPSession && browser.newCDPSession(page),
        () => page.newCDPSession && page.newCDPSession(),
    ];
    for (const make of candidates) {
        try {
            const cdp = await make();
            if (cdp) return cdp;
        } catch (e) {
            // try next candidate
        }
    }
    return null;
}

export function setup() {
    checkWopi(wopiHost, wopiUrl);
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
        await setWopiClientAndFile(page, wopiUrl.toString(), FILE_ID);
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

        // Hold the document open. The kit-side GrammarCheckingIterator
        // continues to dequeue paragraphs while we idle here; the
        // wrapper's perf record captures that work.
        console.log(`document loaded; dwelling ${DWELL_SEC}s for background work...`);
        sleep(DWELL_SEC);

        if (cdp) {
            const { profile } = await cdp.send('Profiler.stop');
            dumpProfile(profile);
        }
    } catch (error) {
        fail(`Grammar-check iteration failed: ${error}`);
    } finally {
        const bytes = await getNetworkBytes(page);
        if (bytes) console.log(`network bytes: sent=${bytes.sent}, received=${bytes.received} vu=${vuId}`);
        await page.close();
    }

    sleep(1);
}
