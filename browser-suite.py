#!/usr/bin/env python3
"""
browser-suite.py: run the per-application browser tests over time and build a
regression dashboard.

Each run of a test writes a metrics.json (page-load, frame-ready and, in edit
mode, edit times). While a test runs, this script also samples the Collabora
Online server's memory (coolwsd + forkit + the document kits) on the host and
folds the peak into that run's metrics.json, tagged with the COOL commit and a
timestamp. A run captured by the --flamegraphs pass gets its memory from the
capture's own /proc sampling instead, because that pass runs its own Collabora
Online on a free port and the admin endpoint queried here is a different server.
Results accumulate as history under results/.

The dashboard shows one chart per document and user pool, with commits/time on
the x axis: load and edit times on the left axis (ms) and memory on the right
axis (MB), which carries the document's own footprint, the browser heap and the
peak of the coolwsd process that serves the client and caches the static files.
Click a point for that run's detail.

A document-process flamegraph can be captured per test document (--flamegraphs):
for each document k6-flamegraph.py launches its own single-kit coolwsd
(on its own free port, so it coexists with any COOL already running) and profiles
the document kit. Each run's detail embeds its own capture, and can show a second
run beside it to compare commits.

Usage:
    ./browser-suite.py --run                  # run the suite at the current COOL commit
    ./browser-suite.py --flamegraphs           # capture per-document flamegraphs (own COOL each)
    ./browser-suite.py --run --flamegraphs     # both, then rebuild the dashboard
    ./browser-suite.py --run --commit abc123   # label the run's commit explicitly
    ./browser-suite.py                          # rebuild the dashboard from results/
    ./browser-suite.py --demo -o demo.html      # synthetic history, no runs

Running needs docker/k6 (via ./k6-run) and a live COOL + WOPI host; building the
dashboard needs only the standard library. Memory sampling reuses pylib.perfutils.

COOL memory is read from the admin endpoint at COOL_URL (default
https://localhost:9980/), kept separate from WOPI_URL (which the k6 container
needs pointed at a docker/LAN address). Admin creds: COOL_ADMIN (default
admin:admin).
"""
import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS = ("writer", "calc", "impress", "draw")
DOCS = ("blank", "blank-vector", "5mb", "5mb-vector", "10mb", "duplicate", "duplicate-vector",
        "ooxml", "ooxml-vector", "image5mb")

# What each application has to open. "ooxml" is the same content as "5mb" in the Microsoft
# format, so the two read as a comparison of the formats; Draw has no such format of its own,
# so there is no document to open for it and no chart for it either. "10mb" is the document
# one service-level requirement is written about, and "image5mb" opens a blank text document
# and inserts a 5 MB image into it, which is another, so only Writer has those two; "duplicate"
# is the deck the slide-duplication requirement is measured on, so only Impress has that one.
# A "-vector" document is the same test with vector rendering, which draws the slides from
# the primitives the engine sends instead of from tiles, so it reads beside the tile one.
DOCS_BY_APP = {
    "writer": ("blank", "5mb", "10mb", "ooxml", "image5mb"),
    "calc": ("blank", "5mb", "ooxml"),
    "impress": ("blank", "blank-vector", "5mb", "5mb-vector", "duplicate", "duplicate-vector",
                "ooxml", "ooxml-vector"),
    "draw": ("blank", "5mb"),
}


def documents() -> list[tuple[str, str]]:
    """Every (application, document) there is to run, in reading order."""
    return [(app, doc) for app in APPS for doc in DOCS_BY_APP[app]]

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


def _reachable(url: str, timeout: int = 5) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout, context=_SSL).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def preflight(coolwsd_dir: str) -> bool:
    cool = os.environ.get("WOPI_URL", "https://localhost:9980/").rstrip("/")
    wopi = os.environ.get("WOPI_HOST", "https://localhost:3000/").rstrip("/")
    checks = [
        ("Collabora Online (coolwsd)", f"{cool}/hosting/discovery",
         f"cd {coolwsd_dir} && make run"),
        ("WOPI host", f"{wopi}/",
         f"cd {ROOT} && SSL_KEY_FILE=server/npm-server-key.pem "
         "SSL_CRT_FILE=server/npm-server-cert.pem NODE_TLS_REJECT_UNAUTHORIZED=0 npm run server"),
    ]
    ok = True
    for name, url, how in checks:
        if _reachable(url):
            print(f"preflight ok: {name} reachable")
        else:
            ok = False
            print(f"ERROR: {name} not reachable at {url}", file=sys.stderr)
            print(f"       start it with:  {how}", file=sys.stderr)
    return ok


def test_script(app: str, doc: str) -> str:
    return f"dist/cool-browser-{app}-{doc}-test.js"


def git_commit(coolwsd_dir: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", coolwsd_dir, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def cool_metrics() -> dict:
    """Fetch and parse COOL's Prometheus metrics (/cool/getMetrics). Needs admin
    credentials (COOL_ADMIN=user:pass, default admin:admin). {} if unreachable.

    Uses COOL_URL (default https://localhost:9980/), deliberately separate from
    WOPI_URL: the k6 container needs WOPI_URL pointed at a docker/LAN address,
    which the admin query running here on the host cannot always use (that is
    why memory came back empty). The admin endpoint is reachable on localhost."""
    base = os.environ.get("COOL_URL", "https://localhost:9980/").rstrip("/")
    creds = os.environ.get("COOL_ADMIN", "admin:admin")
    req = urllib.request.Request(
        f"{base}/cool/getMetrics",
        headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=5, context=_SSL) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[-1])
            except ValueError:
                pass
    return out


def cool_doc_pids(metrics: dict) -> set[str]:
    """The kit pids of the documents that getMetrics lists, from doc_info{...,pid="P"}."""
    pids = set()
    for key in metrics:
        if key.startswith("doc_info{"):
            m = re.search(r'pid="(\d+)"', key)
            if m:
                pids.add(m.group(1))
    return pids


def cool_doc_views(metrics: dict) -> dict[str, float]:
    """The view count of each kit that getMetrics lists, by pid."""
    return {pid: metrics.get(f'doc_views{{pid="{pid}"}}', 0.0)
            for pid in cool_doc_pids(metrics)}


def doc_private_dirty_kb(pid: str, metrics: dict) -> float:
    """The Private_Dirty of the kit @p pid in kB, read from /proc now. coolwsd's own
    doc_memory_used_bytes is the fallback, for a kit whose /proc cannot be read."""
    from pylib.perfutils import group_mem
    kb = group_mem([pid]).get("private_dirty_kb")
    if kb is not None:
        return float(kb)
    return metrics.get(f'doc_memory_used_bytes{{pid="{pid}"}}', 0.0) / 1024


def run_one(app: str, doc: str, users: int, out_rel: str, commit: str | None) -> None:
    out = ROOT / out_rel
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o777)  # the k6 container user writes metrics.json here
    env = {**os.environ, "USERS": str(users), "OUT_DIR": out_rel, "COMMIT": commit or ""}
    print(f"running {app} {doc} {users}u (commit {commit or 'local'}) ...")
    # The kits of earlier tests can still be listed while this one starts. A
    # kit is this test's when it appears during the test, or when it gains
    # views: the 3-user test joins the kit that the 1-user test of the same
    # document left open.
    earlier_views = cool_doc_views(cool_metrics())
    # The 3-user tests have no capture, so sample coolwsd and the Chromium
    # processes here as the capture does, for every test.
    from pylib.perfutils import MemorySampler, browser_pids, pgrep
    groups: dict = {
        "browser_renderer": lambda: browser_pids()["renderer"],
        "browser_gpu": lambda: browser_pids()["gpu"],
        "browser_other": lambda: browser_pids()["other"],
    }
    # Only when one coolwsd runs as this user, to be sure that it is the one
    # that serves COOL_URL. The group holds its forkit too, as the capture's
    # does, so that the figure is the same one across both kinds of run.
    wsd = pgrep(["-x", "coolwsd", "-u", str(os.getuid())])
    if len(wsd) == 1:
        groups["coolwsd"] = lambda: wsd + pgrep(["-x", "forkit", "-P", wsd[0]])
    # The kits of this test, which the loop below finds through getMetrics.
    test_kits: list[str] = []
    groups["kit"] = lambda: test_kits
    sampler = MemorySampler(groups)

    # Tee what k6 says into the run directory. A scenario that measures nothing used to leave
    # only a metrics.json full of nulls, which says that it failed and nothing about why.
    log = (out / "k6.log").open("wb")
    proc = subprocess.Popen(["./k6-run", test_script(app, doc)], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Poll while the test runs and keep the peaks: the Private_Dirty of this
    # test's kit (the document's own footprint), and global_memory_used_bytes,
    # the whole COOL total. coolwsd's kit_memory_used_max_bytes is not used for
    # the document: it is the largest kit still listed, often the previous
    # test's, and coolwsd refreshes it only every few seconds, so a short test
    # can end before its own kit has a value there.
    peak_doc = peak_total = 0.0
    reached = False
    while proc.poll() is None:
        # Read whatever k6 has written, so its output reaches both the terminal and the log
        # rather than filling a pipe nobody empties.
        line = proc.stdout.readline() if proc.stdout else b""
        if line:
            sys.stdout.buffer.write(line)
            sys.stdout.flush()
            log.write(line)

        m = cool_metrics()
        if m:
            reached = True
        test_kits[:] = [pid for pid, views in cool_doc_views(m).items()
                        if pid not in earlier_views or views > earlier_views[pid]]
        doc_kb = sum(doc_private_dirty_kb(pid, m) for pid in test_kits)
        peak_doc = max(peak_doc, doc_kb * 1024)
        peak_total = max(peak_total, m.get("global_memory_used_bytes", 0.0))
        sampler.sample()
        time.sleep(1)
    if proc.stdout:
        rest = proc.stdout.read()
        if rest:
            sys.stdout.buffer.write(rest)
            log.write(rest)
        proc.stdout.close()
    log.close()

    proc.wait()
    if not reached:
        print(f"WARNING: could not read COOL metrics while {app} {doc} {users}u "
              f"ran, so no memory is recorded. Check COOL_URL="
              f"{os.environ.get('COOL_URL', 'https://localhost:9980/')} and "
              f"COOL_ADMIN (default admin:admin).", file=sys.stderr)
    mj = out / "metrics.json"
    mem = sampler.summary()
    wsd_mem = mem.get("coolwsd", {})
    browser = browser_memory_of(mem)
    if mj.exists() and (peak_doc or peak_total or wsd_mem or browser):
        try:
            d = json.loads(mj.read_text())
            cool = {
                "peak_doc_mem_mb": round(peak_doc / 1048576, 1) if peak_doc else None,
                "peak_total_mem_mb": round(peak_total / 1048576, 1) if peak_total else None,
            }
            # This coolwsd served the earlier tests too, so its VmHWM can come
            # from one of them. The peak resident size during this test is this
            # test's.
            if wsd_mem.get("peak_rss_mb") is not None:
                cool["peak_master_rss_mb"] = wsd_mem["peak_rss_mb"]
            if wsd_mem.get("peak_pss_mb") is not None:
                cool["peak_master_pss_mb"] = wsd_mem["peak_pss_mb"]
            cool.update(kit_memory_of(mem.get("kit", {})))
            d["cool_memory"] = cool
            if browser:
                d["browser_memory"] = browser
            # metrics.json belongs to the k6 container's user, so opening it
            # for writing here fails: write a file of our own and rename it
            # over, which the run directory's own permissions allow.
            tmp = mj.with_name(mj.name + ".tmp")
            tmp.write_text(json.dumps(d, indent=2))
            os.replace(tmp, mj)
        except Exception as exc:
            print(f"could not add memory to {mj}: {exc}", file=sys.stderr)


def live_cool_ports(lo: int = 9980, hi: int = 9999) -> list[int]:
    """Ports in [lo, hi] answering /hosting/discovery. Each document of the
    --flamegraphs pass launches its own coolwsd on the next free port and stops
    it again, so more than one instance up here means earlier ones leaked: they
    hold their port and a kit's memory, and the capture then shares the machine
    with them."""
    return [p for p in range(lo, hi + 1)
            if _reachable(f"https://localhost:{p}/hosting/discovery", timeout=2)]


def capture_samples(out_abs: Path) -> int | None:
    """How many perf samples went into a capture, from the harness metrics.json.

    A capture can succeed and still be worthless: if the kit was idle during the
    window, the flamegraph is a handful of samples wide and says nothing. This is
    the number to gate on (see --min-samples)."""
    try:
        return int(json.loads((out_abs / "metrics.json").read_text())
                   .get("flamegraph_samples"))
    except Exception:
        return None


def capture_flamegraphs(results_dir: Path, runid: str, coolwsd_dir: str,
                        commit: str | None = None,
                        prune_perf_data: bool = False,
                        min_samples: int = 0) -> list[dict]:
    """Capture one document-process flamegraph per test document via
    k6-flamegraph.py. For each document it launches its own single-kit coolwsd
    (make run-one, on its own free port via --new-instance, so it coexists with
    any coolwsd already on the default port), opens that document with its
    browser test so the kitbroker_* document kit loads it, and profiles that
    kit. Output: results/<runid>/flamegraph/<app>-<doc>/flamegraph.svg, and the
    browser test's own metrics.json one level down in browser/, which is what
    puts this run on the dashboard as a profiled point.

    Returns one {name, ok, rc, out_rel, log} record per document, for a caller
    that has to report per-document outcomes (see --junit)."""
    env = {**os.environ}
    # On this hybrid P/E-core CPU 'cycles' opens but captures almost nothing for
    # perf record against the kit; cpu_core/cycles/ pins the P-core PMU.
    env.setdefault("PERF_EVENT", "cpu_core/cycles/")
    # A few edits keep the kit busy during the perf window so even a blank
    # document yields more than a handful of samples.
    env.setdefault("EDIT", "1")
    live = live_cool_ports()
    if len(live) > 1:
        print(f"WARNING: {len(live)} coolwsd instances already answer on ports "
              f"{', '.join(str(p) for p in live)}. Leftovers from an earlier "
              "pass compete with the profiled kit for this machine. Stop them "
              "with `pkill -f coolwsd` (and restart your own if you had one) "
              "before trusting these flamegraphs.", file=sys.stderr)
    failed = []
    outcomes = []
    for app, doc in documents():
            out_rel = (results_dir / runid / "flamegraph" / f"{app}-{doc}").as_posix()
            print(f"flamegraph {app}-{doc}: own single-kit cool -> {out_rel} ...")
            # --cgroup counts and records the instance by cgroup from before
            # the document opens. pid mode attaches only once the kit is
            # found, so its counts miss a varying part of the load.
            cmd = [sys.executable, "k6-flamegraph.py", test_script(app, doc),
                   "--out-dir", out_rel, "--coolwsd-dir", coolwsd_dir,
                   "--new-instance", "--cgroup"]
            # Tee the harness output into the run directory. Without this a
            # failed document left only coolwsd.log behind, which says nothing
            # about why the harness itself gave up.
            out_abs = ROOT / out_rel
            out_abs.mkdir(parents=True, exist_ok=True)
            # Give the browser test its own OUT_DIR one level down. It writes a
            # metrics.json of the same name as the harness summary that lands
            # beside the SVG, so they cannot share a directory, and without an
            # OUT_DIR of its own the test used to overwrite results/metrics.json
            # on every document, leaving the capture with no run to attach to.
            # --out-dir above still decides where the harness itself writes, so
            # OUT_DIR here only reaches the test through k6-run.
            browser_rel = f"{out_rel}/browser"
            browser_abs = ROOT / browser_rel
            browser_abs.mkdir(parents=True, exist_ok=True)
            browser_abs.chmod(0o777)  # the k6 container user writes metrics.json here
            env["OUT_DIR"] = browser_rel
            env["USERS"] = "1"
            env["COMMIT"] = commit or ""
            with (out_abs / "harness.log").open("w") as log:
                proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, text=True,
                                        bufsize=1, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT)
                for line in proc.stdout:
                    sys.stdout.write(line)
                    log.write(line)
                rc = proc.wait()
            svg = out_abs / "flamegraph.svg"
            samples = capture_samples(out_abs)
            thin = (min_samples > 0 and samples is not None
                    and samples < min_samples)
            ok = rc == 0 and svg.is_file() and not thin
            if samples is not None:
                print(f"  {samples} perf samples")
            if not ok:
                failed.append(f"{app}-{doc}")
                if rc != 0:
                    why = f"exit {rc}"
                elif not svg.is_file():
                    why = "no flamegraph.svg was written"
                else:
                    why = f"only {samples} perf samples, under --min-samples {min_samples}"
                print(f"  flamegraph {app}-{doc} failed ({why}), see "
                      f"{out_rel}/harness.log", file=sys.stderr)
            elif prune_perf_data:
                # perf.data is by far the biggest thing a capture leaves (tens of
                # MB per document) and the SVG is what gets read afterwards, so a
                # durable history keeps the graph and drops the raw samples.
                pd = out_abs / "perf.data"
                if pd.is_file():
                    freed = pd.stat().st_size
                    pd.unlink()
                    print(f"  pruned perf.data ({freed // 1048576} MB)")
            if prune_perf_data:
                # The binaries that perf copied for its symbols, a GB or more once
                # the engine is in, which no graph needs once it is drawn. A failed
                # capture keeps its perf.data for a look, and loses these too.
                shutil.rmtree(out_abs / "buildid", ignore_errors=True)
            outcomes.append({"name": f"{app}-{doc}", "ok": ok, "rc": rc,
                             "out_rel": out_rel,
                             "samples": samples,
                             "why": None if ok else why,
                             "log": f"{out_rel}/harness.log"})
    done = len(documents()) - len(failed)
    print(f"flamegraphs: {done}/{len(documents())} captured"
          + (f", failed: {', '.join(failed)}" if failed else ""))
    return outcomes


