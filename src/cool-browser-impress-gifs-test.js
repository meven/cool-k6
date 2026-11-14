/**
 * Browser test: open a real document that is hard on Impress, and measure load / edit
 * times. It is a deck of animated GIFs, 19 MB of them, from tdf#142957.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 37);  // documents/ref/impress-gifs.pptx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID); }
export function handleSummary(data) { return makeSummary('impress', 'gifs')(data); }
