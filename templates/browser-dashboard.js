const SVGNS = "http://www.w3.org/2000/svg";
const MS = [
  { m: "document_loaded_ms" },
  { m: "frame_ready_ms" },
  { m: "first_content_ms" },
  { m: "js_ready_ms" },
  { m: "page_load_ms" },
  { m: "edit_ms" },
  { m: "image_displayed_ms" },
  { m: "slide_duplicated_ms" },
];
const NAMES = {
  instr_kit: "document kit instructions",
  instr_wsd: "coolwsd instructions",
  document_loaded_ms: "document loaded",
  frame_ready_ms: "frame ready",
  first_content_ms: "first content",
  js_ready_ms: "js ready",
  page_load_ms: "page load",
  edit_ms: "edit",
  image_displayed_ms: "image displayed",
  slide_duplicated_ms: "slide duplicated",
  cool_mem: "COOL memory",
  browser_mem: "browser heap",
  tab_mem: "page process",
  master_mem: "coolwsd peak",
};
const APP_ORDER = ["writer", "calc", "impress", "draw"],
  DOC_ORDER = [
    "blank",
    "blank-vector",
    "5mb",
    "5mb-vector",
    "10mb",
    "duplicate",
    "duplicate-vector",
    "ooxml",
    "ooxml-vector",
    "image5mb",
  ];
// Instructions count the same work whatever the load of the machine, while
// the times follow it, so either group can be hidden to read the other.
const INS = [
  { cls: "instr_kit", k: "kit" },
  { cls: "instr_wsd", k: "master" },
];
const insOf = (r, k) => {
  const n = r.instructions;
  return n && n[k] != null ? n[k] : null;
};
const SHOW = { instr: true, times: true, mem: true };
try {
  Object.assign(SHOW, JSON.parse(localStorage.getItem("cool-perf-show") || "{}"));
} catch (e) {}
function esc(s) {
  return String(s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
}
function niceStep(s) {
  if (s <= 0) return 1;
  const r = s / 4,
    m = Math.pow(10, Math.floor(Math.log10(r))),
    n = r / m;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * m;
}
function niceCeil(v) {
  return Math.ceil(v / niceStep(v)) * niceStep(v);
}
function el(name, attrs, text) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}
// A run's label is its commit, sometimes with a suffix naming what was changed
// in that build. Shortening to seven characters would drop the suffix and leave
// two builds of the same commit looking identical, so keep whatever follows the
// first dash.
function shortLabel(c) {
  if (!c) return "";
  const i = c.indexOf("-");
  return i < 0 ? c.slice(0, 7) : c.slice(0, Math.min(i, 7)) + c.slice(i);
}
function shaOf(c) {
  const m = /^[0-9a-f]{7,40}/.exec(c || "");
  return m ? m[0] : null;
}
// The commits that a run measured for the first time: after the newest earlier run of this chart
// with another commit, up to the commit of this run.
function commitRange(runs, i) {
  const to = shaOf(runs[i].commit);
  if (!to) return "";
  for (let j = i - 1; j >= 0; j--) {
    const from = shaOf(runs[j].commit);
    if (!from || from === to) continue;
    if ((runs[j].branch || "main") !== (runs[i].branch || "main")) continue;
    const text = `${from}..${to}`;
    const link = COMPARE
      ? `<a href="${esc(COMPARE + from + "..." + to)}" target="_blank" rel="noopener">${esc(text)}</a>`
      : esc(text);
    return `<br>commits since the previous run: ${link}  ·  <code>git log --oneline ${esc(text)}</code>`;
  }
  return "<br>first commit measured in this chart";
}
// The document kit's resident memory in the layers it is made of, bottom-up.
// The layers add up to what the process has resident, so the top of the stack is
// the figure a machine actually has to hold for that document.
const STACK = [
  ["libraries", "lib", "engine libraries"],
  ["other_files", "other", "other mapped files"],
  ["inherited", "inh", "inherited from the fork"],
  ["own", "own", "the document's own"],
];
function stackOf(pt) {
  const m = pt.cool_memory,
    d = m && m.doc_stack;
  if (!d) return null;
  let total = 0;
  const parts = [];
  for (const [key, cls, name] of STACK) {
    const v = d[key];
    if (v == null) continue;
    parts.push({ cls, name, v });
    total += v;
  }
  return parts.length ? { parts, total } : null;
}
// The peak resident size during the test, which a plain run records for every
// test. A run from before that has the capture's high-water mark only.
function masterOf(pt) {
  const m = pt.cool_memory;
  if (!m) return null;
  if (m.peak_master_rss_mb != null) return m.peak_master_rss_mb;
  return m.peak_master_hwm_mb != null ? m.peak_master_hwm_mb : null;
}
function tabOf(pt) {
  const b = pt.browser_memory,
    r = b && b.renderer;
  return r && r.rss != null ? r.rss : null;
}
// A run with no document figure leaves a gap: the COOL total in its place is
// ten times larger, and drew a spike that set the axis.
function memOf(pt) {
  const m = pt.cool_memory;
  if (!m) return null;
  if (m.peak_doc_mem_mb != null) return m.peak_doc_mem_mb;
  return m.peak_pss_mb != null ? m.peak_pss_mb : m.peak_rss_mb != null ? m.peak_rss_mb : null;
}

function render(svg, runs, W = 520, H = 300) {
  // The Chromium renderers hold several times the memory of everything else,
  // so the page process gets a scale of its own, outside the MB axis.
  const tabMax = SHOW.mem ? Math.max(0, ...runs.map((r) => tabOf(r) || 0)) : 0;
  const m = { top: 14, right: tabMax > 0 ? 96 : 52, bottom: 46, left: 50 };
  svg.replaceChildren();
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const pW = W - m.left - m.right,
    pH = H - m.top - m.bottom,
    x0 = m.left,
    x1 = x0 + pW,
    y1 = m.top + pH;
  let msMax = 0,
    memMax = 0,
    insMax = 0;
  const heapOf = (r) => {
    const x = r.metrics.browser_heap_mb;
    return x ? x.avg : null;
  };
  runs.forEach((r) => {
    if (SHOW.times)
      MS.forEach((s) => {
        const v = r.metrics[s.m] && r.metrics[s.m].avg;
        if (v > msMax) msMax = v;
      });
    if (SHOW.mem) {
      const mm = memOf(r);
      if (mm != null && mm > memMax) memMax = mm;
      const wm = masterOf(r);
      if (wm != null && wm > memMax) memMax = wm;
      const sk = stackOf(r);
      if (sk && sk.total > memMax) memMax = sk.total;
      const bh = heapOf(r);
      if (bh != null && bh > memMax) memMax = bh;
    }
    if (SHOW.instr)
      INS.forEach((s) => {
        const v = insOf(r, s.k);
        if (v != null && v > insMax) insMax = v;
      });
  });
  const msTop = niceCeil(msMax) || 1,
    memTop = niceCeil(memMax) || 1,
    insTop = (niceCeil(insMax / 1e9) || 1) * 1e9;
  const yMs = (v) => y1 - (v / msTop) * pH,
    yMem = (v) => y1 - (v / memTop) * pH,
    yIns = (v) => y1 - (v / insTop) * pH;
  const tabTop = niceCeil(tabMax) || 1,
    yTab = (v) => y1 - (v / tabTop) * pH;
  // The left axis is for the times, and for the instructions once the times are hidden.
  const leftIns = !SHOW.times && insMax > 0,
    left = leftIns || msMax > 0;
  if (left) {
    const top = leftIns ? insTop / 1e9 : msTop,
      step = niceStep(top);
    for (let v = 0; v <= top + 1e-9; v += step) {
      const y = y1 - (v / top) * pH,
        t = el("g", { class: "tick" });
      t.appendChild(el("line", { x1: x0, x2: x1, y1: y, y2: y }));
      t.appendChild(
        el(
          "text",
          { x: x0 - 7, y: y + 3.5, "text-anchor": "end" },
          leftIns ? String(+v.toFixed(2)) : String(Math.round(v)),
        ),
      );
      svg.appendChild(t);
    }
  }
  if (memMax > 0) {
    const ms2 = niceStep(memTop);
    for (let v = 0; v <= memTop + 1e-9; v += ms2) {
      svg.appendChild(
        el(
          "text",
          { class: "tick", x: x1 + 7, y: yMem(v) + 3.5, "text-anchor": "start" },
          String(Math.round(v)),
        ),
      );
    }
  }
  if (tabMax > 0) {
    const ts = niceStep(tabTop);
    for (let v = 0; v <= tabTop + 1e-9; v += ts) {
      svg.appendChild(
        el(
          "text",
          { class: "tabtick", x: x1 + 48, y: yTab(v) + 3.5, "text-anchor": "start" },
          String(Math.round(v)),
        ),
      );
    }
  }
  svg.appendChild(el("line", { class: "baseline", x1: x0, x2: x1, y1: y1, y2: y1 }));
  if (left)
    svg.appendChild(
      el(
        "text",
        {
          class: "axtitle",
          x: 12,
          y: m.top + pH / 2,
          "text-anchor": "middle",
          transform: `rotate(-90 12 ${m.top + pH / 2})`,
        },
        leftIns ? "G instructions" : "ms",
      ),
    );
  if (memMax > 0)
    svg.appendChild(
      el(
        "text",
        {
          class: "axtitle",
          x: W - 10,
          y: m.top + pH / 2,
          "text-anchor": "middle",
          transform: `rotate(90 ${W - 10} ${m.top + pH / 2})`,
        },
        "MB",
      ),
    );
  const n = runs.length,
    pad = n > 1 ? 18 : pW / 2;
  const xAt = (i) => x0 + pad + (n > 1 ? (i * (pW - 2 * pad)) / (n - 1) : 0);
  // One stacked bar per run, on the same MB axis as the memory lines so its
  // height can be read off that axis rather than taken on trust. Each bar is
  // centred on its run and drawn first, behind the lines and points.
  if (SHOW.mem)
    runs.forEach((r, i) => {
      const sk = stackOf(r);
      if (!sk) return;
      const bw = Math.max(4, Math.min(13, pW / (runs.length * 3)));
      const cx = xAt(i);
      let acc = 0;
      sk.parts.forEach((part) => {
        const yTop = yMem(acc + part.v),
          h = Math.max(0.5, yMem(acc) - yTop);
        const rect = el("rect", {
          class: "sbar " + part.cls,
          x: cx - bw / 2,
          y: yTop,
          width: bw,
          height: h,
        });
        const tip = `${shortLabel(r.commit)} · ${part.name}: ${Math.round(part.v)} MB of ${Math.round(sk.total)} resident`;
        rect.setAttribute("data-tip", tip);
        rect.setAttribute("aria-label", tip);
        svg.appendChild(rect);
        acc += part.v;
      });
    });
  // fmt gives the value with its unit. A steady line keeps the busy runs on it.
  let drawn = 0;
  function plot(cls, scale, getv, fmt, steady) {
    const pts = [];
    runs.forEach((r, i) => {
      const v = getv(r);
      if (v != null) pts.push([i, xAt(i), scale(v), v]);
    });
    if (!pts.length) return;
    drawn++;
    // the line joins the runs of a quiet machine, and a busy run stays a faded dot beside it
    const quiet = steady
      ? pts
      : pts.filter((p) => !(runs[p[0]].machine && runs[p[0]].machine.busy));
    if (quiet.length > 1)
      svg.appendChild(
        el("polyline", {
          class: "line " + cls,
          points: quiet.map((p) => p[1] + "," + p[2]).join(" "),
        }),
      );
    pts.forEach((p) => {
      const r = runs[p[0]];
      const busy = r.machine && r.machine.busy;
      const dot = el("circle", {
        class: "dot " + cls.split(" ")[0] + (busy && !steady ? " busy" : ""),
        cx: p[1],
        cy: p[2],
        r: 3.5,
        tabindex: 0,
        role: "button",
      });
      const short = cls.split(" ")[0],
        unit = cls.indexOf("mem") >= 0 ? "MB" : "ms";
      const label =
        `${shortLabel(r.commit)} · ${NAMES[short] || short}: ${fmt ? fmt(p[3]) : Math.round(p[3]) + " " + unit}` +
        (r.profiled ? " · profiled" : "") +
        (busy ? " · busy machine" : "");
      dot.setAttribute("data-tip", label);
      dot.setAttribute("aria-label", label);
      dot.addEventListener("click", () => openDetail(runs, p[0]));
      svg.appendChild(dot);
    });
  }
  if (SHOW.times)
    MS.forEach((s) =>
      plot(s.m, yMs, (r) => {
        const x = r.metrics[s.m];
        return x ? x.avg : null;
      }),
    );
  if (SHOW.mem) {
    plot("master_mem mem", yMem, masterOf);
    plot("cool_mem mem", yMem, memOf);
    plot("browser_mem mem", yMem, heapOf);
    plot("tab_mem mem", yTab, tabOf);
  }
  // drawn last, so they stay on top of the other lines
  if (SHOW.instr)
    INS.forEach((s) =>
      plot(
        s.cls,
        yIns,
        (r) => insOf(r, s.k),
        (v) => (v / 1e9).toFixed(2) + " G",
        true,
      ),
    );
  // a run from before the multi-user captures has instructions for one user only
  if (!drawn)
    svg.appendChild(
      el(
        "text",
        { class: "axtitle", x: x0 + pW / 2, y: m.top + pH / 2, "text-anchor": "middle" },
        SHOW.instr && !SHOW.times && !SHOW.mem
          ? "no instruction counts for this run"
          : "nothing to show for the lines selected",
      ),
    );
  // x labels: short commit, thinned if crowded
  const everyk = Math.ceil(n / Math.max(6, Math.floor(pW / 75)));
  runs.forEach((r, i) => {
    if (i % everyk) return;
    svg.appendChild(
      el(
        "text",
        { class: "xlab", x: xAt(i), y: y1 + 15, "text-anchor": "middle" },
        r.commit ? shortLabel(r.commit) : "#" + i,
      ),
    );
  });
}

