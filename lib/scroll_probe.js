/**
 * Page through an open document at a zoom of the user's own.
 *
 * Each user takes the zoom of its place in SCROLL_ZOOMS, so the users of one
 * run ask the kit for the same pages at different scales, and then presses
 * PageDown SCROLL_STEPS times. In Writer and Calc PageDown moves the view a
 * screen down, in Impress and Draw it shows the next slide or page.
 *
 *   scroll_ms  from the key press to the last message the server sent for it,
 *              taken once the server has sent nothing for QUIET_MS
 *
 * The other users page through the same document at the same time, so a step
 * can also wait on the messages that their steps cause, such as their cursor
 * positions.
 *
 * Environment:
 *   SCROLL_ZOOMS    zoom percentages, one per user in turn (default 100,75,150)
 *   SCROLL_STEPS    PageDown presses per user (default 15)
 */
import exec from 'k6/execution';
import { sleep, check, fail } from 'k6';
import { Trend, Counter } from 'k6/metrics';

import { findCoolFrame } from './test_utils.js';

const ZOOMS = (__ENV['SCROLL_ZOOMS'] || '100,75,150').split(',').map(Number);
const STEPS = Number(__ENV['SCROLL_STEPS'] || 15);
const QUIET_MS = 200;
const STEP_TIMEOUT_MS = 10000;

export const SCROLL_METRICS = ['scroll_ms'];
export const SCROLL_TIMEOUT_COUNTER = 'scroll_timeouts';

const scrollMs = new Trend('scroll_ms', true);
const scrollTimeouts = new Counter(SCROLL_TIMEOUT_COUNTER);

/** The zoom percentage of the user that runs this iteration. */
function userZoom() {
    return ZOOMS[(exec.vu.idInTest - 1) % ZOOMS.length] || 100;
}

/**
 * Wait until the server has sent nothing for QUIET_MS. Returns the time of its
 * last message, on the page clock, or null when it was still sending after
 * STEP_TIMEOUT_MS.
 */
async function waitQuiet(frame) {
    const deadline = Date.now() + STEP_TIMEOUT_MS;
    for (;;) {
        const last = await frame.evaluate(() => {
            return window.__coolScrollLastMessageAt;
        });
        if (Date.now() - last >= QUIET_MS) return last;
        if (Date.now() > deadline) return null;
        sleep(0.05);
    }
}

/** Set the user's zoom, then page down through the document. */
export async function scrollThrough(page) {
    const frame = await findCoolFrame(page);
    if (!frame) fail('no Collabora Online frame');

    const watching = await frame.evaluate(() => {
        const socket = window.app && window.app.socket && window.app.socket.socket;
        if (!socket || typeof socket.onmessage !== 'function') return false;
        const handler = socket.onmessage;
        window.__coolScrollLastMessageAt = Date.now();
        socket.onmessage = function (event) {
            window.__coolScrollLastMessageAt = Date.now();
            return handler.call(this, event);
        };
        return true;
    });
    if (!watching) fail('no socket to watch in the Collabora Online frame');

    const percent = userZoom();
    const zoom = await frame.evaluate((p) => {
        const map = window.app.map;
        const doc = window.app.activeDocument;
        const index = doc && doc.getZoomIndex ? doc.getZoomIndex(p)
            : Math.round(10 + Math.log(p / 100) / Math.log(1.2));
        if (index !== map.getZoom()) map.setZoom(index);
        map.focus();
        return map.getZoom();
    }, percent);
    console.log(`scroll: user ${exec.vu.idInTest} at ${percent}%, zoom level ${zoom}`);
    await waitQuiet(frame);

    for (let i = 0; i < STEPS; i++) {
        const pressedAt = Date.now();
        await page.keyboard.press('PageDown');
        const last = await waitQuiet(frame);
        if (!check(last, { 'scroll step answered': (v) => v !== null })) {
            scrollTimeouts.add(1);
            continue;
        }
        scrollMs.add(Math.max(0, last - pressedAt));
    }
}
