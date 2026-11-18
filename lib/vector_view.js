/**
 * The vector rendering of one user of a text document or a presentation, as the browser client
 * asks for it when it is opened with vector=true. In place of tiles, the client asks for the
 * vector primitives of the parts it shows, then for each font and image that they name, once per
 * session. The server pushes a delta of each part that the session holds when the part changes,
 * and the client answers nothing to it.
 *
 * A presentation asks for the current slide, the slides of the slide pane, and the master page
 * of each slide it holds. A text document asks for the pages in view, then for the pages within
 * one view height above and below, two at a time, and forgets the pages wanted longest ago once
 * it holds more than 20.
 *
 * The primitives come compressed with zstd, so each answer is decompressed to find the fonts and
 * images it names. fzstd decodes about 1.4 MB a second in k6, so the deltas, which the typing of a
 * user pushes to every user of the part, are not decompressed. They name the fonts of the text
 * that is typed, which the answer named already. The fonts and the images are not read.
 */
import { decompress } from 'fzstd';

const SLIDES = 0;
const MASTER_PAGES = 1;
// The slides of the slide pane of a desktop browser window.
const PANE_SLIDES = 6;
// The requests for pages out of view that are out at a time.
const AHEAD_REQUESTS = 2;
// The pages of a text document that the browser client keeps.
const MAX_PAGES = 20;

function header(bytes) {
    const end = bytes.indexOf(10);
    let text = '';
    for (let i = 0; i < (end < 0 ? Math.min(bytes.length, 512) : end); i++) {
        text += String.fromCharCode(bytes[i]);
    }
    return { text, end };
}

function utf8(bytes) {
    // k6 has no TextDecoder; the JSON names the ids and checksums in ASCII.
    let text = '';
    const step = 8192;
    for (let i = 0; i < bytes.length; i += step) {
        text += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    return text;
}

export class VectorView {
    /// @p send sends a protocol message to the server.
    constructor(send) {
        this.send = send;
        // the parts asked for and not forgotten since, by id, with the time each was last wanted
        this.held = new Map();
        // the parts whose answer is still out, those of them asked for ahead of a draw, and the
        // pages that wait to be asked for ahead
        this.out = new Set();
        this.aheadOut = new Set();
        this.ahead = [];
        this.fonts = new Set();
        this.graphics = new Set();
        // the page ids and rectangles of a text document, [x, y, width, height] in twips
        this.pageIds = [];
        this.pageRects = [];
        // the decompressed bytes of the answers, and the time spent decompressing and reading them
        this.bytes = 0;
        this.decodeMs = 0;
    }

    request(partId, mode = SLIDES) {
        if (!partId) return;
        const key = `${mode}:${partId}`;
        this.held.set(key, Date.now());
        if (this.out.has(key)) return;
        this.out.add(key);
        this.send(`commandvalues command=.uno:VectorPrimitives?partid=${partId}&mode=${mode}`);
    }

    /// The slide that the user shows, and the slides of the slide pane around it.
    showSlide(parts, index) {
        if (!parts.length) return;
        const current = Math.min(Math.max(0, index), parts.length - 1);
        this.request(String(parts[current].part ?? current));
        const first = Math.max(0, Math.min(current - 1, parts.length - PANE_SLIDES));
        for (let i = first; i < Math.min(parts.length, first + PANE_SLIDES); i++) {
            if (i !== current) this.request(String(parts[i].part ?? i));
        }
    }

    /// The pages list of a status: message of a text document.
    setPages(status) {
        if (Array.isArray(status.pageids)) this.pageIds = status.pageids.map(String);
        let rects = status.pagerectangles;
        if (typeof rects === 'string') rects = rects.split(';').map((r) => r.split(',').map(Number));
        if (Array.isArray(rects)) this.pageRects = rects.map((r) => r.map(Number));
        // A page that is gone is forgotten, as the browser client does.
        const ids = new Set(this.pageIds);
        for (const key of [...this.held.keys()]) {
            if (!ids.has(key.substring(key.indexOf(':') + 1))) this.forget(key);
        }
    }

    /// The pages in the visible area from @p top to @p top + @p height twips, then the pages
    /// within one view height above and below it.
    showPages(top, height) {
        const overlaps = (r, from, to) => r && r[1] < to && from < r[1] + r[3];
        this.ahead = [];
        for (let i = 0; i < this.pageIds.length; i++) {
            const r = this.pageRects[i];
            if (overlaps(r, top, top + height)) this.request(this.pageIds[i]);
            else if (overlaps(r, top - height, top + 2 * height)) this.ahead.push(this.pageIds[i]);
        }
        this._requestAhead();
        this._trim();
    }

    _requestAhead() {
        while (this.ahead.length && this.aheadOut.size < AHEAD_REQUESTS) {
            const partId = this.ahead.shift();
            const key = `${SLIDES}:${partId}`;
            if (this.out.has(key)) continue;
            this.aheadOut.add(key);
            this.request(partId);
        }
    }

    _trim() {
        if (this.held.size <= MAX_PAGES) return;
        const oldest = [...this.held.entries()].sort((a, b) => a[1] - b[1]);
        for (const [key] of oldest.slice(0, this.held.size - MAX_PAGES)) this.forget(key);
    }

    forget(key) {
        if (!this.held.delete(key)) return;
        this.out.delete(key);
        this.aheadOut.delete(key);
        this.send(`commandvalues command=.uno:VectorForgetPart?partid=${key.substring(key.indexOf(':') + 1)}`);
    }

    /// Reads a binary frame. Returns false when it is not one of vector rendering.
    received(data) {
        const bytes = new Uint8Array(data);
        const { text, end } = header(bytes);
        const primitives = text.startsWith('zstdvectorprimitives:');
        const delta = text.startsWith('zstdvectorprimitivesdelta:');
        if (!primitives && !delta) {
            return text.startsWith('zstdvectorrenderingfont:')
                || text.startsWith('vectorrenderinggraphics:');
        }
        if (delta || end < 0) return true;
        const start = Date.now();
        let json;
        try {
            const raw = decompress(bytes.subarray(end + 1));
            this.bytes += raw.length;
            json = JSON.parse(utf8(raw));
        } catch (e) {
            this.decodeMs += Date.now() - start;
            return true;
        }
        if (json.partId !== undefined) {
            const key = `${json.mode ?? SLIDES}:${json.partId}`;
            this.out.delete(key);
            this.aheadOut.delete(key);
            // An answer without a version names a part that is gone.
            if (json.version === undefined) this.held.delete(key);
            this._requestAhead();
        }
        this._walk(json);
        this.decodeMs += Date.now() - start;
        return true;
    }

    // Asks for each font, image and master page that the primitives name and that this session
    // has not asked for yet.
    _walk(node) {
        if (Array.isArray(node)) {
            for (const child of node) this._walk(child);
            return;
        }
        if (!node || typeof node !== 'object') return;
        if (typeof node.fontId === 'string' && !this.fonts.has(node.fontId)) {
            this.fonts.add(node.fontId);
            this.send(`commandvalues command=.uno:VectorRenderingFont?id=${node.fontId}`);
        }
        if (typeof node.checksum === 'number' && !this.graphics.has(node.checksum)) {
            this.graphics.add(node.checksum);
            this.send(`commandvalues command=.uno:VectorRenderingGraphics?checksum=${node.checksum}`);
        }
        if (node.masterPartId !== undefined) this.request(String(node.masterPartId), MASTER_PAGES);
        for (const key in node) {
            const value = node[key];
            if (value && typeof value === 'object') this._walk(value);
        }
    }
}
