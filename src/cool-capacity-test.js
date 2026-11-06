/**
 * Capacity test: how many users one Collabora Online instance serves within its response targets.
 *
 * Each VU is one user. A user opens a document, keeps it open for CAP_SESSION, and works the way a
 * person does: a pause of a few seconds, then a word typed one character at a time, then another
 * pause. Every invalidation the server sends makes the user ask again for the tiles of its visible
 * area, as the browser client does, so the kit renders for each user too. The visible area follows
 * the cursor, as the browser client scrolls to it, so a long document is viewed where it is typed in.
 * Each tile received is acknowledged with tileprocessed, as the browser client does, so coolwsd keeps
 * sending tiles to the user. Each edit is logged on an EDIT line with its time, latency and document
 * group, which latency-spikes.py lines up with the server's timeline. When the session ends the
 * user closes the document and opens it again, so loads keep happening during the whole test.
 *
 * The number of users climbs in a staircase (ramping-vus): a ramp to each plateau in CAP_VUS, then a
 * hold. Every measurement taken during a hold is tagged with that plateau, and each plateau has its
 * own thresholds:
 *
 *   edit_latency    a typed character -> the server moving that user's cursor (invalidatecursor:)
 *   doc_load_time   the load message -> the document status (status:)
 *   session_errors  sessions that failed to load, lost their socket, or had a keystroke time out
 *
 * The summary prints one line per plateau and names the capacity: the largest plateau that met every
 * threshold. A run stops early once more than half of the sessions fail, since the plateaus above
 * that point cannot pass.
 *
 * Users each open a document of their own by default. CAP_USERS_PER_DOC=5 puts users in groups of five
 * which edit one document together.
 *
 * Run k6 on another machine than the server, or the load generator takes the CPU it is measuring.
 * Check the server's own limits before reading a result as capacity: MAX_CONNECTIONS and
 * MAX_DOCUMENTS from configure (--with-max-connections, --with-max-documents) refuse sessions above
 * them, and num_prespawn_children in coolwsd.xml decides how many kits wait ready.
 *
 * Env:
 *   CAP_VUS           - comma-separated user plateaus                (default "10,20,40,80")
 *   CAP_RAMP          - time to climb to each plateau                (default "1m")
 *   CAP_HOLD          - time held at each plateau                    (default "3m")
 *   CAP_SESSION       - seconds a user keeps the document open       (default 300)
 *   CAP_USERS_PER_DOC - users editing the same document              (default 1)
 *   CAP_FILE_ID       - WOPI file id to open                         (default 11, blank.odt)
 *   CAP_THINK_MIN_MS, CAP_THINK_MAX_MS - pause between two words      (default 2000, 8000)
 *   CAP_TYPE_STEP_MS  - pause between two characters of a word       (default 150)
 *   CAP_EDIT_P95_MS   - edit latency target, 95th percentile         (default 300)
 *   CAP_LOAD_P95_MS   - document load target, 95th percentile        (default 10000)
 *   CAP_MAX_ERRORS    - share of failed sessions allowed             (default 0.01)
 */

import exec from 'k6/execution';
import http from 'k6/http';
import { Counter, Rate, Trend } from 'k6/metrics';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.0.2/index.js';

import { checkWopi, getWopiClientUrl, getWopiSrc } from '../lib/wopi_discovery.js';
import { CoolClientWs } from '../lib/cool_client_ws.js';
import { wopiHost, wopiUrl } from './config.js';

const num = (name, fallback) => Number(__ENV[name] ?? fallback);

const PLATEAUS = (__ENV['CAP_VUS'] || '10,20,40,80')
    .split(',').map((s) => Number(s.trim())).filter((n) => n > 0);
const RAMP = __ENV['CAP_RAMP'] || '1m';
const HOLD = __ENV['CAP_HOLD'] || '3m';
const SESSION_MS = num('CAP_SESSION', 300) * 1000;
const USERS_PER_DOC = Math.max(1, num('CAP_USERS_PER_DOC', 1));
const FILE_ID = __ENV['CAP_FILE_ID'] || 11;  // documents/blank.odt
const THINK_MIN_MS = num('CAP_THINK_MIN_MS', 2000);
const THINK_MAX_MS = num('CAP_THINK_MAX_MS', 8000);
const TYPE_STEP_MS = num('CAP_TYPE_STEP_MS', 150);
const EDIT_P95_MS = num('CAP_EDIT_P95_MS', 300);
const LOAD_P95_MS = num('CAP_LOAD_P95_MS', 10000);
const MAX_ERRORS = num('CAP_MAX_ERRORS', 0.01);

