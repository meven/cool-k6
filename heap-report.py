#!/usr/bin/env python3
"""Turn the heaptrack traces of a capacity run into flamegraphs and numbers per process kind.

capacity-profile runs coolwsd under `heaptrack --follow-children --raw` when HEAPTRACK is set,
so that every process of the server, the kits that forkit forks included, writes a raw trace of
its own into OUT_DIR/heaptrack/, named ht.<pid>.raw.zst, or ht.<pid>.1.raw.zst for a process
that exec started. This script:

  - interprets the raw traces in parallel, into ht.<pid>.zst data files;
  - names the kind of each process: coolwsd and forkit by the pids given, the kits, their
    background saves and the spare kits by the names in events.csv, any other process "other";
  - writes heap-<kind>-<cost>.svg, a flamegraph of the stacks of all the processes of a kind,
    for the costs peak (bytes held at the peak of each process), allocations (calls),
    temporary (allocations freed before any other allocation) and leaked (bytes still held
    when the process ended);
  - writes heap-<kind>.txt, the heaptrack_print report of the process of that kind that
    allocated the most, with the full stacks of its most temporary allocations;
  - writes heaptrack.json, the numbers of each process and of each kind, and heap.html, a page
    of those numbers with links to the flamegraphs and the reports.

The peak of a kind is the sum of the peaks of its processes, which need not have happened at
the same time.

    ./heap-report.py OUT_DIR --heaptrack /path/to/bin/heaptrack \\
        [--coolwsd-pid PID] [--forkit-pid PID] [--jobs N] [--keep-data]
"""
import argparse
import concurrent.futures
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

COSTS = ("peak", "allocations", "temporary", "leaked")
COUNT_NAMES = {"peak": "bytes", "allocations": "allocations", "temporary": "allocations",
               "leaked": "bytes"}
# A data file that holds less than this recorded no allocation worth a report: the short-lived
# helpers that coolwsd runs, such as coolmount, write a few hundred bytes.
MIN_DATA_BYTES = 4096
SUMMARY = {
    "runtime_s": re.compile(r"total runtime: ([\d.]+)s"),
    "allocations": re.compile(r"calls to allocation functions: (\d+)"),
    "temporary": re.compile(r"temporary memory allocations: (\d+)"),
    "peak": re.compile(r"peak heap memory consumption: ([\d.]+)([KMGT]?)"),
    "peak_rss": re.compile(r"peak RSS \(including heaptrack overhead\): ([\d.]+)([KMGT]?)"),
    "leaked": re.compile(r"total memory leaked: ([\d.]+)([KMGT]?)"),
}
UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
PID = re.compile(r"^ht\.(\d+)(\.\d+)?$")


def tools(heaptrack: Path) -> tuple[Path, Path]:
    """heaptrack_interpret and heaptrack_print of the heaptrack installation of @p heaptrack."""
    prefix = heaptrack.resolve().parent.parent
    interpret = prefix / "lib" / "heaptrack" / "libexec" / "heaptrack_interpret"
    print_ = heaptrack.resolve().parent / "heaptrack_print"
    for tool in (interpret, print_):
        if not os.access(tool, os.X_OK):
            sys.exit(f"heap-report: {tool} is missing, beside {heaptrack}")
    return interpret, print_


def interpret(interpreter: Path, raw: Path) -> Path:
    """The data file of the raw trace @p raw, .raw or .raw.zst, which is removed after."""
    name = raw.name.removesuffix(".zst").removesuffix(".raw")
    out = raw.with_name(name + ".zst")
    reader = (subprocess.Popen(["zstd", "-dc", str(raw)], stdout=subprocess.PIPE)
              if raw.suffix == ".zst" else subprocess.Popen(["cat", str(raw)],
                                                            stdout=subprocess.PIPE))
    with open(out, "wb") as f:
        interp = subprocess.Popen([str(interpreter)], stdin=reader.stdout, stdout=subprocess.PIPE)
        reader.stdout.close()
        zstd = subprocess.Popen(["zstd", "-q", "-c"], stdin=interp.stdout, stdout=f)
        interp.stdout.close()
        zstd.wait()
        interp.wait()
        reader.wait()
    if interp.returncode == 0 and zstd.returncode == 0:
        raw.unlink()
    else:
        print(f"heap-report: interpreting {raw.name} failed", file=sys.stderr)
    return out


