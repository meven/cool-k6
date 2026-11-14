/**
 * Browser test: open a real document that is hard on Calc, and measure load / edit
 * times. It is one sheet of 6 million cells, 500000 of them formulas, from tdf#148010.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 34);  // documents/ref/calc-formulas.ods

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('calc', 'formulas')(data); }
