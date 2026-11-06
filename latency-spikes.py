#!/usr/bin/env python3
"""
latency-spikes.py: line the slow edits of a capacity run up with what the server and host did then.

Reads from a capacity-profile output directory:
  - k6.log: the EDIT lines cool-capacity-test.js logs, one per edit, with the time the server
    answered, the latency and the document group;
  - timeline.csv, kit-cpu.csv, events.csv: what timeline-sampler.py saw every 0.5 s;
  - metrics.prom: which kit pid served which document group (from coolwsd's doc_info).

For each edit it looks at the window from the keystroke to the answer and notes:
  - own_save:    a background save of the edit's own document ran in the window, or started in the
                 second before it (the fork and the copy-on-write faults that follow it);
  - other_save:  a background save of another document started in the window;
  - swap:        the host swapped pages in or out during the window;
  - cpu_full:    the host's CPUs were at least 90% busy in some sample of the window;
  - kit_busy:    the document's kit used at least 0.8 CPU in some sample of the window.

It prints, for each factor, its share among slow edits next to its share among all edits: a factor
that is as common in every edit as in the slow ones does not explain them. Then the slowest edits
with their factors, and the 10-second spans with the most slow edits.

    ./latency-spikes.py results/capacity-<time> [--slow-ms 300] [--top 20]
"""
import argparse
import bisect
import csv
import os
import re
import sys
from collections import defaultdict

EDIT_RE = re.compile(r'EDIT t_ms=(\d+) ms=(\d+) doc=(\S+) vu=(\d+)')
DOC_INFO_RE = re.compile(r'^doc_info\{[^}]*key="[^"]*~(\d+)"[^}]*pid="(\d+)"')
FACTORS = ("own_save", "other_save", "swap", "cpu_full", "kit_busy")


def load_edits(out_dir):
    edits = []
    with open(os.path.join(out_dir, "k6.log"), errors="replace") as f:
        for line in f:
            m = EDIT_RE.search(line)
            if m:
                edits.append({"t": int(m[1]), "ms": int(m[2]), "doc": m[3], "vu": int(m[4])})
    return edits


