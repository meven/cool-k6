/**
 * Time a slide duplication the way the user sees it.
 *
 * Collabora Online sends no "done" for .uno:DuplicatePage, so this watches the
 * client from inside its frame and stamps, on the page clock, from the command:
 *
 *   slide_confirmed_ms   status: arrived with one more slide
 *   slide_shown_ms       the view switched to the new slide
 *   slide_duplicated_ms  the new slide is painted on the canvas (the budget)
 *   slide_thumbnail_ms   its thumbnail is rendered in the slide sorter
 *   slide_status_ms      the status bar says so (core coalesces it, ~0.4 s late)
 *
 * Users may share the document, so the new slide is followed by identity, not
 * position. Both client generations are handled: index parts with the
 * TileManager global, and GUID parts with RenderManager's instance. With
 * vector rendering the slide is painted once the vector content section has
 * drawn it from its primitives, as there are no tiles.
 *
 * Environment:
 *   DUPLICATES            duplicates per document open (default 5)
 *   DUPLICATE_TIMEOUT_MS  one duplicate's time limit (default 15000)
 */
import { sleep, check } from 'k6';
import { Trend, Counter } from 'k6/metrics';

import { record } from './browser_scenario.js';
import { screenshotPage } from './test_utils.js';

export const DUPLICATES = Number(__ENV['DUPLICATES'] || 5);
const TIMEOUT_MS = Number(__ENV['DUPLICATE_TIMEOUT_MS'] || 15000);
const TRAILING_GRACE_MS = 1500;   // for thumbnail and status bar after the paint
const SETTLE_TIMEOUT_MS = 10000;
const SETTLE_POLL_S = 0.25;
const POLL_S = 0.05;

/** In-page mark -> metric it feeds. */
const SIGNALS = {
    confirmed: 'slide_confirmed_ms',
    shown: 'slide_shown_ms',
    painted: 'slide_duplicated_ms',
    thumbnail: 'slide_thumbnail_ms',
    status_text: 'slide_status_ms',
};
export const DUPLICATE_METRICS = Object.values(SIGNALS);
export const DUPLICATE_FAILED_COUNTER = 'slide_duplicate_failed';

const trends = {};
for (const mark in SIGNALS) trends[mark] = new Trend(SIGNALS[mark], true);
const failed = new Counter(DUPLICATE_FAILED_COUNTER);

// ---- runs inside cool.html -------------------------------------------------

function installProbe() {
    if (window.__coolDup) return { caps: window.__coolDup.caps, info: window.__coolDup.info };
    const { map, impress, sectionContainer } = window.app;
    const layer = map._docLayer;
    const tiles = window.RenderManager ? window.RenderManager._instance : window.TileManager;
    const vector = !!(window.RenderManager && window.RenderManager.isVectorRendering
                      && window.RenderManager.isVectorRendering());
    const section = sectionContainer.getSectionWithName(
        vector ? window.app.CSections.VectorContent.name : 'tiles');
    const sorter = document.querySelector('#slide-sorter');

    const partCount = () => (impress.partList || []).length || layer._parts;
    const partId = (i) => (impress.partList && impress.partList[i] && impress.partList[i].part) ?? i;
    const tilesReady = () => (tiles.visibleTilesReady ? tiles.visibleTilesReady() : true);

    const D = { marks: {}, armed: false, partCount, partId, tilesReady };
    window.__coolDup = D;
    const mark = (name) => { if (D.armed && !(name in D.marks)) D.marks[name] = performance.now(); };
    // What the view did, for the warning when the new slide never gets painted.
    const trace = (what, index) => { if (D.armed) D.trace.push([Math.round(performance.now() - D.t0), what, String(partId(index)).slice(-6)]); };
    const grown = () => D.armed && partCount() > D.before.parts;

    // The server switches this view to its new slide: by setpart:, or already
    // inside the status: that announced it.
    const switched = (index) => {
        const id = partId(index);
        if (!D.armed || D.newId || !grown() || id === D.before.id) return;
        D.newId = id;
        D.newImg = layer._preview && layer._preview._previewTiles[index];
        mark('shown');
    };
    map.on('updateparts', () => { trace('status', layer._selectedPart); if (grown()) { mark('confirmed'); switched(layer._selectedPart); } });
    map.on('setpart', (e) => { trace('setpart', e.selectedPart); switched(e.selectedPart); });
    map.on('commandstatechanged', (e) => {
        if (e.commandName === '.uno:PageStatus' && grown()) mark('status_text');
    });

    // There is no paint event: wrap the section's draw and check what it drew.
    const draw = section.onDraw;
    section.onDraw = function () {
        const result = draw.apply(this, arguments);
        if (D.armed && !D.marks.painted && D.newId && partId(layer._selectedPart) === D.newId) {
            if (vector) {
                // The section draws the page when its primitives and its master are in.
                const mode = window.app.activeDocument.activeModes[0];
                if (window.RenderManager.isPartDrawable(layer._selectedPart, mode)) mark('painted');
                return result;
            }
            let visible = 0, drawn = 0;
            (tiles.tiles || tiles.getTiles()).forEach((t) => {
                if (t.distanceFromView !== 0 || t.coords.part !== D.newId) return;
                visible++;
                if (t.isReadyToDraw()) drawn++;
            });
            if (visible && drawn === visible) mark('painted');
        }
        return result;
    };

    new MutationObserver(() => {
        const src = D.newImg && D.newImg.getAttribute('src');
        if (src && src.startsWith('data:image/png')) mark('thumbnail');
    }).observe(sorter, { subtree: true, attributes: true, attributeFilter: ['src'] });

    D.caps = { duplicatePage: typeof map.duplicatePage === 'function', tiles: !!tiles, section: !!section, sorter: !!sorter };
    D.info = { editMode: !!(map.isEditMode && map.isEditMode()), vector,
               layout: window.app.activeDocument && window.app.activeDocument.activeLayout.type };
    return { caps: D.caps, info: D.info };
}

