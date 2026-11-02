"""Shared helpers for k6-flamegraph.py: number/byte formatting, k6-log
scraping, perf-stat parsing, process memory sampling, and the run-report
builders. Kept separate so the orchestration script stays readable and so
the pure, data-in/data-out functions can be imported and unit-tested.
"""

import re
import subprocess
import time
import json
from pathlib import Path

__all__ = [
    "mean_stddev",
    "fmt_int_stat",
    "fmt_bytes",
    "fmt_bytes_short",
    "fmt_bytes_stat",
    "fmt_count",
    "parse_perf_stat",
    "scrape_timings_per_vu",
    "scrape_start_times_per_vu",
    "scrape_network_bytes_per_vu",
    "pgrep",
    "document_kit_pids",
    "proc_cgroup",
    "coolwsd_cgroup",
    "read_proc_mem",
    "group_mem",
    "MemorySampler",
    "collect_workload",
    "print_workload",
    "collect_perf",
    "print_perf",
    "print_memory",
    "flamegraph_sample_count",
]


def scrape_timings_per_vu(k6_log: Path) -> dict[int, dict]:
    """Pull TIMING: {...} lines bucketed by the Vu field in each
    JSON payload. Payload fields: DOMReadyTime, DocumentLoadedTime,
    DOMToDocumentLoadedMs, Vu."""
    if not k6_log.exists():
        return {}
    out: dict[int, dict] = {}
    for line in k6_log.read_text(errors="replace").splitlines():
        m = re.search(r"TIMING:\s*(\{.*\})", line)
        if not m:
            continue
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        vu = int(payload.get("Vu") or 1)
        out[vu] = payload
    return out


def scrape_start_times_per_vu(k6_log: Path) -> dict[int, int]:
    """Pull 'START_TIME: <ms> vu=<n>' lines bucketed by VU.
    First emission per VU wins."""
    if not k6_log.exists():
        return {}
    out: dict[int, int] = {}
    pat = re.compile(r"START_TIME:\s*(\d+)(?:\s+vu=(\d+))?")
    for line in k6_log.read_text(errors="replace").splitlines():
        m = pat.search(line)
        if not m:
            continue
        ts = int(m.group(1))
        vu = int(m.group(2)) if m.group(2) else 1
        out.setdefault(vu, ts)
    return out


def scrape_network_bytes_per_vu(k6_log: Path) -> dict[int, dict]:
    """Pull 'network bytes: sent=N, received=N vu=<n>' lines
    bucketed by VU. Last emission per VU wins."""
    if not k6_log.exists():
        return {}
    out: dict[int, dict] = {}
    pat = re.compile(
        r"network bytes:\s*sent=(\d+),\s*received=(\d+)(?:\s+vu=(\d+))?"
    )
    for line in k6_log.read_text(errors="replace").splitlines():
        m = pat.search(line)
        if not m:
            continue
        vu = int(m.group(3)) if m.group(3) else 1
        out[vu] = {"sent": int(m.group(1)), "received": int(m.group(2))}
    return out


