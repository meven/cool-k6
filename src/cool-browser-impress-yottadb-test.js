/**
 * Browser test: open a real document that is hard on Impress, and measure load / edit
 * times. It is 29 slides with 15 MB of images, which load slower in 26.2 than in 25.8,
 * from tdf#171645.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 38);  // documents/ref/impress-yottadb.odp

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID); }
export function handleSummary(data) { return makeSummary('impress', 'yottadb')(data); }
