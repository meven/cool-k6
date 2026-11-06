#!/usr/bin/env python3
"""The service behind the Runs and Schedule tabs of the dashboard: it deletes runs from the
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
    POST /api/schedule/<job>/cancel     a queued job
    GET  /api/schedule/<job>/log        the end of the log of a job

deploy/runqueue.py checks a change before it is queued.
"""
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runqueue  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STORE = Path(os.environ.get("COOL_PERF_STORE", "/var/lib/cool-perf"))
PORT = int(os.environ.get("COOL_PERF_API_PORT", "8090"))
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


def measuring_since(unit: dict, holder: tuple[str, str] | None) -> str | None:
    """When a unit that does not wait for the lock took it: the time of lock-holder, when the
    script that wrote it runs in the cgroup of the unit."""
    group = unit.get("ControlGroup")
    if not holder or not group:
        return None
    held_by, at = holder
    return at if held_by == group or held_by.startswith(group + "/") else None


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
    for u in systemctl_show(names, ["Id", "ActiveState", "ActiveEnterTimestamp",
                                    "InactiveExitTimestamp", "NextElapseUSecRealtime",
                                    "ControlGroup"]):
        # scheduled: a timer of systemd-run that has not fired; waiting: started, but waiting
        # for the lock
        if u.get("Id", "").endswith(".timer"):
            if when(u.get("NextElapseUSecRealtime")):
                found.append({"unit": u["Id"], "state": "scheduled",
                              "at": when(u.get("NextElapseUSecRealtime"))})
        elif u.get("ActiveState") in ("active", "activating"):
            waiting = waits_for_lock(u)
            found.append({"unit": u["Id"],
                          "state": "waiting" if waiting else "running",
                          "since": started(u),
                          "measuring_since": None if waiting else measuring_since(u, holder)})
    return found


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
    # The runner runs one job at a time, so a flock in its cgroup is the one of that job, and the
    # lock that its cgroup holds is the one of that job too.
    queue_waiting = waits_for_lock(queue)
    for j in jobs:
        if j["state"] == "running":
            if queue_waiting:
                j["waiting"] = True
            else:
                j["measuring_since"] = measuring_since(queue, holder)
    return jsonify(
        now=runqueue.iso(runqueue.now()),
        main={"next": when(timer.get("NextElapseUSecRealtime")),
              "last": when(timer.get("LastTriggerUSec")),
              "running": running, "waiting": main_waiting,
              "since": started(service),
              "measuring_since": (measuring_since(service, holder)
                                  if running and not main_waiting else None)},
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
        job = runqueue.add(str(body.get("change", "")), body.get("runs", 3), at)
    except runqueue.Refused as e:
        return fail(400, str(e))
    app.logger.info("queued %s: change %s/%s, %s runs", job["id"], job["change"],
                    job["patchset"], job["runs"])
    return jsonify(job=job)


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
