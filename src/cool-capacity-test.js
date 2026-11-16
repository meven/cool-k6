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
 *   edit_latency    a typed character -> the server moving that user's cursor (invalidatecursor:),
 *                   or for a space that leaves the cursor in place and so gets no cursor message,
 *                   the server invalidating the tiles under that cursor (invalidatetiles:)
 *   doc_load_time   the load message -> the document status (status:), from the first attempt
 *   load_retries    loads asked for again because the server was still unloading the document
 *   session_errors  sessions that failed to load, lost their socket, or had a keystroke time out,
 *                   or whose document was still unloading after every retry,
 *                   against the sessions open at the end of the hold. A failure in a ramp counts
 *                   against the plateau that the ramp leads to.
 *
 * The summary prints one line per plateau and names the capacity: the largest plateau that met every
 * threshold. A run stops early once more than half of the sessions fail, since the plateaus above
 * that point cannot pass.
 *
 * Users each open a document of their own by default. CAP_USERS_PER_DOC=5 puts users in groups of five
 * which edit one document together.
 *
 * CAP_SCENARIO picks the documents and how users share them instead, from SCENARIOS below:
 *
 *   writer, calc, impress   the 5 MB ODF and OOXML documents of one application, one user each,
 *                           ODF and OOXML in turn
 *   writer-pairs, ...       the same documents, with half of the users editing a document two by two
 *   mixed                   in every 10 documents, one each of odt, docx, ods, xlsx, odp and pptx
 *                           with one user, then 4 more with two users each
 *   writer-shared, calc-shared, impress-shared
 *                           every user on one 5 MB document of the application. One user in
 *                           CAP_EDITOR_EVERY edits, each in a part of its own: a paragraph 8 after
 *                           the one of the editor before, the next column of the empty row below the
 *                           data, or the next slide of impress-slides-5mb.odp. The other users view
 *                           the part of the editor before them, without typing.
 *   reference               the real documents of documents/reference-docs.json, which
 *                           documents/fetch-reference-docs.py downloads: a 960-page book of
 *                           right-to-left text, an 846-page standard, a guide of 659 images, a 500000-formula sheet, a
 *                           workbook of 31 sheets of dense tables, a deck of 15 MB of images and
 *                           a 104-slide deck, one user each
 *   reference-pairs         the same documents, each opened by one user and then by two users
 *                           who edit it together
 *   reference-ooxml, reference-ooxml-pairs
 *                           the same with the DOCX, XLSX and PPTX ones: the 960-page book, a
 *                           trade agreement of 236 pages in fonts the server lacks, a 409-page
 *                           table of right-to-left text, 50 sheets of RAND() formulas that every
 *                           edit recalculates, the 31-sheet workbook, 56000 cells of multiline
 *                           text, and 2 slides on a master filled with a bitmap
 *   rtl-book-shared, formulas-shared, yottadb-shared
 *                           every user on one of the real documents, as in writer-shared: the
 *                           960-page book, the 500000-formula sheet, or the 29-slide deck
 *                           with 15 MB of images
 *   finance-shared          every user on calc-finance.ods, the sales ledger of a company with
 *                           a pivot table and charts over it. One user in CAP_EDITOR_EVERY edits
 *                           the Units column of the Sales sheet, each in a block of rows of its
 *                           own, which recalculates the revenue, the SUMIFS tables of the Charts
 *                           sheet and their charts. The other users view the Sales sheet where the
 *                           editor before them types, the Pivot sheet or the Charts sheet, a third
 *                           of them each.
 *
 * The user types where the application takes text: at the end of a Writer document, in the empty
 * cell below the last one of a Calc sheet (each word goes in a cell of its own, committed with
 * Enter), and in the text of the first object of an Impress slide. Two users of one document type
 * in different cells or objects. In a slide the user waits until no other view locks the object,
 * since a view closed a moment ago still locks the object it edited.
 *
 * Run k6 on another machine than the server, or the load generator takes the CPU it is measuring.
 * Check the server's own limits before reading a result as capacity: MAX_CONNECTIONS and
 * MAX_DOCUMENTS from configure (--with-max-connections, --with-max-documents) refuse sessions above
 * them, and num_prespawn_children in coolwsd.xml decides how many kits wait ready.
 *
 * Env:
 *   CAP_VUS           - comma-separated user plateaus                (default "10,20,...,160")
 *   CAP_RAMP          - time to climb to each plateau                (default "20s")
 *   CAP_HOLD          - time held at each plateau                    (default "70s")
 *   CAP_SESSION       - seconds a user keeps the document open       (default 300)
 *   CAP_USERS_PER_DOC - users editing the same document              (default 1)
 *   CAP_FILE_ID       - WOPI file id to open                         (default 11, blank.odt)
 *   CAP_SCENARIO      - a name from SCENARIOS, which replaces CAP_FILE_ID and CAP_USERS_PER_DOC
 *   CAP_DOCS          - the documents of a scenario of your own, as "<file id>:<users>,..."; the
 *                       list repeats for as many users as there are
 *   CAP_EDITOR_EVERY  - in a shared scenario, one user in this many edits  (default 10)
 *   CAP_ZOOMS         - zoom levels of the users, each user taking one of the list; a level
 *                       listed twice is twice as common  (default 1, and for finance-shared
 *                       0.75,1,1,1.25,1.5,2)
 *   CAP_THINK_MIN_MS, CAP_THINK_MAX_MS - pause between two words      (default 2000, 8000)
 *   CAP_TYPE_STEP_MS  - pause between two characters of a word       (default 150)
 *   CAP_EDIT_P95_MS   - edit latency target, 95th percentile         (default 300)
 *   CAP_LOAD_P95_MS   - document load target, 95th percentile        (default 10000, and 60000
 *                       when the documents include real ones, which take tens of seconds to
 *                       load for one user)
 *   CAP_TARGET_FACTOR - multiplies the default edit and load targets (default 1), for a server
 *                       that a profiler slows down
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

