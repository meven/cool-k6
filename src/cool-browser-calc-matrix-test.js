/**
 * Browser test: open a real document that is hard on Calc, and measure load / edit
 * times. It is the accessory compatibility matrix of Lenovo, 31 sheets of
 * dense tables, which Calc is slow to open, from tdf#154525.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 35);  // documents/ref/calc-matrix.xlsx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('calc', 'matrix')(data); }