def mean_stddev(values: list[float]) -> tuple[float, float]:
    """Return (mean, sample standard deviation). Uses the n-1
    (Bessel-corrected) sample stddev. For a single value the
    stddev is 0."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n == 1:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, var ** 0.5


def fmt_int_stat(values: list[float], unit: str = "") -> str:
    """Format a stats line: 'mean (sd) unit [v1, v2, ...]'."""
    if not values:
        return "n/a"
    m, sd = mean_stddev(values)
    suffix = f" {unit}" if unit else ""
    if len(values) == 1:
        return f"{int(round(m))}{suffix}"
    breakdown = ", ".join(str(int(round(v))) for v in values)
    return f"{int(round(m))} ({int(round(sd))}){suffix}  [{breakdown}]"


def _scale_bytes(n: int) -> tuple[float, str]:
    """Scale n to the largest binary unit below 1024, as (value, unit)."""
    units = ("B", "KiB", "MiB", "GiB")
    v = float(n)
    u = 0
    while v >= 1024 and u < len(units) - 1:
        v /= 1024
        u += 1
    return v, units[u]


def fmt_bytes(n: int) -> str:
    """Render n as a raw byte count plus a human-readable size."""
    v, unit = _scale_bytes(n)
    return f"{n} ({v:.1f} {unit})"


def fmt_bytes_short(n: int) -> str:
    """Render n as a single human-readable size only (no raw count)."""
    v, unit = _scale_bytes(n)
    if unit == "B":
        return f"{int(v)} B"
    return f"{v:.1f} {unit}"


def fmt_bytes_stat(values: list[int]) -> str:
    """Format a byte-stats line with per-VU breakdown."""
    if not values:
        return "n/a"
    m, sd = mean_stddev(values)
    if len(values) == 1:
        return fmt_bytes(int(round(m)))
    breakdown = ", ".join(fmt_bytes_short(v) for v in values)
    return (f"{fmt_bytes_short(int(round(m)))} "
            f"(sd {fmt_bytes_short(int(round(sd)))})  [{breakdown}]")


def _normalize_event_name(event: str) -> str:
    """Strip hybrid PMU prefix from event names.

    Intel hybrid CPUs report 'instructions' as separate counts under
    'cpu_atom/instructions/' and 'cpu_core/instructions/'. Collapse
    both onto the bare event name so a downstream sum yields the
    whole-process total.
    """
    m = re.match(r"cpu_(?:atom|core|p|e)/([^/]+)/[a-zA-Z]*$", event)
    if m:
        return m.group(1)
    # a bare event may still carry a :modifier (instructions:u, task-clock:u)
    return event.split(":", 1)[0]


def parse_perf_stat(csv_path: Path) -> dict[str, float]:
    """Parse a perf stat CSV output (-x ,) into {event_name: count}.

    Sums all PMUs that map to the same logical event (e.g. atom + core
    on Intel hybrid). Skips '#' comments and <not counted> rows.
    """
    if not csv_path.exists():
        return {}
    out: dict[str, float] = {}
    for raw in csv_path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        # perf stat -x , always emits: value,unit,event,run-time,pct,...
        # so the event is parts[2] regardless of whether a unit is present.
        count_s, event = parts[0], parts[2]
        if count_s in ("<not counted>", "<not supported>", ""):
            continue
        name = _normalize_event_name(event)
        try:
            value = float(count_s)
        except ValueError:
            continue
        out[name] = out.get(name, 0.0) + value
    return out


def fmt_count(n: float) -> str:
    """Render a large count with a K/M/G suffix."""
    abs_n = abs(n)
    if abs_n >= 1e9:
        return f"{n / 1e9:.2f} G"
    if abs_n >= 1e6:
        return f"{n / 1e6:.2f} M"
    if abs_n >= 1e3:
        return f"{n / 1e3:.2f} K"
    return f"{int(n)}"


def pgrep(args: list[str]) -> list[str]:
    res = subprocess.run(["pgrep", *args], capture_output=True, text=True)
    if res.returncode not in (0, 1):
        return []
    return [p for p in res.stdout.split() if p]


def document_kit_pids() -> list[str]:
    """Live kit processes serving a document. The doc kit's comm is
    kitbroker_<hash> (see wsd/AdminModel.cpp), and its lokit_runloop thread runs
    the LibreOfficeKit work. This is deliberately NOT kit_spare_* / subforkit_* /
    forkit, which do no document work."""
    return pgrep(["^kitbroker_"])


def read_proc_mem(pid: str) -> dict[str, int]:
    """Memory of one pid in kB, or {} if the process is gone or unreadable.

    VmRSS is the current resident size and VmHWM the peak resident size since
    the process started (both from /proc/<pid>/status, always readable for our
    own processes). The rest come from /proc/<pid>/smaps_rollup, which may be
    unreadable, in which case they are simply left out.

    Pss (proportional set size) splits shared pages by the number of sharers.
    Private_Dirty is the memory this process alone owns and would have to be
    reclaimed if it exited, which for a document kit is what that one document
    costs on top of the base every kit inherits. Shared_Dirty is the largest
    part of that base: a kit is forked from the parent, so everything the parent
    wrote before forking, above all the preloaded dictionaries, starts out as
    dirty pages shared with the parent and with every other kit, and only turns
    private for the pages a kit goes on to write. Shared_Clean is mostly the
    mapped engine libraries."""
    out: dict[str, int] = {}
    status_keys = {"VmRSS": "rss_kb", "VmHWM": "hwm_kb", "RssAnon": "rss_anon_kb",
                   "RssFile": "rss_file_kb", "RssShmem": "rss_shmem_kb"}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key = line.split(":", 1)[0]
            if key in status_keys:
                out[status_keys[key]] = int(line.split()[1])
    except (OSError, ValueError):
        return {}
    if "rss_kb" not in out:
        return {}  # a kernel thread, or a process that is exiting
    wanted = {"Pss": "pss_kb", "Private_Dirty": "private_dirty_kb",
              "Private_Clean": "private_clean_kb", "Shared_Dirty": "shared_dirty_kb",
              "Shared_Clean": "shared_clean_kb"}
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            key = line.split(":", 1)[0]
            if key in wanted:
                out[wanted[key]] = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return out


_SMAPS_HEADER = re.compile(r"^[0-9a-f]+-[0-9a-f]+ \S+ \S+ \S+ \S+\s*(.*)$")


def read_proc_smaps(pid: str) -> dict[str, int]:
    """Resident memory of one pid split by what backs each mapping (kB), or {}.

    A document kit maps the engine libraries, which are the same files every
    other kit maps, and it inherits the memory the parent had written before the
    fork. Neither is the document's own cost, and a single resident total hides
    both, so the mappings are read one by one from /proc/<pid>/smaps and sorted
    into the libraries, other files, and anonymous memory. The anonymous part is
    reported twice over: the whole of it, and the share still shared with the
    parent, which is what the fork handed over rather than what the document
    went on to write."""
    out = {"lib_rss_kb": 0, "other_rss_kb": 0, "anon_rss_kb": 0,
           "anon_shared_dirty_kb": 0}
    where = None
    try:
        with open(f"/proc/{pid}/smaps", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                header = _SMAPS_HEADER.match(line)
                if header:
                    path = header.group(1).strip()
                    if path.endswith(".so") or ".so." in path:
                        where = "lib"
                    elif not path or path.startswith("["):
                        where = "anon"
                    else:
                        where = "other"
                    continue
                if where is None:
                    continue
                key, _, rest = line.partition(":")
                if key == "Rss":
                    out[f"{where}_rss_kb"] += int(rest.split()[0])
                elif key == "Shared_Dirty" and where == "anon":
                    out["anon_shared_dirty_kb"] += int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return {}
    return out


def group_mem(pids: list[str]) -> dict[str, int]:
    """Sum the memory figures from read_proc_mem across a set of pids (kB).
    The smaps figures are included only when at least one pid exposed them.

    Only Pss and the private figures add up across processes. A page shared by
    several kits is counted once per kit in Shared_Dirty and Shared_Clean, so
    the sum of those over a group tells how much base each member carries, not
    how much memory the group holds."""
    agg: dict[str, int] = {"rss_kb": 0, "hwm_kb": 0}
    status_keys = ("rss_anon_kb", "rss_file_kb", "rss_shmem_kb")
    smaps_keys = ("pss_kb", "private_dirty_kb", "private_clean_kb",
                  "shared_dirty_kb", "shared_clean_kb")
    split_keys = ("lib_rss_kb", "other_rss_kb", "anon_rss_kb",
                  "anon_shared_dirty_kb")
    smaps: dict[str, int] = {}
    for pid in pids:
        m = read_proc_mem(pid)
        if not m:
            continue
        agg["rss_kb"] += m.get("rss_kb", 0)
        agg["hwm_kb"] += m.get("hwm_kb", 0)
        for key in status_keys:
            if key in m:
                agg[key] = agg.get(key, 0) + m[key]
        for key in smaps_keys:
            if key in m:
                smaps[key] = smaps.get(key, 0) + m[key]
        for key, value in read_proc_smaps(pid).items():
            if key in split_keys:
                smaps[key] = smaps.get(key, 0) + value
    agg.update(smaps)
    return agg


def browser_pids(proc: Path = Path("/proc")) -> dict[str, list[str]]:
    """The Chromium processes that k6 runs in its container, by kind:
    "renderer" (the pages, frames of the same site share one), "gpu" (the
    compositor, which rasterises in software when headless) and "other" (the
    browser process, the zygote and the utility processes).

    A Chromium process is one whose program is named chrom* or headless_shell and
    which runs in a docker cgroup. Its kind is its --type= argument."""
    out: dict[str, list[str]] = {"renderer": [], "gpu": [], "other": []}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            cgroup = (d / "cgroup").read_text()
        except OSError:
            continue
        program = argv[0].decode(errors="replace").rsplit("/", 1)[-1]
        if not (program.startswith("chrom") or program == "headless_shell"):
            continue
        if "docker" not in cgroup:
            continue
        kind = "other"
        for arg in argv[1:]:
            if arg == b"--type=renderer":
                kind = "renderer"
            elif arg == b"--type=gpu-process":
                kind = "gpu"
        out[kind].append(d.name)
    return out


class MemorySampler:
    """Samples resident memory of named process groups over time.

    Construct with a mapping of label to pid list, or to a function that returns
    the pid list at each sample for a group whose processes come and go. Call
    sample() repeatedly
    while the workload runs, then summary() for the per-group peak and mean.
    The kernel high-water mark (VmHWM) is tracked at each sample rather than
    read once at the end, because the kit is unloaded soon after the workload
    finishes and would be gone by then."""

    def __init__(self, groups: dict[str, list[str]]):
        self._groups = groups
        self._series: dict[str, list[dict]] = {label: [] for label in groups}
        self._hwm_kb: dict[str, int] = {label: 0 for label in groups}
        self._t0 = time.monotonic()

    def sample(self) -> None:
        t = round(time.monotonic() - self._t0, 1)
        for label, pids in self._groups.items():
            if callable(pids):
                pids = pids()
            m = group_mem(pids)
            if not (m.get("rss_kb") or m.get("pss_kb")):
                continue
            self._hwm_kb[label] = max(self._hwm_kb[label], m.get("hwm_kb", 0))
            # How many processes were in the group, because the proportional
            # figures divide shared pages by the number of sharers and mean
            # nothing without it.
            rec = {"t_s": t, "rss_mb": round(m["rss_kb"] / 1024, 1),
                   "pids": len(pids)}
            for key, field in (("pss_kb", "pss_mb"),
                               ("rss_anon_kb", "rss_anon_mb"),
                               ("rss_shmem_kb", "rss_shmem_mb"),
                               ("lib_rss_kb", "lib_rss_mb"),
                               ("other_rss_kb", "other_rss_mb"),
                               ("anon_rss_kb", "anon_rss_mb"),
                               ("anon_shared_dirty_kb", "anon_shared_dirty_mb"),
                               ("private_dirty_kb", "private_dirty_mb"),
                               ("private_clean_kb", "private_clean_mb"),
                               ("shared_dirty_kb", "shared_dirty_mb"),
                               ("shared_clean_kb", "shared_clean_mb")):
                if key in m:
                    rec[field] = round(m[key] / 1024, 1)
            self._series[label].append(rec)

    def summary(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for label, series in self._series.items():
            if not series:
                continue
            rss = [s["rss_mb"] for s in series]
            pss = [s["pss_mb"] for s in series if "pss_mb" in s]
            entry: dict = {
                "peak_rss_mb": max(rss),
                "mean_rss_mb": round(sum(rss) / len(rss), 1),
                "samples": series,
            }
            if pss:
                entry["peak_pss_mb"] = max(pss)
                entry["mean_pss_mb"] = round(sum(pss) / len(pss), 1)
            # The private figure is what this group costs on its own and the shared
            # ones are the base it inherited, so both peaks are worth keeping.
            for field in ("rss_anon_mb", "rss_shmem_mb",
                          "private_dirty_mb", "private_clean_mb",
                          "shared_dirty_mb", "shared_clean_mb",
                          "lib_rss_mb", "other_rss_mb", "anon_rss_mb",
                          "anon_shared_dirty_mb"):
                values = [s[field] for s in series if field in s]
                if values:
                    entry[f"peak_{field}"] = max(values)
                    entry[f"mean_{field}"] = round(sum(values) / len(values), 1)
            if self._hwm_kb[label]:
                entry["peak_rss_hwm_mb"] = round(self._hwm_kb[label] / 1024, 1)
            counts = [s["pids"] for s in series if "pids" in s]
            if counts:
                entry["max_pids"] = max(counts)
            out[label] = entry
        return out


def proc_cgroup(pid: str) -> str | None:
    """cgroup v2 path (relative to /sys/fs/cgroup) of pid, or None."""
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):  # cgroup v2 unified line
                return line[3:].lstrip("/")
    except Exception:
        pass
    return None


CGROUP_ROOT = Path("/sys/fs/cgroup")


def make_capture_cgroup(name: str, root: Path = CGROUP_ROOT) -> Path | None:
    """Create <own cgroup>/<name>/{wsd,kit} and return <own cgroup>/<name>.

    None when this process may not create cgroups under its own: a service
    without Delegate=yes, or no cgroup v2 at root."""
    own = proc_cgroup("self")
    if own is None or not (root / "cgroup.controllers").exists():
        return None
    base = root / own / name
    created: list[Path] = []
    try:
        for d in (base, base / "wsd", base / "kit"):
            d.mkdir()
            created.append(d)
    except OSError:
        for d in reversed(created):
            d.rmdir()
        return None
    return base


def _is_kit_comm(comm: str) -> bool:
    """forkit (coolforkit-* before it renames itself) and the processes it
    forks: kit_spare_*, kitbroker_*, kitbgsv_*."""
    return comm == "forkit" or comm.startswith("coolforkit") or comm.startswith("kit")


def move_kit_processes(base: Path, timeout_s: float = 15.0,
                       comm_of=None) -> tuple[list[str], bool]:
    """Move forkit and the kits it already forked from base/wsd to base/kit.

    A kit forked while forkit is still in wsd starts in wsd, so scan until
    forkit was seen and three passes in a row moved nothing.
    Returns (moved pids, forkit seen)."""
    if comm_of is None:
        def comm_of(pid: str) -> str:
            return Path(f"/proc/{pid}/comm").read_text().strip()
    moved: list[str] = []
    forkit_seen = False
    quiet = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = False
        for pid in (base / "wsd" / "cgroup.procs").read_text().split():
            try:
                comm = comm_of(pid)
                if not _is_kit_comm(comm):
                    continue
                (base / "kit" / "cgroup.procs").write_text(pid)
            except OSError:
                continue  # the process exited meanwhile
            moved.append(pid)
            found = True
            if comm == "forkit" or comm.startswith("coolforkit"):
                forkit_seen = True
        quiet = 0 if (found or not forkit_seen) else quiet + 1
        if quiet >= 3:
            break
        time.sleep(0.2)
    return moved, forkit_seen


def remove_capture_cgroup(base: Path, timeout_s: float = 10.0) -> bool:
    """Kill what is left under base (cgroup.kill), then remove base/kit,
    base/wsd and base. False if a directory is still there at the timeout."""
    kill = base / "cgroup.kill"
    if kill.exists():
        try:
            kill.write_text("1")
        except OSError:
            pass
    deadline = time.monotonic() + timeout_s
    for d in (base / "kit", base / "wsd", base):
        while d.exists():
            try:
                d.rmdir()
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.2)
    return True


def collect_workload(browser_log: Path) -> dict:
    """Per-VU browser timings and network byte counts scraped from the k6 or
    puppeteer log, as lists ready for statistics or JSON. dom_ready_ms and
    doc_loaded_ms are measured from each VU's own start time."""
    timings = scrape_timings_per_vu(browser_log)
    starts = scrape_start_times_per_vu(browser_log)
    bytes_by_vu = scrape_network_bytes_per_vu(browser_log)
    dom_ms, loaded_ms, delta_ms = [], [], []
    for vu, t in sorted(timings.items()):
        dom = t.get("DOMReadyTime")
        loaded = t.get("DocumentLoadedTime")
        delta = t.get("DOMToDocumentLoadedMs")
        anchor = starts.get(vu)
        if dom is not None and anchor is not None:
            dom_ms.append(dom - anchor)
        if loaded is not None and anchor is not None:
            loaded_ms.append(loaded - anchor)
        if delta is not None:
            delta_ms.append(delta)
    sent, recv = [], []
    if bytes_by_vu:
        sent = [b["sent"] for _, b in sorted(bytes_by_vu.items())]
        recv = [b["received"] for _, b in sorted(bytes_by_vu.items())]
    return {
        "dom_ready_ms": dom_ms,
        "doc_loaded_ms": loaded_ms,
        "dom_to_loaded_ms": delta_ms,
        "ws_sent_bytes": sent,
        "ws_received_bytes": recv,
    }