// The branch that the charts show, from the address so that the view can be
// passed on as a link. A run from before branches is of main.
const BRANCHES = [
  ...new Set(Object.values(DATA).flatMap((rs) => rs.map((r) => r.branch || "main"))),
].sort((a, b) => (a !== "main") - (b !== "main") || a.localeCompare(b));
let BRANCH = "main";
let fullChart = null; // the chart in full view, see setFull
(function () {
  const h = new URLSearchParams(location.hash.slice(1));
  BRANCH = BRANCHES.includes(h.get("branch"))
    ? h.get("branch")
    : BRANCHES.includes("main")
      ? "main"
      : BRANCHES[0] || "main";
})();
function viewOf(key) {
  return (DATA[key] || []).filter((r) => (r.branch || "main") === BRANCH);
}
function renderAll() {
  document.querySelectorAll("section.chart").forEach((sec) => {
    const svg = sec.querySelector("svg[data-key]");
    const runs = viewOf(svg.dataset.key);
    sec.hidden = !runs.length;
    if (runs.length && (!fullChart || fullChart === sec))
      fullChart === sec ? drawFull() : render(svg, runs);
  });
}

const host = document.getElementById("charts");
function chartOrder(a, b) {
  const A = a.split("|"),
    B = b.split("|");
  return (
    APP_ORDER.indexOf(A[0]) - APP_ORDER.indexOf(B[0]) ||
    DOC_ORDER.indexOf(A[1]) - DOC_ORDER.indexOf(B[1]) ||
    Number(A[2]) - Number(B[2])
  );
}
Object.keys(DATA)
  .sort(chartOrder)
  .forEach((key) => {
    const [app, doc, users] = key.split("|");
    const runs = DATA[key];
    // the document under test, from the newest run that recorded one
    const file = (runs.filter((r) => r.file).pop() || {}).file;
    const sec = document.createElement("section");
    sec.className = "chart a-" + app;
    sec.innerHTML = `<h2>${esc(app)} &middot; ${esc(doc)} &middot; ${esc(users)} user${users === "1" ? "" : "s"}<button class="fullbtn" type="button" aria-pressed="false">full view</button></h2>
    ${file ? `<p class="fname" title="the document this test opens">${esc(file)}</p>` : ""}
    <svg data-key="${esc(key)}" viewBox="0 0 520 300" role="img" aria-label="${esc(key)}${file ? " of " + esc(file) : ""} over commits"></svg>`;
    host.appendChild(sec);
  });
renderAll();

// One chart at a time fills the window. It is drawn again at the size of the window, so the text
// keeps its size and the axis has room for more commit labels.
function drawFull() {
  const svg = fullChart.querySelector("svg[data-key]");
  const box = svg.getBoundingClientRect();
  render(
    svg,
    viewOf(svg.dataset.key),
    Math.max(520, Math.round(box.width)),
    Math.max(300, Math.round(box.height)),
  );
}
function setFull(sec) {
  if (fullChart) {
    const old = fullChart;
    fullChart = null;
    old.classList.remove("full");
    old.querySelector(".fullbtn").textContent = "full view";
    old.querySelector(".fullbtn").setAttribute("aria-pressed", "false");
    const svg = old.querySelector("svg[data-key]");
    render(svg, viewOf(svg.dataset.key));
  }
  document.body.classList.toggle("has-full", !!sec);
  if (!sec) return;
  fullChart = sec;
  sec.classList.add("full");
  sec.querySelector(".fullbtn").textContent = "close";
  sec.querySelector(".fullbtn").setAttribute("aria-pressed", "true");
  drawFull();
}
host.addEventListener("click", (e) => {
  const b = e.target.closest(".fullbtn");
  if (!b) return;
  const sec = b.closest(".chart");
  setFull(fullChart === sec ? null : sec);
});
window.addEventListener("resize", () => {
  if (fullChart) drawFull();
});

// The buttons in the legend show or hide a group of lines on every chart. The
// choice is kept in this browser only.
function syncViews() {
  document
    .querySelectorAll(".vt")
    .forEach((b) => b.setAttribute("aria-pressed", String(!!SHOW[b.dataset.v])));
  document
    .querySelectorAll(".legend .key[data-g]")
    .forEach((k) => k.classList.toggle("off", !SHOW[k.dataset.g]));
}
document.querySelector(".views").addEventListener("click", (e) => {
  const b = e.target.closest(".vt");
  if (!b) return;
  SHOW[b.dataset.v] = !SHOW[b.dataset.v];
  try {
    localStorage.setItem("cool-perf-show", JSON.stringify(SHOW));
  } catch (e) {}
  syncViews();
  document.querySelectorAll("svg[data-key]").forEach((svg) => {
    if (!fullChart || !fullChart.contains(svg)) render(svg, viewOf(svg.dataset.key));
  });
  if (fullChart) drawFull();
});
syncViews();

// The address holds the branch of the charts and the two sides of the comparison.
// The history of the runs was a Runs tab of its own, so an old #view=runs opens the schedule.
let VIEW = ((v) =>
  v === "runs" ? "schedule" : ["compare", "schedule", "capacity"].includes(v) ? v : "charts")(
  new URLSearchParams(location.hash.slice(1)).get("view"),
);
function writeHash() {
  const h = new URLSearchParams();
  if (VIEW !== "charts") h.set("view", VIEW);
  if (BRANCH !== "main") h.set("branch", BRANCH);
  if (CMP.a) h.set("a", CMP.a.from + (CMP.a.to ? ".." + CMP.a.to : ""));
  if (CMP.b) h.set("b", CMP.b.from + (CMP.b.to ? ".." + CMP.b.to : ""));
  history.replaceState(null, "", h.toString() ? "#" + h : location.pathname + location.search);
}

(function () {
  if (BRANCHES.length < 2) return;
  const sel = document.getElementById("brsel");
  sel.innerHTML = BRANCHES.map(
    (b) => `<option${b === BRANCH ? " selected" : ""}>${esc(b)}</option>`,
  ).join("");
  sel.addEventListener("change", () => {
    BRANCH = sel.value;
    writeHash();
    renderAll();
  });
  document.getElementById("branches").hidden = false;
})();

