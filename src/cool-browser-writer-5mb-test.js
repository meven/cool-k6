/**
 * Browser test: open the ~5 MB Writer document and measure load / edit times.
 * Single user by default; USERS=3 EDIT=1 runs the multi-user editing variant.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 12);  // documents/writer-5mb.odt

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID); }
export function handleSummary(data) { return makeSummary('writer', '5mb')(data); }
