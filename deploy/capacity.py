"""The capacity runs of the store, for the Capacity tab of the dashboard: a summary of each
run, and the time series of one run on a common time axis.

deploy/run-capacity writes each run to <store>/capacity/<time>-<commit>/ through
capacity-profile. The series start at the start of k6: timeline-sampler.py, the
/cool/getMetrics sampler of server-memory.csv and k6 start together.
"""
import csv
import json
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


def runs(store: Path) -> list[dict]:
    """A summary of every capacity run, newest first."""
    root = store / "capacity"
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else ():
        m = RUN.fullmatch(d.name)
        if m and d.is_dir():
            out.append(summary(d, m, newest=not out))
    return out


def summary(d: Path, m: re.Match, newest: bool = True) -> dict:
    cap = _json(d / "capacity.json")
    cg = cgroup(d)
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
        "capacity": cap.get("capacity") if cap else None,
        "users_per_document": cap.get("usersPerDocument") if cap else None,
        "plateaus": plateaus(cap),
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
    t0 = _first_t_ms(d / "timeline.csv")
    out = summary(d, m)
    out.update(users=users(d), edits=edits(d, t0), cpu=cpu(d, t0), memory=memory(d))
    # the documents open during each stage, at most
    for p in out["plateaus"]:
        held = [m[5] for m in out["memory"] if p["start"] <= m[0] < p["end"]]
        p["documents"] = max(held) if held else None
    return out


def plateaus(cap: dict | None) -> list[dict]:
    rows = (cap or {}).get("plateaus") or []
    t = 0
    out = []
    for r in rows:
        # capacity.json gives each hold from 7 November 2026, older ones are the defaults
        start = r.get("start", t + RAMP_S)
        end = r.get("end", start + HOLD_S)
        t = end
        out.append({"users": r.get("users"), "start": start, "end": end,
                    "reached": r.get("reached"), "measured": r.get("measured"),
                    "ok": r.get("ok"), "edit_p95": r.get("editP95"), "edits": r.get("edits"),
                    "load_p95": r.get("loadP95"), "loads": r.get("loads"),
                    "error_rate": r.get("errorRate")})
    return out


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


def users(d: Path) -> list[list]:
    """The users k6 runs, once per bucket, from the progress lines of k6.log."""
    by: dict[int, int] = {}
    for line in _lines(d / "k6.log"):
        m = VUS.match(line)
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            by[int(t // STEP) * STEP] = int(m.group(3))
    return [[t, n] for t, n in sorted(by.items())]


def edits(d: Path, t0: int | None) -> list[list]:
    """[t, median ms, p95 ms, edits] of the keystrokes answered in each bucket."""
    if t0 is None:
        return []
    by: dict[int, list[int]] = {}
    for line in _lines(d / "k6.log"):
        m = EDIT.search(line)
        if m:
            t = (int(m.group(1)) - t0) / 1000
            by.setdefault(int(t // STEP) * STEP, []).append(int(m.group(2)))
    out = []
    for t, ms in sorted(by.items()):
        ms.sort()
        out.append([t, _pct(ms, 0.5), _pct(ms, 0.95), len(ms)])
    return out


def cpu(d: Path, t0: int | None) -> list[list]:
    """[t, server cores, k6 and browser cores] in each bucket, from timeline.csv: the cores
    that coolwsd, forkit, the kits and their background saves used, and those of the load."""
    if t0 is None:
        return []
    by: dict[int, list[tuple[float, float]]] = {}
    with _open(d / "timeline.csv") as f:
        for r in csv.DictReader(f) if f else ():
            try:
                t = (int(r["t_ms"]) - t0) / 1000
                server = sum(float(r.get(k) or 0) for k in
                             ("coolwsd_cpu", "forkit_cpu", "kit_cpu", "bgsave_cpu"))
                load = sum(float(r.get(k) or 0) for k in ("k6_cpu", "chromium_cpu", "wopi_cpu"))
            except (KeyError, ValueError):
                continue
            by.setdefault(int(t // STEP) * STEP, []).append((server, load))
    return [[t, round(sum(s for s, _ in v) / len(v), 2), round(sum(x for _, x in v) / len(v), 2)]
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
