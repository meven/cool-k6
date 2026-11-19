/**
 * Browser test: 3 users open the ~5 MB Writer document, each at a zoom of its own,
 * and page through it (lib/scroll_probe.js).
 * The suite runs it with 3 users only.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';
import { scrollThrough, SCROLL_METRICS, SCROLL_COUNTERS } from '../lib/scroll_probe.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 12);  // documents/writer-5mb.odt

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () {
    await runDocScenario(FILE_ID, { afterLoad: scrollThrough, edit: false });
}
export function handleSummary(data) {
    return makeSummary('writer', 'scroll', undefined, {
        metrics: SCROLL_METRICS,
        counters: SCROLL_COUNTERS,
    })(data);
}
