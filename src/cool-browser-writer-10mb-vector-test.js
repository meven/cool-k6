/**
 * Browser test: open the ~10 MB Writer report with vector rendering, which
 * draws its pages from the primitives the engine sends instead of from tiles,
 * and assert the same service level as cool-browser-writer-10mb-test.js, which
 * describes the document and the requirement. The two compare the rendering
 * paths.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 22);  // documents/writer-10mb.odt

const REQUIREMENT = { metric: 'document_loaded_ms', percentile: 98, max_ms: 5000 };

export const options = scenarioOptions({ requirement: REQUIREMENT });
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { vector: true }); }
export function handleSummary(data) {
    return makeSummary('writer', '10mb-vector', REQUIREMENT)(data);
}
