/**
 * Large-document load test with a varying number of VUs.
 *
 * Each VU opens the big mixed-content Writer document (fileId=5,
 * large-mixed-10mb.odt) and scrolls through it, pulling the tiles of every
 * screen - so each iteration drives a real import + layout + tile render. The
 * number of concurrent VUs follows a staircase (ramping-vus): it climbs to
 * each plateau in LOAD_VUS, holds, then climbs to the next, so you can watch
 * where load time degrades as concurrency rises.
 *
 * Independent sessions by default: each iteration uses a unique WOPISrc (a
 * per-VU / per-iteration query parameter the WOPI host ignores but COOL keys
 * the document on), so every iteration is its own document with its own kit -
 * a true concurrent-load test. Set LOAD_SHARED=1 to have every VU join one
 * shared document instead (many viewers on a single kit).
 *
 * All socket work is driven from event-loop timers and the iteration waits on
 * a Promise (never a blocking sleep), so queued frames flush and incoming
 * tiles drain - a blocking loop here would stall the socket before the kit is
 * exercised.
 *
 * Env:
 *   LOAD_VUS   - comma-separated VU plateaus            (default "2,5,10")
 *   LOAD_RAMP  - time to climb to each plateau          (default "30s")
 *   LOAD_HOLD  - time held at each plateau              (default "1m")
 *   LOAD_SHARED=1 - all VUs join one shared document    (default independent)
 *   LOAD_PASSES   - full-document scroll passes / iter  (default 1)
 *   LOAD_FILE_ID  - WOPI file id to open                (default 5)
 */

import exec from 'k6/execution';
import { Trend, Counter } from 'k6/metrics';
import { URL } from 'https://jslib.k6.io/url/1.0.0/index.js';

import { checkWopi, getWopiClientUrl, getWopiSrc } from '../lib/wopi_discovery.js';
import { CoolClientWs } from '../lib/cool_client_ws.js';
import { wopiHost, wopiUrl } from './config.js';

const FILE_ID = __ENV['LOAD_FILE_ID'] || 5;
const PASSES = Math.max(1, Number(__ENV['LOAD_PASSES'] ?? 1));
const SHARED = __ENV['LOAD_SHARED'] === '1';

// The staircase: a ramp then a hold at each plateau in LOAD_VUS, then down.
const TARGETS = (__ENV['LOAD_VUS'] || '2,5,10')
    .split(',').map((s) => Number(s.trim())).filter((n) => n > 0);
const RAMP = __ENV['LOAD_RAMP'] || '30s';
const HOLD = __ENV['LOAD_HOLD'] || '1m';

// Tile geometry at 100% zoom (see cool-load-document-test.js).
const TILE_PX = 256;
const TILE_TWIPS = 3840;
const COLS = 4;
const ROWS = 6;
const VIEW_W = COLS * TILE_TWIPS;
const VIEW_H = ROWS * TILE_TWIPS;
const PAGE_TWIPS = 16838;
const DOC_END = PAGE_TWIPS * 44;

const IMPORT_WAIT_MS = Number(__ENV['LOAD_IMPORT_WAIT_MS'] ?? 3000);
const STEP_MS = Number(__ENV['LOAD_STEP_MS'] ?? 120);
const DRAIN_MS = 2000;
const ITER_TIMEOUT_MS = Number(__ENV['LOAD_ITER_TIMEOUT_MS'] ?? 120000);

function buildStages() {
    const stages = [];
    for (const t of TARGETS) {
        stages.push({ duration: RAMP, target: t });
        stages.push({ duration: HOLD, target: t });
    }
    stages.push({ duration: '20s', target: 0 });
    return stages;
}

export const options = {
    insecureSkipTLSVerify: true,
    scenarios: {
        ramp: {
            executor: 'ramping-vus',
            startVUs: 0,
            stages: buildStages(),
            gracefulRampDown: '10s',
        },
    },
};

// open -> "status:" (document loaded, dimensions known)
const docLoadTime = new Trend('doc_load_time', true);
// open -> first tile bytes back (first paint)
const firstTileTime = new Trend('first_tile_time', true);
const docLoads = new Counter('doc_loads');

