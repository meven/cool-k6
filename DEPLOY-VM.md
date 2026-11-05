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
| `/etc/nginx/sites-available/cool-perf.conf` | serves `/var/lib/cool-perf` on port 80 |
| `/etc/sysctl.d/90-cool-perf.conf` | the two kernel settings the suite needs |
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
| disk | 150 GB | engine build with symbols (about 60 GB), ccache (30 GB), online, the store, the k6 image |

On incus, a VM with those resources:

```
incus launch images:ubuntu/26.04 cool-perf --vm \
    -c limits.cpu=8 -c limits.memory=32GiB -d root,size=150GiB
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
  the suite's coolwsd, `kernel.perf_event_mlock_kb = 32768` gives the dwarf
  call graphs their ring buffer, and `kernel.apparmor_restrict_unprivileged_userns = 0` lets the document kit
  build its jail. Ubuntu restricts both by default
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
publish it only on a network where everyone may see it.

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
| `COOL_PERF_PROBE_MAX` | `1.2` | a run is busy when the machine probe is slower than usual by this factor |
| `COOL_PERF_STEAL_MAX` | `2` | a run is busy when the hypervisor took more than this percentage of the CPU time |
| `COOL_PERF_BUSY_RETRIES` | `3` | busy runs of a commit that are not published before one is published with a busy mark |
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
because a measurement never loads them.

The server is configured with `--enable-debug` and `-O2 -g`. The debug build is
required: `cool-ci` starts coolwsd with `--find-free-port`, and the flamegraph
pass adds `--singlekit`, and coolwsd only accepts these two options in a debug
build. The `-O2` keeps the code optimised; `--enable-developer` would compile
it at `-O0`. A debug coolwsd also logs at trace level, which costs some time in
every number, but costs it the same way in every run.

`ENGINE_AUTOGEN_ARGS` and `ONLINE_AUTOGEN_ARGS` replace these options when
`bootstrap` is run. To change the configuration of a tree that is already
configured, run `autogen.sh` in it by hand, or delete
`/home/collabora/online/engine/config_host.mk` or `/home/collabora/online/Makefile`
and run `bootstrap` again.

**Busy runs.** On a host that runs other work, a whole run can be slower than
usual, and every chart then has a spike at the same commit. During the run a
probe does a fixed amount of work every 20 seconds, and the run reads the CPU
steal time of the VM. Both go in `machine.json` in the run directory, and the
run detail of the dashboard shows them. A busy run is not published, so the
next hour measures the same commit again. Pinning the VM to its own cores on
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

To run it later, schedule it with systemd-run, with the settings of
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

**Updating perf-dashboard.** Pull the new commit (see step 1), then run
`deploy/setup-vm --no-packages` as root if anything in `deploy/` changed. The
next run rebuilds the test bundles by itself, because they are rebuilt whenever
the checkout is at a new commit.

**Measuring an older commit.** `cool-build --ref <commit>` builds it in the
tree, and `cool-ci run` measures it. Run both as `collabora` with the
environment loaded, as in the `doctor` line above. The next run builds
`main` again.

**Disk.** The store grows by about 8 MB a run up to `COOL_PERF_KEEP` runs.
The compiler cache in `/home/collabora/.cache/ccache` grows up to its 30 GB
limit. With
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
