#!/usr/bin/env python3
"""The service behind the Schedule and Capacity tabs of the dashboard: it deletes runs from the
store, and queues runs of Gerrit changes for deploy/run-queue.

nginx passes /api/ to it (deploy/nginx/cool-perf.conf), and cool-perf-api.service
runs it as the service user, which owns the store. It listens on localhost only.

    POST /api/runs/delete   {"runids": ["20261102T121359Z-e6aa2e06e7ec", ...]}

removes each run folder and its JUnit report, then rebuilds index.html, and
answers {"deleted": [...]}. A run id that is not in the store fails the whole
request, and nothing is deleted.

    GET  /api/schedule                  the queue, the next run of the timer, and the
                                        cool-perf units started by hand
    POST /api/schedule                  {"change": "18591/3", "runs": 3, "at": null or ISO time}
                                        or a capacity run of a change or a ref of online:
                                        {"kind": "capacity", "target": "18591" or "origin/main",
                                         "scenario": "writer", "profile": "heap", "at": ...}
    POST /api/schedule/main             {"paused": true or false}: pause or resume the runs that
                                        the timer starts on main
    POST /api/schedule/<job>/cancel     a queued job
    GET  /api/schedule/<job>/log        the end of the log of a job
    GET  /api/capacity                  a summary of each capacity run
    GET  /api/capacity/<run>            the time series of one capacity run

deploy/runqueue.py checks a change before it is queued.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from flask import Flask, Response, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capacity  # noqa: E402
import runqueue  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STORE = Path(os.environ.get("COOL_PERF_STORE", "/var/lib/cool-perf"))
PORT = int(os.environ.get("COOL_PERF_API_PORT", "8090"))
# While this file exists, deploy/nightly ends at once, so the timer measures nothing. The service
# user owns the store, and may not enable or disable the timer itself.
MAIN_PAUSED = STORE / "main-paused"
# A run id is a folder name in results/, never a path.
RUNID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
deleting = threading.Lock()


def fail(status: int, error: str):
    return jsonify(error=error), status


def foreign():
    """There is no login, so at least refuse a request that another site's page sends: a
    form cannot post JSON, and a script of another origin carries its Origin."""
    if not request.is_json:
        return fail(415, "the request must be JSON")
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        return fail(403, f"requests from {origin} are not accepted")
    return None


@app.post("/api/runs/delete")
def delete_runs():
    if refused := foreign():
        return refused
    body = request.get_json(silent=True)
    runids = body.get("runids") if isinstance(body, dict) else None
    if not isinstance(runids, list) or not runids:
        return fail(400, "give the runs to delete as a list in runids")
    if not all(isinstance(r, str) and RUNID.fullmatch(r) for r in runids):
        return fail(400, "a run id holds letters, digits, dots, dashes and underscores")
    runids = list(dict.fromkeys(runids))

    results = STORE / "results"
    with deleting:
        missing = [r for r in runids if not (results / r).is_dir()]
        if missing:
            return fail(404, "not in the store: " + ", ".join(missing))
        # The folders leave results/ first, with a rename, so a rebuild that runs at the
        # same time never reads a run that is half deleted.
        trash = STORE / ".deleting"
        trash.mkdir(exist_ok=True)
        for r in runids:
            os.replace(results / r, trash / r)
            (STORE / "junit" / f"{r}.xml").unlink(missing_ok=True)
        build = subprocess.run(
            [sys.executable, str(REPO / "browser-suite.py"),
             "--results", str(results), "-o", str(STORE / "index.html")],
            cwd=REPO, capture_output=True, text=True)
        for r in runids:
            shutil.rmtree(trash / r, ignore_errors=True)
    app.logger.info("deleted %s", " ".join(runids))
    if build.returncode != 0:
        return fail(500, "the runs are deleted, but the dashboard was not rebuilt: "
                    + (build.stderr.strip() or build.stdout.strip())[-500:])
    return jsonify(deleted=runids)


# The units of the VM, read with systemctl, which the service user may do. The tab shows these
# by themselves, so they are not among the units started by hand.
OWN_UNITS = {"cool-perf-api.service", "cool-perf-queue.service", "cool-perf.service",
             "cool-perf.timer"}


def systemctl_show(units: list[str], props: list[str]) -> list[dict]:
    if not units:
        return []
    try:
        out = subprocess.run(["systemctl", "show", "--timestamp=unix", *units,
                              *[f"--property={p}" for p in props]],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    blocks = []
    for block in out.strip().split("\n\n"):
        blocks.append(dict(line.split("=", 1) for line in block.splitlines() if "=" in line))
    return blocks


def when(value: str | None) -> str | None:
    """A systemd time of --timestamp=unix, @<seconds>, in ISO form."""
    if not value or not value.startswith("@"):
        return None
    return runqueue.iso(datetime.fromtimestamp(int(value[1:]), timezone.utc))


def started(unit: dict) -> str | None:
    """When a unit started. A oneshot service stays activating while it runs and gets no
    ActiveEnterTimestamp, so the time it left the inactive state is the start."""
    return when(unit.get("ActiveEnterTimestamp")) or when(unit.get("InactiveExitTimestamp"))


CGROUP = Path("/sys/fs/cgroup")


def waits_for_lock(unit: dict) -> bool:
    """Whether a unit that systemd shows as running waits for the lock of deploy/nightly, in the
    flock of take_lock or of nightly, rather than measures. Its cgroup holds that flock process,
    or a cgroup under it does with Delegate=yes."""
    group = unit.get("ControlGroup")
    if not group:
        return False
    for procs in (CGROUP / group.lstrip("/")).rglob("cgroup.procs"):
        try:
            pids = procs.read_text().split()
        except OSError:
            continue
        for pid in pids:
            try:
                if Path(f"/proc/{pid}/comm").read_text().strip() == "flock":
                    return True
            except OSError:
                pass
    return False


def lock_holder() -> tuple[str, str] | None:
    """(cgroup, ISO time) of the script that holds the lock of deploy/nightly or held it last,
    from the lock-holder file that take_lock writes, while that script still runs."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    try:
        pid, at = (state / "cool-perf" / "lock-holder").read_text().split()
        # cgroup v2: one line, 0::<path>
        group = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
        return group, runqueue.iso(datetime.fromtimestamp(int(at), timezone.utc))
    except (OSError, ValueError, IndexError):
        return None


