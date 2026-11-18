/**
 * Browser test: the same as cool-browser-writer-scroll-test.js with vector
 * rendering, which draws the document from the primitives the engine sends
 * instead of from tiles: 3 users, each at a zoom of its own, page through it
 * (lib/scroll_probe.js). The suite runs it with 3 users only.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';
import { scrollThrough, SCROLL_METRICS, SCROLL_TIMEOUT_COUNTER } from '../lib/scroll_probe.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 12);  // documents/writer-5mb.odt

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () {
    await runDocScenario(FILE_ID, { afterLoad: scrollThrough, edit: false, vector: true });
}
export function handleSummary(data) {
    return makeSummary('writer', 'scroll-vector', undefined, {
        metrics: SCROLL_METRICS,
        counters: { scroll_timeouts: SCROLL_TIMEOUT_COUNTER },
    })(data);
}
