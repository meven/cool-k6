"""How much of the machine a run had for itself.

A probe is a fixed amount of work: two copies of a 64 MB buffer, which is larger than a CPU's last
level cache, so they depend on memory bandwidth, and a SHA-256 over 4 MB, which depends on the CPU.
On an idle machine it takes the same time every time. A Sampler runs it in a thread at an interval
while the suite runs, and reads the CPU steal time of the whole window from /proc/stat. Steal is the
time the hypervisor ran something else while this VM had work for that CPU.

A run's probe times are compared with the probe times of the runs published before it. A run where
the probe was slower than usual, or where the steal time was high, measured a busy machine.
"""
import hashlib
import json
import statistics
import threading
import time
from pathlib import Path

PROBE_BUFFER = 64 << 20
PROBE_HASHED = 4 << 20


def _cpu_jiffies() -> tuple[int, int] | None:
    """(steal, total) jiffies of all CPUs since boot, or None without /proc/stat."""
    try:
        with open("/proc/stat") as f:
            fields = [int(v) for v in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    # user nice system idle iowait irq softirq steal guest guest_nice; guest time is already
    # counted in user and nice.
    steal = fields[7] if len(fields) > 7 else 0
    return steal, sum(fields[:8])


class Sampler(threading.Thread):
    def __init__(self, interval: float = 20.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.probes_ms: list[float] = []
        self._stop = threading.Event()
        self._buffer = bytearray(PROBE_BUFFER)
        self._hashed = bytes(PROBE_HASHED)
        self._start_jiffies = None
        self._end_jiffies = None

    def probe(self) -> float:
        started = time.perf_counter()
        for _ in range(2):
            bytes(self._buffer)
        hashlib.sha256(self._hashed).digest()
        return (time.perf_counter() - started) * 1000

    def run(self) -> None:
        self._start_jiffies = _cpu_jiffies()
        self.probe()  # the first one pays for the page faults of the copies
        while not self._stop.is_set():
            self.probes_ms.append(self.probe())
            self._stop.wait(self.interval)

    def stop(self) -> dict:
        """Stop sampling and return {probe_ms: {med, p90, max, n}, steal_pct}."""
        self._stop.set()
        self.join(timeout=10)
        self._end_jiffies = _cpu_jiffies()
        out: dict = {}
        if self.probes_ms:
            ordered = sorted(self.probes_ms)
            out["probe_ms"] = {
                "med": round(statistics.median(ordered), 1),
                "p90": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 1),
                "max": round(ordered[-1], 1),
                "n": len(ordered),
            }
        if self._start_jiffies and self._end_jiffies:
            steal = self._end_jiffies[0] - self._start_jiffies[0]
            total = self._end_jiffies[1] - self._start_jiffies[1]
            if total > 0:
                out["steal_pct"] = round(100 * steal / total, 2)
        return out


def baseline_ms(results: Path, runs: int = 20) -> float | None:
    """Median probe time of the newest published runs that were not busy, or None when fewer than
    3 of them have one."""
    meds = []
    for run in sorted((p for p in results.iterdir() if p.is_dir()), reverse=True):
        try:
            m = json.loads((run / "machine.json").read_text())
        except (OSError, ValueError):
            continue
        med = (m.get("probe_ms") or {}).get("med")
        if med and not m.get("busy"):
            meds.append(med)
        if len(meds) == runs:
            break
    return statistics.median(meds) if len(meds) >= 3 else None


def judge(machine: dict, baseline: float | None, probe_max: float,
          steal_max: float) -> list[str]:
    """The reasons why a run measured a busy machine, empty for a quiet one. Adds the ratio of
    the probe to the baseline to @p machine."""
    reasons = []
    p90 = (machine.get("probe_ms") or {}).get("p90")
    if p90 and baseline:
        machine["probe_ratio"] = round(p90 / baseline, 2)
        if p90 > baseline * probe_max:
            reasons.append(f"the probe took {p90:.0f} ms at p90, {p90 / baseline:.2f} times "
                           f"the usual {baseline:.0f} ms")
    steal = machine.get("steal_pct")
    if steal is not None and steal > steal_max:
        reasons.append(f"the hypervisor took {steal:.1f}% of the CPU time (steal)")
    return reasons