def print_workload(workload: dict) -> None:
    if workload["dom_ready_ms"]:
        print(f"  dom ready    : {fmt_int_stat(workload['dom_ready_ms'], 'ms')}")
    if workload["doc_loaded_ms"]:
        print(f"  doc loaded   : {fmt_int_stat(workload['doc_loaded_ms'], 'ms')}")
    if workload["dom_to_loaded_ms"]:
        print(f"  dom->loaded  : {fmt_int_stat(workload['dom_to_loaded_ms'], 'ms')}")
    if workload["ws_sent_bytes"]:
        print(f"  ws sent      : {fmt_bytes_stat(workload['ws_sent_bytes'])}")
    if workload["ws_received_bytes"]:
        print(f"  ws received  : {fmt_bytes_stat(workload['ws_received_bytes'])}")


def collect_perf(out_dir: Path) -> dict:
    """Per-process instructions/cycles/IPC/CPU-time from the
    perf-stat-<group>.csv files, plus an exact coolwsd+kit total (the two
    groups are disjoint pid sets, so a straight sum is correct)."""
    totals = {"instructions": 0.0, "cycles": 0.0, "task-clock": 0.0}
    perf_by_label: dict[str, dict] = {}
    for label in ("coolwsd", "kit"):
        st = parse_perf_stat(out_dir / f"perf-stat-{label}.csv")
        if not st:
            continue
        ins = st.get("instructions")
        cyc = st.get("cycles")
        tcl = st.get("task-clock")
        for k in totals:
            if st.get(k) is not None:
                totals[k] += st[k]
        entry: dict = {}
        if ins is not None:
            entry["instructions"] = int(ins)
        if cyc is not None:
            entry["cycles"] = int(cyc)
        if ins is not None and cyc:
            entry["ipc"] = round(ins / cyc, 4)
        if tcl is not None:
            # task-clock is reported in milliseconds
            entry["cpu_time_s"] = round(tcl / 1000, 3)
        perf_by_label[label] = entry
    if perf_by_label:
        ti, tc, tt = totals["instructions"], totals["cycles"], totals["task-clock"]
        perf_by_label["total"] = {
            "instructions": int(ti),
            "cycles": int(tc),
            "ipc": round(ti / tc, 4) if tc else None,
            "cpu_time_s": round(tt / 1000, 3),
        }
    return perf_by_label


