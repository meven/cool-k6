/**
 * Browser test: insert a 5 MB image from the local file system into a text
 * document that is already open, and assert the service level it has to meet.
 *
 * The requirement: after inserting a 5 MB image from the local file system into
 * a text document that has already been fully loaded, the image must be
 * displayed within 2 seconds for at least 98% of users. image_displayed_ms is
 * that wait, measured in the page from the change event of the file input
 * behind Insert > Image, which is the moment the client holds the picked file,
 * to the first frame in which the document canvas shows it. It covers the
 * upload of the file, the insert in core, and the tile that comes back. The
 * assertion is the 98th percentile of image_displayed_ms against a 2000 ms
 * budget. It is a k6 threshold, so k6 exits non-zero on its own, and it is
 * written to metrics.json, so browser-suite.py fails the run and CI reports it.
 *
 * The document opened is blank.odt, so what is measured is the insert and not
 * the document: a document that is already loaded is the requirement's starting
 * point, and the scenario waits for the page to stop changing before inserting.
 *
 * The image is read from the local file system by the test itself and handed to
 * the file input, which is the path a person takes through Insert > Image.
 * Generate it with the rest of the corpus:
 *   python3 documents/gen-docs.py            # writes documents/image-5mb.png
 * IMAGE overrides which file is inserted, FILE_ID which document it goes into.
 *
 * A 98th percentile needs at least 50 inserts to be one, and every insert is a
 * real browser, so the requirement run is the one that takes the machine:
 *   USERS=10 ITERATIONS=100 MAXDUR=30m ./k6-run dist/cool-browser-writer-image5mb-test.js
 * The suite's own 1u and 3u passes still run this test, and report the slowest
 * of their handful of inserts against the same budget.
 */
import encoding from 'k6/encoding';

import { scenarioOptions, setupWopi, runInsertImageScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 11);  // documents/blank.odt
// Relative to this script, which k6 runs from dist/, so the corpus beside it.
const IMAGE_PATH = __ENV['IMAGE'] || '../documents/image-5mb.png';

const MIME_TYPES = {
    png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
    gif: 'image/gif', webp: 'image/webp',
};

const name = IMAGE_PATH.split('/').pop();
const IMAGE = {
    name: name,
    // mimeType, not mimetype: k6 reads the descriptor by that name and gives
    // the File it builds in the page an empty type otherwise.
    mimeType: MIME_TYPES[name.split('.').pop().toLowerCase()] || 'image/png',
    // open() reads from the file system of the machine k6 runs on, in the init
    // context, and the file input takes the bytes base64-encoded.
    buffer: encoding.b64encode(open(IMAGE_PATH, 'b')),
};

const REQUIREMENT = { metric: 'image_displayed_ms', percentile: 98, max_ms: 2000 };

export const options = scenarioOptions({ requirement: REQUIREMENT });
export function setup() { setupWopi(); }
export default async function () { await runInsertImageScenario(FILE_ID, IMAGE); }
export function handleSummary(data) {
    return makeSummary('writer', 'image5mb', REQUIREMENT)(data);
}
