/**
 * Browser test: open a real document that is hard on Writer, and measure load / edit
 * times. It is a library catalogue in Arabic, one right-to-left table over 409
 * pages, from tdf#151718.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 41);  // documents/ref/writer-big-table.docx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('writer', 'big-table')(data); }
