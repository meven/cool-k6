/**
 * Shared browser-test scenario for the per-application document tests.
 *
 * Each test file (one per application and document) is a thin wrapper that
 * calls this with its app name, document label and WOPI file id. The flow:
 *   - open the document in Collabora Online in a real browser (k6/browser),
 *   - measure page_load_ms (page navigation), frame_ready_ms (the editor shell
 *     is up) and document_loaded_ms (the document is open and editable),
 *   - measure what the user sees on the way there, timed from the opening
 *     click (see ./feedback_probe.js),
 *   - in edit mode, run a few edits and measure edit_ms per edit round,
 *   - write the run's numbers to <OUT_DIR>/metrics.json.
 *
 * A test can also declare a service-level requirement (see the 10 MB document
 * test), which becomes both a k6 threshold and a verdict in metrics.json, and
 * it can run its own interaction once the document is open (see the slide
 * duplication test), recording trends of its own that makeSummary writes out
 * beside the load timings.
 *
 * Environment:
 *   USERS      number of concurrent users / VUs (default 1)
 *   ITERATIONS document opens in total (default USERS, one per user)
 *   MAXDUR     how long the scenario may take (default "6m")
 *   EDIT       "1" to run edits; defaults on when USERS > 1 (the multi-user case)
 *   EDITS      number of edit rounds per user (default 8)
 *   OUT_DIR    directory for metrics.json (default ".")
 *
 * The feedback signals, and the FEEDBACK_SLA_MS that can enforce them, live in
 * ./feedback_probe.js, which documents both.
 */
import { browser } from 'k6/browser';
import { sleep, check, fail } from 'k6';
import { Counter, Trend } from 'k6/metrics';

import { checkWopi, getWopiClientUrl, getWopiSrc } from './wopi_discovery.js';
import {
    setWopiClientAndFile, startCool, watchPostMessages, waitForMessage,
    postMessageWindow, screenshotPage, findCoolFrame,
} from './test_utils.js';
import {
    installFeedbackProbe, recordFeedback, feedbackThresholds,
    FEEDBACK_METRICS, LOADER_SHOWN_COUNTER,
} from './feedback_probe.js';
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
// Document_Loaded timed from the click, as the feedback marks are, so the two
// can be read as one timeline.
const loadedAfterClick = new Trend('doc_loaded_click_ms', true);
// Time for one edit round (a few UNO commands) to be sent.
const editResp = new Trend('edit_ms', true);
// Browser-side memory: the page's JS heap (Chromium performance.memory), in MB.
const browserHeap = new Trend('browser_heap_mb', true);
// Time from the client receiving a picked image file until the document shows
// it: the upload, the insert in core, and the tile that comes back.
const imageDisplayed = new Trend('image_displayed_ms', true);

// A k6 trend summary carries no sample count, and an empty trend still reports
// every statistic as zero, so a requirement read from one alone is met by a run
// that measured nothing. Count the samples beside the trend: the metric a
// requirement names gets a counter of the same stem, and requirementOutcome
// refuses to call a budget met over none of them.
const requirementSamples = {
    document_loaded_ms: new Counter('document_loaded_samples'),
    image_displayed_ms: new Counter('image_displayed_samples'),
    slide_duplicated_ms: new Counter('slide_duplicated_samples'),
};

/** Add a sample to a trend, counting it when a requirement is read from it. */
export function record(trend, name, value) {
    trend.add(value);
    const counter = requirementSamples[name];
    if (counter) counter.add(1);
}

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
        // count is not reported unless asked for either, and metrics.json
        // carries it for every trend.
        summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(98)', 'count'],
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
    // The document's requirement and the feedback budget are independent, and
    // a run can be held to both.
    const thresholds = feedbackThresholds() || {};
    if (requirement) {
        thresholds[requirement.metric] =
            [`p(${requirement.percentile})<${requirement.max_ms}`];
    }
    if (Object.keys(thresholds).length > 0) options.thresholds = thresholds;
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

// A word and Enter at the cursor, sent as the client sends what the user types. Selecting all of
// a book of 960 pages or a sheet of millions of cells and making it bold keeps the kit busy long
// after the test ended, so a document of that size is edited where the cursor is.
async function typeRound(page) {
    const frame = await findCoolFrame(page);
    if (!frame) throw new Error('no Collabora Online frame to type in');
    await frame.evaluate(() => {
        app.socket.sendMessage('textinput id=0 text=word');
        app.socket.sendMessage('key type=input char=13 key=1280');
        app.socket.sendMessage('key type=up char=0 key=1280');
    });
}

