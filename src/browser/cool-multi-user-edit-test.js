/**
 * Several real Chromium users join the same Writer document and make
 * heavy collaborative changes: typing paragraphs, a bold heading,
 * inserting a filled table, applying formatting, and scrolling the
 * view up and down. Each user does several rounds so the document
 * keeps growing while the sessions overlap in time.
 *
 * The number of users is the virtual user count, taken from the
 * environment so it can be varied without k6 --vus (which would drop
 * the browser scenario). See browserScenario in config.js.
 *
 *   COOL_K6_VUS         number of concurrent browser users. Default 1.
 *   COOL_K6_ROUNDS      edit rounds each user performs. Default 3.
 *   COOL_K6_HOLD_SEC    seconds to keep the editor open at the end so
 *                       the users overlap. Default 5.
 *   COOL_K6_FILE_ID     document id to open. Default 2.
 *
 * file id 2 (writer-large.fodt) has enough pages for the per-user
 * navigation and scrolling.
 */

import exec from 'k6/execution';
import { browser } from 'k6/browser';
import { sleep, fail } from 'k6';
import { Trend } from 'k6/metrics';

import { getWopiClientUrl, getWopiSrc, checkWopi } from '../../lib/wopi_discovery.js';
import { postMessageWindow, screenshotPage, getNetworkBytes } from '../../lib/test_utils.js';
import { browserScenario, wopiHost, wopiUrl } from '../config.js';

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: browserScenario(),
};

const browserOptions = {
    ignoreHTTPSErrors: true,
};

const FILE_ID = parseInt(__ENV.COOL_K6_FILE_ID || '2', 10);
const ROUNDS = parseInt(__ENV.COOL_K6_ROUNDS || '3', 10);
const HOLD_SEC = parseInt(__ENV.COOL_K6_HOLD_SEC || '5', 10);

// Time from the initial request until the editor canvas is visible.
const frameLoadingTime = new Trend('frame_loading_time', true);
// Time spent in the editing rounds, per user.
const editTime = new Trend('edit_time', true);

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

// Send a UNO command to the editor through the host postMessage API.
// args is the UNO parameter JSON as a string, or omitted for no args.
async function sendUno(page, command, args) {
    const values = args ? { Command: command, Args: args } : { Command: command };
    await postMessageWindow(page, { MessageId: 'Send_UNO_Command', Values: values });
}

// Type a line of text and start a new paragraph.
async function typeLine(page, text) {
    await page.keyboard.type(text);
    await page.keyboard.press('Enter');
}

async function editRound(page, vuId, round) {
    // A bold heading line for this round.
    await page.keyboard.press('Control+b');
    await page.keyboard.type(`Section ${round} by user ${vuId}`);
    await page.keyboard.press('Control+b');
    await page.keyboard.press('Enter');

    // A couple of sentences of body text.
    await typeLine(page,
        `User ${vuId}, round ${round}. The quick brown fox jumps over the lazy dog. ` +
        'This paragraph forces the layout engine to reflow and re-render tiles. ');

    // Insert a 3x3 table and fill every cell, tabbing between them.
    // The last cell gets no trailing Tab so no extra row is added.
    await sendUno(page, '.uno:InsertTable',
        '{"Columns":{"type":"long","value":3},"Rows":{"type":"long","value":3}}');
    sleep(0.5);
    for (let cell = 0; cell < 9; cell++) {
        await page.keyboard.type(`u${vuId}c${cell + 1}`);
        if (cell < 8) {
            await page.keyboard.press('Tab');
        }
    }

    // Leave the table and add an emphasised closing line.
    await page.keyboard.press('Control+End');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Control+i');
    await typeLine(page, `End of round ${round}, user ${vuId}.`);
    await page.keyboard.press('Control+i');

    // Scroll the view down through the new content and back up. This
    // exercises tile fetching and rendering on the scroll path.
    for (let i = 0; i < 4; i++) {
        await page.keyboard.press('PageDown');
        sleep(0.2);
    }
    await page.mouse.move(400, 300);
    await page.mouse.wheel(0, 1600);
    sleep(0.3);
    await page.mouse.wheel(0, -1600);
    for (let i = 0; i < 2; i++) {
        await page.keyboard.press('PageUp');
        sleep(0.2);
    }

    // Let the kit catch up before the next round.
    sleep(1);
}

export default async function () {
    const vuId = exec.vu.idInTest;

    const wopiClient = await getWopiClientUrl(wopiUrl);
    const wopiSrc = getWopiSrc(wopiHost, FILE_ID);
    const wopiClientUrl = new URL(wopiClient);
    wopiClientUrl.searchParams.set('WOPISrc', wopiSrc);

    const context = await browser.newContext(browserOptions);
    const page = await context.newPage();
    try {
        const start = Date.now();
        console.log(`START_TIME: ${start} vu=${vuId}`);
        await page.goto(wopiClientUrl.toString());

        // The editor canvas appearing means the UI is up.
        await page.locator('canvas#document-canvas').waitFor({ state: 'visible' });
        frameLoadingTime.add(Date.now() - start);

        // Enable the host postMessage API and let the document settle.
        await postMessageWindow(page, { MessageId: 'Host_PostmessageReady' });
        sleep(3);

        // Focus the document so keyboard input reaches it, then move
        // this user to its own region to spread the editing out.
        await page.locator('canvas#document-canvas').click();
        await page.keyboard.press('Control+Home');
        for (let i = 0; i < vuId - 1; i++) {
            await page.keyboard.press('PageDown');
            sleep(0.1);
        }

        const editStart = Date.now();
        for (let round = 1; round <= ROUNDS; round++) {
            console.log(`vu${vuId}: edit round ${round}/${ROUNDS}`);
            await editRound(page, vuId, round);
        }
        editTime.add(Date.now() - editStart);

        // Keep the editor open so the users overlap in time.
        sleep(HOLD_SEC);
    } catch (error) {
        screenshotPage(page);
        fail(`Browser iteration failed: ${error}`);
    } finally {
        const bytes = await getNetworkBytes(page);
        if (bytes) {
            console.log(`network bytes: sent=${bytes.sent}, received=${bytes.received} vu=${vuId}`);
        }
        await page.close();
    }

    sleep(1);
}