export function setup() {
    checkWopi(wopiHost, wopiUrl);
}

// Ask the server for the tiles of one screen (parallel tileposx/tileposy).
function requestScreen(client, yTop, part) {
    const xs = [];
    const ys = [];
    const yBase = Math.floor(yTop / TILE_TWIPS) * TILE_TWIPS;
    for (let c = 0; c < COLS; c++) {
        for (let r = 0; r < ROWS; r++) {
            xs.push(c * TILE_TWIPS);
            ys.push(yBase + r * TILE_TWIPS);
        }
    }
    client.send(`clientvisiblearea x=0 y=${yBase} width=${VIEW_W} height=${VIEW_H}`);
    client.send(`tilecombine nviewid=0 part=${part} width=${TILE_PX} height=${TILE_PX} `
        + `tileposx=${xs.join(',')} tileposy=${ys.join(',')} `
        + `tilewidth=${TILE_TWIPS} tileheight=${TILE_TWIPS}`);
}

// A unique WOPISrc so COOL treats this iteration as its own document (its own
// kit); the WOPI host ignores the extra query parameter and serves fileId.
function sessionWopiSrc() {
    const src = new URL(getWopiSrc(wopiHost, FILE_ID).toString());
    if (!SHARED) {
        src.searchParams.set('sess', `${exec.vu.idInTest}-${exec.vu.iterationInScenario}`);
    }
    return src.toString();
}

export default async function () {
    const wopiClient = await getWopiClientUrl(wopiUrl);
    const wopiSrc = sessionWopiSrc();

    await new Promise((resolve) => {
        const state = { y: 0, pass: 0, timer: null, safety: null,
                        openedAt: 0, gotStatus: false, gotTile: false, done: false };
        let client;

        function done() {
            if (state.done) return;
            state.done = true;
            if (state.timer) clearInterval(state.timer);
            if (state.safety) clearTimeout(state.safety);
            // Let the last tiles arrive, then close and end the iteration.
            setTimeout(() => { try { client.close(); } catch (e) { /* closing */ } resolve(); }, DRAIN_MS);
        }

        function tick() {
            // Stop if the socket is gone (server closed it, or we are closing).
            if (state.done || !client || client.socket.readyState !== 1) { done(); return; }
            if (state.y >= DOC_END) {
                state.pass += 1;
                state.y = 0;
                if (state.pass >= PASSES) { done(); return; }
            }
            requestScreen(client, state.y, 0);
            state.y += VIEW_H;
        }

        client = new CoolClientWs(wopiClient, wopiSrc, () => {
            state.openedAt = Date.now();
            docLoads.add(1);
            client.openDocument(wopiSrc);
            client.send('commandvalues command=.uno:CharFontName');
            client.send(`clientzoom tilepixelwidth=${TILE_PX} tilepixelheight=${TILE_PX} `
                + `tiletwipwidth=${TILE_TWIPS} tiletwipheight=${TILE_TWIPS}`);
            // Start scrolling once the kit has had time to import and lay out.
            setTimeout(() => { state.timer = setInterval(tick, STEP_MS); }, IMPORT_WAIT_MS);
        });

        // Measure load milestones and drain incoming traffic. Text frames are
        // protocol messages (status, progress); binary frames are tiles.
        client.socket.onmessage = (e) => {
            if (typeof e.data === 'string') {
                if (!state.gotStatus && e.data.startsWith('status:')) {
                    state.gotStatus = true;
                    docLoadTime.add(Date.now() - state.openedAt);
                }
            } else if (!state.gotTile) {
                state.gotTile = true;
                firstTileTime.add(Date.now() - state.openedAt);
            }
        };
        // If the socket drops on its own, end the iteration too.
        client.socket.onclose = () => {
            if (state.timer) clearInterval(state.timer);
            if (state.safety) clearTimeout(state.safety);
            if (!state.done) { state.done = true; resolve(); }
        };

        // Never let a wedged connection hold the VU past the timeout.
        state.safety = setTimeout(done, ITER_TIMEOUT_MS);
    });
}
