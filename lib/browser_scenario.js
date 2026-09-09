/**
 * Shared browser-test scenario for the per-application document tests.
 *
 * Each test file (one per application and document) is a thin wrapper that
 * calls this with its app name, document label and WOPI file id. The flow:
 *   - open the document in Collabora Online in a real browser (k6/browser),
 *   - measure page_load_ms (page navigation), frame_ready_ms (the editor shell
 *     is up) and document_loaded_ms (the document is open and editable),
 *   - in edit mode, run a few edits and measure edit_ms per edit round,
 *   - write the run's numbers to <OUT_DIR>/metrics.json.
 *
 * A test can also declare a service-level requirement (see the 10 MB document
 * test), which becomes both a k6 threshold and a verdict in metrics.json.
 *
 * Environment:
 *   USERS      number of concurrent users / VUs (default 1)
 *   ITERATIONS document opens in total (default USERS, one per user)
 *   MAXDUR     how long the scenario may take (default "6m")
 *   EDIT       "1" to run edits; defaults on when USERS > 1 (the multi-user case)
 *   EDITS      number of edit rounds per user (default 8)
 *   OUT_DIR    directory for metrics.json (default ".")
 */
import { browser } from 'k6/browser';
import { sleep, check, fail } from 'k6';
import { Trend } from 'k6/metrics';

import { checkWopi } from './wopi_discovery.js';
import {
    setWopiClientAndFile, startCool, watchPostMessages, waitForMessage,
    postMessageWindow, screenshotPage,
} from './test_utils.js';
import { wopiHost, wopiUrl } from '../src/config.js';

const USERS = Number(__ENV['USERS'] || 1);
// A multi-user run is an editing run unless told otherwise.
const EDIT = __ENV['EDIT'] !== undefined ? __ENV['EDIT'] === '1' : USERS > 1;
const EDIT_ROUNDS = Number(__ENV['EDITS'] || 8);
// One open per user, unless asked for more. A requirement written as a
// percentile needs enough opens to have one: at 98% and fewer than 50, the
// 98th percentile is the slowest open there was.
const ITERATIONS = Number(__ENV['ITERATIONS'] || USERS);
const MAX_DURATION = __ENV['MAXDUR'] || '6m';
// Default to the writable results dir k6-wrap prepares (writing to /app,
// the bind mount, is denied for the container user).
const OUT_DIR = (__ENV['OUT_DIR'] || 'results').replace(/\/+$/, '');

// Time from page navigation start until the page responds.
const pageLoad = new Trend('page_load_ms', true);
// Time from navigation start until the editor reports Frame_Ready. That is the
// client shell being up, and it barely moves with the document: a blank
// document and a 46-page one both reach it in about 1.2 s.
const frameReady = new Trend('frame_ready_ms', true);
// Time until the editor reports Document_Loaded, which is the document open and
// editable. This is the one that carries the document's own cost.
const documentLoaded = new Trend('document_loaded_ms', true);
// Time for one edit round (a few UNO commands) to be sent.
const editResp = new Trend('edit_ms', true);
// Browser-side memory: the page's JS heap (Chromium performance.memory), in MB.
const browserHeap = new Trend('browser_heap_mb', true);

/**
 * k6 options for a document scenario: USERS concurrent browser VUs sharing
 * ITERATIONS document opens.
 *
 * A `requirement` becomes the run's k6 threshold, so a run that misses it
 * exits non-zero on its own, without the suite having to read the numbers.
 */
export function scenarioOptions({ requirement } = {}) {
    const options = {
        insecureSkipTLSVerify: true,
        // p(98) is not one of k6's default trend stats and a requirement can be
        // written in it, so ask for it for every test rather than per test.
        summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(98)'],
        scenarios: {
            doc: {
                executor: 'shared-iterations',
                vus: USERS,
                iterations: ITERATIONS,
                maxDuration: MAX_DURATION,
                options: { browser: { type: 'chromium' } },
            },
        },
    };
    if (requirement) {
        options.thresholds = {
            [requirement.metric]:
                [`p(${requirement.percentile})<${requirement.max_ms}`],
        };
    }
    return options;
}

export function setupWopi() {
    checkWopi(wopiHost, wopiUrl);
}

// Generic edits via the postMessage UNO API. They dirty the document and force
// a re-render, which is the work worth measuring under concurrency, and they
// apply across all four applications.
async function editRound(page) {
    await postMessageWindow(page, {
        MessageId: 'Send_UNO_Command', Values: { Command: '.uno:SelectAll' },
    });
    await postMessageWindow(page, {
        MessageId: 'Send_UNO_Command', Values: { Command: '.uno:Bold' },
    });
    await postMessageWindow(page, {
        MessageId: 'Send_UNO_Command', Values: { Command: '.uno:Bold' },
    });
}

