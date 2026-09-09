/**
 * Browser test: open the ~10 MB Writer report and assert the service level it
 * has to meet. The document is 46 sections over 47 pages, each with a table
 * and a photograph, behind a table of contents (documents/gen-docs.py
 * --report).
 *
 * The requirement: after clicking a 10 MB office document in cloud storage,
 * editing in the web client must be possible for at least 98% of users within
 * 5 seconds. Document_Loaded is that moment, so the assertion is the 98th
 * percentile of document_loaded_ms against a 5000 ms budget. Frame_Ready is
 * the client shell coming up and reaches the same number for a blank document
 * as for this one, so it answers nothing about a 10 MB document. It is a k6 threshold,
 * so k6 exits non-zero on its own, and it is written to metrics.json, so
 * browser-suite.py fails the run and CI reports which document missed.
 *
 * A 98th percentile needs at least 50 opens to be one, and every open is a
 * real browser, so the requirement run is the one that takes the machine:
 *   USERS=10 ITERATIONS=100 MAXDUR=30m ./k6-run dist/cool-browser-writer-10mb-test.js
 * The suite's own 1u and 3u passes still run this document, and report the
 * slowest of their handful of opens against the same budget.
 */
import { scenarioOptions, setupWopi, runDocScenario, makeSummary }
    from '../lib/browser_scenario.js';

const FILE_ID = Number(__ENV['FILE_ID'] || 22);  // documents/writer-10mb.odt

const REQUIREMENT = { metric: 'document_loaded_ms', percentile: 98, max_ms: 5000 };

export const options = scenarioOptions({ requirement: REQUIREMENT });
export function setup() { setupWopi(); }
export default async function () { await runDocScenario(FILE_ID); }
export function handleSummary(data) {
    return makeSummary('writer', '10mb', REQUIREMENT)(data);
}
