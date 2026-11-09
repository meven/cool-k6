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
    t0 = _first_t_ms(d / "timeline.csv")
    out = summary(d, m)
    out.update(users=users(d), edits=edits(d, t0), cpu=cpu(d, t0), memory=memory(d),
               oom=oom_kills(d, t0))
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
