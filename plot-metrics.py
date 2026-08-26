#!/usr/bin/env python3
"""
plot-metrics.py: chart CPU instructions and memory across k6-flamegraph runs.

Reads one or more metrics.json files (as written by k6-flamegraph.py) and
renders either:
  - a static image (default): two line panels, CPU instructions and peak memory
    per process group; or
  - an interactive HTML dashboard (--html): runs grouped by test scenario, one
    combined dual-axis plot per scenario (CPU instructions on the left axis,
    memory on the right), where clicking any point opens that run's
    flamegraph.svg.

Scenarios come from each run's test_script. Usage:

    ./plot-metrics.py profile-*/metrics.json -o metrics.png
    ./plot-metrics.py profile-*/metrics.json --html dashboard.html
    ./plot-metrics.py --demo --html demo-dashboard.html
    ./plot-metrics.py a/metrics.json b/metrics.json --labels before,after

The static image needs matplotlib; the HTML dashboard needs only the standard
library.
"""
import argparse
import json
import os
import random
import sys
from pathlib import Path

# The two process groups k6-flamegraph reports, in a fixed order.
GROUPS = ("coolwsd", "kit")

# Categorical hues from the design-system palette (colorblind-safe).
SERIES_COLOR = {"coolwsd": "#2a78d6", "kit": "#eb6834"}
INK = "#0b0b0b"
INK_SOFT = "#52514e"
SURFACE = "#fcfcfb"
GRID = "#e3e3e0"


def _scenario_name(test_script) -> str:
    """The scenario a run belongs to, from its test_script (the k6 script)."""
    return Path(test_script).stem if test_script else "runs"


def load_run(path: str) -> dict:
    """Read one metrics.json into a run dict.

    Instructions are converted to giga-instructions. Memory uses PSS when the
    run recorded it (shared-library aware), else RSS. flamegraph.svg sits next
    to metrics.json in the run's out-dir; the scenario is the test script."""
    p = Path(path)
    data = json.loads(p.read_text())
    perf = data.get("perf") or {}
    mem = data.get("memory") or {}
    label = Path(data.get("out_dir") or p.parent).name or p.stem
    instructions_g, peak_mem_mb = {}, {}
    for g in GROUPS:
        instructions_g[g] = (perf.get(g, {}).get("instructions") or 0) / 1e9
        me = mem.get(g, {})
        peak_mem_mb[g] = me.get("peak_pss_mb") or me.get("peak_rss_mb") or 0.0
    return {
        "label": label,
        "scenario": _scenario_name(data.get("test_script")),
        "instructions_g": instructions_g,
        "peak_mem_mb": peak_mem_mb,
        "flamegraph_path": str(p.parent / "flamegraph.svg"),
    }


def group_scenarios(runs: list[dict]) -> list[dict]:
    """Group runs into scenarios, preserving first-seen order within and across."""
    order, by_name = [], {}
    for r in runs:
        name = r.get("scenario") or "runs"
        if name not in by_name:
            by_name[name] = []
            order.append(name)
        by_name[name].append(r)
    return [{"name": name, "runs": by_name[name]} for name in order]


def _demo_flame(scenario: str, label: str) -> str:
    """A tiny synthetic flamegraph so the demo dashboard shows something on
    click without a real capture. Frames roughly echo a kit render stack."""
    frames = [
        (0, 0, 100, "#d98a4a", f"all - {scenario} / {label}"),
        (1, 2, 94, "#e0a24a", "coolwsd"),
        (2, 6, 74, "#cf7f3a", "lokit_runloop"),
        (3, 10, 52, "#e2b24a", "paintTile"),
        (4, 14, 34, "#c85f3a", "general_composite_rect"),
        (5, 16, 26, "#d76a33", "fetch_pixel_b8g8r8"),
    ]
    rows = []
    for depth, x, w, fill, text in frames:
        y = 96 - depth * 16
        rows.append(
            f'<rect x="{x}%" y="{y}" width="{w}%" height="14" rx="2" '
            f'fill="{fill}" stroke="#fff" stroke-width="1"/>'
            f'<text x="{x + 0.8}%" y="{y + 10}" font-size="9" '
            f'font-family="monospace" fill="#20140c">{text}</text>')
    return ('<svg viewBox="0 0 700 112" width="100%" '
            'xmlns="http://www.w3.org/2000/svg" '
            'style="background:#fbf3ec">' + "".join(rows) + "</svg>")


