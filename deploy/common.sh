# Steps shared by the scripts in deploy/ that measure outside the timer, sourced from the root of
# the checkout after /etc/cool-perf.env.

# Take the lock of deploy/nightly on file descriptor 9, so that no other run measures during this
# one. With $1 set, wait for the run that holds the lock to end; another run that starts
# meanwhile waits in turn. lock-holder beside the lock then gives the pid of this script and the
# time it took the lock, which the Schedule tab shows as the start of the measuring.
take_lock() {
    local state=${XDG_STATE_HOME:-$HOME/.local/state}/cool-perf
    exec 9>"$state/lock"
    if [ -n "${1:-}" ]; then
        flock -n 9 || { echo "=== waiting for the run that holds the lock to end"; flock 9; }
    else
        flock -n 9 || { echo "another run holds the lock; wait for it, or give --wait" >&2
                        exit 1; }
    fi
    echo "$$ $(date +%s)" > "$state/lock-holder"
    # cool-build then knows that its caller holds the lock already.
    export COOL_PERF_LOCKED=1
    lock_held=1
    lock_step "preparing"
}

# Record the step that the holder of the lock is at, such as "building" or "measuring, run 2 of
# 3", in lock-step beside lock-holder: the pid of this script, the time the step started and its
# text. The Schedule tab shows the step beside a running job. A script that does not hold the lock
# records nothing.
lock_step() {
    [ -n "${lock_held:-}" ] || return 0
    local state=${XDG_STATE_HOME:-$HOME/.local/state}/cool-perf
    echo "$$ $(date +%s) $*" > "$state/lock-step"
}

# The ref of a patch set of a Gerrit change, "<number>" for its current patch set or
# "<number>/<patch set>", from the Gerrit REST API, which prefixes its JSON with a line against
# cross-site inclusion.
GERRIT_URL=${COOL_GERRIT_URL:-https://gerrit.collaboraoffice.com}
gerrit_ref() {
    local number=${1%%/*} patchset= ref
    [ "$number" != "$1" ] && patchset=${1#*/}
    ref=$(curl -sf "$GERRIT_URL/changes/$number?o=ALL_REVISIONS" | python3 -c '
import json, sys
c = json.loads(sys.stdin.read().split("\n", 1)[1])
want = sys.argv[1]
for sha, rev in c["revisions"].items():
    if (want and str(rev["_number"]) == want) or (not want and sha == c["current_revision"]):
        print(rev["ref"])
' "$patchset")
    [ -n "$ref" ] || { echo "no patch set ${patchset:-current} of change $number on $GERRIT_URL" >&2
                       return 1; }
    echo "$ref"
}

# The test documents, generated again when documents/gen-docs.py changed, so that a run measures
# the documents that the generator writes now. The generator draws from a fixed seed, so the
# documents that its change leaves alone come out the same. It also rewrites the blank documents
# that git tracks, which are restored, so that the next git pull applies. Call it with the lock
# held, as no run may load a document while it is written.
build_documents() {
    local gen
    gen=$(git rev-parse HEAD:documents/gen-docs.py)
    if [ ! -f documents/.built-from ] || [ "$(cat documents/.built-from)" != "$gen" ]; then
        echo "=== test documents for gen-docs.py $gen"
        python3 documents/gen-docs.py
        python3 documents/gen-docs.py --apps writer --mb 10 --report --no-ooxml --no-image
        python3 documents/gen-docs.py --slide-deck-only
        git checkout -- 'documents/blank.*'
        echo "$gen" > documents/.built-from
    fi
}

# The test bundles, rebuilt when the checkout moved, as deploy/nightly does.
build_bundles() {
    local head
    head=$(git rev-parse HEAD)
    if [ ! -f dist/.built-from ] || [ "$(cat dist/.built-from)" != "$head" ]; then
        npm ci --no-audit --no-fund
        npm run build
        echo "$head" > dist/.built-from
    fi
}