// The server answers a load with status: well within limit_load_secs, 100 s by default.
const LOAD_TIMEOUT_MS = 100000;
const EDIT_TIMEOUT_MS = 10000;
const TILE_REQUEST_GAP_MS = 200;

// The visible area of a desktop browser window at 100% zoom.
const TILE_PX = 256;
const TILE_TWIPS = 3840;
const COLS = 5;
const ROWS = 4;

const WORDS = ['capacity', 'document', 'paragraph', 'online', 'server', 'measure', 'latency',
    'office', 'review', 'editing', 'meeting', 'report', 'budget', 'planning', 'draft'];

function seconds(duration) {
    const m = /^(\d+(?:\.\d+)?)(ms|s|m|h)$/.exec(duration);
    const factor = { ms: 0.001, s: 1, m: 60, h: 3600 }[m[2]];
    return Number(m[1]) * factor;
}

// Each plateau as [start, end) of its hold, in seconds from the start of the test.
const HOLDS = (() => {
    const holds = [];
    let t = 0;
    for (const users of PLATEAUS) {
        t += seconds(RAMP);
        holds.push({ users, start: t, end: t + seconds(HOLD) });
        t += seconds(HOLD);
    }
    return holds;
})();

function buildStages() {
    const stages = [];
    for (const users of PLATEAUS) {
        stages.push({ duration: RAMP, target: users });
        stages.push({ duration: HOLD, target: users });
    }
    stages.push({ duration: '30s', target: 0 });
    return stages;
}

function buildThresholds() {
    const thresholds = {
        // Past half of the sessions failing, the remaining plateaus only add load to a broken server.
        session_errors: [{ threshold: 'rate<0.5', abortOnFail: true, delayAbortEval: '1m' }],
    };
    for (const { users } of HOLDS) {
        thresholds[`edit_latency{plateau:${users}}`] = [`p(95)<${EDIT_P95_MS}`];
        thresholds[`doc_load_time{plateau:${users}}`] = [`p(95)<${LOAD_P95_MS}`];
        thresholds[`session_errors{plateau:${users}}`] = [`rate<${MAX_ERRORS}`];
    }
    return thresholds;
}

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: {
        capacity: {
            executor: 'ramping-vus',
            startVUs: 0,
            stages: buildStages(),
            gracefulRampDown: '30s',
        },
    },
    thresholds: buildThresholds(),
    summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'],
};

const editLatency = new Trend('edit_latency', true);
const docLoadTime = new Trend('doc_load_time', true);
const sessionErrors = new Rate('session_errors');
const editTimeouts = new Counter('edit_timeouts');

// The plateau whose hold is under way, or 'ramp' between two holds.
function plateauTag() {
    const t = exec.instance.currentTestRunDuration / 1000;
    const hold = HOLDS.find((h) => t >= h.start && t < h.end);
    return { plateau: hold ? String(hold.users) : 'ramp' };
}

function randomBetween(low, high) {
    return low + Math.random() * (high - low);
}

// Users of one group get the same WOPISrc. COOL keys the document on the WOPISrc path and ignores
// its query, so the group goes in the path: the WOPI host serves FILE_ID for "<FILE_ID>~<group>".
// ramping-vus does not start its VUs in the order of their ids, so the WOPI host numbers the users
// in the order they first ask, and each group fills up before the next one opens. A VU keeps its
// group when it opens the document again.
let group = null;
function groupWopiSrc() {
    if (group === null) {
        const base = String(wopiHost).replace(/\/?$/, '/');
        const res = http.get(`${base}capacity/group?size=${USERS_PER_DOC}`);
        group = res.status === 200 ? res.json('group')
            : Math.floor((exec.vu.idInTest - 1) / USERS_PER_DOC);
    }
    return getWopiSrc(wopiHost, `${FILE_ID}~${group}`).toString();
}

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

