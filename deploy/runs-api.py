#!/usr/bin/env python3
"""The service behind the Runs tab of the dashboard: it deletes runs from the store.

nginx passes /api/ to it (deploy/nginx/cool-perf.conf), and cool-perf-api.service
runs it as the service user, which owns the store. It listens on localhost only.

    POST /api/runs/delete   {"runids": ["20261102T121359Z-e6aa2e06e7ec", ...]}

removes each run folder and its JUnit report, then rebuilds index.html, and
answers {"deleted": [...]}. A run id that is not in the store fails the whole
request, and nothing is deleted.
"""
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

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


@app.post("/api/runs/delete")
def delete_runs():
    # There is no login, so at least refuse a request that another site's page sends:
    # a form cannot post JSON, and a script of another origin carries its Origin.
    if not request.is_json:
        return fail(415, "the request must be JSON")
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        return fail(403, f"requests from {origin} are not accepted")
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


if __name__ == "__main__":
    # Flask's own server: one endpoint, on localhost, used by a few people.
    app.run(host="127.0.0.1", port=PORT, threaded=False)