def demo_runs() -> list[dict]:
    """Synthetic data: 10 scenarios of 10 runs each, so the dashboard can be
    exercised at scale without real captures. Deterministic (seeded)."""
    rng = random.Random(42)
    names = [
        "cool-load-document-test", "cool-load-ramp-test", "cool-multi-user-story-test",
        "cool-browser-test", "cool-typing-test", "cool-scroll-test",
        "cool-insert-image-test", "cool-chart-render-test", "cool-large-sheet-test",
        "cool-open-close-test",
    ]
    runs = []
    for si, name in enumerate(names):
        base = {
            "kit_cpu": 110 + si * 22, "wsd_cpu": 24 + si * 6,
            "kit_mem": 500 + si * 45, "wsd_mem": 470 + si * 18,
        }
        for i in range(10):
            t = i / 9.0
            j = lambda: 1 + rng.uniform(-0.05, 0.05)
            ins = {"kit": base["kit_cpu"] * (1 + 1.7 * t) * j(),
                   "coolwsd": base["wsd_cpu"] * (1 + 1.2 * t) * j()}
            mem = {"kit": base["kit_mem"] * (1 + 1.0 * t) * j(),
                   "coolwsd": base["wsd_mem"] * (1 + 0.5 * t) * j()}
            label = f"run {i + 1}"
            runs.append({"label": label, "scenario": name,
                         "instructions_g": ins, "peak_mem_mb": mem,
                         "inline_svg": _demo_flame(name, label)})
    return runs


# --------------------------------------------------------------------------
# Static image (matplotlib)
# --------------------------------------------------------------------------

def _line_panel(ax, runs, key, ylabel, title, fmt) -> None:
    x = list(range(len(runs)))
    for g in GROUPS:
        ys = [r[key][g] for r in runs]
        ax.plot(x, ys, marker="o", markersize=6, linewidth=2,
                color=SERIES_COLOR[g], label=g, zorder=3,
                markeredgecolor=SURFACE, markeredgewidth=1.2)
        for xi, yi in zip(x, ys):
            ax.annotate(fmt(yi), (xi, yi), textcoords="offset points",
                        xytext=(0, 8), ha="center", fontsize=8, color=INK_SOFT)
    ax.set_title(title, color=INK, fontsize=12, loc="left", pad=8)
    ax.set_ylabel(ylabel, color=INK_SOFT, fontsize=10)
    ax.set_xticks(x)
    ax.set_xticklabels([r["label"] for r in runs], rotation=20, ha="right",
                       color=INK, fontsize=9)
    ax.tick_params(axis="y", colors=INK_SOFT, labelsize=9)
    ax.margins(x=0.12, y=0.20)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)


def plot(runs: list[dict], out: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_cpu, ax_mem) = plt.subplots(
        1, 2, figsize=(max(8.0, 1.7 * len(runs) + 4.5), 5.0))
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax_cpu, ax_mem):
        ax.set_facecolor(SURFACE)
    _line_panel(ax_cpu, runs, "instructions_g", "instructions (G)",
                "CPU instructions", lambda v: f"{v:.0f}")
    _line_panel(ax_mem, runs, "peak_mem_mb", "peak memory (MB)",
                "Peak resident memory", lambda v: f"{v:.0f}")
    handles, labels = ax_cpu.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(GROUPS),
               frameon=False, fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    print(f"wrote {out}  ({len(runs)} run(s))")


# --------------------------------------------------------------------------
# Interactive HTML dashboard (standard library only)
# --------------------------------------------------------------------------

