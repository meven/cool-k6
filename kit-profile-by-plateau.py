#!/usr/bin/env python3
"""Split the kit samples of a capacity run by plateau.

    kit-profile-by-plateau.py RUN_DIR [--freq HZ] [--out by-plateau] [-- PERF COMMAND ...]

capacity-profile runs it before perf.data is pruned. perf unwinds the dwarf call graphs from the
binaries of the build that the run measured, so a later run on the same run directory needs that
build still in place. The perf command defaults to perf; capacity-profile passes its own, with
--buildid-dir.

It writes <out>/<users>-users.folded.gz for each plateau (zcat one into flamegraph.pl), and
<out>/summary.txt with the kit cores and the share of the callback and render functions per
plateau, and their kit milliseconds per edit.

perf time and wall time are aligned on the background saves: events.csv has the wall time at which
the sampler first saw each one, and perf.data the time of its fork.
"""
import argparse
import csv
import gzip
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

WATCH = [
    "desktop::COKitImpl::runLoop",
    "Document::drainCallbacks",
    "KitQueue::putCallback",
    "KitQueue::elideDuplicateCallback",
    "KitHelper::notifyOtherViewsUpdatePerViewId",
    "desktop::CallbackFlushHandler::enqueueUpdatedTypes",
    "desktop::CallbackFlushHandler::viewUpdatedCallbackPerViewId",
    "SfxViewShell::GetFirst",
    "SfxViewShell::GetNext",
    "SfxViewShell::GetForViewShellId",
    "SwView::getKitPayload",
    "Document::renderTiles",
    "sdr::table::PaintTableStylePreview",
    "Document::forkToSave",
]

HEADER = re.compile(r"^(\S.*?)\s+(\d+)(?:/(\d+))?\s+(?:\[\d+\]\s+)?(\d+\.\d+):")
FORK = re.compile(r"(\d+\.\d+): PERF_RECORD_FORK\((\d+):(\d+)\)")
FRAME = re.compile(r"^\s+[0-9a-f]+\s+(.*?)(?:\+0x[0-9a-f]+)?\s+\((.*)\)\s*$")
# A frame names a function with its parameters, e.g. "SfxViewShell::GetNext(SfxViewShell const&,
# bool, ...)"; WATCH holds the names without them.
PARAMS = re.compile(r"\(.*$")


def test_start_ms(run: Path, capacity: dict) -> tuple[int, str]:
    if "startMs" in capacity:
        return int(capacity["startMs"]), "capacity.json"
    # Older runs: the sampler starts with k6, a few seconds before the first iteration.
    with (run / "timeline.csv").open() as f:
        return int(next(csv.DictReader(f))["t_ms"]), "timeline.csv, a few seconds early"


def bgsave_starts(path: Path) -> dict[int, int]:
    with path.open() as f:
        return {int(r["pid"]): int(r["t_ms"]) for r in csv.DictReader(f)
                if r["event"] == "start" and r["name"].startswith("kitbgsv_")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--freq", type=int, default=49, help="perf record frequency, in Hz")
    ap.add_argument("--out", default="by-plateau")
    argv = sys.argv[1:]
    perf = ["perf"]
    if "--" in argv:
        perf = argv[argv.index("--") + 1:] or perf
        argv = argv[:argv.index("--")]
    args = ap.parse_args(argv)

    run = args.run_dir
    capacity = json.loads((run / "capacity.json").read_text())
    t0_ms, t0_from = test_start_ms(run, capacity)
    starts = bgsave_starts(run / "events.csv")

    cmd = perf + ["script", "-i", str(run / "perf.data"), "--no-inline", "--show-task-events",
                  "-F", "comm,pid,tid,time,ip,sym,dso"]
    print("running:", " ".join(cmd), file=sys.stderr)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, errors="replace",
                            bufsize=1 << 20)

    samples = []  # (perf time, folded stack) of the kit processes
    offsets = []
    unresolved = total_frames = 0
    head = None
    frames: list[str] = []

    def flush():
        nonlocal head, frames
        if head and frames:
            comm, t = head
            if comm.startswith("kitbroker") or comm.startswith("kit_"):
                samples.append((t, ";".join([comm] + frames[::-1])))
        head, frames = None, []

    for line in proc.stdout:
        if not line.strip():
            flush()
            continue
        m = FORK.search(line)
        if m:
            child = int(m.group(2))
            if child in starts:
                offsets.append(starts.pop(child) / 1000 - float(m.group(1)))
            continue
        if line[0] not in " \t":
            flush()
            m = HEADER.match(line)
            if m:
                head = (m.group(1).strip(), float(m.group(4)))
            continue
        m = FRAME.match(line)
        if m and head:
            total_frames += 1
            sym = m.group(1)
            if sym in ("[unknown]", ""):
                unresolved += 1
                sym = "[" + Path(m.group(2)).name + "]"
            frames.append(sym)
    flush()
    if proc.wait() != 0:
        print(f"perf script exited with {proc.returncode}", file=sys.stderr)
        return 1

    if not offsets:
        print("no background save fork found in perf.data, cannot align the clocks",
              file=sys.stderr)
        return 1
    off = statistics.median(offsets)
    spread = max(offsets) - min(offsets)

    out = run / args.out
    out.mkdir(exist_ok=True)
    rows = []
    for p in capacity["plateaus"]:
        lo = (t0_ms / 1000 + p["start"]) - off
        hi = (t0_ms / 1000 + p["end"]) - off
        folded = Counter()
        hits = Counter()
        n = 0
        for t, stack in samples:
            if lo <= t < hi:
                n += 1
                folded[stack] += 1
                names = {PARAMS.sub("", f) for f in stack.split(";")}
                for w in WATCH:
                    if w in names:
                        hits[w] += 1
        if n:
            with gzip.open(out / f"{p['users']:03d}-users.folded.gz", "wt") as f:
                for stack, c in folded.most_common():
                    f.write(f"{stack} {c}\n")
        rows.append((p, n, hits))

    lines = [
        f"{len(samples)} kit samples; test start from {t0_from}; clocks aligned on "
        f"{len(offsets)} background saves (spread {spread * 1000:.0f} ms); "
        f"unresolved frames {100 * unresolved / max(total_frames, 1):.1f}%",
    ]
    if total_frames and unresolved / total_frames > 0.2:
        lines.append("many frames are unresolved: the build was probably replaced since the run")
    lines += ["", "users edits kit cores  share of the kit samples per function, %"]
    short = [w.split("::")[-1] for w in WATCH]
    lines.append(f"{'':>27}" + " ".join(f"{s[:12]:>12}" for s in short))
    for p, n, hits in rows:
        dur = p["end"] - p["start"]
        lines.append(f"{p['users']:>5} {p['edits']:>5} {n / args.freq / dur:9.2f}      " +
                     " ".join(f"{100 * hits[w] / max(n, 1):12.1f}" for w in WATCH))
    lines += ["", "kit milliseconds per edit (samples / frequency / edits), top 5 below runLoop:"]
    for p, n, hits in rows:
        if p["edits"]:
            per = {w: 1000 * hits[w] / args.freq / p["edits"] for w in WATCH[1:]}
            top = sorted(per.items(), key=lambda kv: -kv[1])[:5]
            lines.append(f"{p['users']:>5} users: total {1000 * n / args.freq / p['edits']:.1f}; " +
                         ", ".join(f"{k.split('::')[-1]} {v:.1f}" for k, v in top))
    text = "\n".join(lines) + "\n"
    (out / "summary.txt").write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
