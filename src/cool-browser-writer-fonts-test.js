/**
 * Browser test: open a real document that is hard on Writer, and measure load / edit
 * times. It is a trade agreement in Georgian and English, 236 pages and 36000
 * paragraphs in Sylfaen and MS Mincho, fonts that a Linux server lacks, from tdf#100139.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 40);  // documents/ref/writer-fonts.docx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('writer', 'fonts')(data); }