def load_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def doc_of_pid(out_dir):
    """kit pid -> document group, from coolwsd's doc_info metric."""
    out = {}
    path = os.path.join(out_dir, "metrics.prom")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                m = DOC_INFO_RE.match(line)
                if m:
                    out[int(m[2])] = m[1]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir")
    parser.add_argument("--slow-ms", type=int, default=300)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--cpus", type=int, default=os.cpu_count())
    args = parser.parse_args()

    edits = load_edits(args.out_dir)
    if not edits:
        print("no EDIT lines in k6.log: the run predates the per-edit logging", file=sys.stderr)
        return 1
    timeline = load_csv(os.path.join(args.out_dir, "timeline.csv"))
    tl_t = [int(r["t_ms"]) for r in timeline]
    events = load_csv(os.path.join(args.out_dir, "events.csv"))
    kit_rows = load_csv(os.path.join(args.out_dir, "kit-cpu.csv"))

    # Document group of each docbroker id, through the kit pid coolwsd reports for the group.
    pid_doc = doc_of_pid(args.out_dir)
    broker_doc = {}
    for e in events:
        if e["name"].startswith("kitbroker_") and int(e["pid"]) in pid_doc:
            broker_doc[e["name"][len("kitbroker_"):]] = pid_doc[int(e["pid"])]

    # Background saves as (start, end, document group).
    open_saves = {}
    saves = []
    for e in events:
        if not e["name"].startswith("kitbgsv_"):
            continue
        if e["event"] in ("start", "present"):
            open_saves[e["pid"]] = (int(e["t_ms"]), e["name"])
        elif e["event"] == "end" and e["pid"] in open_saves:
            start, name = open_saves.pop(e["pid"])
            broker = name[len("kitbgsv_"):].rsplit("_", 1)[0]
            saves.append((start, int(e["t_ms"]), broker_doc.get(broker)))
    save_starts = sorted(s[0] for s in saves)

    # CPU of each document's kit, as (t, cpu) lists.
    kit_cpu = defaultdict(list)
    for r in kit_rows:
        doc = pid_doc.get(int(r["pid"]))
        if doc is not None:
            kit_cpu[doc].append((int(r["t_ms"]), float(r["cpu"])))

    def rows_in(t0, t1):
        # The sample that ends after t0 covers the start of the window.
        i = bisect.bisect_left(tl_t, t0)
        j = bisect.bisect_right(tl_t, t1 + 500)
        return timeline[i:j]

    def factors(edit):
        t1 = edit["t"]
        t0 = t1 - edit["ms"]
        rows = rows_in(t0, t1)
        f = {}
        f["own_save"] = any(doc == edit["doc"] and start - 1000 <= t1 and end >= t0
                            for start, end, doc in saves)
        f["other_save"] = any(doc != edit["doc"] for start, end, doc in saves if t0 <= start <= t1)
        f["swap"] = any(int(r["swap_in_pages"]) + int(r["swap_out_pages"]) > 0 for r in rows)
        f["cpu_full"] = any(float(r["cpu_busy"]) >= 0.9 * args.cpus for r in rows)
        f["kit_busy"] = any(c >= 0.8 for t, c in kit_cpu.get(edit["doc"], ()) if t0 <= t <= t1 + 500)
        return f

    for e in edits:
        e["f"] = factors(e)
    slow = [e for e in edits if e["ms"] >= args.slow_ms]

    print(f"{len(edits)} edits, {len(slow)} at {args.slow_ms} ms or more "
          f"({100 * len(slow) / len(edits):.1f}%), {len(saves)} background saves, "
          f"{sum(1 for s in saves if s[2] is None)} of them not matched to a document group")
    print()
    print(f"{'factor':<12} {'slow edits':>11} {'all edits':>10}  ratio")
    for name in FACTORS:
        in_slow = sum(e["f"][name] for e in slow) / max(1, len(slow))
        in_all = sum(e["f"][name] for e in edits) / len(edits)
        ratio = f"{in_slow / in_all:.1f}x" if in_all else "-"
        print(f"{name:<12} {100 * in_slow:10.1f}% {100 * in_all:9.1f}%  {ratio}")
    unexplained = sum(1 for e in slow if not any(e["f"].values()))
    print(f"{'none':<12} {100 * unexplained / max(1, len(slow)):10.1f}%")

    print()
    print(f"slowest {args.top} edits:")
    t_base = edits[0]["t"] - edits[0]["ms"]
    for e in sorted(edits, key=lambda e: -e["ms"])[:args.top]:
        on = ",".join(k for k, v in e["f"].items() if v) or "-"
        print(f"  +{(e['t'] - t_base) / 1000:7.1f}s  {e['ms']:5d} ms  doc {e['doc']:>3}  vu {e['vu']:>3}  {on}")

    print()
    print("10-second spans with the most slow edits:")
    buckets = defaultdict(list)
    for e in edits:
        buckets[(e["t"] - t_base) // 10000].append(e)
    worst = sorted(buckets.items(), key=lambda kv: -sum(e["ms"] >= args.slow_ms for e in kv[1]))[:8]
    print(f"  {'span':>9} {'edits':>6} {'slow':>5} {'p50':>6} {'max':>6} {'saves started':>14} "
          f"{'swap pages':>11} {'cpu busy max':>13}")
    for b, es in sorted(worst):
        t0 = t_base + b * 10000
        rows = rows_in(t0, t0 + 10000)
        ms = sorted(e["ms"] for e in es)
        starts = bisect.bisect_left(save_starts, t0 + 10000) - bisect.bisect_left(save_starts, t0)
        swap = sum(int(r["swap_in_pages"]) + int(r["swap_out_pages"]) for r in rows)
        busy = max((float(r["cpu_busy"]) for r in rows), default=0)
        print(f"  +{b * 10:>6}s {len(es):>6} {sum(m >= args.slow_ms for m in ms):>5} "
              f"{ms[len(ms) // 2]:>6} {ms[-1]:>6} {starts:>14} {swap:>11} {busy:>13.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