// Sample the page's JS heap. Chromium exposes performance.memory; other
// engines do not, so a missing value is simply skipped.
async function sampleBrowserHeap(page) {
    try {
        const bytes = await page.evaluate(
            () => (window.performance && performance.memory)
                ? performance.memory.usedJSHeapSize : 0);
        if (bytes) browserHeap.add(bytes / 1048576);
    } catch (e) {
        // performance.memory unavailable
    }
}

/** Open the document, record load timings, and (in edit mode) run edits. */
export async function runDocScenario(fileId) {
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await context.newPage();
    try {
        const start = Date.now();
        await page.goto(wopiHost.toString());
        pageLoad.add(Date.now() - start);

        await setWopiClientAndFile(page, wopiUrl.toString(), fileId);
        await watchPostMessages(page, ['App_LoadingStatus']);
        await startCool(page);

        let readyAt = null, loadedAt = null;
        do {
            const message = await waitForMessage(page, 'App_LoadingStatus');
            if (!message) continue;
            const status = message.Values.Status;
            if (status === 'Frame_Ready' && readyAt === null) readyAt = Date.now();
            if (status === 'Document_Loaded') loadedAt = Date.now();
        } while (loadedAt === null);
        if (readyAt !== null) frameReady.add(readyAt - start);
        documentLoaded.add(loadedAt - start);
        check(loadedAt, { 'Document_Loaded reached': (v) => v !== null });

        await postMessageWindow(page, { MessageId: 'Host_PostmessageReady' });
        await sampleBrowserHeap(page);

        if (EDIT) {
            for (let i = 0; i < EDIT_ROUNDS; i++) {
                const t0 = Date.now();
                await editRound(page);
                editResp.add(Date.now() - t0);
                sleep(0.5);
                await sampleBrowserHeap(page);
            }
        }
        await sampleBrowserHeap(page);
        await screenshotPage(page);
    } catch (error) {
        await screenshotPage(page);
        fail(`browser iteration failed: ${error}`);
    } finally {
        await page.close();
    }
    sleep(1);
}

/**
 * How the run stands against the requirement it declares: the percentile the
 * requirement names, how many opens it was taken over, and whether it is
 * inside the budget. browser-suite.py fails the run on `ok` being false, so
 * the verdict is reached once, here, and read back by the suite and by CI.
 */
function requirementOutcome(requirement, data) {
    const values = (data.metrics[requirement.metric] || {}).values || {};
    const value = values[`p(${requirement.percentile})`];
    return {
        ...requirement,
        value: value === undefined ? null : value,
        samples: values.count || 0,
        ok: value !== undefined && value <= requirement.max_ms,
    };
}

/**
 * Build the handleSummary function a test exports, writing metrics.json in the
 * shape the dashboard reads. `app` e.g. "writer", `doc` e.g. "blank" | "5mb",
 * `requirement` the service level the test asserts, if it has one.
 */
export function makeSummary(app, doc, requirement) {
    return function (data) {
        const pick = (name) => {
            const v = data.metrics[name] && data.metrics[name].values;
            if (!v) return null;
            return {
                avg: v.avg, min: v.min, max: v.max, med: v.med,
                p90: v['p(90)'], p95: v['p(95)'], p98: v['p(98)'],
                count: v.count,
            };
        };
        const metrics = {
            page_load_ms: pick('page_load_ms'),
            frame_ready_ms: pick('frame_ready_ms'),
            document_loaded_ms: pick('document_loaded_ms'),
            browser_heap_mb: pick('browser_heap_mb'),
        };
        if (EDIT) metrics.edit_ms = pick('edit_ms');
        const verdict = requirement ? requirementOutcome(requirement, data) : null;
        const out = {
            app, doc, scenario: `${app}-${doc}`,
            users: USERS, edit: EDIT,
            // which build was tested + when, so the dashboard can plot over
            // commits/time (COMMIT is passed in by browser-suite.py / k6-wrap)
            commit: __ENV['COMMIT'] || null,
            ts: new Date().toISOString(),
            metrics,
        };
        if (verdict) out.requirement = verdict;
        return {
            [`${OUT_DIR}/metrics.json`]: JSON.stringify(out, null, 2),
            stdout: `\n${app}-${doc}: document_loaded avg ` +
                `${Math.round((metrics.document_loaded_ms || {}).avg || 0)} ms, ` +
                `frame_ready avg ` +
                `${Math.round((metrics.frame_ready_ms || {}).avg || 0)} ms\n`
                + (verdict
                    ? `${app}-${doc}: ${verdict.metric} p(${verdict.percentile}) `
                      + `${Math.round(verdict.value || 0)} ms over ${verdict.samples} `
                      + `open(s), budget ${verdict.max_ms} ms: `
                      + `${verdict.ok ? 'met' : 'MISSED'}\n`
                    : ''),
        };
    };
}
