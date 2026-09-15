# Running the performance suite from Jenkins

This describes how to run the Collabora Online browser performance suite as a
Jenkins job on a VM, how to configure it, and how to keep its results and
flamegraphs available after the workspace is gone.

The job **does not build Collabora Online**. It measures a build that already
exists, which keeps a measurement job from turning into a build job and keeps
the numbers attributable to one known commit. See
[Where the build comes from](#where-the-build-comes-from).

## What one run produces

For each of the 12 test documents (writer, calc and impress each blank, 5 MB
and 5 MB in the Microsoft format, draw blank and 5 MB, and the 10 MB Writer
document the service-level requirement is written about):

* page-load, frame-ready and edit times, plus the browser JS heap, from the k6
  browser test
* Collabora Online peak memory for the document, sampled from the admin
  metrics endpoint while the test runs
* a **document flamegraph**: `perf` recording the `kitbroker_*` document kit
  while that document is open, captured against a single-kit coolwsd of its own

and, over the accumulated history:

* `index.html`, a dashboard with one chart per document and user pool, plotting
  times and memory across commits, where each run point opens its own capture
  inline and can put a second run beside it
* `junit.xml`, one testcase per document, so Jenkins shows which document
  regressed, failed to capture, or missed the service level it has to meet

## The pieces

| file | what it does |
| --- | --- |
| `cool-ci` | the CI entry point: `doctor`, `run`, `publish` |
| `browser-suite.py` | runs the tests, captures the flamegraphs, writes the dashboard and the JUnit report |
| `k6-flamegraph.py` | one document: starts its own coolwsd and WOPI host, drives the k6 test, records perf, renders the SVG |
| `cool-use-build` | installs a kept-aside build into the tree, and prints the coolwsd command line that skips make |
| `k6-run`, `k6-wrap` | run a k6 test in the `grafana/k6:1.7.1-with-browser` container |

## Machine setup

One VM, dedicated. Timing measurements are only comparable when nothing else
competes for the machine, so do not put this agent on a shared builder and do
not let the job run concurrently with itself.

### Packages

Written for Debian 13 (trixie). Ubuntu 24.04 and later takes the same packages,
and the differences that matter are called out where they arise. The tooling
here is Python standard library only, so the packaged `python3` is enough on
either.

```
# Debian 13 (trixie), Ubuntu 24.04 and later
apt install docker.io linux-perf nodejs npm python3 iproute2 git
# Arch
pacman -S docker perf nodejs npm python iproute2 git

# FlameGraph must be on PATH (stackcollapse-perf.pl, flamegraph.pl)
git clone https://github.com/brendangregg/FlameGraph /opt/FlameGraph
ln -s /opt/FlameGraph/stackcollapse-perf.pl /opt/FlameGraph/flamegraph.pl /usr/local/bin/

usermod -aG docker jenkins        # the k6 tests run in a container
systemctl enable --now docker
```

Two Debian details decide whether the suite runs at all.

**Node has to be 20.10 or newer**, which is what `server/index.js` asks for.
Trixie ships the 20 series and bookworm ships 18, so on bookworm take Node from
backports or from nodesource, or the WOPI host will not start and every
scenario will time out waiting for a document:

```
node --version
```

**`perf` is a wrapper.** Debian's `linux-perf` installs `/usr/bin/perf`, which
dispatches to the binary built for the running kernel. After a kernel upgrade
with no reboot it has no exact match, warns, and can refuse to record. Reboot
into the kernel the package was built for, and check what the capture will
really use:

```
perf --version
perf record -o /dev/null -- true && echo "perf records"
```

### Kernel settings

```
# /etc/sysctl.d/90-cool-perf.conf
kernel.perf_event_paranoid = 2
```

The flamegraph pass records its own coolwsd processes by pid, and that is
unprivileged at `perf_event_paranoid <= 2`, so it needs no `sudo` and no
`NOPASSWD` sudoers entry. Check what the machine actually has, since
distributions have shipped stricter defaults than upstream in the past:

```
sysctl kernel.perf_event_paranoid
```

Anything at 2 or below is enough. Only a system-wide capture would want `0` or
less, and the suite never asks for one. `cool-ci doctor` reports this check as
`ok` from 2 downwards, and a timing-only run (`--no-flamegraphs`) does not need
perf at all.

The document kit jails itself with unprivileged user namespaces. Debian allows
them out of the box, so nothing is needed there unless the machine has been
hardened. Where they are restricted, allow them:

```
kernel.unprivileged_userns_clone = 1                  # where present
kernel.apparmor_restrict_unprivileged_userns = 0      # Ubuntu 24.04 and later
```

Add a line only for a key the kernel actually has. `sysctl --system` fails on an
unknown key and stops there, so a stray AppArmor line on a Debian kernel without
it costs you the `perf_event_paranoid` setting in the same file:

```
sysctl kernel.apparmor_restrict_unprivileged_userns   # silence means: leave it out
```

`./cool-ci doctor` checks all of this, including an actual `unshare -U`.

### Disk

A run is about 40 to 60 MB while it is being made, mostly `perf.data`. The CI
path prunes `perf.data` once the SVG exists, so a published run is about 5 to
8 MB (the 8 SVGs plus metrics and logs). With the default retention of 20 runs
the store settles around 100 to 160 MB. Keep a few GB free for the working
copy of a run.

### The store

```
install -d -o jenkins -g jenkins /var/lib/cool-perf
```

This is the durable half and it must live outside the job workspace, which
Jenkins wipes. See [Exposing the results](#exposing-the-results-durably).

## Where the build comes from

`COOLWSD_DIR` points at a **configured, already built** online tree. `doctor`
fails the job unless all four of these are in place, because they are what a
run loads:

```
$COOLWSD_DIR/coolwsd            server binary
$COOLWSD_DIR/systemplate        jail template
$COOLWSD_DIR/engine/instdir     engine (LibreOffice) runtime
$COOLWSD_DIR/browser/dist       browser client bundle
```

Typical arrangements:

* `cool-build` builds the tree from main on this machine, which is what the
  pipeline below does
* an upstream build job on the same VM leaves its tree at a fixed path, and this
  job runs after it
* the tree is built by hand for a measurement campaign and left in place
* a **kept-aside build** is installed into the tree by `cool-use-build`, to
  measure an older commit without rebuilding it

### Building from main on the agent

```
COOLWSD_DIR=/srv/online ./cool-build              # origin/main
COOLWSD_DIR=/srv/online ./cool-build --ref v25.04 # any ref
COOLWSD_DIR=/srv/online ./cool-build --engine     # the engine too, hours
```

It fetches, checks the ref out detached, runs `make build-nocheck`, snapshots
the result under its commit, and marks the tree with the same id. The build
then **is** the testing instance: `cool-ci run` measures the tree as it stands,
and labels the run with the commit that was built.

It refuses to start when the tree has uncommitted changes, since a build from
main over someone's work in progress measures neither.

**What it pulls.** The fetch asks for the one branch it is about to build and
no tags, where a plain `git fetch origin` takes every branch the remote has and
all 755 tags. Nothing in the build needs a tag: `configure.ac` and `Makefile.am`
read `git log -1 --format=%h` for the version and nothing else, so a tagless
tree still reports the right hash.

When the tree does not exist, `COOL_SRC_URL` makes `cool-build` clone it
blobless (`--filter=blob:none --no-tags --single-branch`): the history arrives
without file contents and git fetches the blobs of the one commit checked out,
a few hundred MB against 2.6 GB for the full pack. Later builds then fetch only
what the new commits touch.

`build-nocheck` is `all-am` without `check`, so the unit tests and the linters
do not run. A measurement job wants the binaries; whether they pass their own
tests is another job's question.

The engine under `engine/instdir` is left alone unless `--engine` is given. It
is a LibreOffice core build of several hours and it moves far less often than
the server, so a nightly performance job should not rebuild it. When the engine
does move, that run has to be read as a different baseline, not as a regression
in the server.

Each build snapshot is about 4 GB (`coolwsd`, `instdir/`, `browser-dist/`), so a
job that builds every night fills a disk quickly. `cool-use-build list` shows
what is kept and `--no-save` skips the snapshot for a build nobody will want to
measure again.

### How runs are labelled

The label decides which point on the chart a run becomes, so it has to describe
the binaries rather than the checkout:

1. `COOL_PERF_LABEL`, if set
2. the id in `$COOLWSD_DIR/.cool-build-installed`, written by `cool-use-build`
   when it installs a snapshot, and by `cool-build` for a build it just made
3. `git -C $COOLWSD_DIR rev-parse --short=12 HEAD`

Step 2 exists because after installing a snapshot the tree's `HEAD` no longer
describes the binaries. Pulling the source without rebuilding has the same
effect, so a job that measures a hand-built tree should set `COOL_PERF_LABEL`
explicitly if the checkout can move under it.

### Measuring an older build

```
./cool-use-build list                   # what snapshots exist
./cool-use-build save                   # snapshot the tree's build under its HEAD
./cool-use-build mark <id>              # label the build already in the tree, copying nothing
./cool-use-build use 093eaf495c27       # install one (saving the current one first)
./cool-ci run                           # labels the runs 093eaf495c27 automatically
```

`use` always snapshots what it is about to overwrite, so a swap cannot cost you
a rebuild. Each swap copies a few GB.

### Why make never runs

`make run-one` depends on `all`. A restored binary is older than the tree's
current object files, so make would relink it and the job would profile the
current source while labelling it with the old commit. `cool-ci` therefore
starts coolwsd from the command line `make run-one` would have executed, read
out of the tree's own Makefile by `cool-use-build run-cmd`, with no make in the
loop. Override it with `COOL_CMD` if you need something else.

## Configuration

All environment, so the Jenkins job needs no arguments.

| variable | default | meaning |
| --- | --- | --- |
| `COOLWSD_DIR` | (required) | the prebuilt online tree to measure |
| `COOL_PERF_STORE` | `/var/lib/cool-perf` | durable results store |
| `COOL_PERF_KEEP` | `20` | runs to keep in the store, oldest pruned |
| `COOL_PERF_LABEL` | (unset) | label runs with this instead of the built commit |
| `COOL_PERF_USERS` | `1,3` | user counts for the timing suite |
| `COOL_PERF_MIN_SAMPLES` | `30` | a capture with fewer perf samples counts as failed |
| `COOL_BUILDS_DIR` | `cool-builds` beside the tree | where snapshots live |
| `COOL_SRC_REF` | `origin/main` | what `cool-build` builds |
| `COOL_BUILD_JOBS` | `nproc` | make parallelism for `cool-build` |
| `COOL_SRC_URL` | (unset) | clone from here when the tree does not exist yet |
| `COOL_CMD` | derived from the Makefile | command that starts coolwsd |
| `PERF_EVENT` | `cycles` | sampling event, self-tested and downgraded if it captures nothing |
| `HOST_IP` | auto-detected | address the k6 container reaches the host on |
| `WOPI_PORT` | `3000` | port for the test WOPI host |

Command line flags for `cool-ci run`: `--no-suite`, `--no-flamegraphs`,
`--strict` (fail the build on any single document), `--junit PATH`,
`--min-samples N`.

## Running it

```
./cool-ci doctor        # every prerequisite, exits non-zero with what to fix
./cool-ci run           # doctor, measure, publish
./cool-ci publish       # re-publish the newest run in results/ (rarely needed)
```

`run` does, in order:

1. `doctor`, and stops if anything fails
2. resolves the build label and the coolwsd command line
3. starts coolwsd and the test WOPI host over HTTPS, discovers the port coolwsd
   bound (it uses `--find-free-port`), and runs the timing suite against them
4. stops them, then captures the 8 flamegraphs, each with its own single-kit
   coolwsd on its own port
5. writes `ci-logs/junit.xml`, publishes the run into the store and rebuilds
   `$COOL_PERF_STORE/index.html`

Both phases go into one run directory, `results/<timestamp>-<label>/`, so a
capture attaches to the timing points of the same run.

The WOPI host has to be HTTPS because coolwsd is: the host page refuses to
submit when the two schemes differ, and the browser test then waits forever for
a `Frame_Ready`.

### Jenkinsfile

```groovy
pipeline {
  agent { label 'cool-perf-vm' }

  options {
    disableConcurrentBuilds()          // timings are only comparable alone on the box
    timeout(time: 180, unit: 'MINUTES')   // a build from main, then the suite
    buildDiscarder(logRotator(numToKeepStr: '100'))
  }

  environment {
    COOLWSD_DIR     = '/srv/online'    // the tree Build COOL builds into
    COOL_SRC_REF    = 'origin/main'
    COOL_PERF_STORE = '/var/lib/cool-perf'
    COOL_PERF_KEEP  = '20'
  }

  stages {
    stage('Checkout')      { steps { checkout scm } }
    stage('Test bundles')  { steps { sh 'npm ci && npm run build' } }
    stage('Build COOL')    { steps { sh './cool-build' } }
    stage('Doctor')        { steps { sh './cool-ci doctor' } }
    stage('Measure')       { steps { sh './cool-ci run' } }
  }

  post {
    always {
      junit allowEmptyResults: true, testResults: 'ci-logs/junit.xml'
      archiveArtifacts allowEmptyArchive: true, artifacts: '''
        ci-logs/**,
        results/**/flamegraph.svg,
        results/**/metrics.json,
        results/**/harness.log'''
    }
    cleanup {
      sh 'pkill -f coolwsd || true'    // belt and braces, the harness stops its own
    }
  }
}
```

`npm ci && npm run build` is needed in every workspace: `dist/` is generated and
not committed, and `doctor` fails without it.

### Two jobs: build upstream, measure downstream

One job that builds and measures is the simple arrangement. Splitting it lets a
build run without measuring, lets a measurement re-run without rebuilding, and
gives each half its own history in Jenkins.

Both jobs have to run on the same agent. The build is a 4 GB tree on that
machine and the measurement reads it in place, so nothing is copied between
them: the downstream job learns which commit it is measuring by reading
`$COOLWSD_DIR/.cool-build-installed`, which `cool-build` wrote.

The upstream job, `cool-build-main`:

```groovy
pipeline {
  agent { label 'cool-perf-vm' }

  options {
    disableConcurrentBuilds()
    lock('cool-perf-vm')               // never build while a measurement runs
    timeout(time: 150, unit: 'MINUTES')
  }

  environment {
    COOLWSD_DIR  = '/srv/online'
    COOL_SRC_REF = 'origin/main'
  }

  stages {
    stage('Checkout') { steps { checkout scm } }
    stage('Build')    { steps { sh './cool-build' } }
  }

  post {
    success {
      // The commit that was built, for the build page and for anyone reading
      // the job history later.
      sh 'cat $COOLWSD_DIR/.cool-build-installed'
      build job: 'cool-perf', wait: false
    }
  }
}
```

The downstream job, `cool-perf`, is the pipeline above with the `Build COOL`
stage removed and a trigger added:

```groovy
  options {
    disableConcurrentBuilds()
    lock('cool-perf-vm')               // the build waits while this runs
    timeout(time: 40, unit: 'MINUTES')
  }

  triggers {
    upstream(upstreamProjects: 'cool-build-main',
             threshold: hudson.model.Result.SUCCESS)
  }

  stages {
    stage('Checkout')      { steps { checkout scm } }
    stage('Test bundles')  { steps { sh 'npm ci && npm run build' } }
    stage('Doctor')        { steps { sh './cool-ci doctor' } }
    stage('Measure')       { steps { sh './cool-ci run' } }
  }
```

`lock` comes from the Lockable Resources plugin and it is the part that matters.
Timings are only comparable when nothing else uses the machine, and a compile at
`-j12` is the loudest neighbour there is, so the two jobs must never overlap.
`disableConcurrentBuilds` alone does not give you that, since it only keeps a
job from running beside itself.

Keeping both the trigger and the explicit `build job:` call is deliberate
belt and braces: either one alone starts the measurement, and Jenkins will not
start it twice while the lock is held.

The downstream job still runs on its own whenever you want, and then it measures
whatever build is installed in the tree, labelled with the id in the marker.


A thin or failed capture shows up as a failing JUnit testcase, which Jenkins
reports as **unstable** rather than failed. The build only fails outright when
every document failed, or when you pass `--strict`.

The 10 MB Writer document also carries a service level: its 98th percentile of
document-loaded has to stay under 5 seconds (see "The service-level
requirement" in the README). A run that opens the document but misses that budget fails its
testcase the same way, with the measured percentile in the message. A suite
pass opens it a handful of times per user pool, which is too few for a 98th
percentile to mean anything, so treat those as a smoke test and run the
document explicitly for the answer.

## Exposing the results durably

The workspace is disposable, so nothing that matters is kept in it. `publish`
copies each run into the store and rebuilds the dashboard there:

```
$COOL_PERF_STORE/
  index.html                     the dashboard, over the whole retained history
  last-run.json                  runid, label, publish time, runs in store
  junit/<runid>.xml              per-run JUnit reports
  results/<runid>/               one directory per run
    <app>-<doc>-<n>u/metrics.json          timing suite points
    flamegraph/<app>-<doc>/flamegraph.svg  the capture
    flamegraph/<app>-<doc>/browser/metrics.json
    flamegraph/<app>-<doc>/harness.log
```

`index.html` is written at the top of the store on purpose: the dashboard links
its captures with paths relative to itself, so the store directory is
self-contained and can be copied or served as it stands.

### Serving it

The dashboard is static HTML with no external dependencies. Any web server
works:

```nginx
server {
    listen 80;
    server_name cool-perf.example.com;
    root /var/lib/cool-perf;
    index index.html;
    autoindex on;                 # lets you browse older runs and their logs
}
```

Point the job's description at that URL and every build refreshes it in place.

Alternatives, in rough order of durability:

* **web server on the VM** (above): survives Jenkins upgrades and job deletion,
  and is the only option that keeps the whole history browsable
* **`publishHTML`** (HTML Publisher plugin): convenient link from the build page,
  but it needs `reportDir` inside the workspace, so publish the workspace copy
  rather than the store, and it keeps only what each build produced
* **`archiveArtifacts`** (in the Jenkinsfile above): keeps that build's SVGs and
  metrics with the build, subject to `buildDiscarder`. Good for going back to a
  specific build, not for the trend
* **JUnit**: the per-document pass and fail trend across builds, kept by Jenkins
  itself

Retention is set by `COOL_PERF_KEEP` (store) and `buildDiscarder` (Jenkins)
independently. If you need history beyond the store window, copy or rsync
`$COOL_PERF_STORE` somewhere backed up: it is small once `perf.data` is pruned.

## Reading the dashboard

* Each point is one run of one document at one user count. The x axis is run
  order, not commit order, so a re-measured old build lands on the right.
* **Ringed** points were themselves captured under perf. Profiling inflates
  their own times, so do not read them as a regression against unprofiled runs.
* Clicking a point opens that run's detail, with its own flamegraph inline and a
  picker to put another run of the same document beside it.
* The badge on each chart (`flame 2/2`, `no flame`) says how many of that
  document's runs were profiled.
* A capture belongs only to the run it was taken in. Nothing is shared across
  runs, so an empty spot means that commit was never profiled.

## Timings

| phase | duration |
| --- | --- |
| timing suite, 8 documents x 2 user counts | about 2 minutes |
| flamegraph pass, 8 documents | 5 to 6 minutes (45 to 60 s each) |
| publish and dashboard | seconds |

A full job is comfortably under 15 minutes, which is why the Jenkinsfile sets a
40 minute timeout rather than something tight: a document that hangs is caught
by the harness itself, and the timeout is only a backstop.

## Troubleshooting

| symptom | cause and fix |
| --- | --- |
| `doctor` fails on `docker daemon` | the agent user is not in the `docker` group, or the daemon is not running |
| `doctor` fails on `perf permission` | `kernel.perf_event_paranoid` is above 1 and sudo would prompt. Set the sysctl to `-1` |
| `doctor` fails on `user namespaces` | the kit cannot build its jail. Check `kernel.apparmor_restrict_unprivileged_userns` and `kernel.unprivileged_userns_clone` |
| `doctor` warns about leftover coolwsd | a previous run leaked instances. `pkill -f coolwsd`. They hold their ports and compete with the profiled kit |
| every document fails, `coolwsd.log` ends at the banner | coolwsd started but nothing reached it. Usually the WOPI host: it must be HTTPS and reachable from inside the k6 container, so check `HOST_IP` |
| captures fail with almost no samples | the kit was idle during the window. `EDIT=1` is set by the pass for this reason. `--min-samples` catches it. The draw documents are the weakest here (7 to 17 samples seen), so expect draw to go unstable before the others |
| flamegraph frames resolve to `:tid` with no symbols | perf could not read the jailed kit's binaries. This is what `--new-instance` plus pid targeting avoids, so check that the capture really targeted `kit_*` and not the whole tree |
| the numbers moved but the commit did not | make relinked a restored snapshot. Use `cool-ci`, which never runs make, and check `.cool-build-installed` against the label on the chart |
| no hardware events on the VM | without PMU passthrough, `cycles` cannot be opened. The harness self-tests and falls back to `task-clock`, which is fine for the shape of a flamegraph |
| k6 container cannot reach the host | `k6-wrap` uses `--network host` because the default bridge fails to create veth pairs on some hosts. Override with `COOL_K6_DOCKER_NETWORK` if your setup differs |

## See also

* `PERFORMANCE_TESTING.md`: the suite itself, its scenarios and how to point it
  at a deployment
* `./cool-ci --help`, `./cool-use-build --help`, `./browser-suite.py --help`,
  `./k6-flamegraph.py --help`
