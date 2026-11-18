/**
 * Browser test: insert a 5 MB image into a text document opened with vector
 * rendering, which draws its pages from the primitives the engine sends
 * instead of from tiles, and assert the same service level as
 * cool-browser-writer-image5mb-test.js, which describes the measurement and the
 * requirement. The two compare the rendering paths.
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
export default async function () {
    await runInsertImageScenario(FILE_ID, IMAGE, { vector: true });
}
export function handleSummary(data) {
    return makeSummary('writer', 'image5mb-vector', REQUIREMENT)(data);
}