// Steps of 10 users up to 160, so that the capacity lands within 10 users.
const PLATEAUS = (__ENV['CAP_VUS'] || Array.from({ length: 16 }, (_, i) => 10 * (i + 1)).join(','))
    .split(',').map((s) => Number(s.trim())).filter((n) => n > 0);
const RAMP = __ENV['CAP_RAMP'] || '20s';
const HOLD = __ENV['CAP_HOLD'] || '70s';
const SESSION_MS = num('CAP_SESSION', 300) * 1000;
const USERS_PER_DOC = Math.max(1, num('CAP_USERS_PER_DOC', 1));
const FILE_ID = __ENV['CAP_FILE_ID'] || 11;  // documents/blank.odt

// The WOPI file ids of server/routes/files.json, by format: the 5 MB documents of gen-docs.py.
const ODT = 12, DOCX = 19, ODS = 14, XLSX = 20, ODP = 16, PPTX = 21;
// impress-slides-5mb.odp: 40 slides in about 5 MB, one for each editor of impress-shared
const SLIDES = 24;
// calc-finance.ods: the sheets Sales, Pivot and Charts, and 12000 order lines from row 2 of Sales
const FINANCE = 25;
const FINANCE_SHEETS = 3;
// the rows of Sales between two editors: 160 users have 16 editors
const FINANCE_ROW_STEP = 700;
// The real documents of documents/reference-docs.json, which documents/fetch-reference-docs.py
// downloads into documents/ref/.
// impress-gifs.pptx and impress-gead.pptx are left out: their last slide, where a user of a deck
// types, has no text.
const RTL_BOOK = 30, ODF_SPEC = 31, GUIDE = 32, FORMULAS = 34, MATRIX = 35, YOTTADB = 38,
    CANDY = 39, FONTS = 40, BIG_TABLE = 41, SHEETS = 43, MULTILINE = 44, SLIDE_FILL = 45;
// The file ids of all the real documents, those of CAP_DOCS too.
const FIRST_REAL = 30, LAST_REAL = 46;
const REFERENCE = [RTL_BOOK, FORMULAS, YOTTADB, ODF_SPEC, MATRIX, CANDY, GUIDE];
// The OOXML ones: three of each of Writer and Calc, and the one deck with text on its last slide.
const REFERENCE_OOXML = [RTL_BOOK, SHEETS, SLIDE_FILL, FONTS, MATRIX, BIG_TABLE, MULTILINE];
// One user on each document of the first list, and half of the users two by two in the second.
const alone = (a, b) => [[a, 1], [b, 1]];
const pairs = (a, b) => [[a, 1], [b, 1], [a, 2], [a, 1], [b, 1], [b, 2]];
// Six documents with one user, then four with two. The four shared ones change format over three
// rounds, so that every format is shared twice in 30 documents.
const mixed = [[ODT, DOCX, ODS, XLSX], [ODP, PPTX, ODT, DOCX], [ODS, XLSX, ODP, PPTX]].flatMap(
    (shared) => [ODT, DOCX, ODS, XLSX, ODP, PPTX].map((id) => [id, 1])
        .concat(shared.map((id) => [id, 2])));

