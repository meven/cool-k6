/**
 * Browser test: the slide duplication of cool-browser-impress-duplicate-test.js,
 * on the same deck and with the same requirement, with vector rendering, which
 * draws the slides from the primitives the engine sends instead of from tiles.
 * The new slide counts as painted once the vector content section can draw it,
 * see lib/duplicate_slide_probe.js.
 */
import { sleep, fail } from 'k6';

import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';
import {
    findCoolFrame, armDuplicateProbe, settleForDuplicate, duplicateSlide,
    DUPLICATES, DUPLICATE_METRICS, DUPLICATE_FAILED_COUNTER,
} from '../lib/duplicate_slide_probe.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 23);  // documents/impress-duplicate.odp

const REQUIREMENT = { metric: 'slide_duplicated_ms', percentile: 98, max_ms: 1000 };

export const options = scenarioOptions({ requirement: REQUIREMENT });
export function setup() { setupWopi(); }

async function duplicateSlides(page) {
    const frame = await findCoolFrame(page);
    if (!frame) fail('no Collabora Online frame');
    if (!(await armDuplicateProbe(frame))) fail('slide duplication cannot be measured in this client, see the warning above');
    for (let i = 0; i < DUPLICATES; i++) {
        await settleForDuplicate(frame);   // "already fully loaded", before every sample
        await duplicateSlide(frame, page);
        sleep(0.5);
    }
}

export default async function () {
    await runDocScenario(FILE_ID, { afterLoad: duplicateSlides, vector: true });
}

export function handleSummary(data) {
    return makeSummary('impress', 'duplicate-vector', REQUIREMENT, {
        metrics: DUPLICATE_METRICS,
        counters: { duplicates_failed: DUPLICATE_FAILED_COUNTER },
    })(data);
}