def new_runid(commit: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{commit or 'local'}"


def run_suite(results_dir: Path, runid: str, users_list: list[int],
              commit: str | None) -> list[dict]:
    """Run every document at every user count. Returns one {name, ok, out_rel}
    record per run, a run counting as ok when it measured the document opening."""
    outcomes = []
    for app, doc in documents():
            for users in users_list:
                rel = (results_dir / runid / f"{app}-{doc}-{users}u").as_posix()
                run_one(app, doc, users, rel, commit)
                ok, why = measured(ROOT / rel)
                if ok:
                    ok, why = met_requirement(ROOT / rel)
                if not ok:
                    print(f"  {app}-{doc} {users}u {why}", file=sys.stderr)
                outcomes.append({"name": f"{app}-{doc}-{users}u", "ok": ok,
                                 "why": why, "out_rel": rel, "log": rel})
    return outcomes


def measured(out: Path) -> tuple[bool, str | None]:
    """Whether a run measured the document opening, and what is missing where it did not.

    A file of nulls is what a scenario leaves behind when the document never reached
    Frame_Ready: written, well-formed and empty. Counting that as a pass reports a run where
    nothing was measured as a green one, which is worse than reporting nothing at all.
    """
    mj = out / "metrics.json"
    if not mj.is_file():
        return False, "wrote no metrics.json"

    try:
        metrics = json.loads(mj.read_text()).get("metrics") or {}
    except Exception as exc:
        return False, f"wrote a metrics.json that cannot be read: {exc}"

    # document_loaded_ms is the document appearing, which is what every one of these tests is
    # for. Runs recorded before that metric existed carry frame_ready_ms alone.
    name = "document_loaded_ms" if "document_loaded_ms" in metrics else "frame_ready_ms"
    ready = metrics.get(name)
    if isinstance(ready, dict):
        ready = ready.get("avg")

    if ready is None:
        return False, (f"measured no {name}: the document never appeared. "
                       "See k6.log beside its metrics.json")

    return True, None


def met_requirement(out: Path) -> tuple[bool, str | None]:
    """Whether a run met the service level its test declares, and how it missed it.

    Only the tests that carry a requirement write one (see
    src/cool-browser-writer-10mb-test.js); for every other run this passes. The
    verdict itself is reached in the test, so k6 and the suite cannot disagree
    about it."""
    try:
        req = json.loads((out / "metrics.json").read_text()).get("requirement")
    except Exception:
        return True, None
    if not isinstance(req, dict):
        return True, None

    # The sample count is read before the verdict, not after it. A run that
    # recorded nothing is not a run that was fast, and an empty k6 trend reports
    # zero for every statistic, which any budget accepts. Keep the two apart in
    # a CI log: a budget missed is a regression, a budget never measured is a
    # broken scenario, or a dist/ built before the counters existed.
    if not req.get("samples"):
        return False, (
            f"recorded no {req.get('metric')} at all, so its "
            f"{req.get('max_ms')} ms budget was never tested. A scenario that "
            f"ended early does this, and so does a stale dist/ "
            f"(npm run build)")
    if req.get("ok"):
        return True, None

    value = req.get("value")
    return False, (
        f"missed its requirement: {req.get('metric')} "
        f"p({req.get('percentile')}) was "
        f"{'not measured' if value is None else str(round(value)) + ' ms'}, "
        f"budget {req.get('max_ms')} ms, over {req.get('samples')} open(s)")


def write_junit(path: Path, suite_name: str, cases: list[dict]) -> None:
    """A JUnit report of the run, for a CI server to show per-document results.

    Deliberately hand-rolled: this repo's tooling runs on the standard library
    only, and the schema needed here is a flat list of testcases."""
    def esc(v: str) -> str:
        return (str(v).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    failures = sum(1 for c in cases if not c["ok"])
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuite name="{esc(suite_name)}" tests="{len(cases)}" '
           f'failures="{failures}">']
    for c in cases:
        out.append(f'  <testcase classname="{esc(suite_name)}" '
                   f'name="{esc(c["name"])}">')
        if not c["ok"]:
            detail = c.get("log") or c.get("out_rel") or ""
            out.append(f'    <failure message="{esc(c.get("why") or "run failed")}">'
                       f'{esc(detail)}</failure>')
        out.append('  </testcase>')
    out.append('</testsuite>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    print(f"wrote {path}  ({len(cases)} case(s), {failures} failure(s))")


_FILE_ID_RE = re.compile(r"FILE_ID\s*=\s*Number\(__ENV\['FILE_ID'\]\s*\|\|\s*(\d+)\)")


def document_files() -> dict:
    """{'app|doc': 'documents/blank.odt'}, the document each test opens.

    Read from the test source's default FILE_ID and the WOPI host's file table,
    which is where that mapping actually lives. Doing it here rather than
    recording it per run means runs captured before this existed also show their
    document. A run's own metrics.json wins when it carries a file of its own,
    which is what a FILE_ID override would produce."""
    try:
        files = json.loads((ROOT / "server" / "routes" / "files.json").read_text())
    except Exception:
        return {}
    out = {}
    for app, doc in documents():
            try:
                text = (ROOT / "src" / f"cool-browser-{app}-{doc}-test.js").read_text()
            except OSError:
                continue
            m = _FILE_ID_RE.search(text)
            if not m:
                continue
            path = (files.get(m.group(1)) or {}).get("path")
            if path:
                out[f"{app}|{doc}"] = path
    return out


def _runid_of(mj: Path, results_dir: Path) -> str | None:
    """The results/<runid>/... directory a metrics.json belongs to, if any."""
    try:
        parts = mj.relative_to(results_dir).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 else None


def _capture_instructions(mj: Path) -> dict | None:
    """Instructions the two Collabora Online processes executed during the
    capture, from the capture's own harness summary, or None without one.

    Two figures: the master, which serves the client, and the document kit,
    which does the document work. They cover the window perf was attached for,
    which opens once the server is already up, so they are a picture of the
    test itself and not of a whole process life."""
    try:
        d = json.loads((mj.parent.parent / "metrics.json").read_text())
    except (OSError, ValueError):
        return None
    perf = d.get("perf")
    if not isinstance(perf, dict):
        return None
    out = {}
    for key, field in (("coolwsd", "master"), ("kit", "kit")):
        entry = perf.get(key)
        if isinstance(entry, dict) and entry.get("instructions"):
            out[field] = entry["instructions"]
    return out or None


def kit_memory_of(kit: dict) -> dict:
    """The cool_memory figures of the document kit, from the MemorySampler
    summary entry @p kit."""
    out: dict = {}
    # How the kit's resident memory is made up: the engine libraries it maps,
    # other mapped files, what the fork handed it, and what the document itself
    # went on to write. The four add up to the resident total.
    lib = kit.get("peak_lib_rss_mb")
    other = kit.get("peak_other_rss_mb")
    anon = kit.get("peak_anon_rss_mb")
    inherited = kit.get("peak_anon_shared_dirty_mb")
    private = kit.get("peak_private_dirty_mb")
    shared = kit.get("peak_shared_dirty_mb")
    # Resident totals and how many processes were in each group, so the
    # proportional figures can be read for what they are.
    if kit.get("peak_rss_mb") is not None:
        out["peak_doc_rss_mb"] = kit["peak_rss_mb"]
    if kit.get("max_pids") is not None:
        out["doc_pids"] = kit["max_pids"]
    if None not in (lib, other, anon, inherited):
        out["doc_stack"] = {"libraries": lib, "other_files": other,
                            "inherited": inherited,
                            "own": round(anon - inherited, 1)}
    # What the document costs on its own, and the base it inherited from the
    # process it was forked from, which every other document shares.
    if private is not None:
        out["peak_doc_private_mb"] = private
    if shared is not None:
        out["peak_doc_shared_mb"] = shared
    return out


def _capture_memory(mj: Path) -> dict | None:
    """This run's Collabora Online memory, read from the capture's own harness
    summary, or None when the run has no capture beside it.

    A run of the --flamegraphs pass launches its own Collabora Online on a free
    port, so the admin endpoint that fills cool_memory during a plain run is not
    the server under test and nothing is recorded there. The capture does sample
    memory, from /proc, one level up in the harness metrics.json, so the figures
    exist and only need reading. Doing it here rather than at capture time means
    runs recorded before this existed also show their memory.

    The document figure is the kitbroker group, which is the process doing the
    document work, and proportional set size is preferred over resident size
    because several kits share the same mapped engine libraries. The total is the
    largest sum over the sample times rather than a sum of the two peaks, which
    would count moments that never happened together."""
    try:
        d = json.loads((mj.parent.parent / "metrics.json").read_text())
    except (OSError, ValueError):
        return None
    mem = d.get("memory")
    if not isinstance(mem, dict):
        return None

    def peak_of(label: str) -> float | None:
        entry = mem.get(label)
        if not isinstance(entry, dict):
            return None
        return entry.get("peak_pss_mb", entry.get("peak_rss_mb"))

    def series_of(label: str) -> list[dict]:
        entry = mem.get(label)
        return entry.get("samples", []) if isinstance(entry, dict) else []

    doc_peak = peak_of("kit")
    kit = mem.get("kit") if isinstance(mem.get("kit"), dict) else {}
    wsd = mem.get("coolwsd") if isinstance(mem.get("coolwsd"), dict) else {}
    master_peak = wsd.get("peak_rss_hwm_mb", wsd.get("peak_rss_mb"))
    totals = []
    wsd_series = series_of("coolwsd")
    for i, kit_sample in enumerate(series_of("kit")):
        at = wsd_series[i] if i < len(wsd_series) else {}
        both = [s.get("pss_mb", s.get("rss_mb", 0.0)) for s in (kit_sample, at)]
        totals.append(sum(both))
    total_peak = round(max(totals), 1) if totals else None
    if doc_peak is None and total_peak is None:
        return None
    out = {"peak_doc_mem_mb": doc_peak, "peak_total_mem_mb": total_peak,
           "source": "capture"}
    out.update(kit_memory_of(kit))
    if wsd.get("peak_rss_mb") is not None:
        out["peak_master_rss_mb"] = wsd["peak_rss_mb"]
    if wsd.get("peak_pss_mb") is not None:
        out["peak_master_pss_mb"] = wsd["peak_pss_mb"]
    # The high-water mark of the process that serves the client and holds the
    # static file cache, which no other figure here covers.
    if master_peak is not None:
        out["peak_master_hwm_mb"] = master_peak
    return out


def _capture_browser_memory(mj: Path) -> dict | None:
    """The memory of the Chromium processes during this run's capture, or None.

    browser_heap_mb is the JavaScript heap of the page alone. The renderer
    process also holds the DOM, the decoded images and the canvas pixels of the
    tiles, and the GPU process holds the compositor's buffers. The capture samples
    these processes from /proc. They belong to the k6 container's user, so only
    /proc/<pid>/status can be read: the resident size, its anonymous, file and
    shared memory parts, and the high-water mark. Shared memory is counted in each
    process that maps it, so the sum over processes is an upper bound."""
    try:
        d = json.loads((mj.parent.parent / "metrics.json").read_text())
    except (OSError, ValueError):
        return None
    mem = d.get("memory")
    if not isinstance(mem, dict):
        return None
    return browser_memory_of(mem)


def browser_memory_of(mem: dict) -> dict | None:
    """The browser_memory of a run, from the MemorySampler summary @p mem."""
    out: dict = {}
    for label, key in (("browser_renderer", "renderer"), ("browser_gpu", "gpu"),
                       ("browser_other", "other")):
        entry = mem.get(label)
        if not isinstance(entry, dict):
            continue
        fields = {"peak_rss_mb": "rss", "peak_rss_hwm_mb": "hwm",
                  "peak_rss_anon_mb": "anon", "peak_rss_shmem_mb": "shmem",
                  "peak_pss_mb": "pss", "max_pids": "pids"}
        out[key] = {name: entry[f] for f, name in fields.items() if entry.get(f) is not None}
    if not out:
        return None
    # The largest sum over the sample times, not the sum of the peaks, which
    # would count moments that never happened together.
    by_time: dict = {}
    for label in ("browser_renderer", "browser_gpu", "browser_other"):
        entry = mem.get(label)
        for sample in entry.get("samples", []) if isinstance(entry, dict) else []:
            by_time[sample["t_s"]] = by_time.get(sample["t_s"], 0.0) + sample.get("rss_mb", 0.0)
    if by_time:
        out["peak_total_rss_mb"] = round(max(by_time.values()), 1)
    return out


def _flamegraph_for(mj: Path, results_dir: Path,
                    app: str, doc: str) -> tuple[str | None, bool]:
    """(absolute path to this run's flamegraph or None, was this run profiled).

    Scoped to the run on purpose: a flamegraph is only evidence about the commit
    it was captured on, so it must not be shared across runs the way it used to
    be (the newest capture was pinned to every point of that document, which read
    as if every commit had been profiled). Two shapes match. A run of the
    --flamegraphs pass sits in browser/ under the directory holding its own SVG,
    and it is profiled: perf was attached while it ran, so its times are not
    comparable with a plain run. A plain suite run picks up the capture taken for
    the same document in the same runid."""
    here = mj.parent.parent / "flamegraph.svg"
    if mj.parent.name == "browser" and here.is_file():
        return str(here.resolve()), True
    runid = _runid_of(mj, results_dir)
    if runid:
        same_run = results_dir / runid / "flamegraph" / f"{app}-{doc}" / "flamegraph.svg"
        if same_run.is_file():
            return str(same_run.resolve()), False
    return None, False


def collect(results_dir: Path) -> dict:
    """{'app|doc|users': [ {commit, ts, runid, metrics, cool_memory, flamegraph},
    ... ]} over time."""
    scen: dict = {}
    files = document_files()
    for mj in sorted(results_dir.glob("**/metrics.json")):
        try:
            d = json.loads(mj.read_text())
        except Exception:
            continue
        app, doc = d.get("app"), d.get("doc")
        if not (app and doc):
            continue
        users = int(d.get("users", 1))
        ts = d.get("ts") or datetime.fromtimestamp(mj.stat().st_mtime,
                                                   timezone.utc).isoformat()
        # k6 reports a trend with no samples as all zeros, and its count of 0 is the only
        # sign of it, so such a trend is left out like a metric that was never written.
        metrics = {k: v for k, v in d.get("metrics", {}).items()
                   if not (isinstance(v, dict) and v.get("count") == 0)}
        # Absolute path here, write_dashboard makes it relative to the html.
        flame, profiled = _flamegraph_for(mj, results_dir, app, doc)
        scen.setdefault(f"{app}|{doc}|{users}", []).append({
            "commit": d.get("commit"), "ts": ts,
            "runid": _runid_of(mj, results_dir),
            "metrics": metrics,
            "cool_memory": d.get("cool_memory") or _capture_memory(mj),
            "browser_memory": d.get("browser_memory") or _capture_browser_memory(mj),
            "instructions": d.get("instructions") or _capture_instructions(mj),
            "flamegraph": flame, "profiled": profiled,
            "machine": _machine_of(mj, results_dir),
            "branch": _build_of(mj, results_dir).get("branch") or "main",
            "ref": _build_of(mj, results_dir).get("ref"),
            "base": _build_of(mj, results_dir).get("base"),
            "file": d.get("file") or files.get(f"{app}|{doc}"),
        })
    for key, pts in scen.items():
        scen[key] = _merge_profiled(pts)
        scen[key].sort(key=lambda p: p["ts"])
    return scen


def _build_of(mj: Path, results_dir: Path) -> dict:
    """The build.json of the run that @p mj belongs to: the branch that cool-ci measured
    it for, and the ref it was built from. A run from before build.json is of main."""
    runid = _runid_of(mj, results_dir)
    try:
        return json.loads((results_dir / runid / "build.json").read_text()) if runid else {}
    except (OSError, ValueError):
        return {}


def _machine_of(mj: Path, results_dir: Path) -> dict | None:
    """The machine.json of the run that @p mj belongs to: the probe times and the steal time
    during the run, and whether the machine was busy."""
    runid = _runid_of(mj, results_dir)
    if not runid:
        return None
    try:
        return json.loads((results_dir / runid / "machine.json").read_text())
    except (OSError, ValueError):
        return None


def _merge_profiled(pts: list[dict]) -> list[dict]:
    """Fold each profiled point into the plain point of the same run.

    The plain point keeps its own times on the chart. The times measured under
    perf move to profiled_metrics, for the run detail. Instructions come from
    the capture, and its memory figures fill in those the plain run lacks. A
    profiled point with no plain point in its run stays on the chart as it is."""
    plain = {p["runid"]: p for p in pts if p["runid"] and not p["profiled"]}
    out = []
    for p in pts:
        host = plain.get(p["runid"]) if p["profiled"] else None
        if host is None:
            out.append(p)
            continue
        host["profiled_metrics"] = p["metrics"]
        host["instructions"] = host["instructions"] or p["instructions"]
        # getMetrics gives the plain run its document and total memory only.
        # The capture adds the coolwsd peak, the resident sizes and the kit
        # stack, so merge the two, and keep the plain figure for the same key.
        if p["cool_memory"]:
            host["cool_memory"] = {**p["cool_memory"], **(host["cool_memory"] or {})}
        host["browser_memory"] = host["browser_memory"] or p["browser_memory"]
        host["flamegraph"] = host["flamegraph"] or p["flamegraph"]
    return out


def demo_history() -> dict:
    import random
    rng = random.Random(11)
    commits = [f"{c:07x}" for c in (rng.randrange(1 << 28) for _ in range(6))]
    scen: dict = {}
    for app, doc in documents():
            ai = APPS.index(app)
            base_doc = doc.removesuffix("-vector")
            big = {"5mb": 2.3, "ooxml": 2.3, "10mb": 3.6}.get(base_doc, 1.0)
            for users in (1, 3):
                base_dl = (900 + ai * 350) * big * (1.6 if users == 3 else 1.0)
                # the client shell, near enough the same whatever the document
                base_fr = 1200 * (1.4 if users == 3 else 1.0)
                base_mem = (600 + ai * 120) * big * (1 + 0.5 * (users - 1))
                base_bh = (55 + ai * 12) * big  # browser JS heap, MB
                pts = []
                for i, commit in enumerate(commits):
                    drift = 1 + 0.03 * i + (0.35 if i == 4 else 0)  # a regression at commit 5
                    j = lambda: 1 + rng.uniform(-.04, .04)
                    m = {"page_load_ms": _stat((110 + rng.uniform(-15, 25))),
                         "document_loaded_ms": _stat(base_dl * drift * j()),
                         "frame_ready_ms": _stat(base_fr * j()),
                         "browser_heap_mb": _stat(base_bh * drift * j())}
                    if users > 1:
                        m["edit_ms"] = _stat(190 * big * drift * j())
                    if doc == "image5mb":
                        m["image_displayed_ms"] = _stat(1400 * drift * j())
                    if base_doc == "duplicate":
                        m["slide_duplicated_ms"] = _stat(120 * users * drift * j())
                    pts.append({
                        "commit": commit,
                        "ts": f"2026-08-{8 + i:02d}T09:00:00+00:00",
                        "metrics": m,
                        "cool_memory": {"peak_doc_mem_mb": round(base_mem * drift * j(), 1),
                                        "peak_total_mem_mb": round((base_mem + 350) * drift * j(), 1),
                                        "peak_master_hwm_mb": round((base_mem + 900) * drift * j(), 1),
                                        "doc_stack": {"libraries": round(200 * j(), 1),
                                                      "other_files": round(16 * j(), 1),
                                                      "inherited": round(230 * j(), 1),
                                                      "own": round(base_mem * 0.2 * drift * j(), 1)}},
                    })
                scen[f"{app}|{doc}|{users}"] = pts
    return scen


def _stat(avg: float) -> dict:
    return {"avg": round(avg, 1), "min": round(avg * .8, 1), "max": round(avg * 1.4, 1),
            "med": round(avg * .95, 1), "p90": round(avg * 1.25, 1), "p95": round(avg * 1.35, 1)}


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Collabora Online browser-test dashboard</title>
<style>
  :root{ --surface:#fcfcfb; --panel:#fff; --ink:#0b0b0b; --ink-soft:#52514e;
    --ink-faint:#8a8880; --grid:#e6e6e2; --hair:#dcdbd5; --accent:#2a78d6;
    --s0:#2a78d6; --s1:#eb6834; --s2:#1baf7a; --s3:#5a6b7a; --s4:#d11a45; --s5:#0e8aa8; --s6:#a3199b; --s7:#b5651d; --mem:#7a4fc0; --bmem:#d64f9a; --tmem:#8e2f63; --wmem:#b8860b; --st-lib:#9ec9c4; --st-other:#cfd3a8; --st-inh:#f0c48a; --st-own:#e8896f;
    --app-writer:#2a6cc9; --app-calc:#1a9e4b; --app-impress:#e8710a; --app-draw:#e0a80f;
    --shadow:0 1px 2px rgba(11,11,11,.06),0 8px 24px rgba(11,11,11,.05); }
  @media (prefers-color-scheme:dark){:root:not([data-theme=light]){
    --surface:#161615; --panel:#1f1f1d; --ink:#f4f3ee; --ink-soft:#c3c2b7;
    --ink-faint:#8f8e84; --grid:#33332f; --hair:#3a3a35; --accent:#3987e5;
    --s0:#3987e5; --s1:#d95926; --s2:#199e70; --s3:#8ba0b3; --s4:#f2506f; --s5:#2bb3d6; --s6:#e36fd6; --s7:#d9893a; --mem:#9f7be6; --bmem:#e07bb0; --tmem:#f0a6cf; --wmem:#d8a72a; --st-lib:#3f6f69; --st-other:#6b7040; --st-inh:#9c6a2a; --st-own:#a5442c;
    --app-writer:#5a9bea; --app-calc:#35c46f; --app-impress:#ff9440; --app-draw:#f2c94c;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35); }}
  :root[data-theme=dark]{ --surface:#161615; --panel:#1f1f1d; --ink:#f4f3ee;
    --ink-soft:#c3c2b7; --ink-faint:#8f8e84; --grid:#33332f; --hair:#3a3a35;
    --accent:#3987e5; --s0:#3987e5; --s1:#d95926; --s2:#199e70; --s3:#8ba0b3; --s4:#f2506f; --s5:#2bb3d6; --s6:#e36fd6; --s7:#d9893a; --mem:#9f7be6; --wmem:#d8a72a;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35); }
  body{margin:0;background:var(--surface);color:var(--ink);line-height:1.5;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:none;margin:0 auto;padding:34px clamp(20px,3.5vw,48px) 64px;}
  h1{font-size:22px;font-weight:650;letter-spacing:-.01em;margin:0 0 4px;}
  .sub{color:var(--ink-soft);font-size:14px;margin:0 0 8px;}
  .legend{display:flex;gap:14px 18px;flex-wrap:wrap;align-items:center;font-size:12.5px;
    color:var(--ink-soft);position:sticky;top:0;background:var(--surface);
    padding:10px 0;z-index:5;border-bottom:1px solid var(--hair);}
  .legend .key{display:inline-flex;align-items:center;gap:7px;}
  .legend .sep{width:1px;height:15px;background:var(--hair);}
  .meta a,.fg-inline{color:var(--accent);}
  .lswatch{width:22px;height:8px;}
  /* flex flow: charts wrap to as many per row as the view width allows,
     each between ~420px and ~760px, growing to fill the row */
  #charts{display:flex;flex-wrap:wrap;gap:16px;margin-top:16px;}
  /* basis ~340px so a ~1600px view fits 4 per row; grows to fill up to 520px */
  .chart{flex:1 1 340px;min-width:0;max-width:520px;
    background:var(--panel);border:1px solid var(--hair);
    border-left:4px solid var(--app,var(--hair));border-radius:12px;
    padding:12px 14px 8px;box-shadow:var(--shadow);}
  .chart.a-writer{--app:var(--app-writer);} .chart.a-calc{--app:var(--app-calc);}
  .chart.a-impress{--app:var(--app-impress);} .chart.a-draw{--app:var(--app-draw);}
  .chart h2{font-size:14px;font-weight:650;margin:0 0 6px;text-transform:capitalize;
    display:flex;align-items:center;gap:8px;}
  .chart h2::before{content:"";width:9px;height:9px;border-radius:2px;flex:0 0 auto;
    background:var(--app,var(--ink-faint));}
  svg{width:100%;height:auto;display:block;}
  .fullbtn{margin-left:auto;font:inherit;font-size:11px;font-weight:500;text-transform:none;
    cursor:pointer;border:1px solid var(--hair);border-radius:999px;padding:1px 8px;
    background:var(--surface);color:var(--ink-soft);}
  .fullbtn:hover{color:var(--ink);}
  .chart.full{position:fixed;inset:12px;z-index:40;max-width:none;display:flex;flex-direction:column;}
  .chart.full svg[data-key]{flex:1 1 auto;min-height:0;height:100%;}
  body.has-full::before{content:"";position:fixed;inset:0;z-index:39;background:rgba(11,11,11,.55);}
  .tick line{stroke:var(--grid);stroke-width:1;}
  .tick text,.xlab,.axtitle{fill:var(--ink-faint);font-size:10px;
    font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;}
  .axtitle{font-family:system-ui,sans-serif;}
  .baseline{stroke:var(--hair);stroke-width:1;}
  .tabtick{fill:var(--tmem);font-size:10px;font-family:ui-monospace,Menlo,monospace;
    font-variant-numeric:tabular-nums;}
  .line{fill:none;stroke-width:2;} .line.mem{stroke-dasharray:5 4;}
  .line.document_loaded_ms{stroke:var(--s0);} .line.frame_ready_ms{stroke:var(--s3);}
  .line.page_load_ms{stroke:var(--s1);}
  .line.edit_ms{stroke:var(--s2);} .line.cool_mem{stroke:var(--mem);} .line.browser_mem{stroke:var(--bmem);}
  .line.first_content_ms{stroke:var(--s4);} .line.js_ready_ms{stroke:var(--s5);}
  .line.image_displayed_ms{stroke:var(--s6);}
  .line.slide_duplicated_ms{stroke:var(--s7);}
  .line.master_mem{stroke:var(--wmem);stroke-dasharray:2 3;}
  .line.tab_mem{stroke:var(--tmem);stroke-dasharray:8 3;}
  .line.instr_kit{stroke:var(--ink);stroke-width:3;} .dot.instr_kit{fill:var(--ink);}
  .line.instr_wsd{stroke:var(--ink-soft);stroke-width:2.5;stroke-dasharray:6 3;} .dot.instr_wsd{fill:var(--ink-soft);}
  /* the buttons that show or hide a group of lines on every chart */
  .views{display:inline-flex;gap:6px;align-items:center;}
  .vt{font:inherit;font-size:12px;cursor:pointer;border:1px solid var(--hair);border-radius:999px;
    padding:2px 10px;background:var(--surface);color:var(--ink-faint);text-decoration:line-through;}
  .vt[aria-pressed=true]{background:var(--panel);color:var(--ink);border-color:var(--ink-soft);text-decoration:none;}
  .legend .key.off{opacity:.35;}
  /* the comparison of two builds: green when B is lower, red when higher, lighter when small */
  :root{ --better:#1a9e4b; --worse:#d0312d; }
  @media (prefers-color-scheme:dark){:root:not([data-theme=light]){ --better:#35c46f; --worse:#f2506f; }}
  :root[data-theme=dark]{ --better:#35c46f; --worse:#f2506f; }
  /* the two views of the page */
  .tabs{display:flex;gap:4px;border-bottom:1px solid var(--hair);margin:12px 0 16px;}
  .tabs[hidden],[role=tabpanel][hidden]{display:none;}
  .tabs [role=tab]{font:inherit;font-size:13.5px;cursor:pointer;background:none;border:0;
    border-bottom:2px solid transparent;color:var(--ink-soft);padding:6px 12px;margin-bottom:-1px;}
  .tabs [role=tab][aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600;}
  .tabs [role=tab]:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:4px;}
  .cmp{background:var(--panel);border:1px solid var(--hair);border-radius:12px;padding:14px 16px;margin:14px 0 18px;}
  .cmp h2{font-size:15px;margin:0 0 10px;}
  .runs-bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0;}
  .runs-bar button{font:inherit;font-size:13px;cursor:pointer;padding:4px 12px;border-radius:7px;
    border:1px solid var(--worse);background:var(--surface);color:var(--ink);}
  .runs-bar button:disabled{cursor:default;opacity:.5;border-color:var(--hair);}
  .runs-bar [role=status]{font-size:12.5px;color:var(--ink-soft);}
  .runs-bar [role=status].err{color:var(--worse);}
  .runs table{width:auto;min-width:560px;} .runs td{white-space:nowrap;}
  .runs td.l,.runs th{text-align:left;font-family:system-ui,sans-serif;}
  .runs .ref{display:block;font-size:11.5px;color:var(--ink-faint);}
  .runs tr.sel td{background:color-mix(in srgb,var(--worse) 12%,transparent);}
  .cmp-pick{display:grid;gap:8px;font-size:12.5px;}
  .cmp-pick .side{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;}
  .cmp-pick .side b{width:1.2em;}
  .cmp-pick label{display:inline-flex;gap:6px;align-items:center;min-width:0;max-width:100%;} .cmp-pick .all{color:var(--ink-soft);}
  .cmp-pick select{font:inherit;font-size:12px;padding:2px 6px;border-radius:7px;max-width:min(360px,100%);min-width:0;
    border:1px solid var(--hair);background:var(--surface);color:var(--ink);}
  @media (max-width:600px){
    .cmp-pick .side{display:grid;grid-template-columns:1.4em minmax(0,1fr);}
    .cmp-pick .side label{grid-column:2;} .cmp-pick select{width:100%;max-width:100%;}
    .cmp-pick .side label select{flex:1;width:auto;}}
  .cmp-note{font-size:12.5px;color:var(--ink-soft);margin:10px 0;}
  .cmp-scroll{overflow-x:auto;}
  .cmp table{width:auto;min-width:560px;} .cmp td{white-space:nowrap;}
  .cmp tbody th{text-align:left;vertical-align:top;font-weight:600;font-family:system-ui,sans-serif;
    padding:6px 12px;border-bottom:1px solid var(--hair);white-space:nowrap;}
  .cmp td.m{text-align:left;font-family:system-ui,sans-serif;} .cmp td.d{font-weight:600;}
  .cmp td.better{background:color-mix(in srgb,var(--better) var(--k),transparent);}
  .cmp td.worse{background:color-mix(in srgb,var(--worse) var(--k),transparent);}
  .cmp td.same{color:var(--ink-faint);}
  /* the branch that the charts show */
  .branches{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap;}
  .branches select{font:inherit;font-size:12px;padding:2px 6px;border-radius:7px;
    border:1px solid var(--hair);background:var(--surface);color:var(--ink);}
  /* display:inline-flex would show them through the hidden attribute */
  .branches[hidden]{display:none;}
  .sbar{stroke:none;fill-opacity:.85;} .sbar.lib{fill:var(--st-lib);} .sbar.other{fill:var(--st-other);}
  .sbar.inh{fill:var(--st-inh);} .sbar.own{fill:var(--st-own);}
  .dot{stroke:var(--panel);stroke-width:1.3;cursor:pointer;transition:r .12s ease;}
  .dot.document_loaded_ms{fill:var(--s0);} .dot.frame_ready_ms{fill:var(--s3);}
  .dot.page_load_ms{fill:var(--s1);}
  .dot.edit_ms{fill:var(--s2);} .dot.cool_mem{fill:var(--mem);} .dot.browser_mem{fill:var(--bmem);}
  .dot.first_content_ms{fill:var(--s4);} .dot.js_ready_ms{fill:var(--s5);}
  .dot.image_displayed_ms{fill:var(--s6);}
  .dot.slide_duplicated_ms{fill:var(--s7);}
  .dot.master_mem{fill:var(--wmem);} .dot.tab_mem{fill:var(--tmem);}
  .dot:hover{r:6;} .dot:focus-visible{outline:2px solid var(--ink);outline-offset:1px;}
  /* a run captured under perf: ringed, since profiling inflates its own times */
  .dot.prof{stroke:var(--ink);stroke-width:2;}
  .dot.busy{opacity:.4;}
  .modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;
    padding:24px;background:rgba(11,11,11,.55);} .modal[hidden]{display:none;}
  .modal-card{background:var(--panel);border:1px solid var(--hair);border-radius:12px;
    box-shadow:var(--shadow);width:min(560px,96vw);max-height:92vh;overflow:auto;}
  .modal-head{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;
    border-bottom:1px solid var(--hair);} .modal-head strong{font-size:14px;}
  .modal-head button{font:inherit;font-size:13px;cursor:pointer;border-radius:7px;
    border:1px solid var(--hair);background:var(--surface);color:var(--ink);padding:5px 12px;}
  .modal-head button:disabled{cursor:default;opacity:.45;}
  .modal-head{flex-wrap:wrap;gap:8px;}
  .mnav{display:flex;gap:6px;align-items:center;margin-left:auto;}
  .mnav button,.mnav .pos{white-space:nowrap;}
  .mnav .pos{font-size:12px;color:var(--ink-soft);font-variant-numeric:tabular-nums;margin:0 4px;}
  .modal .meta{padding:8px 16px;color:var(--ink-soft);font-size:12.5px;
    font-family:ui-monospace,Menlo,monospace;}
  table{border-collapse:collapse;width:100%;font-size:13px;}
  th,td{text-align:right;padding:6px 12px;border-bottom:1px solid var(--hair);
    font-variant-numeric:tabular-nums;font-family:ui-monospace,Menlo,monospace;}
  th:first-child,td:first-child{text-align:left;font-family:system-ui,sans-serif;}
  tr.prof td{color:var(--ink-soft);}
  thead th{color:var(--ink-soft);font-weight:600;}
  #tip{position:fixed;z-index:60;pointer-events:none;background:var(--panel);color:var(--ink);
    border:1px solid var(--hair);border-radius:6px;padding:4px 8px;font-size:12px;box-shadow:var(--shadow);
    font-family:ui-monospace,Menlo,monospace;white-space:nowrap;transition:none;}
  #tip[hidden]{display:none;}
  /* the document a chart's test opens */
  .fname{margin:-2px 0 8px;font-size:11.5px;color:var(--ink-faint);
    font-family:ui-monospace,Menlo,monospace;overflow-wrap:anywhere;}
  /* inline flamegraph viewer, in the run detail */
  .modal-card.wide{width:min(1280px,96vw);}
  .fg-sec{border-top:1px solid var(--hair);padding:10px 16px 16px;}
  .fg-sec summary{cursor:pointer;font-size:13px;font-weight:600;}
  .fg-bar{display:flex;gap:10px 16px;align-items:center;flex-wrap:wrap;
    margin:10px 0 9px;font-size:12.5px;color:var(--ink-soft);}
  .fg-bar select{font:inherit;font-size:12.5px;padding:3px 8px;border-radius:7px;
    border:1px solid var(--hair);background:var(--surface);color:var(--ink);}
  .fg-grid{display:grid;grid-template-columns:1fr;gap:12px;}
  .fg-grid.two{grid-template-columns:1fr 1fr;}
  @media (max-width:900px){.fg-grid.two{grid-template-columns:1fr;}}
  .fg-frame{margin:0;border:1px solid var(--hair);border-radius:9px;overflow:hidden;}
  .fg-frame figcaption{font-size:11.5px;color:var(--ink-soft);padding:6px 10px;
    border-bottom:1px solid var(--hair);font-family:ui-monospace,Menlo,monospace;}
  /* A capture is a wide artifact: width is what carries the meaning, so fill the
     panel width and scroll vertically rather than shrinking it to fit a box.
     fitFrame() sets the real height once the SVG reports its aspect. It also
     keeps its own light background, so pin that rather than inherit. */
  .fg-scroll{overflow:auto;max-height:min(72vh,640px);background:#fff;}
  .fg-frame object{width:100%;height:340px;border:0;display:block;background:#fff;}
  .fg-missing{margin:0;padding:12px 16px 16px;font-size:12.5px;color:var(--ink-faint);
    border-top:1px solid var(--hair);}
  .fg-missing code{font-family:ui-monospace,Menlo,monospace;}
  footer{margin-top:26px;font-size:12.5px;color:var(--ink-faint);border-top:1px solid var(--hair);
    padding-top:14px;}
</style></head><body>
<div class="wrap">
  <h1>Collabora Online browser tests</h1>
  <div class="tabs" role="tablist" id="tabs" aria-label="views" hidden>
    <button type="button" role="tab" id="tab-charts" aria-controls="view-charts" aria-selected="true">Charts</button>
    <button type="button" role="tab" id="tab-compare" aria-controls="view-compare" aria-selected="false" tabindex="-1">Compare builds</button>
    <button type="button" role="tab" id="tab-runs" aria-controls="view-runs" aria-selected="false" tabindex="-1">Runs</button>
  </div>
  <div id="view-charts" role="tabpanel" aria-labelledby="tab-charts">
  <p class="sub">One chart per document and user pool, tracked across commits. The thick line is the instructions that the document kit executed, and the dashed grey line those of coolwsd. Left axis: load, feedback and edit times (ms), or the instructions (G) when the times are hidden. The buttons in the legend show or hide each group. Right axis: memory (MB), the document's own footprint, the browser heap, the page process, and the coolwsd process peak. Click a point for detail.</p>
  <div class="legend">
    <span class="views" role="group" aria-label="lines shown on the charts">show <button class="vt" type="button" data-v="instr" aria-pressed="true">instructions</button><button class="vt" type="button" data-v="times" aria-pressed="true">times</button><button class="vt" type="button" data-v="mem" aria-pressed="true">memory</button></span>
    <span class="branches" id="branches" hidden><span class="sep"></span><label>branch <select id="brsel"></select></label></span>
    <span class="sep"></span>
    <span class="key" data-g="instr"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--ink)" stroke-width="3"/></svg> instructions, document kit (G)</span>
    <span class="key" data-g="instr"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--ink-soft)" stroke-width="2.5" stroke-dasharray="6 3"/></svg> instructions, coolwsd (G)</span>
    <span class="sep"></span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s0)" stroke-width="2.5"/></svg> frame ready</span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s4)" stroke-width="2.5"/></svg> first content</span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s5)" stroke-width="2.5"/></svg> js ready</span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s1)" stroke-width="2.5"/></svg> page load</span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s2)" stroke-width="2.5"/></svg> edit</span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s6)" stroke-width="2.5"/></svg> image displayed</span>
    <span class="key" data-g="times"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s7)" stroke-width="2.5"/></svg> slide duplicated</span>
    <span class="sep"></span>
    <span class="key" data-g="mem"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--mem)" stroke-width="2.5" stroke-dasharray="5 4"/></svg> COOL doc memory (MB, right)</span>
    <span class="key" data-g="mem"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--bmem)" stroke-width="2.5" stroke-dasharray="5 4"/></svg> browser heap (MB, right)</span>
    <span class="key" data-g="mem"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--tmem)" stroke-width="2.5" stroke-dasharray="8 3"/></svg> page process, resident (MB, own scale at the far right)</span>
    <span class="key" data-g="mem"><svg class="lswatch" viewBox="0 0 22 8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--wmem)" stroke-width="2.5" stroke-dasharray="2 3"/></svg> coolwsd peak (MB, right)</span>
    <span class="key" data-g="mem"><svg class="lswatch" viewBox="0 0 22 8"><rect x="0" y="1" width="5" height="6" fill="var(--st-lib)"/><rect x="5.5" y="1" width="5" height="6" fill="var(--st-other)"/><rect x="11" y="1" width="5" height="6" fill="var(--st-inh)"/><rect x="16.5" y="1" width="5" height="6" fill="var(--st-own)"/></svg> kit memory stacked: libraries, other files, inherited, own (MB, right)</span>
  </div>
  <div id="charts"></div>
  <footer>Each point is one run (one COOL commit) of the document named under the chart title, which is the file that test opens through the WOPI host. Times are ms averaged over the run (left axis). Memory (right axis, dashed) is the peak during the run: COOL document memory is the document's own footprint: the kit's Private_Dirty from COOL's getMetrics endpoint on a plain run, or the document kit's proportional set size sampled from /proc when the point comes from a flamegraph capture, which runs its own COOL, and browser heap is the page's JS heap. The page process is the resident size of the Chromium renderer that runs the page, sampled from /proc during the test: on top of the JS heap it holds the DOM, the decoded images and the canvas pixels of the tiles. The coolwsd peak is the largest resident size of
