"""The capacity runs of the store, for the Capacity tab of the dashboard: a summary of each
run, and the time series of one run on a common time axis.

deploy/run-capacity writes each run to <store>/capacity/<time>-<commit>/ through
capacity-profile. The series start at the start of k6: timeline-sampler.py, the
/cool/getMetrics sampler of server-memory.csv and k6 start together.
"""
import csv
import json
import os
import re
import time
from pathlib import Path

RUN = re.compile(r"(\d{8}T\d{6}Z)-([0-9a-f]{6,40})")
EDIT = re.compile(r"EDIT t_ms=(\d+) ms=(\d+)")
VUS = re.compile(r"^running \((\d+)m(\d+(?:\.\d+)?)s\), (\d+)/(\d+) VUs")
# A bucket of the series, in seconds: k6 prints the users each second, the timeline samples
# every 0.5 s and perf stat every 5 s.
STEP = 10
# The default stages of cool-capacity-test.js, for a capacity.json without the hold of each
# plateau.
RAMP_S, HOLD_S = 60, 180


# A run without capacity.json whose files have not changed for this long was stopped.
STALE_S = 600

# The summary and the series of each run, with the names, sizes and times of the files of the run
# that they were read from. k6.log alone reaches gigabytes when the users fail in a loop, so a run
# is read again only once its files change.
_CACHE: dict[tuple[str, str], tuple[tuple, dict]] = {}


def _files(d: Path) -> tuple:
    try:
        return tuple(sorted((e.name, e.stat().st_size, e.stat().st_mtime_ns)
                            for e in os.scandir(d) if e.is_file()))
    except OSError:
        return ()


def _cached(kind: str, d: Path, read) -> dict:
    files = _files(d)
    hit = _CACHE.get((kind, d.name))
    if hit and hit[0] == files:
        return hit[1]
    value = read()
    _CACHE[(kind, d.name)] = (files, value)
    return value


def runs(store: Path) -> list[dict]:
    """A summary of every capacity run, newest first."""
    root = store / "capacity"
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else ():
        m = RUN.fullmatch(d.name)
        if m and d.is_dir():
            newest = not out
            # The state of the newest run depends on the clock, not only on its files.
            out.append(summary(d, m, newest) if newest else
                       _cached("summary", d, lambda: summary(d, m, False)))
    return out


def summary(d: Path, m: re.Match, newest: bool = True) -> dict:
    cap = _json(d / "capacity.json")
    cg = cgroup(d)
    kills = oom_kills(d, _first_t_ms(d / "timeline.csv"))
    steps = plateaus(cap, kills)
    # Only the newest run can still be running, and only while it writes its files.
    if cap is not None:
        state = "done"
    elif newest and time.time() - _last_write(d) < STALE_S:
        state = "running"
    else:
        state = "stopped"
    return {
        "run": d.name,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(m.group(1), "%Y%m%dT%H%M%SZ")),
        "commit": m.group(2),
        # a run without capacity.json is still running, or k6 did not finish
        "done": cap is not None,
        "state": state,
        "capacity": capacity(cap, steps, kills, cg.get("oom_kill")),
        # the largest stage that k6 passed, whether or not the kernel killed in the server
        "capacity_k6": cap.get("capacity") if cap else None,
        "users_per_document": cap.get("usersPerDocument") if cap else None,
        # the CAP_SCENARIO of the run, which deploy/run-capacity also writes when the run starts,
        # or None for copies of one document
        "scenario": (cap or {}).get("scenario") or _text(d / "scenario"),
        "plateaus": steps,
        "cpus": cg.get("cpus"),
        "memory_max_mb": cg.get("memory_max_mb"),
        "memory_peak_mb": cg.get("memory_peak_mb"),
        "oom_kills": cg.get("oom_kill"),
        "flamegraph": _size(d / "flamegraph.svg") > 1000,
    }


def detail(store: Path, run: str) -> dict | None:
    """The series of one run, each as rows of [seconds from the start of k6, values...]."""
    m = RUN.fullmatch(run)
    d = store / "capacity" / run
    if not m or not d.is_dir():
        return None
    if _json(d / "capacity.json") is None:
        return _detail(d, m)
    # A copy, since the stages gain the documents of each below.
    return json.loads(json.dumps(_cached("detail", d, lambda: _detail(d, m))))


def _detail(d: Path, m: re.Match) -> dict:
    t0 = _first_t_ms(d / "timeline.csv")
    out = summary(d, m)
    users_by_step, edits_by_step = k6_series(d, t0)
    out.update(users=users_by_step, edits=edits_by_step, cpu=cpu(d, t0), memory=memory(d),
               oom=oom_kills(d, t0), cgroup_memory=cgroup_memory(d, t0),
               memory_kinds=memory_kinds(d, t0))
    # the documents open during each stage, at most
    for p in out["plateaus"]:
        held = [m[5] for m in out["memory"] if p["start"] <= m[0] < p["end"]]
        p["documents"] = max(held) if held else None
    return out