export default async function () {
    const wopiClient = await getWopiClientUrl(wopiUrl);
    const wopiSrc = groupWopiSrc();

    await new Promise((resolve) => {
        const s = {
            openedAt: 0, loaded: false, failed: false, closing: false, done: false,
            editSentAt: 0, word: '', charIndex: 0,
            lastTileRequest: 0, viewTop: 0, processedWids: [], timers: new Set(),
        };
        let client;

        function later(fn, ms) {
            const id = setTimeout(() => { s.timers.delete(id); fn(); }, ms);
            s.timers.add(id);
        }

        function finish() {
            if (s.done) return;
            s.done = true;
            for (const id of s.timers) clearTimeout(id);
            sessionErrors.add(s.failed ? 1 : 0, plateauTag());
            resolve();
        }

        function fail() {
            s.failed = true;
            close();
        }

        function close() {
            if (s.closing) return;
            s.closing = true;
            try { client.close(); } catch (e) { /* already closed */ }
            // onclose resolves; this covers a socket that never reports closing.
            later(finish, 5000);
        }

        function requestVisibleTiles() {
            const now = Date.now();
            if (now - s.lastTileRequest < TILE_REQUEST_GAP_MS) return;
            s.lastTileRequest = now;
            const xs = [];
            const ys = [];
            for (let c = 0; c < COLS; c++) {
                for (let r = 0; r < ROWS; r++) {
                    xs.push(c * TILE_TWIPS);
                    ys.push(s.viewTop + r * TILE_TWIPS);
                }
            }
            client.send(`tilecombine nviewid=0 part=0 width=${TILE_PX} height=${TILE_PX} `
                + `tileposx=${xs.join(',')} tileposy=${ys.join(',')} `
                + `tilewidth=${TILE_TWIPS} tileheight=${TILE_TWIPS}`);
        }

        // coolwsd stops sending tiles to a client that has about a screen of them unacknowledged,
        // so the wire id of each tile goes back in a tileprocessed message, batched as the
        // browser client does.
        function tileReceived(data) {
            const head = new Uint8Array(data, 0, Math.min(data.byteLength, 512));
            const nl = head.indexOf(10);
            let header = '';
            for (let i = 0; i < (nl < 0 ? head.length : nl); i++) header += String.fromCharCode(head[i]);
            if (!header.startsWith('tile:') && !header.startsWith('delta:')) return;
            const m = /\bwid=(\d+)/.exec(header);
            if (!m) return;
            if (s.processedWids.length === 0) {
                later(() => {
                    if (s.closing || !s.processedWids.length) return;
                    client.send(`tileprocessed wids=${s.processedWids.join(',')}`);
                    s.processedWids = [];
                }, 20);
            }
            s.processedWids.push(m[1]);
        }

        function sendVisibleArea() {
            client.send(`clientvisiblearea x=0 y=${s.viewTop} width=${COLS * TILE_TWIPS} `
                + `height=${ROWS * TILE_TWIPS}`);
        }

        // Moves the visible area to a cursor outside it: one tile row of the area stays above the
        // cursor row, as after the browser client scrolls to the cursor.
        function followCursor(payload) {
            let rect;
            try { rect = JSON.parse(payload).rectangle; } catch (e) { return; }
            if (typeof rect !== 'string') return;
            const y = Number(rect.split(',')[1]);
            if (!Number.isFinite(y)) return;
            if (y >= s.viewTop && y < s.viewTop + ROWS * TILE_TWIPS) return;
            s.viewTop = Math.max(0, Math.floor(y / TILE_TWIPS) - 1) * TILE_TWIPS;
            sendVisibleArea();
            s.lastTileRequest = 0;
            requestVisibleTiles();
        }

        function nextWord() {
            if (s.closing) return;
            if (Date.now() - s.openedAt >= SESSION_MS) { close(); return; }
            later(() => {
                s.word = WORDS[Math.floor(Math.random() * WORDS.length)] + ' ';
                s.charIndex = 0;
                typeChar();
            }, randomBetween(THINK_MIN_MS, THINK_MAX_MS));
        }

        // One character at a time, the next only once the server answered the last one, so a slow
        // server shows as a longer edit_latency rather than as a queue of unanswered keystrokes.
        function typeChar() {
            if (s.closing) return;
            if (s.charIndex >= s.word.length) { nextWord(); return; }
            const ch = s.word[s.charIndex++];
            s.editSentAt = Date.now();
            if (ch === ' ') {
                client.send('key type=input char=32 key=0');
                client.send('key type=up char=0 key=0');
            } else {
                client.send(`textinput id=0 text=${ch}`);
            }
            const sentAt = s.editSentAt;
            later(() => {
                if (s.editSentAt === sentAt) {
                    editTimeouts.add(1, plateauTag());
                    fail();
                }
            }, EDIT_TIMEOUT_MS);
        }

        client = new CoolClientWs(wopiClient, wopiSrc, () => {
            s.openedAt = Date.now();
            client.openDocument(wopiSrc);
            later(() => { if (!s.loaded) fail(); }, LOAD_TIMEOUT_MS);
        });

        // Text frames are protocol messages, binary frames are tiles.
        client.socket.binaryType = 'arraybuffer';
        client.socket.onmessage = (e) => {
            if (typeof e.data !== 'string') {
                if (e.data instanceof ArrayBuffer) tileReceived(e.data);
                return;
            }
            if (!s.loaded && e.data.startsWith('status:')) {
                s.loaded = true;
                docLoadTime.add(Date.now() - s.openedAt, plateauTag());
                client.send(`clientzoom tilepixelwidth=${TILE_PX} tilepixelheight=${TILE_PX} `
                    + `tiletwipwidth=${TILE_TWIPS} tiletwipheight=${TILE_TWIPS}`);
                sendVisibleArea();
                client.send('uno .uno:GoToEndOfDoc');
                requestVisibleTiles();
                nextWord();
            } else if (e.data.startsWith('invalidatecursor:')) {
                followCursor(e.data.substring('invalidatecursor:'.length));
                if (!s.editSentAt) return;
                const now = Date.now();
                editLatency.add(now - s.editSentAt, plateauTag());
                // One line per edit, for lining slow edits up with what the server did then.
                console.log(`EDIT t_ms=${now} ms=${now - s.editSentAt} doc=${group} vu=${exec.vu.idInTest}`);
                s.editSentAt = 0;
                later(typeChar, TYPE_STEP_MS);
            } else if (e.data.startsWith('invalidatetiles:')) {
                requestVisibleTiles();
            }
        };
        client.socket.onclose = () => {
            if (!s.closing) s.failed = true;
            finish();
        };
    });
}

