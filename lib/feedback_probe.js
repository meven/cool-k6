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
 *
 * Between js_ready and Document_Loaded the client talks to the server, and four
 * more marks split that wait, from the WebSocket to coolwsd:
 *
 *   ws_open_ms        the WebSocket is open.
 *   doc_status_ms     the first status: message, sent once the kit has loaded
 *                     the document.
 *   style_reply_ms    the first commandvalues: reply for .uno:StyleApply, the
 *                     style list, which the kit builds on its main thread.
 *   first_tile_ms     the first tile: or delta: message.
 *
 * style_requests_n and style_replies_n count the .uno:StyleApply requests the
 * client sent and the replies it got, as a test can ask more than once.
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
const wsOpen = new Trend('ws_open_ms', true);
const docStatus = new Trend('doc_status_ms', true);
const styleReply = new Trend('style_reply_ms', true);
const firstTile = new Trend('first_tile_ms', true);
const styleRequests = new Trend('style_requests_n');
const styleReplies = new Trend('style_replies_n');
const loaderShownMs = new Trend('loader_shown_ms', true);
const loaderShown = new Counter('loader_shown');
// The bytes of the COOL frame until the document is loaded: the HTTP responses as they came over
// the wire, so compressed and with their headers, the same responses uncompressed, and the
// messages of the WebSocket each way. A WebSocket message counts its payload, a text one in
// characters, which COOL keeps ASCII nearly always.
const netHttp = new Trend('net_http_kb');
const netHttpDecoded = new Trend('net_http_decoded_kb');
const netWsRecv = new Trend('net_ws_recv_kb');
const netWsSent = new Trend('net_ws_sent_kb');

/** Trend names this module records, in the order they happen. */
export const FEEDBACK_METRICS = [
    'first_paint_ms', 'js_ready_ms', 'ws_open_ms', 'doc_status_ms',
    'style_reply_ms', 'first_content_ms', 'canvas_ms', 'first_tile_ms',
    'loader_shown_ms', 'style_requests_n', 'style_replies_n',
    'net_http_kb', 'net_http_decoded_kb', 'net_ws_recv_kb', 'net_ws_sent_kb',
];

/** Counter naming how many iterations ever showed the progress overlay. */
export const LOADER_SHOWN_COUNTER = 'loader_shown';

/** Mark name -> the Trend it feeds. */
const TRENDS = {
    first_paint: firstPaint,
    first_content: firstContent,
    js_ready: jsReady,
    canvas: canvasUp,
    ws_open: wsOpen,
    ws_status: docStatus,
    style_reply: styleReply,
    first_tile: firstTile,
};

/**
 * Runs in every frame before any page script, so it is already watching when
 * Collabora Online starts up. It only stamps timestamps; nothing is read back
 * until the document is up.
 */
const PROBE = `
(function () {
  if (window.__coolFeedback) return;
  var M = { origin: performance.timeOrigin, marks: {},
            counts: { style_requests: 0, style_replies: 0,
                      ws_recv_bytes: 0, ws_sent_bytes: 0 } };
  window.__coolFeedback = M;
  // The browser keeps 250 resource entries by default, fewer than COOL loads.
  if (performance.setResourceTimingBufferSize)
    performance.setResourceTimingBufferSize(100000);

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

  // The socket to coolwsd. Only the start of each message is read: it names
  // the message, and a tile arrives as an ArrayBuffer with a text header.
  var head = function (data) {
    if (typeof data === 'string') return data.slice(0, 160);
    if (data instanceof ArrayBuffer && window.TextDecoder)
      return new TextDecoder().decode(
        new Uint8Array(data, 0, Math.min(160, data.byteLength)));
    return '';
  };
  var size = function (data) {
    if (typeof data === 'string') return data.length;
    if (data && data.byteLength !== undefined) return data.byteLength;
    if (data && data.size !== undefined) return data.size;
    return 0;
  };
  var watch = function (ws) {
    ws.addEventListener('open', function () { mark('ws_open'); });
    ws.addEventListener('message', function (event) {
      M.counts.ws_recv_bytes += size(event.data);
      var h = head(event.data);
      if (h.indexOf('status:') === 0) mark('ws_status');
      else if (h.indexOf('tile:') === 0 || h.indexOf('delta:') === 0)
        mark('first_tile');
      else if (h.indexOf('commandvalues:') === 0 &&
               h.indexOf('.uno:StyleApply') >= 0) {
        mark('style_reply');
        M.counts.style_replies++;
      }
    });
    var send = ws.send;
    ws.send = function (data) {
      M.counts.ws_sent_bytes += size(data);
      if (typeof data === 'string' &&
          data.indexOf('commandvalues command=.uno:StyleApply') === 0)
        M.counts.style_requests++;
      return send.apply(ws, arguments);
    };
  };
  var NativeWebSocket = window.WebSocket;
  if (NativeWebSocket) {
    var Watched = function (url, protocols) {
      var ws = protocols === undefined ? new NativeWebSocket(url)
                                       : new NativeWebSocket(url, protocols);
      if (String(url).indexOf('/cool/') >= 0) watch(ws);
      return ws;
    };
    Watched.prototype = NativeWebSocket.prototype;
    ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (name) {
      Watched[name] = NativeWebSocket[name];
    });
    window.WebSocket = Watched;
  }

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

                var out = { origin: probe.origin, marks: {}, counts: {} };
                for (var name in probe.marks) out.marks[name] = probe.marks[name];
                for (var c in probe.counts) out.counts[c] = probe.counts[c];

                // The HTTP responses of the frame, its page included.
                var http = 0, decoded = 0;
                var entries = performance.getEntriesByType('navigation')
                    .concat(performance.getEntriesByType('resource'));
                for (var e = 0; e < entries.length; e++) {
                    http += entries[e].transferSize || 0;
                    decoded += entries[e].decodedBodySize || 0;
                }
                out.counts.http_bytes = http;
                out.counts.http_decoded_bytes = decoded;

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

    // Counted only once the socket opened, so a frame that never reached the
    // server does not record zero requests.
    if (marks.marks.ws_open !== undefined && marks.counts) {
        styleRequests.add(marks.counts.style_requests || 0);
        styleReplies.add(marks.counts.style_replies || 0);
        netHttp.add((marks.counts.http_bytes || 0) / 1024);
        netHttpDecoded.add((marks.counts.http_decoded_bytes || 0) / 1024);
        netWsRecv.add((marks.counts.ws_recv_bytes || 0) / 1024);
        netWsSent.add((marks.counts.ws_sent_bytes || 0) / 1024);
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
