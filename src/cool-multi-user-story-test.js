/**
 * Realistic collaborative editing load test.
 *
 * A blank Writer document (fileId=4, documents/blank.fodt) is opened by up
 * to three users who join one after another (1, then 2, then 3) and edit
 * concurrently, each committing a change roughly every 20 ms: characters,
 * paragraph styles (Title / Heading 1 / Text Body) and headings. Together
 * they write the tale of The Pied Piper of Hamelin and finish with a short
 * index of the chapter titles. All text is set in a serif that suits the
 * period of the tale (Times-metric-compatible Liberation Serif by default;
 * override with the STORY_FONT env var, e.g. "Linux Libertine G").
 *
 * Coordination: each user owns a disjoint, ordered block and appends it at
 * the end of the document. Because they come online staggered, the blocks
 * land in order. Some interleaving at the hand-off between two active users
 * is expected: that overlap is exactly the concurrent-editing traffic this
 * test exists to exercise.
 *
 * This speaks the same WebSocket protocol the browser client sends
 * (textinput, key, uno .uno:...), so it reproduces browser editing traffic
 * without driving a real browser.
 */

import { sleep } from 'k6';
import { vu } from 'k6/execution';

import { checkWopi, getWopiClientUrl, getWopiSrc } from '../lib/wopi_discovery.js';
import { CoolClientWs } from '../lib/cool_client_ws.js';
import { wopiHost, wopiUrl } from './config.js';

const FILE_ID = __ENV['STORY_FILE_ID'] || 4;
const STORY_FONT = __ENV['STORY_FONT'] || 'Liberation Serif';
const NUM_USERS = 3;

const BODY_SIZE = 12;
const HEADING_SIZE = 16;
const TITLE_SIZE = 28;

// TYPE_STEP_MS: pause between each typed character (default 20 ms, a realistic
// typing cadence; set 0 to type flat-out). Structural changes - paragraph
// style, font, new paragraph - are single actions, sent without this delay.
const CHAR_STEP = (Number(__ENV['TYPE_STEP_MS'] ?? 20)) / 1000;
// STORY_REPEAT: how many times each user re-types its block. 1 gives the plain
// tale; raise it to keep the kit busy for a profiling capture. The index is
// still written once, at the end.
const REPEAT = Math.max(1, Number(__ENV['STORY_REPEAT'] ?? 1));
const RAMP_INTERVAL = 15;   // seconds between one user joining and the next

// LibreOffice awt key code for Return (com.sun.star.awt.Key.RETURN).
const KEY_RETURN = 1280;

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: {
        collab: {
            executor: 'per-vu-iterations',
            vus: NUM_USERS,
            iterations: 1,
            maxDuration: '5m',
        },
    },
};

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

// The tale, split into ordered blocks. The key is the user (VU) that writes
// the block. User 1 opens the blank document and lays down the title and the
// first chapters; users 2 and 3 join in turn and append the rest, user 3
// closing with the index.
const STORY = {
    1: [
        { style: 'Title', size: TITLE_SIZE, text: 'The Pied Piper of Hamelin' },
        { style: 'Text Body', text: 'A retelling of the old tale of the town of Hamelin, its plague of rats, and the stranger who came to rid the town of them.' },
        { style: 'Heading 1', text: 'The Rats of Hamelin' },
        { style: 'Text Body', text: 'In the year 1284 the town of Hamelin was overrun by rats. They fought the dogs and killed the cats, and the townsfolk despaired of ever being rid of them.' },
        { style: 'Heading 1', text: 'A Stranger Arrives' },
        { style: 'Text Body', text: 'One day a piper in a coat of red and yellow stood before the council and promised, for a purse of gold, to draw every last rat out of the town.' },
    ],
    2: [
        { style: 'Heading 1', text: 'The Bargain' },
        { style: 'Text Body', text: 'The mayor agreed at once, and the piper raised his pipe to his lips. At the first shrill note the rats came tumbling from the houses and followed him into the street.' },
        { style: 'Heading 1', text: 'The Broken Promise' },
        { style: 'Text Body', text: 'He led them down to the river Weser, where they drowned. But when he returned for his gold the mayor only laughed and offered him a fraction of the promised sum.' },
    ],
    3: [
        { style: 'Heading 1', text: 'The Piper Returns' },
        { style: 'Text Body', text: 'The piper said nothing. He walked back into the street and played a different tune, and this time it was the children who followed. They danced after him to the mountain and were never seen again.' },
    ],
};