// ---- comparison of two sides, each a measured commit or a range of them --------------
// A measured point: the runs of one branch, one after the other, that measured the
// same commit. Grouping by the label alone would merge builds that share a label.
const POINTS = (() => {
  const first = new Map();
  Object.values(DATA).forEach((rs) =>
    rs.forEach((r) => {
      if (!r.runid) return;
      const o = first.get(r.runid);
      if (!o || (r.ts || "") < o.ts)
        first.set(r.runid, {
          runid: r.runid,
          ts: r.ts || "",
          branch: r.branch || "main",
          commit: r.commit,
          base: r.base,
        });
    }),
  );
  const byBranch = {};
  [...first.values()]
    .sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0))
    .forEach((r) => {
      const list = byBranch[r.branch] || (byBranch[r.branch] = []),
        last = list[list.length - 1];
      if (last && last.commit === r.commit) {
        last.runids.push(r.runid);
        last.ts2 = r.ts;
        return;
      }
      list.push({
        id: r.runid,
        branch: r.branch,
        commit: r.commit,
        base: r.base,
        ts: r.ts,
        ts2: r.ts,
        runids: [r.runid],
      });
    });
  return byBranch;
})();
const POINT_BY_ID = {};
Object.values(POINTS).forEach((l) =>
  l.forEach((p) => {
    POINT_BY_ID[p.id] = p;
  }),
);
// A run time in the time zone of the browser; the data holds it in UTC.
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const two = (n) => String(n).padStart(2, "0");
const day = (ts) => {
  const d = new Date(ts);
  return isNaN(d)
    ? String(ts || "")
        .slice(0, 16)
        .replace("T", " ")
    : `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
};
const pointName = (p) =>
  `${p.branch} · ${shortLabel(p.commit || "?")} · ${day(p.ts)}` +
  (p.runids.length > 1 ? ` · ${p.runids.length} runs` : "");
// the run ids of a side, from its first point through its last on the same branch
function sideRuns(side) {
  const f = POINT_BY_ID[side.from];
  if (!f) return new Set();
  const list = POINTS[f.branch],
    t =
      side.to && POINT_BY_ID[side.to] && POINT_BY_ID[side.to].branch === f.branch
        ? POINT_BY_ID[side.to]
        : f;
  let i = list.indexOf(f),
    j = list.indexOf(t);
  if (i > j) [i, j] = [j, i];
  return new Set(list.slice(i, j + 1).flatMap((p) => p.runids));
}
function sideName(side) {
  const f = POINT_BY_ID[side.from],
    t = side.to && POINT_BY_ID[side.to];
  if (!f) return "nothing";
  return t && t !== f
    ? `${f.branch} · ${shortLabel(f.commit)} (${day(f.ts)}) to ${shortLabel(t.commit)} (${day(t.ts)}), ${sideRuns(side).size} runs`
    : pointName(f);
}

// What is compared, and how far a value has to move before it counts as a change: by the
// share th of its value, and by abs in its own unit, so that a few ms on a short time do
// not count. The instruction counts hardly depend on the load of the machine, the times
// depend on it most.
const heapOf = (r) => {
  const x = r.metrics.browser_heap_mb;
  return x ? x.avg : null;
};
const CMP_METRICS = [
  {
    name: "instructions, document kit",
    get: (r) => insOf(r, "kit"),
    fmt: (v) => (v / 1e9).toFixed(2) + " G",
    th: 0.01,
    abs: 5e7,
  },
  {
    name: "instructions, coolwsd",
    get: (r) => insOf(r, "master"),
    fmt: (v) => (v / 1e9).toFixed(2) + " G",
    th: 0.01,
    abs: 5e7,
  },
  // Chromium and the k6 process that drives it, in the k6 container
  {
    name: "instructions, browser",
    get: (r) => insOf(r, "browser"),
    fmt: (v) => (v / 1e9).toFixed(2) + " G",
    th: 0.01,
    abs: 5e7,
  },
  ...MS.map((s) => ({
    name: NAMES[s.m] || s.m,
    get: (r) => {
      const x = r.metrics[s.m];
      return x ? x.avg : null;
    },
    fmt: (v) => Math.round(v) + " ms",
    th: 0.05,
    abs: 50,
  })),
  { name: "COOL doc memory", get: memOf, fmt: (v) => Math.round(v) + " MB", th: 0.03, abs: 10 },
  {
    name: "kit memory, resident",
    get: (r) => {
      const s = stackOf(r);
      return s ? s.total : null;
    },
    fmt: (v) => Math.round(v) + " MB",
    th: 0.03,
    abs: 10,
  },
  { name: "coolwsd peak", get: masterOf, fmt: (v) => Math.round(v) + " MB", th: 0.03, abs: 10 },
  // The JavaScript heap is 25 to 50 MB and moves by up to a third between runs with the
  // garbage collector, so it counts by its share alone.
  { name: "browser heap", get: heapOf, fmt: (v) => Math.round(v) + " MB", th: 0.1, abs: 0 },
  { name: "page process", get: tabOf, fmt: (v) => Math.round(v) + " MB", th: 0.03, abs: 10 },
];
const median = (a) => {
  const s = a.slice().sort((x, y) => x - y),
    n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
};

let CMP = { a: null, b: null },
  CMP_NOTE = "";
(function () {
  const h = new URLSearchParams(location.hash.slice(1));
  const parse = (v) => {
    if (!v) return null;
    const [from, to] = v.split("..");
    return POINT_BY_ID[from] ? { from, to: to && POINT_BY_ID[to] ? to : "" } : null;
  };
  CMP.a = parse(h.get("a"));
  CMP.b = parse(h.get("b"));
  if (CMP.a && CMP.b) return;
  // By default the newest change against main at the commit it is built on, or else the
  // last two measured commits of main.
  const all = Object.values(POINTS)
    .flat()
    .sort((x, y) => (x.ts < y.ts ? -1 : 1));
  const main = POINTS.main || [],
    other = all.filter((p) => p.branch !== "main");
  const b = other.length ? other[other.length - 1] : main[main.length - 1];
  if (!b) return;
  let a = null;
  if (b.branch !== "main") {
    const base = shaOf(b.base);
    a = base ? main.filter((p) => shaOf(p.commit) === base).pop() : null;
    if (!a) {
      a = main.filter((p) => p.ts < b.ts).pop() || main[main.length - 1];
      if (a)
        CMP_NOTE = base
          ? `The base of ${b.branch}, ${base}, was never measured on main, so A is the last run of main before it.`
          : `No base is recorded for ${b.branch}, so A is the last run of main before it.`;
    }
  } else a = main[main.indexOf(b) - 1];
  if (a) {
    CMP.a = { from: a.id, to: "" };
    CMP.b = { from: b.id, to: "" };
  }
})();

function fillSide(k) {
  const side = CMP[k],
    from = document.getElementById("cmp" + k),
    to = document.getElementById("cmp" + k + "2");
  from.innerHTML = Object.keys(POINTS)
    .sort((a, b) => (a !== "main") - (b !== "main") || a.localeCompare(b))
    .map(
      (br) =>
        `<optgroup label="${esc(br)}">` +
        POINTS[br]
          .slice()
          .reverse()
          .map(
            (p) =>
              `<option value="${esc(p.id)}"${side && p.id === side.from ? " selected" : ""}>${esc(pointName(p))}</option>`,
          )
          .join("") +
        `</optgroup>`,
    )
    .join("");
  const f = side && POINT_BY_ID[side.from];
  to.innerHTML =
    `<option value="">that commit only</option>` +
    (f
      ? POINTS[f.branch]
          .slice()
          .reverse()
          .filter((p) => p !== f)
          .map(
            (p) =>
              `<option value="${esc(p.id)}"${p.id === side.to ? " selected" : ""}>${esc(pointName(p))}</option>`,
          )
          .join("")
      : "");
}

function renderCompare() {
  const tab = document.getElementById("cmptab"),
    note = document.getElementById("cmpnote");
  if (!CMP.a || !CMP.b) {
    tab.innerHTML = "";
    note.textContent = "Pick two measured commits to compare.";
    return;
  }
  const ra = sideRuns(CMP.a),
    rb = sideRuns(CMP.b),
    all = document.getElementById("cmpall").checked;
  let rows = "",
    changes = 0,
    still = 0,
    docs = 0,
    missing = 0;
  Object.keys(DATA)
    .sort(chartOrder)
    .forEach((key) => {
      const A = DATA[key].filter((r) => ra.has(r.runid)),
        B = DATA[key].filter((r) => rb.has(r.runid));
      if (!A.length || !B.length) {
        if (A.length || B.length) missing++;
        return;
      }
      const lines = [];
      CMP_METRICS.forEach((m) => {
        const va = median(A.map(m.get).filter((v) => v != null)),
          vb = median(B.map(m.get).filter((v) => v != null));
        if (va == null || vb == null || !va) return;
        const d = (vb - va) / va,
          changed = Math.abs(d) >= m.th && Math.abs(vb - va) >= m.abs;
        changed ? changes++ : still++;
        if (!changed && !all) return;
        // lighter the closer the change is to its threshold, full past four times it
        const k = changed ? Math.min(1, (Math.abs(d) - m.th) / (3 * m.th)) : 0;
        const cls = !changed ? "same" : d < 0 ? "better" : "worse";
        const pct = (d > 0 ? "+" : "") + (d * 100).toFixed(1) + "%";
        lines.push(
          `<td class="m">${esc(m.name)}</td><td>${m.fmt(va)}</td><td>${m.fmt(vb)}</td>` +
            `<td class="d ${cls}" style="--k:${(18 + k * 50).toFixed(0)}%">${pct}</td>`,
        );
      });
      if (!lines.length) return;
      docs++;
      const [app, doc, users] = key.split("|");
      rows += lines
        .map(
          (l, i) =>
            `<tr>${i ? "" : `<th scope="rowgroup" rowspan="${lines.length}">${esc(app)} · ${esc(doc)} · ${esc(users)} user${users === "1" ? "" : "s"}</th>`}${l}</tr>`,
        )
        .join("");
    });
  tab.innerHTML = rows
    ? `<thead><tr><th>test</th><th>metric</th><th>A</th><th>B</th><th>B against A</th></tr></thead><tbody>${rows}</tbody>`
    : "";
  note.innerHTML =
    `<b>A</b> ${esc(sideName(CMP.a))}<br><b>B</b> ${esc(sideName(CMP.b))}<br>` +
    (CMP_NOTE ? esc(CMP_NOTE) + "<br>" : "") +
    `${changes} change${changes === 1 ? "" : "s"} in ${docs} test${docs === 1 ? "" : "s"}, ${still} metric${still === 1 ? "" : "s"} within the noise: ` +
    `a change counts from 1% and 0.05 G for instructions, 3% and 10 MB for memory, 10% for the browser heap, 5% and 50 ms for times. ` +
    `Each value is the median over the runs of the side.` +
    (missing
      ? ` ${missing} test${missing === 1 ? " was" : "s were"} measured on one side only.`
      : "");
}

(function () {
  const n = Object.values(POINTS).reduce((a, l) => a + l.length, 0);
  if (!n) return;
  // the tabs, with the arrow keys moving between them as a tab list does
  const tabs = [...document.querySelectorAll("#tabs [role=tab]")];
  const show = (v, focus, init) => {
    VIEW = v;
    tabs.forEach((t) => {
      const on = t.id === "tab-" + v;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      if (on && focus) t.focus();
    });
    if (v !== "charts" && fullChart) setFull(null);
    if (!init) writeHash();
    document.dispatchEvent(new CustomEvent("viewshown", { detail: v }));
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => show(t.id.slice(4)));
    t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      show(tabs[(i + d + tabs.length) % tabs.length].id.slice(4), true);
    });
  });
  document.getElementById("tabs").hidden = false;
  show(VIEW, false, true);
  const pick = (k, which) => {
    const v = document.getElementById("cmp" + k + which).value;
    if (which === "") {
      CMP[k] = { from: v, to: "" };
    } else CMP[k] = { from: CMP[k].from, to: v };
    CMP_NOTE = "";
    fillSide(k);
    writeHash();
    renderCompare();
  };
  ["a", "b"].forEach((k) => {
    if (!CMP[k]) {
      const l = Object.values(POINTS).flat();
      CMP[k] = { from: l[l.length - 1].id, to: "" };
    }
    document.getElementById("cmp" + k).addEventListener("change", () => pick(k, ""));
    document.getElementById("cmp" + k + "2").addEventListener("change", () => pick(k, "2"));
    fillSide(k);
  });
  document.getElementById("cmpall").addEventListener("change", renderCompare);
  renderCompare();
})();

// ---- the list of runs, and their deletion -------------------------------------------
// One row per run, gathered from the points that the run left on every chart.
const RUNS = (() => {
  const m = new Map();
  Object.values(DATA).forEach((rs) =>
    rs.forEach((r) => {
      if (!r.runid) return;
      let o = m.get(r.runid);
      if (!o)
        m.set(
          r.runid,
          (o = {
            runid: r.runid,
            ts: r.ts || "",
            branch: r.branch || "main",
            commit: r.commit,
            base: r.base,
            ref: r.ref,
            tests: 0,
            busy: false,
            flames: 0,
          }),
        );
      if ((r.ts || "") < o.ts) o.ts = r.ts || "";
      o.tests++;
      if (r.machine && r.machine.busy) o.busy = true;
      if (r.flamegraph) o.flames++;
    }),
  );
  return [...m.values()].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));
})();
// A change is read against its base on main, a run of a branch against the previous run of
// that branch with another commit.
function runRange(r) {
  const to = shaOf(r.commit);
  if (!to) return `<code>${esc(r.commit || "?")}</code>`;
  let from = r.branch !== "main" ? shaOf(r.base) : null;
  if (!from) {
    const prev = RUNS.find(
      (o) => o.branch === r.branch && o.ts < r.ts && shaOf(o.commit) && shaOf(o.commit) !== to,
    );
    from = prev ? shaOf(prev.commit) : null;
  }
  const label = r.commit.length > to.length ? ` <span class="ref">${esc(r.commit)}</span>` : "";
  if (!from) return `<code>${esc(to.slice(0, 12))}</code> first run${label}`;
  const text = `${from.slice(0, 12)}..${to.slice(0, 12)}`;
  return (
    (COMPARE
      ? `<a href="${esc(COMPARE + from + "..." + to)}" target="_blank" rel="noopener"><code>${esc(text)}</code></a>`
      : `<code>${esc(text)}</code>`) + label
  );
}
// A run of a Gerrit change links its branch to the change and its ref to the patch set.
const GERRIT = "https://gerrit.collaboraoffice.com/c/online/+/";
function gerritLink(text, path) {
  return `<a href="${esc(GERRIT + path)}" target="_blank" rel="noopener">${esc(text)}</a>`;
}
function runBranch(r) {
  const b = /^gerrit-(\d+)$/.exec(r.branch);
  const name = b ? gerritLink(r.branch, b[1]) : esc(r.branch);
  if (!r.ref || r.ref === r.branch) return name;
  const ps = /^refs\/changes\/\d+\/(\d+)\/(\d+)$/.exec(r.ref);
  return (
    name + `<span class="ref">${ps ? gerritLink(r.ref, ps[1] + "/" + ps[2]) : esc(r.ref)}</span>`
  );
}
(function () {
  const tab = document.getElementById("runtab"),
    btn = document.getElementById("rundel"),
    msg = document.getElementById("runmsg"),
    picked = new Set();
  const say = (text, err) => {
    msg.textContent = text;
    msg.classList.toggle("err", !!err);
  };
  // The page can only delete when it comes from the store's web server, which runs the
  // delete service beside it.
  const served = location.protocol === "http:" || location.protocol === "https:";
  if (!served) say("Open the dashboard from its web server to delete runs.");
  function sync() {
    btn.disabled = !served || !picked.size;
    btn.textContent = picked.size
      ? `Delete ${picked.size} selected run${picked.size === 1 ? "" : "s"}`
      : "Delete selected runs";
    tab
      .querySelectorAll("tbody tr")
      .forEach((tr) => tr.classList.toggle("sel", picked.has(tr.dataset.run)));
    const all = tab.querySelector("thead input");
    if (all) {
      all.checked = picked.size === RUNS.length;
      all.indeterminate = picked.size > 0 && picked.size < RUNS.length;
    }
  }
  tab.innerHTML =
    `<thead><tr><th><input type="checkbox" aria-label="select every run"></th>` +
    `<th class="l">measured</th><th class="l">branch</th><th class="l">commits</th><th>tests</th>` +
    `<th>flamegraphs</th><th class="l">data</th></tr></thead><tbody>` +
    RUNS.map(
      (r) =>
        `<tr data-run="${esc(r.runid)}"><td><input type="checkbox" aria-label="select ${esc(r.runid)}"></td>` +
        `<td class="l">${esc(day(r.ts))}${r.busy ? ` <span class="ref">busy machine</span>` : ""}</td>` +
        `<td class="l">${runBranch(r)}</td>` +
        `<td class="l">${runRange(r)}</td><td>${r.tests}</td><td>${r.flames || ""}</td>` +
        `<td class="l"><a href="${esc(RESULTS + encodeURIComponent(r.runid) + "/")}">${esc(r.runid)}</a></td></tr>`,
    ).join("") +
    `</tbody>`;
  tab.querySelector("thead input").addEventListener("change", (e) => {
    RUNS.forEach((r) => (e.target.checked ? picked.add(r.runid) : picked.delete(r.runid)));
    tab.querySelectorAll("tbody input").forEach((i) => {
      i.checked = e.target.checked;
    });
    sync();
  });
  tab.querySelectorAll("tbody tr").forEach((tr) =>
    tr.querySelector("input").addEventListener("change", (e) => {
      e.target.checked ? picked.add(tr.dataset.run) : picked.delete(tr.dataset.run);
      sync();
    }),
  );
  btn.addEventListener("click", async () => {
    const ids = [...picked];
    if (
      !confirm(
        `Delete ${ids.length} run${ids.length === 1 ? "" : "s"} and all their data from the store?\n\n${ids.join("\n")}`,
      )
    )
      return;
    btn.disabled = true;
    say("Deleting...");
    try {
      const res = await fetch("api/runs/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runids: ids }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
      say(
        `Deleted ${body.deleted.length} run${body.deleted.length === 1 ? "" : "s"}. Reloading...`,
      );
      location.reload();
    } catch (e) {
      say("The runs were not deleted: " + e.message, true);
      sync();
    }
  });
  sync();
})();

// ---- the schedule: the runs of main, the queue of Gerrit changes, and the units started by
// hand, read from deploy/runs-api.py while the tab is open
(function () {
  const tl = document.getElementById("schtl"),
    tab = document.getElementById("schtab"),
    form = document.getElementById("schform"),
    change = document.getElementById("schchange"),
    runsSel = document.getElementById("schruns"),
    at = document.getElementById("schat"),
    go = document.getElementById("schgo"),
    msg = document.getElementById("schmsg");
  const say = (text, err) => {
    msg.textContent = text;
    msg.classList.toggle("err", !!err);
  };
  const served = location.protocol === "http:" || location.protocol === "https:";
  const HOUR = 3600e3;
  let state = null,
    timer = null;
  for (let n = 1; n <= 5; n++) runsSel.add(new Option(String(n), String(n), n === 3, n === 3));

  const jobName = (j) => `${j.change}/${j.patchset}`;
  const STATES = {
    queued: "queued",
    running: "running",
    done: "done",
    failed: "failed",
    cancelled: "cancelled",
  };

  // One lane per kind of run, on a 24-hour axis centred on now.
  function drawTimeline() {
    const now = Date.parse(state.now),
      t0 = now - 12 * HOUR,
      t1 = now + 12 * HOUR;
    // main and the queue, then a lane for each unit started by hand and each other branch with
    // a run in the window. A unit shares the lane of the branch that it is named after, as
    // cool-perf-gerrit-18257.service measures the branch gerrit-18257.
    const unitLane = (u) => u.unit.replace(/^cool-perf-/, "").replace(/\.(service|timer)$/, "");
    const branchRuns = RUNS.filter((r) => r.branch !== "main" && Date.parse(r.ts) >= t0);
    const lanes = ["main", "queue"];
    [...state.by_hand.map(unitLane), ...branchRuns.map((r) => r.branch)].forEach((l) => {
      if (!lanes.includes(l)) lanes.push(l);
    });
    const W = Math.max(tl.clientWidth, 300),
      LH = 30,
      top = 8,
      left = 110,
      H = top + lanes.length * LH + 24;
    const x = (t) => left + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * (W - left - 8);
    const y = (lane) => top + lanes.indexOf(lane) * LH + LH / 2;
    const parts = [];
    const add = (s) => parts.push(s);
    lanes.forEach((l) => {
      add(
        `<text x="0" y="${y(l) + 4}" class="lane">${esc(l.length > 16 ? l.slice(0, 14) + "..." : l)}</text>` +
          `<line x1="${left}" x2="${W - 8}" y1="${y(l)}" y2="${y(l)}" class="track"/>`,
      );
    });
    // a label every 3 hours, or every 6 on a narrow screen
    const step = (W < 600 ? 6 : 3) * HOUR;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      const d = new Date(t);
      add(
        `<line x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${H - 20}" class="grid"/>` +
          `<text x="${x(t)}" y="${H - 6}" class="tick">${two(d.getHours())}:${two(d.getMinutes())}</text>`,
      );
    }
    const bar = (lane, a, b, cls, tip) =>
      add(
        `<rect x="${x(a)}" y="${y(lane) - 7}" width="${Math.max(x(b) - x(a), 3)}" height="14" rx="3" class="${cls}" data-tip="${esc(tip)}" tabindex="0"/>`,
      );
    const mark = (lane, t, cls, tip) =>
      add(
        `<path d="M${x(t)} ${y(lane) - 7}l7 7l-7 7l-7 -7z" class="${cls}" data-tip="${esc(tip)}" tabindex="0"/>`,
      );
    // A running unit that waited for the lock: the wait, then the measuring from the time it took
    // the lock. An API without that time gives one bar from the start of the unit.
    const span = (lane, since, measuring, end, label) => {
      if (measuring && Date.parse(measuring) > Date.parse(since)) {
        bar(
          lane,
          Date.parse(since),
          Date.parse(measuring),
          "queued",
          `${label}: waited for the lock from ${day(since)}`,
        );
        bar(
          lane,
          Date.parse(measuring),
          end,
          "running",
          `${label}: measuring since ${day(measuring)}`,
        );
      } else bar(lane, Date.parse(since), end, "running", `${label}: running since ${day(since)}`);
    };
    // main: the published runs, then the timer's next starts, each a run only if main moved
    RUNS.filter((r) => r.branch === "main" && Date.parse(r.ts) >= t0).forEach((r) =>
      mark("main", Date.parse(r.ts), "done", `main ${shortLabel(r.commit || "?")}, ${day(r.ts)}`),
    );
    // the start of the running unit, or the last start of the timer on an older API
    const mainSince = state.main.since || state.main.last;
    if (state.main.running && mainSince) {
      if (state.main.waiting)
        bar(
          "main",
          Date.parse(mainSince),
          now,
          "queued",
          `main, waiting for the lock since ${day(mainSince)}`,
        );
      else span("main", mainSince, state.main.measuring_since, now, "main");
    }
    if (state.main.next)
      for (let t = Date.parse(state.main.next); t <= t1; t += HOUR)
        mark(
          "main",
          t,
          "planned",
          `timer at ${day(new Date(t).toISOString())}, a run if main has moved`,
        );
    state.jobs.forEach((j) => {
      const label = `${j.branch} patch set ${j.patchset}, ${j.runs} run${j.runs === 1 ? "" : "s"}`;
      if (j.state === "queued") {
        const t = j.not_before ? Date.parse(j.not_before) : now;
        mark(
          "queue",
          Math.max(t, now),
          "queued",
          `${label}: queued, ${j.not_before ? "from " + day(j.not_before) : "as soon as possible"}`,
        );
      } else if (j.waiting) {
        bar(
          "queue",
          Date.parse(j.started),
          now,
          "queued",
          `${label}: waiting for the lock since ${day(j.started)}`,
        );
      } else if (j.state === "running" && j.started) {
        span("queue", j.started, j.measuring_since, now, label);
      } else if (j.started && j.state !== "cancelled") {
        const end = j.ended ? Date.parse(j.ended) : now;
        bar(
          "queue",
          Date.parse(j.started),
          end,
          j.state,
          `${label}: ${j.state}, ${day(j.started)}${j.ended ? " to " + day(j.ended) : ""}`,
        );
      }
    });
    state.by_hand.forEach((u) => {
      if (u.at) mark(unitLane(u), Date.parse(u.at), "queued", `${u.unit}, starts ${day(u.at)}`);
      else if (u.since && u.state === "running")
        span(unitLane(u), u.since, u.measuring_since, now, u.unit);
      else if (u.since)
        bar(
          unitLane(u),
          Date.parse(u.since),
          now,
          "queued",
          `${u.unit}, waiting for the lock since ${day(u.since)}`,
        );
    });
    // after the bars, so that a running unit does not hide the runs it already published
    branchRuns.forEach((r) =>
      mark(
        r.branch,
        Date.parse(r.ts),
        "done",
        `${r.branch} ${shortLabel(r.commit || "?")}, ${day(r.ts)}`,
      ),
    );
    add(`<line x1="${x(now)}" x2="${x(now)}" y1="${top - 4}" y2="${H - 20}" class="now"/>`);
    tl.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="runs of the last and the next 12 hours">${parts.join("")}</svg>`;
  }

  function drawTable() {
    const mainSince = state.main.since || state.main.last;
    const rows = state.jobs.slice().reverse();
    // A unit that waits for the lock of deploy/nightly is scheduled, not running.
    const by = state.by_hand.map((u) => {
      const running = u.state === "running";
      const start = u.at
        ? "at " + day(u.at)
        : running
          ? "since " + day(u.measuring_since || u.since)
          : "waiting for the lock since " + day(u.since);
      return (
        `<tr class="st-${running ? "running" : "queued"}"><td class="l">${running ? "running" : "scheduled"}</td>` +
        `<td class="l" colspan="4"><code>${esc(u.unit)}</code>` +
        `<span class="ref">started by hand with systemd-run</span></td>` +
        `<td class="l">${esc(start)}</td><td></td></tr>`
      );
    });
    const mainRunning = state.main.running && !state.main.waiting;
    const mainStart = state.main.running
      ? mainSince
        ? state.main.waiting
          ? "waiting for the lock since " + day(mainSince)
          : "since " + day(state.main.measuring_since || mainSince)
        : "-"
      : state.main.next
        ? "next " + day(state.main.next)
        : "-";
    const main =
      `<tr class="st-${mainRunning ? "running" : "queued"}"><td class="l">${mainRunning ? "running" : state.main.running ? "scheduled" : "timer"}</td>` +
      `<td class="l" colspan="4">main<span class="ref">every hour, when main has a new commit</span></td>` +
      `<td class="l">${esc(mainStart)}</td><td></td></tr>`;
    tab.innerHTML =
      `<thead><tr><th class="l">state</th><th class="l">change</th><th class="l">patch set</th>` +
      `<th class="l">subject</th><th>runs</th><th class="l">start</th><th class="l"></th></tr></thead><tbody>` +
      main +
      by.join("") +
      rows
        .map((j) => {
          const start = j.waiting
            ? "waiting for the lock since " + day(j.started)
            : j.state === "queued"
              ? j.not_before
                ? "from " + day(j.not_before)
                : "as soon as possible"
              : j.started
                ? day(j.started) + (j.ended ? " to " + day(j.ended) : "")
                : day(j.ended || j.created);
          const acts = [];
          if (j.state === "queued")
            acts.push(`<button type="button" data-cancel="${esc(j.id)}">Cancel</button>`);
          if (j.started)
            acts.push(
              `<a href="api/schedule/${encodeURIComponent(j.id)}/log" target="_blank" rel="noopener">log</a>`,
            );
          if (j.state === "done")
            acts.push(
              `<a href="#view=charts&branch=${encodeURIComponent(j.branch)}" data-reload>charts</a>`,
            );
          return (
            `<tr class="st-${j.waiting ? "queued" : j.state}"><td class="l">${j.waiting ? "scheduled" : STATES[j.state] || esc(j.state)}${j.exit ? ` <span class="ref">exit ${esc(j.exit)}</span>` : ""}</td>` +
            `<td class="l">${gerritLink(j.branch, String(j.change))}</td>` +
            `<td class="l">${gerritLink(String(j.patchset), j.change + "/" + j.patchset)}<span class="ref">${esc(j.revision.slice(0, 12))}</span></td>` +
            `<td class="l subj">${esc(j.subject)}<span class="ref">${esc(j.uploader)}</span></td>` +
            `<td>${j.runs}</td><td class="l">${esc(start)}</td><td class="l acts">${acts.join(" ")}</td></tr>`
          );
        })
        .join("") +
      `</tbody>`;
  }

  async function load() {
    if (!served) {
      say("Open the dashboard from its web server to see and change the schedule.");
      go.disabled = true;
      return;
    }
    try {
      const res = await fetch("api/schedule", { cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      state = await res.json();
    } catch (e) {
      say("The schedule could not be read: " + e.message, true);
      return;
    }
    document.getElementById("schdomains").textContent = state.domains.join(" or ");
    if (state.runner && state.runner !== "active")
      say(`cool-perf-queue.service is ${state.runner}: queued changes wait until it runs.`, true);
    drawTimeline();
    drawTable();
  }

  tab.addEventListener("click", async (e) => {
    const id = e.target.dataset && e.target.dataset.cancel;
    if (e.target.hasAttribute && e.target.hasAttribute("data-reload")) {
      // the charts of a branch that has new runs since this page was built
      setTimeout(() => location.reload(), 0);
      return;
    }
    if (!id) return;
    e.target.disabled = true;
    try {
      const res = await fetch(`api/schedule/${encodeURIComponent(id)}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
      say(`Cancelled ${jobName(body.job)}.`);
    } catch (err) {
      say("Not cancelled: " + err.message, true);
    }
    load();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    go.disabled = true;
    say("Checking the change on Gerrit...");
    try {
      const when = at.value ? new Date(at.value) : null;
      if (when && isNaN(when)) throw new Error("the start is not a date and time");
      const res = await fetch("api/schedule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          change: change.value,
          runs: Number(runsSel.value),
          at: when ? when.toISOString() : null,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
      say(`Queued ${jobName(body.job)}: ${body.job.subject}`);
      change.value = "";
      at.value = "";
    } catch (err) {
      say("Not queued: " + err.message, true);
    }
    go.disabled = false;
    load();
  });

  // Read the schedule while the tab is shown, every 30 seconds.
  document.addEventListener("viewshown", (e) => {
    clearInterval(timer);
    if (e.detail !== "schedule") return;
    load();
    timer = setInterval(() => {
      if (!document.hidden) load();
    }, 30e3);
  });
  window.addEventListener("resize", () => {
    if (state && VIEW === "schedule") drawTimeline();
  });
  // The tabs showed the view of the address before this listened.
  if (VIEW === "schedule") document.dispatchEvent(new CustomEvent("viewshown", { detail: VIEW }));
})();

