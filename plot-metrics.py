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
import string
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

# The page in templates/: the html, with its css, its script and the runs put in place of
# $css, $js and $scenarios.
TEMPLATES = Path(__file__).resolve().parent / "templates"


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


def write_dashboard(runs: list[dict], out: str) -> None:
    """Write the dashboard with the runs grouped by scenario. Real runs link to their
    flamegraph.svg by a path relative to the dashboard file; demo runs carry an inline
    synthetic flamegraph."""
    base_dir = Path(out).resolve().parent
    scenarios = [{"name": sc["name"], "runs": _js_runs(sc["runs"], base_dir)}
                 for sc in group_scenarios(runs)]
    page = string.Template((TEMPLATES / "metrics-dashboard.html").read_text()).substitute(
        css=(TEMPLATES / "metrics-dashboard.css").read_text(),
        js=(TEMPLATES / "metrics-dashboard.js").read_text(),
        scenarios=json.dumps(scenarios))
    Path(out).write_text(page)
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
