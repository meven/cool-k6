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