coolwsd and its forkit together during the test, sampled from /proc. The forkit holds the
preloaded engine, most of the figure. Runs from before that sampling show the resident
high-water mark (VmHWM) of the capture's own coolwsd instead. The stacked bar beside each point breaks the document kit's resident memory into
    the engine libraries it maps, other mapped files, what the fork handed it (mostly the
    dictionaries the parent preloaded), and what the document itself wrote. Only the last layer is
    the document's own cost; the first three are shared with every other document on the machine,
    which is why the resident total is several times what one more document actually adds.
    The instruction lines have a scale of their own, which the left axis shows when the times
    are hidden. They count only the window perf was attached for, which opens after the server
    is up, so they describe the test and not a whole process life. A busy machine slows the
    times but does not change the instruction counts, so a busy run stays on the instruction
    lines. A rising line across commits is a regression. A faded point was measured on a busy machine: a fixed probe ran slower than usual during the run, or the hypervisor took CPU time from the VM, so its times are not comparable. The run detail of each point gives the commits since the previous point, as a git log range and, for a GitHub source, a link that lists them. When a run was also captured under perf, its point shows the plain times and the run detail lists the times under perf beside them. A ringed point is a capture with no plain run beside it, so its times run high by nature and are not a regression. Each run's detail embeds that run's own <b>document flamegraph</b> (k6-flamegraph profiling the kitbroker_* document kit while the document is open, captured with its own single-kit COOL) and can put a second run beside it to compare two commits. A capture is only ever shown for the run it was taken in, and the badge on each chart says how many of that document's runs were profiled.</footer>
  </div>
  <div id="view-compare" role="tabpanel" aria-labelledby="tab-compare" hidden>
  <section class="cmp" id="cmp" aria-labelledby="cmph">
    <h2 id="cmph">Compare two builds</h2>
    <div class="cmp-pick">
      <div class="side"><b>A</b><select id="cmpa" aria-label="A, first measured commit"></select>
        <label>through <select id="cmpa2"></select></label></div>
      <div class="side"><b>B</b><select id="cmpb" aria-label="B, first measured commit"></select>
        <label>through <select id="cmpb2"></select></label></div>
      <label class="all"><input type="checkbox" id="cmpall"> show unchanged</label>
    </div>
    <p class="cmp-note" id="cmpnote"></p>
    <div class="cmp-scroll"><table id="cmptab"></table></div>
  </section>
  </div>
  <div id="view-runs" role="tabpanel" aria-labelledby="tab-runs" hidden>
  <section class="cmp runs" aria-labelledby="runsh">
    <h2 id="runsh">Runs</h2>
    <p class="cmp-note">Every run on the charts, newest first, with the time it started, in the time zone of this browser.
      The commits are those the run measured for the first time: since the previous run of
      its branch, or since its base on main for a change. The data link opens the run's
      folder, with its logs, metrics and flamegraphs. A deleted run leaves the store and the
      charts.</p>
    <div class="runs-bar"><button type="button" id="rundel" disabled>Delete selected runs</button>
      <span id="runmsg" role="status"></span></div>
    <div class="cmp-scroll"><table id="runtab"></table></div>
  </section>
  </div>
