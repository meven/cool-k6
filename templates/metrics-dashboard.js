const SVGNS = "http://www.w3.org/2000/svg";

// the four series: colour = series (s0..s3), left axis = CPU, right axis = memory
const SERIES = [
  {
    group: "coolwsd",
    key: "instructions_g",
    measure: "instructions",
    unit: "G",
    axis: "cpu",
    cls: "s0",
  },
  {
    group: "kit",
    key: "instructions_g",
    measure: "instructions",
    unit: "G",
    axis: "cpu",
    cls: "s1",
  },
  { group: "coolwsd", key: "peak_mem_mb", measure: "memory", unit: "MB", axis: "mem", cls: "s2" },
  { group: "kit", key: "peak_mem_mb", measure: "memory", unit: "MB", axis: "mem", cls: "s3" },
];

function esc(s) {
  return String(s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
}
function niceStep(span) {
  const raw = span / 4,
    mag = Math.pow(10, Math.floor(Math.log10(raw))),
    n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
}
function niceCeil(v) {
  const s = niceStep(v);
  return Math.ceil(v / s) * s;
}
function el(name, attrs, text) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}
function fmtRaw(v, unit) {
  return unit === "G" ? v.toFixed(1) + " G" : Math.round(v) + " MB";
}

function render(svg, sc) {
  const runs = sc.runs;
  const W = 560,
    H = 360,
    m = { top: 16, right: 56, bottom: 58, left: 56 };
  const plotW = W - m.left - m.right,
    plotH = H - m.top - m.bottom,
    x0 = m.left,
    x1 = x0 + plotW,
    y1 = m.top + plotH;
  const cpuMax = Math.max(...runs.flatMap((r) => [r.instructions_g.coolwsd, r.instructions_g.kit]));
  const memMax = Math.max(...runs.flatMap((r) => [r.peak_mem_mb.coolwsd, r.peak_mem_mb.kit]));
  const cpuTop = niceCeil(cpuMax) || 1,
    memTop = niceCeil(memMax) || 1;
  const yCpu = (v) => y1 - (v / cpuTop) * plotH;
  const yMem = (v) => y1 - (v / memTop) * plotH;

  // left axis: CPU gridlines + ticks
  const cpuStep = niceStep(cpuTop);
  for (let v = 0; v <= cpuTop + 1e-9; v += cpuStep) {
    const y = yCpu(v),
      t = el("g", { class: "tick" });
    t.appendChild(el("line", { x1: x0, x2: x1, y1: y, y2: y }));
    t.appendChild(
      el("text", { x: x0 - 7, y: y + 3.5, "text-anchor": "end" }, String(Math.round(v))),
    );
    svg.appendChild(t);
  }
  // right axis: memory ticks (labels only, no extra gridlines)
  const memStep = niceStep(memTop);
  for (let v = 0; v <= memTop + 1e-9; v += memStep) {
    const y = yMem(v);
    svg.appendChild(
      el(
        "text",
        {
          class: "tick",
          x: x1 + 7,
          y: y + 3.5,
          "text-anchor": "start",
          "font-family": "ui-monospace,Menlo,monospace",
        },
        String(Math.round(v)),
      ),
    );
  }
  svg.appendChild(el("line", { class: "baseline", x1: x0, x2: x1, y1: y1, y2: y1 }));
  // axis titles
  const midY = m.top + plotH / 2;
  svg.appendChild(
    el(
      "text",
      {
        class: "axtitle",
        x: 13,
        y: midY,
        "text-anchor": "middle",
        transform: `rotate(-90 13 ${midY})`,
      },
      "instructions (G)",
    ),
  );
  svg.appendChild(
    el(
      "text",
      {
        class: "axtitle",
        x: W - 11,
        y: midY,
        "text-anchor": "middle",
        transform: `rotate(90 ${W - 11} ${midY})`,
      },
      "memory (MB)",
    ),
  );

  const n = runs.length,
    pad = n > 1 ? 20 : plotW / 2;
  const xAt = (i) => x0 + pad + (n > 1 ? (i * (plotW - 2 * pad)) / (n - 1) : 0);

  SERIES.forEach((s) => {
    const scale = s.axis === "cpu" ? yCpu : yMem;
    const pts = runs.map((r, i) => [xAt(i), scale(r[s.key][s.group])]);
    svg.appendChild(
      el("polyline", {
        class: "line " + s.cls + (s.axis === "mem" ? " mem" : ""),
        points: pts.map((p) => p.join(",")).join(" "),
      }),
    );
    runs.forEach((r, i) => {
      const [px, py] = pts[i],
        val = r[s.key][s.group];
      const dot = el("circle", {
        class: "dot " + s.cls,
        cx: px,
        cy: py,
        r: 3.5,
        tabindex: 0,
        role: "button",
        "aria-label": `${r.label} ${s.group} ${s.measure} ${fmtRaw(val, s.unit)}, open flamegraph`,
      });
      dot.appendChild(
        el(
          "title",
          null,
          `${r.label} · ${s.group} ${s.measure}: ${fmtRaw(val, s.unit)} — click for flamegraph`,
        ),
      );
      dot.addEventListener("click", () => openFlame(sc.name, r));
      dot.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openFlame(sc.name, r);
        }
      });
      svg.appendChild(dot);
    });
  });
  // x labels, rotated to fit ten of them
  runs.forEach((r, i) => {
    const x = xAt(i),
      y = y1 + 14;
    svg.appendChild(
      el(
        "text",
        { class: "xlab", x: x, y: y, "text-anchor": "end", transform: `rotate(-35 ${x} ${y})` },
        r.label,
      ),
    );
  });
}

