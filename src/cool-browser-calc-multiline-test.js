/**
 * Browser test: open a real document that is hard on Calc, and measure load / edit
 * times. It has 56000 cells of text over several lines, which Calc is slow to
 * open, from tdf#166568.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 44);  // documents/ref/calc-multiline.xlsx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('calc', 'multiline')(data); }