def lock_step() -> dict | None:
    """The step that the script holding the lock is at, from the lock-step file that lock_step of
    deploy/common.sh writes: {"text": such as "building" or "measuring, run 2 of 3", "since": ISO
    time}. None when lock-step was written by another script than the one of lock-holder."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "cool-perf"
    try:
        holder_pid = (state / "lock-holder").read_text().split()[0]
        pid, at, text = (state / "lock-step").read_text().strip().split(" ", 2)
        if pid != holder_pid:
            return None
        return {"text": text,
                "since": runqueue.iso(datetime.fromtimestamp(int(at), timezone.utc))}
    except (OSError, ValueError, IndexError):
        return None


def measuring_since(unit: dict, holder: tuple[str, str] | None) -> str | None:
    """When a unit that does not wait for the lock took it: the time of lock-holder, when the
    script that wrote it runs in the cgroup of the unit."""
    group = unit.get("ControlGroup")
    if not holder or not group:
        return None
    held_by, at = holder
    return at if held_by == group or held_by.startswith(group + "/") else None


def command_of(unit: dict) -> list[str]:
    """The command line of a service, from the argv[] of its ExecStart."""
    m = re.search(r"argv\[\]=(.*?) ;", unit.get("ExecStart", ""), re.S)
    if not m:
        return []
    # A script given to sh -c can continue a line with a backslash, which systemctl prints as it
    # is or escaped.
    return re.sub(r"\\(\n|n(?=\S))", " ", m.group(1)).split()


def commands_of(unit: dict) -> list[list[str]]:
    """The commands that a service runs: its ExecStart, or the commands of the script that it
    gives to sh -c, split at && and ;."""
    argv = [a.strip("'\"") for a in command_of(unit)]
    if len(argv) > 2 and Path(argv[0]).name in ("sh", "bash") and argv[1] == "-c":
        argv = argv[2:]
    out, cur = [], []
    for a in argv:
        if a in ("&&", "||", ";"):
            out.append(cur)
            cur = []
        elif a and a != "\\":
            cur.append(a.rstrip(";"))
            if a.endswith(";"):
                out.append(cur)
                cur = []
    out.append(cur)
    return [c for c in out if c]


def command_named(commands: list[list[str]], name: str) -> list[str]:
    """The first of @p commands whose program is @p name, or an empty list."""
    return next((c for c in commands if Path(c[0]).name == name), [])


# The patch sets that units name, by the text of their --gerrit. A failed lookup is not kept, so
# the next poll of the tab asks Gerrit again.
patchsets: dict[str, dict] = {}


def branch_change(argv: list[str]) -> dict | None:
    """The Gerrit patch set that a command of deploy/run-branch or deploy/run-capacity measures,
    with its subject and uploader as the queue shows them, and the number of runs. Only the
    change and the patch set that the command gives when Gerrit cannot be read. None for another
    command, or one without --gerrit."""
    if not argv or Path(argv[0]).name not in ("run-branch", "run-capacity") \
            or "--gerrit" not in argv[:-1]:
        return None
    text = argv[argv.index("--gerrit") + 1]
    runs = argv[argv.index("--runs") + 1] if "--runs" in argv[:-1] else "1"
    if text not in patchsets:
        try:
            patchsets[text] = runqueue.resolve(text)
        except runqueue.Refused:
            m = runqueue.CHANGE.fullmatch(text)
            if not m:
                return None
            return {"change": int(m.group(1)),
                    "patchset": int(m.group(2)) if m.group(2) else None,
                    "runs": int(runs) if runs.isdigit() else 1}
    return {**patchsets[text], "runs": int(runs) if runs.isdigit() else 1}


def all_scenarios() -> list[str]:
    """The scenarios that deploy/run-capacity knows."""
    try:
        text = (REPO / "deploy" / "run-capacity").read_text()
    except OSError:
        return []
    return " ".join(re.findall(r'^SCENARIOS\+?="([^"]*)"', text, re.M)).split()


def capacity_scenarios(argv: list[str], environment: str) -> list[str] | None:
    """The scenarios that a unit of deploy/run-capacity runs, in order: its arguments, all of
    them for "all", else the CAP_SCENARIO of its environment, else blank.odt. None for a unit
    that runs something else."""
    if not argv or Path(argv[0]).name != "run-capacity":
        return None
    # the arguments that are not options, nor the value of --gerrit or --ref
    names, skip = [], False
    for a in argv[1:]:
        if skip:
            skip = False
        elif a in ("--gerrit", "--ref"):
            skip = True
        elif not a.startswith("-"):
            names.append(a)
    if names == ["all"]:
        names = all_scenarios() or names
    if names:
        return names
    m = re.search(r"(?:^|\s)CAP_SCENARIO=(\S+)", environment)
    return [m.group(1)] if m else ["blank.odt"]


def capacity_running() -> str | None:
    """The scenario of the capacity run in progress: the newest run of the store, while it has
    no capacity.json yet."""
    root = STORE / "capacity"
    try:
        newest = max((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    except (OSError, ValueError):
        return None
    if (newest / "capacity.json").exists():
        return None
    try:
        return (newest / "scenario").read_text().strip() or "blank.odt"
    except OSError:
        return "blank.odt"


def units_by_hand(holder: tuple[str, str] | None = None) -> list[dict]:
    """The cool-perf units that someone started with systemd-run, running or waiting for their
    time, as the units of DEPLOY-VM.md are."""
    try:
        out = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend",
                              "cool-perf-*"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = [line.split()[0] for line in out.splitlines() if line.split()]
    names = [n for n in names if n not in OWN_UNITS]
    found = []
    # a timer of systemd-run starts the service of the same name, which holds the command
    services = {u.get("Id"): u for u in systemctl_show(
        [n.rsplit(".", 1)[0] + ".service" for n in names], ["Id", "ExecStart", "Environment"])}
    for u in systemctl_show(names, ["Id", "ActiveState", "ActiveEnterTimestamp",
                                    "InactiveExitTimestamp", "NextElapseUSecRealtime",
                                    "ControlGroup"]):
        service = services.get(u.get("Id", "").rsplit(".", 1)[0] + ".service", {})
        commands = commands_of(service)
        scenarios = capacity_scenarios(command_named(commands, "run-capacity"),
                                       service.get("Environment", ""))
        # the change that a unit of deploy/run-branch or deploy/run-capacity measures, as a job
        # of the queue shows it; a capacity unit makes one run for each scenario
        gerrit = branch_change(command_named(commands, "run-branch"))
        if not gerrit:
            gerrit = branch_change(command_named(commands, "run-capacity"))
            if gerrit and scenarios:
                gerrit = {**gerrit, "runs": len(scenarios)}
        # scheduled: a timer of systemd-run that has not fired; waiting: started, but waiting
        # for the lock
        if u.get("Id", "").endswith(".timer"):
            if when(u.get("NextElapseUSecRealtime")):
                found.append({"unit": u["Id"], "state": "scheduled",
                              "at": when(u.get("NextElapseUSecRealtime")),
                              "scenarios": scenarios, "gerrit": gerrit})
        elif u.get("ActiveState") in ("active", "activating"):
            waiting = waits_for_lock(u)
            measuring = None if waiting else measuring_since(u, holder)
            found.append({"unit": u["Id"],
                          "state": "waiting" if waiting else "running",
                          "since": started(u),
                          "measuring_since": measuring,
                          "step": lock_step() if measuring else None,
                          # the capacity scenarios it runs, and the one it measures now
                          "scenarios": scenarios,
                          "scenario": capacity_running() if scenarios and measuring else None,
                          "gerrit": gerrit})
    return found


@app.get("/api/capacity")
def capacity_runs():
    return jsonify(runs=capacity.runs(STORE))


@app.get("/api/capacity/<run>")
def capacity_run(run):
    d = capacity.detail(STORE, run)
    return jsonify(d) if d else fail(404, f"no capacity run {run}")


# The change that merged each commit of a capacity job queued before jobs recorded it, looked up
# on Gerrit once per commit while the service runs. None is a commit with no change found.
merged_by_commit: dict[str, dict | None] = {}


def merged_of(job: dict) -> dict | None:
    if job.get("change") or job.get("kind") != "capacity":
        return None
    if "merged" in job:
        return job["merged"]
    ref = job.get("ref") or ""
    if not runqueue.SHA.fullmatch(ref):
        return None
    if ref not in merged_by_commit:
        merged_by_commit[ref] = runqueue.merged_change(ref)
    return merged_by_commit[ref]


# The newest commit of each branch of online on Gerrit, as (time read, sha), read again after a
# minute.
branch_tips: dict[str, tuple[float, str | None]] = {}


def branch_tip(branch: str) -> str | None:
    seen = branch_tips.get(branch)
    if seen and time.monotonic() - seen[0] < 60:
        return seen[1]
    sha = None
    url = f"{runqueue.GERRIT_URL}/projects/online/branches/{quote(branch, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            sha = json.loads(r.read().decode().split("\n", 1)[1]).get("revision")
    except (OSError, ValueError, IndexError):
        pass
    branch_tips[branch] = (time.monotonic(), sha)
    return sha


# The commit that a finished job of a ref built, from its log, kept once read.
built_by_job: dict[str, str | None] = {}
BUILT = re.compile(rb"^=== capacity run of ([0-9a-f]{12})", re.M)


def commit_of(job: dict) -> str | None:
    """The commit that a job of a branch or a tag built, from its log, or the newest commit of
    the branch for a job that has not built yet."""
    ref = job.get("ref") or ""
    if job.get("change") or runqueue.SHA.fullmatch(ref):
        return None
    jid = job["id"]
    if jid in built_by_job:
        return built_by_job[jid]
    sha = None
    try:
        m = BUILT.search(runqueue.log_path(jid).read_bytes())
        sha = m.group(1).decode() if m else None
    except OSError:
        pass
    if job["state"] in ("done", "failed", "cancelled"):
        built_by_job[jid] = sha
    elif not sha and job["state"] == "queued":
        sha = branch_tip(ref.removeprefix("origin/"))
    return sha


@app.get("/api/schedule")
def schedule():
    timer = (systemctl_show(["cool-perf.timer"], ["NextElapseUSecRealtime", "LastTriggerUSec"])
             or [{}])[0]
    service = (systemctl_show(["cool-perf.service"], ["ActiveState", "ActiveEnterTimestamp",
                                                      "InactiveExitTimestamp", "ControlGroup"])
               or [{}])[0]
    queue = (systemctl_show(["cool-perf-queue.service"], ["ActiveState", "ControlGroup"])
             or [{}])[0]
    running = service.get("ActiveState") in ("active", "activating")
    main_waiting = running and waits_for_lock(service)
    holder = lock_holder()
    jobs = runqueue.jobs()
    for j in jobs:
        if merged := merged_of(j):
            j["merged"] = merged
        if commit := commit_of(j):
            j["commit"] = commit
    # The runner runs one job at a time, so a flock in its cgroup is the one of that job, and the
    # lock that its cgroup holds is the one of that job too.
    queue_waiting = waits_for_lock(queue)
    for j in jobs:
        if j["state"] == "running":
            if queue_waiting:
                j["waiting"] = True
            else:
                j["measuring_since"] = measuring_since(queue, holder)
                j["step"] = lock_step() if j["measuring_since"] else None
    main_measuring = (measuring_since(service, holder)
                      if running and not main_waiting else None)
    return jsonify(
        now=runqueue.iso(runqueue.now()),
        main={"next": when(timer.get("NextElapseUSecRealtime")),
              "last": when(timer.get("LastTriggerUSec")),
              "running": running, "waiting": main_waiting,
              "paused": MAIN_PAUSED.exists(),
              "commit": branch_tip("main"),
              "since": started(service),
              "measuring_since": main_measuring,
              "step": lock_step() if main_measuring else None},
        runner=queue.get("ActiveState"),
        jobs=jobs, by_hand=units_by_hand(holder),
        domains=runqueue.DOMAINS, max_runs=runqueue.MAX_RUNS)


@app.post("/api/schedule")
def schedule_add():
    if refused := foreign():
        return refused
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return fail(400, "give change, runs and at")
    at = None
    if body.get("at"):
        try:
            at = datetime.fromisoformat(str(body["at"]).replace("Z", "+00:00"))
        except ValueError:
            return fail(400, "at is an ISO date and time")
        if at.tzinfo is None:
            return fail(400, "at needs its time zone")
    try:
        if body.get("kind") == "capacity":
            job = runqueue.add_capacity(str(body.get("target", "")),
                                        str(body.get("scenario") or ""),
                                        str(body.get("profile", "")), all_scenarios(), at)
        else:
            job = runqueue.add(str(body.get("change", "")), body.get("runs", 3), at)
    except runqueue.Refused as e:
        return fail(400, str(e))
    if job.get("kind") == "capacity":
        app.logger.info("queued %s: capacity run of %s, %s, %s", job["id"],
                        job.get("ref") or f"{job['change']}/{job['patchset']}",
                        job["scenario"] or "blank.odt", job["profile"])
    else:
        app.logger.info("queued %s: change %s/%s, %s runs", job["id"], job["change"],
                        job["patchset"], job["runs"])
    return jsonify(job=job)


@app.post("/api/schedule/main")
def schedule_main():
    if refused := foreign():
        return refused
    body = request.get_json(silent=True)
    paused = body.get("paused") if isinstance(body, dict) else None
    if not isinstance(paused, bool):
        return fail(400, "give paused as true or false")
    if paused:
        MAIN_PAUSED.touch()
    else:
        MAIN_PAUSED.unlink(missing_ok=True)
    app.logger.info("%s the runs of main", "paused" if paused else "resumed")
    return jsonify(paused=paused)


@app.post("/api/schedule/<jobid>/cancel")
def schedule_cancel(jobid: str):
    if refused := foreign():
        return refused
    if not runqueue.JOBID.fullmatch(jobid):
        return fail(400, "not a job id")
    try:
        return jsonify(job=runqueue.cancel(jobid))
    except runqueue.Refused as e:
        return fail(409, str(e))


@app.get("/api/schedule/<jobid>/log")
def schedule_log(jobid: str):
    if not runqueue.JOBID.fullmatch(jobid):
        return fail(400, "not a job id")
    try:
        with open(runqueue.log_path(jobid), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            text = f.read().decode(errors="replace")
    except FileNotFoundError:
        text = "" if runqueue.read(jobid) else None
    if text is None:
        return fail(404, f"there is no job {jobid}")
    return Response(text, mimetype="text/plain")


if __name__ == "__main__":
    # Flask's own server: one endpoint, on localhost, used by a few people.
    app.run(host="127.0.0.1", port=PORT, threaded=False)