def plateaus(cap: dict | None, kills: list[list] | None = None) -> list[dict]:
    """The stages of capacity.json. With the OOM kills of the run, each stage also counts the
    kills from the end of the stage before, so that a kill in the ramp counts against the stage
    that the ramp leads to."""
    rows = (cap or {}).get("plateaus") or []
    t = 0
    out = []
    for r in rows:
        # capacity.json gives each hold from 7 November 2026, older ones are the defaults
        start = r.get("start", t + RAMP_S)
        end = r.get("end", start + HOLD_S)
        # the first stage that the run did not reach is where it stopped, often on these kills
        first_missed = not r.get("reached") and all(p["reached"] for p in out)
        oom = None if kills is None or not (r.get("reached") or first_missed) else \
            sum(n for k, n in kills if t <= k < end)
        t = end
        out.append({"users": r.get("users"), "start": start, "end": end, "oom_kills": oom,
                    "reached": r.get("reached"), "measured": r.get("measured"),
                    "ok": r.get("ok"), "edit_p95": r.get("editP95"), "edits": r.get("edits"),
                    "load_p95": r.get("loadP95"), "loads": r.get("loads"),
                    "error_rate": r.get("errorRate")})
    return out


def capacity(cap: dict | None, steps: list[dict], kills: list[list] | None,
             total_kills: int | None) -> int | None:
    """The users of the last stage before the first one that failed or in which the kernel
    killed a process of the server for lack of memory. A run that did not record when the kills
    happened has no capacity once it had one."""
    if not cap:
        return None
    if kills is None:
        return cap.get("capacity") if not total_kills else None
    best = 0
    for p in steps:
        if not (p["measured"] and p["ok"]) or p["oom_kills"]:
            break
        best = p["users"]
    return best


