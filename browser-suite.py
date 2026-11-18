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

A document-process flamegraph can be captured per test document and user count
(--flamegraphs):
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
import concurrent.futures
import fcntl
import json
import os
import re
import shutil
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS = ("writer", "calc", "impress", "draw")
DOCS = ("blank", "blank-vector", "5mb", "5mb-vector", "10mb", "10mb-vector", "duplicate",
        "duplicate-vector", "ooxml", "ooxml-vector", "image5mb", "image5mb-vector", "finance",
        "finance-ooxml")

# What each application has to open. "ooxml" is the same content as "5mb" in the Microsoft
# format, so the two read as a comparison of the formats; Draw has no such format of its own,
# so there is no document to open for it and no chart for it either. "10mb" is the document
# one service-level requirement is written about, and "image5mb" opens a blank text document
# and inserts a 5 MB image into it, which is another, so only Writer has those two; "duplicate"
# is the deck the slide-duplication requirement is measured on, so only Impress has that one.
# A "-vector" document is the same test with vector rendering, which draws the slides or the
# text pages from the primitives the engine sends instead of from tiles, so it reads beside the
# tile one.
# Calc opens a sales ledger with a pivot table, charts and conditional formats instead of a
# 5 MB document, "finance", and "finance-ooxml" is the same workbook in the Microsoft format.
DOCS_BY_APP = {
    "writer": ("blank", "blank-vector", "5mb", "5mb-vector", "10mb", "10mb-vector", "ooxml",
               "ooxml-vector", "image5mb", "image5mb-vector"),
    "calc": ("blank", "finance", "finance-ooxml"),
    "impress": ("blank", "blank-vector", "5mb", "5mb-vector", "duplicate", "duplicate-vector",
                "ooxml", "ooxml-vector"),
    "draw": ("blank", "5mb"),
}


# Real documents that are hard on the application, which documents/fetch-reference-docs.py
# downloads into documents/ref/ (see documents/reference-docs.json for their sources). They load
# in up to a minute where the generated ones take one or two, so they are a set of their own,
# which --documents picks, rather than part of every run.
REFERENCE_DOCS_BY_APP = {
    "writer": ("rtl-book", "odf-spec", "guide", "long-table", "fonts", "big-table", "long-book"),
    "calc": ("formulas", "matrix", "countifs", "sheets", "multiline"),
    "impress": ("gifs", "yottadb", "candy", "slide-fill", "gead"),
    "draw": (),
}
DOCUMENT_SETS = ("generated", "reference", "all")


def documents(which: str = "generated") -> list[tuple[str, str]]:
    """Every (application, document) of the set @p which, in reading order."""
    sets = {"generated": [DOCS_BY_APP], "reference": [REFERENCE_DOCS_BY_APP],
            "all": [DOCS_BY_APP, REFERENCE_DOCS_BY_APP]}[which]
    return [(app, doc) for app in APPS for by_app in sets for doc in by_app[app]]

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