</div>
<div id="tip" role="tooltip" hidden></div>
<div class="modal" id="modal" hidden><div class="modal-card" id="mcard" role="dialog" aria-modal="true">
  <div class="modal-head"><strong id="mt">Run</strong><div class="mnav">
    <button id="mprev" type="button" title="Previous run (Left arrow)">&larr; Previous</button>
    <span class="pos" id="mpos"></span>
    <button id="mnext" type="button" title="Next run (Right arrow)">Next &rarr;</button>
    <button id="mc" type="button">Close</button></div></div>
  <div class="meta" id="mmeta"></div><div id="mb"></div><div id="mfg"></div>
</div></div>
<script>
const DATA = __DATA__;
const COMPARE = __COMPARE__;
const RESULTS = __RESULTS__;
const SVGNS = "http://www.w3.org/2000/svg";
const MS = [{m:"document_loaded_ms"},{m:"frame_ready_ms"},{m:"first_content_ms"},{m:"js_ready_ms"},{m:"page_load_ms"},{m:"edit_ms"},{m:"image_displayed_ms"},{m:"slide_duplicated_ms"}];
const NAMES={instr_kit:"document kit instructions",instr_wsd:"coolwsd instructions",document_loaded_ms:"document loaded",frame_ready_ms:"frame ready",first_content_ms:"first content",js_ready_ms:"js ready",page_load_ms:"page load",edit_ms:"edit",image_displayed_ms:"image displayed",slide_duplicated_ms:"slide duplicated",cool_mem:"COOL memory",browser_mem:"browser heap",tab_mem:"page process",master_mem:"coolwsd peak"};
const APP_ORDER=["writer","calc","impress","draw"],DOC_ORDER=["blank","blank-vector","5mb","5mb-vector","10mb","duplicate","duplicate-vector","ooxml","ooxml-vector","image5mb"];
// Instructions count the same work whatever the load of the machine, while
// the times follow it, so either group can be hidden to read the other.
const INS=[{cls:"instr_kit",k:"kit"},{cls:"instr_wsd",k:"master"}];
const insOf=(r,k)=>{const n=r.instructions;return n&&n[k]!=null?n[k]:null;};
const SHOW={instr:true,times:true,mem:true};
try{Object.assign(SHOW,JSON.parse(localStorage.getItem("cool-perf-show")||"{}"));}catch(e){}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));}
function niceStep(s){if(s<=0)return 1;const r=s/4,m=Math.pow(10,Math.floor(Math.log10(r))),n=r/m;return (n<=1?1:n<=2?2:n<=5?5:10)*m;}
function niceCeil(v){return Math.ceil(v/niceStep(v))*niceStep(v);}
function el(name,attrs,text){const e=document.createElementNS(SVGNS,name);for(const k in attrs)e.setAttribute(k,attrs[k]);if(text!=null)e.textContent=text;return e;}
// A run's label is its commit, sometimes with a suffix naming what was changed
// in that build. Shortening to seven characters would drop the suffix and leave
// two builds of the same commit looking identical, so keep whatever follows the
// first dash.
function shortLabel(c){if(!c)return "";const i=c.indexOf("-");
  return i<0?c.slice(0,7):c.slice(0,Math.min(i,7))+c.slice(i);}
