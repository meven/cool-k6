/**
 * Browser test: open a real document that is hard on Writer, and measure load / edit
 * times. It is a commentary in Persian, 960 pages of right-to-left text in 910000 runs
 * of text, from tdf#100967.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 30);  // documents/ref/writer-rtl-book.docx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('writer', 'rtl-book')(data); }
