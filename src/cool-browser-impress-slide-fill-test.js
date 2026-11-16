/**
 * Browser test: open a real document that is hard on Impress, and measure load / edit
 * times. It has 2 slides on a master filled with a bitmap, where moving to the
 * second slide took a minute, from tdf#132955.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 45);  // documents/ref/impress-slide-fill.pptx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID); }
export function handleSummary(data) { return makeSummary('impress', 'slide-fill')(data); }