// Each scenario is a list of [file id, users of that document], which repeats for every user.
const SCENARIOS = {
    'writer': alone(ODT, DOCX),
    'calc': alone(ODS, XLSX),
    'impress': alone(ODP, PPTX),
    'writer-pairs': pairs(ODT, DOCX),
    'calc-pairs': pairs(ODS, XLSX),
    'impress-pairs': pairs(ODP, PPTX),
    'mixed': mixed,
    'reference': REFERENCE.map((id) => [id, 1]),
    'reference-pairs': REFERENCE.flatMap((id) => [[id, 1], [id, 2]]),
    'reference-ooxml': REFERENCE_OOXML.map((id) => [id, 1]),
    'reference-ooxml-pairs': REFERENCE_OOXML.flatMap((id) => [[id, 1], [id, 2]]),
};
// Every user on one document. One user in CAP_EDITOR_EVERY edits, each in a part of its own, and
// the users after it, up to the next editor, view the part where it types.
const SHARED = {
    'writer-shared': ODT, 'calc-shared': ODS, 'impress-shared': SLIDES, 'finance-shared': FINANCE,
    'rtl-book-shared': RTL_BOOK, 'formulas-shared': FORMULAS, 'yottadb-shared': YOTTADB,
};
const EDITOR_EVERY = Math.max(1, num('CAP_EDITOR_EVERY', 10));
// The paragraphs between two editors of a shared Writer document: writer-5mb.odt has 136, and
// 160 users have 16 editors.
const PARAGRAPH_STEP = 8;
const SCENARIO = __ENV['CAP_SCENARIO'] || (__ENV['CAP_DOCS'] ? 'custom' : null);
const SHARED_FILE = SHARED[SCENARIO] ?? null;
if (SCENARIO && SCENARIO !== 'custom' && !SCENARIOS[SCENARIO] && !SHARED_FILE) {
    throw new Error(`CAP_SCENARIO=${SCENARIO} is not one of `
        + `${[...Object.keys(SCENARIOS), ...Object.keys(SHARED)].join(', ')}`);
}
const DOCS = SCENARIO === 'custom'
    ? __ENV['CAP_DOCS'].split(',').map((s) => s.split(':').map((v) => v.trim()))
        .map(([id, users]) => [id, Math.max(1, Number(users) || 1)])
    : SHARED_FILE ? [[SHARED_FILE, 1]] : SCENARIOS[SCENARIO] ?? [[FILE_ID, USERS_PER_DOC]];
// The users of one round of DOCS, each as [index of its document in DOCS, seat in that document].
const SEATS = DOCS.flatMap(([, users], doc) => Array.from({ length: users }, (_, seat) => [doc, seat]));
const THINK_MIN_MS = num('CAP_THINK_MIN_MS', 2000);
const THINK_MAX_MS = num('CAP_THINK_MAX_MS', 8000);
const TYPE_STEP_MS = num('CAP_TYPE_STEP_MS', 150);
const ZOOMS = (__ENV['CAP_ZOOMS'] || (SHARED_FILE === FINANCE ? '0.75,1,1,1.25,1.5,2' : '1'))
    .split(',').map((z) => Number(z.trim())).filter((z) => z > 0);
const TARGET_FACTOR = num('CAP_TARGET_FACTOR', 1);
const EDIT_P95_MS = num('CAP_EDIT_P95_MS', 300 * TARGET_FACTOR);
const REAL_DOCUMENTS = DOCS.some(([id]) => Number(id) >= FIRST_REAL && Number(id) <= LAST_REAL);
const LOAD_P95_MS = num('CAP_LOAD_P95_MS', (REAL_DOCUMENTS ? 60000 : 10000) * TARGET_FACTOR);
const MAX_ERRORS = num('CAP_MAX_ERRORS', 0.01);
// The cores and memory of the server, which capacity-profile sets, for the summary.
const SERVER = { cpus: __ENV['CAP_SERVER_CPUS'] || null, memory: __ENV['CAP_SERVER_MEMORY'] || null };