// Sample the page's JS heap. Chromium exposes performance.memory; other
// engines do not, so a missing value is simply skipped. A full collection first,
// where Chromium runs with --expose-gc, so that the figure is the memory that the
// page keeps alive and not the garbage that V8 has not collected yet.
async function sampleBrowserHeap(page) {
    try {
        const bytes = await page.evaluate(() => {
            if (typeof window.gc === 'function') window.gc();
            return (window.performance && performance.memory)
                ? performance.memory.usedJSHeapSize : 0;
        });
        if (bytes) browserHeap.add(bytes / 1048576);
    } catch (e) {
        // performance.memory unavailable
    }
}

/** Fail the iteration unless the client in the page draws from vector primitives. */
async function requireVectorRendering(page) {
    const frame = await findCoolFrame(page);
    // A braced body: under k6 the expression-bodied arrow failed in the page
    // with "SyntaxError: Unexpected end of input".
    const on = frame ? await frame.evaluate(() => {
        const rm = window.RenderManager;
        return !!(rm && rm.isVectorRendering && rm.isVectorRendering());
    }) : false;
    if (!check(on, { 'vector rendering on': (v) => v })) {
        fail('the client did not switch to vector rendering: ' +
             (frame ? 'RenderManager reports tiles' : 'no Collabora Online frame'));
    }
}

/**
 * Open the document, record load timings, and (in edit mode) run edits.
 *
 * `afterLoad(page)` runs once the document is loaded and its feedback signals
 * are read, before the generic edit rounds: the place for a test's own
 * interaction. An error thrown there fails the iteration like any other.
 *
 * `vector` opens the document with vector rendering, which Impress and Draw
 * draw from the primitives the engine sends instead of from tiles. The
 * iteration fails when the client did not switch to it, so that such a run
 * never measures the tiles under a vector name.
 *
 * `typing` makes each edit round a word and Enter at the cursor instead of
 * making the whole document bold, for a document too large to format whole.
 */