def names(out_dir: Path) -> dict[int, str]:
    """The last name of each process in events.csv, by pid: a spare kit takes the name of the
    document kit that it becomes."""
    found: dict[int, str] = {}
    try:
        with open(out_dir / "events.csv", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    found[int(row["pid"])] = row["name"]
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return found


def kind_of(pid: int, by_name: dict[int, str], coolwsd: int | None, forkit: int | None) -> str:
    if pid == coolwsd:
        return "coolwsd"
    if pid == forkit:
        return "forkit"
    name = by_name.get(pid, "")
    for prefix, kind in (("kitbgsv", "bgsave"), ("kitbroker", "kit"), ("kit_spare", "spare")):
        if name.startswith(prefix):
            return kind
    return "other"


def summary(printer: Path, data: Path) -> dict:
    text = subprocess.run([str(printer), "-f", str(data), "-p", "0", "-a", "0", "-T", "0",
                           "-l", "0", "-s", "0"], capture_output=True, text=True).stdout
    out: dict = {}
    for key, rx in SUMMARY.items():
        m = rx.search(text)
        if not m:
            continue
        if key in ("peak", "peak_rss", "leaked"):
            out[key] = round(float(m.group(1)) * UNITS[m.group(2)])
        elif key == "runtime_s":
            out[key] = float(m.group(1))
        else:
            out[key] = int(m.group(1))
    return out


def stacks(printer: Path, data: Path, cost: str) -> dict[str, int]:
    """The folded stacks of @p data for @p cost, as stack -> count."""
    folded = data.with_name(f"{data.name}.{cost}.stacks")
    subprocess.run([str(printer), "-f", str(data), "-p", "0", "-a", "0", "-T", "0", "-l", "0",
                    "-s", "0", "--flamegraph-cost-type", cost, "-F", str(folded)],
                   capture_output=True)
    out: dict[str, int] = {}
    try:
        with open(folded, errors="replace") as f:
            for line in f:
                stack, _, count = line.rstrip("\n").rpartition(" ")
                if stack and count.isdigit() and int(count):
                    out[stack] = out.get(stack, 0) + int(count)
    finally:
        folded.unlink(missing_ok=True)
    return out


KIND_ORDER = ("kit", "bgsave", "spare", "coolwsd", "forkit", "other")
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heap profile</title>
<style>
:root {{ --bg: #fff; --fg: #1d1d1f; --muted: #6b6b70; --line: #ddd; --link: #5c2983; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #17171a; --fg: #e8e8ea; --muted: #9a9aa2; --line: #333; --link: #c9a4e8; }}
}}
body {{ background: var(--bg); color: var(--fg); font: 14px/1.4 system-ui, sans-serif;
        margin: 0 auto; padding: 16px; max-width: 1100px; }}
.wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; }}
th, td {{ padding: 4px 10px; border-bottom: 1px solid var(--line); text-align: right;
          white-space: nowrap; }}