// one card per scenario, each a single dual-axis plot
const host = document.getElementById("scenarios");
SCENARIOS.forEach((sc, si) => {
  const sec = document.createElement("section");
  sec.className = "scenario";
  sec.innerHTML = `<h2 class="scenario-title">${esc(sc.name)}<span class="count">${sc.runs.length} run(s)</span></h2>
     <p class="unit">CPU instructions (left) and peak memory (right)</p>
     <svg class="chart" data-si="${si}" viewBox="0 0 560 360" role="img"
          aria-label="${esc(sc.name)} CPU instructions and memory per run"></svg>`;
  host.appendChild(sec);
});
document.querySelectorAll("svg.chart").forEach((svg) => render(svg, SCENARIOS[+svg.dataset.si]));

// flamegraph modal
const modal = document.getElementById("modal");
const modalBody = document.getElementById("modal-body");
const modalTitle = document.getElementById("modal-title");
const modalLink = document.getElementById("modal-link");
function openFlame(scenario, run) {
  modalTitle.textContent = `${scenario} — ${run.label}`;
  modalBody.innerHTML = "";
  if (run.flamegraph) {
    modalLink.href = run.flamegraph;
    modalLink.hidden = false;
  } else {
    modalLink.hidden = true;
  }
  if (run.inlineSvg) {
    const w = document.createElement("div");
    w.className = "flamewrap";
    w.innerHTML = run.inlineSvg;
    modalBody.appendChild(w);
  } else if (run.flamegraph) {
    const f = document.createElement("iframe");
    f.src = run.flamegraph;
    f.loading = "lazy";
    f.title = "flamegraph for " + run.label;
    modalBody.appendChild(f);
  } else {
    modalBody.innerHTML = '<div class="msg">no flamegraph.svg found for this run</div>';
  }
  modal.hidden = false;
  document.getElementById("modal-close").focus();
}
function closeFlame() {
  modal.hidden = true;
  modalBody.innerHTML = "";
}
document.getElementById("modal-close").addEventListener("click", closeFlame);
modal.addEventListener("click", (ev) => {
  if (ev.target === modal) closeFlame();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !modal.hidden) closeFlame();
});