// The server answers a load with status: well within limit_load_secs, 100 s by default.
const LOAD_TIMEOUT_MS = 100000;
const EDIT_TIMEOUT_MS = 10000;
// How long a space waits for its cursor message after the tiles under the cursor were invalidated,
// before that invalidation counts as its answer.
const CURSOR_GRACE_MS = 1000;
// the rectangle of an invalidatetiles: message, in twips
const TILES_RECT = /\bx=(-?\d+) y=(-?\d+) width=(-?\d+) height=(-?\d+)/;
// The browser gives up on a document that is still unloading after this many retries.
const MAX_UNLOADING_RETRIES = 10;
// The wait of a user after a session that failed, before it opens the document again. A server
// that refuses every connection fails a session at once, and without the wait each user would
// fail and log hundreds of sessions a second.
const FAILED_SESSION_PAUSE_MS = 5000;
// How long a user of a presentation waits for another view to release the object it selected.
const LOCK_WAIT_MS = 30000;
const TILE_REQUEST_GAP_MS = 200;

// The visible area of a desktop browser window at 100% zoom.
const TILE_PX = 256;
const TILE_TWIPS = 3840;
const COLS = 5;
const ROWS = 4;

// The key codes of the browser client, from vcl's KEY_ constants.
const KEY_DOWN = 1024, KEY_RIGHT = 1027, KEY_HOME = 1028, KEY_RETURN = 1280, KEY_TAB = 1282;
// Ctrl, added to a key code
const KEY_MOD1 = 8192;

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
const loadRetries = new Counter('load_retries');

// The plateau whose hold is under way, or 'ramp' between two holds.
function plateauTag() {
    const t = exec.instance.currentTestRunDuration / 1000;
    const hold = HOLDS.find((h) => t >= h.start && t < h.end);
    return { plateau: hold ? String(hold.users) : 'ramp' };
}

// The plateau that a failure counts against: the hold under way, or the one that the ramp leads
// to, so that a session that fails while users join counts against the step they join.
function failureTag() {
    const t = exec.instance.currentTestRunDuration / 1000;
    const hold = HOLDS.find((h) => t < h.end);
    return { plateau: hold ? String(hold.users) : 'ramp' };
}

function randomBetween(low, high) {
    return low + Math.random() * (high - low);
}

// Users of one document get the same WOPISrc. COOL keys the document on the WOPISrc path and
// ignores its query, so the document number goes in the path: the WOPI host serves the file of
// <id> for "<id>~<group>". ramping-vus does not start its VUs in the order of their ids, so the
// WOPI host numbers the users in the order they first ask, and the users of one document arrive
// one after the other. A VU keeps its document when it opens it again.
let group = null;
let seat = 0;
let fileId = null;
// whether the user types, and the part it works in: the cell, object, slide or paragraph of
// its seat, or in a shared document the one of its editor
let editor = true;
let place = 0;
// the sheet or slide that the user shows, and its zoom level
let part = 0;
let zoom = 1;
function groupWopiSrc() {
    if (group === null) {
        const base = String(wopiHost).replace(/\/?$/, '/');
        const res = http.get(`${base}capacity/group?size=1`);
        const user = res.status === 200 ? res.json('group') : exec.vu.idInTest - 1;
        if (SHARED_FILE) {
            group = 0;
            seat = user;
            fileId = SHARED_FILE;
            editor = user % EDITOR_EVERY === 0;
            place = Math.floor(user / EDITOR_EVERY);
            // The editors type in the Sales sheet, and the users after each one take the three
            // sheets in turn.
            if (SHARED_FILE === FINANCE && !editor) part = (user % EDITOR_EVERY) % FINANCE_SHEETS;
        } else {
            const [doc, s] = SEATS[user % SEATS.length];
            group = Math.floor(user / SEATS.length) * DOCS.length + doc;
            seat = s;
            place = s;
            fileId = DOCS[doc][0];
        }
        // Users next to each other take different levels, whatever their role.
        zoom = ZOOMS[(Math.imul(user + 1, 2654435761) >>> 0) % ZOOMS.length] ?? 1;
    }
    return getWopiSrc(wopiHost, `${fileId}~${group}`).toString();
}

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

