#!/usr/bin/env python3
"""
timeline-sampler.py: sample the host and the COOL processes every INTERVAL seconds during a run.

Writes into OUT_DIR, until it is sent SIGTERM:
  - timeline.csv: one row per sample, with the host's available memory, swap and major-fault
    traffic, CPU busy and iowait shares, and the CPU used by coolwsd, the forkit, all document
    kits, all background saves, the WOPI host (node), k6 and Chromium. With --cgroup, also the
    count of processes that the kernel has killed for lack of memory in that cgroup so far.
  - kit-cpu.csv: one row per sample and document kit, with the CPU the kit used since the last
    sample.
  - events.csv: when a kit or a background save appears and ends, with its pid and name. A kit is
    named kitbroker_<docbroker id> and its saves kitbgsv_<docbroker id>_<n>, so the two join on
    the docbroker id.

CPU columns are in CPUs (1.0 is one core busy for the whole interval). Times are Unix epoch
milliseconds, the clock k6 uses for Date.now().

    ./timeline-sampler.py OUT_DIR [--interval 0.5] [--cgroup /sys/fs/cgroup/...]
"""
import argparse
import os
import signal
import sys
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1


def read_proc_stat():
    """Busy and iowait ticks summed over all CPUs, and the total."""
    with open("/proc/stat") as f:
        fields = [int(x) for x in f.readline().split()[1:]]
    user, nice, system, idle, iowait, irq, softirq, steal = (fields + [0] * 8)[:8]
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return user + nice + system + irq + softirq + steal, iowait, total


def read_meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            if key in ("MemAvailable", "SwapFree"):
                out[key] = int(value.split()[0]) // 1024
    return out


def read_vmstat():
    out = {}
    with open("/proc/vmstat") as f:
        for line in f:
            key, value = line.split()
            if key in ("pswpin", "pswpout", "pgmajfault"):
                out[key] = int(value)
    return out


def read_oom_kills(cgroup):
    """The oom_kill count of the cgroup's memory.events, or an empty field without one."""
    if not cgroup:
        return ""
    try:
        with open(os.path.join(cgroup, "memory.events")) as f:
            for line in f:
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return value.strip()
    except OSError:
        pass
    return ""


def kind_of(comm, cmdline):
    if comm.startswith("kitbroker_"):
        return "kit"
    if comm.startswith("kitbgsv_"):
        return "bgsave"
    if comm == "coolwsd":
        return "coolwsd"
    if comm in ("forkit", "coolforkit-ns", "coolforkit-caps"):
        return "forkit"
    if comm == "k6":
        return "k6"
    if "chrom" in comm:
        return "chromium"
    if comm in ("node", "npm") and "server" in cmdline:
        return "wopi"
    return None


def scan_processes():
    """pid -> (kind, comm, cpu ticks) for the processes this run cares about."""
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        cmdline = ""
        if comm in ("node", "npm"):
            try:
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    cmdline = f.read().decode(errors="replace")
            except OSError:
                pass
        kind = kind_of(comm, cmdline)
        if kind is None:
            continue
        rest = stat[stat.rindex(")") + 2:].split()
        # utime and stime are fields 14 and 15 of the stat line, 12 and 13 after the comm.
        procs[int(name)] = (kind, comm, int(rest[11]) + int(rest[12]))
    return procs


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--cgroup", default="", help="the cgroup v2 directory of the server")
    args = parser.parse_args()

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    kinds = ("coolwsd", "forkit", "kit", "bgsave", "wopi", "k6", "chromium")
    timeline = open(os.path.join(args.out_dir, "timeline.csv"), "w", buffering=1)
    kit_cpu = open(os.path.join(args.out_dir, "kit-cpu.csv"), "w", buffering=1)
    events = open(os.path.join(args.out_dir, "events.csv"), "w", buffering=1)
    timeline.write("t_ms,mem_available_mb,swap_free_mb,swap_in_pages,swap_out_pages,major_faults,"
                   "cpu_busy,cpu_iowait," + ",".join(f"{k}_cpu" for k in kinds) + ",bgsaves,oom_kills\n")
    kit_cpu.write("t_ms,pid,name,cpu\n")
    events.write("t_ms,event,pid,name\n")

    prev_stat = read_proc_stat()
    prev_vm = read_vmstat()
    prev_procs = scan_processes()
    prev_t = time.time()
    for pid, (kind, comm, _) in prev_procs.items():
        if kind in ("kit", "bgsave"):
            events.write(f"{int(prev_t * 1000)},present,{pid},{comm}\n")

    while running:
        time.sleep(args.interval)
        t = time.time()
        dt = t - prev_t
        t_ms = int(t * 1000)
        busy, iowait, total = read_proc_stat()
        d_total = max(1, total - prev_stat[2])
        vm = read_vmstat()
        mem = read_meminfo()
        procs = scan_processes()

        cpu = dict.fromkeys(kinds, 0.0)
        for pid, (kind, comm, ticks) in procs.items():
            old = prev_procs.get(pid)
            if old is None:
                if kind in ("kit", "bgsave"):
                    events.write(f"{t_ms},start,{pid},{comm}\n")
                # The first interval of a new process counts from its start.
                used = ticks
            else:
                used = ticks - old[2]
                if kind in ("kit", "bgsave") and old[1] != comm:
                    events.write(f"{t_ms},rename,{pid},{comm}\n")
            used_cpus = used / CLK_TCK / dt
            cpu[kind] += used_cpus
            if kind == "kit":
                kit_cpu.write(f"{t_ms},{pid},{comm},{used_cpus:.3f}\n")
        for pid, (kind, comm, _) in prev_procs.items():
            if pid not in procs and kind in ("kit", "bgsave"):
                events.write(f"{t_ms},end,{pid},{comm}\n")

        bgsaves = sum(1 for kind, _, _ in procs.values() if kind == "bgsave")
        timeline.write(
            f"{t_ms},{mem.get('MemAvailable', 0)},{mem.get('SwapFree', 0)},"
            f"{vm['pswpin'] - prev_vm['pswpin']},{vm['pswpout'] - prev_vm['pswpout']},"
            f"{vm['pgmajfault'] - prev_vm['pgmajfault']},"
            f"{(busy - prev_stat[0]) / d_total * NCPU:.2f},{(iowait - prev_stat[1]) / d_total * NCPU:.2f},"
            + ",".join(f"{cpu[k]:.2f}" for k in kinds) + f",{bgsaves},{read_oom_kills(args.cgroup)}\n")

        prev_stat, prev_vm, prev_procs, prev_t = (busy, iowait, total), vm, procs, t

    for f in (timeline, kit_cpu, events):
        f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
