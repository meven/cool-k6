/**
 * Browser test: open the Calc sales ledger in the Microsoft format and measure load / edit
 * times. The same content as documents/calc-finance.ods, so the two read as a comparison of
 * the formats rather than of two documents.
 * Single user by default; USERS=3 EDIT=1 runs the multi-user editing variant.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 26);  // documents/calc-finance.xlsx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID); }
export function handleSummary(data) { return makeSummary('calc', 'finance-ooxml')(data); }