DASHBOARD_INNER = r"""
<style>
  :root {
    --surface:#fcfcfb; --panel:#ffffff; --ink:#0b0b0b; --ink-soft:#52514e;
    --ink-faint:#8a8880; --grid:#e6e6e2; --hair:#dcdbd5; --accent:#2a78d6;
    /* four series colours: coolwsd/kit instructions, coolwsd/kit memory */
    --s0:#2a78d6; --s1:#eb6834; --s2:#1baf7a; --s3:#7a4fc0;
    --shadow:0 1px 2px rgba(11,11,11,.06),0 8px 24px rgba(11,11,11,.05);
  }
  @media (prefers-color-scheme:dark){ :root:not([data-theme=light]){
    --surface:#161615; --panel:#1f1f1d; --ink:#f4f3ee; --ink-soft:#c3c2b7;
    --ink-faint:#8f8e84; --grid:#33332f; --hair:#3a3a35; --accent:#3987e5;
    --s0:#3987e5; --s1:#d95926; --s2:#199e70; --s3:#9f7be6;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
  }}
  :root[data-theme=dark]{
    --surface:#161615; --panel:#1f1f1d; --ink:#f4f3ee; --ink-soft:#c3c2b7;
    --ink-faint:#8f8e84; --grid:#33332f; --hair:#3a3a35; --accent:#3987e5;
    --s0:#3987e5; --s1:#d95926; --s2:#199e70; --s3:#9f7be6;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
  }
  body{margin:0;background:var(--surface);color:var(--ink);line-height:1.5;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1720px;margin:0 auto;padding:34px clamp(20px,3.5vw,48px) 64px;}
  header{margin-bottom:14px;}
  h1{font-size:22px;font-weight:650;letter-spacing:-.01em;margin:0 0 4px;}
  .sub{color:var(--ink-soft);font-size:14px;margin:0;}
  .legend{display:flex;gap:14px 18px;align-items:center;flex-wrap:wrap;margin:0;
    font-size:12.5px;color:var(--ink-soft);position:sticky;top:0;
    background:var(--surface);padding:10px 0;z-index:5;border-bottom:1px solid var(--hair);}
  .legend .key{display:inline-flex;align-items:center;gap:7px;}
  .legend .sep{width:1px;height:15px;background:var(--hair);}
  .lswatch{width:22px;height:8px;}
  #scenarios{display:grid;gap:18px;margin-top:16px;
    grid-template-columns:repeat(auto-fit,minmax(470px,1fr));}
  .scenario{background:var(--panel);border:1px solid var(--hair);border-radius:12px;
    padding:14px 16px 10px;box-shadow:var(--shadow);}
  .scenario-title{font-size:15px;font-weight:650;letter-spacing:-.01em;margin:0;
    display:flex;align-items:baseline;gap:10px;}
  .scenario-title .count{font-size:12px;font-weight:500;color:var(--ink-faint);
    font-family:ui-monospace,Menlo,monospace;}
  .scenario .unit{font-size:12px;color:var(--ink-faint);margin:2px 0 6px;}
  svg{width:100%;height:auto;display:block;}
  .tick line{stroke:var(--grid);stroke-width:1;}
  .tick text,.xlab,.axtitle{fill:var(--ink-faint);font-size:10.5px;
    font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;}
  .xlab{fill:var(--ink-soft);font-family:system-ui,sans-serif;}
  .axtitle{font-family:system-ui,sans-serif;fill:var(--ink-soft);}
  .baseline{stroke:var(--hair);stroke-width:1;}
  .line{fill:none;stroke-width:2;}
  .line.mem{stroke-dasharray:5 4;}
  .line.s0{stroke:var(--s0);} .line.s1{stroke:var(--s1);}
  .line.s2{stroke:var(--s2);} .line.s3{stroke:var(--s3);}
  .dot{stroke:var(--panel);stroke-width:1.3;cursor:pointer;transition:r .12s ease;}
  .dot.s0{fill:var(--s0);} .dot.s1{fill:var(--s1);}
  .dot.s2{fill:var(--s2);} .dot.s3{fill:var(--s3);}
  .dot:hover{r:6;} .dot:focus-visible{outline:2px solid var(--ink);outline-offset:1px;}
  /* flamegraph modal */
  .modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;
    justify-content:center;padding:24px;background:rgba(11,11,11,.55);}
  .modal[hidden]{display:none;}
  .modal-card{background:var(--panel);border:1px solid var(--hair);border-radius:12px;
    box-shadow:var(--shadow);width:min(1320px,97vw);max-height:92vh;
    display:flex;flex-direction:column;overflow:hidden;}
  .modal-head{display:flex;justify-content:space-between;align-items:center;gap:12px;
    padding:12px 16px;border-bottom:1px solid var(--hair);}
  .modal-head strong{font-size:14px;}
  .modal-actions{display:flex;gap:8px;align-items:center;}
  .modal-actions a,.modal-actions button{font:inherit;font-size:13px;cursor:pointer;
    border-radius:7px;border:1px solid var(--hair);background:var(--surface);
    color:var(--ink);padding:5px 12px;text-decoration:none;}
  .modal-actions a{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 40%,var(--hair));}
  .modal-actions a[hidden]{display:none;}
  .modal-actions a:hover,.modal-actions button:hover{background:color-mix(in srgb,var(--ink) 9%,transparent);}
  .modal-body{overflow:auto;background:#fff;}
  .modal-body iframe{width:100%;height:72vh;border:0;display:block;background:#fff;}
  .modal-body .flamewrap{padding:12px 14px;}
  .modal-body .msg{padding:36px;color:var(--ink-faint);text-align:center;}
  footer{margin-top:26px;font-size:12.5px;color:var(--ink-faint);
    border-top:1px solid var(--hair);padding-top:14px;max-width:74ch;}
</style>

<div class="wrap">
  <header>
    <h1>k6-flamegraph metrics</h1>
    <p class="sub">One plot per test scenario. Left axis is CPU instructions, right axis is peak memory. Click any point to open that run's flamegraph.</p>
  </header>
  <div class="legend">
    <span class="key"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s0)" stroke-width="2.5"/></svg> coolwsd instructions</span>
    <span class="key"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s1)" stroke-width="2.5"/></svg> kit instructions</span>
    <span class="sep"></span>
    <span class="key"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s2)" stroke-width="2.5" stroke-dasharray="5 4"/></svg> coolwsd memory</span>
    <span class="key"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s3)" stroke-width="2.5" stroke-dasharray="5 4"/></svg> kit memory</span>
  </div>
  <div id="scenarios"></div>
  <footer>
    Runs are grouped by their k6 test scenario. Each plot has two axes: CPU
    instructions (giga, retired) on the left carry the solid lines, peak memory
    (MB, PSS) on the right carry the dashed lines. Each of the four series has
    its own colour, so identity never relies on line style alone. Click any
    point to open that run's flamegraph.svg (with a link to the raw file).
  </footer>
</div>

<div class="modal" id="modal" hidden>
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-head">
      <strong id="modal-title">Flamegraph</strong>
      <span class="modal-actions">
        <a id="modal-link" href="#" target="_blank" rel="noopener" hidden>Open .svg &#8599;</a>
        <button id="modal-close" type="button">Close</button>
      </span>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const SCENARIOS = __SCENARIOS__;
const SVGNS = "http://www.w3.org/2000/svg";

// the four series: colour = series (s0..s3), left axis = CPU, right axis = memory
const SERIES = [
  {group:"coolwsd", key:"instructions_g", measure:"instructions", unit:"G",  axis:"cpu", cls:"s0"},
  {group:"kit",     key:"instructions_g", measure:"instructions", unit:"G",  axis:"cpu", cls:"s1"},
  {group:"coolwsd", key:"peak_mem_mb",    measure:"memory",       unit:"MB", axis:"mem", cls:"s2"},
  {group:"kit",     key:"peak_mem_mb",    measure:"memory",       unit:"MB", axis:"mem", cls:"s3"},
];

function esc(s){ return String(s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c])); }
function niceStep(span){
  const raw = span/4, mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw/mag;
  return (n<=1?1:n<=2?2:n<=5?5:10)*mag;
}
function niceCeil(v){ const s = niceStep(v); return Math.ceil(v/s)*s; }
function el(name, attrs, text){
  const e = document.createElementNS(SVGNS, name);
  for(const k in attrs) e.setAttribute(k, attrs[k]);
  if(text!=null) e.textContent = text;
  return e;
}
function fmtRaw(v, unit){ return unit === "G" ? v.toFixed(1)+" G" : Math.round(v)+" MB"; }

function render(svg, sc){
  const runs = sc.runs;
  const W=560, H=360, m={top:16,right:56,bottom:58,left:56};
  const plotW=W-m.left-m.right, plotH=H-m.top-m.bottom, x0=m.left, x1=x0+plotW, y1=m.top+plotH;
  const cpuMax = Math.max(...runs.flatMap(r => [r.instructions_g.coolwsd, r.instructions_g.kit]));
  const memMax = Math.max(...runs.flatMap(r => [r.peak_mem_mb.coolwsd, r.peak_mem_mb.kit]));
  const cpuTop = niceCeil(cpuMax) || 1, memTop = niceCeil(memMax) || 1;
  const yCpu = v => y1 - (v/cpuTop)*plotH;
  const yMem = v => y1 - (v/memTop)*plotH;

  // left axis: CPU gridlines + ticks
  const cpuStep = niceStep(cpuTop);
  for(let v=0; v<=cpuTop+1e-9; v+=cpuStep){
    const y=yCpu(v), t=el("g",{class:"tick"});
    t.appendChild(el("line",{x1:x0,x2:x1,y1:y,y2:y}));
    t.appendChild(el("text",{x:x0-7,y:y+3.5,"text-anchor":"end"}, String(Math.round(v))));
    svg.appendChild(t);
  }
  // right axis: memory ticks (labels only, no extra gridlines)
  const memStep = niceStep(memTop);
  for(let v=0; v<=memTop+1e-9; v+=memStep){
    const y=yMem(v);
    svg.appendChild(el("text",{class:"tick",x:x1+7,y:y+3.5,"text-anchor":"start",
      "font-family":"ui-monospace,Menlo,monospace"}, String(Math.round(v))));
  }
  svg.appendChild(el("line",{class:"baseline",x1:x0,x2:x1,y1:y1,y2:y1}));
  // axis titles
  const midY = m.top + plotH/2;
  svg.appendChild(el("text",{class:"axtitle",x:13,y:midY,"text-anchor":"middle",
    transform:`rotate(-90 13 ${midY})`}, "instructions (G)"));
  svg.appendChild(el("text",{class:"axtitle",x:W-11,y:midY,"text-anchor":"middle",
    transform:`rotate(90 ${W-11} ${midY})`}, "memory (MB)"));

  const n = runs.length, pad = n>1 ? 20 : plotW/2;
  const xAt = i => x0 + pad + (n>1 ? i*(plotW-2*pad)/(n-1) : 0);

  SERIES.forEach(s => {
    const scale = s.axis === "cpu" ? yCpu : yMem;
    const pts = runs.map((r,i)=>[xAt(i), scale(r[s.key][s.group])]);
    svg.appendChild(el("polyline",
      {class:"line "+s.cls+(s.axis==="mem"?" mem":""), points:pts.map(p=>p.join(",")).join(" ")}));
    runs.forEach((r,i)=>{
      const [px,py]=pts[i], val=r[s.key][s.group];
      const dot = el("circle",{class:"dot "+s.cls,cx:px,cy:py,r:3.5,tabindex:0,role:"button",
        "aria-label":`${r.label} ${s.group} ${s.measure} ${fmtRaw(val,s.unit)}, open flamegraph`});
      dot.appendChild(el("title",null,
        `${r.label} · ${s.group} ${s.measure}: ${fmtRaw(val,s.unit)} — click for flamegraph`));
      dot.addEventListener("click",()=>openFlame(sc.name, r));
      dot.addEventListener("keydown",ev=>{ if(ev.key==="Enter"||ev.key===" "){ev.preventDefault();openFlame(sc.name,r);} });
      svg.appendChild(dot);
    });
  });
  // x labels, rotated to fit ten of them
  runs.forEach((r,i)=>{
    const x=xAt(i), y=y1+14;
    svg.appendChild(el("text",{class:"xlab",x:x,y:y,"text-anchor":"end",
      transform:`rotate(-35 ${x} ${y})`}, r.label));
  });
}

// one card per scenario, each a single dual-axis plot
const host = document.getElementById("scenarios");
SCENARIOS.forEach((sc, si) => {
  const sec = document.createElement("section");
  sec.className = "scenario";
  sec.innerHTML =
    `<h2 class="scenario-title">${esc(sc.name)}<span class="count">${sc.runs.length} run(s)</span></h2>
     <p class="unit">CPU instructions (left) and peak memory (right)</p>
     <svg class="chart" data-si="${si}" viewBox="0 0 560 360" role="img"
          aria-label="${esc(sc.name)} CPU instructions and memory per run"></svg>`;
  host.appendChild(sec);
});
document.querySelectorAll("svg.chart").forEach(svg => render(svg, SCENARIOS[+svg.dataset.si]));

// flamegraph modal
const modal = document.getElementById("modal");
const modalBody = document.getElementById("modal-body");
const modalTitle = document.getElementById("modal-title");
const modalLink = document.getElementById("modal-link");
function openFlame(scenario, run){
  modalTitle.textContent = `${scenario} — ${run.label}`;
  modalBody.innerHTML = "";
  if(run.flamegraph){ modalLink.href = run.flamegraph; modalLink.hidden = false; }
  else { modalLink.hidden = true; }
  if(run.inlineSvg){
    const w = document.createElement("div"); w.className = "flamewrap";
    w.innerHTML = run.inlineSvg; modalBody.appendChild(w);
  } else if(run.flamegraph){
    const f = document.createElement("iframe");
    f.src = run.flamegraph; f.loading = "lazy"; f.title = "flamegraph for " + run.label;
    modalBody.appendChild(f);
  } else {
    modalBody.innerHTML = '<div class="msg">no flamegraph.svg found for this run</div>';
  }
  modal.hidden = false;
  document.getElementById("modal-close").focus();
}
function closeFlame(){ modal.hidden = true; modalBody.innerHTML = ""; }
document.getElementById("modal-close").addEventListener("click", closeFlame);
modal.addEventListener("click", ev => { if(ev.target === modal) closeFlame(); });
document.addEventListener("keydown", ev => { if(ev.key === "Escape" && !modal.hidden) closeFlame(); });
</script>
"""