export function handleSummary(data) {
    const metric = (name, users) => data.metrics[`${name}{plateau:${users}}`];
    const passed = (m) => !m || !m.thresholds || Object.values(m.thresholds).every((t) => t.ok);
    const ms = (v) => (v === undefined ? '-' : `${Math.round(v)} ms`);

    const rows = HOLDS.map(({ users }) => {
        const edit = metric('edit_latency', users);
        const load = metric('doc_load_time', users);
        const errors = metric('session_errors', users);
        return {
            users,
            reached: Boolean(edit || load || errors),
            editP95: edit?.values['p(95)'],
            edits: edit?.values.count ?? 0,
            loadP95: load?.values['p(95)'],
            loads: load?.values.count ?? 0,
            errorRate: errors?.values.rate,
            // A threshold on a metric with no samples passes, so a plateau where no document loaded
            // or no keystroke was answered would otherwise count as met.
            measured: (edit?.values.count ?? 0) > 0 && (load?.values.count ?? 0) > 0,
            ok: passed(edit) && passed(load) && passed(errors),
        };
    });
    // A plateau the run never reached, because it stopped early, did not pass.
    let capacity = 0;
    for (const row of rows) {
        if (!row.reached || !row.measured || !row.ok) break;
        capacity = row.users;
    }

    const lines = ['', 'users  edit p95   (edits)  load p95   (loads)  failed sessions  result'];
    for (const r of rows) {
        const errors = r.errorRate === undefined ? '-' : `${(100 * r.errorRate).toFixed(1)}%`;
        const result = !r.reached ? 'not reached' : !r.measured ? 'NOT MEASURED' : r.ok ? 'ok' : 'FAILED';
        lines.push(`${String(r.users).padStart(5)}  ${ms(r.editP95).padStart(8)} ${String(r.edits).padStart(9)}`
            + `  ${ms(r.loadP95).padStart(8)} ${String(r.loads).padStart(9)}  ${errors.padStart(15)}  ${result}`);
    }
    lines.push('', `capacity: ${capacity} users (targets: edit p95 < ${EDIT_P95_MS} ms, `
        + `load p95 < ${LOAD_P95_MS} ms, failed sessions < ${100 * MAX_ERRORS}%, `
        + `${USERS_PER_DOC} user(s) per document)`, '');

    return {
        stdout: textSummary(data, { indent: ' ', enableColors: true }) + lines.join('\n'),
        'capacity.json': JSON.stringify({ capacity, usersPerDocument: USERS_PER_DOC, plateaus: rows }, null, 2),
    };
}