function startDuplicate() {
    const D = window.__coolDup;
    const layer = window.app.map._docLayer;
    D.marks = {};
    D.trace = [];
    D.newId = null;
    D.newImg = null;
    D.before = { parts: D.partCount(), id: D.partId(layer._selectedPart) };
    D.armed = true;
    D.t0 = performance.now();
    window.app.map.duplicatePage();
}

function readDuplicate() {
    const D = window.__coolDup;
    const layer = window.app.map._docLayer;
    const out = { grown: D.partCount() > D.before.parts };
    for (const name in D.marks) out[name] = Math.round(D.marks[name] - D.t0);
    if (!('painted' in D.marks)) {
        let visible = 0, drawn = 0;
        const tiles = window.RenderManager ? window.RenderManager._instance : window.TileManager;
        (tiles.tiles || tiles.getTiles()).forEach((t) => {
            if (t.coords.part !== D.newId) return;
            if (t.distanceFromView === 0) visible++;
            if (t.isReadyToDraw()) drawn++;
        });
        out.debug = { newId: String(D.newId).slice(-6), selected: String(D.partId(layer._selectedPart)).slice(-6),
                      tilesOfNew: { visible, drawn }, trace: D.trace };
    }
    return out;
}

function stopDuplicate() {
    window.__coolDup.armed = false;
}

function settleState() {
    const D = window.__coolDup;
    const imgs = [...document.querySelectorAll('#slide-sorter img.preview-img')];
    const fetched = imgs.filter((i) => (i.getAttribute('src') || '').startsWith('data:image/png')).length;
    return { ready: D.tilesReady(), fetched, parts: D.partCount() };
}

// ---- runs in k6 -------------------------------------------------------------

export { findCoolFrame } from './test_utils.js';

/** Install the hooks. False when this client cannot be probed. */
export async function armDuplicateProbe(frame) {
    const { caps, info } = await frame.evaluate(installProbe);
    if (!Object.values(caps).every(Boolean)) {
        console.warn(`duplicate probe cannot read this client: ${JSON.stringify(caps)}`);
        return false;
    }
    // A read-only view (Viewing mode, stacked slides) is not the scenario. It
    // is what the client falls back to when a save fails, so say so.
    if (!info.editMode) {
        console.warn(`document is not in edit mode: ${JSON.stringify(info)}`);
        return false;
    }
    return true;
}

/**
 * Wait until the document is quiet: visible tiles in, thumbnails no longer
 * arriving. "Already fully loaded" is the premise of the requirement.
 */
export async function settleForDuplicate(frame) {
    const deadline = Date.now() + SETTLE_TIMEOUT_MS;
    let last = null, quiet = 0;
    while (Date.now() < deadline) {
        const state = await frame.evaluate(settleState);
        const same = last && state.ready && state.fetched === last.fetched && state.parts === last.parts;
        quiet = same ? quiet + 1 : 0;
        if (quiet >= 3 && state.fetched > 0) return true;
        last = state;
        sleep(SETTLE_POLL_S);
    }
    console.warn(`document did not settle in ${SETTLE_TIMEOUT_MS} ms: ${JSON.stringify(last)}`);
    return false;
}

/**
 * Duplicate the current slide once and record the signals. Returns the
 * slide_duplicated_ms sample, or null when the new slide was never painted,
 * in which case the page is screenshotted to show what the user got instead.
 */
export async function duplicateSlide(frame, page) {
    await frame.evaluate(startDuplicate);
    const deadline = Date.now() + TIMEOUT_MS;
    let marks, paintedAt = null;
    for (;;) {
        marks = await frame.evaluate(readDuplicate);
        if ('painted' in marks) {
            paintedAt = paintedAt || Date.now();
            const complete = Object.keys(SIGNALS).every((name) => name in marks);
            if (complete || Date.now() - paintedAt > TRAILING_GRACE_MS) break;
        } else if (Date.now() > deadline) {
            break;
        }
        sleep(POLL_S);
    }
    await frame.evaluate(stopDuplicate);

    for (const name in trends) if (name in marks) record(trends[name], SIGNALS[name], marks[name]);
    const ok = 'painted' in marks && marks.grown;
    if (!ok) {
        failed.add(1);
        console.warn(`slide duplicate not painted in ${TIMEOUT_MS} ms: ${JSON.stringify(marks)}`);
        if (page) await screenshotPage(page);
    }
    check(ok, { 'duplicated slide painted': (v) => v });
    return ok ? marks.painted : null;
}