// ---- the capacity runs, read from the API when the tab is shown ----------------------
(function () {
  const tab = document.getElementById("captab"),
    scenSel = document.getElementById("capscen"),
    msg = document.getElementById("capmsg"),
    det = document.getElementById("capdet"),
    charts = document.getElementById("capcharts");
  let list = null,
    run = null,
    shown = null;
  const cores = (cpus) =>
    (cpus || "")
      .split(",")
      .filter(Boolean)
      .reduce((n, part) => {
        const [a, b] = part.split("-").map(Number);
        return n + (b === undefined ? 1 : b - a + 1);
      }, 0) || null;
  const when = day;
  const gb = (mb) => (mb == null ? "-" : (mb / 1024).toFixed(1) + " GB");
  // A run without a scenario opened copies of one file, blank.odt unless CAP_FILE_ID was set.
  const scenario = (r) =>
    r.scenario ||
    (r.users_per_document > 1 ? `blank.odt, ${r.users_per_document} users each` : "blank.odt");
  // The scenarios of cool-capacity-test.js, in words.
  const pairs = "half of the users editing a document two by two, the others one each";
  const SCENARIOS = {
    writer: "the 5 MB Writer documents, odt and docx in turn, one user each",
    calc: "the 5 MB Calc documents, ods and xlsx in turn, one user each",
    impress: "the 5 MB Impress documents, odp and pptx in turn, one user each",
    "writer-pairs": `the 5 MB Writer documents, odt and docx, with ${pairs}`,
    "calc-pairs": `the 5 MB Calc documents, ods and xlsx, with ${pairs}`,
    "impress-pairs": `the 5 MB Impress documents, odp and pptx, with ${pairs}`,
    mixed:
      "in every 10 documents, one each of odt, docx, ods, xlsx, odp and pptx with one user, " +
      "then 4 more with two users each",
  };
  const describe = (r) =>
    r.scenario
      ? SCENARIOS[r.scenario] || `the documents of the ${r.scenario} scenario`
      : r.users_per_document > 1
        ? `copies of blank.odt, ${r.users_per_document} users on each`
        : "copies of blank.odt, one user each";
  let filter = "";
  // Only the scenarios that have a run are offered, each with its number of runs.
  function drawFilter() {
    const counts = new Map();
    for (const r of list) counts.set(scenario(r), (counts.get(scenario(r)) || 0) + 1);
    if (filter && !counts.has(filter)) filter = "";
    scenSel.innerHTML =
      `<option value="">all scenarios (${list.length})</option>` +
      [...counts]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([s, n]) => `<option value="${esc(s)}">${esc(s)} (${n})</option>`)
        .join("");
    scenSel.value = filter;
  }
  scenSel.addEventListener("change", () => {
    filter = scenSel.value;
    // The run shown below the list is one of the scenario picked.
    const first = list.find((r) => !filter || scenario(r) === filter);
    if (first && filter && scenario(list.find((r) => r.run === run) || {}) !== filter) pick(first.run);
    else drawList();
  });
  // A stage in which the kernel killed a process of the server for lack of memory ends the
  // capacity, even when every session of it passed.
  const passed = (p) => p.measured && p.ok && !p.oom_kills;
  const result = (p) =>
    !p.reached ? "not reached" : !p.measured ? "not measured" : !p.ok ? "FAILED" :
      p.oom_kills ? "OOM kill" : "passed";
  const capText = (r) =>
    !r.done ? (r.state === "stopped" ? "stopped" : "running")
      : r.capacity == null && r.oom_kills ? "unknown"
      : !r.capacity ? "none"
      : r.capacity + (r.plateaus.length && r.plateaus.every(passed) ? "+" : "") + " users";

  const capTitle = (r) =>
    r.capacity == null && r.oom_kills && r.done
      ? ` title="${esc(`The kernel killed ${r.oom_kills} processes of the server, and this run did not record when.`)}"`
      : r.capacity_k6 != null && r.capacity_k6 !== r.capacity
        ? ` title="${esc(`${r.capacity_k6} users by the session targets alone, before the first OOM kill counts.`)}"`
        : "";

  // What ended the capacity: the first stage that failed, and the targets it missed, with an OOM
  // kill first since it ends the stage whatever the sessions did.
  function stoppedBy(r) {
    if (!r.done) return "-";
    const p = r.plateaus.find((x) => !passed(x));
    if (!p) return "every stage passed";
    const why = [];
    if (p.oom_kills) why.push(`${p.oom_kills} OOM kill${p.oom_kills > 1 ? "s" : ""}`);
    if (!p.reached) return `${p.users} users, not reached${why.length ? ": " + why.join(", ") : ""}`;
    if (p.error_rate > 0) why.push(`${(100 * p.error_rate).toFixed(1)}% of sessions failed`);
    if (p.measured && p.edit_p95 >= 300) why.push(`edit p95 ${Math.round(p.edit_p95)} ms`);
    if (p.measured && p.load_p95 >= 10000) why.push(`load p95 ${(p.load_p95 / 1000).toFixed(1)} s`);
    if (!p.measured && !why.length) why.push("nothing measured");
    return `${p.users} users: ${why.join(", ") || "failed"}`;
  }

  function drawList() {
    if (!list.length) {
      tab.innerHTML = "";
      msg.textContent = "No capacity run yet.";
      return;
    }
    msg.textContent = "";
    const shownRuns = list.filter((r) => !filter || scenario(r) === filter);
    tab.innerHTML =
      "<thead><tr><th>started</th><th>commit</th><th class='txt'>scenario</th><th>capacity</th><th class='txt'>first failed stage</th>" +
      "<th>limits</th><th>peak memory</th><th>OOM kills</th><th></th></tr></thead><tbody>" +
      shownRuns
        .map((r) => {
          const n = cores(r.cpus);
          return (
            `<tr${r.run === run ? ' class="sel"' : ""}>` +
            `<td><button type="button" class="linkish" data-run="${esc(r.run)}">` +
            `${esc(when(r.ts))}</button></td>` +
            `<td><code>${esc(r.commit)}</code></td>` +
            `<td class="txt" title="${esc(describe(r))}">${esc(scenario(r))}</td>` +
            `<td${capTitle(r)}>${capText(r)}</td>` +
            `<td class="txt why">${esc(stoppedBy(r))}</td>` +
            `<td>${n ? n + " cores" : "unknown"}, ${r.memory_max_mb ? gb(r.memory_max_mb) : "unknown"}</td>` +
            `<td>${gb(r.memory_peak_mb)}</td>` +
            `<td>${r.oom_kills == null ? "-" : r.oom_kills}</td>` +
            `<td><a href="capacity/${esc(r.run)}/">data</a>` +
            (r.flamegraph ? ` <a href="capacity/${esc(r.run)}/flamegraph.svg">flamegraph</a>` : "") +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody>";
  }
  tab.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-run]");
    if (b) pick(b.dataset.run);
  });

  async function load() {
    try {
      const res = await fetch("api/capacity", { cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      list = (await res.json()).runs;
    } catch (err) {
      msg.textContent = "The capacity runs could not be read: " + err.message;
      return;
    }
    if (!run || !list.some((r) => r.run === run)) {
      const first = list.find((r) => r.done && r.plateaus.some((p) => p.measured)) || list[0];
      run = first ? first.run : null;
    }
    drawFilter();
    drawList();
    if (run) pick(run);
  }

  async function pick(id) {
    run = id;
    drawList();
    try {
      const res = await fetch(`api/capacity/${encodeURIComponent(id)}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      shown = await res.json();
    } catch (err) {
      msg.textContent = "The run could not be read: " + err.message;
      return;
    }
    drawDetail();
  }

  function drawDetail() {
    const r = shown;
    det.hidden = false;
    document.getElementById("capdh").textContent =
      `${scenario(r)}: run of ${when(r.ts)}, commit ${r.commit}`;
    const n = cores(r.cpus);
    document.getElementById("capdnote").textContent =
      (n && r.memory_max_mb
        ? `The server ran on ${n} cores and in ${gb(r.memory_max_mb)}, and `
        : "The run did not record the cores and memory of the server, and ") +
      `the users worked on ${describe(r)}. ` +
      "The shaded bands are the " +
      "stages, where the users hold steady and the figures in the table are measured; " +
      "between them the users ramp up. " +
      (r.oom == null
        ? "The run did not record when the kernel killed processes of the server."
        : r.oom.length
          ? "The red lines are where the kernel killed processes of the server for lack of memory."
          : "The kernel killed no process of the server.");
    document.getElementById("capstages").innerHTML =
      "<thead><tr><th>users</th><th>open documents</th><th>edit p95</th><th>edits</th><th>load p95</th>" +
      "<th>loads</th><th>failed sessions</th><th>OOM kills</th><th>result</th></tr></thead><tbody>" +
      r.plateaus
        .map(
          (p) =>
            `<tr><td>${p.users}</td><td>${p.documents ?? "-"}</td>` +
            `<td>${p.reached ? Math.round(p.edit_p95) + " ms" : "-"}</td><td>${p.edits ?? "-"}</td>` +
            `<td>${p.reached ? Math.round(p.load_p95) + " ms" : "-"}</td><td>${p.loads ?? "-"}</td>` +
            `<td>${p.reached && p.error_rate != null ? (100 * p.error_rate).toFixed(1) + "%" : "-"}</td>` +
            `<td>${p.oom_kills ?? "-"}</td><td>${result(p)}</td></tr>`,
        )
        .join("") +
      "</tbody>";
    drawCharts();
  }

  // Four charts on one time axis, each with its own unit, rather than two units on one chart.
  function drawCharts() {
    const r = shown;
    const end = Math.max(
      1,
      ...[r.users, r.edits, r.cpu, r.memory].map((s) => (s.length ? s[s.length - 1][0] : 0)),
    );
    const n = cores(r.cpus);
    const specs = [
      {
        title: "users and open documents",
        unit: "",
        series: [
          { name: "users", cls: "s0", rows: r.users.map((x) => [x[0], x[1]]) },
          {
            name: "open documents",
            cls: "s1",
            rows: r.memory.filter((x) => x.length > 5).map((x) => [x[0], x[5]]),
          },
        ],
      },
      {
        title: "edit latency, keystroke to answer, ms",
        unit: " ms",
        series: [
          { name: "p95", cls: "s0", rows: r.edits.map((x) => [x[0], x[2]]) },
          { name: "median", cls: "s1", rows: r.edits.map((x) => [x[0], x[1]]) },
        ],
      },
      {
        title: "CPU, cores",
        unit: "",
        digits: 2,
        limit: n,
        limitLabel: n ? `limit ${n} cores` : "",
        series: [
          { name: "server", cls: "s0", rows: r.cpu.map((x) => [x[0], x[1]]) },
          { name: "k6, browser and WOPI host", cls: "s1", rows: r.cpu.map((x) => [x[0], x[2]]) },
        ],
      },
      {
        title: "server memory as coolwsd reports it, GB",
        unit: " GB",
        digits: 2,
        limit: r.memory_max_mb && r.memory_max_mb / 1024,
        limitLabel: r.memory_max_mb ? `limit ${gb(r.memory_max_mb)}` : "",
        series: [
          {
            name: "coolwsd, forkit and kits",
            cls: "s0",
            rows: r.memory.map((x) => [x[0], (x[1] + x[2] + x[3]) / 1024]),
          },
          { name: "kits", cls: "s1", rows: r.memory.map((x) => [x[0], x[3] / 1024]) },
        ],
        extra: (t) => {
          const m = nearest(r.memory, t);
          return m ? `, ${m[4]} kits` : "";
        },
      },
    ];
    charts.innerHTML = "";
    const w = Math.max(320, charts.clientWidth || 800);
    const oom = r.oom || [];
    const svgs = specs.map((sp, i) =>
      chart(sp, w, end, r.plateaus, oom, i === 0, i === specs.length - 1),
    );
    svgs.forEach((s) => charts.appendChild(s));
    // one crosshair over every chart, at the same time
    const move = (e) => {
      const svg = e.currentTarget;
      const b = svg.getBoundingClientRect();
      const t = ((e.clientX - b.left - PAD.l) / (w - PAD.l - PAD.r)) * end;
      if (t < 0 || t > end) return leave();
      const lines = [`${(t / 60).toFixed(1)} min`];
      svgs.forEach((s, i) => {
        const x = PAD.l + (t / end) * (w - PAD.l - PAD.r);
        const cross = s.querySelector(".cross");
        cross.setAttribute("x1", x);
        cross.setAttribute("x2", x);
        cross.style.display = "";
        const sp = specs[i];
        const vals = sp.series
          .map((se) => {
            const v = nearest(se.rows, t);
            const name = sp.series.length > 1 ? se.name + " " : "";
            return v ? `${name}${v[1].toFixed(sp.digits || 0)}${sp.unit}` : null;
          })
          .filter(Boolean);
        if (vals.length) lines.push(`${sp.title}: ${vals.join(", ")}${sp.extra ? sp.extra(t) : ""}`);
      });
      const k = oom.find((x) => x[0] <= t && t < x[0] + 10);
      if (k) lines.push(`OOM kills in the server: ${k[1]}`);
      tip.textContent = lines.join("\n");
      tip.classList.add("multi");
      tip.hidden = false;
      placeTip(e);
    };
    const leave = () => {
      tip.hidden = true;
      tip.classList.remove("multi");
      svgs.forEach((s) => (s.querySelector(".cross").style.display = "none"));
    };
    svgs.forEach((s) => {
      s.addEventListener("mousemove", move);
      s.addEventListener("mouseleave", leave);
    });
  }

  const PAD = { l: 56, r: 16, t: 26, b: 8 };
  const nearest = (rows, t) => {
    let best = null;
    for (const x of rows) if (!best || Math.abs(x[0] - t) < Math.abs(best[0] - t)) best = x;
    return best && Math.abs(best[0] - t) <= 15 ? best : null;
  };
  const niceMax = (v) => {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v);
  };

  function chart(sp, w, end, plateaus, oom, first, last) {
    // the first chart names the stages under its title and legend
    const pt = PAD.t + (first ? 14 : 0);
    const h = 150 + (first ? 14 : 0) + (last ? 22 : 0),
      ph = 150 - PAD.t - PAD.b;
    const top = niceMax(
      Math.max(sp.limit || 0, ...sp.series.flatMap((s) => s.rows.map((x) => x[1]))) * 1.05,
    );
    const X = (t) => PAD.l + (t / end) * (w - PAD.l - PAD.r);
    const Y = (v) => pt + ph - (v / top) * ph;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.setAttribute("class", "cap-chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", sp.title + " over the run");
    let g = "";
    // the stages, where the users hold steady
    plateaus.forEach((p) => {
      if (p.start >= end) return;
      g += `<rect class="band" x="${X(p.start)}" y="${pt}" width="${X(Math.min(p.end, end)) - X(p.start)}" height="${ph}"/>`;
      // a narrow band, as with steps of 10 users, gives the number alone
      const bw = X(Math.min(p.end, end)) - X(p.start);
      if (first && bw >= 14)
        g += `<text class="band-l" x="${(X(p.start) + X(Math.min(p.end, end))) / 2}" y="${pt - 6}">${p.users}${bw >= 60 ? " users" : ""}</text>`;
    });
    [0, 0.5, 1].forEach((f) => {
      const v = top * f;
      g += `<line class="grid" x1="${PAD.l}" x2="${w - PAD.r}" y1="${Y(v)}" y2="${Y(v)}"/>`;
      g += `<text class="ax" x="${PAD.l - 6}" y="${Y(v) + 4}">${+v.toFixed(2)}</text>`;
    });
    if (last)
      for (let m = 0; m * 60 <= end; m += end > 1200 ? 5 : 2)
        g += `<text class="ax mid" x="${X(m * 60)}" y="${h - 6}">${m} min</text>`;
    if (sp.limit) {
      g += `<line class="limit" x1="${PAD.l}" x2="${w - PAD.r}" y1="${Y(sp.limit)}" y2="${Y(sp.limit)}"/>`;
      g += `<text class="ax lim" x="${w - PAD.r}" y="${Y(sp.limit) - 4}">${esc(sp.limitLabel)}</text>`;
    }
    // a line where the kernel killed processes of the server for lack of memory
    oom.forEach(([t]) => {
      if (t <= end) g += `<line class="oom" x1="${X(t)}" x2="${X(t)}" y1="${pt}" y2="${pt + ph}"/>`;
    });
    sp.series.forEach((se) => {
      if (!se.rows.length) return;
      g += `<path class="cl ${se.cls}" d="${se.rows.map((x, i) => (i ? "L" : "M") + X(x[0]).toFixed(1) + " " + Y(x[1]).toFixed(1)).join("")}"/>`;
    });
    // the title, and a legend when there are two series
    g += `<text class="ttl" x="${PAD.l}" y="12">${esc(sp.title)}</text>`;
    if (sp.series.length > 1) {
      let lx = w - PAD.r;
      [...sp.series].reverse().forEach((se) => {
        const tw = se.name.length * 6.2;
        lx -= tw;
        g += `<text class="leg" x="${lx}" y="12">${esc(se.name)}</text>`;
        lx -= 16;
        g += `<line class="cl ${se.cls}" x1="${lx}" x2="${lx + 12}" y1="8" y2="8"/>`;
        lx -= 14;
      });
    }
    g += `<line class="cross" x1="0" x2="0" y1="${pt}" y2="${pt + ph}" style="display:none"/>`;
    svg.innerHTML = g;
    return svg;
  }

  document.addEventListener("viewshown", (e) => {
    if (e.detail === "capacity") load();
  });
  window.addEventListener("resize", () => {
    if (shown && VIEW === "capacity") drawCharts();
  });
  if (VIEW === "capacity") document.dispatchEvent(new CustomEvent("viewshown", { detail: VIEW }));
})();

const modal = document.getElementById("modal"),
  mb = document.getElementById("mb"),
  mt = document.getElementById("mt"),
  mmeta = document.getElementById("mmeta"),
  mfg = document.getElementById("mfg"),
  mcard = document.getElementById("mcard");

function fgLabel(r) {
  return (r.commit ? shortLabel(r.commit) : "local") + " · " + day(r.ts);
}
function fgFrame(r) {
  // <object>, not <img>: the capture carries its own script, so search and zoom
  // keep working inside the frame
  return (
    `<figure class="fg-frame"><figcaption>${esc(fgLabel(r))} &middot; ` +
    `<a class="fg-inline" href="${esc(r.flamegraph)}" target="_blank" rel="noopener">open \u2197</a></figcaption>` +
    `<div class="fg-scroll"><object type="image/svg+xml" data="${esc(r.flamegraph)}"` +
    // aspect from the SVG itself, measured when this file was written: a file://
    // page is an opaque origin, so it cannot read the size out of the embedded
    // document. Without it the capture would be scaled down to a fixed box.
    (r.fg_aspect ? ` style="height:auto;aspect-ratio:1/${r.fg_aspect}"` : ``) +
    ` aria-label="flamegraph ${esc(fgLabel(r))}"></object></div></figure>`
  );
}
// The open run's flamegraph, inline, optionally beside another run of the same
// document for a before/after read. cmp is an index into runs, or null.
function renderFg(runs, i, cmp) {
  const r = runs[i],
    others = runs.map((x, j) => j).filter((j) => j !== i && runs[j].flamegraph);
  if (!r.flamegraph) {
    mcard.classList.remove("wide");
    mfg.innerHTML =
      `<p class="fg-missing">No flamegraph for this run. ` +
      (others.length
        ? `${others.length} other run(s) of this document have one.`
        : `Capture one with <code>./browser-suite.py --flamegraphs</code>.`) +
      `</p>`;
    return;
  }
  mcard.classList.add("wide");
  const other = cmp != null && runs[cmp] && runs[cmp].flamegraph ? runs[cmp] : null;
  const opts = [`<option value="">none</option>`]
    .concat(
      others.map(
        (j) =>
          `<option value="${j}"${j === cmp ? " selected" : ""}>${esc(fgLabel(runs[j]))}</option>`,
      ),
    )
    .join("");
  mfg.innerHTML = `<details class="fg-sec" open><summary>document flamegraph</summary>
    <div class="fg-bar"><label>side by side with: <select id="fgcmp"${others.length ? "" : " disabled"}>${opts}</select></label>
      <span>the kitbroker_* document kit while this document is open</span></div>
    <div class="fg-grid${other ? " two" : ""}">${fgFrame(r)}${other ? fgFrame(other) : ""}</div></details>`;
  // open on the base of the graph: flamegraph.pl draws the root at the bottom,
  // and the widest frames there are what you look at first
  mfg.querySelectorAll(".fg-scroll").forEach((d) => {
    d.scrollTop = d.scrollHeight;
  });
  const sel = document.getElementById("fgcmp");
  if (sel)
    sel.addEventListener("change", () =>
      renderFg(runs, i, sel.value === "" ? null : Number(sel.value)),
    );
}

// The run that the detail shows, so that Previous and Next can step through
// the runs of the same chart.
let detailRuns = null,
  detailIdx = -1;
const mprev = document.getElementById("mprev"),
  mnext = document.getElementById("mnext"),
  mpos = document.getElementById("mpos");
function openDetail(runs, i) {
  const r = runs[i];
  mt.textContent = "Run detail";
  detailRuns = runs;
  detailIdx = i;
  mprev.disabled = i <= 0;
  mnext.disabled = i >= runs.length - 1;
  mpos.textContent = `${i + 1} of ${runs.length}`;
  let meta = `commit ${esc(r.commit || "(unknown)")}  ·  ${esc(day(r.ts))} ${esc(TZ)}`;
  if (BRANCHES.length > 1)
    meta += `  ·  branch ${esc(r.branch || "main")}` + (r.ref ? ` (${esc(r.ref)})` : "");
  if (r.file) meta += `  ·  ${esc(r.file)}`;
  if (r.runid) meta += `  ·  run ${esc(r.runid)}`;
  meta += commitRange(runs, i);
  mmeta.innerHTML = meta;
  const cols = ["avg", "min", "med", "p90", "p95", "max"];
  let h =
    "<table><thead><tr><th>metric</th>" +
    cols.map((c) => `<th>${c}</th>`).join("") +
    "</tr></thead><tbody>";
  // a metric name ends in its unit: _ms for times, _mb for the browser heap, _n for a count
  const metricLabel = (name) =>
    /_mb$/.test(name)
      ? [name.replace(/_mb$/, ""), "MB"]
      : /_n$/.test(name)
        ? [name.replace(/_n$/, ""), "count"]
        : [name.replace(/_ms$/, ""), "ms"];
  Object.keys(r.metrics).forEach((name) => {
    const s = r.metrics[name] || {};
    const [label, unit] = metricLabel(name);
    h +=
      `<tr><td>${label} (${unit})</td>` +
      cols.map((c) => `<td>${s[c] != null ? Math.round(s[c]) : "-"}</td>`).join("") +
      "</tr>";
  });
  // the same test run again under perf for the flamegraph, slower by nature
  Object.keys(r.profiled_metrics || {}).forEach((name) => {
    const s = r.profiled_metrics[name] || {};
    const [label, unit] = metricLabel(name);
    h +=
      `<tr class="prof"><td>${label} under perf (${unit})</td>` +
      cols.map((c) => `<td>${s[c] != null ? Math.round(s[c]) : "-"}</td>`).join("") +
      "</tr>";
  });
  const cm = r.cool_memory;
  const asMb = (v) => (v == null ? null : Math.round(v));
  if (cm) {
    // The document kit: what it had resident, what the proportional split gives
    // it, and how many kits that split was over. One kit alive means it carries
    // nearly the whole shared base, so the proportional figure runs high.
    const rss = asMb(cm.peak_doc_rss_mb),
      pss = asMb(cm.peak_doc_mem_mb != null ? cm.peak_doc_mem_mb : cm.peak_pss_mb);
    const own = asMb(cm.peak_doc_private_mb),
      inh = asMb(cm.peak_doc_shared_mb),
      kits = cm.doc_pids;
    const bits = [];
    if (rss != null) bits.push(`resident ${rss}`);
    if (pss != null)
      bits.push(`proportional ${pss}${kits ? ` over ${kits} kit${kits > 1 ? "s" : ""}` : ""}`);
    if (own != null) bits.push(`own ${own}`);
    if (inh != null) bits.push(`inherited ${inh}`);
    if (bits.length)
      h += `<tr><td>document kit (MB)</td><td colspan="6" style="text-align:left">${bits.join(", ")}</td></tr>`;
    const wbits = [];
    if (cm.peak_master_hwm_mb != null) wbits.push(`peak ${asMb(cm.peak_master_hwm_mb)}`);
    if (cm.peak_master_rss_mb != null) wbits.push(`resident ${asMb(cm.peak_master_rss_mb)}`);
    if (cm.peak_master_pss_mb != null) wbits.push(`proportional ${asMb(cm.peak_master_pss_mb)}`);
    if (cm.peak_total_mem_mb != null) wbits.push(`both processes ${asMb(cm.peak_total_mem_mb)}`);
    if (wbits.length)
      h += `<tr><td>coolwsd (MB)</td><td colspan="6" style="text-align:left">${wbits.join(", ")}</td></tr>`;
  }
  const ds = cm && cm.doc_stack;
  if (ds) {
    const tot = STACK.reduce((a, [k]) => a + (ds[k] || 0), 0);
    h +=
      `<tr><td>kit memory (MB)</td><td colspan="6" style="text-align:left">` +
      STACK.filter(([k]) => ds[k] != null)
        .map(([k, , name]) => `${name} ${Math.round(ds[k])}`)
        .join(", ") +
      `, resident ${Math.round(tot)}</td></tr>`;
  }
  const bm = r.browser_memory;
  if (bm) {
    // Read from /proc/<pid>/status: shared memory counts once per process that
    // maps it, so the total is an upper bound.
    const part = (name, g) => {
      if (!g) return null;
      const b = [];
      if (g.rss != null) b.push(`resident ${asMb(g.rss)}`);
      if (g.hwm != null) b.push(`peak ${asMb(g.hwm)}`);
      if (g.anon != null) b.push(`anonymous ${asMb(g.anon)}`);
      if (g.shmem != null) b.push(`shared memory ${asMb(g.shmem)}`);
      if (g.pss != null) b.push(`proportional ${asMb(g.pss)}`);
      if (g.pids) b.push(`${g.pids} process${g.pids > 1 ? "es" : ""}`);
      return b.length ? `${name}: ${b.join(", ")}` : null;
    };
    const rows = [
      part("page (renderer)", bm.renderer),
      part("GPU", bm.gpu),
      part("browser and utility", bm.other),
    ].filter(Boolean);
    if (bm.peak_total_rss_mb != null)
      rows.push(`all Chromium processes: resident ${asMb(bm.peak_total_rss_mb)}`);
    if (rows.length)
      h += `<tr><td>browser (MB)</td><td colspan="6" style="text-align:left">${rows.join("<br>")}</td></tr>`;
  }
  const mc = r.machine;
  if (mc) {
    const bits = [];
    const pr = mc.probe_ms;
    if (pr)
      bits.push(
        `probe ${Math.round(pr.med)} ms median, ${Math.round(pr.p90)} ms p90` +
          (mc.probe_ratio != null ? ` (${mc.probe_ratio.toFixed(2)} times the usual)` : ""),
      );
    if (mc.steal_pct != null) bits.push(`steal ${mc.steal_pct.toFixed(1)}%`);
    if (mc.busy) bits.push("<b>busy machine</b>");
    if (bits.length)
      h += `<tr><td>machine</td><td colspan="6" style="text-align:left">${bits.join(", ")}</td></tr>`;
  }
  const ins = r.instructions;
  if (ins) {
    h +=
      `<tr><td>instructions (G)</td><td colspan="6" style="text-align:left">` +
      (ins.master != null ? `coolwsd ${(ins.master / 1e9).toFixed(1)}` : "") +
      (ins.kit != null
        ? `${ins.master != null ? ", " : ""}document kit ${(ins.kit / 1e9).toFixed(1)}`
        : "") +
      (ins.browser != null
        ? `${ins.master != null || ins.kit != null ? ", " : ""}browser ${(ins.browser / 1e9).toFixed(1)}`
        : "") +
      "</td></tr>";
  }
  mb.innerHTML = h + "</tbody></table>";
  renderFg(runs, i, null);
  const wasOpen = !modal.hidden;
  modal.hidden = false;
  if (!wasOpen) document.getElementById("mc").focus();
}
function stepDetail(d) {
  const j = detailIdx + d;
  if (!detailRuns || j < 0 || j >= detailRuns.length) return;
  openDetail(detailRuns, j);
  mcard.scrollTop = 0;
}
function close() {
  modal.hidden = true;
}
document.getElementById("mc").addEventListener("click", close);
mprev.addEventListener("click", () => stepDetail(-1));
mnext.addEventListener("click", () => stepDetail(1));
modal.addEventListener("click", (e) => {
  if (e.target === modal) close();
});
document.addEventListener("keydown", (e) => {
  if (!modal.hidden && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    // leave the arrows to a focused select, such as the flamegraph comparison
    if (e.target.closest && e.target.closest("select,input")) return;
    e.preventDefault();
    stepDetail(e.key === "ArrowLeft" ? -1 : 1);
    return;
  }
  if (e.key !== "Escape") return;
  if (!modal.hidden) close();
  else if (fullChart) setFull(null);
});

// instant hover tooltip (no native <title> delay)
const tip = document.getElementById("tip");
function placeTip(e) {
  const pad = 14,
    w = tip.offsetWidth,
    h = tip.offsetHeight;
  let x = e.clientX + pad,
    y = e.clientY + pad;
  if (x + w > innerWidth) x = e.clientX - pad - w;
  if (y + h > innerHeight) y = e.clientY - pad - h;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
// Whatever carries a tip, rather than the dots alone: the instruction bars and the stacked
// memory bars carry one each and used to say nothing when the pointer was on them.
function tipTarget(t) {
  return t instanceof Element ? t.closest("[data-tip]") : null;
}
document.addEventListener("mouseover", (e) => {
  const d = tipTarget(e.target);
  if (d) {
    tip.textContent = d.getAttribute("data-tip") || "";
    tip.hidden = false;
    placeTip(e);
  }
});
document.addEventListener("mousemove", (e) => {
  if (!tip.hidden) placeTip(e);
});
document.addEventListener("mouseout", (e) => {
  if (tipTarget(e.target)) tip.hidden = true;
});
// also on keyboard focus, for accessibility
document.addEventListener("focusin", (e) => {
  const d = tipTarget(e.target);
  if (d) {
    const b = d.getBoundingClientRect();
    tip.textContent = d.getAttribute("data-tip") || "";
    tip.hidden = false;
    tip.style.left = b.right + 8 + "px";
    tip.style.top = b.top + "px";
  }
});
document.addEventListener("focusout", (e) => {
  if (tipTarget(e.target)) tip.hidden = true;
});