export default async function () {
    let wopiClient;
    try {
        wopiClient = await getWopiClientUrl(wopiUrl);
    } catch (e) {
        console.warn(`FAIL reason=discovery error=${JSON.stringify(String(e))} `
            + `vu=${exec.vu.idInTest}`);
        sessionErrors.add(1, failureTag());
        await new Promise((resolve) => setTimeout(resolve, FAILED_SESSION_PAUSE_MS));
        return;
    }
    const wopiSrc = groupWopiSrc();

    const failed = await new Promise((resolve) => {
        const s = {
            openedAt: 0, loaded: false, failed: false, closing: false, done: false,
            editSentAt: 0, editChar: '', word: '', charIndex: 0,
            // when the tiles under the cursor were invalidated while a space waited, or 0
            spaceTilesAt: 0,
            // the last cursor rectangle of this view, [x, y, width, height] in twips
            cursor: null,
            lastTileRequest: 0, viewLeft: 0, viewTop: 0, processedWids: [], timers: new Set(),
            // the twips that a tile of TILE_PX pixels shows at the zoom level of the user
            tileTwips: Math.round(TILE_TWIPS / zoom),
            // the document type of the status: message: text, spreadsheet or presentation
            kind: 'text',
            // this view, the objects that other views lock, the object selected to type in, and
            // whether the user types yet
            viewId: null, locks: new Map(), object: null, editing: true,
            // the parts of the document, which name a slide to show by its identifier
            parts: [],
            // the keystrokes answered in this session
            edits: 0,
            // the loads asked for again, and whether the next one is due
            retries: 0, retrying: false,
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
            // A session that ends well was counted at the end of each hold it lasted through.
            if (s.failed) sessionErrors.add(1, failureTag());
            resolve(s.failed);
        }

        // One FAIL line for each failed session, with what failed and where, since the summary
        // only counts them.
        function fail(reason) {
            if (s.failed) return;
            s.failed = true;
            const kept = s.openedAt ? Date.now() - s.openedAt : 0;
            console.warn(`FAIL reason=${reason} doc=${fileId}~${group} seat=${seat} `
                + `role=${editor ? 'editor' : 'viewer'} part=${part} zoom=${zoom} kind=${s.kind} `
                + `loaded=${s.loaded} editing=${s.editing} edits=${s.edits} retries=${s.retries} `
                + `after_ms=${kept} `
                + `vu=${exec.vu.idInTest}`);
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
                    xs.push(s.viewLeft + c * s.tileTwips);
                    ys.push(s.viewTop + r * s.tileTwips);
                }
            }
            client.send(`tilecombine nviewid=0 part=${part} width=${TILE_PX} height=${TILE_PX} `
                + `tileposx=${xs.join(',')} tileposy=${ys.join(',')} `
                + `tilewidth=${s.tileTwips} tileheight=${s.tileTwips}`);
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
            client.send(`clientvisiblearea x=${s.viewLeft} y=${s.viewTop} width=${COLS * s.tileTwips} `
                + `height=${ROWS * s.tileTwips}`);
        }

        // Moves the visible area to a cursor outside it: one tile row of the area stays above the
        // cursor row, and one tile column left of it, as after the browser client scrolls to the
        // cursor. The rectangle is "x, y, width, height" in twips.
        function followCursor(rect) {
            const [x, y] = rect.split(',').map(Number);
            if (!Number.isFinite(x) || !Number.isFinite(y)) return;
            const tile = s.tileTwips;
            if (y >= s.viewTop && y < s.viewTop + ROWS * tile
                && x >= s.viewLeft && x < s.viewLeft + COLS * tile) return;
            s.viewTop = Math.max(0, Math.floor(y / tile) - 1) * tile;
            s.viewLeft = x < COLS * tile ? 0 : (Math.floor(x / tile) - 1) * tile;
            sendVisibleArea();
            s.lastTileRequest = 0;
            requestVisibleTiles();
        }

        function nextWord() {
            if (s.closing) return;
            if (Date.now() - s.openedAt >= SESSION_MS) { close(); return; }
            later(() => {
                // The ledger takes a number of units, the other documents a word.
                s.word = SHARED_FILE === FINANCE ? `${1 + Math.floor(Math.random() * 60)} `
                    : WORDS[Math.floor(Math.random() * WORDS.length)] + ' ';
                s.charIndex = 0;
                typeChar();
            }, randomBetween(THINK_MIN_MS, THINK_MAX_MS));
        }

        function pressKey(char, key) {
            client.send(`key type=input char=${char} key=${key}`);
            client.send(`key type=up char=0 key=${key}`);
        }

        // Puts the cursor where the user types: the end of a text document, the empty cell below
        // the used area of a sheet, or the text of an object of the first slide. The second user of
        // a document takes the next cell or the next object. In a shared document each editor
        // takes a paragraph, a column or a slide of its own, and its viewers go to the same place
        // without typing, so that its edits change what they view.
        function placeCursor() {
            if (SHARED_FILE === FINANCE) {
                // The Units cell of the first row of the block of the editor, or the top of the
                // Pivot and Charts sheets.
                client.send(`setclientpart part=${part}`);
                const cell = part === 0 ? `J${2 + place * FINANCE_ROW_STEP}` : 'A1';
                client.send(`uno .uno:GoToCell ${JSON.stringify(
                    { ToPoint: { type: 'string', value: cell } })}`);
            } else if (s.kind === 'spreadsheet') {
                // Ctrl+End: in a sheet .uno:GoToEndOfDoc does not move, and the end is the last
                // used cell.
                client.send('uno .uno:GoToEndOfData');
                pressKey(0, KEY_DOWN);
                pressKey(0, KEY_HOME);
                for (let i = 0; i < place; i++) pressKey(0, KEY_RIGHT);
            } else if (s.kind === 'presentation' && SHARED_FILE) {
                // A slide is named by its identifier in the status, or by its index in an older
                // coolwsd.
                const slide = place % Math.max(1, s.parts.length);
                client.send(`setclientpart part=${s.parts[slide]?.part ?? slide}`);
                if (editor) pressKey(9, KEY_TAB);
            } else if (s.kind === 'presentation') {
                client.send('uno .uno:GoToEndOfDoc');
                for (let i = 0; i <= place; i++) pressKey(9, KEY_TAB);
            } else if (SHARED_FILE) {
                client.send('uno .uno:GoToStartOfDoc');
                // Ctrl+Down: the start of the next paragraph
                for (let i = 0; i < place * PARAGRAPH_STEP; i++) pressKey(0, KEY_DOWN | KEY_MOD1);
            } else {
                client.send('uno .uno:GoToEndOfDoc');
            }
            if (s.kind === 'presentation' && editor) {
                // Tab selected the next object of the slide, and Enter starts editing its text
                // once no other view locks the object. A view closed a moment ago keeps its lock
                // until the kit destroys it, a few seconds later, and Enter on a locked object
                // does not edit it.
                s.editing = false;
                later(startEditing, LOCK_WAIT_MS);
            }
        }

        function startEditing() {
            if (s.editing || s.closing || !editor) return;
            s.editing = true;
            pressKey(13, KEY_RETURN);
            nextWord();
        }

        // The keystroke in flight has its answer: record it and type the next character.
        function editAnswered(now = Date.now()) {
            if (!s.editSentAt) return;
            editLatency.add(now - s.editSentAt, plateauTag());
            // One line per edit, for lining slow edits up with what the server did then.
            console.log(`EDIT t_ms=${now} ms=${now - s.editSentAt} doc=${fileId}~${group} `
                + `vu=${exec.vu.idInTest}`);
            s.editSentAt = 0;
            s.edits++;
            later(typeChar, TYPE_STEP_MS);
        }

        // Whether the rectangle of an invalidatetiles: message overlaps this view's cursor.
        function coversCursor(message) {
            const m = TILES_RECT.exec(message);
            if (!s.cursor || !m) return false;
            const [x, y, w, h] = m.slice(1).map(Number);
            const [cx, cy, cw, ch] = s.cursor;
            return x < cx + cw && cx < x + w && y < cy + ch && cy < y + h;
        }

        function lockedByOther(object) {
            for (const [view, locked] of s.locks) if (view !== s.viewId && locked === object) return true;
            return false;
        }

        // One character at a time, the next only once the server answered the last one, so a slow
        // server shows as a longer edit_latency rather than as a queue of unanswered keystrokes.
        function typeChar() {
            if (s.closing) return;
            if (s.charIndex >= s.word.length) { nextWord(); return; }
            const ch = s.word[s.charIndex++];
            s.editSentAt = Date.now();
            s.editChar = ch;
            s.spaceTilesAt = 0;
            if (ch === ' ' && s.kind === 'spreadsheet') {
                pressKey(13, KEY_RETURN);
            } else if (ch === ' ') {
                pressKey(32, 0);
            } else {
                client.send(`textinput id=0 text=${ch}`);
            }
            const sentAt = s.editSentAt;
            later(() => {
                if (s.editSentAt === sentAt) {
                    editTimeouts.add(1, plateauTag());
                    fail(`edit-timeout char=${JSON.stringify(ch)}`);
                }
            }, EDIT_TIMEOUT_MS);
        }

        // From the start of the session, so that a socket that never opens fails too.
        later(() => { if (!s.loaded) fail('load-timeout'); }, LOAD_TIMEOUT_MS);
        // A session counts once for each hold that ends while it is open and has not failed, so
        // that a hold shorter than a session still has the sessions that went well in its rate.
        const now = exec.instance.currentTestRunDuration;
        for (const { users, end } of HOLDS) {
            if (end * 1000 > now) {
                later(() => { if (!s.failed) sessionErrors.add(0, { plateau: String(users) }); },
                    end * 1000 - now);
            }
        }
        // The server refuses a document that it is still unloading from the last session, and the
        // browser asks for it again after .5, 2, 4.5 ... seconds, ten times at most.
        const isUnloading = (text) => String(text ?? '').includes('kind=docunloading');
        function retryUnloading() {
            if (s.retrying) return;
            if (s.retries >= MAX_UNLOADING_RETRIES) {
                fail('docunloading');
                return;
            }
            s.retries++;
            loadRetries.add(1, plateauTag());
            s.retrying = true;
            try { client.close(); } catch (e) { /* already closed */ }
            later(() => { s.retrying = false; connect(); }, 500 * s.retries * s.retries);
        }

        // A new socket for each attempt to load. The events of an earlier socket are ignored.
        function connect() {
            const c = new CoolClientWs(wopiClient, wopiSrc, () => {
                // The load time counts from the first attempt, as the user waits through all.
                if (!s.openedAt) s.openedAt = Date.now();
                c.openDocument(wopiSrc);
            });
            client = c;

            // Text frames are protocol messages, binary frames are tiles.
            c.socket.binaryType = 'arraybuffer';
            c.socket.onerror = (e) => {
                console.error(`WebSocket error: ${e.error}`);
                if (c === client && !s.loaded && isUnloading(e.error)) retryUnloading();
            };
            c.socket.onmessage = (e) => {
                if (c !== client) return;
                if (!s.loaded && typeof e.data === 'string' && isUnloading(e.data)) {
                    retryUnloading();
                    return;
                }
                if (typeof e.data !== 'string') {
                    if (e.data instanceof ArrayBuffer) tileReceived(e.data);
                    return;
                }
                if (!s.loaded && e.data.startsWith('status:')) {
                    s.loaded = true;
                    docLoadTime.add(Date.now() - s.openedAt, plateauTag());
                    client.send(`clientzoom tilepixelwidth=${TILE_PX} tilepixelheight=${TILE_PX} `
                        + `tiletwipwidth=${s.tileTwips} tiletwipheight=${s.tileTwips}`);
                    // The status is JSON, with "type" as in the older "type=" form of the message.
                    const type = /"type"\s*:\s*"(\w+)"|\btype=(\w+)/.exec(e.data);
                    s.kind = type?.[1] ?? type?.[2] ?? 'text';
                    s.viewId = /"viewid"\s*:\s*(\d+)/.exec(e.data)?.[1] ?? null;
                    try {
                        s.parts = JSON.parse(e.data.substring(e.data.indexOf('{'))).parts ?? [];
                    } catch (err) { /* the older status: of key=value pairs */ }
                    sendVisibleArea();
                    placeCursor();
                    requestVisibleTiles();
                    // A viewer keeps the document open for the session, without typing.
                    if (!editor) later(close, SESSION_MS);
                    else if (s.editing) nextWord();
                } else if (e.data.startsWith('viewlock:')) {
                    try {
                        const lock = JSON.parse(e.data.substring(e.data.indexOf(':') + 1));
                        if (lock.rectangle === 'EMPTY') s.locks.delete(String(lock.viewId));
                        else s.locks.set(String(lock.viewId), String(lock.objectId));
                    } catch (err) { /* not JSON */ }
                    if (s.object && !lockedByOther(s.object)) startEditing();
                } else if (e.data.startsWith('graphicselection:') && !s.editing) {
                    s.object = /"id"\s*:\s*"(\w+)"/.exec(e.data)?.[1] ?? null;
                    if (s.object && !lockedByOther(s.object)) startEditing();
                } else if (e.data.startsWith('invalidatecursor:')
                           || (s.kind === 'spreadsheet' && e.data.startsWith('cellcursor:'))) {
                    // A sheet answers Enter by moving the cell cursor rather than the text cursor.
                    const payload = e.data.substring(e.data.indexOf(':') + 1).trim();
                    let rect = payload;
                    try { rect = JSON.parse(payload).rectangle ?? ''; } catch (err) { /* plain rectangle */ }
                    followCursor(String(rect));
                    const box = String(rect).split(',').map(Number);
                    s.cursor = box.length === 4 && box.every(Number.isFinite) ? box : null;
                    editAnswered();
                } else if (e.data.startsWith('invalidatetiles:')) {
                    // "part=<n> ..." or "EMPTY, <n>, <mode>". A sheet that the user does not show
                    // needs no tiles, as in the browser client.
                    const m = /part=(-?\d+)|EMPTY,\s*(-?\d+)/.exec(e.data);
                    const invalidated = m ? Number(m[1] ?? m[2]) : part;
                    // The kit drops an invalidatecursor: that repeats the last one, so a space
                    // that does not move the cursor, as at the end of a line, gets no cursor
                    // message. When none follows the repaint under the cursor, that repaint
                    // answers the space. A sheet gets Enter for a space, which moves the cell
                    // cursor.
                    if (s.editSentAt && s.editChar === ' ' && s.kind !== 'spreadsheet'
                        && !s.spaceTilesAt && coversCursor(e.data)) {
                        s.spaceTilesAt = Date.now();
                        const sentAt = s.editSentAt;
                        later(() => {
                            if (s.editSentAt === sentAt) editAnswered(s.spaceTilesAt);
                        }, CURSOR_GRACE_MS);
                    }
                    if (SHARED_FILE !== FINANCE || invalidated === part || invalidated < 0) {
                        requestVisibleTiles();
                    }
                }
            };
            c.socket.onclose = (e) => {
                if (c !== client || s.retrying) return;
                if (!s.loaded && isUnloading(e?.reason)) {
                    retryUnloading();
                    return;
                }
                if (!s.closing) fail(`socket-closed code=${e?.code ?? '-'}`);
                finish();
            };
        }
        connect();
    });
    if (failed) await new Promise((resolve) => setTimeout(resolve, FAILED_SESSION_PAUSE_MS));
}