function shaOf(c){const m=/^[0-9a-f]{7,40}/.exec(c||"");return m?m[0]:null;}
// The commits that a run measured for the first time: after the newest earlier run of this chart
// with another commit, up to the commit of this run.
function commitRange(runs,i){
  const to=shaOf(runs[i].commit);if(!to)return "";
  for(let j=i-1;j>=0;j--){const from=shaOf(runs[j].commit);if(!from||from===to)continue;
    if((runs[j].branch||"main")!==(runs[i].branch||"main"))continue;
    const text=`${from}..${to}`;
    const link=COMPARE?`<a href="${esc(COMPARE+from+"..."+to)}" target="_blank" rel="noopener">${esc(text)}</a>`:esc(text);
    return `<br>commits since the previous run: ${link}  ·  <code>git log --oneline ${esc(text)}</code>`;}
  return "<br>first commit measured in this chart";
}
// The document kit's resident memory in the layers it is made of, bottom-up.
// The layers add up to what the process has resident, so the top of the stack is
// the figure a machine actually has to hold for that document.
const STACK=[["libraries","lib","engine libraries"],["other_files","other","other mapped files"],
             ["inherited","inh","inherited from the fork"],["own","own","the document's own"]];
function stackOf(pt){const m=pt.cool_memory,d=m&&m.doc_stack;if(!d)return null;
  let total=0;const parts=[];
  for(const [key,cls,name] of STACK){const v=d[key];if(v==null)continue;
    parts.push({cls,name,v});total+=v;}
  return parts.length?{parts,total}:null;}
// The peak resident size during the test, which a plain run records for every
// test. A run from before that has the capture's high-water mark only.
function masterOf(pt){const m=pt.cool_memory;if(!m)return null;
  if(m.peak_master_rss_mb!=null)return m.peak_master_rss_mb;
  return m.peak_master_hwm_mb!=null?m.peak_master_hwm_mb:null;}
function tabOf(pt){const b=pt.browser_memory,r=b&&b.renderer;
  return r&&r.rss!=null?r.rss:null;}
// A run with no document figure leaves a gap: the COOL total in its place is
// ten times larger, and drew a spike that set the axis.
function memOf(pt){const m=pt.cool_memory;if(!m)return null;
  if(m.peak_doc_mem_mb!=null)return m.peak_doc_mem_mb;
  return m.peak_pss_mb!=null?m.peak_pss_mb:(m.peak_rss_mb!=null?m.peak_rss_mb:null);}

function render(svg, runs, W=520, H=300){
  // The Chromium renderers hold several times the memory of everything else,
  // so the page process gets a scale of its own, outside the MB axis.
  const tabMax=SHOW.mem?Math.max(0,...runs.map(r=>tabOf(r)||0)):0;
  const m={top:14,right:tabMax>0?96:52,bottom:46,left:50};
  svg.replaceChildren();svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  const pW=W-m.left-m.right,pH=H-m.top-m.bottom,x0=m.left,x1=x0+pW,y1=m.top+pH;
  let msMax=0,memMax=0,insMax=0;
  const heapOf=r=>{const x=r.metrics.browser_heap_mb;return x?x.avg:null;};
  runs.forEach(r=>{
    if(SHOW.times)MS.forEach(s=>{const v=r.metrics[s.m]&&r.metrics[s.m].avg;if(v>msMax)msMax=v;});
    if(SHOW.mem){const mm=memOf(r);if(mm!=null&&mm>memMax)memMax=mm;
      const wm=masterOf(r);if(wm!=null&&wm>memMax)memMax=wm;
      const sk=stackOf(r);if(sk&&sk.total>memMax)memMax=sk.total;
      const bh=heapOf(r);if(bh!=null&&bh>memMax)memMax=bh;}
    if(SHOW.instr)INS.forEach(s=>{const v=insOf(r,s.k);if(v!=null&&v>insMax)insMax=v;});});
  const msTop=niceCeil(msMax)||1, memTop=niceCeil(memMax)||1, insTop=(niceCeil(insMax/1e9)||1)*1e9;
  const yMs=v=>y1-(v/msTop)*pH, yMem=v=>y1-(v/memTop)*pH, yIns=v=>y1-(v/insTop)*pH;
  const tabTop=niceCeil(tabMax)||1, yTab=v=>y1-(v/tabTop)*pH;
  // The left axis is for the times, and for the instructions once the times are hidden.
  const leftIns=!SHOW.times&&insMax>0, left=leftIns||msMax>0;
  if(left){const top=leftIns?insTop/1e9:msTop,step=niceStep(top);
    for(let v=0;v<=top+1e-9;v+=step){const y=y1-(v/top)*pH,t=el("g",{class:"tick"});
      t.appendChild(el("line",{x1:x0,x2:x1,y1:y,y2:y}));
      t.appendChild(el("text",{x:x0-7,y:y+3.5,"text-anchor":"end"},leftIns?String(+v.toFixed(2)):String(Math.round(v))));svg.appendChild(t);}}
  if(memMax>0){const ms2=niceStep(memTop);for(let v=0;v<=memTop+1e-9;v+=ms2){
    svg.appendChild(el("text",{class:"tick",x:x1+7,y:yMem(v)+3.5,"text-anchor":"start"},String(Math.round(v))));}}
  if(tabMax>0){const ts=niceStep(tabTop);for(let v=0;v<=tabTop+1e-9;v+=ts){
    svg.appendChild(el("text",{class:"tabtick",x:x1+48,y:yTab(v)+3.5,"text-anchor":"start"},String(Math.round(v))));}}
  svg.appendChild(el("line",{class:"baseline",x1:x0,x2:x1,y1:y1,y2:y1}));
  if(left)svg.appendChild(el("text",{class:"axtitle",x:12,y:m.top+pH/2,"text-anchor":"middle",transform:`rotate(-90 12 ${m.top+pH/2})`},leftIns?"G instructions":"ms"));
  if(memMax>0)svg.appendChild(el("text",{class:"axtitle",x:W-10,y:m.top+pH/2,"text-anchor":"middle",transform:`rotate(90 ${W-10} ${m.top+pH/2})`},"MB"));
  const n=runs.length,pad=n>1?18:pW/2;
  const xAt=i=>x0+pad+(n>1?i*(pW-2*pad)/(n-1):0);
  // One stacked bar per run, on the same MB axis as the memory lines so its
  // height can be read off that axis rather than taken on trust.
  if(SHOW.mem)runs.forEach((r,i)=>{const sk=stackOf(r);if(!sk)return;
    const bw=Math.max(4,Math.min(13,pW/(runs.length*3)));
    const cx=xAt(i)+bw*1.6;
    let acc=0;
    sk.parts.forEach(part=>{
      const yTop=yMem(acc+part.v), h=Math.max(0.5,yMem(acc)-yTop);
      const rect=el("rect",{class:"sbar "+part.cls,x:cx-bw/2,y:yTop,width:bw,height:h});
      const tip=`${shortLabel(r.commit)} · ${part.name}: ${Math.round(part.v)} MB of ${Math.round(sk.total)} resident`;
      rect.setAttribute("data-tip",tip);rect.setAttribute("aria-label",tip);
      svg.appendChild(rect);acc+=part.v;});});
  // fmt gives the value with its unit. A steady line keeps the busy runs on it.
  let drawn=0;
  function plot(cls,scale,getv,fmt,steady){
    const pts=[];runs.forEach((r,i)=>{const v=getv(r);if(v!=null)pts.push([i,xAt(i),scale(v),v]);});
    if(!pts.length)return;
    drawn++;
    // the line joins the runs of a quiet machine, and a busy run stays a faded dot beside it
    const quiet=steady?pts:pts.filter(p=>!(runs[p[0]].machine&&runs[p[0]].machine.busy));
    if(quiet.length>1)svg.appendChild(el("polyline",{class:"line "+cls,points:quiet.map(p=>p[1]+","+p[2]).join(" ")}));
    pts.forEach(p=>{const r=runs[p[0]];
      const busy=r.machine&&r.machine.busy;
      const dot=el("circle",{class:"dot "+cls.split(" ")[0]+(r.profiled?" prof":"")+(busy&&!steady?" busy":""),cx:p[1],cy:p[2],r:3.5,tabindex:0,role:"button"});
      const short=cls.split(" ")[0],unit=cls.indexOf("mem")>=0?"MB":"ms";
      const label=`${shortLabel(r.commit)} · ${NAMES[short]||short}: ${fmt?fmt(p[3]):Math.round(p[3])+" "+unit}`+(r.profiled?" · profiled":"")+(busy?" · busy machine":"");
      dot.setAttribute("data-tip",label);dot.setAttribute("aria-label",label);
      dot.addEventListener("click",()=>openDetail(runs,p[0]));svg.appendChild(dot);});
  }
  if(SHOW.times)MS.forEach(s=>plot(s.m,yMs,r=>{const x=r.metrics[s.m];return x?x.avg:null;}));
  if(SHOW.mem){plot("master_mem mem",yMem,masterOf);
    plot("cool_mem mem",yMem,memOf);
    plot("browser_mem mem",yMem,heapOf);
    plot("tab_mem mem",yTab,tabOf);}
  // drawn last, so they stay on top of the other lines
  if(SHOW.instr)INS.forEach(s=>plot(s.cls,yIns,r=>insOf(r,s.k),v=>(v/1e9).toFixed(2)+" G",true));
  // perf counts the instructions of the 1-user runs only
  if(!drawn)svg.appendChild(el("text",{class:"axtitle",x:x0+pW/2,y:m.top+pH/2,"text-anchor":"middle"},
    SHOW.instr&&!SHOW.times&&!SHOW.mem?"no instruction counts: perf measures the 1-user runs only":"nothing to show for the lines selected"));
  // x labels: short commit, thinned if crowded
  const everyk=Math.ceil(n/Math.max(6,Math.floor(pW/75)));
  runs.forEach((r,i)=>{if(i%everyk)return;svg.appendChild(el("text",{class:"xlab",x:xAt(i),y:y1+15,"text-anchor":"middle"},r.commit?shortLabel(r.commit):("#"+i)));});
}