def print_perf(perf_by_label: dict) -> None:
    for label in ("coolwsd", "kit", "total"):
        entry = perf_by_label.get(label)
        if not entry:
            continue
        print(f"  [{'coolwsd+kit total' if label == 'total' else label}]")
        if "instructions" in entry:
            print(f"    instructions : {fmt_count(entry['instructions'])}  "
                  f"({entry['instructions']:,})")
        if "cycles" in entry:
            print(f"    cycles       : {fmt_count(entry['cycles'])}")
        if entry.get("ipc") is not None:
            print(f"    IPC          : {entry['ipc']:.2f}")
        if "cpu_time_s" in entry:
            print(f"    CPU time     : {entry['cpu_time_s']:.2f} s")


def print_memory(mem_by_label: dict) -> None:
    for label in ("coolwsd", "kit"):
        entry = mem_by_label.get(label)
        if not entry:
            continue
        kind = "PSS" if "peak_pss_mb" in entry else "RSS"
        peak = entry.get("peak_pss_mb", entry["peak_rss_mb"])
        hwm = entry.get("peak_rss_hwm_mb", entry["peak_rss_mb"])
        print(f"  [{label}] peak {kind}: {peak:.0f} MB  (RSS hwm {hwm:.0f} MB)")


def flamegraph_sample_count(out_dir: Path) -> int | None:
    """The sample count perf logged for the flamegraph capture, or None."""
    perf_log = out_dir / "perf.log"
    if not perf_log.exists():
        return None
    m = re.search(r"\(([0-9]+) samples\)", perf_log.read_text(errors="replace"))
    return int(m.group(1)) if m else None
