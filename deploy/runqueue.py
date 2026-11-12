"""The queue of scheduled runs, shared by deploy/runs-api.py, which adds and cancels jobs, and
deploy/run-queue, which runs them.

A job is one JSON file in the queue directory, under the state directory of the service user,
outside the store that nginx serves. Its log is beside it. A job goes from queued to running
to done or failed, or from queued to cancelled. Every change to a job file is made under the
lock of the queue, so the runner never starts a job that is being cancelled.
"""
import contextlib
import fcntl
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GERRIT_URL = os.environ.get("COOL_GERRIT_URL", "https://gerrit.collaboraoffice.com").rstrip("/")
# A scheduled run builds and runs the code of a patch set, so only a patch set that an account
# of these domains uploaded is accepted.
DOMAINS = [d.strip().lower().lstrip("@")
           for d in os.environ.get("COOL_PERF_SCHEDULE_DOMAINS",
                                   "collabora.com,collabora.co.uk").split(",")
           if d.strip()]
MAX_RUNS = 5
# How many finished jobs the queue keeps, with their logs.
KEEP_DONE = 30

# The kinds of profile of a capacity job: a perf flamegraph, or a heaptrack record of the server.
PROFILES = ("flamegraph", "heap")
# A branch, tag or commit of the online repository, which cool-build fetches from origin.
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")

JOBID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
# 18591, 18591/3, or the address of the change on Gerrit, with or without a patch set.
CHANGE = re.compile(r"(?:.*/\+/)?(\d{1,7})(?:/(\d{1,4}))?/?")


def queue_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    d = state / "cool-perf" / "queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def locked():
    with open(queue_dir() / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _path(jobid: str) -> Path:
    return queue_dir() / f"{jobid}.json"


def log_path(jobid: str) -> Path:
    return queue_dir() / f"{jobid}.log"


def write(job: dict) -> None:
    tmp = _path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1) + "\n")
    os.replace(tmp, _path(job["id"]))


def read(jobid: str) -> dict | None:
    try:
        return json.loads(_path(jobid).read_text())
    except (OSError, ValueError):
        return None


def jobs() -> list[dict]:
    out = []
    for p in queue_dir().glob("*.json"):
        job = read(p.stem)
        if job:
            out.append(job)
    return sorted(out, key=lambda j: j["created"])


class Refused(Exception):
    """A request that the queue does not accept, with the reason to show."""


def gerrit_change(number: str) -> dict:
    """The change from the Gerrit REST API, which prefixes its JSON with a line against
    cross-site inclusion."""
    url = f"{GERRIT_URL}/changes/{number}?o=ALL_REVISIONS&o=DETAILED_ACCOUNTS"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            text = r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Refused(f"there is no change {number} on {GERRIT_URL}")
        raise Refused(f"Gerrit answered {e.code} for change {number}")
    except OSError as e:
        raise Refused(f"Gerrit could not be reached: {e}")
    return json.loads(text.split("\n", 1)[1])


def resolve(text: str) -> dict:
    """The patch set that @p text names, checked against the rules of the queue: a change of
    online, not abandoned, whose patch set an account of DOMAINS uploaded."""
    m = CHANGE.fullmatch(text.strip())
    if not m:
        raise Refused("give a change number, number/patch set, or the address of the change")
    number, want = m.group(1), m.group(2)
    change = gerrit_change(number)
    if change.get("project") != "online":
        raise Refused(f"change {number} is in {change.get('project')}, not in online")
    if change.get("status") == "ABANDONED":
        raise Refused(f"change {number} is abandoned")
    revs = change.get("revisions", {})
    pick = None
    for sha, rev in revs.items():
        if (str(rev["_number"]) == want if want else sha == change["current_revision"]):
            pick = (sha, rev)
    if not pick:
        raise Refused(f"change {number} has no patch set {want}")
    sha, rev = pick
    email = ((rev.get("uploader") or {}).get("email") or "").lower()
    if not any(email.endswith("@" + d) for d in DOMAINS):
        raise Refused(f"patch set {rev['_number']} of change {number} was not uploaded from "
                      + " or ".join(DOMAINS))
    return {"change": int(number), "patchset": rev["_number"], "ref": rev["ref"],
            "revision": sha, "subject": change.get("subject", ""),
            "uploader": rev["uploader"].get("name") or email}


def _queue(fields: dict, at: datetime | None) -> dict:
    t = now()
    job = {"id": t.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3), **fields,
           "created": iso(t), "not_before": iso(at) if at else None,
           "state": "queued", "started": None, "ended": None, "exit": None}
    with locked():
        write(job)
    return job


def add(text: str, runs: int, at: datetime | None) -> dict:
    if not isinstance(runs, int) or not 1 <= runs <= MAX_RUNS:
        raise Refused(f"runs is a number from 1 to {MAX_RUNS}")
    patchset = resolve(text)
    return _queue({"branch": f"gerrit-{patchset['change']}", **patchset, "runs": runs}, at)


def add_capacity(target: str, scenario: str, profile: str, scenarios: list[str],
                 at: datetime | None) -> dict:
    """A capacity run of one scenario, "" for copies of blank.odt, of a Gerrit change or of a
    ref of the online repository."""
    if scenario and scenario not in scenarios:
        raise Refused(f"there is no scenario {scenario}")
    if profile not in PROFILES:
        raise Refused("the profile is " + " or ".join(PROFILES))
    target = target.strip()
    fields = {"kind": "capacity", "scenario": scenario, "profile": profile, "runs": 1}
    if CHANGE.fullmatch(target):
        patchset = resolve(target)
        fields.update(branch=f"gerrit-{patchset['change']}", **patchset)
    else:
        if not REF.fullmatch(target) or ".." in target or target.endswith((".lock", "/")):
            raise Refused("give a Gerrit change, or a branch, tag or commit of online")
        fields.update(branch=target.removeprefix("origin/"), ref=target)
    return _queue(fields, at)


def cancel(jobid: str) -> dict:
    with locked():
        job = read(jobid)
        if not job:
            raise Refused(f"there is no job {jobid}")
        if job["state"] != "queued":
            raise Refused(f"job {jobid} is {job['state']}; only a queued job can be cancelled")
        job["state"], job["ended"] = "cancelled", iso(now())
        write(job)
    return job


def claim() -> dict | None:
    """Mark the next due job as running and return it."""
    with locked():
        due = [j for j in jobs() if j["state"] == "queued"
               and (not j["not_before"] or j["not_before"] <= iso(now()))]
        if not due:
            return None
        job = min(due, key=lambda j: (j["not_before"] or j["created"], j["created"]))
        job["state"], job["started"] = "running", iso(now())
        write(job)
        return job


def finish(job: dict, code: int) -> None:
    with locked():
        job["state"] = "done" if code == 0 else "failed"
        job["ended"], job["exit"] = iso(now()), code
        write(job)
        prune()


def recover() -> None:
    """A job still running when the runner starts was cut off by a stop or a reboot."""
    with locked():
        for j in jobs():
            if j["state"] == "running":
                j["state"], j["ended"], j["exit"] = "failed", iso(now()), None
                write(j)


def prune() -> None:
    """Keep the KEEP_DONE newest finished jobs. Called under the lock."""
    finished = [j for j in jobs() if j["state"] in ("done", "failed", "cancelled")]
    for j in sorted(finished, key=lambda j: j["ended"] or j["created"])[:-KEEP_DONE]:
        _path(j["id"]).unlink(missing_ok=True)
        log_path(j["id"]).unlink(missing_ok=True)