// The branch that the charts show, from the address so that the view can be
// passed on as a link. A run from before branches is of main.
const BRANCHES=[...new Set(Object.values(DATA).flatMap(rs=>rs.map(r=>r.branch||"main")))]
  .sort((a,b)=>(a!=="main")-(b!=="main")||a.localeCompare(b));
let BRANCH="main";
let fullChart=null;  // the chart in full view, see setFull
(function(){const h=new URLSearchParams(location.hash.slice(1));
  BRANCH=BRANCHES.includes(h.get("branch"))?h.get("branch"):(BRANCHES.includes("main")?"main":BRANCHES[0]||"main");})();
function viewOf(key){return (DATA[key]||[]).filter(r=>(r.branch||"main")===BRANCH);}
function renderAll(){
  document.querySelectorAll("section.chart").forEach(sec=>{const svg=sec.querySelector("svg[data-key]");
    const runs=viewOf(svg.dataset.key);sec.hidden=!runs.length;
    if(runs.length&&(!fullChart||fullChart===sec))fullChart===sec?drawFull():render(svg,runs);});}

const host=document.getElementById("charts");
function chartOrder(a,b){const A=a.split("|"),B=b.split("|");
  return (APP_ORDER.indexOf(A[0])-APP_ORDER.indexOf(B[0]))
      || (DOC_ORDER.indexOf(A[1])-DOC_ORDER.indexOf(B[1]))
      || (Number(A[2])-Number(B[2]));}
Object.keys(DATA).sort(chartOrder).forEach(key=>{
  const [app,doc,users]=key.split("|");
  const runs=DATA[key];
  // the document under test, from the newest run that recorded one
  const file=(runs.filter(r=>r.file).pop()||{}).file;
  const sec=document.createElement("section");sec.className="chart a-"+app;
  sec.innerHTML=`<h2>${esc(app)} &middot; ${esc(doc)} &middot; ${esc(users)} user${users==="1"?"":"s"}<button class="fullbtn" type="button" aria-pressed="false">full view</button></h2>
    ${file?`<p class="fname" title="the document this test opens">${esc(file)}</p>`:""}
    <svg data-key="${esc(key)}" viewBox="0 0 520 300" role="img" aria-label="${esc(key)}${file?" of "+esc(file):""} over commits"></svg>`;
  host.appendChild(sec);
});
renderAll();

// One chart at a time fills the window. It is drawn again at the size of the window, so the text
// keeps its size and the axis has room for more commit labels.
function drawFull(){const svg=fullChart.querySelector("svg[data-key]");
  const box=svg.getBoundingClientRect();
  render(svg,viewOf(svg.dataset.key),Math.max(520,Math.round(box.width)),Math.max(300,Math.round(box.height)));}
function setFull(sec){
  if(fullChart){const old=fullChart;fullChart=null;old.classList.remove("full");
    old.querySelector(".fullbtn").textContent="full view";
    old.querySelector(".fullbtn").setAttribute("aria-pressed","false");
    const svg=old.querySelector("svg[data-key]");render(svg,viewOf(svg.dataset.key));}
  document.body.classList.toggle("has-full",!!sec);
  if(!sec)return;
  fullChart=sec;sec.classList.add("full");
  sec.querySelector(".fullbtn").textContent="close";
  sec.querySelector(".fullbtn").setAttribute("aria-pressed","true");
  drawFull();}
host.addEventListener("click",e=>{const b=e.target.closest(".fullbtn");if(!b)return;
  const sec=b.closest(".chart");setFull(fullChart===sec?null:sec);});
window.addEventListener("resize",()=>{if(fullChart)drawFull();});

// The buttons in the legend show or hide a group of lines on every chart. The
// choice is kept in this browser only.
function syncViews(){
  document.querySelectorAll(".vt").forEach(b=>b.setAttribute("aria-pressed",String(!!SHOW[b.dataset.v])));
  document.querySelectorAll(".legend .key[data-g]").forEach(k=>k.classList.toggle("off",!SHOW[k.dataset.g]));}
document.querySelector(".views").addEventListener("click",e=>{const b=e.target.closest(".vt");if(!b)return;
  SHOW[b.dataset.v]=!SHOW[b.dataset.v];
  try{localStorage.setItem("cool-perf-show",JSON.stringify(SHOW));}catch(e){}
  syncViews();
  document.querySelectorAll("svg[data-key]").forEach(svg=>{
    if(!fullChart||!fullChart.contains(svg))render(svg,viewOf(svg.dataset.key));});
  if(fullChart)drawFull();});
syncViews();

// The address holds the branch of the charts and the two sides of the comparison.
let VIEW=(v=>["compare","runs"].includes(v)?v:"charts")(new URLSearchParams(location.hash.slice(1)).get("view"));
function writeHash(){const h=new URLSearchParams();if(VIEW!=="charts")h.set("view",VIEW);
  if(BRANCH!=="main")h.set("branch",BRANCH);
  if(CMP.a)h.set("a",CMP.a.from+(CMP.a.to?".."+CMP.a.to:""));
  if(CMP.b)h.set("b",CMP.b.from+(CMP.b.to?".."+CMP.b.to:""));
  history.replaceState(null,"",h.toString()?"#"+h:location.pathname+location.search);}

(function(){if(BRANCHES.length<2)return;
  const sel=document.getElementById("brsel");
  sel.innerHTML=BRANCHES.map(b=>`<option${b===BRANCH?" selected":""}>${esc(b)}</option>`).join("");
  sel.addEventListener("change",()=>{BRANCH=sel.value;writeHash();renderAll();});
  document.getElementById("branches").hidden=false;})();

// ---- comparison of two sides, each a measured commit or a range of them --------------
// A measured point: the runs of one branch, one after the other, that measured the
// same commit. Grouping by the label alone would merge builds that share a label.
const POINTS=(()=>{const first=new Map();
  Object.values(DATA).forEach(rs=>rs.forEach(r=>{if(!r.runid)return;const o=first.get(r.runid);
    if(!o||(r.ts||"")<o.ts)first.set(r.runid,{runid:r.runid,ts:r.ts||"",branch:r.branch||"main",commit:r.commit,base:r.base});}));
  const byBranch={};
  [...first.values()].sort((a,b)=>a.ts<b.ts?-1:a.ts>b.ts?1:0).forEach(r=>{
    const list=byBranch[r.branch]||(byBranch[r.branch]=[]),last=list[list.length-1];
    if(last&&last.commit===r.commit){last.runids.push(r.runid);last.ts2=r.ts;return;}
    list.push({id:r.runid,branch:r.branch,commit:r.commit,base:r.base,ts:r.ts,ts2:r.ts,runids:[r.runid]});});
  return byBranch;})();