def oom_kills(d: Path, t0: int | None) -> list[list] | None:
    """[t, kills] of each bucket in which the kernel killed processes of the server cgroup for
    lack of memory, from the oom_kills column of timeline.csv, or None for a run from before
    that column."""
    if t0 is None:
        return None
    by: dict[int, int] = {}
    prev = None
    with _open(d / "timeline.csv") as f:
        for r in csv.DictReader(f) if f else ():
            try:
                t = (int(r["t_ms"]) - t0) / 1000
                n = int(r.get("oom_kills") or "")
            except (KeyError, ValueError):
                continue
            if prev is not None and n > prev:
                b = int(t // STEP) * STEP
                by[b] = by.get(b, 0) + n - prev
            prev = n
    return None if prev is None else [[t, n] for t, n in sorted(by.items())]


def cgroup(d: Path) -> dict:
    """The limits of the server cgroup and what it used, from server-cgroup.txt."""
    out: dict = {}
    try:
        lines = (d / "server-cgroup.txt").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        key, _, value = line.partition(":") if ":" in line else line.partition(" ")
        key, value = key.strip(), value.strip()
        if key == "cpuset.cpus":
            out["cpus"] = value
        elif key == "memory.max" and value.isdigit():
            out["memory_max_mb"] = round(int(value) / 1048576)
        elif key == "memory.peak" and value.isdigit():
            out["memory_peak_mb"] = round(int(value) / 1048576)
        elif key == "oom_kill" and value.isdigit():
            out["oom_kill"] = int(value)
    return out


def k6_series(d: Path, t0: int | None) -> tuple[list[list], list[list]]:
    """From one read of k6.log: the users k6 runs, once per bucket, from its progress lines, and
    [t, median ms, p95 ms, edits] of the keystrokes answered in each bucket, which is empty
    without @p t0."""
    users_by: dict[int, int] = {}
    edits_by: dict[int, list[int]] = {}
    for line in _lines(d / "k6.log"):
        if line.startswith("running ("):
            m = VUS.match(line)
            if m:
                t = int(m.group(1)) * 60 + float(m.group(2))
                users_by[int(t // STEP) * STEP] = int(m.group(3))
        elif t0 is not None and "EDIT t_ms=" in line:
            m = EDIT.search(line)
            if m:
                t = (int(m.group(1)) - t0) / 1000
                edits_by.setdefault(int(t // STEP) * STEP, []).append(int(m.group(2)))
    edit_rows = []
    for t, ms in sorted(edits_by.items()):
        ms.sort()
        edit_rows.append([t, _pct(ms, 0.5), _pct(ms, 0.95), len(ms)])
    return [[t, n] for t, n in sorted(users_by.items())], edit_rows


def cpu(d: Path, t0: int | None) -> list[list]:
    """The cores in each bucket, from timeline.csv, as rows of
    [t, server, load, background saves, most background saves at once,
     coolwsd and forkit, kits, k6, browser user, WOPI host].
    The server is coolwsd, forkit, the kits and their background saves, which all run in the
    server cgroup. The load is k6, the browser user and the WOPI host. The cores are averages
    over the bucket; the number of background saves is the most that ran at once."""
    if t0 is None:
        return []
    by: dict[int, list[tuple]] = {}
    with _open(d / "timeline.csv") as f:
        for r in csv.DictReader(f) if f else ():
            try:
                t = (int(r["t_ms"]) - t0) / 1000
                c = {k: float(r.get(f"{k}_cpu") or 0) for k in
                     ("coolwsd", "forkit", "kit", "bgsave", "k6", "chromium", "wopi")}
                bgsaves = int(r.get("bgsaves") or 0)
            except (KeyError, ValueError):
                continue
            server = c["coolwsd"] + c["forkit"] + c["kit"] + c["bgsave"]
            load = c["k6"] + c["chromium"] + c["wopi"]
            by.setdefault(int(t // STEP) * STEP, []).append(
                (server, load, c["bgsave"], bgsaves, c["coolwsd"] + c["forkit"], c["kit"],
                 c["k6"], c["chromium"], c["wopi"]))

    def mean(v: list[tuple], i: int) -> float:
        return round(sum(s[i] for s in v) / len(v), 2)

    return [[t, mean(v, 0), mean(v, 1), mean(v, 2), max(s[3] for s in v),
             *(mean(v, i) for i in range(4, 9))]
            for t, v in sorted(by.items())]


def cgroup_memory(d: Path, t0: int | None) -> list[list]:
    """[t, MB] of the server cgroup in each bucket, the most it held, from the cgroup_mb column
    of timeline.csv: what the kernel charges against the limit, background saves included.
    Empty for a run from before that column."""
    if t0 is None:
        return []
    by: dict[int, int] = {}
    with _open(d / "timeline.csv") as f:
        for r in csv.DictReader(f) if f else ():
            try:
                t = (int(r["t_ms"]) - t0) / 1000
                mb = int(r.get("cgroup_mb") or "")
            except (KeyError, ValueError):
                continue
            b = int(t // STEP) * STEP
            by[b] = max(by.get(b, 0), mb)
    return [[t, mb] for t, mb in sorted(by.items())]


def memory_kinds(d: Path, t0: int | None) -> list[list]:
    """The memory of the server by kind of process, in MB, as rows of
    [t, coolwsd, forkit, kits, background saves, not in a process], the averages over each bucket
    of the samples that have every column, from the Pss columns and cgroup_mb of timeline.csv.
    Pss splits each page among the processes that map it, so the kinds add up to what the server
    processes map; what the cgroup holds beyond that is page cache and kernel memory. Empty for a
    run from before the Pss columns, or whose processes could not be read."""
    if t0 is None:
        return []
    kinds = ("coolwsd", "forkit", "kit", "bgsave")
    by: dict[int, list[list[int]]] = {}
    with _open(d / "timeline.csv") as f:
        for r in csv.DictReader(f) if f else ():
            try:
                t = (int(r["t_ms"]) - t0) / 1000
                pss = [int(r[f"{k}_pss_mb"]) for k in kinds]
                cg = int(r["cgroup_mb"])
            except (KeyError, ValueError, TypeError):
                continue
            by.setdefault(int(t // STEP) * STEP, []).append(pss + [max(0, cg - sum(pss))])
    return [[t, *(round(sum(s[i] for s in v) / len(v)) for i in range(5))]
            for t, v in sorted(by.items())]


def memory(d: Path) -> list[list]:
    """[t, coolwsd MB, forkit MB, kits MB, kits, documents] from server-memory.csv, as
    coolwsd reports them: the kits include the spare one, and the documents are the kits
    with a document open. A row where coolwsd did not answer is left out."""
    out = []
    with _open(d / "server-memory.csv") as f:
        for r in csv.DictReader(f) if f else ():
            try:
                out.append([int(r["elapsed_s"]), int(r["coolwsd_mb"]), int(r["forkit_mb"]),
                            int(r["kits_mb"]), int(r["kit_count"]),
                            int(r["kit_assigned"])])
            except (KeyError, ValueError):
                continue
    return out


def _pct(sorted_values: list[int], q: float) -> int:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def _first_t_ms(p: Path) -> int | None:
    with _open(p) as f:
        for r in csv.DictReader(f) if f else ():
            try:
                return int(r["t_ms"])
            except (KeyError, ValueError):
                continue
    return None


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _text(p: Path) -> str | None:
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _lines(p: Path):
    try:
        with p.open(errors="replace") as f:
            yield from f
    except OSError:
        return


class _open:
    """open() for reading, that gives None for a missing file instead of failing."""

    def __init__(self, p: Path):
        self.p, self.f = p, None

    def __enter__(self):
        try:
            self.f = self.p.open(newline="", errors="replace")
        except OSError:
            self.f = None
        return self.f

    def __exit__(self, *a):
        if self.f:
            self.f.close()


def _last_write(d: Path) -> float:
    """The time of the latest change to a file of the run."""
    times = [d.stat().st_mtime]
    for f in d.iterdir():
        try:
            times.append(f.stat().st_mtime)
        except OSError:
            pass
    return max(times)
