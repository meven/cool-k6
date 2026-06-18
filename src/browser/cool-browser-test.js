/**
 * This simple test loads a document in Collabora Online in the
 * browser
 */

import http from 'k6/http';
import { browser } from 'k6/browser';
import { sleep, check, fail } from 'k6';
import { Trend } from 'k6/metrics';

import { checkWopi, getWopiClientUrl, getWopiSrc } from '../../lib/wopi_discovery.js';
import { getNetworkBytes, screenshotPage, setWopiClientAndFile, startCool, watchPostMessages, waitForMessage } from '../../lib/test_utils.js';
import exec from 'k6/execution';
import { browserScenario, wopiHost, wopiUrl } from '../config.js';

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: browserScenario(),
};

const browserOptions = {
    ignoreHTTPSErrors: true,
}

// Time to go to the page. From initial request.
const pageLoadingTime = new Trend('page_loading_time', true);
// Time to load the UI. From initial request.
const frameLoadingTime = new Trend('frame_loading_time', true);

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

export default async function () {
    let context = await browser.newContext(browserOptions);
    const page = await context.newPage();
    const vuId = exec.vu.idInTest;
    try {
        page.on('console', (msg) => {
            let text = msg.text();
            if (text.startsWith("DEBUG")) {
                console.log(`page log: ${msg.type()} - ${text}`);
            }
        });

        let start = Date.now();
        console.log(`START_TIME: ${start} vu=${vuId}`);
        frameLoadingTime.add(0);
        pageLoadingTime.add(0);
        await page.goto(wopiHost);

        pageLoadingTime.add(Date.now() - start);

        await setWopiClientAndFile(page, wopiUrl.toString(), 2);
        await watchPostMessages(page, [ "App_LoadingStatus" ]);
        await startCool(page);

        await screenshotPage(page);

        let ready = false;
        do {
            let message = await waitForMessage(page, "App_LoadingStatus");
            console.log(`message2: ${JSON.stringify(message)}`);
            if (message) {
                if (message.Values && message.Values.Status == "Document_Loaded")
                    console.log(`TIMING: ${JSON.stringify({...message.Values, Vu: vuId})}`);
                ready = (message.Values.Status == "Frame_Ready");
            }
        } while (!ready);
        check(ready, {
            "Got Frame Ready": ready => ready
        });
        frameLoadingTime.add(Date.now() - start);
    } catch (error) {
        await screenshotPage(page);
        fail(`Browser iteration failed: ${error}`);
    } finally {
        // Capture cumulative WS byte totals before the tab is closed.
        const bytes = await getNetworkBytes(page);
        if (bytes) console.log(`network bytes: sent=${bytes.sent}, received=${bytes.received} vu=${vuId}`);
        await page.close();
    }

    sleep(1);
}
