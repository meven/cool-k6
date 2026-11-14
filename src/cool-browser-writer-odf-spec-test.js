/**
 * Browser test: open a real document that is hard on Writer, and measure load / edit
 * times. It is OASIS OpenDocument 1.2 Part 1: 846 pages and 19000 paragraphs, with
 * cross-references on every page, a table of contents and 27 tables.
 * documents/fetch-reference-docs.py downloads it. Single user by default; USERS=3 EDIT=1 runs the
 * multi-user editing variant, in which each user types a word and Enter at the cursor.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 31);  // documents/ref/writer-odf-spec.odt

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { typing: true }); }
export function handleSummary(data) { return makeSummary('writer', 'odf-spec')(data); }