export function handleSummary(data) {
    const metric = (name, users) => data.metrics[`${name}{plateau:${users}}`];
    const passed = (m) => !m || !m.thresholds || Object.values(m.thresholds).every((t) => t.ok);
    const ms = (v) => (v === undefined ? '-' : `${Math.round(v)} ms`);

    const rows = HOLDS.map(({ users, start, end }) => {
        const edit = metric('edit_latency', users);
        const load = metric('doc_load_time', users);
        const errors = metric('session_errors', users);
        return {
            users,
            // the hold, in seconds from the start of the test
            start,
            end,
            // The thresholds declare every plateau's metrics, so a metric exists without samples.
            reached: (edit?.values.count ?? 0) > 0 || (load?.values.count ?? 0) > 0
                || (errors?.values.passes ?? 0) + (errors?.values.fails ?? 0) > 0,
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
    const on = SERVER.cpus ? ` on cores ${SERVER.cpus} and ${SERVER.memory}` : ' on a server of unknown size';
    lines.push('', `capacity: ${capacity} users${on} (targets: edit p95 < ${EDIT_P95_MS} ms, `
        + `load p95 < ${LOAD_P95_MS} ms, failed sessions < ${100 * MAX_ERRORS}%, `
        + `${SCENARIO ? `scenario ${SCENARIO}` : `${USERS_PER_DOC} user(s) per document`})`, '');

    return {
        stdout: textSummary(data, { indent: ' ', enableColors: true }) + lines.join('\n'),
        'capacity.json': JSON.stringify({
            capacity, server: SERVER, usersPerDocument: SCENARIO ? null : USERS_PER_DOC,
            // Wall time of the start of the test, in ms: the plateau times count from it.
            startMs: Date.now() - data.state.testRunDurationMs,
            scenario: SCENARIO,
            zooms: ZOOMS,
            documents: SHARED_FILE ? [{ fileId: SHARED_FILE, users: 'all', editorEvery: EDITOR_EVERY }]
                : DOCS.map(([id, users]) => ({ fileId: id, users })),
            plateaus: rows,
        }, null, 2),
    };
}
