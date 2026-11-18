/**
 * Browser test: open the ~5 MB Writer document in the Microsoft format with vector rendering, which
 * draws its pages from the primitives the engine sends instead of from tiles.
 * The same document as cool-browser-writer-ooxml-test.js, so the two compare the
 * rendering paths.
 * Single user by default; USERS=3 EDIT=1 runs the multi-user editing variant.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 19);  // documents/writer-5mb.docx

export const options = scenarioOptions();
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID, { vector: true }); }
export function handleSummary(data) { return makeSummary('writer', 'ooxml-vector')(data); }