def render_all(out_dirs: list[Path]) -> None:
    """Draw the flamegraphs of deferred captures, several at a time. Each one is a
    perf script piped into stackcollapse-perf.pl, two processes of one thread each.
    COOL_PERF_RENDER_JOBS sets how many run at once."""
    if not out_dirs:
        return
    jobs = int(os.environ.get("COOL_PERF_RENDER_JOBS")
               or max(1, min(6, (os.cpu_count() or 2) // 2)))
    print(f"flamegraphs: drawing {len(out_dirs)}, {jobs} at a time ...")
    start = time.monotonic()

    def render(out_abs: Path) -> None:
        proc = subprocess.run(
            [sys.executable, "k6-flamegraph.py", "--render-only",
             "--out-dir", str(out_abs)],
            cwd=str(ROOT), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        with (out_abs / "harness.log").open("a") as log:
            log.write(proc.stdout)
        drawn = [l for l in proc.stdout.splitlines() if l.startswith("flamegraph: ")]
        print(f"  {out_abs.name}: " + (drawn[-1].rpartition("  ")[2] if drawn
                                         else f"failed, see {out_abs}/harness.log"))

    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        list(pool.map(render, out_dirs))
    print(f"flamegraphs: drawn in {time.monotonic() - start:.0f}s")


def capture_flamegraphs(results_dir: Path, runid: str, coolwsd_dir: str,
                        commit: str | None = None,
                        prune_perf_data: bool = False,
                        min_samples: int = 0,
                        users_list: list[int] = (1,),
                        which: str = "generated") -> list[dict]:
    """Capture one document-process flamegraph per test document and user count via
    k6-flamegraph.py. For each document it launches its own single-kit coolwsd
    (make run-one, on its own free port via --new-instance, so it coexists with
    any coolwsd already on the default port), opens that document with its
    browser test so the kitbroker_* document kit loads it, and profiles that
    kit. Output: results/<runid>/flamegraph/<app>-<doc>/flamegraph.svg for one user,
    <app>-<doc>-<n>u/ for n users, and the browser test's own metrics.json one level
    down in browser/, which is what puts this run on the dashboard as a profiled
    point with the instructions of its user count.

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
    captures = [(app, doc, users) for app, doc in documents(which) for users in users_list]
    ran = []
    for app, doc, users in captures:
            name = f"{app}-{doc}" + (f"-{users}u" if users > 1 else "")
            out_rel = (results_dir / runid / "flamegraph" / name).as_posix()
            print(f"flamegraph {name}: own single-kit cool -> {out_rel} ...")
            # --cgroup counts and records the instance by cgroup from before
            # the document opens. pid mode attaches only once the kit is
            # found, so its counts miss a varying part of the load.
            cmd = [sys.executable, "k6-flamegraph.py", test_script(app, doc),
                   "--out-dir", out_rel, "--coolwsd-dir", coolwsd_dir,
                   "--new-instance", "--cgroup", "--defer-render"]
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
            env["USERS"] = str(users)
            env["COMMIT"] = commit or ""
            with (out_abs / "harness.log").open("w") as log:
                proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, text=True,
                                        bufsize=1, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT)
                for line in proc.stdout:
                    sys.stdout.write(line)
                    log.write(line)
                rc = proc.wait()
            ran.append((name, out_rel, out_abs, rc))

    # The flamegraphs are drawn after the last test, so that the work of perf script,
    # which unwinds every sample, never runs beside a measurement.
    render_all([out_abs for _, _, out_abs, rc in ran if rc == 0])

    for name, out_rel, out_abs, rc in ran:
        svg = out_abs / "flamegraph.svg"
        samples = capture_samples(out_abs)
        thin = (min_samples > 0 and samples is not None
                and samples < min_samples)
        ok = rc == 0 and svg.is_file() and not thin
        if samples is not None:
            print(f"  {name}: {samples} perf samples")
        if not ok:
            failed.append(name)
            if rc != 0:
                why = f"exit {rc}"
            elif not svg.is_file():
                why = "no flamegraph.svg was written"
            else:
                why = f"only {samples} perf samples, under --min-samples {min_samples}"
            print(f"  flamegraph {name} failed ({why}), see "
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
        outcomes.append({"name": name, "ok": ok, "rc": rc,
                         "out_rel": out_rel,
                         "samples": samples,
                         "why": None if ok else why,
                         "log": f"{out_rel}/harness.log"})
    done = len(captures) - len(failed)
    print(f"flamegraphs: {done}/{len(captures)} captured"
          + (f", failed: {', '.join(failed)}" if failed else ""))
    return outcomes


def new_runid(commit: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{commit or 'local'}"


def run_suite(results_dir: Path, runid: str, users_list: list[int],
              commit: str | None, which: str = "generated") -> list[dict]:
    """Run every document at every user count. Returns one {name, ok, out_rel}
    record per run, a run counting as ok when it measured the document opening."""
    outcomes = []
    for app, doc in documents(which):
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
    for app, doc in documents("all"):
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

    Three figures: the master, which serves the client, the document kit,
    which does the document work, and the browser, the k6 container that runs
    Chromium. They cover the window perf was attached for,
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
    for key, field in (("coolwsd", "master"), ("kit", "kit"), ("browser", "browser")):
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

    The document figure is the private dirty memory of the kitbroker group, which is
    the process doing the document work: the figure that the admin metrics give a plain
    run. A capture from before that was sampled gives the proportional set size. The total is the
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

    kit = mem.get("kit") if isinstance(mem.get("kit"), dict) else {}
    doc_peak = kit.get("peak_private_dirty_mb")
    if doc_peak is None:
        doc_peak = peak_of("kit")
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
                    app: str, doc: str, users: int) -> tuple[str | None, bool]:
    """(absolute path to this run's flamegraph or None, was this run profiled).

    Scoped to the run on purpose: a flamegraph is only evidence about the commit
    it was captured on, so it must not be shared across runs the way it used to
    be (the newest capture was pinned to every point of that document, which read
    as if every commit had been profiled). Two shapes match. A run of the
    --flamegraphs pass sits in browser/ under the directory holding its own SVG,
    and it is profiled: perf was attached while it ran, so its times are not
    comparable with a plain run. A plain suite run picks up the capture taken for
    the same document and user count in the same runid, or else the 1-user one,
    which is the only capture of a run from before the multi-user captures."""
    here = mj.parent.parent / "flamegraph.svg"
    if mj.parent.name == "browser" and here.is_file():
        return str(here.resolve()), True
    runid = _runid_of(mj, results_dir)
    if runid:
        for name in ([f"{app}-{doc}-{users}u"] if users > 1 else []) + [f"{app}-{doc}"]:
            same_run = results_dir / runid / "flamegraph" / name / "flamegraph.svg"
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
        flame, profiled = _flamegraph_for(mj, results_dir, app, doc, users)
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
            big = {"5mb": 2.3, "ooxml": 2.3, "10mb": 3.6, "finance": 1.6,
                   "finance-ooxml": 1.6}.get(base_doc, 1.0)
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
                    if base_doc == "image5mb":
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


# The page in templates/: the html, with its css, its script and the data of the runs
# put in place of $css, $js, $data, $compare and $results.
TEMPLATES = Path(__file__).resolve().parent / "templates"


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


def add_main_parents(scen: dict, coolwsd_dir: str | None) -> None:
    """Give each run of a branch other than main the label of the newest commit of main that was
    measured and that its base descends from, as "parent". The base itself is often a commit of
    main that no run measured. The history comes from the online tree of $COOLWSD_DIR, and a
    base that tree does not hold gets no parent."""
    if not coolwsd_dir:
        return
    sha = re.compile(r"[0-9a-f]{12}")
    main = {m.group(0): p["commit"] for pts in scen.values() for p in pts
            if p.get("branch", "main") == "main" and (m := sha.match(p.get("commit") or ""))}
    parents: dict[str, str | None] = {}
    for pts in scen.values():
        for p in pts:
            base = p.get("base") or ""
            if p.get("branch", "main") == "main" or not sha.match(base):
                continue
            if base not in parents:
                parents[base] = None
                try:
                    out = subprocess.run(["git", "-C", coolwsd_dir, "rev-list",
                                          "--max-count=5000", base],
                                         capture_output=True, text=True, timeout=20).stdout
                except (OSError, subprocess.SubprocessError):
                    out = ""
                for line in out.split():
                    if line[:12] in main:
                        parents[base] = main[line[:12]]
                        break
            if parents[base]:
                p["parent"] = parents[base]


def write_dashboard(scen: dict, out: str, results_dir: Path | None = None) -> None:
    add_main_parents(scen, os.environ.get("COOLWSD_DIR"))
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
    page = string.Template((TEMPLATES / "browser-dashboard.html").read_text()).substitute(
        css=(TEMPLATES / "browser-dashboard.css").read_text(),
        js=(TEMPLATES / "browser-dashboard.js").read_text(),
        data=json.dumps(scen), compare=json.dumps(compare_url_base()),
        results=json.dumps(results_rel))
    tmp.write_text(page)
    os.replace(tmp, out)
    runs = sum(len(v) for v in scen.values())
    print(f"wrote {out}  ({len(scen)} chart(s), {runs} run(s), {flames} flamegraph link(s))")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run browser tests over time and build a dashboard.")
    ap.add_argument("--results", default="results", help="history directory (default results/)")
    ap.add_argument("--run", action="store_true", help="run the suite first (needs docker/k6)")
    ap.add_argument("--users", default="1,3", help="comma-separated user counts to run and to capture")
    ap.add_argument("--documents", choices=DOCUMENT_SETS, default="generated",
                    help="the documents to run and to capture: those of documents/gen-docs.py "
                         "(default), the real documents of documents/fetch-reference-docs.py, "
                         "or both")
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
                                   commit, args.documents)
            if args.flamegraphs:
                # The pass launches its own single-kit coolwsd per document (on
                # its own free port), so it needs no pre-running COOL; it just
                # needs docker/k6, perf and the FlameGraph tools.
                cases += capture_flamegraphs(results, runid, args.coolwsd_dir,
                                             commit, args.prune_perf_data,
                                             args.min_samples,
                                             [int(u) for u in args.users.split(",")
                                              if u.strip()], args.documents)
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