th:first-child, td:first-child, th.l, td.l {{ text-align: left; }}
a {{ color: var(--link); }}
p {{ color: var(--muted); max-width: 70ch; }}
</style></head><body>
<h1>Heap profile of {run}</h1>
<p>Recorded with heaptrack on every process of the server. heaptrack slows the server down, so
the timings and the capacity of this run do not compare with other runs. A peak is the most heap
that one process held; the sum adds the peaks of the processes of a kind, which need not happen at
the same time. A temporary allocation is freed before any other allocation.</p>
<div class="wrap"><table>
<thead><tr><th>kind</th><th>processes</th><th>allocations</th><th>per second</th>
<th>temporary</th><th>peak, largest</th><th>peak, sum</th><th>leaked</th>
<th class="l">flamegraphs</th><th class="l">report</th></tr></thead>
<tbody>
{rows}
</tbody></table></div>
</body></html>
"""


def page(run: str, kinds: dict) -> str:
    def mb(n):
        return f"{n / 1e6:.1f} MB"

    rows = []
    for kind in sorted(kinds, key=lambda k: KIND_ORDER.index(k) if k in KIND_ORDER else 99):
        k = kinds[kind]
        rate = k["allocations"] / k["runtime_s"] if k["runtime_s"] else 0
        share = f"{100 * k['temporary_share']:.1f}%" if k["temporary_share"] is not None else "-"
        links = " ".join(f'<a href="{html.escape(name)}">{cost}</a>'
                         for cost, name in k.get("flamegraphs", {}).items())
        report = k.get("report")
        rows.append(
            f"<tr><td>{kind}</td><td>{k['processes']}</td><td>{k['allocations']:,}</td>"
            f"<td>{rate:,.0f}</td><td>{share}</td><td>{mb(k['peak_max'])}</td>"
            f"<td>{mb(k['peak_sum'])}</td><td>{mb(k['leaked'])}</td><td class='l'>{links}</td>"
            f"<td class='l'>" + (f'<a href="{html.escape(report)}">text</a>' if report else "")
            + "</td></tr>")
    return PAGE.format(run=html.escape(run), rows="\n".join(rows))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--heaptrack", type=Path, required=True,
                    help="the heaptrack script that recorded the traces")
    ap.add_argument("--coolwsd-pid", type=int)
    ap.add_argument("--forkit-pid", type=int)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--keep-data", action="store_true",
                    help="keep the data files, which heaptrack --analyze opens")
    args = ap.parse_args()

    trace_dir = args.out_dir / "heaptrack"
    interpreter, printer = tools(args.heaptrack)
    for tool in ("zstd", "flamegraph.pl"):
        if not shutil.which(tool):
            sys.exit(f"heap-report: {tool} is not on PATH")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs))

    raws = sorted(trace_dir.glob("ht.*.raw*"))
    list(pool.map(lambda raw: interpret(interpreter, raw), raws))
    data = [p for p in sorted(trace_dir.glob("ht.*.zst"))
            if not p.name.endswith(".raw.zst") and p.stat().st_size >= MIN_DATA_BYTES]

    by_name = names(args.out_dir)
    processes = []
    for p in data:
        m = PID.match(p.name.removesuffix(".zst"))
        if m:
            pid = int(m.group(1))
            processes.append({"pid": pid, "file": p,
                              "kind": kind_of(pid, by_name, args.coolwsd_pid, args.forkit_pid),
                              "name": by_name.get(pid)})

    for proc, numbers in zip(processes, pool.map(lambda pr: summary(printer, pr["file"]),
                                                 processes)):
        proc.update(numbers)

    kinds: dict[str, dict] = {}
    for proc in processes:
        k = kinds.setdefault(proc["kind"], {"processes": 0, "allocations": 0, "temporary": 0,
                                             "peak_sum": 0, "peak_max": 0, "leaked": 0,
                                             "runtime_s": 0.0})
        k["processes"] += 1
        for key in ("allocations", "temporary", "leaked"):
            k[key] += proc.get(key, 0)
        k["peak_sum"] += proc.get("peak", 0)
        k["peak_max"] = max(k["peak_max"], proc.get("peak", 0))
        k["runtime_s"] = max(k["runtime_s"], proc.get("runtime_s", 0.0))
    for k in kinds.values():
        k["temporary_share"] = round(k["temporary"] / k["allocations"], 4) if k["allocations"] \
            else None

    # One flamegraph per kind and cost, from the stacks of all its processes.
    jobs = [(kind, cost) for kind in kinds for cost in COSTS]

    def draw(job):
        kind, cost = job
        merged: dict[str, int] = {}
        for proc in processes:
            if proc["kind"] == kind:
                for stack, n in stacks(printer, proc["file"], cost).items():
                    merged[stack] = merged.get(stack, 0) + n
        if not merged:
            return None
        svg = args.out_dir / f"heap-{kind}-{cost}.svg"
        folded = "".join(f"{s} {n}\n" for s, n in merged.items())
        with open(svg, "w") as f:
            subprocess.run(["flamegraph.pl", "--title", f"{kind}: {cost}", "--countname",
                            COUNT_NAMES[cost], "--colors", "mem"],
                           input=folded, text=True, stdout=f)
        return svg.name

    drawn = {job: name for job, name in zip(jobs, pool.map(draw, jobs)) if name}
    for (kind, cost), name in drawn.items():
        kinds[kind].setdefault("flamegraphs", {})[cost] = name

    # The report of the busiest process of each kind, with the full stacks of its most temporary
    # allocations and of its peak.
    def report(kind):
        busiest = max((p for p in processes if p["kind"] == kind),
                      key=lambda p: p.get("allocations", 0))
        text = subprocess.run([str(printer), "-f", str(busiest["file"]), "-n", "10", "-p", "1",
                               "-a", "1", "-T", "1", "-l", "0", "-s", "0",
                               "--merge-backtraces=false"],
                              capture_output=True, text=True).stdout
        name = f"heap-{kind}.txt"
        (args.out_dir / name).write_text(f"process {busiest['pid']} "
                                         f"({busiest.get('name') or kind})\n\n{text}")
        return name

    for kind, name in zip(list(kinds), pool.map(report, list(kinds))):
        kinds[kind]["report"] = name

    result = {"kinds": kinds,
              "processes": [{k: v for k, v in p.items() if k != "file"} for p in processes]}
    (args.out_dir / "heaptrack.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.out_dir / "heap.html").write_text(page(args.out_dir.resolve().name, kinds))
    if not args.keep_data:
        for p in trace_dir.glob("ht.*"):
            p.unlink()
        try:
            trace_dir.rmdir()
        except OSError:
            pass
    for kind, k in sorted(kinds.items()):
        share = f"{100 * k['temporary_share']:.1f}%" if k["temporary_share"] is not None else "-"
        print(f"{kind:8} {k['processes']:3} processes, {k['allocations']:>10} allocations, "
              f"{share:>6} temporary, peak {k['peak_max'] / 1e6:.1f} MB at most")


if __name__ == "__main__":
    main()