export async function runDocScenario(fileId, { afterLoad, vector = false, typing = false } = {}) {
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    // Before the context has a page, so the probe is in place for the iframe.
    await installFeedbackProbe(context);
    const page = await context.newPage();
    try {
        const start = Date.now();
        const host = new URL(wopiHost.toString());
        if (vector) host.searchParams.set('vector', 'true');
        await page.goto(host.toString());
        pageLoad.add(Date.now() - start);

        await setWopiClientAndFile(page, wopiUrl.toString(), fileId);
        await watchPostMessages(page, ['App_LoadingStatus']);

        // The feedback signals are timed from here: clicking load is the user
        // action the 0.3 s budget is about. frame_ready_ms keeps timing from
        // navigation instead, so the two are not directly comparable.
        const clickedAt = Date.now();
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
        record(documentLoaded, 'document_loaded_ms', loadedAt - start);
        loadedAfterClick.add(loadedAt - clickedAt);
        check(loadedAt, { 'Document_Loaded reached': (v) => v !== null });
        if (vector) await requireVectorRendering(page);

        await postMessageWindow(page, { MessageId: 'Host_PostmessageReady' });
        await sampleBrowserHeap(page);

        // Read the probe only now: reading late cannot move a timing, and it
        // gives signals landing just after Frame_Ready the chance to be there.
        await recordFeedback(page, clickedAt);

        if (afterLoad) await afterLoad(page);

        if (EDIT) {
            for (let i = 0; i < EDIT_ROUNDS; i++) {
                const t0 = Date.now();
                await (typing ? typeRound(page) : editRound(page));
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
 * In the page: watch the document canvas for the inserted image.
 *
 * The canvas is sampled into a small offscreen one, so a frame costs a scaled
 * blit and a few thousand bytes rather than the megabytes the document canvas
 * holds. A cell counts as changed when its colour moves by more than `delta`,
 * and the image counts as displayed once `fraction` of the cells have moved,
 * which is far more than a blinking text cursor touches.
 *
 * The clock starts at the change event of the file input, which is the moment
 * the client has the file the user picked, and stops at the first animation
 * frame that shows the change. Both times are taken in the page, so the wait
 * the test does around this costs the measurement nothing.
 */
function watchForInsertedImage(opts) {
    const canvas = document.getElementById('document-canvas');
    const size = opts.size;
    const cells = size * size;
    const small = document.createElement('canvas');
    small.width = size;
    small.height = size;
    const ctx = small.getContext('2d', { willReadFrequently: true });
    const snapshot = function () {
        ctx.drawImage(canvas, 0, 0, size, size);
        return ctx.getImageData(0, 0, size, size).data;
    };

    const before = snapshot();
    const watch = { startedAt: null, displayedMs: null, changed: 0 };
    window.__coolImageWatch = watch;

    document.getElementById('insertgraphic').addEventListener('change', function () {
        watch.startedAt = Date.now();
    });

    const changedCells = function (now) {
        let changed = 0;
        for (let i = 0; i < now.length; i += 4) {
            const moved = Math.abs(now[i] - before[i])
                + Math.abs(now[i + 1] - before[i + 1])
                + Math.abs(now[i + 2] - before[i + 2]);
            if (moved > opts.delta) changed++;
        }
        return changed;
    };

    const tick = function () {
        if (watch.startedAt !== null) {
            watch.changed = changedCells(snapshot());
            if (watch.changed >= cells * opts.fraction) {
                watch.displayedMs = Date.now() - watch.startedAt;
                return;
            }
        }
        requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
}

/** Poll an expression in the page until it answers something other than null. */
async function waitInPage(page, fn, arg, timeoutMs, what) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        const value = await page.evaluate(fn, arg);
        if (value !== null && value !== undefined && value !== false) return value;
        if (Date.now() > deadline) return null;
        sleep(0.1);
    }
}

/**
 * Wait until the document is open and the page has stopped changing: the tile
 * layer exists, the canvas is up, and two samples taken 400 ms apart are the
 * same. The requirement is about a document that is already fully loaded, so
 * the insert must not start while tiles are still arriving.
 */
async function waitForDocumentStill(page) {
    const ready = await waitInPage(page, () => {
        const app = window.app;
        return !!(app && app.map && app.map._docLayer
            && document.getElementById('document-canvas'));
    }, null, 120000, 'the document to open');
    if (!ready) fail('the document never opened');

    const sample = () => {
        const canvas = document.getElementById('document-canvas');
        const small = document.createElement('canvas');
        small.width = 32;
        small.height = 32;
        const ctx = small.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(canvas, 0, 0, 32, 32);
        return Array.from(ctx.getImageData(0, 0, 32, 32).data).join(',');
    };
    const deadline = Date.now() + 120000;
    let previous = await page.evaluate(sample);
    for (;;) {
        sleep(0.4);
        const now = await page.evaluate(sample);
        if (now === previous) return;
        if (Date.now() > deadline) fail('the document never settled');
        previous = now;
    }
}

/**
 * Record in the page when the server last sent the client a message. The
 * client reads its socket through onmessage, so the hook wraps that handler.
 * Returns false when the page has no socket to watch.
 */
function watchServerMessages() {
    const socket = window.app && window.app.socket && window.app.socket.socket;
    if (!socket || typeof socket.onmessage !== 'function') return false;
    const handler = socket.onmessage;
    window.__coolLastMessageAt = Date.now();
    socket.onmessage = function (event) {
        window.__coolLastMessageAt = Date.now();
        return handler.call(this, event);
    };
    return true;
}

/**
 * Wait until the server has sent the client nothing for @p quietMs, which is
 * when the kit has finished the work that the last action started: the tiles,
 * the selection and the state updates. A person stays in the document that
 * long; a page closed sooner cuts that work at a point that varies from run
 * to run, and the instruction counts of the kit with it.
 */
async function waitForServerQuiet(page, quietMs, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        const idle = await page.evaluate(() => Date.now() - window.__coolLastMessageAt);
        if (idle >= quietMs) return true;
        if (Date.now() > deadline) return false;
        sleep(Math.max(0.1, (quietMs - idle) / 1000));
    }
}

/**
 * Open a text document, and once it is fully loaded insert an image from the
 * local file system and measure how long the document takes to show it.
 *
 * `image` is the picked file, as k6 hands one to a file input: { name,
 * mimeType, buffer } with the bytes base64-encoded (the test reads them with
 * open() in the init context, which is the local file system this is about).
 *
 * The document is opened directly in the browser rather than inside the WOPI
 * host page, because this test reads the client's own page: the file input
 * behind Insert > Image, and the canvas the document is drawn on. In the host
 * page the client is a frame of another origin, where neither is reachable.
 * The trade is that the client posts no App_LoadingStatus to a host it does not
 * have, so document_loaded_ms here is the document being open and still rather
 * than the Document_Loaded message the other tests take it from.
 */
export async function runInsertImageScenario(fileId, image) {
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await context.newPage();
    try {
        const client = new URL(await getWopiClientUrl(wopiUrl));
        client.searchParams.set('WOPISrc', getWopiSrc(wopiHost, fileId).toString());
        // The token that the WOPI host page posts, which coolwsd takes from the
        // query here. The upload behind Insert > Image carries it, and coolwsd
        // refuses an upload that has none.
        client.searchParams.set('access_token', 'test');

        const start = Date.now();
        await page.goto(client.toString());
        pageLoad.add(Date.now() - start);

        await waitForDocumentStill(page);
        record(documentLoaded, 'document_loaded_ms', Date.now() - start);
        await sampleBrowserHeap(page);

        await page.evaluate(watchForInsertedImage,
                            { size: 64, delta: 30, fraction: 0.01 });
        const watching = await page.evaluate(watchServerMessages);
        // On the page and not on a locator: k6's Locator carries no
        // setInputFiles. The input is hidden, which this path allows, since it
        // waits for the element to be attached and for no other state.
        await page.setInputFiles('#insertgraphic', image);

        const ms = await waitInPage(page, () => window.__coolImageWatch.displayedMs,
                                    null, 60000, 'the image to be displayed');
        if (ms === null) {
            await screenshotPage(page);
            fail('the inserted image was never displayed');
        }
        record(imageDisplayed, 'image_displayed_ms', ms);

        // The client selects what it has just inserted, so this says the change
        // on the canvas is the image and not something else that moved.
        const selected = await page.evaluate(() => {
            const selection = window.app && window.app.definitions
                && window.app.definitions.graphicSelection;
            return selection ? selection.hasActiveSelection() : null;
        });
        if (selected !== null)
            check(selected, { 'the image is in the document': (v) => v === true });

        if (watching && !await waitForServerQuiet(page, 1000, 30000))
            console.warn('the server was still sending messages 30 s after the insert');

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
    const counter = data.metrics[requirement.metric.replace(/_ms$/, '_samples')];
    const samples = ((counter || {}).values || {}).count || 0;
    // A budget cannot be met over nothing. An empty trend reports zero for
    // every statistic, which reads as the fastest run ever recorded, so a
    // requirement measured over nothing has no value: it is unanswered.
    return {
        ...requirement,
        value: samples > 0 && value !== undefined ? value : null,
        samples,
        ok: samples > 0 && value !== undefined && value <= requirement.max_ms,
    };
}

/**
 * Build the handleSummary function a test exports, writing metrics.json in the
 * shape the dashboard reads. `app` e.g. "writer", `doc` e.g. "blank" | "5mb",
 * `requirement` the service level the test asserts, if it has one, and
 * `extra` the test's own trends (`metrics`, names) and counters (`counters`,
 * output key -> counter name) to write beside the load timings.
 */
export function makeSummary(app, doc, requirement, extra = {}) {
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
        // A counter that never fired is absent from data.metrics entirely, which
        // for loader_shown is the interesting answer, so report it as 0 rather
        // than leaving it out.
        const count = (name) => {
            const v = data.metrics[name] && data.metrics[name].values;
            return v ? (v.count || 0) : 0;
        };
        const metrics = {
            page_load_ms: pick('page_load_ms'),
            frame_ready_ms: pick('frame_ready_ms'),
            document_loaded_ms: pick('document_loaded_ms'),
            browser_heap_mb: pick('browser_heap_mb'),
        };
        metrics.doc_loaded_click_ms = pick('doc_loaded_click_ms');
        FEEDBACK_METRICS.forEach((name) => { metrics[name] = pick(name); });
        (extra.metrics || []).forEach((name) => { metrics[name] = pick(name); });
        if (EDIT) metrics.edit_ms = pick('edit_ms');
        const imageMs = pick('image_displayed_ms');
        if (imageMs) metrics.image_displayed_ms = imageMs;
        const verdict = requirement ? requirementOutcome(requirement, data) : null;
        const out = {
            app, doc, scenario: `${app}-${doc}`,
            users: USERS, edit: EDIT,
            // which build was tested + when, so the dashboard can plot over
            // commits/time (COMMIT is passed in by browser-suite.py / k6-wrap)
            commit: __ENV['COMMIT'] || null,
            ts: new Date().toISOString(),
            // How many of this run's iterations ever showed COOL's progress
            // overlay. Zero means the user got no explicit loading indicator.
            loader_shown: count(LOADER_SHOWN_COUNTER),
            metrics,
        };
        if (verdict) out.requirement = verdict;
        Object.entries(extra.counters || {}).forEach(([key, name]) => { out[key] = count(name); });
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
                    : '')
                + `${app}-${doc}: first_content avg `
                + `${Math.round((metrics.first_content_ms || {}).avg || 0)} ms, `
                + `loader shown ${out.loader_shown}x\n`,
        };
    };
}
