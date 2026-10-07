/**
 * Browser test: duplicating a slide from a presentation that is already fully
 * loaded has to be completed within 1 second for 98% of all users.
 *
 * Opens documents/impress-duplicate.odp (every slide: formatted text, a photo,
 * five drawing objects), waits for it to settle, duplicates the current slide
 * DUPLICATES times and holds the 98th percentile of slide_duplicated_ms, the
 * new slide painted on the canvas, to 1000 ms. See lib/duplicate_slide_probe.js
 * for the signals. The verdict is a k6 threshold and a line in metrics.json
 * that browser-suite.py fails the run on.
 *
 * A 98th percentile needs 50+ samples, and every duplicate grows the deck, so
 * the requirement run takes its samples from many opens:
 *   USERS=5 ITERATIONS=20 MAXDUR=30m ./k6-run dist/cool-browser-impress-duplicate-test.js
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
    await runDocScenario(FILE_ID, { afterLoad: duplicateSlides });
}

export function handleSummary(data) {
    return makeSummary('impress', 'duplicate', REQUIREMENT, {
        metrics: DUPLICATE_METRICS,
        counters: { duplicates_failed: DUPLICATE_FAILED_COUNTER },
    })(data);
}