const POINT_BY_ID={};Object.values(POINTS).forEach(l=>l.forEach(p=>{POINT_BY_ID[p.id]=p;}));
// A run time in the time zone of the browser; the data holds it in UTC.
const TZ=Intl.DateTimeFormat().resolvedOptions().timeZone;
const two=n=>String(n).padStart(2,"0");
const day=ts=>{const d=new Date(ts);
  return isNaN(d)?String(ts||"").slice(0,16).replace("T"," "):
    `${d.getFullYear()}-${two(d.getMonth()+1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;};
const pointName=p=>`${p.branch} · ${shortLabel(p.commit||"?")} · ${day(p.ts)}`+(p.runids.length>1?` · ${p.runids.length} runs`:"");
// the run ids of a side, from its first point through its last on the same branch
function sideRuns(side){const f=POINT_BY_ID[side.from];if(!f)return new Set();
  const list=POINTS[f.branch],t=side.to&&POINT_BY_ID[side.to]&&POINT_BY_ID[side.to].branch===f.branch?POINT_BY_ID[side.to]:f;
  let i=list.indexOf(f),j=list.indexOf(t);if(i>j)[i,j]=[j,i];
  return new Set(list.slice(i,j+1).flatMap(p=>p.runids));}
function sideName(side){const f=POINT_BY_ID[side.from],t=side.to&&POINT_BY_ID[side.to];
  if(!f)return "nothing";
  return t&&t!==f?`${f.branch} · ${shortLabel(f.commit)} (${day(f.ts)}) to ${shortLabel(t.commit)} (${day(t.ts)}), ${sideRuns(side).size} runs`:pointName(f);}

// What is compared, and how far a value has to move before it counts as a change: by the
// share th of its value, and by abs in its own unit, so that a few ms on a short time do
// not count. The instruction counts hardly depend on the load of the machine, the times
// depend on it most.
const heapOf=r=>{const x=r.metrics.browser_heap_mb;return x?x.avg:null;};
const CMP_METRICS=[
  {name:"instructions, document kit",get:r=>insOf(r,"kit"),fmt:v=>(v/1e9).toFixed(2)+" G",th:.01,abs:5e7},
  {name:"instructions, coolwsd",get:r=>insOf(r,"master"),fmt:v=>(v/1e9).toFixed(2)+" G",th:.01,abs:5e7},
  ...MS.map(s=>({name:NAMES[s.m]||s.m,get:r=>{const x=r.metrics[s.m];return x?x.avg:null;},fmt:v=>Math.round(v)+" ms",th:.05,abs:50})),
  {name:"COOL doc memory",get:memOf,fmt:v=>Math.round(v)+" MB",th:.03,abs:10},
  {name:"kit memory, resident",get:r=>{const s=stackOf(r);return s?s.total:null;},fmt:v=>Math.round(v)+" MB",th:.03,abs:10},
  {name:"coolwsd peak",get:masterOf,fmt:v=>Math.round(v)+" MB",th:.03,abs:10},
  {name:"browser heap",get:heapOf,fmt:v=>Math.round(v)+" MB",th:.03,abs:10},
  {name:"page process",get:tabOf,fmt:v=>Math.round(v)+" MB",th:.03,abs:10},
];
const median=a=>{const s=a.slice().sort((x,y)=>x-y),n=s.length;return n?(n%2?s[(n-1)/2]:(s[n/2-1]+s[n/2])/2):null;};

let CMP={a:null,b:null},CMP_NOTE="";
(function(){const h=new URLSearchParams(location.hash.slice(1));
  const parse=v=>{if(!v)return null;const [from,to]=v.split("..");return POINT_BY_ID[from]?{from,to:to&&POINT_BY_ID[to]?to:""}:null;};
  CMP.a=parse(h.get("a"));CMP.b=parse(h.get("b"));
  if(CMP.a&&CMP.b)return;
  // By default the newest change against main at the commit it is built on, or else the
  // last two measured commits of main.
  const all=Object.values(POINTS).flat().sort((x,y)=>x.ts<y.ts?-1:1);
  const main=POINTS.main||[],other=all.filter(p=>p.branch!=="main");
  const b=other.length?other[other.length-1]:main[main.length-1];
  if(!b)return;
  let a=null;
  if(b.branch!=="main"){
    const base=shaOf(b.base);
    a=base?main.filter(p=>shaOf(p.commit)===base).pop():null;
    if(!a){a=main.filter(p=>p.ts<b.ts).pop()||main[main.length-1];
      if(a)CMP_NOTE=base?`The base of ${b.branch}, ${base}, was never measured on main, so A is the last run of main before it.`
                        :`No base is recorded for ${b.branch}, so A is the last run of main before it.`;}
  }else a=main[main.indexOf(b)-1];
  if(a){CMP.a={from:a.id,to:""};CMP.b={from:b.id,to:""};}})();

function fillSide(k){const side=CMP[k],from=document.getElementById("cmp"+k),to=document.getElementById("cmp"+k+"2");
  from.innerHTML=Object.keys(POINTS).sort((a,b)=>(a!=="main")-(b!=="main")||a.localeCompare(b)).map(br=>
    `<optgroup label="${esc(br)}">`+POINTS[br].slice().reverse().map(p=>
      `<option value="${esc(p.id)}"${side&&p.id===side.from?" selected":""}>${esc(pointName(p))}</option>`).join("")+`</optgroup>`).join("");
  const f=side&&POINT_BY_ID[side.from];
  to.innerHTML=`<option value="">that commit only</option>`+(f?POINTS[f.branch].slice().reverse().filter(p=>p!==f).map(p=>
    `<option value="${esc(p.id)}"${p.id===side.to?" selected":""}>${esc(pointName(p))}</option>`).join(""):"");}

function renderCompare(){
  const tab=document.getElementById("cmptab"),note=document.getElementById("cmpnote");
  if(!CMP.a||!CMP.b){tab.innerHTML="";note.textContent="Pick two measured commits to compare.";return;}
  const ra=sideRuns(CMP.a),rb=sideRuns(CMP.b),all=document.getElementById("cmpall").checked;
  let rows="",changes=0,still=0,docs=0,missing=0;
  Object.keys(DATA).sort(chartOrder).forEach(key=>{
    const A=DATA[key].filter(r=>ra.has(r.runid)),B=DATA[key].filter(r=>rb.has(r.runid));
    if(!A.length||!B.length){if(A.length||B.length)missing++;return;}
    const lines=[];
    CMP_METRICS.forEach(m=>{
      const va=median(A.map(m.get).filter(v=>v!=null)),vb=median(B.map(m.get).filter(v=>v!=null));
      if(va==null||vb==null||!va)return;
      const d=(vb-va)/va,changed=Math.abs(d)>=m.th&&Math.abs(vb-va)>=m.abs;
      changed?changes++:still++;
      if(!changed&&!all)return;
      // lighter the closer the change is to its threshold, full past four times it
      const k=changed?Math.min(1,(Math.abs(d)-m.th)/(3*m.th)):0;
      const cls=!changed?"same":d<0?"better":"worse";
      const pct=(d>0?"+":"")+(d*100).toFixed(1)+"%";
      lines.push(`<td class="m">${esc(m.name)}</td><td>${m.fmt(va)}</td><td>${m.fmt(vb)}</td>`+
        `<td class="d ${cls}" style="--k:${(18+k*50).toFixed(0)}%">${pct}</td>`);});
    if(!lines.length)return;
    docs++;
    const [app,doc,users]=key.split("|");
    rows+=lines.map((l,i)=>`<tr>${i?"":`<th scope="rowgroup" rowspan="${lines.length}">${esc(app)} · ${esc(doc)} · ${esc(users)} user${users==="1"?"":"s"}</th>`}${l}</tr>`).join("");});
  tab.innerHTML=rows?`<thead><tr><th>test</th><th>metric</th><th>A</th><th>B</th><th>B against A</th></tr></thead><tbody>${rows}</tbody>`:"";
  note.innerHTML=`<b>A</b> ${esc(sideName(CMP.a))}<br><b>B</b> ${esc(sideName(CMP.b))}<br>`+
    (CMP_NOTE?esc(CMP_NOTE)+"<br>":"")+
    `${changes} change${changes===1?"":"s"} in ${docs} test${docs===1?"":"s"}, ${still} metric${still===1?"":"s"} within the noise: `+
    `a change counts from 1% and 0.05 G for instructions, 3% and 10 MB for memory, 5% and 50 ms for times. `+
    `Each value is the median over the runs of the side.`+
    (missing?` ${missing} test${missing===1?" was":"s were"} measured on one side only.`:"");}

(function(){const n=Object.values(POINTS).reduce((a,l)=>a+l.length,0);if(!n)return;
  // the tabs, with the arrow keys moving between them as a tab list does
  const tabs=[...document.querySelectorAll("#tabs [role=tab]")];
  const show=(v,focus,init)=>{VIEW=v;tabs.forEach(t=>{const on=t.id==="tab-"+v;
      t.setAttribute("aria-selected",String(on));t.tabIndex=on?0:-1;
      document.getElementById(t.getAttribute("aria-controls")).hidden=!on;if(on&&focus)t.focus();});
    if(v!=="charts"&&fullChart)setFull(null);if(!init)writeHash();};
  tabs.forEach((t,i)=>{t.addEventListener("click",()=>show(t.id.slice(4)));
    t.addEventListener("keydown",e=>{const d=e.key==="ArrowRight"?1:e.key==="ArrowLeft"?-1:0;if(!d)return;
      e.preventDefault();show(tabs[(i+d+tabs.length)%tabs.length].id.slice(4),true);});});
  document.getElementById("tabs").hidden=false;
  show(VIEW,false,true);
  const pick=(k,which)=>{const v=document.getElementById("cmp"+k+which).value;
    if(which===""){CMP[k]={from:v,to:""};}else CMP[k]={from:CMP[k].from,to:v};
    CMP_NOTE="";fillSide(k);writeHash();renderCompare();};
  ["a","b"].forEach(k=>{if(!CMP[k]){const l=Object.values(POINTS).flat();CMP[k]={from:l[l.length-1].id,to:""};}
    document.getElementById("cmp"+k).addEventListener("change",()=>pick(k,""));
    document.getElementById("cmp"+k+"2").addEventListener("change",()=>pick(k,"2"));fillSide(k);});
  document.getElementById("cmpall").addEventListener("change",renderCompare);
  renderCompare();})();

// ---- the list of runs, and their deletion -------------------------------------------
// One row per run, gathered from the points that the run left on every chart.
const RUNS=(()=>{const m=new Map();
  Object.values(DATA).forEach(rs=>rs.forEach(r=>{if(!r.runid)return;let o=m.get(r.runid);
    if(!o)m.set(r.runid,o={runid:r.runid,ts:r.ts||"",branch:r.branch||"main",commit:r.commit,
      base:r.base,ref:r.ref,tests:0,busy:false,flames:0});
    if((r.ts||"")<o.ts)o.ts=r.ts||"";
    o.tests++;if(r.machine&&r.machine.busy)o.busy=true;if(r.flamegraph)o.flames++;}));
  return [...m.values()].sort((a,b)=>a.ts<b.ts?1:a.ts>b.ts?-1:0);})();
// A change is read against its base on main, a run of a branch against the previous run of
// that branch with another commit.
function runRange(r){const to=shaOf(r.commit);
  if(!to)return `<code>${esc(r.commit||"?")}</code>`;
  let from=r.branch!=="main"?shaOf(r.base):null;
  if(!from){const prev=RUNS.find(o=>o.branch===r.branch&&o.ts<r.ts&&shaOf(o.commit)&&shaOf(o.commit)!==to);
    from=prev?shaOf(prev.commit):null;}
  const label=r.commit.length>to.length?` <span class="ref">${esc(r.commit)}</span>`:"";
  if(!from)return `<code>${esc(to.slice(0,12))}</code> first run${label}`;
  const text=`${from.slice(0,12)}..${to.slice(0,12)}`;
  return (COMPARE?`<a href="${esc(COMPARE+from+"..."+to)}" target="_blank" rel="noopener"><code>${esc(text)}</code></a>`
                 :`<code>${esc(text)}</code>`)+label;}
(function(){const tab=document.getElementById("runtab"),btn=document.getElementById("rundel"),
    msg=document.getElementById("runmsg"),picked=new Set();
  const say=(text,err)=>{msg.textContent=text;msg.classList.toggle("err",!!err);};
  // The page can only delete when it comes from the store's web server, which runs the
  // delete service beside it.
  const served=location.protocol==="http:"||location.protocol==="https:";
  if(!served)say("Open the dashboard from its web server to delete runs.");
  function sync(){btn.disabled=!served||!picked.size;
    btn.textContent=picked.size?`Delete ${picked.size} selected run${picked.size===1?"":"s"}`:"Delete selected runs";
    tab.querySelectorAll("tbody tr").forEach(tr=>tr.classList.toggle("sel",picked.has(tr.dataset.run)));
    const all=tab.querySelector("thead input");
    if(all){all.checked=picked.size===RUNS.length;all.indeterminate=picked.size>0&&picked.size<RUNS.length;}}
  tab.innerHTML=`<thead><tr><th><input type="checkbox" aria-label="select every run"></th>`+
    `<th class="l">measured</th><th class="l">branch</th><th class="l">commits</th><th>tests</th>`+
    `<th>flamegraphs</th><th class="l">data</th></tr></thead><tbody>`+
    RUNS.map(r=>`<tr data-run="${esc(r.runid)}"><td><input type="checkbox" aria-label="select ${esc(r.runid)}"></td>`+
      `<td class="l">${esc(day(r.ts))}${r.busy?` <span class="ref">busy machine</span>`:""}</td>`+
      `<td class="l">${esc(r.branch)}${r.ref&&r.ref!==r.branch?`<span class="ref">${esc(r.ref)}</span>`:""}</td>`+
      `<td class="l">${runRange(r)}</td><td>${r.tests}</td><td>${r.flames||""}</td>`+
      `<td class="l"><a href="${esc(RESULTS+encodeURIComponent(r.runid)+"/")}">${esc(r.runid)}</a></td></tr>`).join("")+
    `</tbody>`;
  tab.querySelector("thead input").addEventListener("change",e=>{
    RUNS.forEach(r=>e.target.checked?picked.add(r.runid):picked.delete(r.runid));
    tab.querySelectorAll("tbody input").forEach(i=>{i.checked=e.target.checked;});sync();});
  tab.querySelectorAll("tbody tr").forEach(tr=>tr.querySelector("input").addEventListener("change",e=>{
    e.target.checked?picked.add(tr.dataset.run):picked.delete(tr.dataset.run);sync();}));
  btn.addEventListener("click",async()=>{const ids=[...picked];
    if(!confirm(`Delete ${ids.length} run${ids.length===1?"":"s"} and all their data from the store?\n\n${ids.join("\n")}`))return;
    btn.disabled=true;say("Deleting...");
    try{const res=await fetch("api/runs/delete",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({runids:ids})});
      const body=await res.json().catch(()=>({}));
      if(!res.ok)throw new Error(body.error||`${res.status} ${res.statusText}`);
      say(`Deleted ${body.deleted.length} run${body.deleted.length===1?"":"s"}. Reloading...`);
      location.reload();
    }catch(e){say("The runs were not deleted: "+e.message,true);sync();}});
  sync();})();

const modal=document.getElementById("modal"),mb=document.getElementById("mb"),mt=document.getElementById("mt"),
  mmeta=document.getElementById("mmeta"),mfg=document.getElementById("mfg"),mcard=document.getElementById("mcard");

function fgLabel(r){return (r.commit?shortLabel(r.commit):"local")+" · "+day(r.ts);}
function fgFrame(r){
  // <object>, not <img>: the capture carries its own script, so search and zoom
  // keep working inside the frame
  return `<figure class="fg-frame"><figcaption>${esc(fgLabel(r))} &middot; `+
    `<a class="fg-inline" href="${esc(r.flamegraph)}" target="_blank" rel="noopener">open \u2197</a></figcaption>`+
    `<div class="fg-scroll"><object type="image/svg+xml" data="${esc(r.flamegraph)}"`+
    // aspect from the SVG itself, measured when this file was written: a file://
    // page is an opaque origin, so it cannot read the size out of the embedded
    // document. Without it the capture would be scaled down to a fixed box.
    (r.fg_aspect?` style="height:auto;aspect-ratio:1/${r.fg_aspect}"`:``)+
    ` aria-label="flamegraph ${esc(fgLabel(r))}"></object></div></figure>`;
}
// The open run's flamegraph, inline, optionally beside another run of the same
// document for a before/after read. cmp is an index into runs, or null.
function renderFg(runs,i,cmp){
  const r=runs[i],others=runs.map((x,j)=>j).filter(j=>j!==i&&runs[j].flamegraph);
  if(!r.flamegraph){
    mcard.classList.remove("wide");
    mfg.innerHTML=`<p class="fg-missing">No flamegraph for this run. `+
      (others.length?`${others.length} other run(s) of this document have one.`
                    :`Capture one with <code>./browser-suite.py --flamegraphs</code>.`)+`</p>`;
    return;
  }
  mcard.classList.add("wide");
  const other=cmp!=null&&runs[cmp]&&runs[cmp].flamegraph?runs[cmp]:null;
  const opts=[`<option value="">none</option>`].concat(others.map(j=>
    `<option value="${j}"${j===cmp?" selected":""}>${esc(fgLabel(runs[j]))}</option>`)).join("");
  mfg.innerHTML=`<details class="fg-sec" open><summary>document flamegraph</summary>
    <div class="fg-bar"><label>side by side with: <select id="fgcmp"${others.length?"":" disabled"}>${opts}</select></label>
      <span>the kitbroker_* document kit while this document is open</span></div>
    <div class="fg-grid${other?" two":""}">${fgFrame(r)}${other?fgFrame(other):""}</div></details>`;
  // open on the base of the graph: flamegraph.pl draws the root at the bottom,
  // and the widest frames there are what you look at first
  mfg.querySelectorAll(".fg-scroll").forEach(d=>{d.scrollTop=d.scrollHeight;});
  const sel=document.getElementById("fgcmp");
  if(sel)sel.addEventListener("change",()=>renderFg(runs,i,sel.value===""?null:Number(sel.value)));
}

// The run that the detail shows, so that Previous and Next can step through
// the runs of the same chart.
let detailRuns=null,detailIdx=-1;
const mprev=document.getElementById("mprev"),mnext=document.getElementById("mnext"),
  mpos=document.getElementById("mpos");
function openDetail(runs,i){
  const r=runs[i];mt.textContent="Run detail";
  detailRuns=runs;detailIdx=i;
  mprev.disabled=i<=0;mnext.disabled=i>=runs.length-1;
  mpos.textContent=`${i+1} of ${runs.length}`;
  let meta=`commit ${esc(r.commit||"(unknown)")}  ·  ${esc(day(r.ts))} ${esc(TZ)}`;
  if(BRANCHES.length>1)meta+=`  ·  branch ${esc(r.branch||"main")}`+(r.ref?` (${esc(r.ref)})`:"");
  if(r.file)meta+=`  ·  ${esc(r.file)}`;
  if(r.runid)meta+=`  ·  run ${esc(r.runid)}`;
  meta+=commitRange(runs,i);
  mmeta.innerHTML=meta;
  const cols=["avg","min","med","p90","p95","max"];
  let h='<table><thead><tr><th>metric</th>'+cols.map(c=>`<th>${c}</th>`).join("")+'</tr></thead><tbody>';
  // a metric name ends in its unit: _ms for times, _mb for the browser heap, _n for a count
  const metricLabel=name=>/_mb$/.test(name)?[name.replace(/_mb$/,""),"MB"]
    :/_n$/.test(name)?[name.replace(/_n$/,""),"count"]:[name.replace(/_ms$/,""),"ms"];
  Object.keys(r.metrics).forEach(name=>{const s=r.metrics[name]||{};const [label,unit]=metricLabel(name);
    h+=`<tr><td>${label} (${unit})</td>`+cols.map(c=>`<td>${s[c]!=null?Math.round(s[c]):"-"}</td>`).join("")+"</tr>";});
  // the same test run again under perf for the flamegraph, slower by nature
  Object.keys(r.profiled_metrics||{}).forEach(name=>{const s=r.profiled_metrics[name]||{};const [label,unit]=metricLabel(name);
    h+=`<tr class="prof"><td>${label} under perf (${unit})</td>`+cols.map(c=>`<td>${s[c]!=null?Math.round(s[c]):"-"}</td>`).join("")+"</tr>";});
  const cm=r.cool_memory;
  const asMb=v=>v==null?null:Math.round(v);
  if(cm){
    // The document kit: what it had resident, what the proportional split gives
    // it, and how many kits that split was over. One kit alive means it carries
    // nearly the whole shared base, so the proportional figure runs high.
    const rss=asMb(cm.peak_doc_rss_mb), pss=asMb(cm.peak_doc_mem_mb!=null?cm.peak_doc_mem_mb:cm.peak_pss_mb);
    const own=asMb(cm.peak_doc_private_mb), inh=asMb(cm.peak_doc_shared_mb), kits=cm.doc_pids;
    const bits=[];
    if(rss!=null)bits.push(`resident ${rss}`);
    if(pss!=null)bits.push(`proportional ${pss}${kits?` over ${kits} kit${kits>1?"s":""}`:""}`);
    if(own!=null)bits.push(`own ${own}`);
    if(inh!=null)bits.push(`inherited ${inh}`);
    if(bits.length)h+=`<tr><td>document kit (MB)</td><td colspan="6" style="text-align:left">${bits.join(", ")}</td></tr>`;
    const wbits=[];
    if(cm.peak_master_hwm_mb!=null)wbits.push(`peak ${asMb(cm.peak_master_hwm_mb)}`);
    if(cm.peak_master_rss_mb!=null)wbits.push(`resident ${asMb(cm.peak_master_rss_mb)}`);
    if(cm.peak_master_pss_mb!=null)wbits.push(`proportional ${asMb(cm.peak_master_pss_mb)}`);
    if(cm.peak_total_mem_mb!=null)wbits.push(`both processes ${asMb(cm.peak_total_mem_mb)}`);
    if(wbits.length)h+=`<tr><td>coolwsd (MB)</td><td colspan="6" style="text-align:left">${wbits.join(", ")}</td></tr>`;}
  const ds=cm&&cm.doc_stack;
  if(ds){const tot=STACK.reduce((a,[k])=>a+(ds[k]||0),0);
    h+=`<tr><td>kit memory (MB)</td><td colspan="6" style="text-align:left">`+
      STACK.filter(([k])=>ds[k]!=null).map(([k,,name])=>`${name} ${Math.round(ds[k])}`).join(", ")+
      `, resident ${Math.round(tot)}</td></tr>`;}
  const bm=r.browser_memory;
  if(bm){
    // Read from /proc/<pid>/status: shared memory counts once per process that
    // maps it, so the total is an upper bound.
    const part=(name,g)=>{if(!g)return null;const b=[];
      if(g.rss!=null)b.push(`resident ${asMb(g.rss)}`);
      if(g.hwm!=null)b.push(`peak ${asMb(g.hwm)}`);
      if(g.anon!=null)b.push(`anonymous ${asMb(g.anon)}`);
      if(g.shmem!=null)b.push(`shared memory ${asMb(g.shmem)}`);
      if(g.pss!=null)b.push(`proportional ${asMb(g.pss)}`);
      if(g.pids)b.push(`${g.pids} process${g.pids>1?"es":""}`);
      return b.length?`${name}: ${b.join(", ")}`:null;};
    const rows=[part("page (renderer)",bm.renderer),part("GPU",bm.gpu),part("browser and utility",bm.other)].filter(Boolean);
    if(bm.peak_total_rss_mb!=null)rows.push(`all Chromium processes: resident ${asMb(bm.peak_total_rss_mb)}`);
    if(rows.length)h+=`<tr><td>browser (MB)</td><td colspan="6" style="text-align:left">${rows.join("<br>")}</td></tr>`;}
  const mc=r.machine;
  if(mc){const bits=[];const pr=mc.probe_ms;
    if(pr)bits.push(`probe ${Math.round(pr.med)} ms median, ${Math.round(pr.p90)} ms p90`+
      (mc.probe_ratio!=null?` (${mc.probe_ratio.toFixed(2)} times the usual)`:""));
    if(mc.steal_pct!=null)bits.push(`steal ${mc.steal_pct.toFixed(1)}%`);
    if(mc.busy)bits.push("<b>busy machine</b>");
    if(bits.length)h+=`<tr><td>machine</td><td colspan="6" style="text-align:left">${bits.join(", ")}</td></tr>`;}
  const ins=r.instructions;
  if(ins){h+=`<tr><td>instructions (G)</td><td colspan="6" style="text-align:left">`+
    (ins.master!=null?`coolwsd ${(ins.master/1e9).toFixed(1)}`:"")+
    (ins.kit!=null?`${ins.master!=null?", ":""}document kit ${(ins.kit/1e9).toFixed(1)}`:"")+"</td></tr>";}
  mb.innerHTML=h+"</tbody></table>";
  renderFg(runs,i,null);
  const wasOpen=!modal.hidden;
  modal.hidden=false;
  if(!wasOpen)document.getElementById("mc").focus();
}
function stepDetail(d){const j=detailIdx+d;
  if(!detailRuns||j<0||j>=detailRuns.length)return;
  openDetail(detailRuns,j);mcard.scrollTop=0;}
function close(){modal.hidden=true;}
document.getElementById("mc").addEventListener("click",close);
mprev.addEventListener("click",()=>stepDetail(-1));
mnext.addEventListener("click",()=>stepDetail(1));
modal.addEventListener("click",e=>{if(e.target===modal)close();});
document.addEventListener("keydown",e=>{
  if(!modal.hidden&&(e.key==="ArrowLeft"||e.key==="ArrowRight")){
    // leave the arrows to a focused select, such as the flamegraph comparison
    if(e.target.closest&&e.target.closest("select,input"))return;
    e.preventDefault();stepDetail(e.key==="ArrowLeft"?-1:1);return;}
  if(e.key!=="Escape")return;
  if(!modal.hidden)close();else if(fullChart)setFull(null);});

// instant hover tooltip (no native <title> delay)
const tip=document.getElementById("tip");
function placeTip(e){const pad=14,w=tip.offsetWidth,h=tip.offsetHeight;
  let x=e.clientX+pad,y=e.clientY+pad;
  if(x+w>innerWidth)x=e.clientX-pad-w; if(y+h>innerHeight)y=e.clientY-pad-h;
  tip.style.left=x+"px";tip.style.top=y+"px";}
// Whatever carries a tip, rather than the dots alone: the instruction bars and the stacked
// memory bars carry one each and used to say nothing when the pointer was on them.
function tipTarget(t){return (t instanceof Element)?t.closest("[data-tip]"):null;}
document.addEventListener("mouseover",e=>{const d=tipTarget(e.target);
  if(d){tip.textContent=d.getAttribute("data-tip")||"";tip.hidden=false;placeTip(e);}});
document.addEventListener("mousemove",e=>{if(!tip.hidden)placeTip(e);});
document.addEventListener("mouseout",e=>{if(tipTarget(e.target))tip.hidden=true;});
// also on keyboard focus, for accessibility
document.addEventListener("focusin",e=>{const d=tipTarget(e.target);
  if(d){const b=d.getBoundingClientRect();
    tip.textContent=d.getAttribute("data-tip")||"";tip.hidden=false;
    tip.style.left=(b.right+8)+"px";tip.style.top=(b.top)+"px";}});
document.addEventListener("focusout",e=>{if(tipTarget(e.target))tip.hidden=true;});
</script></body></html>
"""


def _svg_aspect(path: Path) -> float | None:
    """height/width of an SVG, from its viewBox or its width/height attributes.

    The dashboard needs it to give an embedded capture the right height: a page
    opened over file:// is an opaque origin, so its script cannot read the size
    out of the document inside an <object>, and a flamegraph left to fit a fixed
    box shrinks to a fraction of the panel width."""
    try:
        head = path.open("rb").read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'viewBox\s*=\s*"[\d.+-]+\s+[\d.+-]+\s+([\d.]+)\s+([\d.]+)"', head)
    if not m:
        w = re.search(r'\swidth\s*=\s*"([\d.]+)(?:px)?"', head)
        h = re.search(r'\sheight\s*=\s*"([\d.]+)(?:px)?"', head)
        if not (w and h):
            return None
        m = (w.group(1), h.group(1))
        wv, hv = float(m[0]), float(m[1])
    else:
        wv, hv = float(m.group(1)), float(m.group(2))
    if wv <= 0 or hv <= 0:
        return None
    return round(hv / wv, 4)


def compare_url_base() -> str:
    """The GitHub page that lists the commits between two others, for the repository in
    COOL_SRC_URL, or "" when that is not a GitHub https URL."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
                 os.environ.get("COOL_SRC_URL", ""))
    return f"https://github.com/{m.group(1)}/{m.group(2)}/compare/" if m else ""


@contextlib.contextmanager
def dashboard_lock(out: Path):
    """Hold the lock of the dashboard at @p out while its runs are read and it is written.

    The hourly run publishes into the store while a run can be deleted from the page, and
    each rebuilds the same dashboard."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out.parent / f".{out.name}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def write_dashboard(scen: dict, out: str, results_dir: Path | None = None) -> None:
    # Flamegraph paths are stored absolute; rewrite them relative to the html
    # file's directory so the links work from wherever the dashboard is opened.
    out_dir = Path(out).resolve().parent
    flames = 0
    for pts in scen.values():
        for p in pts:
            fg = p.get("flamegraph")
            if fg:
                p["fg_aspect"] = _svg_aspect(Path(fg))
                p["flamegraph"] = os.path.relpath(fg, out_dir).replace(os.sep, "/")
                flames += 1
    # The run folders are linked from the list of runs, relative to the html like the
    # flamegraphs.
    results_rel = ""
    if results_dir is not None:
        results_rel = os.path.relpath(results_dir.resolve(), out_dir).replace(os.sep, "/") + "/"
    # A reader never sees half a page, since nginx serves it while a run is published.
    tmp = Path(out).with_name(f".{Path(out).name}.tmp")
    tmp.write_text(DASHBOARD.replace("__DATA__", json.dumps(scen))
                   .replace("__COMPARE__", json.dumps(compare_url_base()))
                   .replace("__RESULTS__", json.dumps(results_rel)))
    os.replace(tmp, out)
    runs = sum(len(v) for v in scen.values())
    print(f"wrote {out}  ({len(scen)} chart(s), {runs} run(s), {flames} flamegraph link(s))")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run browser tests over time and build a dashboard.")
    ap.add_argument("--results", default="results", help="history directory (default results/)")
    ap.add_argument("--run", action="store_true", help="run the suite first (needs docker/k6)")
    ap.add_argument("--users", default="1,3", help="comma-separated user counts to run")
    ap.add_argument("--commit", help="commit label for this run (default: git HEAD of --coolwsd-dir)")
    ap.add_argument("--coolwsd-dir", default=os.environ.get("COOLWSD_DIR"),
                    help="online checkout, to read the commit under test "
                         "(default: $COOLWSD_DIR)")
    ap.add_argument("-o", "--out", default="browser-dashboard.html", help="dashboard output file")
    ap.add_argument("--flamegraphs", action="store_true",
                    help=f"capture a document-process flamegraph per test document "
                         f"({len(documents())}), each via k6-flamegraph.py launching its own "
                         "single-kit coolwsd on its own port; each run's detail then "
                         "embeds its own capture")
    ap.add_argument("--demo", action="store_true", help="use synthetic history instead of results/")
    ap.add_argument("--junit", metavar="FILE",
                    help="write a JUnit report of this run's per-document "
                         "outcomes, for a CI server to display and trend")
    ap.add_argument("--min-samples", type=int, default=0, metavar="N",
                    help="count a capture as failed when perf collected fewer "
                         "than N samples for it (0 = off). An idle kit yields a "
                         "flamegraph that renders but means nothing")
    ap.add_argument("--prune-perf-data", action="store_true",
                    help="delete each capture's perf.data once its flamegraph.svg "
                         "exists (tens of MB per document), for a history that is "
                         "kept around")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any document failed, not just when all "
                         "of them did (for CI)")
    args = ap.parse_args()

    cases: list[dict] = []
    if args.demo:
        write_dashboard(demo_history(), args.out)
    else:
        results = Path(args.results)
        if args.run or args.flamegraphs:
            if not args.coolwsd_dir:
                return ("no online checkout given: set COOLWSD_DIR (or pass "
                        "--coolwsd-dir) to the online checkout under test")
            commit = args.commit or git_commit(args.coolwsd_dir)
            runid = new_runid(commit)
            if args.run:
                if not preflight(args.coolwsd_dir):
                    return ("aborting: start Collabora Online and the WOPI host "
                            "(see the commands above), then re-run")
                cases += run_suite(results, runid,
                                   [int(u) for u in args.users.split(",") if u.strip()],
                                   commit)
            if args.flamegraphs:
                # The pass launches its own single-kit coolwsd per document (on
                # its own free port), so it needs no pre-running COOL; it just
                # needs docker/k6, perf and the FlameGraph tools.
                cases += capture_flamegraphs(results, runid, args.coolwsd_dir,
                                             commit, args.prune_perf_data,
                                             args.min_samples)
            if args.junit:
                write_junit(Path(args.junit), f"cool-perf.{runid}", cases)
        with dashboard_lock(Path(args.out)):
            scen = collect(results)
            if not scen:
                return (f"no metrics.json found under {results}/ - run the suite first "
                        f"(./browser-suite.py --run) or pass --demo")
            write_dashboard(scen, args.out, results)
    # A pass where every document failed is a broken run, not an empty one, and
    # CI wants to hear about it. --strict makes any single failure fatal.
    failed = [c["name"] for c in cases if not c["ok"]]
    if failed and (args.strict or len(failed) == len(cases)):
        return f"{len(failed)} of {len(cases)} run(s) failed: {', '.join(failed)}"
    return 0


if __name__ == "__main__":
    sys.exit(main())