def _js_runs(runs: list[dict], base_dir: Path) -> list[dict]:
    out = []
    for r in runs:
        entry = {"label": r["label"], "instructions_g": r["instructions_g"],
                 "peak_mem_mb": r["peak_mem_mb"]}
        if r.get("inline_svg"):
            entry["inlineSvg"] = r["inline_svg"]
        elif r.get("flamegraph_path"):
            entry["flamegraph"] = os.path.relpath(r["flamegraph_path"], base_dir)
        out.append(entry)
    return out


def dashboard_inner(runs: list[dict], base_dir: Path) -> str:
    """Return the dashboard body (style + markup + script) with run data grouped
    by scenario. Real runs link to their flamegraph.svg by a path relative to
    the dashboard file; demo runs carry an inline synthetic flamegraph."""
    scenarios = [{"name": sc["name"], "runs": _js_runs(sc["runs"], base_dir)}
                 for sc in group_scenarios(runs)]
    return DASHBOARD_INNER.replace("__SCENARIOS__", json.dumps(scenarios))


def write_dashboard(runs: list[dict], out: str) -> None:
    inner = dashboard_inner(runs, Path(out).resolve().parent)
    doc = ('<!doctype html>\n<html lang="en">\n<head>\n'
           '<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
           '<title>k6-flamegraph dashboard</title>\n</head>\n<body>\n'
           + inner + '\n</body>\n</html>\n')
    Path(out).write_text(doc)
    scen = group_scenarios(runs)
    print(f"wrote {out}  ({len(scen)} scenario(s), {len(runs)} run(s))")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Chart CPU instructions and memory across k6-flamegraph runs.")
    ap.add_argument("paths", nargs="*",
                    help="metrics.json files (default: profile-*/metrics.json)")
    ap.add_argument("-o", "--out", default="metrics.png",
                    help="static image output (extension picks the format)")
    ap.add_argument("--html", metavar="FILE",
                    help="write an interactive HTML dashboard here instead of an image")
    ap.add_argument("--labels",
                    help="comma-separated labels overriding the run dir names")
    ap.add_argument("--demo", action="store_true",
                    help="use synthetic sample runs instead of reading files")
    args = ap.parse_args()

    if args.demo:
        runs = demo_runs()
    else:
        paths = args.paths or [str(p) for p in sorted(Path().glob("profile-*/metrics.json"))]
        if not paths:
            return "no metrics.json given and none match profile-*/metrics.json (try --demo)"
        runs = [load_run(p) for p in paths]

    if args.labels:
        for run, lbl in zip(runs, [s.strip() for s in args.labels.split(",")]):
            run["label"] = lbl

    if args.html:
        write_dashboard(runs, args.html)
    else:
        plot(runs, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
