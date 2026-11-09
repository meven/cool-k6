#!/usr/bin/env python3
"""
k6-flamegraph: drive a coolwsd workload with a k6 test and capture a perf
flamegraph of the kit activity. Python port of the bash script.

CLI flags override environment variables override built-in defaults. Run
with --help for the full list.

Outputs (under --out-dir): coolwsd.log, wopi.log, k6.log, perf.log,
perf.data, flamegraph.svg, and per-process instruction
counts in perf-stat-coolwsd.csv / perf-stat-kit.csv (coolwsd+forkit vs the
document kit; the run summary prints both plus a combined total). A
machine-readable metrics.json collects the per-process instructions, cycles,
IPC and CPU time, plus a resident-memory time series and peak (RSS and, when
readable, PSS) for coolwsd and the kit, for plotting across runs.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from pylib.perfutils import (
    MemorySampler,
    browser_pids,
    descendant_pids,
    collect_perf,
    collect_workload,
    document_kit_pids,
    flamegraph_sample_count,
    CGROUP_ROOT,
    make_capture_cgroup,
    move_kit_processes,
    pgrep,
    print_memory,
    print_perf,
    print_workload,
    remove_capture_cgroup,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def need(cmd: str, hint: str) -> None:
    if shutil.which(cmd) is None:
        die(f"missing required command: {cmd}\n{hint}")


def env_default(name: str, default: str) -> str:
    """Read NAME from environment, falling back to default. Empty = default."""
    val = os.environ.get(name)
    return val if val else default


def default_cool_cmd() -> str:
    """The command that starts coolwsd when none was given.

    A measurement must not run make: `make run-one` depends on `all`, so make
    walks the tree first and relinks whatever is out of date, which both delays
    the start past the point where this script looks for a listening port and
    replaces the build being measured. cool-use-build prints the direct coolwsd
    line read out of the tree's own makefile, so ask it, and fall back to
    `make run-one` only when it cannot answer (no COOLWSD_DIR, or the script is
    missing)."""
    tool = SCRIPT_DIR / "cool-use-build"
    if tool.is_file():
        try:
            out = subprocess.run([str(tool), "run-cmd"], cwd=str(SCRIPT_DIR),
                                 capture_output=True, text=True, timeout=30)
            cmd = out.stdout.strip()
            if out.returncode == 0 and cmd:
                return cmd
        except (OSError, subprocess.SubprocessError):
            pass
    return "make run-one"


# The build-id cache of a capture, under its output directory.
BUILDID_DIR = "buildid"

# Every frame of coolwsd carries this annotation, and every frame of forkit the other one.
# flamegraph.pl strips both from the name it shows and searches, so the frames of each
# process can have colours of their own.
WSD_TAG = "_[i]"
FORKIT_TAG = "_[w]"
FLAME_LEGEND = "aqua: coolwsd, purple: forkit, red to yellow: document kit"
# A frame for each process side under the threads, so that each side is one block with
# its share of the samples. flamegraph.pl sorts by name, which puts coolwsd first.
WSD_ROOT = "[coolwsd and forkit]"
KIT_ROOT = "[kit]"


def process_stack(frames: list[str], wsd: bool, forkit: bool = False) -> str:
    """A collapsed stack, thread first, under the frame of its process side, with every
    frame tagged when it is coolwsd or forkit. The frame of the side keeps the coolwsd tag
    for both, so that coolwsd and forkit stay one block."""
    # a background save thread is named after its document, and a kit and a spare kit are
    # numbered like the document thread of coolwsd, so that the threads of a capacity run
    # with many documents merge
    frames = [re.sub(r"^(kitbgsv|kitbroker|kit_spare|docbroker).*", r"\1", frames[0]), *frames[1:]]
    if wsd:
        tag = FORKIT_TAG if forkit else WSD_TAG
        return ";".join([WSD_ROOT + WSD_TAG, *(f + tag for f in frames)])
    return ";".join([KIT_ROOT, *frames])


def tag_by_process(lines, wsd_pids: set[str], forkit_pids: set[str] = frozenset()):
    """Collapsed stacks from stackcollapse-perf.pl --pid, put under the process side
    they were sampled in, and tagged as forkit when their pid is one of forkit_pids. The
    pid is dropped from the thread frame again, so the threads of all kits merge as
    before."""
    for line in lines:
        stack, _, count = line.rstrip("\n").rpartition(" ")
        if not stack:
            continue
        frames = stack.split(";")
        comm, _, pid = frames[0].rpartition("-")
        if not comm or not pid.isdigit():
            comm, pid = frames[0], ""
        yield process_stack([comm, *frames[1:]], pid in wsd_pids or pid in forkit_pids,
                            pid in forkit_pids) + f" {count}\n"


def stack_shares(stacks: list[str]) -> tuple[float, float]:
    """(kit %, unresolved %) of the samples in tagged collapsed stacks: the share under the
    kit frame, and the share whose innermost frame perf could not name."""
    total = kit = unknown = 0
    for line in stacks:
        stack, _, count = line.rstrip("\n").rpartition(" ")
        if not stack or not count.isdigit():
            continue
        n = int(count)
        frames = stack.split(";")
        total += n
        if frames[0] == KIT_ROOT:
            kit += n
        if frames[-1].removesuffix(WSD_TAG).removesuffix(FORKIT_TAG) == "[unknown]":
            unknown += n
    if not total:
        return 0.0, 0.0
    return 100.0 * kit / total, 100.0 * unknown / total


def render_flamegraph(stacks: list[str], svg_path: Path) -> None:
    """Draw tagged collapsed stacks. The kits keep the default hot palette, and the
    coolwsd frames take an aqua and the forkit frames a purple from palette.map, which
    flamegraph.pl --cp reads by the full frame name, annotation included."""
    tagged = {f for line in stacks if WSD_TAG in line
              for f in line.rpartition(" ")[0].split(";")}
    with tempfile.TemporaryDirectory() as tmp:
        with open(Path(tmp) / "palette.map", "w") as pal:
            for name in sorted(tagged):
                v = (zlib.crc32(name.encode()) % 1000) / 1000
                if name.endswith(FORKIT_TAG):
                    rgb = (150 + int(50 * v), 110 + int(50 * v), 210 + int(40 * v))
                elif name.endswith(WSD_TAG):
                    rgb = (50 + int(60 * v), 165 + int(55 * v), 165 + int(55 * v))
                else:
                    continue
                pal.write(f"{name}->rgb({rgb[0]},{rgb[1]},{rgb[2]})\n")
        with svg_path.open("w") as svg:
            subprocess.run(["flamegraph.pl", "--cp", "--subtitle", FLAME_LEGEND],
                           input="".join(stacks), stdout=svg, text=True, cwd=tmp)


def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val == "1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="k6-flamegraph",
        description=(
            "Drive a coolwsd workload with a k6 test and capture a perf "
            "flamegraph of kit activity. Precedence: CLI flags > env vars > "
            "built-in defaults."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "test_script",
        nargs="?",
        default=None,
        help=(
            "k6 test script, relative to this script's directory "
            "(default dist/cool-load-document-test.js) (env TEST_SCRIPT)"
        ),
    )
    p.add_argument(
        "--coolwsd-dir",
        default=os.environ.get("COOLWSD_DIR") or None,
        help="path to the online checkout (default: $COOLWSD_DIR)",
    )
    p.add_argument(
        "--coolwsd-url",
        default=env_default("COOLWSD_URL", "https://localhost:9980/"),
        help="URL coolwsd serves at (env COOLWSD_URL)",
    )
    p.add_argument(
        "--host-ip",
        default=os.environ.get("HOST_IP") or None,
        help="host IP reachable from the k6 docker container "
             "(default: auto-detect via `ip route`) (env HOST_IP)",
    )
    p.add_argument(
        "--wopi-port",
        default=env_default("WOPI_PORT", "3000"),
        help="WOPI host port (env WOPI_PORT)",
    )
    p.add_argument(
        "--wopi-scheme",
        choices=("http", "https"),
        default=env_default("WOPI_SCHEME", "https"),
        help="WOPI host scheme; must match coolwsd's "
             "(the demo html rejects http parent + https iframe) (env WOPI_SCHEME)",
    )
    p.add_argument(
        "--wopi-key",
        default=os.environ.get("WOPI_SSL_KEY") or None,
        help="TLS key for the WOPI host (default $COOLWSD_DIR/etc/key.pem) "
             "(env WOPI_SSL_KEY)",
    )
    p.add_argument(
        "--wopi-crt",
        default=os.environ.get("WOPI_SSL_CRT") or None,
        help="TLS cert (default $COOLWSD_DIR/etc/cert.pem) (env WOPI_SSL_CRT)",
    )
    p.add_argument(
        "--cool-cmd",
        default=env_default("COOL_CMD", default_cool_cmd()),
        help="command that starts coolwsd, run in the coolwsd directory "
             "(default: the direct line cool-use-build run-cmd prints, or "
             "'make run-one' when it cannot answer). Pass a direct ./coolwsd "
             "command line to "
             "profile a restored build snapshot: `make run-one` depends on `all`, "
             "so make would relink the old binary from the tree's current objects "
             "and you would profile the wrong commit. `cool-use-build run-cmd` "
             "prints a suitable command (env COOL_CMD)",
    )
    p.add_argument(
        "--out-dir",
        default=env_default("OUT_DIR", "profile-out"),
        help="output directory (env OUT_DIR)",
    )
    # A 10 s test at 999 Hz gives a flamegraph of about 1750 samples. An odd rate keeps the
    # sampling out of step with the timers of the kernel and of the processes.
    p.add_argument(
        "--perf-freq",
        type=int,
        default=int(env_default("PERF_FREQ", "999")),
        help="perf sampling frequency Hz (env PERF_FREQ)",
    )
    p.add_argument(
        "--perf-event",
        default=env_default("PERF_EVENT", "cycles"),
        help="perf sampling event (env PERF_EVENT). On a hybrid P/E-core CPU "
             "the default 'cycles' can fail to open for perf record; try "
             "'cpu_core/cycles/' or 'cpu_atom/cycles/'",
    )
    p.add_argument(
        "--cgroup",
        action=argparse.BooleanOptionalAction,
        default=env_bool("COOL_K6_CGROUP", False),
        help="launch coolwsd in a cgroup of its own, created under this "
             "process's cgroup, with forkit and its kits moved to a child "
             "cgroup. perf record and perf stat then count by cgroup "
             "(perf -a -G) from before the document opens, so the counts do "
             "not depend on when the kit was found. Needs a cgroup this "
             "process may create children in (a service needs Delegate=yes) "
             "and perf_event_paranoid <= 0. Falls back to --pid targeting "
             "if unavailable. (env COOL_K6_CGROUP=1/0)",
    )
    p.add_argument(
        "--new-instance",
        action=argparse.BooleanOptionalAction,
        default=env_bool("COOL_K6_NEW_INSTANCE", False),
        help="always launch a fresh single-kit coolwsd (make run-one, which "
             "uses --find-free-port) instead of attaching to one already "
             "running, and use the port it binds. Lets a profiling instance "
             "coexist with a coolwsd already on the default port - each new "
             "instance takes the next free port. (env COOL_K6_NEW_INSTANCE=1/0)",
    )
    p.add_argument(
        "--stop-coolwsd",
        action=argparse.BooleanOptionalAction,
        default=env_bool("STOP_COOLWSD", True),
        help="when coolwsd was started by this script, stop it at "
             "the end of the run so the next invocation starts a "
             "fresh process with the env it inherits. Pass "
             "--no-stop-coolwsd to keep the started coolwsd alive "
             "for reuse across runs. Has no effect when coolwsd was "
             "already running before this invocation. "
             "(env STOP_COOLWSD=1/0)",
    )
    p.add_argument(
        "--defer-render",
        action="store_true",
        help="stop after the capture and leave the flamegraph to a later "
             "--render-only run, so that a caller can draw several captures "
             "at once after its tests",
    )
    p.add_argument(
        "--render-only",
        action="store_true",
        help="draw the flamegraph of the --defer-render capture in --out-dir",
    )
    return p.parse_args()


def detect_host_ip() -> str | None:
    try:
        out = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if "src" in parts:
            i = parts.index("src")
            if i + 1 < len(parts):
                return parts[i + 1]
    return None


# coolwsd and the WOPI host use self-signed certs in dev, so reachability
# checks skip certificate verification (the equivalent of curl -k).
_INSECURE_SSL = ssl.create_default_context()
_INSECURE_SSL.check_hostname = False
_INSECURE_SSL.verify_mode = ssl.CERT_NONE


def http_ok(url: str) -> bool:
    """True if url answers over HTTP(S), the way `curl -ks` exits 0. An HTTP
    error status still counts as reachable, since the server did answer."""
    try:
        urllib.request.urlopen(url, timeout=15, context=_INSECURE_SSL).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def wait_for(desc: str, max_seconds: int, check) -> None:
    for _ in range(max_seconds):
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(1)
    die(f"timed out waiting for {desc}")


def listening_ports() -> set:
    """TCP ports in LISTEN state. The net namespace is shared, so this sees
    every listener on the host regardless of who owns it."""
    try:
        out = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return set()
    ports = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            m = re.search(r":(\d+)$", cols[3])  # Local Address:Port
            if m:
                ports.add(int(m.group(1)))
    return ports


def discover_new_cool_port(before: set, timeout: int = 60) -> int | None:
    """Find the client port a freshly launched coolwsd bound. run-one uses
    --find-free-port, so with the default port already taken it lands on the
    next free one; diff the LISTEN sockets against `before` and pick the new
    port that answers hosting/discovery over https. None if none appears."""
    for _ in range(timeout):
        for p in sorted(listening_ports() - before):
            if http_ok(f"https://localhost:{p}/hosting/discovery"):
                return p
        time.sleep(1)
    return None


def sudo_needs_password() -> bool:
    """True if a sudo command would prompt for a password (no cached or
    passwordless credentials)."""
    try:
        return subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5,
        ).returncode != 0
    except Exception:
        return True


def _read_int(path: str, default: int) -> int:
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return default


def perf_can_bypass_paranoid() -> bool:
    """True if perf can record regardless of perf_event_paranoid: running as
    root, or the perf binary carries CAP_PERFMON / CAP_SYS_ADMIN. When true we
    skip lowering the kernel knob (and the sudo prompt that needs)."""
    if os.geteuid() == 0:
        return True
    perf = shutil.which("perf")
    if not perf:
        return False
    try:
        out = subprocess.run(["getcap", os.path.realpath(perf)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True).stdout.lower()
    except FileNotFoundError:
        return False
    return "cap_perfmon" in out or "cap_sys_admin" in out


def prepare_perf(system_wide: bool = False) -> tuple[list[str], list[str], str]:
    """Make perf usable for recording our own coolwsd processes, prompting for
    sudo only when a kernel knob actually has to change.

    Returns (sudo_perf, perf_mmap, mmap_desc):
      - sudo_perf : command prefix for perf (empty; recording our own
                    processes needs no elevation once paranoid <= 2).
      - perf_mmap : the ["-m", "<N>M"] mmap-ring option for `perf record` ([]
                    if the cap is too small for even 1 MB).
      - mmap_desc : human-readable mmap size, for log messages.
    """
    paranoid_path = "/proc/sys/kernel/perf_event_paranoid"
    mlock_path = "/proc/sys/kernel/perf_event_mlock_kb"

    # Per-process (--pid) recording of our own coolwsd works unprivileged at
    # perf_event_paranoid <= 2. System-wide / cgroup capture (-a, -G) uses
    # CPU-wide events, which need <= 0. If the knob is too high, lower it once
    # with sudo (prompting for the password); the recording then runs
    # unprivileged.
    if system_wide:
        max_paranoid, target = 0, 0
        why = "system-wide / cgroup capture"
    else:
        max_paranoid, target = 2, 1
        why = "profiling"
    paranoid = _read_int(paranoid_path, 4)
    if perf_can_bypass_paranoid():
        # perf has CAP_PERFMON (or we are root): no knob change, no sudo prompt.
        # This is the fool-proof setup - grant it once with:
        #   sudo setcap cap_perfmon,cap_sys_ptrace+ep "$(command -v perf)"
        print(f"perf can record without lowering perf_event_paranoid "
              f"(currently {paranoid}): CAP_PERFMON on perf, or running as root.")
    elif paranoid > max_paranoid:
        print(f"kernel.perf_event_paranoid={paranoid} is too restrictive for {why}; "
              f"lowering it to {target}.")
        if sudo_needs_password():
            print("You may be prompted for your sudo password. To avoid this, "
                  "grant perf CAP_PERFMON once: "
                  "sudo setcap cap_perfmon,cap_sys_ptrace+ep \"$(command -v perf)\"")
        subprocess.run(f"echo {target} | sudo tee {paranoid_path}", shell=True)
        paranoid = _read_int(paranoid_path, 4)
        if paranoid > max_paranoid:
            die(f"perf_event_paranoid is still {paranoid}; cannot record. Lower it "
                f"manually (echo {target} | sudo tee {paranoid_path}) or grant "
                f"perf CAP_PERFMON (setcap cap_perfmon,cap_sys_ptrace+ep perf).")
    # Recording at this paranoid level needs no per-command elevation.
    sudo_perf: list[str] = []

    # dwarf call-graph copies a large stack per sample, which overflows the
    # unprivileged perf ring buffer (perf_event_mlock_kb, 516 KB by default)
    # and drops almost every sample. Raise the cap if it is too small (prompts
    # for sudo), then size perf's mmap ring to fit under it.
    MLOCK_MIN_KB = 32768
    mlock_kb = _read_int(mlock_path, 0)
    if mlock_kb < MLOCK_MIN_KB:
        print(f"kernel.perf_event_mlock_kb={mlock_kb} is too small for dwarf "
              f"call-graph capture; raising it to {MLOCK_MIN_KB}.")
        if sudo_needs_password():
            print("You may be prompted for your sudo password.")
        subprocess.run(f"echo {MLOCK_MIN_KB} | sudo tee {mlock_path}", shell=True)
        mlock_kb = _read_int(mlock_path, 0)

    # Largest power-of-two-MB mmap ring that fits under the per-cpu cap (perf
    # requires -m to be a power-of-two page count; leave a page of headroom).
    mmap_mb = 0
    mb = 1
    while mb * 1024 <= mlock_kb - 4:
        mmap_mb = mb
        mb *= 2
    perf_mmap = ["-m", f"{mmap_mb}M"] if mmap_mb >= 1 else []
    mmap_desc = f"{mmap_mb}M" if mmap_mb >= 1 else "default"
    if mlock_kb < MLOCK_MIN_KB:
        print(f"warning: perf_event_mlock_kb is only {mlock_kb} KB; dwarf capture "
              f"may still drop samples (mmap {mmap_desc})", file=sys.stderr)

    return sudo_perf, perf_mmap, mmap_desc


def _perf_event_captures(event: str, perf_freq: str, sudo_perf: list[str],
                         perf_mmap: list[str], out_dir: Path) -> bool:
    """Record a short busy workload and report whether perf actually captured
    samples with this event. Catches a hybrid P/E-core PMU that silently opens
    no counter for `perf record` (while `perf stat` still counts)."""
    tmp = out_dir / ".perf-selftest.data"
    for f in (tmp, Path(str(tmp) + ".old")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    # `yes` burns CPU; timeout bounds it; >/dev/null keeps its output off perf's
    # pipe. Same dwarf + mmap options as the real capture, so the test exercises
    # the real path.
    proc = subprocess.run(
        # Not -N: perf counts the samples in its build-id pass, and perf 7.3,
        # which puts the build ids in the mmap events, skips that pass under -N
        # and prints no "(N samples)". The capture's own cache takes the few
        # binaries of the test instead.
        [*sudo_perf, "perf", "--buildid-dir", str(out_dir / BUILDID_DIR),
         "record", f"-F{perf_freq}", "-e", event,
         "--call-graph", "dwarf,32768", *perf_mmap, "-o", str(tmp),
         "--", "sh", "-c", "timeout 0.5 yes >/dev/null 2>&1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    m = re.search(r"\((\d+)\s+samples\)", proc.stdout or "")
    captured = bool(m and int(m.group(1)) > 0)
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    return captured


def pick_perf_event(preferred: str, perf_freq: str, sudo_perf: list[str],
                    perf_mmap: list[str], out_dir: Path) -> str | None:
    """First sampling event that actually captures on this CPU: the preferred
    one, then hybrid P/E-core variants, then task-clock (a software event that
    always works). None if perf can capture nothing at all."""
    candidates = [preferred, "cpu_core/cycles/", "cpu_atom/cycles/", "task-clock"]
    tried: list[str] = []
    for ev in candidates:
        if ev in tried:
            continue
        tried.append(ev)
        if _perf_event_captures(ev, perf_freq, sudo_perf, perf_mmap, out_dir):
            if ev != preferred:
                print(f"perf self-test: '{preferred}' captured no samples on this "
                      f"CPU; falling back to '{ev}'.")
            else:
                print(f"perf self-test: event '{ev}' captures samples - good.")
            return ev
        print(f"perf self-test: '{ev}' captured nothing, trying next ...")
    return None


class Phases:
    """The wall time of each phase of a capture, printed as each one ends and kept for
    metrics.json."""

    def __init__(self) -> None:
        self.mark = time.monotonic()
        self.seconds: dict[str, float] = {}

    def end(self, name: str) -> None:
        now = time.monotonic()
        took = now - self.mark
        self.mark = now
        self.seconds[name] = round(self.seconds.get(name, 0.0) + took, 1)
        print(f"[phase] {name}: {took:.1f}s", flush=True)


RENDER_JOB = "render-job.json"


def render_capture(out_dir: Path, perf_cmd: list[str], wsd_pids: list[str],
                   phases: "Phases", forkit_pids: list[str] = ()) -> None:
    """Draw out_dir/flamegraph.svg from out_dir/perf.data, and print the share of the
    kit and of the unresolved frames."""
    print("rendering flamegraph ...")
    # perf script | stackcollapse-perf.pl --pid | tag_by_process | flamegraph.pl --cp
    p1 = subprocess.Popen(
        # +pid: the default fields carry the thread id only
        [*perf_cmd, "script", "--no-inline", "-F", "+pid",
         "-i", str(out_dir / "perf.data")],
        stdout=subprocess.PIPE,
    )
    p2 = subprocess.Popen(["stackcollapse-perf.pl", "--pid"],
                          stdin=p1.stdout, stdout=subprocess.PIPE, text=True)
    p1.stdout.close()
    stacks = list(tag_by_process(p2.stdout, set(wsd_pids), set(forkit_pids)))
    render_flamegraph(stacks, out_dir / "flamegraph.svg")
    for p in (p1, p2):
        p.wait()
    phases.end("perf script and drawing the flamegraph")

    # Post-render sanity check: how much of the capture went to the kit, and how much did not
    # resolve. A flamegraph dominated by [unknown] is the jailed-kit case under system-wide or
    # cgroup capture and is not useful. Both figures come from the stacks drawn above.
    kit_pct, unknown_pct = stack_shares(stacks)
    print(f"flamegraph: {out_dir}/flamegraph.svg  "
          f"(kit {kit_pct:.0f}%, unresolved {unknown_pct:.0f}%)")
    if unknown_pct >= 50.0:
        print(f"WARNING: {unknown_pct:.0f}% of samples are [unknown] - "
              "symbols did not resolve. This is the jailed-kit case under "
              "system-wide/cgroup capture; use pid mode (drop --cgroup) "
              "and profile a single document for a symbolised flamegraph.",
              file=sys.stderr)
    phases.end("sanity check of the stacks")


def render_deferred(out_dir: Path) -> int:
    """Draw the flamegraph of a capture made with --defer-render, and add the time it
    took to the phases in its metrics.json."""
    job_path = out_dir / RENDER_JOB
    try:
        job = json.loads(job_path.read_text())
    except (OSError, ValueError) as e:
        print(f"no render job in {out_dir}: {e}", file=sys.stderr)
        return 1
    phases = Phases()
    # a job written before forkit_pids draws forkit in the colours of coolwsd
    render_capture(out_dir, job["perf_cmd"], job["wsd_pids"], phases,
                   job.get("forkit_pids", ()))
    metrics_path = out_dir / "metrics.json"
    try:
        metrics = json.loads(metrics_path.read_text())
        metrics.setdefault("phases_s", {}).update(phases.seconds)
        metrics_path.write_text(json.dumps(metrics, indent=2) + "\n")
    except (OSError, ValueError):
        pass
    job_path.unlink()
    return 0 if (out_dir / "flamegraph.svg").is_file() else 1


def main() -> int:
    args = parse_args()
    if args.render_only:
        return render_deferred(Path(args.out_dir))
    start_ts = time.monotonic()
    phases = Phases()

    # Only the k6 runner is supported. Kept as a named constant so the
    # summary strings and the reachability guard read clearly, and so the
    # puppeteer runner can be restored later if needed.
    runner = "k6"
    test_script = args.test_script or "dist/cool-load-document-test.js"

    if not args.coolwsd_dir:
        die("no online checkout given: set COOLWSD_DIR (or pass --coolwsd-dir) "
            "to the online checkout to profile")
    coolwsd_dir = Path(args.coolwsd_dir)
    coolwsd_url = args.coolwsd_url
    wopi_port = args.wopi_port
    out_dir = Path(args.out_dir)
    perf_freq = str(args.perf_freq)
    perf_event = args.perf_event

    host_ip = args.host_ip or detect_host_ip()
    if not host_ip:
        die("could not auto-detect HOST_IP; pass --host-ip <your host IP>")

    need("perf", "Install perf (Arch: sudo pacman -S perf, Fedora: sudo dnf install perf)")
    need("stackcollapse-perf.pl", "Install FlameGraph (Arch AUR: yay -S flamegraph; Fedora: sudo dnf install flamegraph; or git clone https://github.com/brendangregg/FlameGraph and add to PATH)")
    need("flamegraph.pl", "Install FlameGraph (same as above)")
    need("docker", "k6-wrap uses docker; install and enable the docker daemon")
    need("pgrep", "pgrep should be in any base install")

    if not coolwsd_dir.is_dir():
        die(f"coolwsd directory not found: {coolwsd_dir}")
    test_path = SCRIPT_DIR / test_script
    if not test_path.is_file():
        die(f"k6 test script not found: {test_path}\n"
            f"(did you run `npm run build` in {SCRIPT_DIR}?)")

    cgroup_mode = bool(args.cgroup)
    if cgroup_mode and not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        print("warning: --cgroup needs cgroup v2 (unified) mounted at "
              "/sys/fs/cgroup; using --pid targeting instead", file=sys.stderr)
        cgroup_mode = False

    if (cgroup_mode and not perf_can_bypass_paranoid()
            and _read_int("/proc/sys/kernel/perf_event_paranoid", 4) > 0
            and sudo_needs_password() and not sys.stdin.isatty()):
        print("warning: --cgroup needs kernel.perf_event_paranoid <= 0 and sudo "
              "cannot lower it here; using --pid targeting instead",
              file=sys.stderr)
        cgroup_mode = False

    sudo_perf, perf_mmap, mmap_desc = prepare_perf(system_wide=cgroup_mode)

    out_dir.mkdir(parents=True, exist_ok=True)

    # Preflight: confirm perf can actually capture with the chosen event and
    # auto-fall-back if not (hybrid P/E-core CPUs silently record nothing on the
    # default 'cycles'). Doing this now - before starting coolwsd and running
    # the whole workload - turns a wasted 2-minute run into a 2-second check.
    print("perf self-test: checking the sampling event captures on this CPU ...")
    chosen = pick_perf_event(perf_event, perf_freq, sudo_perf, perf_mmap, out_dir)
    if chosen is None:
        die("perf captured no samples in a self-test with any of "
            f"{perf_event}, cpu_core/cycles/, cpu_atom/cycles/, task-clock. "
            "perf recording is not working on this machine - check "
            "'perf record -e cycles -F99 -- sh -c \"timeout 1 yes >/dev/null\"' "
            "and perf_event_paranoid.")
    perf_event = chosen
    phases.end("perf self-test")

    capture_cg: Path | None = None  # cgroup coolwsd runs in, in cgroup mode

    coolwsd_proc: subprocess.Popen | None = None
    wopi_proc: subprocess.Popen | None = None
    perf_proc: subprocess.Popen | None = None
    perf_stat_procs: list[subprocess.Popen] = []
    runner_proc: subprocess.Popen | None = None
    started_coolwsd = False
    started_wopi = False

    def sigint_wait(proc: subprocess.Popen | None, timeout: int) -> None:
        """Stop a still-running perf process with SIGINT and wait for it to
        flush and exit, ignoring a wait timeout. perf may be running under
        sudo, so the signal is sent the same way."""
        if not proc or proc.poll() is not None:
            return
        subprocess.run([*sudo_perf, "kill", "-SIGINT", str(proc.pid)],
                       stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=timeout)
        except Exception:
            pass

    def cleanup() -> None:
        nonlocal perf_proc, perf_stat_procs, runner_proc, wopi_proc, coolwsd_proc
        sigint_wait(perf_proc, 10)
        for sp in perf_stat_procs:
            sigint_wait(sp, 10)
        if runner_proc and runner_proc.poll() is None:
            runner_proc.terminate()
            try:
                runner_proc.wait(timeout=10)
            except Exception:
                runner_proc.kill()
        if started_wopi and wopi_proc and wopi_proc.poll() is None:
            wopi_proc.terminate()
            try:
                wopi_proc.wait(timeout=10)
            except Exception:
                wopi_proc.kill()
        if started_coolwsd and args.stop_coolwsd and coolwsd_proc:
            # `make run` forks coolwsd, so signal the whole process group.
            # Default on so a follow-up run with different env (e.g.
            # SAL_KIT_OPTIONS) gets a fresh kit, pass
            # --no-stop-coolwsd to keep it alive between runs.
            #
            # Deliberately not gated on coolwsd_proc.poll() being None: make can
            # exit while the coolwsd it forked lives on, and a survivor holds its
            # client port for the rest of the session. killpg still reaches the
            # group through the leader's pid once the leader itself is gone, so
            # escalate until the port stops answering.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(coolwsd_proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    coolwsd_proc.wait(timeout=15)
                except Exception:
                    pass
                if not http_ok(f"{coolwsd_url}hosting/discovery"):
                    break
            if capture_cg and not remove_capture_cgroup(capture_cg):
                print(f"WARNING: could not remove cgroup {capture_cg}",
                      file=sys.stderr)
            if http_ok(f"{coolwsd_url}hosting/discovery"):
                print(f"WARNING: coolwsd at {coolwsd_url} still answers after "
                      "SIGTERM and SIGKILL to its process group. It is leaked "
                      "and holds that port: kill it before the next capture.",
                      file=sys.stderr)

    def on_signal(*_a):
        cleanup()
        sys.exit(130)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        # coolwsd. With --new-instance we always launch our own (so a profiling
        # instance can coexist with a coolwsd already on the default port);
        # otherwise attach to one already running, else launch.
        already = http_ok(f"{coolwsd_url}hosting/discovery")
        if already and not args.new_instance:
            print(f"WARNING: coolwsd is already running at {coolwsd_url}; "
                  "attaching to it instead of launching one.", file=sys.stderr)
            print("  It is not controlled by this tool: it may be a multi-kit "
                  "'make run' whose kits spawn and unload during the run, not "
                  "the single stable kit a clean capture needs.", file=sys.stderr)
            print("  For a reliable, symbolised profile, stop it first "
                  "(pkill -f coolwsd) and re-run, or pass --new-instance to "
                  "launch a separate single kit on its own port.", file=sys.stderr)
            if cgroup_mode:
                print("  --cgroup needs a coolwsd this tool launched; using "
                      "--pid targeting instead.", file=sys.stderr)
                cgroup_mode = False
        else:
            if already and args.new_instance:
                print(f"--new-instance: a coolwsd is already on {coolwsd_url}; "
                      "launching a separate single-kit one on its own free port.")
            print(f"starting coolwsd from {coolwsd_dir} ...")
            before_ports = listening_ports()
            log = (out_dir / "coolwsd.log").open("w")
            # No setsid wrapper here: start_new_session=True below already puts
            # the launcher in its own session, and setsid forks when it is
            # already a process group leader, so the tracked child would exit at
            # once. That left coolwsd_proc dead with an unknown pgid, cleanup
            # skipped the kill, and every run leaked its instance for the whole
            # session.
            cmd = shlex.split(args.cool_cmd)
            if cgroup_mode:
                capture_cg = make_capture_cgroup(f"coolperf-{os.getpid()}")
                if capture_cg is None:
                    print("warning: --cgroup could not create a cgroup under "
                          "this process's own (a service needs Delegate=yes); "
                          "using --pid targeting instead", file=sys.stderr)
                    cgroup_mode = False
            if cgroup_mode:
                # The shell joins wsd/ and execs the command, so coolwsd and
                # everything it forks start in that cgroup.
                cmd = ["sh", "-c", 'echo $$ > "$0" && exec "$@"',
                       str(capture_cg / "wsd" / "cgroup.procs"), *cmd]
                print(f"launching coolwsd in cgroup {capture_cg}/wsd ...")
            print(f"coolwsd command: {args.cool_cmd}")
            coolwsd_proc = subprocess.Popen(
                cmd,
                cwd=str(coolwsd_dir),
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            started_coolwsd = True
            if args.new_instance:
                # run-one bound the next free port; find which one and retarget.
                port = discover_new_cool_port(before_ports)
                if port is None:
                    die("could not determine the port the new coolwsd bound "
                        "(no new LISTEN socket answered hosting/discovery); "
                        f"see {out_dir}/coolwsd.log")
                coolwsd_url = f"https://localhost:{port}/"
                print(f"new coolwsd bound port {port} -> {coolwsd_url}")
            else:
                wait_for("coolwsd ready", 60,
                         lambda: http_ok(f"{coolwsd_url}hosting/discovery"))
            if cgroup_mode:
                moved, forkit_seen = move_kit_processes(capture_cg)
                print(f"moved forkit and its kits to {capture_cg}/kit "
                      f"(pids {','.join(moved) or 'none'})")
                if not forkit_seen:
                    print("WARNING: no forkit appeared in the coolwsd cgroup; "
                          "the kit counts will be empty", file=sys.stderr)

        wopi_scheme = args.wopi_scheme
        wopi_key = args.wopi_key or str(coolwsd_dir / "etc" / "key.pem")
        wopi_crt = args.wopi_crt or str(coolwsd_dir / "etc" / "cert.pem")
        wopi_base = f"{wopi_scheme}://{host_ip}:{wopi_port}"

        # WOPI host
        if http_ok(f"{wopi_base}/wopi/files/1"):
            print(f"WOPI host already running at {wopi_base}/")
        else:
            if wopi_scheme == "https" and (
                not Path(wopi_key).is_file() or not Path(wopi_crt).is_file()
            ):
                die(
                    "missing TLS cert/key for WOPI host:\n"
                    f"  key: {wopi_key}\n  crt: {wopi_crt}\n"
                    "set WOPI_SSL_KEY / WOPI_SSL_CRT (or WOPI_SCHEME=http) and retry."
                )
            print(f"starting WOPI host on {wopi_base}/ ...")
            log = (out_dir / "wopi.log").open("w")
            wopi_proc = subprocess.Popen(
                ["npm", "run", "server"],
                cwd=str(SCRIPT_DIR),
                env={**os.environ,
                     "PORT": wopi_port,
                     "SSL_KEY_FILE": wopi_key,
                     "SSL_CRT_FILE": wopi_crt,
                     "NODE_TLS_REJECT_UNAUTHORIZED": "0"},
                stdout=log, stderr=subprocess.STDOUT,
            )
            started_wopi = True
            wait_for("WOPI host ready", 30,
                     lambda: http_ok(f"{wopi_base}/wopi/files/1"))

        perf_data = out_dir / "perf.data"
        # perf record copies every binary it sampled into a build-id cache, by default
        # ~/.debug, which nothing empties: a machine that builds every hour gained a
        # 1.2 GB libmergedlo.so there per run. The capture keeps its own, beside
        # perf.data, so that the two go together.
        perf_cmd = [*sudo_perf, "perf", "--buildid-dir", str(out_dir / BUILDID_DIR)]

        k6_cidfile = out_dir / "k6.cid"

        def build_runner():
            # k6 runs inside docker; translate localhost -> HOST_IP so the
            # container reaches host coolwsd.
            url = re.sub(r"//(localhost|127\.0\.0\.1)([:/])",
                         f"//{host_ip}\\2", coolwsd_url)
            print(f"starting k6 test: {test_script} (WOPI_URL={url}) ...")
            env = {**os.environ, "WOPI_URL": url, "WOPI_HOST": f"{wopi_base}/",
                   "NODE_TLS_REJECT_UNAUTHORIZED": "0"}
            # k6-wrap writes the id of the k6 container there, which names its cgroup
            try:
                k6_cidfile.unlink()
            except FileNotFoundError:
                pass
            env["COOL_K6_CIDFILE"] = str(k6_cidfile.resolve())
            cmd = ["./k6-run", test_script]
            blog = (out_dir / "browser.log").open("w")
            return subprocess.Popen(cmd, cwd=str(SCRIPT_DIR), env=env,
                                    stdout=blog, stderr=subprocess.STDOUT)

        def start_perf(target, extra=()):
            try:
                perf_data.unlink()
            except FileNotFoundError:
                pass
            print(f"starting perf record (F={perf_freq}, event {perf_event}, "
                  f"mmap {mmap_desc}) ...")
            plog = (out_dir / "perf.log").open("w")
            # Name the event explicitly (and before any --cgroup in target): on
            # a hybrid P/E-core CPU perf record's implicit default event can
            # fail to open and capture nothing, while perf stat still counts.
            proc = subprocess.Popen(
                [*perf_cmd, "record", f"-F{perf_freq}", "-e", perf_event,
                 "--call-graph", "dwarf,32768", *perf_mmap, *extra, *target,
                 "-o", str(perf_data)],
                stdout=plog, stderr=subprocess.STDOUT)
            for _ in range(10):  # wait until perf is actually writing
                if perf_data.exists() and perf_data.stat().st_size > 0:
                    break
                time.sleep(0.5)
            if not perf_data.exists() or perf_data.stat().st_size == 0:
                sys.stderr.write("perf failed to start recording; perf.log says:\n")
                sys.stderr.write((out_dir / "perf.log").read_text())
                return None
            return proc

        def start_perf_stat_group(label: str, pids: list[str] = (),
                                  cgroup: str | None = None):
            """Count instructions/cycles for one process group (its own CSV),
            either a pid set or, in cgroup mode, a cgroup counted on every CPU.

            The groups are disjoint, so no task is counted twice - that keeps
            each event on its own hardware counter instead of multiplexing
            (which would scale the numbers). Output: perf-stat-<label>.csv."""
            if cgroup:
                target = ["-a", "-G", cgroup]
                desc = f"cgroup {cgroup}"
            elif pids:
                target = ["--pid", ",".join(pids)]
                desc = f"pid {','.join(pids)}"
            else:
                print(f"skipping perf stat ({label}): no pids to attach to")
                return None
            csv_out = out_dir / f"perf-stat-{label}.csv"
            try:
                csv_out.unlink()
            except FileNotFoundError:
                pass
            print(f"starting perf stat ({label}: {desc}) ...")
            slog = (out_dir / f"perf-stat-{label}.log").open("w")
            # LC_ALL=C forces a period decimal separator so the -x , CSV holds.
            return subprocess.Popen(
                [*sudo_perf, "perf", "stat", "-e", "instructions,cycles,task-clock",
                 *target, "-x", ",", "-o", str(csv_out)],
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                stdout=slog, stderr=subprocess.STDOUT)

        def start_perf_stat_groups() -> list[subprocess.Popen]:
            """pid mode: start one perf stat per process group. coolwsd/forkit
            are the WSD side; kitbroker_* is the document kit (LibreOfficeKit
            work)."""
            wsd_pids = sorted(set(ours(pgrep(["-x", "coolwsd"]) + pgrep(["-x", "forkit"]))))
            groups = [("coolwsd", wsd_pids), ("kit", kit_pids)]
            return [p for label, pids in groups
                    if (p := start_perf_stat_group(label, pids)) is not None]

        # The k6 container dials the host_ip-rewritten coolwsd URL, not
        # localhost. Verify that exact URL is reachable *before* running the
        # whole workload: coolwsd's readiness was checked on localhost, but it
        # may listen only on loopback, or --find-free-port may have bound a
        # different port - either way the container would get "connection
        # refused" and the run would waste minutes for nothing.
        dialed = re.sub(r"//(localhost|127\.0\.0\.1)([:/])",
                        f"//{host_ip}\\2", coolwsd_url)
        if not http_ok(f"{dialed}hosting/discovery"):
            die(f"coolwsd is up on localhost but not reachable at {dialed} "
                "- the address the k6 container will dial. It is likely "
                "listening only on loopback, or --find-free-port bound a "
                "different port. Check: ss -ltnp | grep -E ':(9980|998[0-9])'. "
                "The container reaches the host via its LAN IP, so coolwsd "
                "must listen on 0.0.0.0 (or that IP) on the expected port.")
        print(f"reachability: k6 will dial {dialed} (verified reachable).")

        # Both modes profile the live document kit (comm kitbroker_<hash>; its
        # lokit_runloop thread does the LO work), but they differ in ordering:
        #
        #  - cgroup mode: coolwsd runs inside a systemd --scope, so the scope's
        #    cgroup is known as soon as coolwsd is up. The kit is forked from
        #    coolwsd and inherits that same cgroup (membership follows fork and
        #    survives the kit's namespace unshare), so perf can attach to the
        #    cgroup BEFORE the workload runs. Opening the document then happens
        #    while perf is already recording, which captures the kit's initial
        #    import and first render, not just the steady-state scroll.
        #
        #  - pid mode: perf --pid cannot target a process that does not exist
        #    yet, so the workload must start first; we wait for the kit to spawn
        #    before attaching (and its pid is what resolves the jailed symbols).
        kit_pids: list[str] = []

        def ours(pids: list[str]) -> list[str]:
            """pids without the processes of a coolwsd this tool did not launch,
            such as the instance already serving port 9980. A process outside
            every coolwsd tree is kept."""
            if not (started_coolwsd and coolwsd_proc):
                return pids
            own = descendant_pids([str(coolwsd_proc.pid)])
            others = [p for p in pgrep(["-x", "coolwsd"]) if p not in own]
            if not others:
                return pids
            theirs = descendant_pids(others)
            return [p for p in pids if p not in theirs]

        def start_browser_stat(runner: subprocess.Popen):
            """Count the k6 container: Chromium, and k6 itself, which only drives it. The
            container is up a moment before k6 has compiled the test and launched
            Chromium, so the count starts before the browser does any work."""
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and runner.poll() is None:
                cid = (k6_cidfile.read_text().strip()
                       if k6_cidfile.exists() else "")
                for cg in ((CGROUP_ROOT / "system.slice" / f"docker-{cid}.scope",
                            CGROUP_ROOT / "docker" / cid) if cid else ()):
                    if cg.is_dir():
                        return start_perf_stat_group(
                            "browser", cgroup=cg.relative_to(CGROUP_ROOT).as_posix())
                time.sleep(0.02)
            print("warning: no cgroup of the k6 container found, so no browser "
                  "instructions", file=sys.stderr)
            return None

        def launch_and_wait_for_kit() -> tuple[float, subprocess.Popen, list[str]]:
            start = time.monotonic()
            proc = build_runner()
            if (p := start_browser_stat(proc)) is not None:
                perf_stat_procs.append(p)
            print("waiting for the document kit (kitbroker_*) to spawn ...")
            pids: list[str] = []
            for i in range(30):
                pids = ours(document_kit_pids())
                if pids:
                    print(f"document kit pid(s): {','.join(pids)}")
                    break
                if proc.poll() is not None:
                    break  # workload exited before a kit showed up
                # Stop waiting early if coolwsd itself has gone away (crash /
                # assert / OOM kill) - no kit can spawn from a dead coolwsd.
                if i % 3 == 2 and not http_ok(f"{coolwsd_url}hosting/discovery"):
                    print("coolwsd became unreachable before a kit spawned",
                          file=sys.stderr)
                    break
                time.sleep(1)
            if not pids:
                print("warning: no document kit (kitbroker_*) appeared; the doc "
                      "may not have opened", file=sys.stderr)
            return start, proc, pids

        phases.end("coolwsd and WOPI host start")
        if cgroup_mode:
            # perf -G takes the path relative to the cgroup2 mount, and counts
            # the tasks of the named cgroup and of its descendants.
            cgpath = capture_cg.relative_to(CGROUP_ROOT).as_posix()
            # Both counters run before the document is opened, so they count
            # the whole load whatever time the kit takes to appear.
            perf_stat_procs = [
                p for p in (start_perf_stat_group("coolwsd", cgroup=f"{cgpath}/wsd"),
                            start_perf_stat_group("kit", cgroup=f"{cgpath}/kit"))
                if p is not None]
            target = ["-a", "--cgroup", cgpath]
            print(f"perf will record cgroup {cgpath} (system-wide, cgroup-filtered)")
            # start_perf names the event (before --cgroup, as perf requires) and
            # --namespaces resolves symbols across the kit's namespaces.
            perf_proc = start_perf(target, extra=["--namespaces"])
            if perf_proc is None:
                return 1
            runner_start, runner_proc, kit_pids = launch_and_wait_for_kit()
        else:
            # perf --pid needs the kit to exist first: open the document, wait
            # for the kit, then attach.
            runner_start, runner_proc, kit_pids = launch_and_wait_for_kit()
            cool_pids = sorted(set(
                ours(pgrep(["-x", "coolwsd"]) + pgrep(["-x", "forkit"])) + kit_pids))
            if not cool_pids:
                print("no coolwsd processes found; perf will record system-wide (-a)")
                target = ["-a"]
            else:
                print(f"perf will record PIDs: {','.join(cool_pids)}")
                target = ["--pid", ",".join(cool_pids)]
            perf_proc = start_perf(target)
            if perf_proc is None:
                return 1
        phases.end("perf start, k6 start until the kit appears")

        # Fail fast if the document never opened: no kit spawned and coolwsd is
        # no longer reachable means it died during startup or the first load
        # (crash, assert, or an OOM kill under load). Recording on would only
        # produce an empty perf.data, so stop now and point at the log. cleanup()
        # in the finally block tears down the perf we just started.
        if not kit_pids and not http_ok(f"{coolwsd_url}hosting/discovery"):
            print("ERROR: coolwsd is no longer reachable and no document kit "
                  "ever spawned - it died during the run, so there is nothing "
                  "to profile (perf.data would be empty). See "
                  f"{out_dir}/coolwsd.log for the cause (crash / assert / OOM). "
                  "Under a heavy or concurrent load a --singlekit coolwsd (what "
                  "'make run-one' starts) is a common culprit; profile a single "
                  "document, or run the load test without perf.", file=sys.stderr)
            return 1

        # pid mode: count instructions/cycles per process group now that the
        # kit pid is known, one perf stat for coolwsd+forkit, one for the
        # document kit, written to perf-stat-coolwsd.csv / perf-stat-kit.csv.
        # Whatever the kit ran before this point is not counted.
        if not cgroup_mode:
            perf_stat_procs += start_perf_stat_groups()

        print(f"waiting for {runner} to finish ...")
        # In pid mode perf only follows the kit(s) that existed at attach time.
        # If a targeted kit is unloaded mid-run (a workload that churns
        # documents, or a multi-kit coolwsd), perf loses its target and the
        # capture ends up empty or partial - warn loudly rather than fail
        # silently and hand back an unreadable perf.data.
        monitor_kits = (not cgroup_mode) and bool(kit_pids)
        warned_kit_gone = False

        # Sample resident memory every couple of seconds while the workload
        # runs, so the JSON carries a time series and a true concurrent peak.
        # coolwsd's and forkit's pids are stable for the run, so resolve once.
        mem_forkit_pids = sorted(set(ours(pgrep(["-x", "forkit"]))))
        mem_wsd_pids = sorted(set(ours(pgrep(["-x", "coolwsd"])) + mem_forkit_pids))
        # The browser runs in the k6 container and starts its processes as the
        # page needs them, so those groups are looked up again at each sample.
        mem_sampler = MemorySampler({
            "coolwsd": mem_wsd_pids, "kit": kit_pids,
            "browser_renderer": lambda: browser_pids()["renderer"],
            "browser_gpu": lambda: browser_pids()["gpu"],
            "browser_other": lambda: browser_pids()["other"],
        })

        mem_sampler.sample()
        while True:
            try:
                runner_rc = runner_proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                pass
            mem_sampler.sample()
            if monitor_kits and not warned_kit_gone:
                alive = [p for p in kit_pids if Path(f"/proc/{p}").exists()]
                if not alive:
                    warned_kit_gone = True
                    print("WARNING: the profiled document kit(s) "
                          f"({','.join(kit_pids)}) exited while perf was still "
                          "recording. pid mode follows only the kit that existed "
                          "at attach time, so this capture will be empty or "
                          "partial. Profile a single stable document, or re-run "
                          "with no coolwsd running so a controlled single kit is "
                          "launched.", file=sys.stderr)
        runner_proc = None
        phases.end("test, until k6 exits")
        runner_elapsed = int(time.monotonic() - runner_start)
        print(f"{runner} elapsed: {runner_elapsed}s")

        # let perf capture a tail then stop it gracefully
        time.sleep(2)
        print("stopping perf ...")
        print("perf report: writing and finalizing the capture on shutdown "
              "(this can take a while for a large perf.data) ...")
        sigint_wait(perf_proc, 30)
        perf_proc = None

        for sp in perf_stat_procs:
            sigint_wait(sp, 15)
        perf_stat_procs = []
        phases.end("perf writing the capture")

        # Validate the capture before rendering: an empty or header-only
        # perf.data renders to a useless flamegraph ("No stack counts found").
        # Read the sample count perf logged and fail loudly with the likely
        # cause instead.
        perf_log_text = ((out_dir / "perf.log").read_text(errors="replace")
                         if (out_dir / "perf.log").exists() else "")
        m = re.search(r"\((\d+)\s+samples\)", perf_log_text)
        n_samples = int(m.group(1)) if m else None
        empty = (not perf_data.exists() or perf_data.stat().st_size == 0
                 or n_samples == 0
                 or (n_samples is None and perf_data.stat().st_size < 100_000))
        if empty:
            print("ERROR: perf captured no usable samples - refusing to render "
                  "an empty flamegraph.", file=sys.stderr)
            print("  Likely causes: the sampling event did not open on this CPU "
                  "(hybrid P/E-core - the self-test should have caught this), the "
                  "profiled process was idle, or the kit exited mid-capture.",
                  file=sys.stderr)
            if perf_log_text.strip():
                sys.stderr.write("  perf.log:\n" + perf_log_text)
            return 1
        print(f"capture: {n_samples if n_samples is not None else 'unknown'} "
              "samples recorded.")

        if runner_rc != 0:
            print(f"{runner} test failed (rc={runner_rc}); see {out_dir}/browser.log",
                  file=sys.stderr)

        if args.defer_render:
            (out_dir / RENDER_JOB).write_text(json.dumps(
                {"perf_cmd": perf_cmd, "wsd_pids": mem_wsd_pids,
                 "forkit_pids": mem_forkit_pids}) + "\n")
            print(f"flamegraph: deferred, run --render-only --out-dir {out_dir}")
        else:
            render_capture(out_dir, perf_cmd, mem_wsd_pids, phases, mem_forkit_pids)

        total_elapsed = int(time.monotonic() - start_ts)
        print()
        print("done.")
        print(f"  {runner} elapsed : {runner_elapsed}s")
        print(f"  total elapsed: {total_elapsed}s")
        print(f"  {runner} log    : {out_dir}/browser.log")
        print(f"  perf data    : {perf_data}")
        print(f"  flamegraph   : {out_dir}/flamegraph.svg")

        # Gather and print the run summary, then write metrics.json.
        workload = collect_workload(out_dir / "browser.log")
        print_workload(workload)
        perf_by_label = collect_perf(out_dir)
        print_perf(perf_by_label)
        mem_by_label = mem_sampler.summary()
        print_memory(mem_by_label)

        # Machine-readable dump for plotting IPC / CPU time / memory across runs.
        metrics = {
            "out_dir": str(out_dir),
            "runner": runner,
            "test_script": test_script,
            "perf_event": perf_event,
            "runner_elapsed_s": runner_elapsed,
            "total_elapsed_s": total_elapsed,
            "phases_s": phases.seconds,
            "flamegraph_samples": flamegraph_sample_count(out_dir),
            "workload": workload,
            "perf": perf_by_label,
            "memory": mem_by_label,
        }
        metrics_path = out_dir / "metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"  metrics json : {metrics_path}")
        return runner_rc
    finally:
        cleanup()
        phases.end("stopping coolwsd and the test")


if __name__ == "__main__":
    sys.exit(main())
