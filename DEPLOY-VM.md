# Deploying the performance suite on a VM

This sets up one Ubuntu 26.04 machine that builds Collabora Online `main` every
hour when it has a new commit, runs the performance suite against that build, and serves the dashboard
over HTTP. No Jenkins is involved: a systemd timer starts the run. To drive the
same suite from Jenkins instead, see `CI-JENKINS.md`, which describes the same
tools.

The machine is set up in four steps, each a single command:

1. Put this repository on the machine.
2. `deploy/setup-vm`, as root: packages, kernel settings, the service user,
   the timer and the web server. About 15 minutes.
3. `deploy/bootstrap`, as the service user: the first build of the engine and
   the server. Several hours.
4. Check the result, and open the dashboard.

Everything after that runs by itself.

## What is installed where

| path | content |
| --- | --- |
| `/home/collabora/` | home of the `collabora` user, which runs everything |
| `/home/collabora/perf-dashboard/` | this repository, the checkout the timer runs |
| `/home/collabora/online/` | the online tree that is built and measured, with the engine in `engine/` |
| `/var/lib/cool-perf/` | the results store: every published run, and `index.html`, the dashboard |
| `/etc/cool-perf.env` | the configuration, see [Configuration](#configuration) |
| `/etc/systemd/system/cool-perf.{service,timer}` | the hourly job |
| `/etc/systemd/system/cool-perf-api.service` | deletes runs and queues changes from the Schedule tab |
| `/etc/systemd/system/cool-perf-queue.service` | builds and measures the changes that the Schedule tab queued |
| `/etc/nginx/sites-available/cool-perf.conf` | serves `/var/lib/cool-perf` on port 80 |
| `/etc/sysctl.d/90-cool-perf.conf` | the kernel settings the suite needs |
| `/opt/FlameGraph/` | the scripts that turn perf output into flamegraphs |

The files that `setup-vm` installs come from `deploy/` in this repository, so a
change goes into the repository first, and then `setup-vm` is run again.

## The machine

A VM, used for nothing else. Timings are only comparable when nothing else
uses the machine. The build runs before the measurement, never at the
same time.

| resource | minimum | why |
| --- | --- | --- |
| vCPUs | 8 | the engine build; the measurement itself uses few |
| memory | 16 GB, 32 GB better | linking the engine with symbols |
| disk | 170 GB | engine build with symbols (about 60 GB), ccache (50 GB), online, the store, the k6 image |

On incus, a VM with those resources:

```
incus launch images:ubuntu/26.04 cool-perf --vm \
    -c limits.cpu=8 -c limits.memory=32GiB -d root,size=170GiB
```

Without `-d root,size=...`, incus gives a VM a 10 GB disk. `setup-vm` stops
when `/` is smaller than 120 GB. To grow the disk of an existing VM, run this on
the host:

```
incus config device override cool-perf root size=150GiB
```

Use `incus config device set` instead when the VM already has its own `root`
device. Then restart the VM, or grow the partition and the filesystem from
inside it, with `growpart /dev/sda 2` and `resize2fs /dev/sda2`. `lsblk` shows
which partition holds `/`.

`limits.cpu=8` lets the 8 vCPUs float over the host's cores. A range such as
`limits.cpu=0-7` pins them to fixed cores, which makes the numbers steadier
when other instances run on the same host.

**Use a VM rather than a container.** In a container the kernel belongs to the
host, so the two kernel settings must be made on the host, and docker needs
`security.nesting=true` on the instance. `setup-vm` detects a container and
prints what to set on the host, but a VM needs none of this.

A VM normally has no access to the CPU's hardware counters. perf then cannot
sample on `cycles`, and the suite falls back to `task-clock` by itself. The
flamegraphs keep their shape, but the instruction counts on the dashboard stay
empty.

## 1. Put this repository on the machine

The repository is private on gitlab.collabora.com, so the machine cannot clone
it anonymously. Choose one of the two ways.

### From your workstation, with a bundle

A bundle is a file that holds a branch and its history. It needs no access from
the VM to GitLab.

```
git -C ~/path/to/perf-dashboard bundle create /tmp/perf-dashboard.bundle main
incus file push /tmp/perf-dashboard.bundle cool-perf/tmp/
incus shell cool-perf
git clone -b main /tmp/perf-dashboard.bundle /home/collabora/perf-dashboard
```

The bundle carries commits only, so changes that are not committed in your
checkout do not reach the VM.

To update the VM later, make a new bundle the same way and pull it on the VM:

```
sudo -iu collabora git -C /home/collabora/perf-dashboard pull /tmp/perf-dashboard.bundle main
```

### From GitLab, with a deploy token

A deploy token gives read access to this one repository and nothing else. Use
it when the VM can reach gitlab.collabora.com and the hourly job should take
the latest commit by itself.

1. In GitLab, open the project, then Settings > Repository > Deploy tokens.
   Create a token with the `read_repository` scope only.
2. On the VM, clone as the service user, and let git store the token, so
   that the `git pull` of each run does not prompt:

   ```
   sudo -iu collabora
   git config --global credential.helper store
   git clone https://gitlab.collabora.com/productivity/perf-dashboard.git
   chmod 600 ~/.git-credentials
   ```

   Give the token's user name, such as `gitlab+deploy-token-12`, as the user
   name, and the token as the password. Do not put the token in the URL: git
   would write it into `.git/config`.
3. Set `COOL_PERF_SELF_UPDATE=1` in `/etc/cool-perf.env`.

## 2. Prepare the machine

As root, in the VM (`incus shell cool-perf` gives a root shell):

```
/home/collabora/perf-dashboard/deploy/setup-vm
```

It does the following, and prints a heading for each step:

- stops when `/` is smaller than 120 GB, see [The machine](#the-machine)
- writes a line in `/etc/gai.conf` that prefers IPv4, when the machine has an
  IPv6 address but no IPv6 route out. Otherwise every download of the engine
  build waits for the IPv6 timeout first
- enables the source package lists and installs the build dependencies of
  Ubuntu's own `libreoffice` package, which is what the engine needs
- installs the server's build dependencies, node, docker, perf, nginx, and a
  system LibreOffice that generates the `.docx`, `.xlsx` and `.pptx` documents
  of the corpus
- installs FlameGraph in `/opt/FlameGraph`
- writes `/etc/sysctl.d/90-cool-perf.conf` and applies it:
  `kernel.perf_event_paranoid = 0` lets perf count and record the cgroup of
  the suite's coolwsd, `kernel.kptr_restrict = 0` lets perf name the kernel
  functions in that recording, `kernel.perf_event_mlock_kb = 32768` gives the dwarf
  call graphs their ring buffer, and `kernel.apparmor_restrict_unprivileged_userns = 0` lets the document kit
  build its jail. Ubuntu restricts all three by default
- adds the `collabora` user to the `docker` group, because the k6 browser
  tests run in a container. It creates the user if it does not exist yet
- copies `deploy/cool-perf.env` to `/etc/cool-perf.env`, only if that file does
  not exist yet
- installs and enables the timer, and the nginx site

It is safe to run again. Use `--no-packages` to skip the apt step when only
the files in `deploy/` changed.

## 3. The first build

As the service user. The engine build takes several hours, so start it in
`tmux` and detach:

```
sudo -iu collabora
tmux new -s bootstrap
~/perf-dashboard/deploy/bootstrap
```

Detach with Ctrl+B then D. Reattach with `tmux attach -t bootstrap`.

`bootstrap` does, in order:

1. `npm ci && npm run build`, which builds the k6 tests into `dist/`
2. the TLS certificate of the test WOPI host, in `server/`
3. the document corpus in `documents/`, with `documents/gen-docs.py`, including
   the 10 MB report that the load-time requirement is measured on
4. `docker pull` of the k6 image
5. a blobless clone of online into `/home/collabora/online`, from the public
   GitHub mirror
6. `autogen.sh` for the engine, then the engine build. The server's
   configure needs the built engine. See [The build](#the-build)
7. `autogen.sh` for the server, then `cool-build`, which builds it and labels
   the tree with the commit it built
8. `cool-ci doctor`, which checks everything a run needs

The hourly job refuses to run until `bootstrap` has finished. The two take
the same lock, so a timer run cannot start while `bootstrap` builds.

When a step fails, fix the cause and run `bootstrap` again. Each step that is
already done is skipped.

## 4. Check it, and run once

The last thing `bootstrap` prints is the `doctor` table. All lines should be
`ok`. One `warn` is expected when the VM cannot sample on hardware counters.

Then run the job once, rather than waiting for the next hour:

```
sudo systemctl start cool-perf.service
journalctl -u cool-perf.service -f
```

`systemctl start` returns when the run is finished. The first run has
nothing new to build, so it takes about 15 to 20 minutes. Check that the timer is waiting for
its next hour:

```
systemctl list-timers cool-perf.timer
```

## Seeing the dashboard

nginx serves `/var/lib/cool-perf` on port 80 of the VM. The dashboard is
`index.html`, and every older run is browsable below it. How you reach it
depends on where you are.

**From the incus host.** The VM has an address on the incus bridge:

```
incus list cool-perf -c n4
```

Open `http://<that address>/` in a browser on the host.

When the VM's only network device is on a macvlan network, the host cannot
reach it: macvlan does not pass traffic between a host and its own guests.
Give the VM a second device on a bridge, and use the address it gets there:

```
incus config device add cool-perf eth1 nic network=incusbr0
```

`incus network list` names the bridges. Restart the VM if `ip -4 addr` in the
VM shows no address on the new device.

**From another machine, through the host.** If you can ssh to the incus host,
forward a local port:

```
ssh -L 8080:<VM address>:80 <incus host>
```

and open `http://localhost:8080/`.

**From the network, permanently.** An incus proxy device publishes the VM's
port 80 on a port of the host. For a VM it needs `nat=true` and a fixed address
on the instance's network device:

```
incus config device override cool-perf eth0 ipv4.address=<VM address>
incus config device add cool-perf dashboard proxy nat=true \
    listen=tcp:<host address>:8080 connect=tcp:<VM address>:80
```

The dashboard is then at `http://<host address>:8080/`. It has no login, so
publish it only on a network where everyone may see it: whoever opens it can
also delete runs.

**Deleting runs.** The history of the Schedule tab lists every run with its
date, branch, the commits it measured and a link to its folder. Ticked runs
are deleted with the button above the list: `cool-perf-api.service` runs
`deploy/runs-api.py`, a small Flask service on `127.0.0.1:8090`
(`COOL_PERF_API_PORT`), and nginx passes `/api/` to it. It removes the run
folders and their JUnit reports, then rebuilds `index.html`. Its log is
`journalctl -u cool-perf-api.service`. A copy of the store opened from disk
cannot delete anything.

Deleting the last run of main does not make the next hour measure that commit
again, because `last-runs.json` still names it. Set
`COOL_PERF_SKIP_UNCHANGED=0` for one run, or use `deploy/rerun`.

**With no network access at all.** The dashboard is static, and it links every
run with paths relative to itself, so a copy of the store works anywhere:

```
incus file pull -r cool-perf/var/lib/cool-perf ./cool-perf-store
xdg-open ./cool-perf-store/index.html
```

## Configuration

`/etc/cool-perf.env` holds the settings, one `NAME=value` per line. It starts
as a copy of `deploy/cool-perf.env`, which explains each one:

| variable | default | meaning |
| --- | --- | --- |
| `COOLWSD_DIR` | `/home/collabora/online` | the online tree that is built and measured |
| `COOL_SRC_URL` | GitHub mirror of online | where the tree is cloned from the first time |
| `COOL_SRC_REF` | `origin/main` | what each run builds |
| `COOL_PERF_STORE` | `/var/lib/cool-perf` | the published runs and the dashboard |
| `COOL_PERF_KEEP` | `240` | how many runs the store keeps, about 2 GB |
| `COOL_PERF_SCHEDULE_DOMAINS` | `collabora.com,collabora.co.uk` | whose patch sets the Schedule tab accepts |
| `COOL_PERF_HEAPTRACK` | `/opt/heaptrack/bin/heaptrack` | the heaptrack of a heap profile queued from the Capacity tab |
| `COOL_PERF_PROBE_MAX` | `1.12` | a run is busy when the machine probe is slower than usual by this factor |
| `COOL_PERF_STEAL_MAX` | `2` | a run is busy when the hypervisor took more than this percentage of the CPU time |
| `COOL_PERF_BUSY_RETRIES` | `3` | busy runs of a commit after which the commit is not measured again; each is published with a busy mark |
| `COOL_PERF_SKIP_UNCHANGED` | `1` | skip the build and the measurement while `COOL_SRC_REF` is at the commit of the last published run |
| `COOL_PERF_ENGINE` | `1` | rebuild the engine at each run, see below |
| `COOL_PERF_SAVE_BUILDS` | `0` | keep each run's build as a 4 GB snapshot |
| `COOL_BUILDS_DIR` | `/home/collabora/cool-builds` | where snapshots go |
| `COOL_PERF_SELF_UPDATE` | `0` | `git pull` this checkout before each run |

Every variable listed in `CI-JENKINS.md`, such as `COOL_PERF_USERS`, can be
added to this file as well. A change takes
effect at the next run, with no restart.

The time of the run is in the timer. To move it, override the timer rather
than editing the installed file, which `setup-vm` rewrites:

```
sudo systemctl edit cool-perf.timer
```

```
[Timer]
OnCalendar=
OnCalendar=*-*-* 02:00:00
```

The empty `OnCalendar=` clears the hourly entry first. A run takes about 20
minutes when the engine build is incremental. A run that is still going when
the timer fires again holds the lock, so the new run exits at once.

## The build

The engine is configured like a production engine, with
`--with-distro=CPLinux-LOKit`: optimised, and with symbols, so that the
flamegraphs show function names. Packaging and translations are turned off,
because a measurement never loads them. `--with-system-openssl` links the
OpenSSL of the distribution, built with its assembly code, so the engine builds
no OpenSSL of its own, and coolwsd encrypts with the AES instructions of the
CPU, as a server with the distribution's OpenSSL does. `--enable-python=system`
and `--with-system-nss` take Python and NSS from the distribution too: no
measurement runs a script or signs a document, and both take long to build.
The libraries that render and lay out text, such as cairo, pixman, freetype,
harfbuzz and icu, stay bundled, as production ships them.

The server is configured with `--enable-debug` and `-O2 -g`. The debug build is
required: `cool-ci` starts coolwsd with `--find-free-port`, and the flamegraph
pass adds `--singlekit`, and coolwsd only accepts these two options in a debug
build. The `-O2` keeps the code optimised; `--enable-developer` would compile
it at `-O0`. A debug coolwsd also logs at trace level, which costs some time in
every number, but costs it the same way in every run.

`ENGINE_AUTOGEN_ARGS` and `ONLINE_AUTOGEN_ARGS` in `/etc/cool-perf.env`
replace these options when `bootstrap` is run. To change the configuration of
a tree that is already configured, run `bootstrap --reconfigure` as the service
user. It waits for the run that holds the lock, configures and builds the
engine and the server again, and holds the lock until both are built, so no run
measures a tree that is half rebuilt. A change of engine options rebuilds most
of the engine.

```
sudo -iu collabora ~collabora/perf-dashboard/deploy/bootstrap --reconfigure
```

**Busy runs.** On a host that runs other work, a whole run can be slower than
usual, and every chart then has a spike at the same commit. During the run a
probe does a fixed amount of work every 20 seconds, and the run reads the CPU
steal time of the VM. Both go in `machine.json` in the run directory, and the
run detail of the dashboard shows them. A busy run is published with a busy
mark: its timings stay off the lines of the charts as faded dots, while its
memory and instruction counts, which a busy machine barely changes, stay on
them. The next hour measures the same commit again for its timings. Pinning the VM to its own cores on
one NUMA node of the host makes busy runs rare:

```
incus config set cool-perf limits.cpu=<cores of one node, both threads of each>
incus config set cool-perf limits.cpu.nodes=<that node>
incus restart cool-perf
```

**Why the engine is rebuilt at each run.** The engine is in the same
repository as the server, so a commit on `main` can change both. A server built
against an older engine can crash the kit, and its numbers then measure a mix
of two commits. The engine build is incremental and goes through
ccache, so it usually takes a minute or two. After a change to a header that
many files include, it can take an hour or more. `COOL_PERF_ENGINE=0` skips
it.

**How a run is labelled.** `cool-build` writes the commit it built into
`/home/collabora/online/.cool-build-installed`, and `cool-ci` labels the run
with it. Each point on the dashboard therefore names the commit of `main` that
was measured.

## Day to day

| task | command |
| --- | --- |
| run now | `sudo systemctl start cool-perf.service` |
| follow a run | `journalctl -u cool-perf.service -f` |
| the last runs | `journalctl -u cool-perf.service --since -3d` |
| when it runs next | `systemctl list-timers cool-perf.timer` |
| pause the runs | `sudo systemctl disable --now cool-perf.timer` |
| resume them | `sudo systemctl enable --now cool-perf.timer` |
| check the machine | `sudo -iu collabora sh -c 'set -a; . /etc/cool-perf.env; ~/perf-dashboard/cool-ci doctor'` |
| the last published run | `cat /var/lib/cool-perf/last-run.json` |

The logs of each run are kept in the store, under
`/var/lib/cool-perf/results/<run>/`, and the JUnit report under
`/var/lib/cool-perf/junit/`. The run's work directory is
`/home/collabora/perf-dashboard/results/`.

**Measuring a change or a branch.** `deploy/run-branch` builds a change under
review, or any ref, measures it, and publishes its runs as a branch of their own.
The charts show main by default, and the "branch" selector shows another
branch. "Compare two builds", above the charts, puts two sides against each
other, each a measured commit or a range of them on one branch, and lists the
metrics that moved past the noise: green when B is lower, red when higher, and
lighter for a small change. It starts on the newest change against main at the
commit the change is based on, which deploy/run-branch records. The choices are
kept in the address, so a comparison can be sent as a link. Wait for the run of the timer to end, or stop the timer, then:

```
sudo -iu collabora /home/collabora/perf-dashboard/deploy/run-branch --gerrit 18591 --runs 3
```

**Queuing a change from the dashboard.** The Schedule tab shows the runs of the
last and the next 12 hours: the runs of main, the queued Gerrit changes and the
units started by hand with systemd-run. Its form queues a change, as `18591`, `18591/3`
or the address of the change, with the number of runs and a time to start from.
`cool-perf-queue.service` runs `deploy/run-queue`, which takes the queued changes
one at a time once they are due and runs `deploy/run-branch --gerrit <change>/<patch
set> --runs <n> --wait` for each, with the settings of cool-perf.service, so a
change and a run of the timer never measure at the same time. The tab links the
log of each job; `journalctl -u cool-perf-queue.service` lists the jobs.

A queued change builds and runs the code of its patch set on the VM, and the
dashboard has no login, so `deploy/runs-api.py` accepts a change of online only
when its patch set was uploaded by an account of `COOL_PERF_SCHEDULE_DOMAINS`
(default `collabora.com,collabora.co.uk`), and queues that patch set, not
whatever is uploaded later. The queue is under
`/home/collabora/.local/state/cool-perf/queue/`, one JSON file and one log per
job, and keeps the 30 newest finished jobs. A queued job can be cancelled from
the tab. To stop the job that is running, restart the service, which stops the
job's coolwsd with it and marks the job failed:

```
sudo systemctl restart cool-perf-queue.service
```

**Queuing a capacity run from the dashboard.** The form of the Capacity tab
queues one capacity run of a scenario, with a flamegraph or a heap profile, of
a Gerrit change or of a branch, tag or commit of online, such as
`origin/main`. The same rules apply to a change. `deploy/run-queue` runs
`deploy/run-capacity --wait` with `--gerrit` or `--ref` for it, and a heap
profile with `HEAPTRACK=$COOL_PERF_HEAPTRACK`, `CAP_VUS=10` and
`CAP_HOLD=180s`. capacity-profile moves the processes of the service into a
cgroup of its own on the cores that it leaves to k6, so after a capacity job
the runner gives that cgroup every core again, and the next jobs measure as
before.

To run it later from the command line, schedule it with systemd-run, with the settings of
cool-perf.service. `--wait` has it start once a run of the timer has ended, rather
than give up, and the time limit leaves room for that wait:

```
sudo systemd-run --unit=cool-perf-gerrit-18591 --on-calendar='02:30' \
  -p User=collabora -p Group=collabora -p EnvironmentFile=/etc/cool-perf.env \
  -p WorkingDirectory=/home/collabora/perf-dashboard \
  -p TimeoutStartSec=12h -p KillMode=control-group -p Delegate=yes \
  /home/collabora/perf-dashboard/deploy/run-branch --gerrit 18591 --runs 3 --wait
```

`--gerrit` takes the current patch set of the change from Gerrit, or one patch
set as `18591/2`, and names the branch `gerrit-18591`. `--name` and `--ref`
measure anything else, with `--from` for a remote other than origin. A change to
the engine needs `COOL_PERF_ENGINE=1`, the default. The engine build is
incremental and goes through ccache, so a change to a few engine files costs
their compilation and the link of `libmergedlo.so`. The tree keeps the build
until main next moves, and the next run of main builds main again the same way.

**Measuring capacity.** `deploy/run-capacity` runs the staircase test of
`src/cool-capacity-test.js` through `capacity-profile` against the build in the
tree: steps of 10 users from 10 to 160, each typing in the documents of the
scenario, with 20 seconds of ramp and 70 seconds held at each step, so about 25
minutes, or less when a step fails. It records a flamegraph of coolwsd and every kit, the timeline of
memory and CPU, and the edit latency of each user, and one real browser user
joins at the last step. It holds the lock of the timer and stops when `cool-ci doctor` fails, and
its results go to `/var/lib/cool-perf/capacity/<time>-<commit>/`, which the
dashboard host lists under `/capacity/`, and the Capacity tab of the dashboard
shows each run: its stages, and the users, edit latency, CPU and memory over
time. Schedule it like a branch run:

```
sudo systemd-run --unit=cool-perf-capacity --collect \
  -p User=collabora -p Group=collabora -p EnvironmentFile=/etc/cool-perf.env \
  -p WorkingDirectory=/home/collabora/perf-dashboard \
  -p TimeoutStartSec=12h -p KillMode=control-group -p Delegate=yes \
  /home/collabora/perf-dashboard/deploy/run-capacity --wait
```

Scenario names after `--wait` run the test once for each, one after the
other, and `all` runs every one, about four hours:

- `writer`, `calc`, `impress`: the 5 MB ODF and OOXML documents of one
  application, one user each.
- `writer-pairs`, `calc-pairs`, `impress-pairs`: the same documents, with half
  of the users editing a document two by two.
- `mixed`: in every 10 documents, one each of odt, docx, ods, xlsx, odp and
  pptx with one user, then 4 more with two users each.
- `writer-shared`, `calc-shared`, `impress-shared`: every user on one 5 MB
  document. One user in 10 edits, each in a paragraph, a column or a slide of
  its own, and the 9 users after it view the part where it types.
  `impress-shared` opens `documents/impress-slides-5mb.odp`, 40 slides, which
  `python3 documents/gen-docs.py --slide-deck-only` writes on a VM set up
  before it.
- `finance-shared`: every user on `documents/calc-finance.ods`, the sales
  ledger of a company over three years (12000 order lines with formulas), a
  pivot table of its revenue, and four charts over SUMIFS tables of it. One
  user in 10 types numbers in the Units column of the ledger, each in a block
  of rows of its own, which recalculates the tables and redraws the charts.
  The 9 users after it view the ledger where it types, the pivot table or the
  charts, a third of them each. Users take zoom levels from 75% to 200%
  (CAP_ZOOMS).

The Capacity tab names the scenario of each run.

A run that finds the limit exits with an error, because a step missed its
targets or the host ran short of memory. `--collect` removes the unit anyway,
so the next run can use the same name, and `journalctl -u cool-perf-capacity`
still shows its log.

coolwsd, forkit and the kits run in a cgroup of their own, limited to cores 0
to 3 and 8 GB of memory with no swap, the size of a small server.
`SERVER_CPUS` and `SERVER_MEMORY` change the limits, which a run always has: a
capacity is a number of users on a server of a known size. k6, perf, the
samplers and the browser user run on the other cores, so the server has its
cores to itself.
`server-cgroup.txt` in the results gives the peak memory of the server and how
often the kernel killed a process in it for lack of memory. perf counts the
instructions, cycles and CPU time of the whole server every 5 seconds into
`perf-stat.csv`, so that each step of users can be read off, with the total in
`perf-stat-total.txt`. Every `CAP_*`
variable of the test passes through, set with
`-p Environment=CAP_VUS=10,20,40`, and `NO_BROWSER=1` leaves out the browser
user. `capacity.json` names the capacity, the largest step that met every
target, and `python3 latency-spikes.py <run dir>` lines the slow edits up with
the timeline.

**Heap profile of the server.** With `HEAPTRACK` set to a heaptrack that has
`--follow-children`, a capacity run records every allocation of coolwsd,
forkit, the kits and their background saves instead of a perf profile.
`heap-report.py` then writes `heap.html` beside the other results, with the
allocations, the temporary allocations, the peak and the leaks of each kind
of process, a flamegraph of each, and the full stacks of the busiest process
of each kind. The Capacity tab marks the run as a heap profile and links its
page. heaptrack slows the server down several times, so give such a run one
short step. Its edit and load targets are 4 times their default, which
`CAP_TARGET_FACTOR` changes:

```
sudo systemd-run --unit=cool-perf-heap \
  -p User=collabora -p Group=collabora \
  -p EnvironmentFile=/etc/cool-perf.env \
  -p WorkingDirectory=/home/collabora/perf-dashboard \
  -p Delegate=yes -p TimeoutStartSec=12h \
  -p KillMode=control-group \
  -p Environment=HEAPTRACK=/opt/heaptrack/bin/heaptrack \
  -p Environment=CAP_VUS=10 -p Environment=CAP_HOLD=180s \
  deploy/run-capacity --wait finance-shared
```

A capacity run logs at warning level. To see why the server behaves
differently under heaptrack, add `-p Environment=CAP_LOG_LEVEL=debug`, which
keeps that level for coolwsd and the kits.

Every process writes its raw trace into `heaptrack/` of the run while it
runs, about 200 MB a minute for a kit that a user edits, and
`heap-report.py` deletes them once it has drawn the flamegraphs. Interpreting
them takes a few minutes after k6 ends. heaptrack also makes each process
larger, so a scenario with many documents may need a larger `SERVER_MEMORY`.

`--follow-children` is on the branch `work/meven/follow-children` of
heaptrack. To build it into `/opt/heaptrack`:

```
sudo apt-get install -y libunwind-dev libdw-dev zstd \
  libboost-iostreams-dev libboost-program-options-dev \
  libboost-filesystem-dev libboost-container-dev
git clone -b work/meven/follow-children \
  https://invent.kde.org/sdk/heaptrack.git heaptrack
cmake -S heaptrack -B heaptrack/build \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DHEAPTRACK_BUILD_GUI=OFF \
  -DCMAKE_INSTALL_PREFIX=/opt/heaptrack
cmake --build heaptrack/build -j4
sudo cmake --install heaptrack/build
```

**Updating perf-dashboard.** Pull the new commit (see step 1), then run
`deploy/setup-vm --no-packages` as root if anything in `deploy/` changed. The
next run rebuilds the test bundles by itself, because they are rebuilt whenever
the checkout is at a new commit.

**Measuring an older commit.** `cool-build --ref <commit>` builds it in the
tree, and `cool-ci run` measures it. Run both as `collabora` with the
environment loaded, as in the `doctor` line above. The next run builds
`main` again.

**Disk.** The store grows by about 8 MB a run up to `COOL_PERF_KEEP` runs.
The compiler cache in `/home/collabora/.cache/ccache` grows up to its 50 GB
limit, which holds main beside the builds of changes and release tags. A full
cache evicts the objects of main while another branch builds, and the next
build of main then compiles them again. `ccache -s` shows how full it is, and
`ccache --max-size` changes the limit. With
`COOL_PERF_SAVE_BUILDS=1`, each run adds 4 GB under `COOL_BUILDS_DIR`, and
nothing removes them, so delete old ones with `cool-use-build list` and `rm`.

**Backups.** Only `/var/lib/cool-perf` holds results that cannot be rebuilt.
Copy it elsewhere now and then, for example with
`incus file pull -r cool-perf/var/lib/cool-perf <backup dir>`.

## Troubleshooting

| symptom | cause and fix |
| --- | --- |
| `apt-get build-dep` says it cannot find a source package | the source lists are not enabled. `setup-vm` enables them in `/etc/apt/sources.list.d/ubuntu.sources`; check that the file has `Types: deb deb-src` |
| `no space left on device`, often in `docker pull` | the disk is the incus default of 10 GB. Grow it, see [The machine](#the-machine) |
| engine downloads hang on `Connecting to ...\|2a01:...\|:443` | the machine has IPv6 but no IPv6 route out. Run `setup-vm` again, or add `precedence ::ffff:0:0/96  100` to `/etc/gai.conf` |
| `bootstrap` stops in the engine `autogen.sh` | a build dependency is missing, and the message names it. Install it, then run `bootstrap` again |
| the engine build is killed with no error | the VM ran out of memory while linking. Give it more memory, or set `COOL_BUILD_JOBS` lower in `/etc/cool-perf.env` |
| `doctor` fails on `docker daemon` | the user was added to `docker` after the shell started. Leave the `collabora` shell and enter it again |
| `doctor` fails on `user namespaces` or `apparmor_restrict_unprivileged_userns` | the sysctl file was not applied. `sudo sysctl -p /etc/sysctl.d/90-cool-perf.conf`. In a container, set it on the host |
| the run fails in `git pull` | `COOL_PERF_SELF_UPDATE=1`, but the checkout has no remote that it can read without a prompt. Store the deploy token (step 1), or set it back to 0 |
| every document fails, `coolwsd.log` ends at the banner | the k6 container cannot reach the WOPI host. See the same row in `CI-JENKINS.md` |
| the run stops at `the first build is not done` | `bootstrap` has not finished yet. Run it to the end, it writes `~/.local/state/cool-perf/bootstrapped` |
| `No route to host` from the incus host, while `curl http://127.0.0.1/` in the VM answers | the VM is on a macvlan network, which the host cannot reach. Add a bridge device, see [Seeing the dashboard](#seeing-the-dashboard) |
| the dashboard is an empty file list | no run was published yet. Run the job once |

More rows for the suite itself are in the troubleshooting section of
`CI-JENKINS.md`.

## Removing it

```
systemctl disable --now cool-perf.timer
rm /etc/systemd/system/cool-perf.{service,timer} /etc/cool-perf.env
rm /etc/nginx/sites-enabled/cool-perf.conf /etc/nginx/sites-available/cool-perf.conf
rm /etc/sysctl.d/90-cool-perf.conf
gpasswd -d collabora docker
rm -r ~collabora/online ~collabora/perf-dashboard ~collabora/cool-builds ~collabora/.local/state/cool-perf
rm -r /var/lib/cool-perf     # the results: copy them first
```
