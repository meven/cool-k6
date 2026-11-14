/**
 * Browser test: open a real document that is hard on Writer, and measure load / edit
 * times. It is the LibreOffice Getting Started Guide 5.0, from tdf#135116: 531 pages with
 * 659 images, 270 tables and tracked changes.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 32);  // documents/ref/writer-guide.odt

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('writer', 'guide')(data); }
