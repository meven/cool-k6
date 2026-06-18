/**
 * Five concurrent users edit the same Writer document, each typing
 * on a different page.
 *
 * Each VU opens a WebSocket session against the shared document,
 * jumps to the start of the document, sends PageDown key events
 * (vuId - 1) times to land on its page, types a unique sentence,
 * then keeps the connection open briefly so editing overlaps.
 *
 * The document at fileId=2 must already contain at least five
 * paginated pages. PageDown is sent as the COOL key code 1031
 * (KEY_PAGEDOWN).
 */

import { sleep } from 'k6';
import exec from 'k6/execution';

import { checkWopi, getWopiClientUrl, getWopiSrc } from '../../lib/wopi_discovery.js';
import { CoolClientWs } from '../../lib/cool_client_ws.js';
import { wopiHost, wopiUrl } from '../config.js';

const NUM_USERS = 5;
const FILE_ID = 2;
const KEY_PAGEDOWN = 1031;

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: {
        collab: {
            executor: 'per-vu-iterations',
            vus: NUM_USERS,
            iterations: 1,
            maxDuration: '2m',
        },
    },
};

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

function sendKey(client, keyCode, char) {
    client.send(`key type=input char=${char || 0} key=${keyCode}`);
}

function typeText(client, text) {
    for (const ch of text) {
        if (ch === ' ') {
            client.send('key type=input char=32 key=0');
        } else {
            client.send(`textinput id=0 text=${ch}`);
        }
    }
}

export default async function () {
    const vuId = exec.vu.idInTest;
    const pageIndex = vuId - 1;

    const wopiClient = await getWopiClientUrl(wopiUrl);
    const wopiSrc = getWopiSrc(wopiHost, FILE_ID);

    console.log(`vu${vuId}: connecting, target page ${pageIndex + 1}`);
    console.log(`START_TIME: ${Date.now()} vu=${vuId}`);

    let done = false;
    const client = new CoolClientWs(wopiClient, wopiSrc, () => {
        client.openDocument(wopiSrc);

        // Mirror what Map.js does at toolbar init - the browser
        // fetches the font list right after the document loads.
        // Without this request the kit never sends the
        // .uno:CharFontName JSON, so measurements of that payload
        // (legacy vs compact) don't show up in the WS byte counters.
        client.send('commandvalues command=.uno:CharFontName');

        // Let the kit finish opening the document and dispatch the
        // first cursor before we start moving it. Without this brief
        // pause the navigation keys race against the load.
        sleep(2);

        client.send('uno .uno:GoToStartOfDoc');
        sleep(0.2);

        for (let i = 0; i < pageIndex; i++) {
            sendKey(client, KEY_PAGEDOWN, 0);
            sleep(0.1);
        }

        const sentence = `User ${vuId} editing page ${pageIndex + 1}. `;
        typeText(client, sentence);

        // Hold the connection open so the five users overlap in time.
        sleep(5);

        console.log(`network bytes: sent=${client.bytesSent}, received=${client.bytesReceived} vu=${vuId}`);
        client.close();
        done = true;
    });

    // Keep the VU alive until the onopen body actually finishes
    // (otherwise k6 ends the iteration the moment this function
    // returns, tearing down the socket mid-flight).
    const deadline = Date.now() + 30000;
    while (!done && Date.now() < deadline) {
        sleep(0.5);
    }
    if (!done) {
        console.error(`vu${vuId}: workload did not finish within 30s`);
    }
}