// The index of chapter titles, written once at the very end by the last user to
// join, so it survives STORY_REPEAT instead of being duplicated in every pass.
const INDEX_AUTHOR = NUM_USERS;
const INDEX = [
    { style: 'Heading 1', text: 'Index' },
    { style: 'Text Body', text: 'The Rats of Hamelin' },
    { style: 'Text Body', text: 'A Stranger Arrives' },
    { style: 'Text Body', text: 'The Bargain' },
    { style: 'Text Body', text: 'The Broken Promise' },
    { style: 'Text Body', text: 'The Piper Returns' },
];

// Type a run of text the way the browser does: space arrives as a key event,
// every other character as a text-input event, pausing CHAR_STEP between each
// character.
function typeText(client, text) {
    let sinceYield = 0;
    for (const ch of text) {
        if (ch === ' ') {
            client.send('key type=input char=32 key=0');
        } else {
            client.send(`textinput id=0 text=${ch}`);
        }
        // Pace the stream. TYPE_STEP_MS between characters is the realistic
        // cadence; but a zero-delay flood never yields to k6's event loop, so
        // the queued frames don't flush and the socket stalls after one burst.
        // Always yield briefly at least every few dozen characters.
        if (CHAR_STEP > 0) {
            sleep(CHAR_STEP);
        } else if (++sinceYield >= 32) {
            sinceYield = 0;
            sleep(0.003);
        }
    }
}

// Structural edits (a new paragraph, a paragraph style, a font) are single
// actions, sent without the per-character delay.
function newParagraph(client) {
    client.send(`key type=input char=13 key=${KEY_RETURN}`);
}

function applyStyle(client, style) {
    client.send(`uno .uno:StyleApply?Style:string=${style}&FamilyName:string=ParagraphStyles`);
}

function applyFont(client, font) {
    client.send(`uno .uno:CharFontName {"CharFontName.FamilyName": {"type": "string", "value": "${font}"}}`);
}

function applyFontSize(client, size) {
    client.send(`uno .uno:FontHeight {"FontHeight.Height": {"type": "float", "value": "${size}"}}`);
}

// Write one paragraph: start a fresh paragraph (unless this is the very first
// paragraph of the whole document), give it its paragraph style and the story
// font, then type its text.
function writeParagraph(client, para, isFirstInDocument) {
    if (!isFirstInDocument) {
        newParagraph(client);
    }
    applyStyle(client, para.style);
    applyFont(client, STORY_FONT);
    applyFontSize(client, para.size || (para.style === 'Text Body' ? BODY_SIZE : HEADING_SIZE));
    typeText(client, para.text);
}

export default async function () {
    const vuId = vu.idInTest;
    const block = STORY[vuId];
    if (!block) {
        return;
    }

    // Join one after another: user 1 at the start, user 2 about RAMP_INTERVAL
    // later, user 3 later still. Their editing then overlaps in time.
    sleep((vuId - 1) * RAMP_INTERVAL);

    const wopiClient = await getWopiClientUrl(wopiUrl);
    const wopiSrc = getWopiSrc(wopiHost, FILE_ID);
    console.log(`vu${vuId}: joining — TYPE_STEP_MS=${__ENV['TYPE_STEP_MS'] ?? '(unset->20)'} `
        + `STORY_REPEAT=${REPEAT}; writing ${block.length} paragraphs x ${REPEAT}`);

    let done = false;
    const client = new CoolClientWs(wopiClient, wopiSrc, () => {
        client.openDocument(wopiSrc);
        // Mirror the browser: it fetches the font list right after load.
        client.send('commandvalues command=.uno:CharFontName');
        // Let the kit finish opening the document and place the first cursor.
        sleep(2);

        // Append at the current end of the shared document.
        client.send('uno .uno:GoToEndOfDoc');
        sleep(0.3);

        for (let r = 0; r < REPEAT; r++) {
            block.forEach((para, i) => {
                // Only user 1's very first paragraph reuses the blank document's
                // initial empty paragraph; everything else (and every later
                // repeat) starts a fresh one.
                writeParagraph(client, para, vuId === 1 && i === 0 && r === 0);
            });
        }

        // The last user closes the document with the index, once.
        if (vuId === INDEX_AUTHOR) {
            INDEX.forEach((para) => writeParagraph(client, para, false));
        }

        // Stay a moment so the users' edits overlap before leaving.
        sleep(3);
        client.close();
        done = true;
    });
    // Drain incoming server traffic (tiles, cursors) so it does not pile up.
    client.socket.onmessage = () => {};

    // Keep the VU alive until the socket work above finishes; k6 tears the
    // iteration down as soon as this function returns otherwise.
    const deadline = Date.now() + 280000;
    while (!done && Date.now() < deadline) {
        sleep(0.5);
    }
    if (!done) {
        console.error(`vu${vuId}: workload did not finish in time`);
    }
}
