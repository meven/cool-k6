/**
 * How long before the user sees anything happen.
 *
 * The requirement behind this module is "the system must provide user feedback
 * within 0.3 seconds, in 98% of all user actions". frame_ready_ms cannot answer
 * it: that is the END of loading. These five signals time the visible steps on
 * the way there, all from the click that opens the document.
 *
 *   first_paint_ms    first pixels in the Collabora Online iframe. Usually a
 *                     background fill, so treat it as the floor rather than as
 *                     real feedback.
 *   first_content_ms  first-contentful-paint: the editor chrome becomes
 *                     visible. The closest honest match to "the user sees
 *                     something happening", and the one the budget guards.
 *   js_ready_ms       window.app exists, so the bundle has run and the client
 *                     is reactive. Taken on the first animation frame that sees
 *                     it, which means a blocked main thread pushes it out. That
 *                     is the truth we want, not an error.
 *   canvas_ms         the document canvas is in the DOM.
 *   loader_shown_ms   Collabora Online's progress overlay appeared. It is armed
 *                     by a 500 ms timer (ProgressOverlay.delayedStart) that the
 *                     initial load path normally cancels first, so this is
 *                     usually absent; the loader_shown counter says how often it
 *                     appeared at all.
 *
 * Every timestamp is taken inside the browser and read out afterwards in one
 * call. Polling over the browser protocol costs tens of milliseconds, which is
 * not affordable when the budget is 300.
 *
 * Environment:
 *   FEEDBACK_SLA_MS  when set, fail the run if first_content_ms p(98) exceeds
 *                    it (the requirement is 300). Off by default: the suite has
 *                    to be able to measure before it is made to enforce.
 */
import { sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const SLA_MS = Number(__ENV['FEEDBACK_SLA_MS'] || 0);

/** How long the canvas gets to turn up after the document is ready. */
const CANVAS_GRACE_MS = 1500;
const CANVAS_POLL_S = 0.25;

const firstPaint = new Trend('first_paint_ms', true);
const firstContent = new Trend('first_content_ms', true);
const jsReady = new Trend('js_ready_ms', true);
const canvasUp = new Trend('canvas_ms', true);
const loaderShownMs = new Trend('loader_shown_ms', true);
const loaderShown = new Counter('loader_shown');

/** Trend names this module records, in the order they happen. */
export const FEEDBACK_METRICS = [
    'first_paint_ms', 'js_ready_ms', 'first_content_ms', 'canvas_ms',
    'loader_shown_ms',
];

/** Counter naming how many iterations ever showed the progress overlay. */
export const LOADER_SHOWN_COUNTER = 'loader_shown';

/** Mark name -> the Trend it feeds. */
const TRENDS = {
    first_paint: firstPaint,
    first_content: firstContent,
    js_ready: jsReady,
    canvas: canvasUp,
};

/**
 * Runs in every frame before any page script, so it is already watching when
 * Collabora Online starts up. It only stamps timestamps; nothing is read back
 * until the document is up.
 */
const PROBE = `
(function () {
  if (window.__coolFeedback) return;
  var M = { origin: performance.timeOrigin, marks: {} };
  window.__coolFeedback = M;

  var mark = function (name) {
    if (M.marks[name] === undefined) M.marks[name] = performance.now();
  };

  // window.app is Collabora Online's own namespace: once it exists the bundle
  // has run. The canvas is the document surface itself.
  var tick = function () {
    if (window.app) mark('js_ready');
    if (document.querySelector('#document-container canvas, #document-canvas'))
      mark('canvas');
    if (M.marks.js_ready === undefined || M.marks.canvas === undefined)
      requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  // A MutationObserver still fires when animation frames are starved by a busy
  // main thread, which is exactly when the progress overlay would appear.
  var isLoader = function (node) {
    if (!node || node.nodeType !== 1) return false;
    if (node.classList && node.classList.contains('leaflet-progress-layer'))
      return true;
    return !!(node.querySelector &&
              node.querySelector('.leaflet-progress-layer'));
  };
  var observer = new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var added = records[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        if (isLoader(added[j])) {
          mark('loader');
          observer.disconnect();
          return;
        }
      }
    }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
`;

/**
 * Arm the probe on a browser context. Must be called before the context has a
 * page, so the script is in place for the iframe's first script.
 *
 * A k6 build without addInitScript should still produce the load timings, so a
 * failure here is reported and swallowed rather than thrown.
 */
export async function installFeedbackProbe(context) {
    try {
        await context.addInitScript(PROBE);
        return true;
    } catch (error) {
        console.warn(`feedback probe not installed, ` +
                     `its signals will be missing: ${error}`);
        return false;
    }
}

/**
 * Read the probe's marks out of the Collabora Online iframe.
 *
 * The iframe is a different origin than the WOPI host page, so page script
 * cannot reach into it. k6 drives the browser over CDP instead, which addresses
 * each frame directly and does not care about the origin boundary.
 *
 * Returns null when the frame or the probe is not there. That must not fail the
 * run: these signals are extra to what the test already asserts.
 */
async function readMarks(page) {
    const frames = page.frames();
    for (let i = 0; i < frames.length; i++) {
        try {
            const marks = await frames[i].evaluate(function () {
                if (String(location.href).indexOf('cool.html') < 0) return null;
                var probe = window.__coolFeedback;
                if (!probe) return null;

                var out = { origin: probe.origin, marks: {} };
                for (var name in probe.marks) out.marks[name] = probe.marks[name];

                // Paint timings come from the browser itself; no probe needed.
                var paints = performance.getEntriesByType('paint');
                for (var p = 0; p < paints.length; p++) {
                    if (paints[p].name === 'first-paint')
                        out.marks.first_paint = paints[p].startTime;
                    if (paints[p].name === 'first-contentful-paint')
                        out.marks.first_content = paints[p].startTime;
                }
                return out;
            });
            if (marks) return marks;
        } catch (error) {
            // The frame went away mid-read; try the next one.
        }
    }
    return null;
}

/**
 * Read the marks, giving the canvas a bounded chance to turn up.
 *
 * On a fast document Frame_Ready arrives before the canvas is in the DOM, so
 * reading straight away would record canvas_ms as absent on exactly the
 * quickest runs. Waiting cannot move a timing -- the marks were stamped in the
 * page when they happened -- it only costs wall clock, and only while the mark
 * is still missing.
 */
async function readSettledMarks(page) {
    const deadline = Date.now() + CANVAS_GRACE_MS;
    let marks = await readMarks(page);
    while (marks && marks.marks.canvas === undefined && Date.now() < deadline) {
        sleep(CANVAS_POLL_S);
        marks = await readMarks(page);
    }
    return marks;
}

/**
 * Record the feedback signals for one iteration.
 *
 * @p clickedAt is the moment the document was opened; every signal is reported
 * relative to it, because that is the user action the budget is about.
 */
export async function recordFeedback(page, clickedAt) {
    const marks = await readSettledMarks(page);
    if (!marks) return;

    // A mark earlier than the click would have come from a previous document in
    // the same context. Drop it rather than poison the trend.
    const msSinceClick = (name) => {
        const mark = marks.marks[name];
        if (mark === undefined) return null;
        const ms = Math.round(marks.origin + mark - clickedAt);
        return ms >= 0 ? ms : null;
    };

    for (const name in TRENDS) {
        const ms = msSinceClick(name);
        if (ms !== null) TRENDS[name].add(ms);
    }

    const loader = msSinceClick('loader');
    if (loader !== null) {
        loaderShownMs.add(loader);
        loaderShown.add(1);
    }
}

/**
 * The k6 threshold expressing the requirement, or null when it is not being
 * enforced. "Within 0.3 seconds, in 98% of all user actions", as k6 checks it.
 */
export function feedbackThresholds() {
    if (SLA_MS <= 0) return null;
    return { first_content_ms: [`p(98)<${SLA_MS}`] };
}
